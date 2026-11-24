"""Офлайн-тесты слоя метаданных: разбор имён, матчинг и кэш. Сеть не используется."""

from __future__ import annotations

import contextlib
import datetime
import json
import tempfile
import threading
import time
import unittest
from collections.abc import Iterator
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

    def test_entry_carries_the_moment_of_writing(self) -> None:
        cache = meta_service.TTLCache(path=Path(tempfile.mkdtemp()) / "cache.json")
        before = time.time()
        cache.put("key", [1, 2])
        found = cache.entry("key")
        assert found is not None
        self.assertGreaterEqual(found[0], before)
        self.assertEqual(found[1], [1, 2])
        self.assertIsNone(cache.entry("missing"))


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


class ParseReleaseScopeTests(unittest.TestCase):
    """Строки взяты из настоящих имён раздач, уже разобранных выше в файле."""

    def test_single_season(self) -> None:
        scope = meta_service.parse_release_scope(
            "Игра Престолов / Game of Thrones (Сезон 4) [2014, BDRip 1080p]"
        )
        self.assertEqual((scope.season_from, scope.season_to), (4, 4))
        self.assertIsNone(scope.episode_from)
        self.assertIsNone(scope.episodes_declared)

    def test_season_range(self) -> None:
        scope = meta_service.parse_release_scope(
            "Игра престолов / Game of Thrones (Сезон 1-8) / 2011-2019, WEB-DL"
        )
        self.assertEqual((scope.season_from, scope.season_to), (1, 8))

    def test_year_range_is_not_a_season_range(self) -> None:
        scope = meta_service.parse_release_scope("Некий сериал / 2011-2019, WEB-DL 1080p")
        self.assertIsNone(scope.season_from)
        self.assertIsNone(scope.season_to)

    def test_short_season_form(self) -> None:
        scope = meta_service.parse_release_scope("Игра престолов S01 WEB-DL 1080p")
        self.assertEqual((scope.season_from, scope.season_to), (1, 1))

    def test_short_season_range(self) -> None:
        scope = meta_service.parse_release_scope("The Wire S01-S03 1080p BDRip")
        self.assertEqual((scope.season_from, scope.season_to), (1, 3))

    def test_episode_range_with_declared_total(self) -> None:
        scope = meta_service.parse_release_scope(
            "Сорвиголова / Daredevil (Сезон 1, серии 1-8 из 10) [2025, WEB-DL 1080p]"
        )
        self.assertEqual((scope.season_from, scope.season_to), (1, 1))
        self.assertEqual((scope.episode_from, scope.episode_to), (1, 8))
        self.assertEqual(scope.episodes_declared, 10)

    def test_single_episode_out_of_total(self) -> None:
        scope = meta_service.parse_release_scope("Друзья (Сезон 2) 07 из 24 [WEB-DL]")
        self.assertEqual((scope.episode_from, scope.episode_to), (7, 7))
        self.assertEqual(scope.episodes_declared, 24)

    def test_latin_episode_range(self) -> None:
        scope = meta_service.parse_release_scope("Some Show S02E01-E12 1080p WEB-DL")
        self.assertEqual((scope.season_from, scope.season_to), (2, 2))
        self.assertEqual((scope.episode_from, scope.episode_to), (1, 12))
        self.assertIsNone(scope.episodes_declared)

    def test_single_episode_marker(self) -> None:
        scope = meta_service.parse_release_scope("Некий сериал S01E07 WEB-DL 1080p")
        self.assertEqual((scope.episode_from, scope.episode_to), (7, 7))

    def test_movie_has_no_scope(self) -> None:
        scope = meta_service.parse_release_scope("Дюна 2021 WEB-DL 1080p Дублированный")
        self.assertEqual(
            (scope.season_from, scope.season_to, scope.episode_from, scope.episode_to),
            (None, None, None, None),
        )

    def test_resolution_is_not_an_episode_range(self) -> None:
        scope = meta_service.parse_release_scope("Sharp.Obsession.2025.1080p.WEB-DL.x264")
        self.assertIsNone(scope.episode_from)
        self.assertIsNone(scope.episodes_declared)


class VideoSpecTests(unittest.TestCase):
    def test_web_dl_hevc_1080p(self) -> None:
        spec = meta_service.parse_video_spec("Дюна 2021 WEB-DL 1080p H.265 HDR10")
        self.assertEqual(spec.resolution, 1080)
        self.assertEqual(spec.source_type, "web-dl")
        self.assertEqual(spec.video_codec, "h265")
        self.assertEqual(spec.hdr, "hdr10")

    def test_remux_wins_over_bluray(self) -> None:
        spec = meta_service.parse_video_spec("Фильм 2025 BDRemux 2160p Dolby Vision")
        self.assertEqual(spec.source_type, "bdremux")
        self.assertEqual(spec.resolution, 2160)
        self.assertEqual(spec.hdr, "dolby_vision")

    def test_4k_is_2160(self) -> None:
        self.assertEqual(meta_service.parse_video_spec("Фильм 4K UHD BDRip").resolution, 2160)

    def test_frame_size_from_topic_details(self) -> None:
        spec = meta_service.parse_video_spec(
            "Фильм 2025 BDRip", "Видео: MPEG-4 AVC, 1920x1080, 8000 kbps"
        )
        self.assertEqual(spec.resolution, 1080)
        self.assertEqual(spec.video_codec, "h264")

    def test_unknown_stays_none(self) -> None:
        spec = meta_service.parse_video_spec("Просто название без технических слов")
        self.assertIsNone(spec.resolution)
        self.assertIsNone(spec.source_type)
        self.assertIsNone(spec.video_codec)
        self.assertIsNone(spec.hdr)


class VoiceoverSpecTests(unittest.TestCase):
    def test_dub_and_studio(self) -> None:
        spec = meta_service.parse_voiceover_spec("Сериал S01 WEB-DL 1080p Дубляж LostFilm")
        self.assertIn("dub", spec.kinds)
        self.assertEqual(spec.studios, ["LostFilm"])

    def test_track_codes_are_expanded(self) -> None:
        spec = meta_service.parse_voiceover_spec("Фильм 2025 BDRip 1080p | D, P, A")
        self.assertEqual(set(spec.kinds), {"dub", "professional", "author"})

    def test_single_voice_is_marked(self) -> None:
        spec = meta_service.parse_voiceover_spec("Фильм 2025 HDRip одноголосый закадровый")
        self.assertIn("single", spec.kinds)
        self.assertEqual(spec.studios, [])

    def test_translation_field_from_topic(self) -> None:
        spec = meta_service.parse_voiceover_spec(
            "Фильм 2025 BDRip", "Перевод: профессиональный многоголосый, Кубик в Кубе"
        )
        self.assertIn("professional", spec.kinds)
        self.assertIn("multi", spec.kinds)
        self.assertEqual(spec.studios, ["Кубик в Кубе"])

    def test_nothing_found_is_empty(self) -> None:
        spec = meta_service.parse_voiceover_spec("Movie 2025 BDRip 1080p")
        self.assertEqual(spec.kinds, [])
        self.assertEqual(spec.studios, [])


class FakeHttpResponse:
    def __init__(self, payload: Any, status_code: int = 200) -> None:
        self.payload = payload
        self.status_code = status_code

    def json(self) -> Any:
        return self.payload

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class FakeHttp:
    """Подмена сессии на границе HTTP: проверяем и разбор ответа, и лимиты."""

    def __init__(self, routes: dict[str, Any], status: int = 200) -> None:
        self.routes = routes
        self.status = status
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.headers: dict[str, str] = {}

    def get(self, url: str, params: dict[str, Any] | None = None, timeout: int = 0) -> FakeHttpResponse:
        self.calls.append((url, dict(params or {})))
        key = next((route for route in self.routes if route in url), "")
        return FakeHttpResponse(self.routes.get(key, {}), self.status)


# Формы ответов — из документации Kinopoisk API Unofficial (openapi.json).
# Ловушки оттуда же: year строкой в подборках и числом в фильтре, опечатка
# ratingImbd в подборках, filmId вместо kinopoiskId в поиске по слову.
COLLECTION_PAYLOAD = {
    "total": 2,
    "totalPages": 1,
    "items": [
        {
            "kinopoiskId": 301,
            "nameRu": "Матрица",
            "nameEn": None,
            "nameOriginal": "The Matrix",
            "countries": [{"country": "США"}],
            "genres": [{"genre": "фантастика"}],
            "ratingKinopoisk": 8.5,
            "ratingImbd": 8.7,
            "year": "1999",
            "type": "FILM",
            "posterUrl": "http://example.org/poster.jpg",
            "posterUrlPreview": "http://example.org/preview.jpg",
        }
    ],
}

FILTER_PAYLOAD = {
    "total": 1,
    "totalPages": 1,
    "items": [
        {
            "kinopoiskId": 555,
            "imdbId": "tt0111161",
            "nameRu": "Побег из Шоушенка",
            "nameOriginal": "The Shawshank Redemption",
            "countries": [{"country": "США"}],
            "genres": [{"genre": "драма"}],
            "ratingKinopoisk": 9.1,
            "ratingImdb": 9.3,
            "year": 1994,
            "type": "FILM",
            "posterUrl": "https://example.org/p.jpg",
            "posterUrlPreview": "https://example.org/pp.jpg",
        }
    ],
}

PREMIERES_PAYLOAD = {
    "total": 1,
    "items": [
        {
            "kinopoiskId": 777,
            "nameRu": "Новинка",
            "nameEn": "Newcomer",
            "year": 2026,
            "posterUrl": "https://example.org/n.jpg",
            "posterUrlPreview": "https://example.org/np.jpg",
            "countries": [{"country": "Россия"}],
            "genres": [{"genre": "комедия"}],
            "duration": 101,
            "premiereRu": "2026-10-01",
        }
    ],
}

FILTERS_PAYLOAD = {
    "genres": [{"id": 11, "genre": "боевик"}, {"id": 8, "genre": "драма"}],
    "countries": [{"id": 1, "country": "США"}],
}

SERIES_FILM_PAYLOAD = {
    "kinopoiskId": 4444,
    "imdbId": "tt0944947",
    "nameRu": "Игра престолов",
    "nameOriginal": "Game of Thrones",
    "year": 2011,
    "type": "TV_SERIES",
    "serial": True,
    "ratingKinopoisk": 9.0,
    "description": "Описание",
    "posterUrl": "http://example.org/got.jpg",
    "coverUrl": "https://example.org/got-cover.jpg",
    "genres": [{"genre": "фэнтези"}],
    "countries": [{"country": "США"}],
    "filmLength": 55,
}

SEASONS_PAYLOAD = {
    "total": 2,
    "items": [
        {
            "number": 1,
            "episodes": [
                {"seasonNumber": 1, "episodeNumber": 1, "nameRu": "Зима близко", "releaseDate": "2011-04-17"},
                {"seasonNumber": 1, "episodeNumber": 2, "nameRu": "Дорога короля"},
            ],
        },
        {"number": 2, "episodes": [{"seasonNumber": 2, "episodeNumber": 1, "nameRu": "Север помнит"}]},
    ],
}


class FakeRedirect:
    """Ответ редиректа TMDB: он же единственное, что читает resolve_tmdb_library_id."""

    def __init__(self, location: str, status_code: int = 307) -> None:
        self.status_code = status_code
        self.headers = {"Location": location}


@contextlib.contextmanager
def tmdb_redirect(location: str | None, status_code: int = 307) -> Iterator[list[dict[str, Any]]]:
    """Подменяет сетевой вызов TMDB: тесты сеть не трогают."""
    calls: list[dict[str, Any]] = []

    def fake_head(url: str, **kwargs: Any) -> FakeRedirect:
        calls.append(dict(kwargs.get("params") or {}))
        if location is None:
            raise meta_service.requests.RequestException("TMDB недоступен")
        return FakeRedirect(location, status_code)

    saved = meta_service.requests.head
    meta_service.requests.head = fake_head  # type: ignore[assignment]
    try:
        yield calls
    finally:
        meta_service.requests.head = saved  # type: ignore[assignment]


def catalog_service(routes: dict[str, Any], status: int = 200) -> tuple[meta_service.MetaService, FakeHttp]:
    kinopoisk = meta_service.KinopoiskMeta("test-key")
    http = FakeHttp(routes, status)
    kinopoisk.http = http  # type: ignore[assignment]
    folder = Path(tempfile.mkdtemp())
    service = meta_service.MetaService(
        kinopoisk=kinopoisk,
        tmdb=None,
        cache=meta_service.TTLCache(folder / "meta.json"),
        shelf_cache=meta_service.TTLCache(folder / "shelf.json", 3600),
    )
    return service, http


class CatalogShelfTests(unittest.TestCase):
    def test_collection_shelf_maps_cards(self) -> None:
        service, http = catalog_service({"films/collections": COLLECTION_PAYLOAD})
        [title] = service.shelf("top_series")
        self.assertEqual(title.catalog_id, 301)
        self.assertEqual(title.name, "Матрица")
        self.assertEqual(title.original_name, "The Matrix")
        self.assertEqual(title.year, "1999")
        self.assertEqual(title.rating, "8.5")
        self.assertEqual(title.genres, ("фантастика",))
        # http:// в постерах Кинопоиска переписывается на https
        self.assertTrue(title.poster_url.startswith("https://"))
        self.assertEqual(http.calls[0][1], {"type": "TOP_250_TV_SHOWS", "page": 1})

    def test_each_shelf_asks_its_own_collection(self) -> None:
        service, http = catalog_service({"films/collections": COLLECTION_PAYLOAD})
        for kind, expected in (
            ("popular", "TOP_POPULAR_ALL"),
            ("top_movies", "TOP_250_MOVIES"),
            ("popular_series", "POPULAR_SERIES"),
            ("closest_releases", "CLOSES_RELEASES"),
        ):
            service.shelf(kind)
            self.assertEqual(http.calls[-1][1]["type"], expected)

    def test_genre_shelf_resolves_numeric_id_first(self) -> None:
        service, http = catalog_service(
            {"films/filters": FILTERS_PAYLOAD, "api/v2.2/films": FILTER_PAYLOAD}
        )
        [title] = service.shelf("genre/Боевик")
        self.assertEqual(title.catalog_id, 555)
        self.assertEqual(title.year, "1994")
        # Фильтр принимает только числовой id жанра, названия он не знает
        self.assertEqual(http.calls[-1][1], {"genres": 11, "order": "RATING", "page": 1})

    def test_unknown_genre_is_reported(self) -> None:
        service, _ = catalog_service({"films/filters": FILTERS_PAYLOAD})
        with self.assertRaises(ValueError):
            service.shelf("genre/киберпанк")

    def test_year_shelf_uses_both_bounds(self) -> None:
        service, http = catalog_service({"api/v2.2/films": FILTER_PAYLOAD})
        service.shelf("year/1994")
        self.assertEqual(http.calls[-1][1], {"yearFrom": 1994, "yearTo": 1994, "order": "RATING", "page": 1})

    def test_premieres_shelf_asks_for_a_month(self) -> None:
        service, http = catalog_service({"films/premieres": PREMIERES_PAYLOAD})
        [title] = service.shelf("premieres")
        self.assertEqual(title.catalog_id, 777)
        self.assertEqual(title.rating, "")
        self.assertIn(http.calls[-1][1]["month"], meta_service.MONTH_NAMES)

    def test_unknown_shelf_is_rejected(self) -> None:
        service, _ = catalog_service({})
        with self.assertRaises(ValueError):
            service.shelf("что-нибудь")

    def test_shelf_is_cached(self) -> None:
        service, http = catalog_service({"films/collections": COLLECTION_PAYLOAD})
        service.shelf("popular")
        service.shelf("popular")
        self.assertEqual(len(http.calls), 1)

    def test_page_is_clamped_to_what_the_api_serves(self) -> None:
        service, http = catalog_service({"films/collections": COLLECTION_PAYLOAD})
        service.shelf("popular", page=9999)
        self.assertEqual(http.calls[-1][1]["page"], meta_service.MAX_COLLECTION_PAGE)

    def test_daily_quota_is_its_own_error(self) -> None:
        service, _ = catalog_service({"films/collections": COLLECTION_PAYLOAD}, status=402)
        with self.assertRaises(meta_service.CatalogLimitError):
            service.shelf("popular")

    def test_rate_limit_is_its_own_error(self) -> None:
        service, _ = catalog_service({"films/collections": COLLECTION_PAYLOAD}, status=429)
        with self.assertRaises(meta_service.CatalogLimitError):
            service.shelf("popular")


class TitleCardTests(unittest.TestCase):
    def routes(self) -> dict[str, Any]:
        return {
            "films/4444/seasons": SEASONS_PAYLOAD,
            "api/v2.2/films/4444": SERIES_FILM_PAYLOAD,
            "v1/staff": [],
        }

    def test_card_carries_seasons_and_external_ids(self) -> None:
        service, _ = catalog_service(self.routes())
        with tmdb_redirect("https://www.themoviedb.org/tv/1399"):
            card = service.title_card(4444)
        self.assertTrue(card.meta.matched)
        self.assertEqual(card.meta.title, "Игра престолов")
        self.assertEqual(card.meta.original_title, "Game of Thrones")
        self.assertEqual(card.imdb_id, "tt0944947")
        self.assertEqual([(item.season, item.episodes) for item in card.seasons], [(1, 2), (2, 1)])
        # tmdb_id приходит внутри meta и добывается по IMDb, а не поиском
        self.assertEqual(card.meta.tmdb_id, 1399)
        self.assertEqual(card.meta.tmdb_media_type, "tv")

    def test_card_is_cached_between_calls(self) -> None:
        service, http = catalog_service(self.routes())
        with tmdb_redirect("https://www.themoviedb.org/tv/1399"):
            service.title_card(4444)
            calls = len(http.calls)
            service.title_card(4444)
        self.assertEqual(len(http.calls), calls)

    def test_card_costs_three_catalog_requests(self) -> None:
        service, http = catalog_service(self.routes())
        with tmdb_redirect("https://www.themoviedb.org/tv/1399"):
            service.title_card(4444)
        # Карточка, состав и сезоны — и ни одного лишнего запроса из 500 в сутки
        self.assertEqual(len(http.calls), 3)

    def test_unknown_title_is_not_matched(self) -> None:
        service, _ = catalog_service({})
        self.assertFalse(service.title_card(999).meta.matched)


DAY = 24 * 3600
TODAY = datetime.date(2026, 9, 23)


def season(number: int, *dates: str | None) -> dict[str, Any]:
    """Сезон в форме ответа v2.2/films/{id}/seasons: по серии на каждую дату."""
    return {
        "number": number,
        "episodes": [
            {"seasonNumber": number, "episodeNumber": index, "nameEn": f"Episode #{number}.{index}", "releaseDate": date}
            for index, date in enumerate(dates, start=1)
        ],
    }


# 4 сезон «Теда Лассо», каким его отдавал Кинопоиск живьём 23.09.2026
TED_LASSO_PLACEHOLDER = [season(3, "2023-03-15", "2023-05-31"), season(4, *["2026-01-01"] * 10)]
FINISHED = [season(1, "2020-08-14", "2020-10-02"), season(3, "2023-03-15", "2023-05-31")]


class SeasonIsAiringTests(unittest.TestCase):
    """Какой состав сезонов перечитывать раз в сутки, а какой держать 30 дней.

    Случаи — из кэша боевого сервера на 23.09.2026: правило пометило 3
    сериала из 9, ровно эти три.
    """

    def airing(self, *seasons: dict[str, Any]) -> bool:
        return meta_service.season_is_airing(list(seasons), today=TODAY)

    def test_finished_season_is_not_airing(self) -> None:
        self.assertFalse(meta_service.season_is_airing(FINISHED, today=TODAY))

    def test_new_year_placeholder_is_airing(self) -> None:
        self.assertTrue(meta_service.season_is_airing(TED_LASSO_PLACEHOLDER, today=TODAY))

    def test_single_real_new_year_episode_is_not_a_placeholder(self) -> None:
        # Новогодний выпуск бывает настоящим: 4 сезон «Шерлока» начался 1 января
        self.assertFalse(self.airing(season(4, "2017-01-01", "2017-01-08", "2017-01-15")))

    def test_episode_without_a_date_is_airing(self) -> None:
        # «Джентльмены», 3 сезон: у первой серии дата есть, у остальных нет
        self.assertTrue(self.airing(season(3, "2026-09-03", None, None)))

    def test_future_episode_is_airing(self) -> None:
        self.assertTrue(self.airing(season(1, "2024-01-10"), season(2, "2027-03-01")))

    def test_recent_episode_keeps_the_season_airing_for_four_weeks(self) -> None:
        # «Холод»: последняя серия вышла 17.09 — сезон ещё может прирасти
        self.assertTrue(self.airing(season(1, "2026-07-16", "2026-09-17")))
        self.assertFalse(self.airing(season(1, "2026-07-16", "2026-08-19")))

    def test_only_the_last_season_by_number_counts(self) -> None:
        # Пробелы в датах старого сезона не делают сериал идущим, а последний
        # сезон ищется по номеру, а не по месту в списке
        self.assertFalse(self.airing(season(2, "2019-05-01"), season(1, None, None)))

    def test_announced_season_without_episodes_is_airing(self) -> None:
        self.assertTrue(self.airing(season(1, "2024-01-10"), {"number": 2, "episodes": []}))

    def test_film_is_not_airing(self) -> None:
        # У фильма Кинопоиск отдаёт пустой список сезонов
        self.assertFalse(meta_service.season_is_airing([], today=TODAY))
        self.assertFalse(meta_service.season_is_airing(None, today=TODAY))


def seasons_service(
    payload: Any, cached: list[dict[str, Any]] | None = None, age: float = 0, status: int = 200
) -> tuple[meta_service.MetaService, FakeHttp]:
    """Сервис с записью `seasons:4444` заданного возраста — как её оставила прежняя версия."""
    kinopoisk = meta_service.KinopoiskMeta("test-key")
    http = FakeHttp({"films/4444/seasons": payload}, status)
    kinopoisk.http = http  # type: ignore[assignment]
    folder = Path(tempfile.mkdtemp())
    if cached is not None:
        entry = {"stamp": time.time() - age, "value": cached}
        (folder / "meta.json").write_text(json.dumps({"seasons:4444": entry}), encoding="utf-8")
    service = meta_service.MetaService(
        kinopoisk=kinopoisk,
        tmdb=None,
        cache=meta_service.TTLCache(folder / "meta.json"),
        shelf_cache=meta_service.TTLCache(folder / "shelf.json", 3600),
    )
    return service, http


class SlowSeasons:
    """Каталог, который отвечает на сезоны не сразу, как живой."""

    def __init__(self, items: list[dict[str, Any]]) -> None:
        self.items = items
        self.calls = 0
        self._lock = threading.Lock()

    def seasons(self, film_id: int) -> list[dict[str, Any]]:
        with self._lock:
            self.calls += 1
        time.sleep(0.2)
        return self.items


class SeasonsRefreshTests(unittest.TestCase):
    """Срок записи `seasons:` решается при чтении, по её содержимому.

    Прежде она лежала в общем кэше 30 дней. Живьём 23.09.2026 запись «Теда
    Лассо» была суточной давности и совпадала с ответом Кинопоиска, то есть
    заглушки отдаёт он сам — но исправление тот же срок заморозил бы на месяц.
    """

    def test_airing_season_is_reread_after_a_day(self) -> None:
        fresh = [season(4, "2026-08-04", "2026-08-11")]
        service, http = seasons_service({"items": fresh}, cached=TED_LASSO_PLACEHOLDER, age=2 * DAY)
        episodes = service.episodes(4444, 4)
        self.assertEqual(len(http.calls), 1)
        self.assertEqual([item.air_date for item in episodes], ["2026-08-04", "2026-08-11"])

    def test_airing_season_is_not_reread_within_a_day(self) -> None:
        service, http = seasons_service({"items": []}, cached=TED_LASSO_PLACEHOLDER, age=3600)
        self.assertEqual(len(service.episodes(4444, 4)), 10)
        self.assertEqual(http.calls, [])

    def test_finished_season_keeps_its_thirty_days(self) -> None:
        service, http = seasons_service({"items": []}, cached=FINISHED, age=20 * DAY)
        self.assertEqual(len(service.episodes(4444, 3)), 2)
        self.assertEqual(http.calls, [])

    def test_failed_reread_keeps_the_previous_answer(self) -> None:
        # Квота кончилась посреди дня: вчерашний состав лучше карточки без сезонов
        service, http = seasons_service({}, cached=TED_LASSO_PLACEHOLDER, age=2 * DAY, status=402)
        self.assertEqual(len(service.episodes(4444, 4)), 10)
        self.assertEqual(len(http.calls), 1)

    def test_parallel_reads_cost_one_request(self) -> None:
        # KinoApp спрашивает карточку и серии тайтла в одну секунду, и обе
        # ручки читают один и тот же состав сезонов
        folder = Path(tempfile.mkdtemp())
        entry = {"stamp": time.time() - 2 * DAY, "value": TED_LASSO_PLACEHOLDER}
        (folder / "meta.json").write_text(json.dumps({"seasons:4444": entry}), encoding="utf-8")
        catalog = SlowSeasons([season(4, "2026-08-04")])
        service = meta_service.MetaService(
            kinopoisk=catalog,  # type: ignore[arg-type]
            tmdb=None,
            cache=meta_service.TTLCache(folder / "meta.json"),
            shelf_cache=meta_service.TTLCache(folder / "shelf.json", 3600),
        )
        start = threading.Barrier(2)
        answers: list[int] = []

        def read() -> None:
            start.wait()
            answers.append(len(service.episodes(4444, 4)))

        threads = [threading.Thread(target=read) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(catalog.calls, 1)
        self.assertEqual(answers, [1, 1])


class GenreDirectoryTests(unittest.TestCase):
    def test_genres_are_cached(self) -> None:
        service, http = catalog_service({"films/filters": FILTERS_PAYLOAD})
        self.assertEqual(service.genre_id("драма"), 8)
        self.assertEqual(service.genre_id("ДРАМА"), 8)
        self.assertEqual(len(http.calls), 1)

    def test_unknown_genre_has_no_id(self) -> None:
        service, _ = catalog_service({"films/filters": FILTERS_PAYLOAD})
        self.assertIsNone(service.genre_id("киберпанк"))

    def test_genres_and_countries_share_one_request(self) -> None:
        service, http = catalog_service({"films/filters": FILTERS_PAYLOAD})
        self.assertEqual(service.genre_id("драма"), 8)
        self.assertEqual(service.country_id("США"), 1)
        # Оба справочника приходят одним ответом: второй из кэша, без запроса
        self.assertEqual(len(http.calls), 1)

    def test_unknown_country_has_no_id(self) -> None:
        service, _ = catalog_service({"films/filters": FILTERS_PAYLOAD})
        self.assertIsNone(service.country_id("Атлантида"))


# Формы и значения — из живых ответов Кинопоиска 23.09.2026 (запросы с VPS):
# у «похожих» в элементе нет ни страны, ни оценки, а у советского «Соляриса»
# нет и оригинального названия.
SIMILARS_PAYLOAD = {
    "total": 3,
    "items": [
        {
            "filmId": 1236063,
            "nameRu": "Довод",
            "nameEn": "Tenet",
            "nameOriginal": "Tenet",
            "posterUrl": "https://kinopoiskapiunofficial.tech/images/posters/kp/1236063.jpg",
            "posterUrlPreview": "https://kinopoiskapiunofficial.tech/images/posters/kp_small/1236063.jpg",
            "relationType": "SIMILAR",
        },
        {
            "filmId": 43911,
            "nameRu": "Солярис",
            "nameEn": None,
            "nameOriginal": None,
            "posterUrl": "https://kinopoiskapiunofficial.tech/images/posters/kp/43911.jpg",
            "posterUrlPreview": "https://kinopoiskapiunofficial.tech/images/posters/kp_small/43911.jpg",
            "relationType": "SIMILAR",
        },
        {
            "filmId": 718811,
            "nameRu": "Прибытие",
            "nameEn": "Arrival",
            "nameOriginal": "Arrival",
            "posterUrl": "https://kinopoiskapiunofficial.tech/images/posters/kp/718811.jpg",
            "posterUrlPreview": "https://kinopoiskapiunofficial.tech/images/posters/kp_small/718811.jpg",
            "relationType": "SIMILAR",
        },
    ],
}

TENET_FILM_PAYLOAD = {
    "kinopoiskId": 1236063,
    "imdbId": "tt6723592",
    "nameRu": "Довод",
    "nameEn": None,
    "nameOriginal": "Tenet",
    "posterUrl": "https://kinopoiskapiunofficial.tech/images/posters/kp/1236063.jpg",
    "posterUrlPreview": "https://kinopoiskapiunofficial.tech/images/posters/kp_small/1236063.jpg",
    "coverUrl": "https://avatars.mds.yandex.net/get-ott/1531675/2a0000017c2d2fe7c91f8b446084b299d7a2/orig",
    "logoUrl": "https://avatars.mds.yandex.net/get-ott/239697/2a000001806006ba05ae72483e16dd65706b/orig",
    "ratingKinopoisk": 7.5,
    "ratingKinopoiskVoteCount": 592560,
    "ratingImdb": 7.3,
    "year": 2020,
    "filmLength": 150,
    "shortDescription": "«Не пытайся понять — почувствуй» Виртуозный фантастический боевик Кристофера Нолана",
    "description": "После теракта в киевском оперном театре агент ЦРУ объединяется с британской разведкой.",
    "type": "FILM",
    "countries": [{"country": "США"}, {"country": "Великобритания"}],
    "genres": [{"genre": "фантастика"}, {"genre": "боевик"}, {"genre": "триллер"}],
}

SOLARIS_FILM_PAYLOAD = {
    "kinopoiskId": 43911,
    "nameRu": "Солярис",
    "nameEn": None,
    "nameOriginal": None,
    "posterUrl": "https://kinopoiskapiunofficial.tech/images/posters/kp/43911.jpg",
    "posterUrlPreview": "https://kinopoiskapiunofficial.tech/images/posters/kp_small/43911.jpg",
    "coverUrl": None,
    "logoUrl": None,
    "ratingKinopoisk": 7.8,
    "ratingImdb": 8.0,
    "year": 1972,
    "filmLength": 169,
    "shortDescription": None,
    "type": "FILM",
    "countries": [{"country": "СССР"}],
    "genres": [{"genre": "драма"}, {"genre": "фантастика"}],
}


def similar_routes() -> dict[str, Any]:
    # Порядок ключей важен: FakeHttp берёт первый ключ, входящий в адрес,
    # а адрес «похожих» содержит в себе адрес карточки фильма
    return {
        "films/258687/similars": SIMILARS_PAYLOAD,
        "api/v2.2/films/1236063": TENET_FILM_PAYLOAD,
        "api/v2.2/films/43911": SOLARIS_FILM_PAYLOAD,
    }


class DiscoverTests(unittest.TestCase):
    def test_discover_goes_by_votes_with_every_filter(self) -> None:
        service, http = catalog_service({"films/filters": FILTERS_PAYLOAD, "api/v2.2/films": FILTER_PAYLOAD})
        service.discover(
            media_type="tv_series", genre="Драма", country="США",
            year_from=2024, year_to=2026, rating_from=7.5, page=2,
        )
        self.assertEqual(
            http.calls[-1][1],
            {
                "order": "NUM_VOTE", "page": 2, "type": "TV_SERIES", "genres": 8, "countries": 1,
                "yearFrom": 2024, "yearTo": 2026, "ratingFrom": 7.5,
            },
        )

    def test_default_is_every_type_by_votes(self) -> None:
        service, http = catalog_service({"api/v2.2/films": FILTER_PAYLOAD})
        service.discover()
        # Ни типа, ни порога: фильтр получает ровно то, что задано
        self.assertEqual(http.calls[-1][1], {"order": "NUM_VOTE", "page": 1})

    def test_card_carries_imdb_rating_as_a_string(self) -> None:
        service, _ = catalog_service({"api/v2.2/films": FILTER_PAYLOAD})
        [card] = service.discover(media_type="FILM")
        self.assertEqual(card["catalog_id"], 555)
        self.assertEqual(card["year"], "1994")
        self.assertEqual(card["rating"], "9.1")
        self.assertEqual(card["rating_imdb"], "9.3")
        self.assertEqual(card["countries"], ["США"])
        self.assertFalse(card["detailed"])
        self.assertEqual(card["tagline"], "")

    def test_unknown_values_are_reported(self) -> None:
        service, _ = catalog_service({"films/filters": FILTERS_PAYLOAD})
        for kwargs in ({"country": "Атлантида"}, {"genre": "киберпанк"}, {"media_type": "CARTOON"}, {"order": "RANDOM"}):
            with self.subTest(**kwargs), self.assertRaises(ValueError):
                service.discover(**kwargs)

    def test_discover_is_cached_by_its_parameters(self) -> None:
        service, http = catalog_service({"api/v2.2/films": FILTER_PAYLOAD})
        service.discover(media_type="FILM", year_from=2025)
        service.discover(media_type="FILM", year_from=2025)
        self.assertEqual(len(http.calls), 1)
        service.discover(media_type="FILM", year_from=2024)
        self.assertEqual(len(http.calls), 2)

    def test_page_is_clamped(self) -> None:
        service, http = catalog_service({"api/v2.2/films": FILTER_PAYLOAD})
        service.discover(page=999)
        self.assertEqual(http.calls[-1][1]["page"], meta_service.MAX_FILTER_PAGE)


class SimilarTests(unittest.TestCase):
    def test_each_similar_gets_country_and_cover_from_its_film_card(self) -> None:
        service, _ = catalog_service(similar_routes())
        tenet, solaris, arrival = service.similar(258687)
        self.assertEqual(tenet["countries"], ["США", "Великобритания"])
        self.assertEqual(tenet["rating_imdb"], "7.3")
        self.assertEqual(tenet["votes"], 592560)
        self.assertEqual(tenet["duration_minutes"], 150)
        self.assertTrue(tenet["backdrop_url"].startswith("https://avatars.mds.yandex.net/"))
        self.assertTrue(tenet["tagline"].startswith("«Не пытайся понять"))
        self.assertTrue(tenet["detailed"])
        self.assertEqual(solaris["countries"], ["СССР"])
        # Пустые поля карточки фильма не затирают элемент «похожих»
        self.assertEqual(solaris["name"], "Солярис")
        self.assertEqual(solaris["backdrop_url"], "")
        # Карточка «Прибытия» не пришла: похожий остаётся, но без страны
        self.assertFalse(arrival["detailed"])
        self.assertEqual(arrival["countries"], [])
        self.assertEqual(arrival["name"], "Прибытие")

    def test_limit_keeps_the_head_of_the_list(self) -> None:
        service, http = catalog_service(similar_routes())
        cards = service.similar(258687, limit=1)
        self.assertEqual([card["catalog_id"] for card in cards], [1236063])
        # Список похожих и одна карточка — и больше ни одного запроса
        self.assertEqual(len(http.calls), 2)

    def test_similars_and_film_cards_are_cached(self) -> None:
        service, http = catalog_service(similar_routes())
        service.similar(258687, limit=2)
        calls = len(http.calls)
        service.similar(258687, limit=2)
        self.assertEqual(len(http.calls), calls)

    def test_quota_with_nothing_in_cache_is_an_error(self) -> None:
        service, _ = catalog_service(similar_routes(), status=402)
        with self.assertRaises(meta_service.CatalogLimitError):
            service.similar(258687)


class BriefsTests(unittest.TestCase):
    def test_briefs_keep_request_order_and_skip_the_missing(self) -> None:
        service, _ = catalog_service(similar_routes())
        cards = service.briefs([43911, 999, 1236063, 43911])
        self.assertEqual([card["catalog_id"] for card in cards], [43911, 1236063])
        self.assertTrue(all(card["detailed"] for card in cards))

    def test_briefs_have_a_ceiling(self) -> None:
        service, http = catalog_service({})
        service.briefs(list(range(1, 100)))
        self.assertEqual(len(http.calls), meta_service.MAX_BRIEF_IDS)

    def test_brief_from_cache_costs_nothing(self) -> None:
        service, http = catalog_service(similar_routes())
        service.briefs([1236063])
        service.briefs([1236063])
        self.assertEqual(len(http.calls), 1)


class CatalogCardTests(unittest.TestCase):
    def test_collection_item_with_the_imdb_typo(self) -> None:
        card = meta_service.catalog_card(COLLECTION_PAYLOAD["items"][0])
        self.assertEqual(card["rating_imdb"], "8.7")
        self.assertEqual(card["original_name"], "The Matrix")
        self.assertTrue(card["poster_url"].startswith("https://"))

    def test_same_first_fields_as_a_shelf_card(self) -> None:
        card = meta_service.catalog_card(FILTER_PAYLOAD["items"][0])
        # Форма `/catalog/popular` — приложение разбирает обе одним типом
        for key in ("catalog_id", "name", "original_name", "media_type", "year", "rating",
                    "countries", "genres", "poster_url", "search_query"):
            self.assertIn(key, card)
        self.assertEqual(card["search_query"], "Побег из Шоушенка 1994")

    def test_missing_rating_is_an_empty_string(self) -> None:
        card = meta_service.catalog_card({"kinopoiskId": 1, "nameRu": "Премьера", "ratingKinopoisk": None})
        self.assertEqual(card["rating"], "")
        self.assertEqual(card["rating_imdb"], "")
        self.assertEqual(card["votes"], 0)
        self.assertEqual(card["duration_minutes"], 0)


class RealTrackerNamesTests(unittest.TestCase):
    """Имена сняты живьём с NNM-Club и Prowlarr 21.09.2026. Это публичные
    строки трекера, и они здесь ровно затем, чтобы разбор проверялся не на
    придуманных примерах."""

    def test_season_range_glued_to_episode_range(self) -> None:
        # «S1-8E1-73 of 73» — правая граница сезонов упирается в маркер серий.
        # Живьём такой релиз разбирался как «сезон 1» с 73 сериями в нём.
        scope = meta_service.parse_release_scope(
            "Игра престолов / Game of Thrones, S1-8E1-73 of 73 (2011-2019) BDRip 720p, WEB-DL"
        )
        self.assertEqual((scope.season_from, scope.season_to), (1, 8))
        self.assertEqual((scope.episode_from, scope.episode_to), (1, 73))
        self.assertEqual(scope.episodes_declared, 73)

    def test_single_season_with_episode_range(self) -> None:
        scope = meta_service.parse_release_scope(
            "Игра престолов / Game of Thrones, S8E1-6 of 6 (2019) WEB-DL 1080p-Amedia"
        )
        self.assertEqual((scope.season_from, scope.season_to), (8, 8))
        self.assertEqual((scope.episode_from, scope.episode_to), (1, 6))
        self.assertEqual(scope.episodes_declared, 6)

    def test_bracketed_season_range(self) -> None:
        scope = meta_service.parse_release_scope(
            "Игра престолов / Game of Thrones [S01-08] (2011-2019) BDRip-HEVC 1080p от RIPS CLUB"
        )
        self.assertEqual((scope.season_from, scope.season_to), (1, 8))

    def test_russian_season_and_episode_words(self) -> None:
        scope = meta_service.parse_release_scope(
            "Игра престолов / Game of Thrones (2011-2019) BDRemux [H.264/1080p] "
            "(сезон 1-8, серии 1-73 из 73)"
        )
        self.assertEqual((scope.season_from, scope.season_to), (1, 8))
        self.assertEqual((scope.episode_from, scope.episode_to), (1, 73))
        self.assertEqual(scope.episodes_declared, 73)

    def test_spin_off_movie_has_no_season(self) -> None:
        scope = meta_service.parse_release_scope(
            "Игра престолов: Последний дозор / Game of Thrones: The Last Watch (2019) WEB-DL 1080p"
        )
        self.assertIsNone(scope.season_from)
        self.assertIsNone(scope.episode_from)

    def test_real_movie_quality_lines(self) -> None:
        cases = [
            ("Интерстеллар / Interstellar (2014) UHD BDRemux [H.265/2160p] [4K, HDR, 10-bit] [IMAX Edition]",
             2160, "bdremux", "h265", "hdr"),
            ("Интерстеллар / Interstellar (2014) BDRip [H.265/2160p] [4K, HDR10, 10-bit] [IMAX Edition]",
             2160, "bdrip", "h265", "hdr10"),
            ("Интерстеллар / Interstellar (2014) BDRip 720p | 60 fps-IMAX", 720, "bdrip", None, None),
            ("Интерстеллар / Interstellar (2014) BDRip-AVC | 60 fps", None, "bdrip", "h264", None),
        ]
        for title, resolution, source, codec, hdr in cases:
            with self.subTest(title=title[:40]):
                spec = meta_service.parse_video_spec(title)
                self.assertEqual(spec.resolution, resolution)
                self.assertEqual(spec.source_type, source)
                self.assertEqual(spec.video_codec, codec)
                self.assertEqual(spec.hdr, hdr)

    def test_dolby_vision_and_track_codes_together(self) -> None:
        title = (
            "Интерстеллар / Interstellar (2014) UHD BDRemux 2160p | 4K | HDR | IMAX Edition | "
            "Dolby Vision Profile 8 | D, P, A-Лицензия"
        )
        self.assertEqual(meta_service.parse_video_spec(title).hdr, "dolby_vision")
        kinds = meta_service.parse_voiceover_spec(title).kinds
        self.assertEqual(set(kinds), {"licensed", "dub", "professional", "author"})

    def test_studios_from_a_real_release_name(self) -> None:
        spec = meta_service.parse_voiceover_spec(
            "Игра престолов / Game of Thrones (2011-2019) UHD BDRemux [H.265/2160p] "
            "[4K, HDR10, 10-bit] | LostFilm, Amedia"
        )
        self.assertEqual(spec.studios, ["LostFilm", "Amedia"])


class ResolveTmdbLibraryIdTests(unittest.TestCase):
    """Точный tmdb_id по IMDb через редирект TMDB. Ключ API здесь не нужен —
    в отличие от кадров серий, которые по-прежнему требуют TMDB_API_KEY."""

    def test_series_redirect_is_accepted(self) -> None:
        with tmdb_redirect("https://www.themoviedb.org/tv/1399") as calls:
            self.assertEqual(meta_service.resolve_tmdb_library_id("tt0944947", "tv"), (1399, "tv"))
        self.assertEqual(calls[0], {"external_source": "imdb_id", "external_id": "tt0944947"})

    def test_movie_redirect_is_accepted(self) -> None:
        with tmdb_redirect("https://www.themoviedb.org/movie/157336"):
            self.assertEqual(meta_service.resolve_tmdb_library_id("tt0816692", "movie"), (157336, "movie"))

    def test_wrong_type_is_rejected(self) -> None:
        # Ждали сериал, редирект привёл на фильм — это не наш тайтл
        with tmdb_redirect("https://www.themoviedb.org/movie/157336"):
            self.assertEqual(meta_service.resolve_tmdb_library_id("tt0816692", "tv"), (None, None))

    def test_foreign_host_is_rejected(self) -> None:
        with tmdb_redirect("https://evil.example.org/tv/1399"):
            self.assertEqual(meta_service.resolve_tmdb_library_id("tt0944947", "tv"), (None, None))

    def test_malformed_imdb_id_never_reaches_the_network(self) -> None:
        with tmdb_redirect("https://www.themoviedb.org/tv/1399") as calls:
            self.assertEqual(meta_service.resolve_tmdb_library_id("не-imdb", "tv"), (None, None))
        self.assertEqual(calls, [], "кривой идентификатор не должен уходить в сеть")

    def test_missing_redirect_is_not_a_match(self) -> None:
        with tmdb_redirect("https://www.themoviedb.org/tv/1399", status_code=200):
            self.assertEqual(meta_service.resolve_tmdb_library_id("tt0944947", "tv"), (None, None))

    def test_network_failure_is_survivable(self) -> None:
        with tmdb_redirect(None):
            self.assertEqual(meta_service.resolve_tmdb_library_id("tt0944947", "tv"), (None, None))


class FakeStills:
    """Подмена TMDB: считает, звали ли нечёткий поиск по названию."""

    def __init__(self) -> None:
        self.searches: list[str] = []
        self.stills_for: list[tuple[int, int]] = []

    def find_series(self, query: str, year: int | None) -> int | None:
        self.searches.append(query)
        return 999

    def stills(self, tv_id: int, season: int) -> dict[int, str]:
        self.stills_for.append((tv_id, season))
        return {1: "https://image.tmdb.org/t/p/w780/still.jpg"}


class StillsUseExactIdTests(unittest.TestCase):
    def service_with_stills(self) -> tuple[meta_service.MetaService, FakeStills, FakeHttp]:
        service, http = catalog_service(
            {"films/4444/seasons": SEASONS_PAYLOAD, "api/v2.2/films/4444": SERIES_FILM_PAYLOAD, "v1/staff": []}
        )
        stills = FakeStills()
        service.tmdb = stills  # type: ignore[assignment]
        return service, stills, http

    def test_exact_id_is_preferred_over_title_search(self) -> None:
        service, stills, _ = self.service_with_stills()
        with tmdb_redirect("https://www.themoviedb.org/tv/1399"):
            episodes = service.episodes(4444, season=1)
        self.assertEqual(stills.searches, [], "поиск по названию звать не должны")
        self.assertEqual(stills.stills_for, [(1399, 1)])
        self.assertEqual(episodes[0].still_url, "https://image.tmdb.org/t/p/w780/still.jpg")

    def test_title_search_is_the_fallback(self) -> None:
        service, stills, _ = self.service_with_stills()
        # Редирект не нашёл тайтл — тогда и только тогда ищем по названию
        with tmdb_redirect("https://www.themoviedb.org/tv/1399", status_code=404):
            service.episodes(4444, season=1)
        self.assertEqual(len(stills.searches), 1)
        self.assertEqual(stills.stills_for, [(999, 1)])

    def test_resolved_id_is_cached(self) -> None:
        service, stills, _ = self.service_with_stills()
        with tmdb_redirect("https://www.themoviedb.org/tv/1399") as calls:
            service.episodes(4444, season=1)
            service.episodes(4444, season=2)
        self.assertEqual(len(calls), 1, "редирект TMDB спрашиваем один раз на тайтл")


# «Джентльмены» (kp 1435343, TMDB 236235), как их отдавали Кинопоиск и TMDB
# живьём 28.09.2026: у Кинопоиска сезон-заглушка 3 — восемь серий
# «Episode #3.N», дата только у первой, и та скопирована со 2 сезона. У TMDB
# два сезона; Netflix продлил сериал в августе 2026, даты 3 сезона нет.
GENTLEMEN_SEASONS = [
    season(1, *["2024-03-07"] * 8),
    season(2, *["2026-09-03"] * 8),
    season(3, "2026-09-03", *[None] * 7),
]
GENTLEMEN_TMDB = {1: "2024-03-07", 2: "2026-09-03"}
RULE_DAY = datetime.date(2026, 9, 28)


class UnreleasedSeasonsTests(unittest.TestCase):
    """Правило: сезон не вышел, если у сериала есть TMDB и TMDB этого сезона
    не знает или ставит ему дату позже сегодняшней, а у Кинопоиска в этом
    сезоне не больше одной серии с прошедшей датой."""

    def unreleased(self, seasons: list[dict[str, Any]], tmdb: dict[int, str] | None) -> set[int]:
        return meta_service.unreleased_seasons(seasons, tmdb, today=RULE_DAY)

    def test_gentlemen_placeholder_season_is_unreleased(self) -> None:
        self.assertEqual(self.unreleased(GENTLEMEN_SEASONS, GENTLEMEN_TMDB), {3})

    def test_series_without_tmdb_is_left_as_it_was(self) -> None:
        # Судить не по чему — поле не появляется, как до правила
        self.assertEqual(self.unreleased(GENTLEMEN_SEASONS, None), set())

    def test_tmdb_date_in_the_future_marks_the_season(self) -> None:
        seasons = [season(1, "2024-01-10", "2024-01-17"), season(2, "2027-03-05", None)]
        self.assertEqual(self.unreleased(seasons, {1: "2024-01-10", 2: "2027-03-05"}), {2})
        # В день премьеры сезон уже вышел, даже если Кинопоиск ещё пуст
        on_premiere = meta_service.unreleased_seasons(seasons, {2: "2027-03-05"}, today=datetime.date(2027, 3, 5))
        self.assertEqual(on_premiere, set())

    def test_released_seasons_under_other_numbering_are_not_marked(self) -> None:
        # Аниме: у Кинопоиска три сезона, у TMDB всё лежит в одном. Даты
        # серий Кинопоиска и держат правило от ложной пометки
        seasons = [
            season(1, "2013-04-07", "2013-04-14"),
            season(2, "2017-04-01", "2017-04-08"),
            season(3, "2018-07-23", "2018-07-30"),
        ]
        self.assertEqual(self.unreleased(seasons, {0: "", 1: "2013-04-07"}), set())
        # И наоборот: у TMDB сезонов больше, чем у Кинопоиска
        self.assertEqual(self.unreleased(seasons[:1], {1: "2013-04-07", 2: "2017-04-01"}), set())

    def test_season_tmdb_lists_without_a_date_is_not_confirmed(self) -> None:
        # Объявленный сезон TMDB заводит без даты — это ещё не выход
        self.assertEqual(self.unreleased(GENTLEMEN_SEASONS, {**GENTLEMEN_TMDB, 3: ""}), {3})

    def test_first_episode_of_an_airing_season_counts_as_released(self) -> None:
        # Неделя премьеры: у Кинопоиска одна серия вышла, остальные впереди,
        # а TMDB уже знает дату сезона
        seasons = [season(4, "2026-09-23", "2026-09-30", "2026-10-07")]
        self.assertEqual(self.unreleased(seasons, {4: "2026-09-23"}), set())

    def test_kinopoisk_dates_outweigh_a_lagging_tmdb(self) -> None:
        # Две вышедшие серии у Кинопоиска — сезон вышел, чего бы TMDB ни знал
        seasons = [season(1, "2025-01-01", "2025-01-08"), season(2, "2026-09-20", "2026-09-27", None)]
        self.assertEqual(self.unreleased(seasons, {1: "2025-01-01"}), set())

    def test_announced_season_without_episodes(self) -> None:
        seasons = [season(1, "2025-05-01", "2025-05-08"), {"number": 2, "episodes": []}]
        self.assertEqual(self.unreleased(seasons, {1: "2025-05-01"}), {2})

    def test_film_has_nothing_to_mark(self) -> None:
        self.assertEqual(self.unreleased([], {}), set())


class FakeTmdb:
    """TMDB на границе клиента: даты сезонов, кадры и нечёткий поиск, со счётом вызовов."""

    def __init__(self, dates: dict[int, str] | None = None, error: Exception | None = None) -> None:
        self.dates = dict(GENTLEMEN_TMDB if dates is None else dates)
        self.error = error
        self.season_calls: list[int] = []
        self.searches: list[str] = []

    def season_dates(self, tv_id: int) -> dict[int, str]:
        self.season_calls.append(tv_id)
        if self.error is not None:
            raise self.error
        return dict(self.dates)

    def stills(self, tv_id: int, season: int) -> dict[int, str]:
        return {}

    def find_series(self, query: str, year: int | None) -> int | None:
        self.searches.append(query)
        return 999


GENTLEMEN_FILM = {
    "kinopoiskId": 1435343,
    "imdbId": "tt13210838",
    "nameRu": "Джентльмены",
    "nameOriginal": "The Gentlemen",
    "year": 2024,
    "type": "TV_SERIES",
    "serial": True,
}
GENTLEMEN_REDIRECT = "https://www.themoviedb.org/tv/236235"


def gentlemen_service(
    tmdb: FakeTmdb,
    seasons: list[dict[str, Any]] | None = None,
    record: dict[int, str] | None = None,
    record_age: float = 0,
) -> tuple[meta_service.MetaService, FakeHttp]:
    """Сервис с кэшем, какой лежал на сервере до правила: карточка, состав и сезоны.

    `record` — запись дат TMDB заданного возраста, как её оставил прежний ответ.
    """
    now = time.time()
    entries: dict[str, Any] = {
        "film:1435343": {"stamp": now, "value": GENTLEMEN_FILM},
        "staff:1435343": {"stamp": now, "value": []},
        "seasons:1435343": {"stamp": now, "value": GENTLEMEN_SEASONS if seasons is None else seasons},
    }
    if record is not None:
        value = {"tmdb_id": 236235, "seasons": {str(number): date for number, date in record.items()}}
        entries["tmdb-seasons:1435343"] = {"stamp": now - record_age, "value": value}
    folder = Path(tempfile.mkdtemp())
    (folder / "meta.json").write_text(json.dumps(entries), encoding="utf-8")
    kinopoisk = meta_service.KinopoiskMeta("test-key")
    http = FakeHttp({})
    kinopoisk.http = http  # type: ignore[assignment]
    service = meta_service.MetaService(
        kinopoisk=kinopoisk,
        tmdb=tmdb,  # type: ignore[arg-type]
        cache=meta_service.TTLCache(folder / "meta.json"),
        shelf_cache=meta_service.TTLCache(folder / "shelf.json", 3600),
    )
    return service, http


class UnreleasedSeasonServiceTests(unittest.TestCase):
    """Пометка в карточке и сериях — из кэша, без запросов к Кинопоиску."""

    def released(self, card: meta_service.TitleCard) -> list[tuple[int, bool | None]]:
        return [(item.season, item.released) for item in card.seasons]

    def test_card_cached_before_the_rule_gets_the_mark(self) -> None:
        # Карточка и сезоны лежат в кэше с прошлой версии: пометка считается
        # при ответе, ждать истечения кэша не нужно
        tmdb = FakeTmdb()
        service, http = gentlemen_service(tmdb)
        with tmdb_redirect(GENTLEMEN_REDIRECT):
            card = service.title_card(1435343)
        self.assertEqual(self.released(card), [(1, None), (2, None), (3, False)])
        self.assertEqual(card.meta.seasons_count, 3, "число сезонов правило не трогает")
        self.assertEqual(http.calls, [], "ни одного запроса к Кинопоиску")
        self.assertEqual(tmdb.season_calls, [236235])

    def test_episodes_carry_the_same_mark(self) -> None:
        tmdb = FakeTmdb()
        service, http = gentlemen_service(tmdb)
        with tmdb_redirect(GENTLEMEN_REDIRECT):
            episodes = service.episodes(1435343)
            third = service.episodes(1435343, season=3)
        marks = {(item.season, item.released) for item in episodes}
        self.assertEqual(marks, {(1, None), (2, None), (3, False)})
        self.assertEqual([item.released for item in third], [False] * 8)
        self.assertEqual(http.calls, [])

    def test_card_and_episodes_share_one_tmdb_request(self) -> None:
        tmdb = FakeTmdb()
        service, _ = gentlemen_service(tmdb)
        with tmdb_redirect(GENTLEMEN_REDIRECT) as redirects:
            service.title_card(1435343)
            service.episodes(1435343)
            service.title_card(1435343)
        self.assertEqual(tmdb.season_calls, [236235])
        # Редирект карточки (tmdb_id в meta) звучит на каждый ответ, как и
        # прежде; правило и кадры добавляют к нему один на тайтл
        self.assertEqual(len(redirects), 3)

    def test_mark_is_rechecked_after_a_day(self) -> None:
        tmdb = FakeTmdb({**GENTLEMEN_TMDB, 3: "2026-09-25"})
        service, _ = gentlemen_service(tmdb, record=GENTLEMEN_TMDB, record_age=2 * DAY)
        with tmdb_redirect(GENTLEMEN_REDIRECT):
            card = service.title_card(1435343)
        # TMDB завёл вышедший сезон — пометка снята в тот же день
        self.assertEqual(tmdb.season_calls, [236235])
        self.assertEqual(self.released(card), [(1, None), (2, None), (3, None)])

    def test_mark_is_not_rechecked_within_a_day(self) -> None:
        tmdb = FakeTmdb()
        service, _ = gentlemen_service(tmdb, record=GENTLEMEN_TMDB, record_age=3600)
        with tmdb_redirect(GENTLEMEN_REDIRECT):
            card = service.title_card(1435343)
        self.assertEqual(tmdb.season_calls, [])
        self.assertEqual(self.released(card)[-1], (3, False))

    def test_record_that_confirms_every_season_keeps_thirty_days(self) -> None:
        tmdb = FakeTmdb()
        confirmed = {**GENTLEMEN_TMDB, 3: "2026-09-20"}
        service, _ = gentlemen_service(tmdb, record=confirmed, record_age=20 * DAY)
        with tmdb_redirect(GENTLEMEN_REDIRECT):
            card = service.title_card(1435343)
        self.assertEqual(tmdb.season_calls, [])
        self.assertEqual(self.released(card)[-1], (3, None))

    def test_released_series_never_asks_tmdb(self) -> None:
        tmdb = FakeTmdb()
        service, _ = gentlemen_service(tmdb, seasons=GENTLEMEN_SEASONS[:2])
        with tmdb_redirect(GENTLEMEN_REDIRECT):
            card = service.title_card(1435343)
        self.assertEqual(tmdb.season_calls, [])
        self.assertEqual(self.released(card), [(1, None), (2, None)])

    def test_series_without_exact_tmdb_match_is_not_marked(self) -> None:
        # Редирект по IMDb не нашёл сериал. Нечёткий поиск по названию нашёл
        # бы что-то, но чужой сериал — не повод прятать сезон
        tmdb = FakeTmdb()
        service, _ = gentlemen_service(tmdb)
        with tmdb_redirect(GENTLEMEN_REDIRECT, status_code=404):
            card = service.title_card(1435343)
            episodes = service.episodes(1435343, season=3)
        self.assertEqual(self.released(card), [(1, None), (2, None), (3, None)])
        self.assertEqual({item.released for item in episodes}, {None})
        self.assertEqual(tmdb.season_calls, [], "даты чужого сериала не спрашиваем")

    def test_without_tmdb_key_nothing_changes(self) -> None:
        service, _ = gentlemen_service(FakeTmdb())
        service.tmdb = None
        with tmdb_redirect(GENTLEMEN_REDIRECT):
            card = service.title_card(1435343)
        self.assertEqual(self.released(card), [(1, None), (2, None), (3, None)])

    def test_silent_tmdb_leaves_seasons_unmarked(self) -> None:
        tmdb = FakeTmdb(error=RuntimeError("TMDB молчит"))
        service, _ = gentlemen_service(tmdb)
        with tmdb_redirect(GENTLEMEN_REDIRECT):
            card = service.title_card(1435343)
        self.assertEqual(self.released(card), [(1, None), (2, None), (3, None)])

    def test_failed_recheck_keeps_the_previous_record_for_a_day(self) -> None:
        tmdb = FakeTmdb(error=RuntimeError("TMDB молчит"))
        service, _ = gentlemen_service(tmdb, record=GENTLEMEN_TMDB, record_age=2 * DAY)
        with tmdb_redirect(GENTLEMEN_REDIRECT):
            first = service.title_card(1435343)
            second = service.title_card(1435343)
        self.assertEqual(self.released(first)[-1], (3, False))
        self.assertEqual(self.released(second)[-1], (3, False))
        # Второй ответ таймаута TMDB уже не ждёт
        self.assertEqual(tmdb.season_calls, [236235])


class TmdbSeasonDatesTests(unittest.TestCase):
    """Разбор /3/tv/{id}: форма — из живого ответа TMDB для «Джентльменов» 28.09.2026.

    Третий сезон без даты дописан: `air_date: null` у TMDB бывает — так в тот
    же день пришли спецвыпуски (сезон 0) сериала «BBC: Планеты».
    """

    def test_season_list_becomes_dates(self) -> None:
        client = meta_service.TMDBStills("v3-key")
        http = FakeHttp({"3/tv/236235": {
            "id": 236235,
            "number_of_seasons": 2,
            "seasons": [
                {"air_date": "2024-03-07", "episode_count": 8, "season_number": 1, "name": "Season 1"},
                {"air_date": "2026-09-03", "episode_count": 8, "season_number": 2, "name": "Season 2"},
                {"air_date": None, "episode_count": 0, "season_number": 3, "name": "Season 3"},
            ],
        }})
        client.http = http  # type: ignore[assignment]
        self.assertEqual(client.season_dates(236235), {1: "2024-03-07", 2: "2026-09-03", 3: ""})
        self.assertEqual(http.calls, [("https://api.themoviedb.org/3/tv/236235", {"api_key": "v3-key"})])


SEARCH_KEYWORD_PAYLOAD = {
    "keyword": "матрица",
    "pagesCount": 1,
    "films": [
        {
            "filmId": 301,
            "nameRu": "Матрица",
            "nameEn": "The Matrix",
            "type": "FILM",
            "year": "1999",
            "rating": "8.5",
            "countries": [{"country": "США"}],
            "genres": [{"genre": "фантастика"}],
            "posterUrl": "http://example.org/poster.jpg",
            "posterUrlPreview": "http://example.org/preview.jpg",
        }
    ],
}


class CatalogSearchTests(unittest.TestCase):
    def test_search_is_cached_across_case_and_whitespace(self) -> None:
        service, http = catalog_service({"films/search-by-keyword": SEARCH_KEYWORD_PAYLOAD})
        first = service.search("  Матрица  ")
        second = service.search("матрица")
        third = service.search("МАТРИЦА\t")
        self.assertEqual(len(http.calls), 1)
        self.assertEqual(http.calls[0][1], {"keyword": "Матрица", "page": 1})
        self.assertEqual(first, second)
        self.assertEqual(first, third)
        self.assertEqual(first[0].catalog_id, 301)
        self.assertEqual(first[0].name, "Матрица")
        self.assertTrue(first[0].poster_url.startswith("https://"))

    def test_search_collapses_internal_whitespace_in_cache_key(self) -> None:
        service, http = catalog_service({"films/search-by-keyword": SEARCH_KEYWORD_PAYLOAD})
        service.search("Звёздные   войны")
        service.search("звёздные войны")
        self.assertEqual(len(http.calls), 1)
        self.assertEqual(http.calls[0][1]["keyword"], "Звёздные войны")

    def test_search_cache_persists_between_instances(self) -> None:
        service, http = catalog_service({"films/search-by-keyword": SEARCH_KEYWORD_PAYLOAD})
        service.search("Матрица")
        self.assertEqual(len(http.calls), 1)

        restarted_kp = meta_service.KinopoiskMeta("test-key")
        restarted_http = FakeHttp({"films/search-by-keyword": SEARCH_KEYWORD_PAYLOAD})
        restarted_kp.http = restarted_http  # type: ignore[assignment]
        restarted = meta_service.MetaService(
            kinopoisk=restarted_kp,
            tmdb=None,
            cache=meta_service.TTLCache(service.cache._path),
            shelf_cache=meta_service.TTLCache(service.shelf_cache._path, meta_service.SHELF_TTL_SECONDS),
        )
        titles = restarted.search("матрица")
        self.assertEqual(len(restarted_http.calls), 0)
        self.assertEqual(titles[0].catalog_id, 301)

    def test_valid_empty_search_is_cached_for_short_ttl(self) -> None:
        service, http = catalog_service({"films/search-by-keyword": {"keyword": "нет", "films": []}})
        self.assertEqual(service.search("НесуществующийФильм"), [])
        self.assertEqual(service.search("несуществующийфильм"), [])
        self.assertEqual(len(http.calls), 1)

        # Старше SEARCH_EMPTY_TTL_SECONDS пустая выдача перечитывается заново
        cache_key = "search:несуществующийфильм"
        service.shelf_cache._items[cache_key] = (
            time.time() - meta_service.SEARCH_EMPTY_TTL_SECONDS - 60,
            [],
        )
        http.routes["films/search-by-keyword"] = SEARCH_KEYWORD_PAYLOAD
        refreshed = service.search("НесуществующийФильм")
        self.assertEqual(len(http.calls), 2)
        self.assertEqual(refreshed[0].catalog_id, 301)

    def test_errors_and_quota_are_never_cached(self) -> None:
        for status, expected_exc in (
            (402, meta_service.CatalogLimitError),
            (429, meta_service.CatalogLimitError),
            (500, RuntimeError),
        ):
            with self.subTest(status=status):
                service, http = catalog_service({"films/search-by-keyword": SEARCH_KEYWORD_PAYLOAD}, status=status)
                with self.assertRaises(expected_exc):
                    service.search("Матрица")
                self.assertIsNone(service.shelf_cache.get("search:матрица"))
                http.status = 200
                titles = service.search("Матрица")
                self.assertEqual(len(http.calls), 2)
                self.assertEqual(titles[0].catalog_id, 301)

    def test_unexpected_payload_is_error_and_not_cached(self) -> None:
        service, http = catalog_service({"films/search-by-keyword": {"error": "oops"}}, status=200)
        with self.assertRaises(RuntimeError):
            service.search("Матрица")
        self.assertIsNone(service.shelf_cache.get("search:матрица"))
        self.assertEqual(len(http.calls), 1)


class ConnectionPoolTests(unittest.TestCase):
    def _assert_pool(self, session: Any, url: str, *, expect_retries: bool) -> None:
        adapter = session.get_adapter(url)
        self.assertGreaterEqual(adapter._pool_maxsize, 24)
        self.assertGreaterEqual(adapter._pool_connections, 24)
        if expect_retries:
            self.assertEqual(adapter.max_retries.total, 2)
            self.assertIn(429, adapter.max_retries.status_forcelist)
            self.assertFalse(adapter.max_retries.raise_on_status)
        else:
            self.assertEqual(adapter.max_retries.total, 0)

    def test_all_http_clients_have_enlarged_connection_pools(self) -> None:
        self._assert_pool(
            meta_service.KinopoiskMeta("key").http,
            "https://kinopoiskapiunofficial.tech/api/v2.2/films",
            expect_retries=True,
        )
        self._assert_pool(
            meta_service.TMDBStills("key").http,
            "https://api.themoviedb.org/3/tv/1",
            expect_retries=True,
        )
        self._assert_pool(
            meta_service.bot.KinopoiskCatalog("key").http,
            "https://kinopoiskapiunofficial.tech/api/v2.1/films/search-by-keyword",
            expect_retries=True,
        )
        self._assert_pool(
            meta_service.bot.NnmClient().http,
            "https://nnmclub.to/forum/",
            expect_retries=True,
        )
        self._assert_pool(
            meta_service.bot.ProwlarrClient("http://127.0.0.1:9696", "key").http,
            "http://127.0.0.1:9696/api/v1/search",
            expect_retries=True,
        )
        self._assert_pool(
            meta_service.bot.ByparrClient().http,
            "http://127.0.0.1:8192/v1",
            expect_retries=False,
        )


import tmdb_catalog


class _TmdbFakeResponse:
    def __init__(self, payload: Any, status_code: int = 200) -> None:
        self._payload = payload
        self.status_code = status_code

    def json(self) -> Any:
        return self._payload

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            err = meta_service.requests.HTTPError(f"HTTP {self.status_code}")
            err.response = self  # type: ignore[assignment]
            raise err


class _TmdbFakeHttp:
    def __init__(self, routes: dict[str, Any], status: int = 200) -> None:
        self.routes = routes
        self.status = status
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.headers: dict[str, str] = {}

    def get(self, url: str, params: dict[str, Any] | None = None, timeout: int = 0) -> _TmdbFakeResponse:
        self.calls.append((url, dict(params or {})))
        for route, payload in self.routes.items():
            if route in url:
                if callable(payload):
                    return _TmdbFakeResponse(payload(url, params or {}), self.status)
                return _TmdbFakeResponse(payload, self.status)
        return _TmdbFakeResponse({}, self.status)


PUSS_MOVIE_DETAIL = {
    "id": 315162,
    "title": "Кот в сапогах 2: Последнее желание",
    "original_title": "Puss in Boots: The Last Wish",
    "release_date": "2022-12-07",
    "runtime": 102,
    "vote_average": 8.3,
    "vote_count": 7800,
    "tagline": "Скажи «привет» своему маленькому другу",
    "overview": "Кот в сапогах потратил восемь из девяти жизней.",
    "poster_path": "/kuf6dutpsT0vSVehic3EZIqkOBt.jpg",
    "backdrop_path": "/jr8tSoJGj33XLgFBy6lmZhpGQNu.jpg",
    "original_language": "en",
    "production_countries": [{"iso_3166_1": "US", "name": "United States"}],
    "genres": [{"id": 16, "name": "Мультфильм"}, {"id": 10751, "name": "Семейный"}],
    "external_ids": {"imdb_id": "tt3915174"},
    "images": {
        "logos": [{"file_path": "/pussRuLogo123.png", "iso_639_1": "ru", "vote_average": 5.5, "vote_count": 10}],
        "posters": [{"file_path": "/pussCleanArt1.jpg", "iso_639_1": None, "vote_average": 6.0, "vote_count": 8}],
    },
    "credits": {
        "crew": [{"name": "Джоэл Кроуфорд", "job": "Director", "profile_path": "/dirPhoto1234.jpg"}],
        "cast": [{"name": "Антонио Бандерас", "character": "Puss in Boots", "profile_path": "/actPhoto1234.jpg"}],
    },
}

GRAVITY_FALLS_DETAIL = {
    "id": 40075,
    "name": "Гравити Фолз",
    "original_name": "Gravity Falls",
    "first_air_date": "2012-06-15",
    "status": "Ended",
    "type": "Scripted",
    "episode_run_time": [22],
    "vote_average": 8.6,
    "vote_count": 3200,
    "overview": "Близнецы проводят лето у дяди Стэна.",
    "poster_path": "/gfPoster12345.jpg",
    "backdrop_path": "/gfBackdrop123.jpg",
    "original_language": "en",
    "origin_country": ["US"],
    "genres": [{"id": 16, "name": "Мультфильм"}, {"id": 10762, "name": "Детский"}],
    "external_ids": {"imdb_id": "tt1865718"},
    "images": {"logos": [], "posters": []},
    "created_by": [{"name": "Алекс Хирш", "profile_path": "/alexHirsch12.jpg"}],
    "credits": {"crew": [], "cast": [{"name": "Джейсон Риттер", "character": "Dipper", "profile_path": "/ritter123456.jpg"}]},
    "seasons": [
        {"season_number": 0, "episode_count": 17, "air_date": "2013-10-14"},
        {"season_number": 1, "episode_count": 20, "air_date": "2012-06-15"},
        {"season_number": 2, "episode_count": 20, "air_date": "2014-08-01"},
        {"season_number": 3, "episode_count": 0, "air_date": None},
    ],
}

GRAVITY_SEASON_1 = {
    "air_date": "2012-06-15",
    "episodes": [
        {
            "season_number": 1,
            "episode_number": 1,
            "name": "Ловушка для туриста",
            "overview": "Диппер находит дневник.",
            "air_date": "2012-06-15",
            "still_path": "/gfEp1Still123.jpg",
        },
        {
            "season_number": 1,
            "episode_number": 2,
            "name": "Легенда о Живогрызе",
            "overview": "Рыбалка на озере.",
            "air_date": "2012-06-29",
            "still_path": "/gfEp2Still123.jpg",
        },
    ],
}


def _make_tmdb_service(
    tmdb_routes: dict[str, Any],
    kp_routes: dict[str, Any] | None = None,
    *,
    imdb_map: dict[str, tuple[float, int]] | None = None,
    tmdb_status: int = 200,
    img_base_url: str = "",
) -> tuple[meta_service.MetaService, _TmdbFakeHttp, FakeHttp]:
    folder = Path(tempfile.mkdtemp())
    cache = meta_service.TTLCache(folder / "meta.json")
    shelf_cache = meta_service.TTLCache(folder / "shelf.json", meta_service.SHELF_TTL_SECONDS)
    tmdb_http = _TmdbFakeHttp(tmdb_routes, status=tmdb_status)
    lookup_dict = imdb_map or {"tt3915174": (7.8, 195000), "tt1865718": (8.9, 145000)}
    tcat = tmdb_catalog.TmdbCatalog(
        "tmdb-test-key",
        session=tmdb_http,  # type: ignore[arg-type]
        img_base_url=img_base_url,
        imdb_lookup=lambda tid: lookup_dict.get(tid),
        imdb_id_resolver=lambda cid: str(cache.get(f"tmdb:imdb:{cid}") or ""),
        cache=cache,
    )
    kp = meta_service.KinopoiskMeta("kp-test-key")
    kp_http = FakeHttp(kp_routes or {})
    kp.http = kp_http  # type: ignore[assignment]
    svc = meta_service.MetaService(
        kinopoisk=kp,
        tmdb=None,
        cache=cache,
        shelf_cache=shelf_cache,
        tmdb_catalog=tcat,
        primary_backend="tmdb",
    )
    return svc, tmdb_http, kp_http


class TmdbHybridMetaServiceTests(unittest.TestCase):
    def test_shelf_routes_to_tmdb_and_caches(self) -> None:
        routes = {
            "discover/movie": {
                "results": [
                    {
                        "id": 315162,
                        "title": "Кот в сапогах 2",
                        "original_title": "Puss in Boots: The Last Wish",
                        "release_date": "2022-12-07",
                        "vote_average": 8.3,
                        "vote_count": 7800,
                        "genre_ids": [16, 10751],
                        "original_language": "en",
                        "poster_path": "/kuf6dutpsT0vSVehic3EZIqkOBt.jpg",
                    }
                ]
            }
        }
        svc, tmdb_http, kp_http = _make_tmdb_service(routes)
        first = svc.shelf("kids_animation", page=5)
        second = svc.shelf("kids_animation", page=5)
        self.assertEqual(len(first), 1)
        self.assertEqual(first[0].catalog_id, 100315162)
        self.assertEqual(first[0].name, "Кот в сапогах 2")
        self.assertEqual(first, second)
        self.assertEqual(len(tmdb_http.calls), 1)
        self.assertEqual(tmdb_http.calls[0][1]["page"], 5)
        self.assertEqual(kp_http.calls, [])

    def test_search_routes_to_tmdb_caches_non_empty_and_short_empty_never_errors(self) -> None:
        routes = {
            "search/multi": {
                "results": [
                    {
                        "id": 315162,
                        "media_type": "movie",
                        "title": "Атлантида: Затерянный мир",
                        "original_title": "Atlantis: The Lost Empire",
                        "release_date": "2001-06-15",
                        "vote_average": 7.0,
                        "vote_count": 4800,
                        "popularity": 45.0,
                        "genre_ids": [16],
                        "original_language": "en",
                        "poster_path": "/atlantis12345.jpg",
                    }
                ]
            }
        }
        svc, tmdb_http, kp_http = _make_tmdb_service(routes)
        first = svc.search("  Атлантида  ")
        second = svc.search("атлантида")
        self.assertEqual(len(tmdb_http.calls), 1)
        self.assertEqual(kp_http.calls, [])
        self.assertEqual(first[0].catalog_id, 100315162)
        self.assertEqual(first, second)

        svc2, tmdb_http2, _ = _make_tmdb_service({"search/multi": {"results": []}})
        self.assertEqual(svc2.search("НетТакого"), [])
        self.assertEqual(svc2.search("неттакого"), [])
        self.assertEqual(len(tmdb_http2.calls), 1)
        svc2.shelf_cache._items["tmdb:search:неттакого"] = (
            time.time() - meta_service.SEARCH_EMPTY_TTL_SECONDS - 60,
            [],
        )
        svc2.search("НетТакого")
        self.assertEqual(len(tmdb_http2.calls), 2)

        svc3, tmdb_http3, _ = _make_tmdb_service({"search/multi": {}}, tmdb_status=500)
        with self.assertRaises(Exception):
            svc3.search("Ошибка")
        self.assertIsNone(svc3.shelf_cache.get("tmdb:search:ошибка"))

    def test_discover_supports_page_10_and_up_to_50_with_cache(self) -> None:
        routes = {
            "discover/movie": {
                "results": [
                    {
                        "id": 5001,
                        "title": "Фильм 10",
                        "original_title": "Movie 10",
                        "release_date": "2023-05-01",
                        "vote_average": 7.4,
                        "vote_count": 200,
                        "genre_ids": [16],
                        "original_language": "en",
                        "poster_path": "/movie10poster.jpg",
                    }
                ]
            }
        }
        svc, tmdb_http, kp_http = _make_tmdb_service(routes)
        cards = svc.discover(media_type="FILM", genre="мультфильм", country="США", year_from=2021, page=10)
        self.assertEqual(len(cards), 1)
        self.assertEqual(cards[0]["catalog_id"], 100005001)
        self.assertEqual(tmdb_http.calls[0][1]["page"], 10)
        svc.discover(media_type="FILM", genre="мультфильм", country="США", year_from=2021, page=10)
        self.assertEqual(len(tmdb_http.calls), 1)
        svc.discover(media_type="FILM", page=999)
        self.assertEqual(tmdb_http.calls[-1][1]["page"], tmdb_catalog.MAX_DISCOVER_PAGE)
        self.assertEqual(kp_http.calls, [])

    def test_title_card_routes_tmdb_and_legacy_kp_independently(self) -> None:
        tmdb_routes = {
            "movie/315162": PUSS_MOVIE_DETAIL,
            "tv/40075": GRAVITY_FALLS_DETAIL,
        }
        kp_routes = {
            "films/4444/seasons": SEASONS_PAYLOAD,
            "api/v2.2/films/4444": SERIES_FILM_PAYLOAD,
            "v1/staff": [],
        }
        svc, tmdb_http, kp_http = _make_tmdb_service(tmdb_routes, kp_routes)

        movie = svc.title_card(100315162)
        self.assertTrue(movie.meta.matched)
        self.assertEqual(movie.meta.source, "tmdb")
        self.assertEqual(movie.meta.tmdb_id, 315162)
        self.assertEqual(movie.meta.tmdb_media_type, "movie")
        self.assertEqual(movie.imdb_id, "tt3915174")
        self.assertEqual(svc.cache.get("tmdb:imdb:100315162"), "tt3915174")
        self.assertEqual(movie.meta.cast[0]["profession"], "director")
        self.assertEqual(movie.meta.cast[1]["name"], "Антонио Бандерас")
        self.assertEqual(kp_http.calls, [])

        svc.title_card(100315162)
        self.assertEqual(len(tmdb_http.calls), 1)

        tv = svc.title_card(200040075)
        self.assertTrue(tv.meta.matched)
        self.assertEqual(tv.imdb_id, "tt1865718")
        self.assertEqual([(s.season, s.episodes, s.released) for s in tv.seasons], [(1, 20, None), (2, 20, None), (3, 0, False)])
        self.assertEqual(kp_http.calls, [])

        with tmdb_redirect("https://www.themoviedb.org/tv/1399"):
            legacy = svc.title_card(4444)
        self.assertTrue(legacy.meta.matched)
        self.assertEqual(legacy.meta.source, "kinopoisk")
        self.assertEqual(legacy.meta.title, "Игра престолов")
        self.assertGreater(len(kp_http.calls), 0)

    def test_episodes_routes_tmdb_and_caches(self) -> None:
        svc, tmdb_http, kp_http = _make_tmdb_service({"tv/40075/season/1": GRAVITY_SEASON_1})
        eps = svc.episodes(200040075, season=1)
        self.assertEqual(len(eps), 2)
        self.assertEqual(eps[0].title, "Ловушка для туриста")
        self.assertEqual(eps[0].still_url, "/kino/img/w780/gfEp1Still123.jpg")
        self.assertIsNone(eps[0].released)
        eps2 = svc.episodes(200040075, season=1)
        self.assertEqual(eps, eps2)
        self.assertEqual(len(tmdb_http.calls), 1)
        self.assertEqual(kp_http.calls, [])

    def test_similar_works_for_tmdb_id_and_cached_legacy_kp_id_with_zero_kp_calls(self) -> None:
        rec_payload = {
            "results": [
                {
                    "id": 9991,
                    "media_type": "movie",
                    "title": "Шрек",
                    "original_title": "Shrek",
                    "release_date": "2001-05-18",
                    "vote_average": 7.7,
                    "vote_count": 16000,
                    "genre_ids": [16, 10751],
                    "original_language": "en",
                    "poster_path": "/shrekPoster12.jpg",
                }
            ]
        }
        tmdb_routes = {
            "movie/315162/recommendations": rec_payload,
            "find/tt6723592": {"movie_results": [{"id": 577922}]},
            "movie/577922/recommendations": rec_payload,
        }
        svc, tmdb_http, kp_http = _make_tmdb_service(tmdb_routes)

        sim = svc.similar(100315162, limit=8)
        self.assertEqual([c["catalog_id"] for c in sim], [100009991])
        svc.similar(100315162, limit=8)
        self.assertEqual(len(tmdb_http.calls), 1)
        self.assertEqual(kp_http.calls, [])

        svc.cache.put("film:1236063", TENET_FILM_PAYLOAD)
        sim_kp = svc.similar(1236063, limit=8)
        self.assertEqual([c["catalog_id"] for c in sim_kp], [100009991])
        self.assertEqual(kp_http.calls, [])

    def test_mixed_briefs_preserves_order_for_tmdb_and_legacy_kp(self) -> None:
        svc, tmdb_http, kp_http = _make_tmdb_service(
            {"movie/315162": PUSS_MOVIE_DETAIL},
            {"api/v2.2/films/1236063": TENET_FILM_PAYLOAD},
        )
        svc.cache.put("film:1236063", TENET_FILM_PAYLOAD)
        cards = svc.briefs([100315162, 1236063])
        self.assertEqual([c["catalog_id"] for c in cards], [100315162, 1236063])
        self.assertEqual(cards[0]["name"], "Кот в сапогах 2: Последнее желание")
        self.assertEqual(cards[0]["rating_imdb"], "7.8")
        self.assertEqual(cards[1]["name"], "Довод")
        self.assertEqual(kp_http.calls, [])
        self.assertEqual(svc.cache.get("tmdb:imdb:100315162"), "tt3915174")

    def test_match_and_quota_use_tmdb_without_hitting_kp(self) -> None:
        tmdb_routes = {
            "search/multi": {
                "results": [
                    {
                        "id": 40075,
                        "media_type": "tv",
                        "name": "Гравити Фолз",
                        "original_name": "Gravity Falls",
                        "first_air_date": "2012-06-15",
                        "vote_average": 8.6,
                        "vote_count": 3200,
                        "popularity": 80.0,
                        "genre_ids": [16],
                        "original_language": "en",
                        "origin_country": ["US"],
                        "poster_path": "/gfPoster12345.jpg",
                    }
                ]
            },
            "tv/40075": GRAVITY_FALLS_DETAIL,
        }
        svc, tmdb_http, kp_http = _make_tmdb_service(tmdb_routes)
        matched = svc.match("Гравити Фолз / Gravity Falls (Сезон 2) [2014, WEB-DL 1080p]")
        self.assertTrue(matched.matched)
        self.assertEqual(matched.source, "tmdb")
        self.assertEqual(matched.source_id, 200040075)
        self.assertEqual(matched.season_hint, 2)
        self.assertEqual(kp_http.calls, [])

        calls_before = len(tmdb_http.calls)
        matched2 = svc.match("Гравити Фолз / Gravity Falls (Сезон 2) [2014, WEB-DL 1080p]")
        self.assertEqual(len(tmdb_http.calls), calls_before)
        self.assertEqual(matched2.season_hint, 2)

        q = svc.quota()
        self.assertEqual(q["accountType"], "TMDB")
        self.assertEqual(q["totalQuota"]["value"], -1)
        self.assertEqual(q["dailyQuota"]["value"], 100000)
        self.assertGreaterEqual(q["dailyQuota"]["used"], 2)
        self.assertEqual(kp_http.calls, [])
