"""Офлайн-тесты слоя метаданных: разбор имён, матчинг и кэш. Сеть не используется."""

from __future__ import annotations

import json
import tempfile
import time
import unittest
from pathlib import Path
from typing import Any

import meta_service


class ParseReleaseNameTests(unittest.TestCase):
    def test_plain_series_name(self) -> None:
        parsed = meta_service.parse_release_name("Game of Thrones")
        self.assertEqual(parsed.queries, ["Game of Thrones"])
        self.assertIsNone(parsed.year)
        self.assertIsNone(parsed.season)
        self.assertIsNone(parsed.is_series)

    def test_dual_title_with_season_and_year(self) -> None:
        parsed = meta_service.parse_release_name(
            "Игра Престолов / Game of Thrones (Сезон 4) [2014, BDRip 1080p]"
        )
        self.assertEqual(parsed.queries, ["Game of Thrones", "Игра Престолов"])
        self.assertEqual(parsed.year, 2014)
        self.assertEqual(parsed.season, 4)
        self.assertTrue(parsed.is_series)

    def test_season_range_gives_no_season_hint(self) -> None:
        parsed = meta_service.parse_release_name(
            "Игра престолов / Game of Thrones (Сезон 1-8) / 2011-2019, WEB-DL"
        )
        self.assertIsNone(parsed.season)
        self.assertTrue(parsed.is_series)

    def test_dotted_movie_name_drops_release_tail(self) -> None:
        parsed = meta_service.parse_release_name("Sharp.Obsession.2025.1080p.WEB-DL.x264")
        self.assertEqual(parsed.queries, ["Sharp Obsession"])
        self.assertEqual(parsed.year, 2025)

    def test_latin_query_goes_first(self) -> None:
        parsed = meta_service.parse_release_name("Интерстеллар / Interstellar (2014) BDRemux")
        self.assertEqual(parsed.queries[0], "Interstellar")


class TitleSimilarityTests(unittest.TestCase):
    def test_exact_match(self) -> None:
        self.assertEqual(meta_service.title_similarity("Interstellar", ["Interstellar"]), 1.0)

    def test_case_and_punctuation_insensitive(self) -> None:
        self.assertEqual(
            meta_service.title_similarity("Игра престолов", ["Игра Престолов!"]), 1.0
        )

    def test_partial_overlap_is_penalised(self) -> None:
        self.assertLess(meta_service.title_similarity("Sharp Obsession", ["Sharp Corner"]), 0.6)

    def test_longer_name_reduces_similarity(self) -> None:
        self.assertLess(meta_service.title_similarity("Obsession", ["Sharp Obsession"]), 1.0)


class ScoreCandidateTests(unittest.TestCase):
    def test_different_title_is_rejected(self) -> None:
        parsed = meta_service.parse_release_name("Sharp.Obsession.2025.1080p")
        item = {"nameRu": "Крутой поворот", "nameEn": "Sharp Corner", "year": "2024", "type": "FILM"}
        self.assertLess(meta_service.score_candidate(item, parsed, "Sharp Obsession"), 0)

    def test_exact_series_match_scores_high(self) -> None:
        parsed = meta_service.parse_release_name("Игра Престолов / Game of Thrones (Сезон 4) [2014]")
        item = {
            "nameRu": "Игра престолов",
            "nameEn": "Game of Thrones",
            "year": "2011",
            "type": "TV_SERIES",
            "rating": "9.0",
        }
        self.assertGreater(meta_service.score_candidate(item, parsed, "Game of Thrones"), 6)

    def test_movie_penalised_when_name_says_series(self) -> None:
        parsed = meta_service.parse_release_name("Некий сериал (Сезон 2) [2020]")
        series = {"nameRu": "Некий сериал", "year": "2020", "type": "TV_SERIES"}
        movie = {"nameRu": "Некий сериал", "year": "2020", "type": "FILM"}
        self.assertGreater(
            meta_service.score_candidate(series, parsed, "Некий сериал"),
            meta_service.score_candidate(movie, parsed, "Некий сериал"),
        )


class FakeKinopoisk:
    """Подмена сетевого клиента: отдаёт заранее заданные ответы."""

    def __init__(self, results: dict[str, list[dict[str, Any]]]) -> None:
        self.results = results
        self.queries: list[str] = []

    def search(self, query: str) -> list[dict[str, Any]]:
        self.queries.append(query)
        return self.results.get(query, [])

    def film(self, film_id: int) -> dict[str, Any]:
        return {
            "kinopoiskId": film_id,
            "nameRu": "Обсессия",
            "nameOriginal": "Obsession",
            "year": "2025",
            "type": "FILM",
            "ratingKinopoisk": "7.1",
            "posterUrl": "http://example.org/poster.jpg",
            "coverUrl": "https://example.org/cover.jpg",
            "filmLength": "97",
            "genres": [{"genre": "триллер"}],
        }

    def staff(self, film_id: int) -> list[dict[str, Any]]:
        return [
            {"nameRu": "Режиссёр", "professionKey": "DIRECTOR", "posterUrl": "http://e/1.jpg"},
            {
                "nameRu": "Актриса",
                "description": "Саманта",
                "professionKey": "ACTOR",
                "posterUrl": "https://e/2.jpg",
            },
        ]

    def seasons(self, film_id: int) -> list[dict[str, Any]]:
        return []


def make_service(results: dict[str, list[dict[str, Any]]]) -> tuple[meta_service.MetaService, FakeKinopoisk]:
    fake = FakeKinopoisk(results)
    cache = meta_service.TTLCache(path=Path(tempfile.mkdtemp()) / "cache.json")
    service = meta_service.MetaService(kinopoisk=fake, tmdb=None, cache=cache)  # type: ignore[arg-type]
    return service, fake


class MetaServiceMatchTests(unittest.TestCase):
    def test_no_match_when_catalog_empty(self) -> None:
        service, _ = make_service({})
        meta = service.match("Sharp.Obsession.2025.1080p.WEB-DL")
        self.assertFalse(meta.matched)

    def test_wrong_title_is_not_accepted(self) -> None:
        service, _ = make_service(
            {
                "Sharp Obsession": [],
                "Sharp": [
                    {"filmId": 1, "nameEn": "Sharp", "year": "2013", "type": "FILM"},
                ],
            }
        )
        # Год не совпал (2013 против 2025) — укороченный запрос не принимаем
        self.assertFalse(service.match("Sharp.Obsession.2025.1080p").matched)

    def test_short_variant_accepted_when_year_matches(self) -> None:
        service, fake = make_service(
            {
                "Obsession MA": [],
                "Obsession": [
                    {
                        "filmId": 77,
                        "nameRu": "Обсессия",
                        "nameEn": "Obsession",
                        "year": "2025",
                        "type": "FILM",
                        "rating": "7.1",
                    }
                ],
            }
        )
        meta = service.match("Obsession.2025.MA.x265.WEB-DL.2160p")
        self.assertTrue(meta.matched)
        self.assertEqual(meta.source_id, 77)
        self.assertEqual(meta.title, "Обсессия")
        self.assertEqual(meta.duration_minutes, 97)
        self.assertEqual(meta.poster_url, "https://example.org/poster.jpg")
        self.assertEqual([person["profession"] for person in meta.cast], ["director", "actor"])
        self.assertIn("Obsession MA", fake.queries)

    def test_match_is_cached(self) -> None:
        service, fake = make_service(
            {
                "Obsession": [
                    {
                        "filmId": 77,
                        "nameRu": "Обсессия",
                        "nameEn": "Obsession",
                        "year": "2025",
                        "type": "FILM",
                    }
                ]
            }
        )
        service.match("Obsession (2025)")
        first_calls = len(fake.queries)
        service.match("Obsession (2025)")
        self.assertEqual(len(fake.queries), first_calls)


class TTLCacheTests(unittest.TestCase):
    def test_roundtrip_and_expiry(self) -> None:
        path = Path(tempfile.mkdtemp()) / "cache.json"
        cache = meta_service.TTLCache(path=path, ttl=1)
        cache.put("key", {"value": 1})
        self.assertEqual(cache.get("key"), {"value": 1})
        time.sleep(1.1)
        self.assertIsNone(cache.get("key"))

    def test_persists_between_instances(self) -> None:
        path = Path(tempfile.mkdtemp()) / "cache.json"
        meta_service.TTLCache(path=path).put("key", [1, 2, 3])
        self.assertEqual(meta_service.TTLCache(path=path).get("key"), [1, 2, 3])

    def test_broken_file_does_not_crash(self) -> None:
        path = Path(tempfile.mkdtemp()) / "cache.json"
        path.write_text("{ not json", encoding="utf-8")
        cache = meta_service.TTLCache(path=path)
        self.assertIsNone(cache.get("key"))
        cache.put("key", "value")
        self.assertEqual(json.loads(path.read_text())["key"]["value"], "value")


if __name__ == "__main__":
    unittest.main()


class TMDBStillsTests(unittest.TestCase):
    def test_v3_key_goes_to_params(self) -> None:
        tmdb = meta_service.TMDBStills("0123456789abcdef0123456789abcdef")
        self.assertFalse(tmdb._is_token)
        self.assertEqual(tmdb._params({"query": "x"})["api_key"], "0123456789abcdef0123456789abcdef")
        self.assertNotIn("Authorization", tmdb.http.headers)

    def test_v4_read_token_goes_to_header(self) -> None:
        token = "eyJhbGciOiJIUzI1NiJ9.eyJhdWQiOiJ0ZXN0In0.signature"
        tmdb = meta_service.TMDBStills(token)
        self.assertTrue(tmdb._is_token)
        self.assertNotIn("api_key", tmdb._params({"query": "x"}))
        self.assertEqual(tmdb.http.headers["Authorization"], f"Bearer {token}")


class TMDBQueryTests(unittest.TestCase):
    def test_query_falls_back_to_film_card(self) -> None:
        service, fake = make_service({})
        # Кэш пуст: название и год берём из карточки Кинопоиска
        title, year = service._tmdb_query(77)
        self.assertEqual(title, "Obsession")
        self.assertEqual(year, 2025)

    def test_query_is_cached(self) -> None:
        service, fake = make_service({})
        service._tmdb_query(77)
        service.cache.put("tmdbq:77", {"title": "Cached", "year": "2011"})
        self.assertEqual(service._tmdb_query(77), ("Cached", 2011))
