"""Офлайн-тесты RuTracker и Kinozal через Byparr: разбор, хэши, фоновая очередь.

Страницы в `fixtures/` сняты живьём 02.11.2026 под входом и вычищены: имя
пользователя, его id на обоих трекерах, `form_token`, `hash4u` и прочее
сессионное заменены заглушками, большие страницы укорочены до нужных частей
без правки разметки. Сеть не используется: Byparr подменён.
"""

from __future__ import annotations

import contextlib
import io
import logging
import os
import tempfile
import threading
import time
import unittest
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from unittest import mock

import requests

import meta_service
import search_api
import torbox_tg_bot as bot

FIXTURES = Path(__file__).parent / "fixtures"
RUTRACKER_COOKIE = "bb_session=0-SEKRETRUTRACKER0000"
KINOZAL_COOKIE = "uid=SEKRETUID777; pass=SEKRETPASS999"
SECRETS = ("SEKRETRUTRACKER0000", "SEKRETUID777", "SEKRETPASS999")
OWN_PROFILE_ID = "11111111"
RUTRACKER_TOPIC_HASH = "8f0d511242a84436bbb1243a18bc09d09ac18d94"
KINOZAL_HASH = "caf7083024a9bdd365f40c037a9c568d2db3760a"


def fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


class FakeHttpResponse:
    def __init__(self, status_code: int, payload: Any) -> None:
        self.status_code = status_code
        self._payload = payload

    def json(self) -> Any:
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


class FakeByparrHttp:
    """Подмена HTTP-сессии ByparrClient: страница по адресу или заданный отказ.

    `cookies` — куки ответа по куску адреса, как их отдаёт браузер Byparr;
    `gate` держит каждый запрос, пока его не откроют.
    """

    def __init__(
        self,
        pages: dict[str, str] | None = None,
        failure: Any = None,
        cookies: dict[str, list[dict[str, Any]]] | None = None,
        gate: threading.Event | None = None,
    ) -> None:
        self.pages = pages or {}
        self.failure = failure
        self.cookies = cookies or {}
        self.gate = gate
        self.calls: list[dict[str, Any]] = []
        self.lock = threading.Lock()
        self.entered = threading.Event()

    def post(self, url: str, json: dict[str, Any], timeout: float) -> FakeHttpResponse:
        with self.lock:
            self.calls.append({"endpoint": url, **json})
        self.entered.set()
        if self.gate is not None:
            self.gate.wait(5)
        if isinstance(self.failure, Exception):
            raise self.failure
        if isinstance(self.failure, FakeHttpResponse):
            return self.failure
        page = next((text for marker, text in self.pages.items() if marker in json["url"]), None)
        if page is None:
            return FakeHttpResponse(200, {"status": "ok", "solution": {"status": 404, "response": ""}})
        cookies = next((jar for marker, jar in self.cookies.items() if marker in json["url"]), [])
        return FakeHttpResponse(200, {
            "status": "ok",
            "solution": {"status": 200, "response": page, "cookies": list(cookies), "userAgent": "Mozilla/5.0"},
        })


def byparr_with(
    pages: dict[str, str] | None = None, failure: Any = None, gate: threading.Event | None = None
) -> tuple[bot.ByparrClient, FakeByparrHttp]:
    http = FakeByparrHttp(pages, failure, gate=gate)
    return bot.ByparrClient("http://byparr.test:8192", http=http), http


def tracker_pages() -> dict[str, str]:
    return {
        "rutracker.org/forum/tracker.php": fixture("rutracker-search.html"),
        "rutracker.org/forum/viewtopic.php": fixture("rutracker-topic.html"),
        "kinozal.me/browse.php": fixture("kinozal-search.html"),
        "kinozal.me/details.php": fixture("kinozal-details.html"),
        "kinozal.me/get_srv_details.php": fixture("kinozal-hash.html"),
    }


def clients(
    pages: dict[str, str] | None = None, failure: Any = None, gate: threading.Event | None = None
) -> tuple[bot.RutrackerClient, bot.KinozalClient, FakeByparrHttp]:
    byparr, http = byparr_with(tracker_pages() if pages is None else pages, failure, gate)
    return bot.RutrackerClient(RUTRACKER_COOKIE, byparr), bot.KinozalClient(KINOZAL_COOKIE, byparr), http


def temp_store() -> meta_service.TTLCache:
    return meta_service.TTLCache(Path(tempfile.mkdtemp()) / "store.json", 3600)


@contextlib.contextmanager
def server_state(providers: list[Any], prefetcher: search_api.HashPrefetcher | None = None) -> Iterator[search_api.HashPrefetcher]:
    """Подменяет источники, кэши и очередь сервера на время теста."""
    saved = (
        search_api.PROVIDERS, search_api.PROVIDER_BY_KEY, search_api.CACHE,
        search_api.HASH_CACHE, search_api.PREFETCHER,
    )
    search_api.PROVIDERS = providers
    search_api.PROVIDER_BY_KEY = {provider.key: provider for provider in providers}
    search_api.CACHE = search_api.ReleaseCache()
    search_api.HASH_CACHE = temp_store()
    search_api.PREFETCHER = prefetcher or search_api.HashPrefetcher(
        search_api.prefetch_resolve, search_api.prefetch_known, autostart=False
    )
    try:
        yield search_api.PREFETCHER
    finally:
        (
            search_api.PROVIDERS, search_api.PROVIDER_BY_KEY, search_api.CACHE,
            search_api.HASH_CACHE, search_api.PREFETCHER,
        ) = saved


class RutrackerSearchParsingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.releases = bot.parse_rutracker_search(fixture("rutracker-search.html"))
        self.by_topic = {release.topic_id: release for release in self.releases}

    def test_every_row_with_a_topic_id_becomes_a_release(self) -> None:
        self.assertEqual(len(self.releases), 9)
        self.assertEqual(len(self.by_topic), 9)

    def test_row_fields(self) -> None:
        release = self.by_topic["6147893"]
        self.assertTrue(release.title.startswith("Интерстеллар / Interstellar (Кристофер Нолан"))
        self.assertNotIn("<", release.title)
        self.assertEqual(release.category, "Зарубежное кино (UHD Video)")
        self.assertEqual(release.size_bytes, 94505802825)
        self.assertEqual(release.size_text, "88.02 GB")
        self.assertEqual((release.seeders, release.leechers), (149, 10))
        self.assertEqual(release.source_url, "https://rutracker.org/forum/viewtopic.php?t=6147893")
        self.assertEqual(release.provider, "rutracker")
        self.assertEqual(release.source_name, "RuTracker")
        self.assertEqual(release.published_at, "2021-12-08T15:47:10Z")
        # Хэша в выдаче нет — его даёт только страница темы
        self.assertEqual((release.info_hash, release.magnet_url), ("", ""))

    def test_kind_and_score_are_filled_like_nnm(self) -> None:
        self.assertEqual(bot.release_content_kind(self.by_topic["6147893"]), "video")
        self.assertEqual(bot.release_content_kind(self.by_topic["4874288"]), "music")
        for release in self.releases:
            self.assertEqual(release.score, bot.score_release(release.title, release.seeders, release.leechers))
        self.assertEqual(self.releases, sorted(self.releases, key=lambda item: item.score, reverse=True))

    def test_stable_id_is_provider_and_topic(self) -> None:
        self.assertEqual(search_api.stable_release_id(self.by_topic["6147893"]), "rutracker:6147893")

    def test_torrent_registration_time_names_the_file(self) -> None:
        # Перерегистрация .torrent в той же теме даёт новый ключ кэша хэша
        release = self.by_topic["6147893"]
        self.assertEqual(release.torrent_id, "6147893-1638978430")
        self.assertNotEqual(
            search_api.hash_cache_key("rutracker:6147893", release),
            search_api.hash_cache_key("rutracker:6147893", bot.replace(release, torrent_id="6147893-1700000000")),
        )

    def test_logged_out_page_is_an_error_not_an_empty_result(self) -> None:
        rutracker, _, _ = clients({"tracker.php": '<form><input name="login_username"></form>'})
        with self.assertRaisesRegex(RuntimeError, "RUTRACKER_COOKIE"):
            rutracker.search("Интерстеллар")


class KinozalSearchParsingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.page = fixture("kinozal-search.html")
        self.releases = bot.parse_kinozal_search(self.page)
        self.by_id = {release.topic_id: release for release in self.releases}

    def test_every_result_row_becomes_a_release(self) -> None:
        self.assertEqual(len(self.releases), 10)

    def test_own_profile_link_is_not_a_result(self) -> None:
        # Ссылка на свой профиль тоже содержит `details.php?id=`
        self.assertIn(f"userdetails.php?id={OWN_PROFILE_ID}", self.page)
        self.assertNotIn(OWN_PROFILE_ID, self.by_id)
        # Как и ссылки на раздающих
        self.assertNotIn("726531", self.by_id)
        self.assertNotIn("4482203", self.by_id)

    def test_row_fields(self) -> None:
        release = self.by_id["2084548"]
        self.assertEqual(
            release.title,
            "Интерстеллар / Interstellar (IMAX Edition) / 2014 / ДБ / 4K, HEVC, HDR / Blu-Ray Remux (2160p)",
        )
        self.assertEqual(release.size_text, "68.45 ГБ")
        self.assertEqual(release.size_bytes, int(68.45 * 1024**3))
        self.assertEqual((release.seeders, release.leechers), (47, 1))
        self.assertEqual(release.source_url, "https://kinozal.me/details.php?id=2084548")
        self.assertEqual(release.provider, "kinozal")
        # 05.04.2025 в 16:33 по Москве
        self.assertEqual(release.published_at, "2025-04-05T13:33:00Z")
        self.assertEqual(bot.release_content_kind(release), "video")
        self.assertEqual(search_api.stable_release_id(release), "kinozal:2084548")

    def test_megabytes_in_russian(self) -> None:
        self.assertEqual(self.by_id["1297579"].size_bytes, 657 * 1024**2)

    def test_relative_dates(self) -> None:
        now = bot.datetime(2026, 11, 2, 12, 0, tzinfo=bot.MOSCOW_TZ)
        self.assertEqual(bot.parse_kinozal_date("сегодня в 10:15", now), "2026-11-02T07:15:00Z")
        self.assertEqual(bot.parse_kinozal_date("вчера в 23:50", now), "2026-11-01T20:50:00Z")
        self.assertEqual(bot.parse_kinozal_date("давно"), "")


class HashExtractionTests(unittest.TestCase):
    def test_rutracker_hash_comes_from_the_topic_magnet(self) -> None:
        self.assertEqual(bot.rutracker_topic_hash(fixture("rutracker-topic.html")), RUTRACKER_TOPIC_HASH)

    def test_kinozal_hash_comes_from_srv_details(self) -> None:
        self.assertEqual(bot.kinozal_info_hash(fixture("kinozal-hash.html")), KINOZAL_HASH)

    def test_pages_without_a_hash_give_nothing(self) -> None:
        self.assertEqual(bot.rutracker_topic_hash("<html></html>"), "")
        self.assertEqual(bot.kinozal_info_hash("<html>Инфо хеш: нет</html>"), "")

    def test_clients_read_one_page_each(self) -> None:
        rutracker, kinozal, http = clients()
        release = bot.parse_rutracker_search(fixture("rutracker-search.html"))[0]
        self.assertEqual(rutracker.resolve_info_hash(release), RUTRACKER_TOPIC_HASH)
        self.assertIn(f"viewtopic.php?t={release.topic_id}", http.calls[-1]["url"])
        kinozal_release = bot.parse_kinozal_search(fixture("kinozal-search.html"))[0]
        self.assertEqual(kinozal.resolve_info_hash(kinozal_release), KINOZAL_HASH)
        self.assertTrue(http.calls[-1]["url"].endswith(f"get_srv_details.php?id={kinozal_release.topic_id}&action=2"))
        self.assertEqual(len(http.calls), 2)

    def test_restore_rutracker_topic_takes_title_and_magnet(self) -> None:
        rutracker, _, _ = clients()
        release = search_api.restore_rutracker_topic(rutracker, "4874288")
        assert release is not None
        self.assertTrue(release.title.startswith("(Score) Интерстеллар / Interstellar"))
        self.assertNotIn("RuTracker.org", release.title)
        self.assertEqual(release.info_hash, RUTRACKER_TOPIC_HASH)
        self.assertTrue(release.magnet_url.startswith(f"magnet:?xt=urn:btih:{RUTRACKER_TOPIC_HASH}"))
        self.assertEqual(release.size_bytes, 415625827)

    def test_restore_kinozal_details_takes_title_and_size(self) -> None:
        _, kinozal, _ = clients()
        release = search_api.restore_kinozal_details(kinozal, "1297579")
        assert release is not None
        self.assertTrue(release.title.startswith("Интерстеллар / Interstellar (Illuminated Star"))
        self.assertEqual(release.size_bytes, 657 * 1024**2)

    def test_restore_release_maps_the_new_prefixes(self) -> None:
        rutracker, kinozal, _ = clients()
        with server_state([rutracker, kinozal]):
            restored = search_api.restore_release("rutracker:4874288")
            self.assertIsNotNone(search_api.restore_release("kinozal:1297579"))
            self.assertIsNone(search_api.restore_release("kinozal:abc"))
        assert restored is not None
        self.assertEqual(search_api.stable_release_id(restored), "rutracker:4874288")


class OneRequestPerSearchTests(unittest.TestCase):
    def test_slow_trackers_get_only_the_first_query(self) -> None:
        rutracker, kinozal, http = clients()
        queries = ["Интерстеллар 2014", "Интерстеллар", "Interstellar"]
        releases, _, sources, partial = search_api.aggregate_releases([rutracker, kinozal], queries, None)
        self.assertEqual(len(http.calls), 2, "по одному запросу к Byparr на трекер")
        urls = sorted(call["url"] for call in http.calls)
        # Запрос — в cp1251, как кодируют поиск сами трекеры
        self.assertIn("browse.php?s=%C8%ED%F2%E5%F0%F1%F2%E5%EB%EB%E0%F0+2014", urls[0])
        self.assertIn("tracker.php?nm=%C8%ED%F2%E5%F0%F1%F2%E5%EB%EB%E0%F0+2014", urls[1])
        self.assertFalse(partial)
        self.assertEqual({source.name: source.state for source in sources}, {"RuTracker": "ok", "Kinozal": "ok"})
        self.assertTrue(any(release.provider == "kinozal" for release in releases))
        self.assertTrue(any(release.provider == "rutracker" for release in releases))

    def test_byparr_request_shape(self) -> None:
        rutracker, kinozal, http = clients()
        rutracker.search("Интерстеллар 2014")
        kinozal.search("Интерстеллар 2014")
        rutracker_call, kinozal_call = http.calls
        self.assertEqual(rutracker_call["endpoint"], "http://byparr.test:8192/v1")
        self.assertEqual(rutracker_call["cmd"], "request.get")
        self.assertEqual(rutracker_call["maxTimeout"], 120000)
        self.assertEqual(
            rutracker_call["cookies"],
            [{"name": "bb_session", "value": "0-SEKRETRUTRACKER0000", "domain": ".rutracker.org", "path": "/"}],
        )
        self.assertEqual([cookie["name"] for cookie in kinozal_call["cookies"]], ["uid", "pass"])
        self.assertEqual({cookie["domain"] for cookie in kinozal_call["cookies"]}, {".kinozal.me"})

    def test_repeated_search_is_served_from_memory(self) -> None:
        rutracker, _, http = clients()
        first = rutracker.search("Интерстеллар 2014")
        second = rutracker.search("интерстеллар 2014 ")
        self.assertEqual(first, second)
        self.assertEqual(len(http.calls), 1)

    def test_search_endpoint_never_reads_topic_pages(self) -> None:
        rutracker, kinozal, http = clients()
        with server_state([rutracker, kinozal]) as prefetcher:
            result = search_api.tracker_search(q="Интерстеллар 2014", limit=40)
        self.assertEqual(len(http.calls), 2)
        self.assertFalse(any("viewtopic" in call["url"] or "get_srv" in call["url"] for call in http.calls))
        self.assertTrue(result.results)
        # Хэши медленных трекеров — в фоновую очередь, в порядке ранга
        slow_ids = [item.id for item in result.results if item.provider in {"rutracker", "kinozal"}]
        self.assertEqual(prefetcher.pending(), slow_ids[: search_api.PREFETCH_PER_SEARCH])

    def test_failed_byparr_marks_only_that_provider(self) -> None:
        broken_byparr, _ = byparr_with(failure=FakeHttpResponse(500, {"status": "error", "message": "browser crashed"}))
        broken = bot.RutrackerClient(RUTRACKER_COOKIE, broken_byparr)
        _, kinozal, _ = clients()
        releases, status, sources, partial = search_api.aggregate_releases([broken, kinozal], ["Интерстеллар 2014"], None)
        self.assertEqual(status["RuTracker"], "ошибка")
        self.assertEqual({source.name: source.state for source in sources}, {"RuTracker": "error", "Kinozal": "ok"})
        self.assertTrue(releases)
        self.assertFalse(partial)


class SecretLeakTests(unittest.TestCase):
    """Куки входа — секрет: их нет ни в журнале, ни в исключениях, ни в repr."""

    def assert_clean(self, text: str) -> None:
        for secret in SECRETS:
            self.assertNotIn(secret, text)

    def failures(self) -> list[Any]:
        echoed = f"bad cookie {RUTRACKER_COOKIE} {KINOZAL_COOKIE}"
        return [
            FakeHttpResponse(500, {"status": "error", "message": echoed}),
            FakeHttpResponse(200, {"status": "error", "message": echoed}),
            FakeHttpResponse(200, ValueError("not json")),
            requests.ConnectionError(f"refused, body had {KINOZAL_COOKIE}"),
        ]

    def test_exceptions_and_repr_do_not_carry_cookies(self) -> None:
        for failure in self.failures():
            rutracker, kinozal, _ = clients(failure=failure)
            for client in (rutracker, kinozal):
                self.assert_clean(repr(client))
                with self.assertRaises(bot.ByparrError) as ctx:
                    client.search("Интерстеллар")
                self.assert_clean(str(ctx.exception))
                self.assert_clean(repr(ctx.exception))
                self.assertIsNone(ctx.exception.__cause__)
                with self.assertRaises(RuntimeError) as download:
                    client.download_torrent(bot.parse_kinozal_search(fixture("kinozal-search.html"))[0])
                self.assert_clean(str(download.exception))

    def test_failed_search_and_hash_logs_have_no_cookies(self) -> None:
        stream = io.StringIO()
        handler = logging.StreamHandler(stream)
        handler.setFormatter(search_api.RedactingFormatter(search_api.LOG_FORMAT))
        root = logging.getLogger()
        root.addHandler(handler)
        try:
            for failure in self.failures():
                rutracker, kinozal, _ = clients(failure=failure)
                with server_state([rutracker, kinozal]) as prefetcher:
                    search_api.aggregate_releases([rutracker, kinozal], ["Интерстеллар 2014"], None)
                    release = bot.parse_kinozal_search(fixture("kinozal-search.html"))[0]
                    prefetcher.enqueue([("kinozal:" + release.topic_id, release)])
                    prefetcher.run_once()
                    self.assertIsNone(search_api.restore_release("rutracker:4874288"))
        finally:
            root.removeHandler(handler)
        output = stream.getvalue()
        self.assertIn("Byparr", output)
        self.assert_clean(output)


class ProviderConfigTests(unittest.TestCase):
    def build(self, env: dict[str, str]) -> list[Any]:
        names = ("RUTRACKER_COOKIE", "KINOZAL_COOKIE", "RUTRACKER_ENABLED", "KINOZAL_ENABLED", "BYPARR_URL")
        clean = {key: value for key, value in os.environ.items() if key not in names}
        with mock.patch.dict(os.environ, {**clean, **env}, clear=True):
            return bot.build_byparr_providers()

    def test_disabled_without_cookies(self) -> None:
        self.assertEqual(self.build({}), [])
        self.assertEqual(self.build({"RUTRACKER_COOKIE": "  ", "KINOZAL_COOKIE": ""}), [])

    def test_each_tracker_needs_its_own_cookie(self) -> None:
        self.assertEqual([p.key for p in self.build({"RUTRACKER_COOKIE": RUTRACKER_COOKIE})], ["rutracker"])
        self.assertEqual([p.key for p in self.build({"KINOZAL_COOKIE": KINOZAL_COOKIE})], ["kinozal"])

    def test_enabled_flags_switch_off_a_configured_tracker(self) -> None:
        both = {"RUTRACKER_COOKIE": RUTRACKER_COOKIE, "KINOZAL_COOKIE": KINOZAL_COOKIE}
        self.assertEqual([p.key for p in self.build(both)], ["rutracker", "kinozal"])
        self.assertEqual([p.key for p in self.build({**both, "KINOZAL_ENABLED": "false"})], ["rutracker"])
        self.assertEqual([p.key for p in self.build({**both, "RUTRACKER_ENABLED": "0"})], ["kinozal"])

    def test_byparr_url_from_env(self) -> None:
        providers = self.build({"RUTRACKER_COOKIE": RUTRACKER_COOKIE})
        self.assertEqual(providers[0].byparr.base_url, "http://127.0.0.1:8192")
        providers = self.build({"RUTRACKER_COOKIE": RUTRACKER_COOKIE, "BYPARR_URL": "http://10.0.0.5:9000/"})
        self.assertEqual(providers[0].byparr.base_url, "http://10.0.0.5:9000")

    def test_api_build_providers_includes_them(self) -> None:
        env = {"RUTRACKER_COOKIE": RUTRACKER_COOKIE, "NNM_ENABLED": "false", "PROWLARR_URL": ""}
        with mock.patch.dict(os.environ, env):
            self.assertEqual([p.key for p in search_api.build_providers()], ["rutracker"])


def slow_items(count: int) -> list[tuple[str, bot.Release]]:
    releases = bot.parse_rutracker_search(fixture("rutracker-search.html"))[:count]
    return [(search_api.stable_release_id(release), release) for release in releases]


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


class HashPrefetcherTests(unittest.TestCase):
    def make(self, resolve: Any = None, known: Any = None, **kwargs: Any) -> search_api.HashPrefetcher:
        self.resolved: list[str] = []

        def default_resolve(release_id: str, release: bot.Release) -> str:
            self.resolved.append(release_id)
            return "b" * 40

        return search_api.HashPrefetcher(
            resolve or default_resolve, known or (lambda *_: False), autostart=False, **kwargs
        )

    def test_duplicates_are_queued_once(self) -> None:
        prefetcher = self.make()
        items = slow_items(3)
        prefetcher.enqueue(items + items[:1])
        prefetcher.enqueue(items[1:2])
        self.assertEqual(len(prefetcher.pending()), 3)
        # Повтор поднимает релиз в начало, но не удваивает
        self.assertEqual(prefetcher.pending()[0], items[1][0])
        while prefetcher.run_once():
            pass
        self.assertEqual(sorted(self.resolved), sorted(release_id for release_id, _ in items))

    def test_queue_is_bounded_and_keeps_the_newest_search(self) -> None:
        prefetcher = self.make(max_queue=4)
        items = slow_items(9)
        prefetcher.enqueue(items[:5])
        self.assertEqual(prefetcher.pending(), [release_id for release_id, _ in items[:4]])
        prefetcher.enqueue(items[5:8])
        self.assertEqual(
            prefetcher.pending(), [release_id for release_id, _ in items[5:8]] + [items[0][0]]
        )

    def test_known_hash_is_not_queued(self) -> None:
        items = slow_items(2)
        prefetcher = self.make(known=lambda release_id, release: release_id == items[0][0])
        self.assertEqual(prefetcher.enqueue(items), [items[1][0]])

    def test_failing_provider_backs_off_and_does_not_stop_the_queue(self) -> None:
        clock = FakeClock()
        items = slow_items(2)
        calls: list[str] = []

        def resolve(release_id: str, release: bot.Release) -> str | None:
            calls.append(release_id)
            if release_id == items[0][0]:
                raise bot.ByparrError("Byparr вернул HTTP 500")
            return "c" * 40

        prefetcher = self.make(resolve=resolve, clock=clock, backoff_seconds=60, backoff_max_seconds=200)
        prefetcher.enqueue(items)
        self.assertTrue(prefetcher.run_once())
        self.assertTrue(prefetcher.run_once())
        self.assertEqual(calls, [items[0][0], items[1][0]])
        # Сразу после отказа — не берём
        self.assertEqual(prefetcher.enqueue(items[:1]), [])
        clock.now += 61
        self.assertEqual(prefetcher.enqueue(items[:1]), [items[0][0]])
        prefetcher.run_once()
        # Второй отказ — пауза вдвое длиннее
        clock.now += 61
        self.assertEqual(prefetcher.enqueue(items[:1]), [])
        clock.now += 60
        self.assertEqual(prefetcher.enqueue(items[:1]), [items[0][0]])

    def test_empty_hash_counts_as_failure(self) -> None:
        clock = FakeClock()
        prefetcher = self.make(resolve=lambda *_: None, clock=clock)
        items = slow_items(1)
        prefetcher.enqueue(items)
        prefetcher.run_once()
        self.assertEqual(prefetcher.enqueue(items), [])

    def test_resolved_hash_lands_in_the_hash_cache(self) -> None:
        rutracker, kinozal, http = clients()
        with server_state([rutracker, kinozal]) as prefetcher:
            [(release_id, release)] = slow_items(1)
            search_api.CACHE.put_all([release])
            prefetcher.enqueue([(release_id, release)])
            prefetcher.run_once()
            key = search_api.hash_cache_key(release_id, release)
            self.assertEqual(search_api.HASH_CACHE.get(key), RUTRACKER_TOPIC_HASH)
            # Запрос хэшей отдаёт его сразу, без Byparr
            before = len(http.calls)
            hashes = search_api.collect_info_hashes([release_id], deadline=0.1)
            self.assertEqual(hashes, {release_id: RUTRACKER_TOPIC_HASH})
            self.assertEqual(len(http.calls), before)
            # Уже известный хэш второй раз в очередь не встаёт
            self.assertEqual(prefetcher.enqueue([(release_id, release)]), [])

    def test_hash_request_queues_unknown_ones_and_returns_none_past_deadline(self) -> None:
        rutracker, kinozal, http = clients()
        with server_state([rutracker, kinozal]) as prefetcher:
            items = slow_items(2)
            search_api.CACHE.put_all([release for _, release in items])
            hashes = search_api.collect_info_hashes([release_id for release_id, _ in items], deadline=0.05)
            self.assertEqual(hashes, {release_id: None for release_id, _ in items})
            self.assertEqual(prefetcher.pending(), [release_id for release_id, _ in items])
            self.assertEqual(http.calls, [], "сам запрос хэшей к Byparr не ходит")

    def test_hash_request_waits_for_the_worker_within_its_deadline(self) -> None:
        rutracker, kinozal, _ = clients()
        prefetcher = search_api.HashPrefetcher(search_api.prefetch_resolve, search_api.prefetch_known)
        with server_state([rutracker, kinozal], prefetcher):
            # Релиза нет ни в кэше, ни в хранилище: хватает номера темы
            hashes = search_api.collect_info_hashes(["kinozal:1297579", "rutracker:4874288"], deadline=5)
        self.assertEqual(hashes, {"kinozal:1297579": KINOZAL_HASH, "rutracker:4874288": RUTRACKER_TOPIC_HASH})

    def test_worker_does_not_start_while_a_search_runs(self) -> None:
        resolved = threading.Event()
        prefetcher = search_api.HashPrefetcher(
            lambda *_: resolved.set() or "e" * 40, lambda *_: False
        )
        items = slow_items(1)
        with prefetcher.foreground():
            prefetcher.enqueue(items)
            self.assertFalse(resolved.wait(0.3), "пока идёт поиск, Byparr не занимаем")
        self.assertTrue(prefetcher.wait_for([items[0][0]], 5))
        self.assertTrue(resolved.is_set())

    def test_byparr_outage_pauses_the_queue_and_keeps_the_release(self) -> None:
        clock = FakeClock()
        items = slow_items(3)
        calls: list[str] = []
        down = True

        def resolve(release_id: str, release: bot.Release) -> str | None:
            calls.append(release_id)
            if down:
                raise bot.ByparrUnavailable("Byparr не ответил (ConnectionError) на запрос к rutracker.org")
            return "c" * 40

        prefetcher = self.make(resolve=resolve, clock=clock, outage_pause_seconds=5, outage_pause_max_seconds=60)
        prefetcher.enqueue(items)
        self.assertTrue(prefetcher.run_once())
        self.assertEqual(calls, [items[0][0]], "за отказ Byparr — одна попытка, а не вся очередь")
        self.assertEqual(prefetcher.pending(), [release_id for release_id, _ in items], "раздача вернулась первой")
        self.assertEqual(prefetcher.enqueue(items[:1]), [items[0][0]], "раздачу не списали и не отложили на минуту")
        self.assertEqual(prefetcher.paused_for(), 5)
        clock.now += 5
        prefetcher.run_once()
        self.assertEqual(prefetcher.paused_for(), 10, "пока Byparr молчит, пауза удваивается")
        clock.now += 10
        down = False
        prefetcher.run_once()
        self.assertEqual(prefetcher.pending(), [release_id for release_id, _ in items[1:]])
        self.assertEqual(prefetcher.paused_for(), 0)
        down = True
        prefetcher.run_once()
        self.assertEqual(prefetcher.paused_for(), 5, "Byparr ответил — пауза снова с начала")

    def test_only_byparrs_own_failures_pause_the_queue(self) -> None:
        unavailable = [
            requests.ConnectionError("refused"),
            requests.ReadTimeout("read timed out"),
            FakeHttpResponse(500, {"status": "error", "message": "Bad file descriptor"}),
            FakeHttpResponse(408, {"detail": "Timed out while loading the page or solving the challenge"}),
            FakeHttpResponse(200, ValueError("not json")),
        ]
        page_failures = [
            FakeHttpResponse(422, {"detail": "validation"}),
            FakeHttpResponse(200, {"status": "error", "message": "tracker said no"}),
        ]
        for failure in unavailable:
            rutracker, _, _ = clients(failure=failure)
            with self.subTest(failure=failure), self.assertRaises(bot.ByparrUnavailable):
                rutracker.search("Интерстеллар 2014")
        for failure in page_failures:
            rutracker, _, _ = clients(failure=failure)
            with self.subTest(failure=failure), self.assertRaises(bot.ByparrError) as ctx:
                rutracker.search("Интерстеллар 2014")
            self.assertNotIsInstance(ctx.exception, bot.ByparrUnavailable)
        missing_topic, _, _ = clients(pages={})
        with self.assertRaises(bot.ByparrError) as ctx:
            missing_topic.search("Интерстеллар 2014")
        self.assertNotIsInstance(ctx.exception, bot.ByparrUnavailable, "404 трекера — про страницу, не про Byparr")

    def test_worker_waits_out_an_outage_instead_of_draining_the_queue(self) -> None:
        moments: list[float] = []

        def resolve(release_id: str, release: bot.Release) -> str | None:
            moments.append(time.monotonic())
            if len(moments) == 1:
                raise bot.ByparrUnavailable("Byparr вернул HTTP 500 на запрос к kinozal.me")
            return "d" * 40

        prefetcher = search_api.HashPrefetcher(
            resolve, lambda *_: False, outage_pause_seconds=0.2, outage_pause_max_seconds=1
        )
        items = slow_items(3)
        prefetcher.enqueue(items)
        self.assertTrue(prefetcher.wait_for([release_id for release_id, _ in items], 5))
        self.assertEqual(len(moments), 4, "одна попытка во время отказа и по одной на раздачу после")
        self.assertGreaterEqual(moments[1] - moments[0], 0.2)

    def test_known_hash_makes_search_result_carry_hash_and_magnet(self) -> None:
        rutracker, kinozal, _ = clients()
        with server_state([rutracker, kinozal]):
            [(release_id, release)] = slow_items(1)
            search_api.HASH_CACHE.put(search_api.hash_cache_key(release_id, release), "d" * 40)
            result = search_api.tracker_search(q="Интерстеллар 2014", limit=40)
        item = next(row for row in result.results if row.id == release_id)
        self.assertEqual(item.info_hash, "d" * 40)
        self.assertTrue(item.magnet_url.startswith("magnet:?xt=urn:btih:" + "d" * 40))


CLEARANCE = "SEKRETCLEARANCE4242"


def clearance_cookie(expires: float, value: str = CLEARANCE, domain: str = ".rutracker.org") -> dict[str, Any]:
    """`cf_clearance` в той форме, в какой его отдаёт браузер Byparr."""
    return {
        "name": "cf_clearance", "value": value, "domain": domain, "path": "/",
        "expires": expires, "httpOnly": True, "secure": True, "sameSite": "None",
    }


class CloudflareClearanceTests(unittest.TestCase):
    """Пропуск Cloudflare из ответа Byparr уходит со следующим запросом к тому же сайту.

    Без него браузер Byparr, новый на каждый запрос, проходит проверку
    заново: ~17 с на страницу вместо ~6 (замер 04.11.2026).
    """

    def setUp(self) -> None:
        self.clock = FakeClock()
        self.http = FakeByparrHttp(tracker_pages())
        self.byparr = bot.ByparrClient("http://byparr.test:8192", http=self.http, clock=self.clock)
        self.rutracker = bot.RutrackerClient(RUTRACKER_COOKIE, self.byparr)
        self.kinozal = bot.KinozalClient(KINOZAL_COOKIE, self.byparr)

    def sent(self, call: int) -> dict[str, str]:
        return {cookie["name"]: cookie["value"] for cookie in self.http.calls[call]["cookies"]}

    def answer_with(self, *cookies: dict[str, Any]) -> None:
        self.http.cookies = {"rutracker.org": list(cookies)}

    def test_clearance_goes_back_only_to_its_own_site(self) -> None:
        self.answer_with(clearance_cookie(self.clock.now + 3600))
        self.rutracker.search("Интерстеллар 2014")
        self.rutracker.search("Дюна 2021")
        self.kinozal.search("Дюна 2021")
        self.assertEqual(self.sent(0), {"bb_session": "0-SEKRETRUTRACKER0000"})
        self.assertEqual(self.sent(1), {"bb_session": "0-SEKRETRUTRACKER0000", "cf_clearance": CLEARANCE})
        self.assertEqual(self.http.calls[1]["cookies"][-1]["domain"], ".rutracker.org")
        self.assertEqual(self.sent(2), {"uid": "SEKRETUID777", "pass": "SEKRETPASS999"})

    def test_topic_pages_reuse_the_search_clearance(self) -> None:
        self.answer_with(clearance_cookie(self.clock.now + 3600))
        [release, *_] = self.rutracker.search("Интерстеллар 2014")
        self.rutracker.resolve_info_hash(release)
        self.assertEqual(self.sent(1).get("cf_clearance"), CLEARANCE)

    def test_clearance_stops_a_minute_before_it_expires(self) -> None:
        self.answer_with(clearance_cookie(self.clock.now + 600))
        self.rutracker.search("Интерстеллар 2014")
        self.answer_with()
        self.clock.now += 600 - bot.CF_CLEARANCE_MARGIN_SECONDS - 1
        self.rutracker.search("Дюна 2021")
        self.clock.now += 1
        self.rutracker.search("Аватар 2009")
        self.assertIn("cf_clearance", self.sent(1))
        self.assertNotIn("cf_clearance", self.sent(2))

    def test_session_clearance_is_kept_for_half_an_hour(self) -> None:
        self.answer_with(clearance_cookie(-1))
        self.rutracker.search("Интерстеллар 2014")
        self.answer_with()
        self.clock.now += bot.CF_CLEARANCE_SESSION_TTL_SECONDS - bot.CF_CLEARANCE_MARGIN_SECONDS - 1
        self.rutracker.search("Дюна 2021")
        self.clock.now += 1
        self.rutracker.search("Аватар 2009")
        self.assertIn("cf_clearance", self.sent(1))
        self.assertNotIn("cf_clearance", self.sent(2))

    def test_reissued_clearance_replaces_the_old_one(self) -> None:
        self.answer_with(clearance_cookie(self.clock.now + 3600, value="SEKRETCLEARANCEOLD"))
        self.rutracker.search("Интерстеллар 2014")
        self.answer_with(clearance_cookie(self.clock.now + 3600, value="SEKRETCLEARANCENEW"))
        self.rutracker.search("Дюна 2021")
        self.rutracker.search("Аватар 2009")
        self.assertEqual(self.sent(1)["cf_clearance"], "SEKRETCLEARANCEOLD")
        self.assertEqual(self.sent(2)["cf_clearance"], "SEKRETCLEARANCENEW")

    def test_own_clearance_wins_over_one_pasted_into_env(self) -> None:
        rutracker = bot.RutrackerClient(RUTRACKER_COOKIE + "; cf_clearance=SEKRETFROMBROWSER", self.byparr)
        self.answer_with(clearance_cookie(self.clock.now + 3600))
        rutracker.search("Интерстеллар 2014")
        rutracker.search("Дюна 2021")
        self.assertEqual(self.sent(0)["cf_clearance"], "SEKRETFROMBROWSER")
        names = [cookie["name"] for cookie in self.http.calls[1]["cookies"]]
        self.assertEqual(names.count("cf_clearance"), 1)
        self.assertEqual(self.sent(1)["cf_clearance"], CLEARANCE)

    def test_clearance_is_not_in_errors_or_the_log(self) -> None:
        stream = io.StringIO()
        handler = logging.StreamHandler(stream)
        handler.setFormatter(search_api.RedactingFormatter(search_api.LOG_FORMAT))
        logger = logging.getLogger("torbox_tg_bot")
        logger.addHandler(handler)
        previous_level = logger.level
        logger.setLevel(logging.INFO)
        try:
            self.answer_with(clearance_cookie(self.clock.now + 3600))
            self.rutracker.search("Интерстеллар 2014")
            self.http.failure = FakeHttpResponse(500, {"status": "error", "message": f"echo {CLEARANCE}"})
            with self.assertRaises(bot.ByparrError) as ctx:
                self.rutracker.search("Дюна 2021")
        finally:
            logger.removeHandler(handler)
            logger.setLevel(previous_level)
        self.assertIn("пропуск Cloudflare для rutracker.org получен, действует 60 мин", stream.getvalue())
        self.assertNotIn(CLEARANCE, stream.getvalue())
        self.assertNotIn(CLEARANCE, str(ctx.exception))
        self.assertNotIn(CLEARANCE, repr(self.byparr))


class SharedSearchTests(unittest.TestCase):
    """Тот же поиск, пока первый у Byparr, ждёт его ответа, а не идёт вторым."""

    def run_in_background(self, target: Any) -> threading.Thread:
        thread = threading.Thread(target=target, daemon=True)
        thread.start()
        return thread

    @contextlib.contextmanager
    def waiter_spy(self) -> Iterator[threading.Event]:
        """Событие «второй поиск встал ждать первый»."""
        waiting = threading.Event()
        original_wait = bot.PendingSearch.wait

        def spy_wait(pending: bot.PendingSearch) -> list[bot.Release]:
            waiting.set()
            return original_wait(pending)

        with mock.patch.object(bot.PendingSearch, "wait", spy_wait):
            yield waiting

    def run_two_searches(self, http: FakeByparrHttp, gate: threading.Event, first: Any, second: Any) -> None:
        """Второй поиск уходит, пока первый держит Byparr; потом Byparr отвечает."""
        with self.waiter_spy() as waiting:
            threads = [self.run_in_background(first)]
            self.assertTrue(http.entered.wait(5))
            threads.append(self.run_in_background(second))
            self.assertTrue(waiting.wait(5), "второй поиск ждёт первый")
            self.assertEqual(len(http.calls), 1)
            gate.set()
            for thread in threads:
                thread.join(5)

    def test_repeat_during_a_running_request_waits_for_it(self) -> None:
        gate = threading.Event()
        rutracker, _, http = clients(gate=gate)
        results: list[list[bot.Release]] = []
        self.run_two_searches(
            http, gate,
            lambda: results.append(rutracker.search("Интерстеллар 2014")),
            lambda: results.append(rutracker.search("интерстеллар 2014 ")),
        )
        self.assertEqual(len(http.calls), 1)
        self.assertEqual(len(results), 2)
        self.assertTrue(results[0])
        self.assertEqual(results[0], results[1])

    def test_failure_reaches_the_waiter_and_is_not_remembered(self) -> None:
        gate = threading.Event()
        crashed = FakeHttpResponse(500, {"status": "error", "message": "browser crashed"})
        rutracker, _, http = clients(gate=gate, failure=crashed)
        errors: list[BaseException] = []

        def search() -> None:
            try:
                rutracker.search("Интерстеллар 2014")
            except bot.ByparrError as exc:
                errors.append(exc)

        self.run_two_searches(http, gate, search, search)
        self.assertEqual(len(errors), 2)
        self.assertEqual(len(http.calls), 1)
        http.failure = None
        self.assertTrue(rutracker.search("Интерстеллар 2014"), "после отказа — новый запрос, а не память")
        self.assertEqual(len(http.calls), 2)

    def test_two_pass_search_costs_one_page_per_tracker(self) -> None:
        """Проходы ТВ на 4 и 45 с: второй забирает ответы первого, не спрашивая Byparr снова."""
        gate = threading.Event()
        rutracker, kinozal, http = clients(gate=gate)
        queries = ["Интерстеллар 2014", "Интерстеллар", "Interstellar"]
        _, status, _, partial = search_api.aggregate_releases([rutracker, kinozal], queries, 0.05)
        self.assertTrue(partial)
        self.assertEqual(status, {"RuTracker": "таймаут", "Kinozal": "таймаут"})
        opener = threading.Timer(0.2, gate.set)
        opener.start()
        releases, status, _, partial = search_api.aggregate_releases([rutracker, kinozal], queries, 5)
        opener.join()
        self.assertFalse(partial)
        self.assertEqual(len(http.calls), 2, "по странице на трекер на оба прохода")
        self.assertEqual(set(status), {"RuTracker", "Kinozal"})
        self.assertNotIn("таймаут", status.values())
        self.assertTrue(any(release.provider == "rutracker" for release in releases))
        self.assertTrue(any(release.provider == "kinozal" for release in releases))


if __name__ == "__main__":
    unittest.main()
