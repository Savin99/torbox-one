"""Local SQLite store for IMDb official ratings dataset (title.ratings.tsv.gz).

Streams https://datasets.imdbws.com/title.ratings.tsv.gz into a temporary SQLite
database (WITHOUT ROWID), atomically swaps it into place via os.replace(), and
serves thread-safe <0.2ms lookups by IMDb tconst ID without external API quota.
"""

from __future__ import annotations

import argparse
import gzip
import logging
import math
import os
import sqlite3
import threading
import time
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any, TypeVar

import requests

LOG = logging.getLogger("imdb_ratings")

IMDB_RATINGS_URL = "https://datasets.imdbws.com/title.ratings.tsv.gz"
DEFAULT_TTL_SECONDS = 24 * 3600
BATCH_SIZE = 50_000
LOOKUP_CHUNK_SIZE = 500

_T = TypeVar("_T")


def _normalize_imdb_id(imdb_id: Any) -> str:
    tconst = str(imdb_id or "").strip().lower()
    if not tconst.startswith("tt"):
        return ""
    return tconst


def _parse_tsv_line(raw_line: bytes) -> tuple[str, float, int] | None:
    line = raw_line.decode("utf-8", errors="replace").rstrip("\r\n")
    if not line:
        return None
    parts = line.split("\t")
    if len(parts) != 3:
        return None
    tconst = parts[0].strip().lower()
    if tconst == "tconst" or len(tconst) <= 2 or not tconst.startswith("tt"):
        return None
    try:
        rating = float(parts[1])
        votes = int(parts[2])
    except ValueError:
        return None
    if not math.isfinite(rating) or rating < 0.0 or votes < 0:
        return None
    return (tconst, rating, votes)


def _open_readonly(db_path: Path) -> sqlite3.Connection:
    uri = f"{db_path.resolve().as_uri()}?mode=ro"
    conn = sqlite3.connect(uri, uri=True, check_same_thread=False, timeout=5.0)
    conn.execute("PRAGMA query_only = ON")
    return conn


class _CachedConn:
    __slots__ = ("conn", "sig", "lock", "closed")

    def __init__(
        self,
        conn: sqlite3.Connection,
        sig: tuple[int, int, int, int, int],
    ) -> None:
        self.conn = conn
        self.sig = sig
        self.lock = threading.Lock()
        self.closed = False


class ImdbRatingsStore:
    """Thread-safe local SQLite mirror of IMDb title.ratings.tsv.gz."""

    def __init__(
        self,
        db_path: Path | str,
        url: str = IMDB_RATINGS_URL,
        ttl_seconds: int = DEFAULT_TTL_SECONDS,
        timeout: float = 60.0,
    ) -> None:
        self.db_path = Path(db_path)
        self.url = url
        self.ttl_seconds = ttl_seconds
        self.timeout = timeout

        self._refresh_lock = threading.Lock()
        self._conn_lock = threading.Lock()
        self._generation = 0
        self._conns: dict[int, _CachedConn] = {}

        self._bg_lock = threading.Lock()
        self._bg_thread: threading.Thread | None = None
        self._stop_event = threading.Event()

    def _close_entry(self, entry: _CachedConn) -> None:
        with entry.lock:
            if not entry.closed:
                entry.closed = True
                try:
                    entry.conn.close()
                except Exception:
                    pass

    def _evict_thread_connection(self, tid: int) -> None:
        with self._conn_lock:
            old = self._conns.pop(tid, None)
        if old is not None:
            self._close_entry(old)

    def _evict_connection(self, target: _CachedConn) -> None:
        tid = threading.get_ident()
        with self._conn_lock:
            current = self._conns.get(tid)
            if current is target:
                self._conns.pop(tid, None)
        self._close_entry(target)

    def _invalidate_connections(self) -> None:
        with self._conn_lock:
            self._generation += 1
            old_entries = list(self._conns.values())
            self._conns.clear()
        for entry in old_entries:
            self._close_entry(entry)

    def _acquire_connection(self) -> _CachedConn | None:
        tid = threading.get_ident()
        for _ in range(3):
            try:
                st = os.stat(self.db_path)
            except OSError:
                self._evict_thread_connection(tid)
                return None

            with self._conn_lock:
                gen = self._generation
                sig = (gen, st.st_dev, st.st_ino, st.st_mtime_ns, st.st_size)
                existing = self._conns.get(tid)
                if existing is not None and existing.sig == sig and not existing.closed:
                    return existing

            try:
                conn = _open_readonly(self.db_path)
            except sqlite3.Error as exc:
                LOG.warning("Failed to open IMDb ratings DB %s: %s", self.db_path, exc)
                return None

            new_entry = _CachedConn(conn, sig)
            old_entry: _CachedConn | None = None
            stale_open = False
            with self._conn_lock:
                if self._generation != gen:
                    stale_open = True
                else:
                    old_entry = self._conns.get(tid)
                    self._conns[tid] = new_entry

            if old_entry is not None:
                self._close_entry(old_entry)

            if stale_open:
                self._close_entry(new_entry)
                continue
            return new_entry
        return None

    def _run_read(self, fn: Callable[[sqlite3.Connection], _T]) -> _T | None:
        for attempt in range(3):
            entry = self._acquire_connection()
            if entry is None:
                return None
            try:
                with entry.lock:
                    if entry.closed:
                        continue
                    return fn(entry.conn)
            except sqlite3.Error as exc:
                self._evict_connection(entry)
                if attempt < 2:
                    continue
                LOG.warning("IMDb ratings read failed for %s: %s", self.db_path, exc)
                return None
        return None

    def status(self) -> dict[str, Any]:
        def _read_meta(conn: sqlite3.Connection) -> dict[str, str]:
            rows = conn.execute(
                "SELECT key, value FROM meta WHERE key IN ('updated_at', 'count')"
            ).fetchall()
            return {str(k): str(v) for k, v in rows}

        meta = self._run_read(_read_meta)
        if not meta:
            return {
                "loaded": False,
                "count": 0,
                "updated_at": 0.0,
                "stale": True,
            }
        try:
            updated_at = float(meta.get("updated_at", "0"))
            count = int(meta.get("count", "0"))
        except ValueError:
            return {
                "loaded": False,
                "count": 0,
                "updated_at": 0.0,
                "stale": True,
            }
        loaded = count > 0 and updated_at > 0.0
        stale = (not loaded) or ((time.time() - updated_at) >= self.ttl_seconds)
        return {
            "loaded": loaded,
            "count": count if loaded else 0,
            "updated_at": updated_at if loaded else 0.0,
            "stale": stale,
        }

    def refresh(self, force: bool = False) -> bool:
        with self._refresh_lock:
            if not force:
                st = self.status()
                if st["loaded"] and not st["stale"]:
                    return False

            tmp_path = self.db_path.with_suffix(f".sqlite.tmp.{os.getpid()}")
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
            if tmp_path.exists():
                tmp_path.unlink()

            conn: sqlite3.Connection | None = None
            try:
                with requests.get(self.url, stream=True, timeout=self.timeout) as resp:
                    resp.raise_for_status()
                    conn = sqlite3.connect(str(tmp_path), isolation_level=None)
                    conn.execute("PRAGMA synchronous = OFF")
                    conn.execute("PRAGMA journal_mode = OFF")
                    conn.execute("PRAGMA temp_store = MEMORY")
                    conn.execute("PRAGMA cache_size = -64000")
                    conn.execute("BEGIN")
                    conn.execute(
                        "CREATE TABLE ratings ("
                        "tconst TEXT PRIMARY KEY, "
                        "rating REAL NOT NULL, "
                        "votes INTEGER NOT NULL"
                        ") WITHOUT ROWID"
                    )
                    conn.execute(
                        "CREATE TABLE meta ("
                        "key TEXT PRIMARY KEY, "
                        "value TEXT NOT NULL"
                        ")"
                    )

                    batch: list[tuple[str, float, int]] = []
                    insert_sql = "INSERT OR REPLACE INTO ratings (tconst, rating, votes) VALUES (?, ?, ?)"
                    with gzip.GzipFile(fileobj=resp.raw, mode="rb") as gz:
                        for raw_line in gz:
                            row = _parse_tsv_line(raw_line)
                            if row is None:
                                continue
                            batch.append(row)
                            if len(batch) >= BATCH_SIZE:
                                conn.executemany(insert_sql, batch)
                                batch.clear()
                    if batch:
                        conn.executemany(insert_sql, batch)
                        batch.clear()

                    count = int(conn.execute("SELECT COUNT(*) FROM ratings").fetchone()[0])
                    if count <= 0:
                        raise ValueError("IMDb ratings dataset contained 0 valid rows")

                    updated_at = time.time()
                    conn.executemany(
                        "INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)",
                        [
                            ("updated_at", repr(updated_at)),
                            ("count", str(count)),
                        ],
                    )
                    conn.execute("COMMIT")
                    conn.close()
                    conn = None

                fd = os.open(tmp_path, os.O_RDONLY)
                try:
                    os.fsync(fd)
                finally:
                    os.close(fd)

                os.replace(tmp_path, self.db_path)
                self._invalidate_connections()
                LOG.info("IMDb ratings refreshed: %d rows -> %s", count, self.db_path)
                return True
            except Exception:
                if conn is not None:
                    try:
                        conn.close()
                    except Exception:
                        pass
                try:
                    if tmp_path.exists():
                        tmp_path.unlink()
                except OSError:
                    pass
                raise

    def lookup(self, imdb_id: str) -> tuple[float, int] | None:
        tconst = _normalize_imdb_id(imdb_id)
        if not tconst:
            return None

        def _query(conn: sqlite3.Connection) -> tuple[float, int] | None:
            row = conn.execute(
                "SELECT rating, votes FROM ratings WHERE tconst = ?",
                (tconst,),
            ).fetchone()
            if row is None:
                return None
            return (float(row[0]), int(row[1]))

        return self._run_read(_query)

    def lookup_many(self, imdb_ids: Iterable[str]) -> dict[str, tuple[float, int]]:
        normalized: list[str] = []
        seen: set[str] = set()
        for raw_id in imdb_ids:
            tconst = _normalize_imdb_id(raw_id)
            if not tconst or tconst in seen:
                continue
            seen.add(tconst)
            normalized.append(tconst)
        if not normalized:
            return {}

        def _query(conn: sqlite3.Connection) -> dict[str, tuple[float, int]]:
            result: dict[str, tuple[float, int]] = {}
            for offset in range(0, len(normalized), LOOKUP_CHUNK_SIZE):
                chunk = normalized[offset : offset + LOOKUP_CHUNK_SIZE]
                placeholders = ",".join("?" for _ in chunk)
                rows = conn.execute(
                    f"SELECT tconst, rating, votes FROM ratings WHERE tconst IN ({placeholders})",
                    chunk,
                ).fetchall()
                for tconst, rating, votes in rows:
                    result[str(tconst)] = (float(rating), int(votes))
            return result

        found = self._run_read(_query)
        return found if found is not None else {}

    def _background_loop(self, check_interval: float) -> None:
        while not self._stop_event.is_set():
            try:
                self.refresh(force=False)
            except Exception:
                LOG.exception("IMDb ratings background refresh failed")
            if self._stop_event.wait(timeout=check_interval):
                break

    def start_background_refresh(self, check_interval: float = 6 * 3600) -> threading.Thread:
        interval = max(0.01, float(check_interval))
        with self._bg_lock:
            if self._bg_thread is not None and self._bg_thread.is_alive():
                return self._bg_thread
            self._stop_event.clear()
            thread = threading.Thread(
                target=self._background_loop,
                args=(interval,),
                name="imdb-ratings-refresh",
                daemon=True,
            )
            self._bg_thread = thread
            thread.start()
            return thread

    def close(self) -> None:
        self._stop_event.set()
        self._invalidate_connections()


def _build_arg_parser() -> argparse.ArgumentParser:
    default_state_dir = Path(os.getenv("SEARCH_API_STATE_DIR", "").strip() or ".")
    default_db = os.getenv("IMDB_RATINGS_DB_PATH", "").strip() or str(default_state_dir / "imdb_ratings.sqlite")
    parser = argparse.ArgumentParser(description="Download and inspect local IMDb ratings SQLite store.")
    parser.add_argument("--db", default=default_db, help="Path to imdb_ratings.sqlite")
    parser.add_argument("--url", default=IMDB_RATINGS_URL, help="IMDb ratings TSV.GZ dataset URL")
    parser.add_argument("--ttl", type=int, default=DEFAULT_TTL_SECONDS, help="Freshness TTL in seconds")
    parser.add_argument("--timeout", type=float, default=60.0, help="HTTP timeout in seconds")
    parser.add_argument("--force", action="store_true", help="Force refresh even if DB is fresh")
    parser.add_argument("--status-only", action="store_true", help="Only print status without refreshing")
    parser.add_argument("--lookup", nargs="*", default=[], help="Optional IMDb IDs to look up after refresh")
    return parser


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    args = _build_arg_parser().parse_args(argv)
    store = ImdbRatingsStore(
        db_path=args.db,
        url=args.url,
        ttl_seconds=args.ttl,
        timeout=args.timeout,
    )
    try:
        if not args.status_only:
            t0 = time.perf_counter()
            refreshed = store.refresh(force=args.force)
            elapsed = time.perf_counter() - t0
            print(f"refreshed={refreshed} elapsed={elapsed:.2f}s")
        st = store.status()
        size_bytes = store.db_path.stat().st_size if store.db_path.exists() else 0
        print(
            f"status: loaded={st['loaded']} count={st['count']} "
            f"updated_at={st['updated_at']:.1f} stale={st['stale']} "
            f"size_bytes={size_bytes}"
        )
        for imdb_id in args.lookup:
            t1 = time.perf_counter()
            val = store.lookup(imdb_id)
            dt_ms = (time.perf_counter() - t1) * 1000.0
            print(f"lookup {imdb_id}: {val} ({dt_ms:.3f} ms)")
    finally:
        store.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
