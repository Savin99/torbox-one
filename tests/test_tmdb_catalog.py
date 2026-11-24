"""Offline unit tests for `tmdb_catalog.py`. No network access is used."""

from __future__ import annotations

import datetime
import unittest
from typing import Any

import requests

import meta_service
import tmdb_catalog as tc
import torbox_tg_bot as bot


class FakeResponse:
    def __init__(self, payload: Any, status_code: int = 200) -> None:
        self._payload = payload
        self.status_code = status_code

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            err = requests.HTTPError(f"HTTP {self.status_code}")
            err.response = self  # type: ignore[assignment]
            raise err

    def json(self) -> Any:
        return self._payload


class FakeSession:
    def __init__(self, handler: Any) -> None:
        self.handler = handler
        self.headers: dict[str, str] = {}
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def get(self, url: str, params: dict[str, Any] | None = None, timeout: int = 15) -> FakeResponse:
        copied = dict(params or {})
        self.calls.append((url, copied))
        return self.handler(url, copied)


class DictCache:
    def __init__(self) -> None:
        self.store: dict[str, Any] = {}

    def get(self, key: str) -> Any | None:
        return self.store.get(key)

    def put(self, key: str, value: Any) -> None:
        self.store[key] = value


class IdEncodingTests(unittest.TestCase):
    def test_encode_and_decode_movie_and_tv(self) -> None:
        mid = tc.encode_id("movie", 315162)
        tid = tc.encode_id("tv", 40075)
        self.assertEqual(mid, 100_315_162)
        self.assertEqual(tid, 200_040_075)
        self.assertEqual(tc.decode_id(mid), ("movie", 315162))
        self.assertEqual(tc.decode_id(tid), ("tv", 40075))

    def test_decode_legacy_kp_and_invalid_ids(self) -> None:
        for raw in (0, -5, 1, 326, 99_999_999, 100_000_000, 200_000_000, 300_000_000, True):
            self.assertIsNone(tc.decode_id(raw))  # type: ignore[arg-type]

    def test_encode_rejects_invalid_inputs(self) -> None:
        with self.assertRaises(ValueError):
            tc.encode_id("person", 123)
        with self.assertRaises(ValueError):
            tc.encode_id("movie", 0)
        with self.assertRaises(ValueError):
            tc.encode_id("tv", 100_000_000)


class GenreAndCountryMappingTests(unittest.TestCase):
    def test_movie_and_tv_combo_genres(self) -> None:
        self.assertEqual(tc.map_genres([28, 12, 878, 53]), ["боевик", "приключения", "фантастика", "триллер"])
        self.assertEqual(
            tc.map_genres([10759, 10762, 10765, 10768]),
            ["боевик", "приключения", "детский", "семейный", "фантастика", "фэнтези", "военный", "драма"],
        )
        self.assertEqual(
            tc.map_genres([{"id": 10763}, {"id": 10764}, {"id": 10767}]),
            ["новости", "реальное тв", "ток-шоу"],
        )

    def test_anime_tagging(self) -> None:
        self.assertEqual(tc.map_genres([16, 12], original_language="ja"), ["мультфильм", "приключения", "аниме"])
        self.assertEqual(
            tc.map_genres([16, 10759], origin_countries=["JP"]),
            ["мультфильм", "боевик", "приключения", "аниме"],
        )
        self.assertEqual(
            tc.map_genres([16, 10751], original_language="en", origin_countries=["US"]),
            ["мультфильм", "семейный"],
        )
        # Live-action Japanese films do NOT receive the anime tag
        self.assertEqual(tc.map_genres([18, 53], original_language="ja", origin_countries=["JP"]), ["драма", "триллер"])

    def test_reverse_genre_lookup(self) -> None:
        self.assertEqual(tc.genre_id_for("боевик", "movie"), 28)
        self.assertEqual(tc.genre_id_for("боевик", "tv"), 10759)
        self.assertEqual(tc.genre_id_for("приключения", "movie"), 12)
        self.assertEqual(tc.genre_id_for("приключения", "tv"), 10759)
        self.assertEqual(tc.genre_id_for("фантастика", "movie"), 878)
        self.assertEqual(tc.genre_id_for("фантастика", "tv"), 10765)
        self.assertEqual(tc.genre_id_for("фэнтези", "movie"), 14)
        self.assertEqual(tc.genre_id_for("фэнтези", "tv"), 10765)
        self.assertEqual(tc.genre_id_for("детский", "movie"), 10751)
        self.assertEqual(tc.genre_id_for("детский", "tv"), 10762)
        self.assertEqual(tc.genre_id_for("мультфильм", "movie"), 16)
        self.assertEqual(tc.genre_id_for("триллер", "movie"), 53)
        self.assertEqual(tc.genre_id_for("триллер", "tv"), 9648)
        self.assertIsNone(tc.genre_id_for("выдуманный", "movie"))

    def test_country_mapping_aliases_and_fallbacks(self) -> None:
        self.assertEqual(tc.country_iso_for("США"), "US")
        self.assertEqual(tc.country_iso_for("Корея Южная"), "KR")
        self.assertEqual(tc.country_iso_for("южная корея"), "KR")
        self.assertEqual(tc.canonical_country_name("южная корея"), "Корея Южная")

        # Origin / production countries take priority
        self.assertEqual(
            tc.map_countries({"origin_country": ["GB", "US"], "original_language": "en"}),
            ["Великобритания", "США"],
        )
        self.assertEqual(
            tc.map_countries({"production_countries": [{"iso_3166_1": "FR"}, {"iso_3166_1": "BE"}]}),
            ["Франция", "Бельгия"],
        )
        # Russian original_language ALWAYS maps to Россия
        self.assertEqual(tc.map_countries({"original_language": "ru"}, fallback_country="США"), ["Россия"])
        # Explicit country filter fallback when movie list omits origin_country
        self.assertEqual(
            tc.map_countries({"original_language": "en"}, fallback_country="Великобритания"),
            ["Великобритания"],
        )
        # Language fallback when no country metadata or filter
        self.assertEqual(tc.map_countries({"original_language": "ja"}), ["Япония"])
        self.assertEqual(tc.map_countries({"original_language": "ko"}), ["Корея Южная"])
        # Unknown language never yields empty list
        self.assertEqual(tc.map_countries({"original_language": "xx"}), ["Другие"])


class ImageRatingAndSessionTests(unittest.TestCase):
    def test_img_path_url_validation_and_base(self) -> None:
        self.assertEqual(tc.img_path_url("/abcd1234ef.jpg", "w500"), "/kino/img/w500/abcd1234ef.jpg")
        self.assertEqual(
            tc.img_path_url("/abcd1234ef.png", "w1280", "https://example.com/"),
            "https://example.com/kino/img/w1280/abcd1234ef.png",
        )
        self.assertEqual(tc.img_path_url("/short.jpg", "w500"), "")
        self.assertEqual(tc.img_path_url("/abcd1234ef.svg", "w500"), "")
        self.assertEqual(tc.img_path_url("/../abcd1234ef.jpg", "w500"), "")
        self.assertEqual(tc.img_path_url(None, "w500"), "")
        self.assertEqual(tc.img_path_url("/abcd1234ef.jpg", "../bad"), "")

    def test_rating_hygiene_with_and_without_imdb(self) -> None:
        rating, rating_imdb, votes = tc.resolve_ratings(
            7.84,
            4200,
            imdb_id="tt3915174",
            imdb_lookup=lambda iid: (8.1, 180_000) if iid == "tt3915174" else None,
        )
        self.assertEqual((rating, rating_imdb, votes), ("7.8", "8.1", 180_000))

        rating, rating_imdb, votes = tc.resolve_ratings(
            0.0,
            10,
            catalog_id=100_000_001,
            imdb_id_resolver=lambda cid: "tt1234567",
            imdb_lookup=lambda iid: (7.5, 50_000),
        )
        self.assertEqual((rating, rating_imdb, votes), ("7.5", "7.5", 50_000))

        # >= 50 votes mirrors TMDB rating into rating_imdb to avoid -0.4 KP-only discount
        self.assertEqual(tc.resolve_ratings(7.66, 120), ("7.7", "7.7", 120))

        # < 50 votes leaves rating_imdb empty
        self.assertEqual(tc.resolve_ratings(8.9, 12), ("8.9", "", 12))

    def test_default_session_retry_and_pool_configuration(self) -> None:
        cat = tc.TmdbCatalog("test-v3-key")
        adapter = cat.http.get_adapter("https://api.themoviedb.org/3/movie/1")
        self.assertEqual(adapter._pool_connections, 32)
        self.assertEqual(adapter._pool_maxsize, 32)
        retry = adapter.max_retries
        self.assertEqual(retry.total, 2)
        self.assertEqual(retry.connect, 2)
        self.assertEqual(retry.read, 2)
        self.assertEqual(retry.backoff_factor, 0.3)
        self.assertEqual(retry.status_forcelist, (429, 500, 502, 503, 504))
        self.assertEqual(retry.allowed_methods, frozenset({"GET"}))
        self.assertFalse(retry.raise_on_status)


class TmdbCatalogEndpointsTests(unittest.TestCase):
    def _sample_movie(self, movie_id: int = 315162, **overrides: Any) -> dict[str, Any]:
        base: dict[str, Any] = {
            "id": movie_id,
            "title": "Кот в сапогах 2: Последнее желание",
            "original_title": "Puss in Boots: The Last Wish",
            "release_date": "2022-12-07",
            "vote_average": 8.26,
            "vote_count": 7800,
            "popularity": 90.5,
            "genre_ids": [16, 10751, 12, 35],
            "original_language": "en",
            "poster_path": "/pussPoster123.jpg",
            "backdrop_path": "/pussBackdrop123.jpg",
            "overview": "Кот тратит восемь жизней из девяти.",
        }
        base.update(overrides)
        return base

    def _sample_tv(self, tv_id: int = 40075, **overrides: Any) -> dict[str, Any]:
        base: dict[str, Any] = {
            "id": tv_id,
            "name": "Гравити Фолз",
            "original_name": "Gravity Falls",
            "first_air_date": "2012-06-15",
            "vote_average": 8.5,
            "vote_count": 3200,
            "popularity": 55.0,
            "genre_ids": [16, 35, 9648, 10762],
            "origin_country": ["US"],
            "original_language": "en",
            "poster_path": "/gfPoster12345.jpg",
            "backdrop_path": "/gfBackdrop123.jpg",
            "overview": "Близнецы проводят лето у дяди Стэна.",
        }
        base.update(overrides)
        return base

    def test_search_reranking_atlantis_and_russian_hits(self) -> None:
        search_payload = {
            "results": [
                {"media_type": "person", "id": 999, "name": "Атлантида Актёр"},
                {
                    "media_type": "movie",
                    "id": 618181,
                    "title": "Атлантида",
                    "original_title": "Атлантида",
                    "release_date": "2019-01-01",
                    "vote_count": 3,
                    "vote_average": 6.0,
                    "popularity": 0.3,
                    "original_language": "ru",
                    "genre_ids": [18],
                    "poster_path": "/ruAtlantis12.jpg",
                },
                {
                    "media_type": "tv",
                    "id": 2290,
                    "name": "Звёздные врата: Атлантида",
                    "original_name": "Stargate Atlantis",
                    "first_air_date": "2004-07-16",
                    "vote_count": 1273,
                    "vote_average": 7.9,
                    "popularity": 54.4,
                    "origin_country": ["CA", "US"],
                    "original_language": "en",
                    "genre_ids": [10765],
                    "poster_path": "/stargateAtl12.jpg",
                },
                {
                    "media_type": "movie",
                    "id": 428399,
                    "title": "Атлантида",
                    "original_title": "Cold Skin",
                    "release_date": "2017-10-20",
                    "vote_count": 773,
                    "vote_average": 6.1,
                    "popularity": 2.0,
                    "original_language": "en",
                    "genre_ids": [27, 878],
                    "poster_path": "/coldSkin1234.jpg",
                },
                {
                    "media_type": "movie",
                    "id": 10865,
                    "title": "Атлантида: Затерянный мир",
                    "original_title": "Atlantis: The Lost Empire",
                    "release_date": "2001-06-02",
                    "vote_count": 5329,
                    "vote_average": 7.0,
                    "popularity": 6.2,
                    "original_language": "en",
                    "genre_ids": [16, 10751, 12],
                    "poster_path": "/atlantisLost1.jpg",
                },
            ]
        }
        session = FakeSession(lambda url, params: FakeResponse(search_payload))
        cat = tc.TmdbCatalog("v3key", session=session, img_base_url="https://kino.example")
        cards = cat.search("Атлантида")

        self.assertEqual(len(cards), 4)
        self.assertEqual(cards[0]["catalog_id"], tc.encode_id("movie", 10865))
        self.assertEqual(cards[0]["name"], "Атлантида: Затерянный мир")
        self.assertEqual(cards[0]["poster_url"], "https://kino.example/kino/img/w500/atlantisLost1.jpg")
        self.assertIn(tc.encode_id("movie", 618181), [c["catalog_id"] for c in cards])

        title_obj = cat.to_catalog_title(cards[0])
        self.assertIsInstance(title_obj, bot.CatalogTitle)
        self.assertEqual(title_obj.catalog_id, tc.encode_id("movie", 10865))
        self.assertEqual(title_obj.search_query, "Атлантида: Затерянный мир 2001")

    def test_search_empty_query_skips_network(self) -> None:
        session = FakeSession(lambda url, params: FakeResponse({}))
        cat = tc.TmdbCatalog("v3key", session=session)
        self.assertEqual(cat.search("   "), [])
        self.assertEqual(session.calls, [])

    def test_discover_film_tv_miniseries_cartoons_and_page10(self) -> None:
        captured: list[tuple[str, dict[str, Any]]] = []

        def handler(url: str, params: dict[str, Any]) -> FakeResponse:
            captured.append((url, params))
            if url.endswith("/discover/tv"):
                return FakeResponse(
                    {
                        "results": [
                            self._sample_tv(101),
                            self._sample_tv(102, original_language="ru", origin_country=["RU"]),
                        ]
                    }
                )
            return FakeResponse(
                {
                    "results": [
                        self._sample_movie(201),
                        self._sample_movie(202, original_language="ru"),
                    ]
                }
            )

        cat = tc.TmdbCatalog("v3key", session=FakeSession(handler))

        # 1. Cartoon movie discover excludes anime keyword 210024 and Russian items, supports page=10
        cartoons = cat.discover(media_type="FILM", genre="мультфильм", page=10)
        self.assertEqual(len(cartoons), 1)
        self.assertEqual(cartoons[0]["media_type"], "FILM")
        url, params = captured[-1]
        self.assertTrue(url.endswith("/discover/movie"))
        self.assertEqual(params["with_genres"], "16")
        self.assertEqual(params["without_keywords"], "210024")
        self.assertEqual(params["page"], 10)
        self.assertEqual(params["vote_count.gte"], 100)
        self.assertEqual(params["with_origin_country"], tc.NON_RUSSIAN_ORIGIN_COUNTRIES)

        # 2. Cartoon with country=Япония does NOT exclude anime
        cat.discover(media_type="FILM", genre="мультфильм", country="Япония", order="YEAR", year_to=2024)
        _, params = captured[-1]
        self.assertNotIn("without_keywords", params)
        self.assertEqual(params["with_origin_country"], "JP")
        self.assertEqual(params["vote_count.gte"], 40)
        self.assertEqual(params["sort_by"], "primary_release_date.desc")
        self.assertEqual(params["primary_release_date.lte"], "2024-12-31")

        # 3. TV thriller maps to "9648|80", excludes news/reality/talk
        tv_thrillers = cat.discover(
            media_type="TV_SERIES", genre="триллер", country="США", year_from=2012, rating_from=7.0
        )
        self.assertEqual(len(tv_thrillers), 2)
        url, params = captured[-1]
        self.assertTrue(url.endswith("/discover/tv"))
        self.assertEqual(params["with_genres"], "9648|80")
        self.assertEqual(params["with_origin_country"], "US")
        self.assertEqual(params["without_genres"], tc.TV_NON_FICTION_GENRES)
        self.assertEqual(params["first_air_date.gte"], "2012-01-01")
        self.assertEqual(params["vote_average.gte"], 7.0)
        self.assertEqual(params["vote_count.gte"], 50)

        # 4. MINI_SERIES sets with_type=2, vote_count.gte=25, marks media_type="MINI_SERIES"
        minis = cat.discover(media_type="MINI_SERIES", country="Великобритания", order="RATING")
        self.assertEqual(minis[0]["media_type"], "MINI_SERIES")
        _, params = captured[-1]
        self.assertEqual(params["with_type"], 2)
        self.assertEqual(params["vote_count.gte"], 25)
        self.assertEqual(params["sort_by"], "vote_average.desc")

        # 5. Recent NUM_VOTE switches to popularity.desc + lower vote threshold
        current_year = datetime.date.today().year
        cat.discover(media_type="ALL", year_from=current_year - 1, order="NUM_VOTE")
        url, params = captured[-1]
        self.assertTrue(url.endswith("/discover/movie"))
        self.assertEqual(params["sort_by"], "popularity.desc")
        self.assertEqual(params["vote_count.gte"], 40)

        # 6. Movie-only genre queried on TV returns empty list gracefully instead of 400
        self.assertEqual(cat.discover(media_type="TV_SERIES", genre="ужасы"), [])

        # 7. Invalid inputs raise ValueError
        with self.assertRaises(ValueError):
            cat.discover(media_type="INVALID")
        with self.assertRaises(ValueError):
            cat.discover(order="BOGUS")
        with self.assertRaises(ValueError):
            cat.discover(genre="несуществующий")
        with self.assertRaises(ValueError):
            cat.discover(country="Марс")

    def test_all_shelves_and_unknown_shelf_raises(self) -> None:
        calls: list[tuple[str, dict[str, Any]]] = []

        def handler(url: str, params: dict[str, Any]) -> FakeResponse:
            calls.append((url, params))
            if "trending/all" in url:
                return FakeResponse(
                    {
                        "results": [
                            {"media_type": "person", "id": 1, "name": "Actor"},
                            {**self._sample_movie(10), "media_type": "movie"},
                            {**self._sample_movie(11, original_language="ru"), "media_type": "movie"},
                            {**self._sample_tv(20), "media_type": "tv"},
                        ]
                    }
                )
            if "/tv" in url:
                return FakeResponse({"results": [self._sample_tv(21)]})
            return FakeResponse({"results": [self._sample_movie(12)]})

        cat = tc.TmdbCatalog("v3key", session=FakeSession(handler))

        pop = cat.shelf("popular", page=1)
        self.assertEqual(len(pop), 2)
        self.assertEqual([c["media_type"] for c in pop], ["FILM", "TV_SERIES"])

        all_shelves = [
            "popular",
            "popular_movies",
            "popular_series",
            "top",
            "top_movies",
            "top_series",
            "top_xxi",
            "premieres",
            "closest_releases",
            "kids_animation",
            "family",
            "comics",
            "vampires",
            "zombies",
            "catastrophe",
            "love",
            "programmers",
            "oscar_2021",
            "genre/комедия",
            "year/2023",
        ]
        for shelf_name in all_shelves:
            items = cat.shelf(shelf_name, page=1)
            self.assertGreaterEqual(len(items), 1, msg=f"Shelf {shelf_name} returned empty")

        # Verify kids_animation exact params
        cat.shelf("kids_animation", page=3)
        url, params = calls[-1]
        self.assertTrue(url.endswith("/discover/movie"))
        self.assertEqual(params["with_genres"], "16,10751")
        self.assertEqual(params["with_origin_country"], tc.KIDS_ORIGIN_COUNTRIES)
        self.assertEqual(params["without_keywords"], "210024")
        self.assertEqual(params["vote_count.gte"], 150)
        self.assertEqual(params["vote_average.gte"], 6.5)
        self.assertEqual(params["page"], 3)

        # Verify premieres returns clean FILM cards
        premieres = cat.shelf("premieres", page=1)
        self.assertEqual(premieres[0]["media_type"], "FILM")

        with self.assertRaises(ValueError):
            cat.shelf("nonexistent_shelf")
        with self.assertRaises(ValueError):
            cat.shelf("genre/выдуманный")

    def test_title_card_tv_with_future_unreleased_season_and_404(self) -> None:
        future_date = (datetime.date.today() + datetime.timedelta(days=120)).isoformat()
        detail = {
            "id": 40075,
            "name": "Гравити Фолз",
            "original_name": "Gravity Falls",
            "first_air_date": "2012-06-15",
            "type": "Miniseries",
            "episode_run_time": [],
            "last_episode_to_air": {"runtime": 44},
            "vote_average": 8.5,
            "vote_count": 3200,
            "overview": "Описание.",
            "tagline": "Доверять нельзя никому",
            "poster_path": "/gfPoster12345.jpg",
            "backdrop_path": "/gfBackdrop123.jpg",
            "origin_country": ["US"],
            "original_language": "en",
            "genres": [{"id": 16}, {"id": 9648}],
            "external_ids": {"imdb_id": "tt1865718"},
            "created_by": [{"name": "Алекс Хирш", "profile_path": "/alexHirsch12.jpg"}],
            "credits": {
                "cast": [{"name": "Джейсон Риттер", "character": "Диппер", "profile_path": "/jRitter12345.jpg"}],
                "crew": [{"name": "Джон Аошима", "job": "Director", "profile_path": None}],
            },
            "images": {
                "logos": [
                    {"iso_639_1": "ru", "file_path": "/badVector123.svg", "vote_average": 9.9, "vote_count": 100},
                    {"iso_639_1": "en", "file_path": "/enLogo123456.png", "vote_average": 5.0, "vote_count": 10},
                    {"iso_639_1": "ru", "file_path": "/ruLogo123456.png", "vote_average": 4.0, "vote_count": 5},
                ],
                "posters": [
                    {"iso_639_1": "ru", "file_path": "/ruPoster1234.jpg", "vote_average": 9.0, "vote_count": 50},
                    {"iso_639_1": None, "file_path": "/cleanArt1234.jpg", "vote_average": 6.0, "vote_count": 8},
                ],
            },
            "seasons": [
                {"season_number": 0, "episode_count": 102, "air_date": "2013-10-14"},
                {"season_number": 1, "episode_count": 20, "air_date": "2012-06-15"},
                {"season_number": 2, "episode_count": 8, "air_date": future_date},
                {"season_number": 3, "episode_count": 0, "air_date": None},
            ],
        }
        cache = DictCache()
        session = FakeSession(lambda url, params: FakeResponse(detail))
        cat = tc.TmdbCatalog("v3key", session=session, cache=cache)
        cid = tc.encode_id("tv", 40075)
        card = cat.title_card(cid)

        meta_obj = meta_service.TitleMeta(**card["meta"])
        self.assertTrue(meta_obj.matched)
        self.assertEqual(meta_obj.source, "tmdb")
        self.assertEqual(meta_obj.source_id, cid)
        self.assertEqual(meta_obj.tmdb_id, 40075)
        self.assertEqual(meta_obj.tmdb_media_type, "tv")
        self.assertEqual(meta_obj.media_type, "MINI_SERIES")
        self.assertEqual(meta_obj.duration_minutes, 44)
        self.assertEqual(meta_obj.logo_url, "/kino/img/w500/ruLogo123456.png")
        self.assertEqual(meta_obj.backdrop_url, "/kino/img/w1280/gfBackdrop123.jpg")
        self.assertEqual(meta_obj.seasons_count, 3)
        self.assertEqual(len(meta_obj.cast), 3)
        self.assertEqual(meta_obj.cast[0]["profession"], "director")
        self.assertEqual(meta_obj.cast[2]["profession"], "actor")
        self.assertEqual(card["imdb_id"], "tt1865718")
        self.assertEqual(card["raw"]["id"], 40075)

        season_objs = [meta_service.SeasonBrief(**s) for s in card["seasons"]]
        self.assertEqual(
            [(s.season, s.episodes, s.released) for s in season_objs],
            [(1, 20, None), (2, 8, False), (3, 0, False)],
        )
        # Cached hit does not hit TMDB again
        self.assertEqual(len(session.calls), 1)
        cat.title_card(cid)
        self.assertEqual(len(session.calls), 1)

        # 404 yields unmatched TitleMeta matching TitleMeta.empty()
        not_found = tc.TmdbCatalog("v3key", session=FakeSession(lambda u, p: FakeResponse({}, 404)))
        empty_card = not_found.title_card(tc.encode_id("movie", 999999))
        self.assertEqual(meta_service.TitleMeta(**empty_card["meta"]), meta_service.TitleMeta.empty())
        self.assertEqual(empty_card["seasons"], [])
        self.assertEqual(empty_card["imdb_id"], "")

    def test_episodes_single_and_all_seasons_with_stills(self) -> None:
        future_date = (datetime.date.today() + datetime.timedelta(days=30)).isoformat()

        def handler(url: str, params: dict[str, Any]) -> FakeResponse:
            if url.endswith("/tv/40075"):
                return FakeResponse(
                    {
                        "id": 40075,
                        "seasons": [
                            {"season_number": 0, "episode_count": 5},
                            {"season_number": 1, "episode_count": 1},
                            {"season_number": 2, "episode_count": 1},
                        ],
                    }
                )
            if url.endswith("/season/1"):
                return FakeResponse(
                    {
                        "air_date": "2012-06-15",
                        "episodes": [
                            {
                                "season_number": 1,
                                "episode_number": 1,
                                "name": "Ловушка для туристов",
                                "overview": "Первая серия.",
                                "air_date": "2012-06-15",
                                "still_path": "/stillEp00001.jpg",
                            },
                            {
                                "season_number": 1,
                                "episode_number": 2,
                                "name": "Будущая серия",
                                "overview": "",
                                "air_date": future_date,
                                "still_path": None,
                            },
                        ],
                    }
                )
            return FakeResponse(
                {
                    "air_date": "2014-08-01",
                    "episodes": [
                        {
                            "season_number": 2,
                            "episode_number": 1,
                            "name": "Жуткое караоке",
                            "overview": "Второй сезон.",
                            "air_date": "2014-08-01",
                            "still_path": "/stillEp02001.jpg",
                        }
                    ],
                }
            )

        session = FakeSession(handler)
        cat = tc.TmdbCatalog("v3key", session=session)
        tv_cid = tc.encode_id("tv", 40075)

        # Single season fetch -> 1 call
        s1 = cat.episodes(tv_cid, season=1)
        self.assertEqual(len(s1), 2)
        ep0 = meta_service.EpisodeMeta(**s1[0])
        ep1 = meta_service.EpisodeMeta(**s1[1])
        self.assertEqual(ep0.title, "Ловушка для туристов")
        self.assertEqual(ep0.still_url, "/kino/img/w780/stillEp00001.jpg")
        self.assertIsNone(ep0.released)
        self.assertFalse(ep1.released)
        self.assertEqual(len(session.calls), 1)

        # All seasons fetch skips season 0 and preserves season order
        all_eps = cat.episodes(tv_cid)
        self.assertEqual([(e["season"], e["episode"]) for e in all_eps], [(1, 1), (1, 2), (2, 1)])

        # Movie ID returns empty list without HTTP calls
        calls_before = len(session.calls)
        self.assertEqual(cat.episodes(tc.encode_id("movie", 315162)), [])
        self.assertEqual(len(session.calls), calls_before)

    def test_similar_one_call_and_fallback(self) -> None:
        session1 = FakeSession(
            lambda url, params: FakeResponse(
                {
                    "results": [
                        self._sample_movie(500, original_language="ru"),
                        self._sample_movie(501),
                        self._sample_movie(502),
                    ]
                }
            )
        )
        cat1 = tc.TmdbCatalog("v3key", session=session1)
        res1 = cat1.similar(tc.encode_id("movie", 315162), limit=1)
        self.assertEqual(len(res1), 1)
        self.assertEqual(res1[0]["catalog_id"], tc.encode_id("movie", 501))
        self.assertEqual(len(session1.calls), 1)
        self.assertTrue(session1.calls[0][0].endswith("/movie/315162/recommendations"))

        def fallback_handler(url: str, params: dict[str, Any]) -> FakeResponse:
            if url.endswith("/recommendations"):
                return FakeResponse({"results": []})
            return FakeResponse({"results": [self._sample_movie(601)]})

        session2 = FakeSession(fallback_handler)
        cat2 = tc.TmdbCatalog("v3key", session=session2)
        res2 = cat2.similar_by_tmdb("movie", 315162, limit=8)
        self.assertEqual(len(res2), 1)
        self.assertEqual(res2[0]["catalog_id"], tc.encode_id("movie", 601))
        self.assertEqual(len(session2.calls), 2)
        self.assertTrue(session2.calls[1][0].endswith("/movie/315162/similar"))

    def test_briefs_batching_cache_imdb_lookup_and_error_isolation(self) -> None:
        cache = DictCache()
        cid1 = tc.encode_id("movie", 315162)
        cid2 = tc.encode_id("movie", 10865)
        cid_bad = tc.encode_id("movie", 999999)
        cached_detail = {
            **self._sample_movie(315162),
            "runtime": 102,
            "tagline": "Почувствуй лапы",
            "imdb_id": "tt3915174",
            "production_countries": [{"iso_3166_1": "US"}],
            "images": {
                "logos": [{"iso_639_1": "ru", "file_path": "/pussLogo1234.png", "vote_average": 5.0, "vote_count": 3}],
                "posters": [{"iso_639_1": None, "file_path": "/pussArt12345.jpg", "vote_average": 4.0, "vote_count": 2}],
            },
        }
        cache.put(f"tmdb:detail:{cid1}", cached_detail)

        fetched_detail = {
            **self._sample_movie(10865, title="Атлантида: Затерянный мир"),
            "runtime": 95,
            "tagline": "Затерянная империя",
            "imdb_id": "tt0230011",
            "production_countries": [{"iso_3166_1": "US"}],
            "images": {"logos": [], "posters": []},
        }

        def handler(url: str, params: dict[str, Any]) -> FakeResponse:
            if url.endswith("/999999"):
                return FakeResponse({}, status_code=500)
            return FakeResponse(fetched_detail)

        session = FakeSession(handler)
        imdb_scores = {"tt3915174": (7.8, 195_000), "tt0230011": (6.9, 130_000)}
        cat = tc.TmdbCatalog("v3key", session=session, cache=cache, imdb_lookup=imdb_scores.get)

        cards = cat.briefs([326, cid1, cid_bad, cid2, cid1], max_workers=2)
        self.assertEqual([c["catalog_id"] for c in cards], [cid1, cid2])
        self.assertEqual(len(session.calls), 2)  # cid1 came from cache; cid2 & cid_bad fetched
        self.assertTrue(cards[0]["detailed"])
        self.assertEqual(cards[0]["logo_url"], "/kino/img/w500/pussLogo1234.png")
        self.assertEqual(cards[0]["art_url"], "/kino/img/w780/pussArt12345.jpg")
        self.assertEqual(cards[0]["backdrop_url"], "/kino/img/w1280/pussBackdrop123.jpg")
        self.assertEqual(cards[0]["rating_imdb"], "7.8")
        self.assertEqual(cards[0]["votes"], 195_000)
        self.assertEqual(cards[1]["tagline"], "Затерянная империя")
        self.assertEqual(cards[1]["votes"], 130_000)

    def test_find_by_imdb_and_bearer_auth(self) -> None:
        session = FakeSession(
            lambda url, params: FakeResponse({"movie_results": [{"id": 315162}], "tv_results": [{"id": 40075}]})
        )
        jwt_token = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJ0ZXN0In0.sig"
        cat = tc.TmdbCatalog(jwt_token, session=session)
        self.assertEqual(session.headers.get("Authorization"), f"Bearer {jwt_token}")
        self.assertEqual(cat.find_by_imdb("tt3915174"), ("movie", 315162))
        self.assertEqual(cat.find_by_imdb("tt1865718", prefer_kind="tv"), ("tv", 40075))
        self.assertNotIn("api_key", session.calls[0][1])
        self.assertIsNone(cat.find_by_imdb("not-an-imdb-id"))


if __name__ == "__main__":
    unittest.main()
