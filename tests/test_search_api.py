"""Офлайн-тесты мини-API: кэш, маппинг ответов и авторизация. Сеть не используется."""

from __future__ import annotations

import base64
import contextlib
import hashlib
import logging
import sys
import tempfile
import threading
import time
import unittest
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import bencodepy
from fastapi import HTTPException

import meta_service
import search_api
import torbox_tg_bot as bot


def make_release(**overrides: object) -> bot.Release:
    values: dict[str, object] = {
        "torrent_id": "42",
        "topic_id": "100500",
        "title": "Тестовый фильм (2024) 1080p BDRip",
        "category": "Фильмы",
        "size_text": "8.2 GB",
        "size_bytes": 8_804_682_956,
        "seeders": 25,
        "leechers": 3,
        "source_url": "https://example.org/topic/100500",
        "download_url": "https://example.org/download/42",
        "score": 100,
        "provider": "nnm",
        "source_name": "NNM-Club",
        "info_hash": "a" * 40,
        "magnet_url": "",
        "details_text": "",
    }
    values.update(overrides)
    return bot.Release(**values)  # type: ignore[arg-type]


class ReleaseCacheTests(unittest.TestCase):
    def test_put_and_get_roundtrip(self) -> None:
        cache = search_api.ReleaseCache(ttl_seconds=3600)
        release = make_release()
        [(release_id, stored)] = cache.put_all([release])
        self.assertEqual(stored, release)
        self.assertEqual(cache.get(release_id), release)

    def test_expired_entry_is_gone(self) -> None:
        cache = search_api.ReleaseCache(ttl_seconds=0)
        [(release_id, _)] = cache.put_all([make_release()])
        self.assertIsNone(cache.get(release_id))

    def test_unknown_id_returns_none(self) -> None:
        cache = search_api.ReleaseCache()
        self.assertIsNone(cache.get("missing"))


class PayloadTests(unittest.TestCase):
    def test_release_payload_fields(self) -> None:
        release = make_release(magnet_url="magnet:?xt=urn:btih:" + "a" * 40)
        payload = search_api.release_payload("rid-1", release)
        self.assertEqual(payload["id"], "rid-1")
        self.assertEqual(payload["title"], release.title)
        self.assertEqual(payload["size_bytes"], release.size_bytes)
        self.assertEqual(payload["seeders"], 25)
        self.assertEqual(payload["magnet_url"], release.magnet_url)
        self.assertEqual(payload["info_hash"], "a" * 40)
        self.assertIn("quality_summary", payload)
        self.assertIn("voiceover_summary", payload)
        # Сводка качества собирается из названия релиза
        self.assertIn("1080p", payload["quality_summary"])

    def test_catalog_payload_fields(self) -> None:
        title = bot.CatalogTitle(
            catalog_id=301,
            name="Матрица",
            original_name="The Matrix",
            media_type="FILM",
            year="1999",
            rating="8.5",
            countries=("США",),
            genres=("фантастика",),
            poster_url="https://example.org/poster.jpg",
        )
        payload = search_api.catalog_payload(title)
        self.assertEqual(payload["catalog_id"], 301)
        self.assertEqual(payload["search_query"], "Матрица 1999")
        self.assertEqual(payload["countries"], ["США"])


class AuthTests(unittest.TestCase):
    def setUp(self) -> None:
        self._saved_token = search_api.API_TOKEN

    def tearDown(self) -> None:
        search_api.API_TOKEN = self._saved_token

    def test_disabled_without_token(self) -> None:
        search_api.API_TOKEN = ""
        self.assertIsNone(search_api.require_token(""))

    def test_rejects_wrong_token(self) -> None:
        search_api.API_TOKEN = "secret"
        with self.assertRaises(HTTPException) as ctx:
            search_api.require_token("Bearer wrong")
        self.assertEqual(ctx.exception.status_code, 401)

    def test_accepts_valid_token(self) -> None:
        search_api.API_TOKEN = "secret"
        self.assertIsNone(search_api.require_token("Bearer secret"))


class CachedReleaseLookupTests(unittest.TestCase):
    def test_missing_release_raises_404(self) -> None:
        with self.assertRaises(HTTPException) as ctx:
            search_api.get_cached_release("gone")
        self.assertEqual(ctx.exception.status_code, 404)


if __name__ == "__main__":
    unittest.main()


class StableIdTests(unittest.TestCase):
    def test_nnm_id_is_provider_and_topic(self) -> None:
        release = make_release(provider="nnm", topic_id="100500")
        self.assertEqual(search_api.stable_release_id(release), "nnm:100500")

    def test_nnm_id_survives_a_new_search(self) -> None:
        # Тот же релиз в следующей выдаче получает тот же идентификатор:
        # на этом держится привязка «тайтл → раздача» в телефоне
        first = make_release(provider="nnm", topic_id="100500", seeders=25)
        later = make_release(provider="nnm", topic_id="100500", seeders=31, score=140)
        self.assertEqual(search_api.stable_release_id(first), search_api.stable_release_id(later))

    def test_prowlarr_id_names_the_indexer(self) -> None:
        release = make_release(
            provider="prowlarr",
            source_name="RuTracker.org",
            source_url="https://rutracker.org/forum/viewtopic.php?t=12",
        )
        release_id = search_api.stable_release_id(release)
        self.assertTrue(release_id.startswith("prowlarr:rutracker-org:"), release_id)

    def test_prowlarr_ids_differ_per_topic(self) -> None:
        first = make_release(provider="prowlarr", source_name="RuTor", source_url="https://rutor.info/1")
        second = make_release(provider="prowlarr", source_name="RuTor", source_url="https://rutor.info/2")
        self.assertNotEqual(search_api.stable_release_id(first), search_api.stable_release_id(second))

    def test_id_is_safe_inside_a_url_path(self) -> None:
        release = make_release(provider="prowlarr", source_name="Rutracker (RU)", source_url="https://x/1")
        self.assertNotRegex(search_api.stable_release_id(release), r"[\s/?#]")


def temp_store(ttl: int = 3600) -> meta_service.TTLCache:
    return meta_service.TTLCache(Path(tempfile.mkdtemp()) / "store.json", ttl)


class ReleaseStoreTests(unittest.TestCase):
    def test_release_survives_process_restart(self) -> None:
        store = temp_store()
        release = make_release(provider="nnm", topic_id="100500")
        [(release_id, _)] = search_api.ReleaseCache(store=store).put_all([release])

        # Новый процесс: память пуста, на диске — то же хранилище
        restarted = search_api.ReleaseCache(store=meta_service.TTLCache(store._path, 3600))
        self.assertEqual(restarted.get(release_id), release)

    def test_memory_only_cache_forgets_expired_entry(self) -> None:
        cache = search_api.ReleaseCache(ttl_seconds=0)
        [(release_id, _)] = cache.put_all([make_release()])
        self.assertIsNone(cache.get(release_id))

    def test_unknown_id_is_not_found_in_store(self) -> None:
        self.assertIsNone(search_api.ReleaseCache(store=temp_store()).get("nnm:1"))


class FakeResponse:
    def __init__(self, text: str = "", content: bytes = b"") -> None:
        self.text = text
        self.content = content

    def raise_for_status(self) -> None:
        return None


class FakeNnmClient:
    key = "nnm"
    name = "NNM-Club"

    def __init__(self, page: str = "", torrent: bytes = b"") -> None:
        self.http = self
        self.page = page
        self.torrent = torrent
        self.downloads = 0

    def get(self, url: str, **kwargs: object) -> FakeResponse:
        return FakeResponse(text=self.page)

    def download_torrent(self, release: bot.Release) -> bytes:
        self.downloads += 1
        if not self.torrent:
            raise RuntimeError("трекер не отдал файл")
        return self.torrent


TOPIC_PAGE = """
<html><head><title>Сорвиголова / Daredevil (Сезон 1) :: NNM-Club</title></head>
<body><a class="maintitle" href="viewtopic.php?t=100500">Сорвиголова / Daredevil (Сезон 1) [2025, WEB-DL 1080p]</a>
<a href="download.php?id=777">Скачать .torrent</a>
<a href="magnet:?xt=urn:btih:0123456789abcdef0123456789abcdef01234567&amp;dn=test">magnet</a>
</body></html>
"""


class RestoreTests(unittest.TestCase):
    def test_topic_page_gives_download_link_and_hash(self) -> None:
        client = FakeNnmClient(page=TOPIC_PAGE)
        release = search_api.restore_nnm_topic(client, "100500")
        assert release is not None
        self.assertEqual(release.torrent_id, "777")
        self.assertEqual(release.topic_id, "100500")
        self.assertIn("download.php?id=777", release.download_url)
        self.assertEqual(release.info_hash, "0123456789abcdef0123456789abcdef01234567")
        self.assertIn("Сорвиголова", release.title)
        self.assertNotIn("NNM-Club", release.title)

    def test_page_without_download_link_restores_nothing(self) -> None:
        self.assertIsNone(search_api.restore_nnm_topic(FakeNnmClient(page="<html></html>"), "1"))

    def test_torrent_endpoint_restores_release_after_restart(self) -> None:
        client = FakeNnmClient(page=TOPIC_PAGE, torrent=SMALL_TORRENT)
        with providers_replaced([client]), cache_replaced(search_api.ReleaseCache()):
            # Кэш пуст, как после перезапуска: 404 быть не должно
            payload = search_api.release_torrent("nnm:100500")
        self.assertEqual(payload.media_type, "application/x-bittorrent")
        self.assertEqual(payload.body, SMALL_TORRENT)

    def test_unknown_prowlarr_id_still_gives_404(self) -> None:
        with cache_replaced(search_api.ReleaseCache()):
            with self.assertRaises(HTTPException) as ctx:
                search_api.get_cached_release("prowlarr:rutor:deadbeefdeadbeef")
        self.assertEqual(ctx.exception.status_code, 404)


INFO_DICT = {
    b"length": 1024,
    b"name": b"release.mkv",
    b"piece length": 16384,
    b"pieces": b"\x01" * 20,
}
SMALL_TORRENT = bencodepy.encode({b"announce": b"https://tracker.test/ann", b"info": INFO_DICT})
EXPECTED_HASH = hashlib.sha1(bencodepy.encode(INFO_DICT)).hexdigest()


@contextlib.contextmanager
def providers_replaced(providers: list[object]) -> Iterator[None]:
    saved = (search_api.PROVIDERS, search_api.PROVIDER_BY_KEY)
    search_api.PROVIDERS = providers  # type: ignore[assignment]
    search_api.PROVIDER_BY_KEY = {provider.key: provider for provider in providers}  # type: ignore[attr-defined]
    try:
        yield
    finally:
        search_api.PROVIDERS, search_api.PROVIDER_BY_KEY = saved


@contextlib.contextmanager
def cache_replaced(cache: search_api.ReleaseCache) -> Iterator[None]:
    saved_cache, saved_hashes = search_api.CACHE, search_api.HASH_CACHE
    search_api.CACHE = cache
    search_api.HASH_CACHE = temp_store()
    try:
        yield
    finally:
        search_api.CACHE, search_api.HASH_CACHE = saved_cache, saved_hashes


class InfoHashTests(unittest.TestCase):
    def test_hash_is_sha1_of_the_info_dictionary(self) -> None:
        client = FakeNnmClient(torrent=SMALL_TORRENT)
        cache = search_api.ReleaseCache()
        release = make_release(provider="nnm", topic_id="100500", info_hash="", magnet_url="")
        with providers_replaced([client]), cache_replaced(cache):
            cache.put_all([release])
            value = search_api.release_info_hash("nnm:100500", release)
        self.assertEqual(value, EXPECTED_HASH)
        self.assertRegex(value or "", r"^[0-9a-f]{40}$")

    def test_hash_is_cached_for_a_day(self) -> None:
        client = FakeNnmClient(torrent=SMALL_TORRENT)
        release = make_release(provider="nnm", topic_id="100500", info_hash="", magnet_url="")
        with providers_replaced([client]), cache_replaced(search_api.ReleaseCache()):
            search_api.release_info_hash("nnm:100500", release)
            search_api.release_info_hash("nnm:100500", release)
        self.assertEqual(client.downloads, 1)

    def test_known_hash_does_not_touch_the_tracker(self) -> None:
        client = FakeNnmClient(torrent=SMALL_TORRENT)
        release = make_release(provider="nnm", info_hash="A" * 40)
        with providers_replaced([client]), cache_replaced(search_api.ReleaseCache()):
            value = search_api.release_info_hash("nnm:1", release)
        self.assertEqual(value, "a" * 40)
        self.assertEqual(client.downloads, 0)

    def test_base32_magnet_hash_becomes_hex40(self) -> None:
        raw = base64.b32encode(bytes.fromhex(EXPECTED_HASH)).decode()
        release = make_release(provider="nnm", info_hash="", magnet_url=f"magnet:?xt=urn:btih:{raw}")
        with providers_replaced([FakeNnmClient()]), cache_replaced(search_api.ReleaseCache()):
            self.assertEqual(search_api.release_info_hash("nnm:2", release), EXPECTED_HASH)

    def test_one_failing_release_does_not_break_the_batch(self) -> None:
        good = FakeNnmClient(torrent=SMALL_TORRENT)
        cache = search_api.ReleaseCache()
        with providers_replaced([good]), cache_replaced(cache):
            cache.put_all([
                make_release(provider="nnm", topic_id="1", info_hash="", magnet_url=""),
                make_release(provider="nnm", topic_id="2", info_hash="B" * 40),
            ])
            # Первый релиз отдаёт файл, у второго хэш уже известен,
            # третьего в кэше нет вовсе — и это не должно ронять ответ
            result = search_api.release_hashes(
                search_api.HashRequest(ids=["nnm:1", "nnm:2", "nnm:404"], deadline=5)
            )
        self.assertEqual(result.hashes["nnm:1"], EXPECTED_HASH)
        self.assertEqual(result.hashes["nnm:2"], "b" * 40)
        self.assertIsNone(result.hashes["nnm:404"])

    def test_batch_is_capped(self) -> None:
        with providers_replaced([FakeNnmClient()]), cache_replaced(search_api.ReleaseCache()):
            result = search_api.release_hashes(
                search_api.HashRequest(ids=[f"nnm:{index}" for index in range(50)], deadline=2)
            )
        self.assertEqual(result.limit, search_api.DEFAULT_HASH_IDS)
        self.assertEqual(len(result.hashes), search_api.DEFAULT_HASH_IDS)

    def test_batch_limit_never_exceeds_the_ceiling(self) -> None:
        with providers_replaced([FakeNnmClient()]), cache_replaced(search_api.ReleaseCache()):
            result = search_api.release_hashes(
                search_api.HashRequest(ids=[f"nnm:{index}" for index in range(50)], limit=999, deadline=2)
            )
        self.assertEqual(result.limit, search_api.MAX_HASH_IDS)


class StaticProvider:
    """Источник, который отдаёт заранее заданные релизы на любой запрос."""

    def __init__(self, key: str, name: str, releases: list[bot.Release]) -> None:
        self.key, self.name, self.releases = key, name, releases
        self.queries: list[str] = []

    def search(self, query: str) -> list[bot.Release]:
        self.queries.append(query)
        return list(self.releases)

    def topic_summary(self, release: bot.Release) -> str:
        return ""

    def download_torrent(self, release: bot.Release) -> bytes:
        return SMALL_TORRENT


class SlowProvider(StaticProvider):
    """Трекер, который думает дольше, чем клиент готов ждать."""

    def __init__(self, key: str, name: str, delay: float) -> None:
        super().__init__(key, name, [])
        self.delay = delay
        self.finished = threading.Event()

    def search(self, query: str) -> list[bot.Release]:
        time.sleep(self.delay)
        self.finished.set()
        return []


def video_release(title: str, **overrides: object) -> bot.Release:
    return make_release(title=title, category="Кино", **overrides)


# Поля, которые разбирает выпущенный TorBox One. Набор заморожен: любое
# переименование здесь ломает приложение, которое уже стоит на телефоне.
LEGACY_RESULT_FIELDS = (
    "id", "title", "size_text", "size_bytes", "seeders", "leechers", "source_name",
    "category", "magnet_url", "info_hash", "quality_summary", "voiceover_summary", "source_url",
)


class SearchCompatibilityTests(unittest.TestCase):
    def test_old_call_keeps_every_old_field(self) -> None:
        release = video_release("Дюна 2021 WEB-DL 1080p Дублированный")
        provider = StaticProvider("nnm", "NNM-Club", [release])
        with providers_replaced([provider]), cache_replaced(search_api.ReleaseCache()):
            payload = search_api.tracker_search(q="Дюна 2021").model_dump()

        self.assertEqual(provider.queries, ["Дюна 2021"])
        self.assertEqual(payload["status"], {"NNM-Club": "1"})
        [item] = payload["results"]
        for field in LEGACY_RESULT_FIELDS:
            self.assertIn(field, item)
        self.assertEqual(item["title"], release.title)
        self.assertEqual(item["size_text"], release.size_text)
        self.assertEqual(item["size_bytes"], release.size_bytes)
        self.assertEqual(item["seeders"], release.seeders)
        self.assertEqual(item["leechers"], release.leechers)
        self.assertEqual(item["source_name"], release.source_name)
        self.assertEqual(item["category"], release.category)
        self.assertEqual(item["magnet_url"], release.magnet_url)
        self.assertEqual(item["info_hash"], release.info_hash)
        self.assertEqual(item["source_url"], release.source_url)
        self.assertIn("1080p", item["quality_summary"])
        self.assertEqual(item["voiceover_summary"], bot.voiceover_summary(release))

    def test_old_call_still_returns_eight_results(self) -> None:
        # Разные хэши: иначе release_dedupe_key схлопнет их в одну раздачу
        releases = [
            video_release(f"Дюна 2021 WEB-DL 1080p вариант {index}", info_hash=f"{index:040x}")
            for index in range(20)
        ]
        with providers_replaced([StaticProvider("nnm", "NNM-Club", releases)]):
            with cache_replaced(search_api.ReleaseCache()):
                payload = search_api.tracker_search(q="Дюна 2021")
        self.assertEqual(len(payload.results), search_api.MAX_APP_RESULTS)
        self.assertFalse(payload.partial)

    def test_short_query_is_still_rejected(self) -> None:
        with providers_replaced([StaticProvider("nnm", "NNM-Club", [])]):
            with self.assertRaises(HTTPException) as ctx:
                search_api.tracker_search(q="a")
        self.assertEqual(ctx.exception.status_code, 400)

    def test_failed_provider_is_reported_as_before(self) -> None:
        class Failing(StaticProvider):
            def search(self, query: str) -> list[bot.Release]:
                raise RuntimeError("трекер лежит")

        good = StaticProvider("nnm", "NNM-Club", [video_release("Дюна 2021 WEB-DL 1080p")])
        with providers_replaced([Failing("bad", "Prowlarr", []), good]):
            with cache_replaced(search_api.ReleaseCache()):
                payload = search_api.tracker_search(q="Дюна 2021")
        self.assertEqual(payload.status, {"Prowlarr": "ошибка", "NNM-Club": "1"})
        self.assertFalse(payload.partial)


class SearchLimitsTests(unittest.TestCase):
    def test_limit_widens_the_candidate_list(self) -> None:
        releases = [
            video_release(
                f"Дюна 2021 WEB-DL 1080p издание {index}",
                source_name=f"Трекер {index}",
                info_hash=f"{index:040x}",
            )
            for index in range(30)
        ]
        with providers_replaced([StaticProvider("nnm", "NNM-Club", releases)]):
            with cache_replaced(search_api.ReleaseCache()):
                payload = search_api.tracker_search(q="Дюна 2021", limit=30)
        self.assertEqual(len(payload.results), 30)

    def test_limit_never_exceeds_the_ceiling(self) -> None:
        releases = [
            video_release(
                f"Дюна 2021 WEB-DL 1080p издание {index}",
                source_name=f"Трекер {index}",
                info_hash=f"{index:040x}",
            )
            for index in range(60)
        ]
        with providers_replaced([StaticProvider("nnm", "NNM-Club", releases)]):
            with cache_replaced(search_api.ReleaseCache()):
                payload = search_api.tracker_search(q="Дюна 2021", limit=500)
        self.assertLessEqual(len(payload.results), search_api.MAX_SEARCH_LIMIT)

    def test_deadline_returns_what_arrived_in_time(self) -> None:
        fast = StaticProvider("nnm", "NNM-Club", [video_release("Дюна 2021 WEB-DL 1080p")])
        slow = SlowProvider("slow", "Медленный трекер", delay=0.6)
        with providers_replaced([fast, slow]), cache_replaced(search_api.ReleaseCache()):
            started = time.monotonic()
            payload = search_api.tracker_search(q="Дюна 2021", deadline=0.15)
            elapsed = time.monotonic() - started

        self.assertTrue(payload.partial)
        self.assertEqual(payload.status["Медленный трекер"], "таймаут")
        self.assertEqual(payload.status["NNM-Club"], "1")
        self.assertEqual(len(payload.results), 1)
        self.assertLess(elapsed, 0.6, "ответ не должен ждать медленный источник")
        # Поток трекера доживает сам, ответ его не ждёт
        self.assertTrue(slow.finished.wait(timeout=5))

    def test_no_deadline_waits_for_everyone(self) -> None:
        fast = StaticProvider("nnm", "NNM-Club", [video_release("Дюна 2021 WEB-DL 1080p")])
        slow = SlowProvider("slow", "Медленный трекер", delay=0.2)
        with providers_replaced([fast, slow]), cache_replaced(search_api.ReleaseCache()):
            payload = search_api.tracker_search(q="Дюна 2021")
        self.assertFalse(payload.partial)
        self.assertEqual(payload.status["Медленный трекер"], "0")


class SearchPayloadTests(unittest.TestCase):
    def test_structural_fields_are_filled(self) -> None:
        release = video_release(
            "Сорвиголова / Daredevil (Сезон 1, серии 1-8 из 10) [2025, WEB-DL 1080p, H.265] Дубляж LostFilm",
            provider="nnm",
            topic_id="100500",
        )
        with providers_replaced([StaticProvider("nnm", "NNM-Club", [release])]):
            with cache_replaced(search_api.ReleaseCache()):
                payload = search_api.tracker_search(q="Сорвиголова").model_dump()
        [item] = payload["results"]
        self.assertEqual(item["id"], "nnm:100500")
        self.assertEqual(item["provider"], "nnm")
        self.assertEqual(item["content_kind"], "video")
        self.assertEqual(item["resolution"], 1080)
        self.assertEqual(item["source_type"], "web-dl")
        self.assertEqual(item["video_codec"], "h265")
        self.assertIsNone(item["hdr"])
        self.assertIn("dub", item["voiceover_kinds"])
        self.assertEqual(item["voiceover_studios"], ["LostFilm"])
        self.assertEqual((item["season_from"], item["season_to"]), (1, 1))
        self.assertEqual((item["episode_from"], item["episode_to"]), (1, 8))
        self.assertEqual(item["episodes_declared"], 10)
        self.assertGreater(item["score"], 0)

    def test_published_at_comes_from_the_indexer(self) -> None:
        release = video_release(
            "Дюна 2021 WEB-DL 1080p", provider="prowlarr", published_at="2025-03-14T12:00:00Z"
        )
        with providers_replaced([StaticProvider("prowlarr", "RuTracker.org", [release])]):
            with cache_replaced(search_api.ReleaseCache()):
                payload = search_api.tracker_search(q="Дюна 2021").model_dump()
        self.assertEqual(payload["results"][0]["published_at"], "2025-03-14T12:00:00Z")

    def test_missing_publish_date_is_null_not_empty_string(self) -> None:
        with providers_replaced([StaticProvider("nnm", "NNM-Club", [video_release("Дюна 2021 BDRip")])]):
            with cache_replaced(search_api.ReleaseCache()):
                payload = search_api.tracker_search(q="Дюна 2021").model_dump()
        self.assertIsNone(payload["results"][0]["published_at"])


class FakeMeta:
    """Подмена каталога: отдаёт готовую карточку без единого запроса."""

    def __init__(self, card: meta_service.TitleCard | None) -> None:
        self.card = card
        self.available = True
        self.asked: list[int] = []

    def title_card(self, kp_id: int) -> meta_service.TitleCard:
        self.asked.append(kp_id)
        if self.card is None:
            raise RuntimeError("каталог молчит")
        return self.card

    def episodes(self, film_id: int, season: int | None = None) -> list[meta_service.EpisodeMeta]:
        self.asked.append(film_id)
        return [meta_service.EpisodeMeta(season=season or 1, episode=1, title="Первая")]


@contextlib.contextmanager
def meta_replaced(meta: FakeMeta) -> Iterator[None]:
    saved = search_api.META
    search_api.META = meta  # type: ignore[assignment]
    try:
        yield
    finally:
        search_api.META = saved


def movie_card() -> meta_service.TitleCard:
    return meta_service.TitleCard(
        meta=meta_service.TitleMeta(
            matched=True, source="kinopoisk", source_id=301, title="Дюна",
            original_title="Dune", year="2021", media_type="FILM",
        )
    )


def series_card() -> meta_service.TitleCard:
    return meta_service.TitleCard(
        meta=meta_service.TitleMeta(
            matched=True, source="kinopoisk", source_id=4444, title="Игра престолов",
            original_title="Game of Thrones", year="2011", media_type="TV_SERIES",
        ),
        seasons=[meta_service.SeasonBrief(season=1, episodes=10)],
    )


class SearchQueryBuildingTests(unittest.TestCase):
    def test_free_text_stays_a_single_query(self) -> None:
        with meta_replaced(FakeMeta(None)):
            self.assertEqual(search_api.build_search_queries("Дюна 2021", None, None, None, None), ["Дюна 2021"])

    def test_movie_gets_year_and_original_name(self) -> None:
        with meta_replaced(FakeMeta(movie_card())):
            queries = search_api.build_search_queries("", 301, None, None, "movie")
        self.assertEqual(queries, ["Дюна 2021", "Дюна", "Dune"])

    def test_series_season_becomes_its_own_pass(self) -> None:
        with meta_replaced(FakeMeta(series_card())):
            queries = search_api.build_search_queries("", 4444, 4, None, "series")
        self.assertEqual(queries, ["Игра престолов сезон 4", "Игра престолов", "Game of Thrones"])

    def test_series_year_is_not_added_to_the_query(self) -> None:
        # Четвёртый сезон вышел в 2014, а год тайтла — 2011: запрос с годом
        # отсеял бы ровно те раздачи, которые ищем
        with meta_replaced(FakeMeta(series_card())):
            queries = search_api.build_search_queries("", 4444, 2, None, None)
        self.assertNotIn("2011", " ".join(queries))

    def test_explicit_year_beats_the_catalog(self) -> None:
        with meta_replaced(FakeMeta(movie_card())):
            queries = search_api.build_search_queries("", 301, None, 1984, "movie")
        self.assertEqual(queries[0], "Дюна 1984")

    def test_never_more_than_the_ceiling(self) -> None:
        with meta_replaced(FakeMeta(series_card())):
            queries = search_api.build_search_queries("своя строка", 4444, 4, None, "series")
        self.assertEqual(len(queries), search_api.MAX_SEARCH_QUERIES)
        self.assertEqual(queries[0], "своя строка")

    def test_silent_catalog_leaves_the_free_text(self) -> None:
        with meta_replaced(FakeMeta(None)):
            self.assertEqual(search_api.build_search_queries("Дюна", 301, None, None, None), ["Дюна"])

    def test_nothing_to_search_for(self) -> None:
        with meta_replaced(FakeMeta(None)):
            self.assertEqual(search_api.build_search_queries("", None, None, None, None), [])

    def test_kp_id_search_reaches_the_tracker(self) -> None:
        provider = StaticProvider("nnm", "NNM-Club", [video_release("Дюна 2021 WEB-DL 1080p")])
        with meta_replaced(FakeMeta(movie_card())), providers_replaced([provider]):
            with cache_replaced(search_api.ReleaseCache()):
                payload = search_api.tracker_search(kp_id=301, kind="movie")
        # Проходы идут параллельно, поэтому сверяем набор, а не порядок
        self.assertEqual(sorted(provider.queries), sorted(["Дюна 2021", "Дюна", "Dune"]))
        self.assertEqual(len(payload.results), 1)


class CatalogEndpointTests(unittest.TestCase):
    def test_title_card_endpoint_shape(self) -> None:
        with meta_replaced(FakeMeta(series_card())):
            result = search_api.catalog_title(4444).model_dump()
        self.assertTrue(result["meta"]["matched"])
        self.assertEqual(result["meta"]["title"], "Игра престолов")
        self.assertEqual(result["seasons"], [{"season": 1, "episodes": 10}])
        # tmdb_id и tmdb_media_type приходят внутри meta, рядом их нет
        self.assertNotIn("tmdb_id", result)
        self.assertIn("tmdb_id", result["meta"])
        self.assertIn("tmdb_media_type", result["meta"])

    def test_episodes_alias_matches_the_old_path(self) -> None:
        meta = FakeMeta(series_card())
        with meta_replaced(meta):
            new_path = search_api.catalog_episodes(4444, season=2)
            old_path = search_api.meta_episodes(4444, season=2)
        self.assertEqual(new_path, old_path)
        self.assertEqual(new_path[0]["season"], 2)

    def test_health_needs_no_external_call(self) -> None:
        payload = search_api.health().model_dump()
        self.assertEqual(payload["status"], "ok")
        self.assertEqual(payload["version"], search_api.API_VERSION)
        self.assertIn("token_required", payload)
        self.assertIn("providers", payload)

    def test_health_auth_is_behind_the_token(self) -> None:
        saved = search_api.API_TOKEN
        search_api.API_TOKEN = "secret"
        try:
            with self.assertRaises(HTTPException) as ctx:
                search_api.require_token("Bearer wrong")
            self.assertEqual(ctx.exception.status_code, 401)
            self.assertEqual(search_api.health_auth()["status"], "ok")
        finally:
            search_api.API_TOKEN = saved

    def test_quota_limit_is_reported_as_429(self) -> None:
        error = search_api.catalog_error(meta_service.CatalogLimitError("квота"))
        self.assertEqual(error.status_code, 429)

    def test_other_catalog_errors_stay_502(self) -> None:
        self.assertEqual(search_api.catalog_error(RuntimeError("сеть")).status_code, 502)


class DiscoverMeta(FakeMeta):
    """Каталог для маршрутов ленты: запоминает, с чем его позвали."""

    def __init__(self, error: Exception | None = None) -> None:
        super().__init__(None)
        self.error = error
        self.calls: list[tuple[str, Any]] = []

    def _answer(self, name: str, value: Any) -> list[dict[str, Any]]:
        self.calls.append((name, value))
        if self.error is not None:
            raise self.error
        return [{"catalog_id": 1, "detailed": name != "discover"}]

    def discover(self, **kwargs: Any) -> list[dict[str, Any]]:
        return self._answer("discover", kwargs)

    def similar(self, film_id: int, limit: int) -> list[dict[str, Any]]:
        return self._answer("similar", (film_id, limit))

    def briefs(self, film_ids: list[int]) -> list[dict[str, Any]]:
        return self._answer("briefs", film_ids)


class DiscoverEndpointTests(unittest.TestCase):
    def test_discover_passes_every_filter_through(self) -> None:
        meta = DiscoverMeta()
        with meta_replaced(meta):
            search_api.catalog_discover(
                media_type="FILM", genre="драма", country="США", year_from=2025,
                year_to=2026, rating_from=7.0, order="NUM_VOTE", page=2,
            )
        self.assertEqual(
            meta.calls,
            [("discover", {
                "media_type": "FILM", "genre": "драма", "country": "США", "year_from": 2025,
                "year_to": 2026, "rating_from": 7.0, "order": "NUM_VOTE", "page": 2,
            })],
        )

    def test_unknown_country_is_404(self) -> None:
        with meta_replaced(DiscoverMeta(ValueError("Каталог не знает страны «Атлантида»"))):
            with self.assertRaises(HTTPException) as ctx:
                search_api.catalog_discover(country="Атлантида")
        self.assertEqual(ctx.exception.status_code, 404)

    def test_quota_on_similar_is_429(self) -> None:
        with meta_replaced(DiscoverMeta(meta_service.CatalogLimitError("квота"))):
            with self.assertRaises(HTTPException) as ctx:
                search_api.catalog_similar(258687, limit=12)
        self.assertEqual(ctx.exception.status_code, 429)

    def test_briefs_take_ids_from_the_body(self) -> None:
        meta = DiscoverMeta()
        with meta_replaced(meta):
            cards = search_api.catalog_briefs(search_api.BriefRequest(ids=[301, 258687]))
        self.assertEqual(meta.calls, [("briefs", [301, 258687])])
        self.assertTrue(cards[0]["detailed"])

    def test_discover_route_reads_the_type_parameter(self) -> None:
        # Параметр в адресе — `type`, а имя в Python другое: `type` занят встроенным
        parameters = search_api.app.openapi()["paths"]["/catalog/discover"]["get"]["parameters"]
        self.assertIn("type", [parameter["name"] for parameter in parameters])


class SourceStatusTests(unittest.TestCase):
    """Живая проверка 21.09.2026: оба источника отказали (Cloudflare у NNM-Club,
    Prowlarr не поднят), а ответ выглядел как обычная пустая выдача. Отличить
    «ничего не нашлось» от «все трекеры легли» по строкам status нельзя."""

    def test_all_sources_down_is_visible_without_parsing_strings(self) -> None:
        class Failing(StaticProvider):
            def search(self, query: str) -> list[bot.Release]:
                raise RuntimeError("403 Forbidden")

        with providers_replaced([Failing("nnm", "NNM-Club", []), Failing("p", "Prowlarr", [])]):
            with cache_replaced(search_api.ReleaseCache()):
                payload = search_api.tracker_search(q="Интерстеллар 2014")

        self.assertEqual(payload.results, [])
        self.assertFalse(payload.partial)
        self.assertEqual({source.state for source in payload.sources}, {"error"})
        self.assertTrue(all(source.found == 0 for source in payload.sources))
        # Прежний словарь строк не изменился: его печатает бот и читает TorBox One
        self.assertEqual(payload.status, {"NNM-Club": "ошибка", "Prowlarr": "ошибка"})

    def test_empty_result_from_a_working_source_is_not_an_error(self) -> None:
        with providers_replaced([StaticProvider("nnm", "NNM-Club", [])]):
            with cache_replaced(search_api.ReleaseCache()):
                payload = search_api.tracker_search(q="Интерстеллар 2014")
        self.assertEqual([source.state for source in payload.sources], ["ok"])
        self.assertEqual(payload.status, {"NNM-Club": "0"})

    def test_timeout_state_matches_the_string_status(self) -> None:
        fast = StaticProvider("nnm", "NNM-Club", [video_release("Дюна 2021 WEB-DL 1080p")])
        slow = SlowProvider("slow", "Медленный трекер", delay=0.6)
        with providers_replaced([fast, slow]), cache_replaced(search_api.ReleaseCache()):
            payload = search_api.tracker_search(q="Дюна 2021", deadline=0.15)
        by_name = {source.name: source for source in payload.sources}
        self.assertEqual(by_name["Медленный трекер"].state, "timeout")
        self.assertEqual(by_name["NNM-Club"].state, "ok")
        self.assertEqual(by_name["NNM-Club"].found, 1)
        self.assertTrue(slow.finished.wait(timeout=5))

    def test_sources_and_status_never_disagree(self) -> None:
        class Failing(StaticProvider):
            def search(self, query: str) -> list[bot.Release]:
                raise RuntimeError("трекер лежит")

        good = StaticProvider("nnm", "NNM-Club", [video_release("Дюна 2021 WEB-DL 1080p")])
        with providers_replaced([good, Failing("p", "Prowlarr", [])]):
            with cache_replaced(search_api.ReleaseCache()):
                payload = search_api.tracker_search(q="Дюна 2021")
        expected = {"ok": "1", "error": "ошибка", "timeout": "таймаут"}
        for source in payload.sources:
            self.assertEqual(payload.status[source.name], expected[source.state])


class FuturesTimeoutTests(unittest.TestCase):
    """До Python 3.11 concurrent.futures.TimeoutError — отдельный класс, а не
    встроенный TimeoutError. На боевом сервере Python 3.10.12, и `except
    TimeoutError` там дедлайн не ловил: вместо partial клиент получал 500.
    На Mac (3.14) оба имени указывают на один объект, поэтому обычные тесты
    дедлайна ошибку не видели."""

    def test_handler_catches_the_futures_class_not_the_builtin(self) -> None:
        import concurrent.futures

        self.assertIs(search_api.FuturesTimeout, concurrent.futures.TimeoutError)

    def test_deadline_survives_the_futures_timeout(self) -> None:
        import concurrent.futures

        def raise_timeout(*args: object, **kwargs: object) -> object:
            raise concurrent.futures.TimeoutError("срок вышел")

        provider = StaticProvider("nnm", "NNM-Club", [video_release("Дюна 2021 WEB-DL 1080p")])
        saved = search_api.as_completed
        search_api.as_completed = raise_timeout  # type: ignore[assignment]
        try:
            with providers_replaced([provider]), cache_replaced(search_api.ReleaseCache()):
                payload = search_api.tracker_search(q="Дюна 2021", deadline=5)
        finally:
            search_api.as_completed = saved  # type: ignore[assignment]
        self.assertTrue(payload.partial)
        self.assertEqual([source.state for source in payload.sources], ["timeout"])

    def test_hashes_survive_the_futures_timeout(self) -> None:
        import concurrent.futures

        def raise_timeout(*args: object, **kwargs: object) -> object:
            raise concurrent.futures.TimeoutError("срок вышел")

        saved = search_api.as_completed
        search_api.as_completed = raise_timeout  # type: ignore[assignment]
        try:
            with providers_replaced([FakeNnmClient(torrent=SMALL_TORRENT)]):
                with cache_replaced(search_api.ReleaseCache()):
                    result = search_api.release_hashes(
                        search_api.HashRequest(ids=["nnm:1"], deadline=5)
                    )
        finally:
            search_api.as_completed = saved  # type: ignore[assignment]
        self.assertEqual(result.hashes, {"nnm:1": None})


class RealTopicPageTests(unittest.TestCase):
    """Страница темы NNM-Club, снятая живьём 21.09.2026 и вычищенная.

    Смотрел анонимно, без входа: ни `sid=`, ни passkey, ни ссылок на профиль
    на странице нет — проверено поиском перед сохранением."""

    def setUp(self) -> None:
        path = Path(__file__).parent / "fixtures" / "nnm-topic.html"
        self.page = path.read_text(encoding="utf-8")

    def test_restore_reads_attachment_id_title_and_magnet(self) -> None:
        release = search_api.restore_nnm_topic(FakeNnmClient(page=self.page), "1201971")
        assert release is not None
        # Ссылка на .torrent живёт под номером вложения, а не темы
        self.assertEqual(release.torrent_id, "979391")
        self.assertEqual(release.topic_id, "1201971")
        self.assertIn("download.php?id=979391", release.download_url)
        self.assertTrue(release.title.startswith("Интерстеллар / Interstellar (2014)"))
        self.assertNotIn("NNM-Club", release.title)

    def test_magnet_on_the_page_gives_the_hash_for_free(self) -> None:
        release = search_api.restore_nnm_topic(FakeNnmClient(page=self.page), "1201971")
        assert release is not None
        self.assertTrue(release.magnet_url.startswith("magnet:?xt=urn:btih:"))
        # Живьём этот же хэш совпал с sha1 от info-словаря скачанного .torrent
        self.assertEqual(
            search_api.normalized_info_hash(release.info_hash),
            "af9c4501de08d591611b3717c82a0b0fe3e1e0e9",
        )

    def test_restored_release_needs_no_torrent_download_for_its_hash(self) -> None:
        client = FakeNnmClient(page=self.page, torrent=SMALL_TORRENT)
        with providers_replaced([client]), cache_replaced(search_api.ReleaseCache()):
            value = search_api.release_info_hash(
                "nnm:1201971", search_api.restore_nnm_topic(client, "1201971")
            )
        self.assertEqual(value, "af9c4501de08d591611b3717c82a0b0fe3e1e0e9")
        self.assertEqual(client.downloads, 0, "хэш берётся из magnet, .torrent не качаем")


class LogRedactionTests(unittest.TestCase):
    """TMDB принимает ключ параметром адреса, поэтому его адрес попадает в
    текст ошибки requests и в предупреждения urllib3 о повторах — то есть в
    журнал сервиса. Проверено живьём: без вырезания ключ виден целиком."""

    def format_record(self, message: str, exc: Exception | None = None) -> str:
        formatter = search_api.RedactingFormatter(search_api.LOG_FORMAT)
        record = logging.LogRecord(
            name="проба", level=logging.WARNING, pathname=__file__, lineno=1,
            msg=message, args=(), exc_info=None,
        )
        if exc is not None:
            try:
                raise exc
            except Exception:
                record.exc_info = sys.exc_info()
        return formatter.format(record)

    def test_tmdb_key_in_a_url_is_cut(self) -> None:
        line = self.format_record(
            "TMDB не отдал кадры: https://api.themoviedb.org/3/tv/1399/season/1?api_key=0123456789abcdef0123456789abcdef"
        )
        self.assertNotIn("0123456789abcdef", line)
        self.assertIn("api_key=<вырезано>", line)

    def test_key_inside_a_traceback_is_cut(self) -> None:
        error = RuntimeError(
            "500 Server Error for url: https://api.themoviedb.org/3/tv/1/season/1?api_key=SEKRETSEKRETSEKRET"
        )
        line = self.format_record("TMDB не отдал кадры", exc=error)
        self.assertNotIn("SEKRET", line)
        self.assertIn("api_key=<вырезано>", line)

    def test_bearer_and_passkey_are_cut_too(self) -> None:
        line = self.format_record("ссылка https://tracker.test/dl?passkey=abcdef0123&token=zzz9")
        self.assertNotIn("abcdef0123", line)
        self.assertNotIn("zzz9", line)

    def test_ordinary_lines_are_untouched(self) -> None:
        line = self.format_record("Поиск ['Дюна 2021']: 8 релизов")
        self.assertIn("Поиск ['Дюна 2021']: 8 релизов", line)
