#!/usr/bin/env python3
"""Offline unit tests plus an opt-in source smoke test."""
from __future__ import annotations

import json
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from typing import Any

import bencodepy
import requests

from torbox_tg_bot import (
    BOTTOM_KEYBOARD_VERSION,
    STOP,
    CatalogTitle,
    CredentialStore,
    KinopoiskCatalog,
    NnmClient,
    ProwlarrClient,
    Release,
    SearchAggregator,
    State,
    TelegramBot,
    TorBoxClient,
    availability_summary,
    cache_ranked_releases,
    progress_percent,
    release_content_kind,
    score_release,
    seed_health_summary,
    select_diverse_releases,
    status_label,
    title_matches_query,
    torrent_fingerprint,
    video_quality_summary,
    voiceover_summary,
)


def release(title: str = "Тестовый <релиз>") -> Release:
    return Release(
        torrent_id="11", topic_id="22", title=title, category="Кино", size_text="1.5 GB",
        size_bytes=1610612736, seeders=20, leechers=1,
        source_url="https://example.test/topic", download_url="https://example.test/file", score=100,
    )


class FakeNnm:
    def search(self, query: str) -> list[Release]:
        return [release()]

    def topic_summary(self, item: Release) -> str:
        return "Перевод: официальный <дубляж>"

    def download_torrent(self, item: Release) -> bytes:
        return bencodepy.encode({b"announce": b"https://tracker.test", b"info": {b"name": b"test", b"length": 1}})


class FakeTorBox:
    def __init__(self) -> None:
        self.added = 0
        self.operations: list[tuple[int, str]] = []
        self.items: list[dict[str, Any]] = []
        self.notification_items: list[dict[str, Any]] = []
        self.magnets: list[str] = []
        self.cached: set[str] = set()

    def add_file(self, torrent_bytes: bytes, item: Release) -> dict[str, Any]:
        self.added += 1
        return {"success": True, "detail": "created", "data": {"torrent_id": 77}}

    def add_magnet(self, magnet_url: str, item: Release) -> dict[str, Any]:
        self.added += 1
        self.magnets.append(magnet_url)
        return {"success": True, "detail": "created", "data": {"torrent_id": 78}}

    def list_torrents(self, **kwargs: Any) -> list[dict[str, Any]]:
        return self.items

    def cached_hashes(self, info_hashes: list[str]) -> set[str]:
        return {value.casefold() for value in info_hashes if value.casefold() in self.cached}

    def find_torrent_by_hash(self, info_hash: str) -> dict[str, Any] | None:
        wanted = info_hash.casefold()
        return next((item for item in self.items if str(item.get("hash") or "").casefold() == wanted), None)

    def get_torrent(self, torrent_id: int, **kwargs: Any) -> dict[str, Any] | None:
        return next((item for item in self.items if item.get("id") == torrent_id), None)

    def control_torrent(self, torrent_id: int, operation: str) -> dict[str, Any]:
        self.operations.append((torrent_id, operation))
        if operation == "delete":
            self.items = [item for item in self.items if item.get("id") != torrent_id]
        return {"success": True}

    def notifications(self) -> list[dict[str, Any]]:
        return self.notification_items

    def clear_notifications(self) -> None:
        return None


class FakeResponse:
    def __init__(self, payload: Any, status_code: int = 200) -> None:
        self.payload = payload
        self.status_code = status_code
        self.ok = 200 <= status_code < 400

    def json(self) -> Any:
        return self.payload

    def raise_for_status(self) -> None:
        if not self.ok:
            raise requests.HTTPError(f"HTTP {self.status_code}")


class FakeSession:
    def __init__(self, responses: list[FakeResponse]) -> None:
        self.headers: dict[str, str] = {}
        self.responses = responses
        self.calls: list[tuple[str, str, dict[str, Any]]] = []

    def request(self, method: str, url: str, **kwargs: Any) -> FakeResponse:
        self.calls.append((method, url, kwargs))
        return self.responses.pop(0)

    def get(self, url: str, **kwargs: Any) -> FakeResponse:
        return self.request("GET", url, **kwargs)


class StaticProvider:
    def __init__(self, key: str, name: str, items: list[Release]) -> None:
        self.key, self.name, self.items = key, name, items

    def search(self, query: str) -> list[Release]:
        return self.items

    def topic_summary(self, item: Release) -> str:
        return item.details_text

    def download_torrent(self, item: Release) -> bytes:
        raise AssertionError("not used")


class FailingProvider(StaticProvider):
    def search(self, query: str) -> list[Release]:
        raise requests.ConnectionError("source unavailable")


class RecordingBot(TelegramBot):
    def __init__(self, state: State, torbox: FakeTorBox | None = None) -> None:
        self.messages: list[tuple[int, str, Any]] = []
        self.inline_edits: list[tuple[str, str, Any]] = []
        self.deleted_messages: list[tuple[int, int]] = []
        self.bottom_keyboard_installs: list[tuple[int, bool]] = []
        super().__init__("token", 123, torbox or FakeTorBox(), FakeNnm(), state)
        self.state.set_bottom_keyboard_version(123, BOTTOM_KEYBOARD_VERSION)

    def install_bottom_keyboard(self, chat_id: int, *, force: bool = False) -> int:
        self.bottom_keyboard_installs.append((chat_id, force))
        message_id = 1000 + len(self.bottom_keyboard_installs)
        self.state.set_screen_message_id(chat_id, message_id)
        self.state.set_bottom_keyboard_version(chat_id, BOTTOM_KEYBOARD_VERSION)
        return message_id

    def _send_new(self, chat_id: int, text: str, keyboard: Any = None) -> dict[str, int]:
        self.messages.append((chat_id, text, keyboard))
        return {"message_id": len(self.messages)}

    def edit_message(self, chat_id: int, message_id: int, text: str, keyboard: Any = None) -> dict[str, int]:
        if 1 <= message_id <= len(self.messages):
            self.messages[message_id - 1] = (chat_id, text, keyboard)
        else:
            self.messages.append((chat_id, text, keyboard))
        return {"message_id": message_id}

    def edit_inline_message(self, inline_message_id: str, text: str, keyboard: Any = None) -> bool:
        self.inline_edits.append((inline_message_id, text, keyboard))
        return True

    def answer_callback(self, callback_id: str, text: str | None = None, *, alert: bool = False) -> None:
        return None

    def delete_user_message(self, chat_id: int, message_id: int) -> None:
        self.deleted_messages.append((chat_id, message_id))


class InlineRecordingBot(RecordingBot):
    def __init__(self, state: State, catalog: Any = None) -> None:
        super().__init__(state)
        self.catalog = catalog
        self.username = "tor_boxai_bot"
        self.inline_answers: list[list[dict[str, Any]]] = []

    def answer_inline_query(self, inline_query_id: str, results: list[dict[str, Any]], *, cache_time: int = 30) -> None:
        self.inline_answers.append(results)


class QualityRankingTests(unittest.TestCase):
    def test_proper_hevc_release_beats_lq_even_with_more_seeds(self) -> None:
        lq = score_release("Фильм BDRip [H.264/1080p-LQ]", 60, 0)
        proper = score_release("Фильм BDRip [H.265/1080p] Дублированный", 29, 0)
        self.assertGreater(proper, lq)

    def test_camrip_is_penalized(self) -> None:
        camrip = score_release("Фильм CAMRip одноголосый", 90, 0)
        proper = score_release("Фильм WEB-DL [H.265/1080p] Дублированный", 10, 0)
        self.assertGreater(proper, camrip)

    def test_seeded_release_beats_seedless_remux(self) -> None:
        dead = score_release("Фильм BDRemux H.264 1080p", 0, 0)
        healthy = score_release("Фильм BDRip H.265 1080p", 9, 0)
        self.assertGreater(healthy, dead)

    def test_cached_release_beats_uncached_high_seed_release(self) -> None:
        cached = Release(**{**release("Фильм 2025 WEB-DL 1080p").to_dict(), "info_hash": "cached", "score": 20})
        uncached = Release(**{**release("Фильм 2025 BDRip 1080p").to_dict(), "info_hash": "fresh", "score": 100})
        ranked = cache_ranked_releases([uncached, cached], {"CACHED"})
        self.assertEqual(ranked[0].info_hash, "cached")
        self.assertTrue(ranked[0].cached)
        self.assertFalse(ranked[1].cached)


class FormattingTests(unittest.TestCase):
    def test_progress_accepts_fraction_and_percent(self) -> None:
        self.assertEqual(progress_percent(0.42), 42)
        self.assertEqual(progress_percent(42), 42)
        self.assertEqual(progress_percent(5), 5)
        self.assertEqual(progress_percent(200), 100)

    def test_completed_status_uses_download_finished(self) -> None:
        self.assertEqual(status_label({"download_finished": True, "download_state": "downloading"}), ("✅", "готово"))

    def test_release_metadata_is_readable_before_opening_details(self) -> None:
        item = release("Сериал BDRip [H.265/1080p] Кравец, AlexFilm, LostFilm")
        self.assertEqual(video_quality_summary(item), "BDRip · 1080p · H.265 / HEVC")
        self.assertEqual(voiceover_summary(item), "Кравец · AlexFilm · LostFilm")

    def test_tracker_translation_overrides_title_guess(self) -> None:
        item = release("Сериал WEB-DL 1080p LostFilm")
        details = "Перевод: дублированный (Пифагор), многоголосый (LostFilm)\nВидео: H.264"
        self.assertEqual(voiceover_summary(item, details), "дублированный (Пифагор), многоголосый (LostFilm)")

    def test_license_and_compact_track_codes_are_visible(self) -> None:
        licensed = release("Фильм BDRip-AVC-iTunes-Лицензия")
        coded = release("Фильм WEB-DL 1080p | D, P, A |")
        self.assertEqual(voiceover_summary(licensed), "Лицензия · iTunes")
        self.assertEqual(voiceover_summary(coded), "Авторская · Профессиональная · Дубляж")

    def test_seed_health_never_looks_green_for_zero(self) -> None:
        self.assertEqual(seed_health_summary(0), "⚠️ 0 сидов")
        self.assertEqual(seed_health_summary(1), "🟠 1 сид")
        self.assertEqual(seed_health_summary(22), "🟢 22 сида")

    def test_availability_distinguishes_cache_from_tracker_seeds(self) -> None:
        cached = Release(**{**release().to_dict(), "cached": True})
        uncached = Release(**{**release().to_dict(), "cached": False, "seeders": 22})
        self.assertEqual(availability_summary(cached), "⚡ в кэше TorBox")
        self.assertIn("не в кэше", availability_summary(uncached))

    def test_content_kind_separates_video_games_and_music(self) -> None:
        self.assertEqual(release_content_kind(release("Сериал S01 WEB-DL 1080p")), "video")
        self.assertEqual(release_content_kind(release("Game of Thrones (2012) PC | RePack")), "game")
        self.assertEqual(release_content_kind(release("Ramin Djawadi - Game of Thrones FLAC")), "music")

    def test_title_matching_rejects_same_root_but_missing_title_word(self) -> None:
        self.assertTrue(title_matches_query("Игра престолов", "Игра престолов / Game of Thrones S01"))
        self.assertFalse(title_matches_query("Игра престолов", "Что? Где? Когда? Весенняя серия игр"))


class StateTests(unittest.TestCase):
    def test_mode_and_notifications_survive_reload(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "state.json"
            state = State(path)
            state.set_mode(123, "search")
            state.mark_notifications(["1", "2"])
            restored = State(path)
            self.assertEqual(restored.mode(123), "search")
            self.assertEqual(restored.notification_ids(), {"1", "2"})
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_concurrent_inline_updates_keep_valid_complete_state(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "state.json"
            state = State(path)
            threads = [
                threading.Thread(
                    target=state.put_inline_item,
                    args=(f"token_{index}", {"kind": "catalog", "query": str(index)}),
                )
                for index in range(30)
            ]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
            restored = State(path)
            self.assertEqual(
                {restored.get_inline_item(f"token_{index}")["query"] for index in range(30)},
                {str(index) for index in range(30)},
            )

    def test_torbox_maintenance_state_is_isolated_per_user(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            state = State(Path(folder) / "state.json")
            state.mark_notifications(["owner-event"], 123)
            state.mark_notifications(["other-event"], 456)
            self.assertEqual(state.notification_ids(123), {"owner-event"})
            self.assertEqual(state.notification_ids(456), {"other-event"})


class CredentialStoreTests(unittest.TestCase):
    def test_keys_are_persisted_separately_with_private_permissions(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "credentials.json"
            store = CredentialStore(path)
            store.set_api_key(123, "owner-key-123456789")
            store.set_api_key(456, "other-key-123456789")

            restored = CredentialStore(path)
            self.assertEqual(restored.api_key(123), "owner-key-123456789")
            self.assertEqual(restored.api_key(456), "other-key-123456789")
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertNotIn("owner-key", json.dumps(State(Path(folder) / "state.json").data))

    def test_connect_flow_deletes_key_message_and_never_echoes_secret(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            state = State(Path(folder) / "state.json")
            store = CredentialStore(Path(folder) / "credentials.json")
            client = FakeTorBox()
            bot = RecordingBot(state)
            bot.credentials = store
            bot.multi_user_enabled = True
            bot.torbox_factory = lambda api_key: client
            secret = "new-user-key-123456789"

            bot.handle({"message": {
                "message_id": 10, "text": "/connect",
                "chat": {"id": 456}, "from": {"id": 456},
            }})
            bot.handle({"message": {
                "message_id": 11, "text": secret,
                "chat": {"id": 456}, "from": {"id": 456},
            }})

            self.assertEqual(store.api_key(456), secret)
            self.assertIn((456, 11), bot.deleted_messages)
            self.assertNotIn(secret, "\n".join(text for _, text, _ in bot.messages))
            self.assertIs(bot.torbox_for(456), client)

    def test_each_user_operates_only_their_own_torbox_client(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            state = State(Path(folder) / "state.json")
            store = CredentialStore(Path(folder) / "credentials.json")
            store.set_api_key(123, "owner-key-123456789")
            store.set_api_key(456, "other-key-123456789")
            owner_client = FakeTorBox()
            other_client = FakeTorBox()
            owner_client.items = [{"id": 9, "name": "Owner", "download_state": "cached"}]
            other_client.items = [{"id": 9, "name": "Other", "download_state": "cached"}]
            clients = {
                "owner-key-123456789": owner_client,
                "other-key-123456789": other_client,
            }
            bot = RecordingBot(state)
            bot.credentials = store
            bot.multi_user_enabled = True
            bot.torbox_factory = clients.__getitem__

            bot.handle({"callback_query": {
                "id": "delete-other", "data": "delconfirm:9",
                "message": {"chat": {"id": 456}}, "from": {"id": 456},
            }})

            self.assertEqual(other_client.operations, [(9, "delete")])
            self.assertEqual(owner_client.operations, [])


class TorBoxClientTests(unittest.TestCase):
    def test_list_torrents_accepts_torbox_empty_404_response(self) -> None:
        client = TorBoxClient("secret")
        session = FakeSession([FakeResponse({"success": True, "error": "ITEM_NOT_FOUND", "data": None}, 404)])
        client.session = session  # type: ignore[assignment]
        self.assertEqual(client.list_torrents(), [])
        self.assertEqual(session.calls[0][0], "GET")
        self.assertTrue(session.calls[0][1].endswith("/torrents/mylist"))

    def test_delete_sends_exact_id_and_operation(self) -> None:
        client = TorBoxClient("secret")
        session = FakeSession([FakeResponse({"success": True, "data": None})])
        client.session = session  # type: ignore[assignment]
        client.control_torrent(42, "delete")
        self.assertEqual(session.calls[0][2]["json"], {"torrent_id": 42, "operation": "delete", "all": False})

    def test_failed_torbox_response_raises_useful_error(self) -> None:
        client = TorBoxClient("secret")
        client.session = FakeSession([FakeResponse({"success": False, "detail": "Bad key"}, 403)])  # type: ignore[assignment]
        with self.assertRaisesRegex(RuntimeError, "Bad key"):
            client.notifications()


class MultiSourceSearchTests(unittest.TestCase):
    def test_prowlarr_maps_torrent_result(self) -> None:
        payload = [{
            "id": 12,
            "protocol": "torrent",
            "title": "Дюна 2021 WEB-DL 1080p Дублированный",
            "size": 10 * 1024**3,
            "indexerId": 3,
            "indexer": "RuTracker.org",
            "seeders": 77,
            "leechers": 4,
            "categories": [{"id": 2000, "name": "Movies"}],
            "magnetUrl": "magnet:?xt=urn:btih:ABCDEF1234&dn=test",
            "infoHash": "ABCDEF1234",
            "commentUrl": "https://rutracker.org/forum/viewtopic.php?t=12",
        }]
        client = ProwlarrClient("http://127.0.0.1:9696", "api-key", (3,))
        session = FakeSession([FakeResponse(payload)])  # type: ignore[arg-type]
        client.http = session  # type: ignore[assignment]
        items = client.search("Дюна 2021")
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].source_name, "RuTracker.org")
        self.assertEqual(items[0].provider, "prowlarr")
        self.assertEqual(items[0].info_hash, "ABCDEF1234")
        self.assertIn(("indexerIds", 3), session.calls[0][2]["params"])

    def test_prowlarr_http_magnet_proxy_uses_torrent_download_instead(self) -> None:
        payload = [{
            "id": 13,
            "protocol": "torrent",
            "title": "Я ругаюсь / I Swear (2025) BDRip 1080p",
            "size": 9 * 1024**3,
            "indexer": "RuTor",
            "seeders": 15,
            "magnetUrl": "http://127.0.0.1:9696/1/download?link=redirect-to-magnet",
            "downloadUrl": "http://127.0.0.1:9696/1/download?file=release.torrent",
            "infoHash": "ABCDEF1234567890",
        }]
        client = ProwlarrClient("http://127.0.0.1:9696", "api-key")
        client.http = FakeSession([FakeResponse(payload)])  # type: ignore[assignment]
        item = client.search("Я ругаюсь 2025")[0]
        self.assertEqual(item.magnet_url, "")
        self.assertIn("file=release.torrent", item.download_url)

    def test_aggregator_deduplicates_by_hash_and_prefers_trusted_source(self) -> None:
        common = release("Дюна 2021 WEB-DL 1080p Дублированный")
        a = Release(**{**common.to_dict(), "provider": "a", "source_name": "RuTor", "info_hash": "same", "seeders": 30})
        b = Release(**{**common.to_dict(), "provider": "b", "source_name": "RuTracker.org", "info_hash": "SAME", "seeders": 30})
        aggregator = SearchAggregator([StaticProvider("a", "A", [a]), StaticProvider("b", "B", [b])])
        items = aggregator.search("Дюна 2021")
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].source_name, "RuTracker.org")

    def test_one_failed_provider_does_not_break_other_results(self) -> None:
        good = StaticProvider("good", "Working", [release("Дюна 2021")])
        bad = FailingProvider("bad", "Broken", [])
        aggregator = SearchAggregator([bad, good])
        with self.assertLogs("torbox_tg_bot", level="WARNING"):
            items = aggregator.search("Дюна 2021")
        self.assertEqual(len(items), 1)
        self.assertIn("Broken: ошибка", aggregator.status_summary())

    def test_video_results_displace_similarly_named_games_and_music(self) -> None:
        videos = [release(f"Игра престолов S0{index} WEB-DL 1080p") for index in range(1, 5)]
        game = Release(**{**release("Игра престолов PC | RePack").to_dict(), "score": 999, "seeders": 500})
        music = Release(**{**release("Игра престолов OST FLAC").to_dict(), "score": 998, "seeders": 499})
        aggregator = SearchAggregator([StaticProvider("mixed", "Mixed", [game, music, *videos])])
        items = aggregator.search("Игра престолов")
        self.assertEqual(len(items), 4)
        self.assertTrue(all(release_content_kind(item) == "video" for item in items))

    def test_short_list_limits_near_identical_editions_when_alternatives_exist(self) -> None:
        clones = [
            Release(**{**release(f"Фильм 2025 BDRemux H.264 1080p cut-{index}").to_dict(), "source_name": "NNM-Club"})
            for index in range(6)
        ]
        alternatives = [
            Release(**{**release("Фильм 2025 WEB-DL H.265 1080p Лицензия").to_dict(), "source_name": "RuTor"}),
            Release(**{**release("Фильм 2025 BDRip H.264 720p iTunes").to_dict(), "source_name": "BigFANGroup"}),
        ]
        selected = select_diverse_releases([*clones, *alternatives], 4)
        self.assertEqual(len(selected), 4)
        self.assertIn("RuTor", {item.source_name for item in selected})
        self.assertIn("BigFANGroup", {item.source_name for item in selected})

    def test_magnet_result_is_added_without_downloading_torrent_file(self) -> None:
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        state = State(Path(folder.name) / "state.json")
        torbox = FakeTorBox()
        bot = RecordingBot(state, torbox)
        item = Release(**{
            **release().to_dict(),
            "provider": "prowlarr",
            "source_name": "RuTracker.org",
            "magnet_url": "magnet:?xt=urn:btih:ABC123&dn=test",
            "info_hash": "ABC123",
        })
        state.put_pending(123, item)
        bot.confirm_add(123)
        self.assertEqual(torbox.magnets, ["magnet:?xt=urn:btih:ABC123&dn=test"])


class InlineSearchTests(unittest.TestCase):
    def test_catalog_maps_poster_and_metadata(self) -> None:
        catalog = KinopoiskCatalog("api-key")
        catalog.http = FakeSession([FakeResponse({"films": [{
            "filmId": 123,
            "nameRu": "Игра",
            "nameEn": "The Game",
            "type": "TV_SERIES",
            "year": "2011",
            "rating": "8.1",
            "countries": [{"country": "Россия"}],
            "genres": [{"genre": "криминал"}, {"genre": "детектив"}],
            "posterUrlPreview": "http://images.example/poster.jpg",
        }]})])  # type: ignore[assignment]
        titles = catalog.search("игра")
        self.assertEqual(len(titles), 1)
        self.assertEqual(titles[0].search_query, "Игра 2011")
        self.assertEqual(titles[0].poster_url, "https://images.example/poster.jpg")

    def test_catalog_maps_popular_collection_with_poster(self) -> None:
        catalog = KinopoiskCatalog("api-key")
        catalog.http = FakeSession([FakeResponse({"items": [{
            "kinopoiskId": 456,
            "nameRu": "Популярный фильм",
            "nameOriginal": "Popular Movie",
            "type": "FILM",
            "year": "2026",
            "ratingKinopoisk": 7.9,
            "countries": [{"country": "США"}],
            "genres": [{"genre": "фантастика"}],
            "posterUrlPreview": "https://images.example/popular.jpg",
        }]})])  # type: ignore[assignment]
        titles = catalog.popular()
        self.assertEqual(len(titles), 1)
        self.assertEqual(titles[0].catalog_id, 456)
        self.assertEqual(titles[0].rating, "7.9")
        self.assertEqual(titles[0].poster_url, "https://images.example/popular.jpg")
        self.assertEqual(catalog.http.calls[0][2]["params"]["type"], "TOP_POPULAR_ALL")  # type: ignore[attr-defined]

    def test_empty_inline_query_shows_popular_titles(self) -> None:
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        title = CatalogTitle(
            456, "Популярный фильм", "Popular Movie", "FILM", "2026", "7.9",
            ("США",), ("фантастика",), "https://images.example/popular.jpg",
        )

        class PopularCatalog:
            def popular(self) -> list[CatalogTitle]:
                return [title]

            def search(self, query: str) -> list[CatalogTitle]:
                raise AssertionError("empty inline query must use popular collection")

        bot = InlineRecordingBot(State(Path(folder.name) / "state.json"), PopularCatalog())
        bot.inline_query({"id": "inline-popular", "from": {"id": 123}, "query": ""})
        deadline = time.monotonic() + 1
        while not bot.inline_answers and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertTrue(bot.inline_answers)
        self.assertEqual(bot.inline_answers[-1][0]["title"], "Популярный фильм (Popular Movie)")
        self.assertEqual(bot.inline_answers[-1][0]["thumbnail_url"], title.poster_url)

    def test_first_typed_character_switches_to_relevant_search(self) -> None:
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        title = CatalogTitle(
            789, "Дюна", "Dune", "FILM", "2021", "7.8",
            ("США",), ("фантастика",), "https://images.example/dune.jpg",
        )
        searched: list[str] = []

        class SearchCatalog:
            def popular(self) -> list[CatalogTitle]:
                raise AssertionError("non-empty inline query must use relevant search")

            def search(self, query: str) -> list[CatalogTitle]:
                searched.append(query)
                return [title]

        bot = InlineRecordingBot(State(Path(folder.name) / "state.json"), SearchCatalog())
        bot.inline_query({"id": "inline-short", "from": {"id": 123}, "query": "д"})
        deadline = time.monotonic() + 1
        while not bot.inline_answers and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertEqual(searched, ["д"])
        self.assertEqual(bot.inline_answers[-1][0]["title"], "Дюна (Dune)")

    def test_inline_catalog_result_has_poster_and_deep_link(self) -> None:
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        state = State(Path(folder.name) / "state.json")
        title = CatalogTitle(
            123, "Игра", "The Game", "TV_SERIES", "2011", "8.1",
            ("Россия",), ("криминал", "детектив"), "https://images.example/poster.jpg",
        )

        class StaticCatalog:
            def search(self, query: str) -> list[CatalogTitle]:
                return [title]

        bot = InlineRecordingBot(state, StaticCatalog())
        bot._process_inline_query("inline-1", "игра")
        result = bot.inline_answers[-1][0]
        self.assertEqual(result["thumbnail_url"], title.poster_url)
        self.assertIn("сериал · КП 8.1 · 2011", result["description"])
        self.assertEqual(
            result["reply_markup"]["inline_keyboard"][0][0]["callback_data"],
            "catalog:kp_123",
        )

    def test_inline_search_is_private(self) -> None:
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        bot = InlineRecordingBot(State(Path(folder.name) / "state.json"))
        bot.inline_query({"id": "inline-2", "from": {"id": 999}, "query": "игра"})
        self.assertEqual(bot.inline_answers[-1], [])

    def test_inline_search_waits_for_catalog_instead_of_showing_placeholder_cards(self) -> None:
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        bot = InlineRecordingBot(State(Path(folder.name) / "state.json"))
        bot.inline_query({"id": "inline-3", "from": {"id": 123}, "query": "игра престолов"})
        self.assertEqual(bot.inline_answers[-1], [])

    def test_inline_release_search_uses_fast_direct_provider(self) -> None:
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        item = release("Игра престолов S01 WEB-DL 1080p")
        direct = StaticProvider("nnm", "NNM-Club", [item])
        slow = FailingProvider("slow", "Slow", [])
        bot = InlineRecordingBot(State(Path(folder.name) / "state.json"))
        bot.nnm = SearchAggregator([direct, slow])
        self.assertEqual(bot._fast_inline_release_search("Игра престолов"), [item])

    def test_catalog_deep_link_starts_release_search(self) -> None:
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        state = State(Path(folder.name) / "state.json")
        state.put_inline_item("kp_123", {"kind": "catalog", "query": "Игра 2011", "title": "Игра"})
        bot = InlineRecordingBot(state)
        bot.handle({
            "message": {
                "text": "/start kp_123",
                "chat": {"id": 123},
                "from": {"id": 123},
            }
        })
        self.assertIn("Игра 2011", bot.messages[0][1])

    def test_chosen_catalog_card_becomes_release_results_in_same_inline_message(self) -> None:
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        state = State(Path(folder.name) / "state.json")
        state.put_inline_item("kp_123", {"kind": "catalog", "query": "Игра 2011", "title": "Игра"})
        bot = InlineRecordingBot(state)
        bot.chosen_inline_result({
            "result_id": "kp_123",
            "inline_message_id": "inline-message-1",
            "from": {"id": 123},
        })
        self.assertGreaterEqual(len(bot.inline_edits), 2)
        self.assertTrue(all(item[0] == "inline-message-1" for item in bot.inline_edits))
        self.assertIn("Нашёл", bot.inline_edits[-1][1])
        callbacks = [
            button.get("callback_data")
            for row in bot.inline_edits[-1][2]
            for button in row
        ]
        self.assertIn("add:0", callbacks)

    def test_catalog_fallback_button_edits_inline_card_without_deep_link(self) -> None:
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        state = State(Path(folder.name) / "state.json")
        state.put_inline_item("kp_123", {"kind": "catalog", "query": "Игра 2011", "title": "Игра"})
        bot = InlineRecordingBot(state)
        bot.callback({
            "id": "cb-catalog",
            "inline_message_id": "inline-message-2",
            "data": "catalog:kp_123",
            "from": {"id": 123},
        })
        self.assertEqual(bot.messages, [])
        self.assertIn("Нашёл", bot.inline_edits[-1][1])
        self.assertTrue(all(item[0] == "inline-message-2" for item in bot.inline_edits))


class TelegramFlowTests(unittest.TestCase):
    def make_state(self) -> tuple[tempfile.TemporaryDirectory[str], State]:
        folder = tempfile.TemporaryDirectory()
        return folder, State(Path(folder.name) / "state.json")

    def test_search_output_escapes_telegram_html(self) -> None:
        folder, state = self.make_state()
        self.addCleanup(folder.cleanup)
        bot = RecordingBot(state)
        bot.show_search(123, "movie <2025>")
        output = "\n".join(message[1] for message in bot.messages)
        self.assertIn("movie &lt;2025&gt;", output)
        self.assertIn("Тестовый &lt;релиз&gt;", output)

    def test_obsolete_bottom_keyboard_is_removed(self) -> None:
        markup = TelegramBot.bottom_keyboard_markup()
        self.assertEqual(markup, {"remove_keyboard": True})

    def test_start_recreates_keyboard_and_screen_after_chat_is_cleared(self) -> None:
        folder, state = self.make_state()
        self.addCleanup(folder.cleanup)
        state.set_screen_message_id(123, 281)
        state.set_bottom_keyboard_version(123, BOTTOM_KEYBOARD_VERSION)
        bot = RecordingBot(state)

        bot.handle({
            "message": {
                "message_id": 777,
                "text": "/start",
                "chat": {"id": 123},
                "from": {"id": 123},
            }
        })

        self.assertEqual(bot.bottom_keyboard_installs, [(123, True)])
        self.assertEqual(bot.deleted_messages, [(123, 777)])
        self.assertEqual(state.screen_message_id(123), 1001)
        self.assertIn("TorBox — личный пульт", bot.messages[-1][1])

    def test_slow_maintenance_does_not_block_telegram_polling(self) -> None:
        folder, state = self.make_state()
        self.addCleanup(folder.cleanup)
        maintenance_started = threading.Event()
        release_maintenance = threading.Event()
        telegram_polled = threading.Event()

        class ResponsiveBot(RecordingBot):
            def setup(self) -> None:
                return None

            def maintenance_loop(self) -> None:
                maintenance_started.set()
                release_maintenance.wait(1)

            def call(self, method: str, **payload: Any) -> Any:
                if method == "getUpdates":
                    maintenance_started.wait(1)
                    telegram_polled.set()
                    STOP.set()
                    return []
                raise AssertionError(method)

        STOP.clear()
        try:
            ResponsiveBot(state).run()
        finally:
            release_maintenance.set()
            STOP.clear()
        self.assertTrue(maintenance_started.is_set())
        self.assertTrue(telegram_polled.is_set())

    def test_home_keeps_all_global_actions_in_message(self) -> None:
        folder, state = self.make_state()
        self.addCleanup(folder.cleanup)
        bot = RecordingBot(state)
        bot.inline_enabled = True
        bot.catalog = object()  # type: ignore[assignment]
        bot.show_home(123)
        buttons = [button for row in bot.messages[-1][2] for button in row]
        labels = [button["text"] for button in buttons]
        self.assertIn("🔎 Поиск", labels)
        self.assertIn("📥 Мой TorBox", labels)
        search = next(button for button in buttons if button["text"] == "🔎 Поиск")
        self.assertEqual(search["switch_inline_query_current_chat"], "")

    def test_search_does_not_return_to_bottom_keyboard(self) -> None:
        folder, state = self.make_state()
        self.addCleanup(folder.cleanup)
        bot = RecordingBot(state)
        bot.inline_enabled = True
        bot.catalog = object()  # type: ignore[assignment]
        self.assertEqual(bot.bottom_keyboard_markup(), {"remove_keyboard": True})

    def test_bottom_search_uses_bot_owned_catalog_screen_and_removes_user_messages(self) -> None:
        folder, state = self.make_state()
        self.addCleanup(folder.cleanup)
        title = CatalogTitle(
            123, "Троя", "Troy", "FILM", "2004", "8.1",
            ("США",), ("боевик", "драма"), "https://images.example/troy.jpg",
        )

        class StaticCatalog:
            def search(self, query: str) -> list[CatalogTitle]:
                return [title]

        bot = RecordingBot(state)
        bot.catalog = StaticCatalog()  # type: ignore[assignment]
        bot.handle({
            "message": {
                "message_id": 10,
                "text": "🔎",
                "chat": {"id": 123},
                "from": {"id": 123},
            }
        })
        self.assertEqual(state.mode(123), "catalog_search")
        self.assertEqual(bot.deleted_messages, [(123, 10)])
        self.assertIn("Что найти", bot.messages[0][1])

        bot.handle({
            "message": {
                "message_id": 11,
                "text": "Троя",
                "chat": {"id": 123},
                "from": {"id": 123},
            }
        })
        self.assertEqual(bot.deleted_messages, [(123, 10), (123, 11)])
        self.assertEqual(len(bot.messages), 1)
        self.assertIn("Выбери фильм", bot.messages[0][1])
        self.assertEqual(bot.messages[0][2][0][0]["callback_data"], "catalog:kp_123")

        bot.callback({
            "id": "cb-pick-catalog",
            "data": "catalog:kp_123",
            "message": {"message_id": 1, "chat": {"id": 123}},
            "from": {"id": 123},
        })
        self.assertEqual(len(bot.messages), 1)
        self.assertIn("Нашёл", bot.messages[0][1])

    def test_search_offers_details_and_direct_add_per_result(self) -> None:
        folder, state = self.make_state()
        self.addCleanup(folder.cleanup)
        bot = RecordingBot(state)
        bot.show_search(123, "Сериал")
        keyboard = bot.messages[-1][2]
        callbacks = [button.get("callback_data") for row in keyboard for button in row]
        self.assertIn("info:0", callbacks)
        self.assertIn("add:0", callbacks)
        self.assertNotIn("queue:0", callbacks)

    def test_search_loading_is_replaced_instead_of_creating_two_messages(self) -> None:
        folder, state = self.make_state()
        self.addCleanup(folder.cleanup)
        bot = RecordingBot(state)
        bot.show_search(123, "Сериал")
        self.assertEqual(len(bot.messages), 1)
        self.assertIn("Нашёл", bot.messages[0][1])

    def test_callback_navigation_edits_one_screen(self) -> None:
        folder, state = self.make_state()
        self.addCleanup(folder.cleanup)
        bot = RecordingBot(state)
        bot.show_home(123)
        bot.callback({
            "id": "cb-screen",
            "data": "list:0",
            "message": {"message_id": 1, "chat": {"id": 123}},
            "from": {"id": 123},
        })
        self.assertEqual(len(bot.messages), 1)
        self.assertIn("TorBox пока пуст", bot.messages[0][1])

    def test_add_button_starts_add_without_second_confirmation(self) -> None:
        folder, state = self.make_state()
        self.addCleanup(folder.cleanup)
        torbox = FakeTorBox()
        bot = RecordingBot(state, torbox)
        state.put_search(123, [release()], query="Тест")
        bot._send_new(123, "results", [])
        bot.callback({
            "id": "cb-add",
            "data": "add:0",
            "message": {"message_id": 1, "chat": {"id": 123}},
            "from": {"id": 123},
        })
        self.assertEqual(torbox.added, 1)
        self.assertEqual(len(bot.messages), 1)
        self.assertIn("Добавлено в TorBox", bot.messages[0][1])

    def test_details_can_return_to_cached_results_on_same_screen(self) -> None:
        folder, state = self.make_state()
        self.addCleanup(folder.cleanup)
        bot = RecordingBot(state)
        state.put_search(123, [release("Сериал 2025 BDRip 1080p")], query="Сериал 2025")
        bot._send_new(123, "results", [])
        callback = {"message": {"message_id": 1, "chat": {"id": 123}}, "from": {"id": 123}}
        bot.callback({**callback, "id": "cb-info", "data": "info:0"})
        self.assertIn("← К результатам", str(bot.messages[0][2]))
        bot.callback({**callback, "id": "cb-back", "data": "results"})
        self.assertEqual(len(bot.messages), 1)
        self.assertIn("Сериал 2025", bot.messages[0][1])

    def test_details_highlight_voiceover_and_quality(self) -> None:
        folder, state = self.make_state()
        self.addCleanup(folder.cleanup)
        bot = RecordingBot(state)
        item = Release(**{**release("Сериал BDRip H.265 1080p LostFilm").to_dict(), "provider": "nnm"})
        state.put_search(123, [item])
        bot.callback({"id": "cb", "data": "info:0", "message": {"chat": {"id": 123}}})
        output = bot.messages[-1][1]
        self.assertIn("Озвучка:</b> официальный &lt;дубляж&gt;", output)
        self.assertIn("Качество:</b> BDRip · 1080p · H.265 / HEVC", output)

    def test_selected_inline_card_is_not_parsed_as_a_new_search(self) -> None:
        folder, state = self.make_state()
        self.addCleanup(folder.cleanup)
        bot = RecordingBot(state)
        bot.handle({
            "message": {
                "text": "🎬 Очень длинная карточка фильма " * 20,
                "chat": {"id": 123},
                "from": {"id": 123},
                "via_bot": {"id": 777, "username": "tor_boxai_bot"},
            }
        })
        self.assertEqual(bot.messages, [])

    def test_confirmation_does_not_require_live_search_cache(self) -> None:
        folder, state = self.make_state()
        self.addCleanup(folder.cleanup)
        torbox = FakeTorBox()
        bot = RecordingBot(state, torbox)
        state.put_pending(123, release())
        # Regression: old code looked up search index 0 before processing confirm.
        bot.callback({"id": "cb", "data": "confirm", "message": {"chat": {"id": 123}}})
        self.assertEqual(torbox.added, 1)
        self.assertTrue(any("Добавлено в TorBox" in text for _, text, _ in bot.messages))

    def test_duplicate_hash_opens_existing_torrent_instead_of_adding(self) -> None:
        folder, state = self.make_state()
        self.addCleanup(folder.cleanup)
        torbox = FakeTorBox()
        torbox.items = [{"id": 91, "hash": "ABC123", "name": "Уже добавлено", "download_state": "cached"}]
        bot = RecordingBot(state, torbox)
        item = Release(**{**release().to_dict(), "info_hash": "abc123", "magnet_url": "magnet:?xt=urn:btih:abc123"})
        state.put_pending(123, item)
        bot.confirm_add(123)
        self.assertEqual(torbox.added, 0)
        self.assertIn("уже есть", bot.messages[-1][1])
        self.assertEqual(bot.messages[-1][2][0][0]["callback_data"], "tinfo:91")

    def test_failed_add_keeps_confirmation_for_retry(self) -> None:
        class FailingTorBox(FakeTorBox):
            def add_magnet(self, magnet_url: str, item: Release) -> dict[str, Any]:
                raise requests.ConnectionError("temporary")

        folder, state = self.make_state()
        self.addCleanup(folder.cleanup)
        bot = RecordingBot(state, FailingTorBox())
        item = Release(**{**release().to_dict(), "magnet_url": "magnet:?xt=urn:btih:ABC123"})
        state.put_pending(123, item)
        with self.assertLogs("torbox_tg_bot", level="ERROR"):
            bot.confirm_add(123)
        self.assertEqual(bot.messages[-1][2][0][0]["callback_data"], "confirm")
        self.assertIsNotNone(state.pop_pending(123))

    def test_saved_http_magnet_proxy_falls_back_to_torrent_file(self) -> None:
        folder, state = self.make_state()
        self.addCleanup(folder.cleanup)
        torbox = FakeTorBox()
        bot = RecordingBot(state, torbox)
        item = Release(**{
            **release().to_dict(),
            "provider": "prowlarr",
            "magnet_url": "http://127.0.0.1:9696/1/download?link=magnet-redirect",
            "download_url": "http://127.0.0.1:9696/1/download?file=release.torrent",
        })
        state.put_pending(123, item)
        bot.confirm_add(123)
        self.assertEqual(torbox.added, 1)
        self.assertEqual(torbox.magnets, [])
        self.assertIn("Добавлено в TorBox", bot.messages[-1][1])

    def test_invalid_source_link_does_not_offer_endless_retry(self) -> None:
        folder, state = self.make_state()
        self.addCleanup(folder.cleanup)
        bot = RecordingBot(state)
        item = Release(**{
            **release().to_dict(),
            "magnet_url": "http://tracker.invalid/not-a-magnet",
            "download_url": "",
        })
        state.put_pending(123, item)
        with self.assertLogs("torbox_tg_bot", level="WARNING"):
            bot.confirm_add(123)
        keyboard = bot.messages[-1][2]
        self.assertFalse(keyboard)
        self.assertIsNone(state.pop_pending(123))

    def test_telegram_confirmation_failure_does_not_offer_duplicate_retry(self) -> None:
        class ConfirmationFailingBot(RecordingBot):
            def send(self, chat_id: int, text: str, keyboard: Any = None) -> None:
                if "Добавлено в TorBox" in text:
                    raise requests.ConnectionError("telegram unavailable")
                super().send(chat_id, text, keyboard)

        folder, state = self.make_state()
        self.addCleanup(folder.cleanup)
        torbox = FakeTorBox()
        bot = ConfirmationFailingBot(state, torbox)
        item = Release(**{
            **release().to_dict(),
            "info_hash": "abc123",
            "magnet_url": "magnet:?xt=urn:btih:ABC123",
        })
        state.put_pending(123, item)
        with self.assertLogs("torbox_tg_bot", level="WARNING"):
            bot.confirm_add(123)
        self.assertEqual(torbox.added, 1)
        self.assertIsNone(state.pop_pending(123))
        self.assertFalse(any(
            keyboard and keyboard[0][0].get("callback_data") == "confirm"
            for _, _, keyboard in bot.messages
        ))

    def test_stalled_torrent_alert_is_sent_once_after_grace_period(self) -> None:
        folder, state = self.make_state()
        self.addCleanup(folder.cleanup)
        torbox = FakeTorBox()
        torbox.items = [{
            "id": 92, "name": "Нет сидов", "download_state": "stalled (no seeds)",
            "download_finished": False, "cached": False,
        }]
        state.data["stalled_downloads"] = {"92": int(time.time()) - 301}
        state.save()
        bot = RecordingBot(state, torbox)
        bot.poll_torrent_health(force=True)
        self.assertEqual(len(bot.messages), 1)
        self.assertIn("Загрузка не началась", bot.messages[0][1])
        bot.poll_torrent_health(force=True)
        self.assertEqual(len(bot.messages), 1)

    def test_delete_requires_confirmation_and_uses_exact_id(self) -> None:
        folder, state = self.make_state()
        self.addCleanup(folder.cleanup)
        torbox = FakeTorBox()
        torbox.items = [{"id": 9, "name": "Old", "size": 10, "download_state": "cached"}]
        bot = RecordingBot(state, torbox)
        bot.callback({"id": "cb1", "data": "delask:9", "message": {"chat": {"id": 123}}})
        self.assertEqual(torbox.operations, [])
        bot.callback({"id": "cb2", "data": "delconfirm:9", "message": {"chat": {"id": 123}}})
        self.assertEqual(torbox.operations, [(9, "delete")])

    def test_notification_poll_seeds_history_then_sends_only_new_items(self) -> None:
        folder, state = self.make_state()
        self.addCleanup(folder.cleanup)
        torbox = FakeTorBox()
        torbox.notification_items = [{"id": 1, "title": "Старое", "message": "До запуска"}]
        bot = RecordingBot(state, torbox)
        bot.poll_notifications(force=True)
        self.assertEqual(bot.messages, [])
        torbox.notification_items.append({"id": 2, "title": "Готово", "message": "Загрузка завершена"})
        bot.poll_notifications(force=True)
        self.assertEqual(len(bot.messages), 1)
        self.assertIn("Загрузка завершена", bot.messages[0][1])
        bot.poll_notifications(force=True)
        self.assertEqual(len(bot.messages), 1)


@unittest.skipUnless(os.getenv("RUN_LIVE_TESTS") == "1", "set RUN_LIVE_TESTS=1 for live NNM smoke test")
class NnmSmokeTests(unittest.TestCase):
    def test_search_and_parse_one_torrent(self) -> None:
        client = SearchAggregator([NnmClient()])
        releases = client.search("Интерстеллар 2014")
        self.assertGreater(len(releases), 0)
        first = next(item for item in releases if item.size_bytes and item.seeders > 0)
        torrent = client.download_torrent(first)
        self.assertGreater(len(torrent), 100)
        self.assertEqual(len(torrent_fingerprint(torrent)), 40)


if __name__ == "__main__":
    unittest.main(verbosity=2)
