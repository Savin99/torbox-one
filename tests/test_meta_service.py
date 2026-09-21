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
        card = service.title_card(4444)
        self.assertTrue(card.meta.matched)
        self.assertEqual(card.meta.title, "Игра престолов")
        self.assertEqual(card.meta.original_title, "Game of Thrones")
        self.assertEqual(card.imdb_id, "tt0944947")
        self.assertEqual([(item.season, item.episodes) for item in card.seasons], [(1, 2), (2, 1)])
        # tmdb_id Кинопоиск не отдаёт, а TMDB в этой сборке не настроен
        self.assertEqual(card.tmdb_id, 0)

    def test_card_is_cached_between_calls(self) -> None:
        service, http = catalog_service(self.routes())
        service.title_card(4444)
        calls = len(http.calls)
        service.title_card(4444)
        self.assertEqual(len(http.calls), calls)

    def test_card_costs_three_catalog_requests(self) -> None:
        service, http = catalog_service(self.routes())
        service.title_card(4444)
        # Карточка, состав и сезоны — и ни одного лишнего запроса из 500 в сутки
        self.assertEqual(len(http.calls), 3)

    def test_unknown_title_is_not_matched(self) -> None:
        service, _ = catalog_service({})
        self.assertFalse(service.title_card(999).meta.matched)


class GenreDirectoryTests(unittest.TestCase):
    def test_genres_are_cached(self) -> None:
        service, http = catalog_service({"films/filters": FILTERS_PAYLOAD})
        self.assertEqual(service.genre_id("драма"), 8)
        self.assertEqual(service.genre_id("ДРАМА"), 8)
        self.assertEqual(len(http.calls), 1)

    def test_unknown_genre_has_no_id(self) -> None:
        service, _ = catalog_service({"films/filters": FILTERS_PAYLOAD})
        self.assertIsNone(service.genre_id("киберпанк"))


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
