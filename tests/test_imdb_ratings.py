"""Offline unit tests for imdb_ratings.ImdbRatingsStore. No network access."""

from __future__ import annotations

import contextlib
import gzip
import io
import sqlite3
import tempfile
import threading
import time
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

import requests

import imdb_ratings


def _make_gzip_bytes(text: str) -> bytes:
    buf = io.BytesIO()
    with gzip.GzipFile(fileobj=buf, mode="wb") as gz:
        gz.write(text.encode("utf-8"))
    return buf.getvalue()


class _FakeResponse:
    def __init__(
        self,
        payload: bytes,
        status_code: int = 200,
        raise_on_status: Exception | None = None,
    ) -> None:
        self.raw = io.BytesIO(payload)
        self.status_code = status_code
        self._raise_on_status = raise_on_status
        self.closed = False

    def raise_for_status(self) -> None:
        if self._raise_on_status is not None:
            raise self._raise_on_status
        if self.status_code >= 400:
            raise requests.HTTPError(f"HTTP {self.status_code}")

    def close(self) -> None:
        self.closed = True

    def __enter__(self) -> _FakeResponse:
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        self.close()


class ImdbRatingsStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.db_path = Path(self._tmp.name) / "imdb_ratings.sqlite"

    def test_missing_db_returns_none_empty_and_unloaded_status(self) -> None:
        store = imdb_ratings.ImdbRatingsStore(self.db_path)
        self.addCleanup(store.close)

        self.assertIsNone(store.lookup("tt0944947"))
        self.assertIsNone(store.lookup(""))
        self.assertIsNone(store.lookup("nm0000001"))
        self.assertEqual(store.lookup_many(["tt0944947", "tt3915174"]), {})
        self.assertEqual(
            store.status(),
            {"loaded": False, "count": 0, "updated_at": 0.0, "stale": True},
        )

    def test_refresh_parses_gzip_tsv_skips_malformed_and_serves_lookups(self) -> None:
        tsv = "\n".join(
            [
                "tconst\taverageRating\tnumVotes",
                "tt0944947\t9.2\t2350000",
                "TT3915174\t7.8\t195000",
                "tt0230011\t6.9\t132000",
                "",
                "bad_line_without_tabs",
                "tt1111111\tnot_a_float\t100",
                "tt2222222\t7.5\tnot_an_int",
                "tt3333333\tnan\t100",
                "tt4444444\tinf\t100",
                "tt5555555\t-1.0\t100",
                "tt6666666\t8.0\t-5",
                "nm0000001\t8.0\t100",
                "tt\t8.0\t100",
                "tt7777777\t8.0\t100\textra_col",
                "tt0230011\t7.0\t135000",  # duplicate overwrites cleanly
            ]
        ) + "\n"
        payload = _make_gzip_bytes(tsv)

        store = imdb_ratings.ImdbRatingsStore(self.db_path)
        self.addCleanup(store.close)

        with mock.patch("imdb_ratings.requests.get", return_value=_FakeResponse(payload)) as m_get:
            refreshed = store.refresh()

        self.assertTrue(refreshed)
        m_get.assert_called_once_with(imdb_ratings.IMDB_RATINGS_URL, stream=True, timeout=60.0)
        self.assertTrue(self.db_path.exists())
        self.assertEqual(list(self.db_path.parent.glob("*.tmp.*")), [])

        st = store.status()
        self.assertTrue(st["loaded"])
        self.assertEqual(st["count"], 3)
        self.assertGreater(st["updated_at"], 0.0)
        self.assertFalse(st["stale"])

        self.assertEqual(store.lookup("tt0944947"), (9.2, 2350000))
        self.assertEqual(store.lookup("  TT3915174 \n"), (7.8, 195000))
        self.assertEqual(store.lookup("tt0230011"), (7.0, 135000))
        self.assertIsNone(store.lookup("tt9999999"))
        self.assertIsNone(store.lookup("nm0000001"))
        self.assertIsNone(store.lookup("  "))
        self.assertIsNone(store.lookup(None))  # type: ignore[arg-type]

        many = store.lookup_many(
            ["TT0944947", "tt0944947", " tt3915174 ", "tt9999999", "bad", ""]
        )
        self.assertEqual(
            many,
            {
                "tt0944947": (9.2, 2350000),
                "tt3915174": (7.8, 195000),
            },
        )

    def test_lookup_many_chunks_over_500_ids(self) -> None:
        lines = ["tconst\taverageRating\tnumVotes"]
        for i in range(1, 651):
            lines.append(f"tt{i:07d}\t{(i % 9) + 1}.0\t{i * 10}")
        payload = _make_gzip_bytes("\n".join(lines) + "\n")

        store = imdb_ratings.ImdbRatingsStore(self.db_path)
        self.addCleanup(store.close)
        with mock.patch("imdb_ratings.requests.get", return_value=_FakeResponse(payload)):
            self.assertTrue(store.refresh())

        query_ids = [f"tt{i:07d}" for i in range(1, 651)] + ["tt9999999"]
        found = store.lookup_many(query_ids)
        self.assertEqual(len(found), 650)
        self.assertEqual(found["tt0000001"], (2.0, 10))
        self.assertEqual(found["tt0000650"], (3.0, 6500))
        self.assertNotIn("tt9999999", found)

    def test_ttl_skip_when_fresh_and_force_override(self) -> None:
        payload_v1 = _make_gzip_bytes("tconst\taverageRating\tnumVotes\ntt0944947\t9.2\t2000000\n")
        payload_v2 = _make_gzip_bytes("tconst\taverageRating\tnumVotes\ntt0944947\t9.3\t2100000\n")
        payload_v3 = _make_gzip_bytes("tconst\taverageRating\tnumVotes\ntt0944947\t9.4\t2200000\n")

        store = imdb_ratings.ImdbRatingsStore(self.db_path, ttl_seconds=3600)
        self.addCleanup(store.close)

        with mock.patch("imdb_ratings.requests.get", return_value=_FakeResponse(payload_v1)) as m_get:
            self.assertTrue(store.refresh(force=False))
            self.assertEqual(m_get.call_count, 1)

        # Still fresh -> must skip without downloading
        with mock.patch("imdb_ratings.requests.get", return_value=_FakeResponse(payload_v2)) as m_get:
            self.assertFalse(store.refresh(force=False))
            self.assertEqual(m_get.call_count, 0)
        self.assertEqual(store.lookup("tt0944947"), (9.2, 2000000))

        # force=True bypasses TTL
        with mock.patch("imdb_ratings.requests.get", return_value=_FakeResponse(payload_v2)) as m_get:
            self.assertTrue(store.refresh(force=True))
            self.assertEqual(m_get.call_count, 1)
        self.assertEqual(store.lookup("tt0944947"), (9.3, 2100000))

        # Make DB stale by backdating meta.updated_at -> refresh(force=False) downloads again
        conn = sqlite3.connect(str(self.db_path))
        conn.execute("UPDATE meta SET value = ? WHERE key = 'updated_at'", (str(time.time() - 7200),))
        conn.commit()
        conn.close()

        self.assertTrue(store.status()["stale"])
        with mock.patch("imdb_ratings.requests.get", return_value=_FakeResponse(payload_v3)) as m_get:
            self.assertTrue(store.refresh(force=False))
            self.assertEqual(m_get.call_count, 1)
        self.assertEqual(store.lookup("tt0944947"), (9.4, 2200000))
        self.assertFalse(store.status()["stale"])

    def test_failed_and_empty_downloads_preserve_existing_db(self) -> None:
        good_payload = _make_gzip_bytes("tconst\taverageRating\tnumVotes\ntt0944947\t9.2\t2000000\n")
        store = imdb_ratings.ImdbRatingsStore(self.db_path)
        self.addCleanup(store.close)

        with mock.patch("imdb_ratings.requests.get", return_value=_FakeResponse(good_payload)):
            self.assertTrue(store.refresh())
        original_status = store.status()

        # 1. Network error
        with mock.patch("imdb_ratings.requests.get", side_effect=requests.ConnectionError("boom")):
            with self.assertRaises(requests.ConnectionError):
                store.refresh(force=True)
        self.assertEqual(store.lookup("tt0944947"), (9.2, 2000000))
        self.assertEqual(store.status()["updated_at"], original_status["updated_at"])
        self.assertEqual(list(self.db_path.parent.glob("*.tmp.*")), [])

        # 2. HTTP 503 error
        with mock.patch("imdb_ratings.requests.get", return_value=_FakeResponse(b"", status_code=503)):
            with self.assertRaises(requests.HTTPError):
                store.refresh(force=True)
        self.assertEqual(store.lookup("tt0944947"), (9.2, 2000000))
        self.assertEqual(list(self.db_path.parent.glob("*.tmp.*")), [])

        # 3. Header-only (0 valid rows)
        empty_payload = _make_gzip_bytes("tconst\taverageRating\tnumVotes\n")
        with mock.patch("imdb_ratings.requests.get", return_value=_FakeResponse(empty_payload)):
            with self.assertRaises(ValueError):
                store.refresh(force=True)
        self.assertEqual(store.lookup("tt0944947"), (9.2, 2000000))
        self.assertEqual(store.status()["count"], 1)
        self.assertEqual(list(self.db_path.parent.glob("*.tmp.*")), [])

        # 4. Truncated / corrupt gzip stream
        corrupt_payload = good_payload[:10]
        with mock.patch("imdb_ratings.requests.get", return_value=_FakeResponse(corrupt_payload)):
            with self.assertRaises((EOFError, gzip.BadGzipFile, OSError)):
                store.refresh(force=True)
        self.assertEqual(store.lookup("tt0944947"), (9.2, 2000000))
        self.assertEqual(store.status()["updated_at"], original_status["updated_at"])
        self.assertEqual(list(self.db_path.parent.glob("*.tmp.*")), [])

    def test_atomic_swap_immediately_visible_to_cached_readers(self) -> None:
        payload_v1 = _make_gzip_bytes(
            "tconst\taverageRating\tnumVotes\n"
            "tt0944947\t9.1\t1000\n"
            "tt0000001\t5.0\t50\n"
        )
        payload_v2 = _make_gzip_bytes(
            "tconst\taverageRating\tnumVotes\n"
            "tt0944947\t9.2\t2000\n"
            "tt3915174\t7.8\t3000\n"
        )
        payload_v3 = _make_gzip_bytes(
            "tconst\taverageRating\tnumVotes\n"
            "tt0944947\t9.5\t4000\n"
            "tt0230011\t6.9\t5000\n"
        )

        reader = imdb_ratings.ImdbRatingsStore(self.db_path)
        external_writer = imdb_ratings.ImdbRatingsStore(self.db_path)
        self.addCleanup(reader.close)
        self.addCleanup(external_writer.close)

        # Populate and warm reader cached connection
        with mock.patch("imdb_ratings.requests.get", return_value=_FakeResponse(payload_v1)):
            self.assertTrue(reader.refresh())
        self.assertEqual(reader.lookup("tt0944947"), (9.1, 1000))
        self.assertEqual(reader.lookup("tt0000001"), (5.0, 50))
        self.assertIsNone(reader.lookup("tt3915174"))

        # In-process swap on the same store
        with mock.patch("imdb_ratings.requests.get", return_value=_FakeResponse(payload_v2)):
            self.assertTrue(reader.refresh(force=True))
        self.assertEqual(reader.lookup("tt0944947"), (9.2, 2000))
        self.assertIsNone(reader.lookup("tt0000001"))
        self.assertEqual(reader.lookup("tt3915174"), (7.8, 3000))

        # External swap (separate store instance replacing the file on disk via os.replace)
        with mock.patch("imdb_ratings.requests.get", return_value=_FakeResponse(payload_v3)):
            self.assertTrue(external_writer.refresh(force=True))
        self.assertEqual(reader.lookup("tt0944947"), (9.5, 4000))
        self.assertIsNone(reader.lookup("tt3915174"))
        self.assertEqual(reader.lookup("tt0230011"), (6.9, 5000))

    def test_concurrent_refresh_deduplicates_and_readers_stay_safe(self) -> None:
        payload_v1 = _make_gzip_bytes("tconst\taverageRating\tnumVotes\ntt0944947\t9.2\t2000000\n")
        store = imdb_ratings.ImdbRatingsStore(self.db_path)
        self.addCleanup(store.close)

        calls = 0
        started = threading.Event()
        release = threading.Event()

        def _slow_get(*args: Any, **kwargs: Any) -> _FakeResponse:
            nonlocal calls
            calls += 1
            started.set()
            release.wait(timeout=2.0)
            return _FakeResponse(payload_v1)

        results: list[bool] = []
        errors: list[Exception] = []

        def _worker() -> None:
            try:
                results.append(store.refresh(force=False))
            except Exception as exc:
                errors.append(exc)

        with mock.patch("imdb_ratings.requests.get", side_effect=_slow_get):
            t1 = threading.Thread(target=_worker)
            t2 = threading.Thread(target=_worker)
            t1.start()
            self.assertTrue(started.wait(timeout=2.0))
            t2.start()
            time.sleep(0.05)
            release.set()
            t1.join(timeout=2.0)
            t2.join(timeout=2.0)

        self.assertEqual(errors, [])
        self.assertEqual(calls, 1)
        self.assertCountEqual(results, [True, False])

        # Concurrent lookups during forced refreshes
        stop_readers = threading.Event()
        reader_errors: list[Exception] = []

        def _reader() -> None:
            try:
                while not stop_readers.is_set():
                    val = store.lookup("tt0944947")
                    if val is None or val[0] < 9.0:
                        raise AssertionError(f"Unexpected lookup value during swap: {val}")
            except Exception as exc:
                reader_errors.append(exc)

        readers = [threading.Thread(target=_reader) for _ in range(4)]
        for t in readers:
            t.start()
        try:
            for i in range(5):
                p = _make_gzip_bytes(f"tconst\taverageRating\tnumVotes\ntt0944947\t9.{i + 1}\t{2000000 + i}\n")
                with mock.patch("imdb_ratings.requests.get", return_value=_FakeResponse(p)):
                    store.refresh(force=True)
        finally:
            stop_readers.set()
            for t in readers:
                t.join(timeout=2.0)

        self.assertEqual(reader_errors, [])
        self.assertEqual(store.lookup("tt0944947"), (9.5, 2000004))

    def test_background_refresh_idempotent_and_resilient(self) -> None:
        store = imdb_ratings.ImdbRatingsStore(self.db_path)
        self.addCleanup(store.close)

        attempted = threading.Event()

        def _failing_get(*args: Any, **kwargs: Any) -> Any:
            attempted.set()
            raise requests.Timeout("simulated timeout")

        with (
            self.assertLogs("imdb_ratings", level="ERROR") as logs,
            mock.patch("imdb_ratings.requests.get", side_effect=_failing_get),
        ):
            t1 = store.start_background_refresh(check_interval=60.0)
            t2 = store.start_background_refresh(check_interval=60.0)
            self.assertIs(t1, t2)
            self.assertTrue(t1.daemon)
            self.assertTrue(attempted.wait(timeout=2.0))
            time.sleep(0.05)
            # Thread must still be alive after catching refresh exception
            self.assertTrue(t1.is_alive())
            store.close()
            t1.join(timeout=2.0)
            self.assertFalse(t1.is_alive())

        self.assertTrue(any("background refresh failed" in msg for msg in logs.output))

    def test_cli_main_force_refresh_and_lookup(self) -> None:
        payload = _make_gzip_bytes("tconst\taverageRating\tnumVotes\ntt0944947\t9.2\t2350000\n")
        buf = io.StringIO()
        with (
            mock.patch("imdb_ratings.logging.basicConfig"),
            mock.patch("imdb_ratings.requests.get", return_value=_FakeResponse(payload)),
            contextlib.redirect_stdout(buf),
        ):
            rc = imdb_ratings.main(
                ["--db", str(self.db_path), "--force", "--lookup", "tt0944947"]
            )
        self.assertEqual(rc, 0)
        out = buf.getvalue()
        self.assertIn("refreshed=True", out)
        self.assertIn("lookup tt0944947: (9.2, 2350000)", out)
        store = imdb_ratings.ImdbRatingsStore(self.db_path)
        self.addCleanup(store.close)
        self.assertEqual(store.lookup("tt0944947"), (9.2, 2350000))


if __name__ == "__main__":
    unittest.main()
