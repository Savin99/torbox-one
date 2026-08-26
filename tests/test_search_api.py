"""Офлайн-тесты мини-API: кэш, маппинг ответов и авторизация. Сеть не используется."""

from __future__ import annotations

import unittest

from fastapi import HTTPException

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
