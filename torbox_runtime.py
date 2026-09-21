"""Bounded concurrency and private I/O for the public Telegram bot."""
from __future__ import annotations

import json
import logging
import os
import re
import tempfile
import time
from collections import OrderedDict, deque
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Condition, Lock, local
from typing import Any


class UserDispatcher:
    """FIFO per user, fair scheduling across users, and bounded queued work."""

    def __init__(self, workers: int = 8, capacity: int = 128, per_user: int = 4,
                 name: str = "updates") -> None:
        self.capacity, self.per_user = capacity, per_user
        self._pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix=name)
        self._condition = Condition()
        self._queues: dict[int, deque[Callable[[], Any]]] = {}
        self._running: set[int] = set()
        self._count = 0
        self._closed = False

    def submit(self, user_id: int, task: Callable[[], Any], *, coalesce: bool = False) -> bool:
        with self._condition:
            if self._closed:
                return False
            queue = self._queues.get(user_id)
            if coalesce and queue:
                queue[-1] = task
                return True
            if self._count >= self.capacity:
                return False
            if queue is not None and len(queue) + (user_id in self._running) >= self.per_user:
                return False
            new_user = queue is None
            if new_user:
                queue = self._queues[user_id] = deque()
            queue.append(task)
            self._count += 1
            if new_user:
                self._pool.submit(self._run_one, user_id)
            return True

    def _run_one(self, user_id: int) -> None:
        with self._condition:
            task = self._queues[user_id].popleft()
            self._running.add(user_id)
        try:
            task()
        except Exception:
            logging.getLogger(__name__).exception("Background task failed")
        finally:
            with self._condition:
                self._running.remove(user_id)
                self._count -= 1
                if self._queues[user_id]:
                    # Rejoin the executor's queue so one busy user cannot monopolize a worker.
                    self._pool.submit(self._run_one, user_id)
                else:
                    del self._queues[user_id]
                self._condition.notify_all()

    def wait_idle(self, timeout: float | None = None) -> bool:
        with self._condition:
            return self._condition.wait_for(lambda: self._count == 0, timeout)

    def close(self) -> None:
        with self._condition:
            self._closed = True
        self.wait_idle()
        self._pool.shutdown(wait=True)


class RateLimiter:
    """Token bucket with bounded bookkeeping; active limits are never evicted."""

    def __init__(self, burst: int, period: float, *, max_keys: int = 4096,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self.burst, self.period, self.max_keys = burst, period, max_keys
        self.clock = clock
        self._lock = Lock()
        self._buckets: OrderedDict[Any, tuple[float, float]] = OrderedDict()

    def allow(self, key: Any) -> bool:
        now = self.clock()
        with self._lock:
            while self._buckets:
                oldest, (_, updated) = next(iter(self._buckets.items()))
                if now - updated < self.period:
                    break
                del self._buckets[oldest]
            if key not in self._buckets and len(self._buckets) >= self.max_keys:
                return False
            tokens, updated = self._buckets.get(key, (float(self.burst), now))
            tokens = min(self.burst, tokens + max(0, now - updated) * self.burst / self.period)
            allowed = tokens >= 1
            self._buckets[key] = (tokens - 1 if allowed else tokens, now)
            self._buckets.move_to_end(key)
            return allowed


class ThreadLocalHTTP:
    """Give each worker a separate requests session, including its cookies."""

    def init_http(self, factory: Callable[[], Any], headers: dict[str, str]) -> None:
        self._http_factory = factory
        self._http_headers = headers
        self._http_local = local()

    @property
    def http(self) -> Any:
        session = getattr(self._http_local, "session", None)
        if session is None:
            session = self._http_factory()
            session.headers.update(self._http_headers)
            self._http_local.session = session
        return session

    @http.setter
    def http(self, session: Any) -> None:
        self._http_local.session = session


class OutboundThrottle:
    """Space Telegram requests globally and messages within each private chat."""

    def __init__(self) -> None:
        self._lock = Lock()
        self._next_request = 0.0
        self._chats: OrderedDict[int, float] = OrderedDict()

    def wait(self, chat_id: int | None = None) -> None:
        while True:
            with self._lock:
                now = time.monotonic()
                delay = max(self._next_request, self._chats.get(chat_id, 0.0)) - now
                if delay <= 0:
                    self._next_request = now + 0.05
                    if chat_id is not None:
                        self._chats[chat_id] = now + 1.05
                        self._chats.move_to_end(chat_id)
                        while len(self._chats) > 4096:
                            self._chats.popitem(last=False)
                    return
            time.sleep(min(delay, 0.2))

    def defer(self, seconds: float) -> None:
        with self._lock:
            self._next_request = max(self._next_request, time.monotonic() + min(60, max(1, seconds)))


def write_private_json(path: Path, data: Any) -> None:
    """Create private temporary files from the first byte, then replace atomically."""
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(data, stream, ensure_ascii=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


class PrivateFormatter(logging.Formatter):
    """Redact credentials in exception tracebacks as well as ordinary log messages."""

    def __init__(self) -> None:
        super().__init__("%(asctime)s %(levelname)s %(message)s")
        self._lock = Lock()
        self._secrets: set[str] = set()

    def add_secret(self, value: str) -> None:
        if value:
            with self._lock:
                self._secrets.add(value)

    def remove_secret(self, value: str) -> None:
        with self._lock:
            self._secrets.discard(value)

    def format(self, record: logging.LogRecord) -> str:
        text = super().format(record)
        with self._lock:
            for secret in sorted(self._secrets, key=len, reverse=True):
                text = text.replace(secret, "[REDACTED]")
        text = re.sub(r"bot\d+:[A-Za-z0-9_-]+", "bot[REDACTED]", text)
        return re.sub(r"(?i)([?&](?:apikey|api_key|token|key|auth)=)[^&\s'\"<>]+", r"\1[REDACTED]", text)


PRIVATE_LOG = PrivateFormatter()


def configure_private_logging() -> None:
    for name in ("TELEGRAM_BOT_TOKEN", "TORBOX_API_KEY", "PROWLARR_API_KEY", "KINOPOISK_API_KEY"):
        PRIVATE_LOG.add_secret(os.getenv(name, ""))
    for handler in logging.getLogger().handlers:
        handler.setFormatter(PRIVATE_LOG)
