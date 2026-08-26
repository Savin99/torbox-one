"""HTTP-мост между iOS-приложением TorBox One и поисковой логикой бота.

Отдаёт каталог Кинопоиска, поиск по трекерам с рейтингом и `.torrent`-файлы.
Управление TorBox сюда не входит: приложение ходит в TorBox API напрямую,
и ключ TorBox этому серверу не передаётся.
"""

from __future__ import annotations

import dataclasses
import logging
import os
import threading
import time
import uuid
from typing import Any

# Логгер настраивается до импорта бота: torbox_tg_bot вызывает
# logging.basicConfig на импорте и иначе перехватил бы конфигурацию uvicorn.
logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)

from fastapi import Depends, FastAPI, Header, HTTPException, Response

import meta_service
import torbox_tg_bot as bot

LOG = logging.getLogger("search_api")

API_VERSION = "1.0"
API_TOKEN = os.getenv("SEARCH_API_TOKEN", "").strip()
RESULTS_TTL_SECONDS = 3600
MAX_APP_RESULTS = bot.MAX_RESULTS


class ReleaseCache:
    """Потокобезопасный кэш найденных релизов с TTL, как часовой кэш в боте."""

    def __init__(self, ttl_seconds: int = RESULTS_TTL_SECONDS) -> None:
        self._ttl = ttl_seconds
        self._lock = threading.Lock()
        self._items: dict[str, tuple[float, bot.Release]] = {}

    def put_all(self, releases: list[bot.Release]) -> list[tuple[str, bot.Release]]:
        now = time.monotonic()
        with self._lock:
            self._prune(now)
            stored: list[tuple[str, bot.Release]] = []
            for release in releases:
                release_id = uuid.uuid4().hex
                self._items[release_id] = (now, release)
                stored.append((release_id, release))
            return stored

    def get(self, release_id: str) -> bot.Release | None:
        now = time.monotonic()
        with self._lock:
            self._prune(now)
            entry = self._items.get(release_id)
            return entry[1] if entry else None

    def _prune(self, now: float) -> None:
        expired = [
            key for key, (stamp, _) in self._items.items() if now - stamp > self._ttl
        ]
        for key in expired:
            del self._items[key]


def build_providers() -> list[bot.SearchProvider]:
    """Собирает поисковые источники из тех же env-переменных, что и бот."""
    providers: list[bot.SearchProvider] = []
    if bot.env_bool("NNM_ENABLED", True):
        providers.append(bot.NnmClient())
    prowlarr_url = os.getenv("PROWLARR_URL", "").strip()
    prowlarr_key = os.getenv("PROWLARR_API_KEY", "").strip()
    if prowlarr_url and prowlarr_key:
        providers.append(
            bot.ProwlarrClient(
                prowlarr_url, prowlarr_key, bot.env_int_tuple("PROWLARR_INDEXER_IDS")
            )
        )
    return providers


def build_catalog() -> bot.KinopoiskCatalog | None:
    key = os.getenv("KINOPOISK_API_KEY", "").strip()
    return bot.KinopoiskCatalog(key) if key else None


def catalog_payload(title: bot.CatalogTitle) -> dict[str, Any]:
    return {
        "catalog_id": title.catalog_id,
        "name": title.name,
        "original_name": title.original_name,
        "media_type": title.media_type,
        "year": title.year,
        "rating": title.rating,
        "countries": list(title.countries),
        "genres": list(title.genres),
        "poster_url": title.poster_url,
        "search_query": title.search_query,
    }


def release_payload(release_id: str, release: bot.Release) -> dict[str, Any]:
    return {
        "id": release_id,
        "title": release.title,
        "size_text": release.size_text,
        "size_bytes": release.size_bytes,
        "seeders": release.seeders,
        "leechers": release.leechers,
        "source_name": release.source_name,
        "category": release.category,
        "magnet_url": release.magnet_url,
        "info_hash": release.info_hash,
        "quality_summary": bot.video_quality_summary(release),
        "voiceover_summary": bot.voiceover_summary(release),
        "source_url": release.source_url,
    }


META = meta_service.build_meta_service()
PROVIDERS = build_providers()
PROVIDER_BY_KEY = {provider.key: provider for provider in PROVIDERS}
CATALOG = build_catalog()
CACHE = ReleaseCache()

app = FastAPI(title="TorBox One Search API", version=API_VERSION)


def require_token(authorization: str = Header(default="")) -> None:
    """Статический bearer-токен; без SEARCH_API_TOKEN авторизация выключена (dev-режим)."""
    if not API_TOKEN:
        return
    if authorization != f"Bearer {API_TOKEN}":
        raise HTTPException(status_code=401, detail="Неверный токен")


def get_cached_release(release_id: str) -> bot.Release:
    release = CACHE.get(release_id)
    if release is None:
        raise HTTPException(
            status_code=404, detail="Результат поиска устарел, повтори поиск"
        )
    return release


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok", "version": API_VERSION}


@app.get("/catalog/popular", dependencies=[Depends(require_token)])
def catalog_popular() -> list[dict[str, Any]]:
    if CATALOG is None:
        raise HTTPException(status_code=503, detail="KINOPOISK_API_KEY не настроен")
    try:
        return [catalog_payload(title) for title in CATALOG.popular()]
    except Exception as exc:
        raise HTTPException(
            status_code=502, detail=f"Каталог недоступен: {exc}"
        ) from exc


@app.get("/catalog/search", dependencies=[Depends(require_token)])
def catalog_search(q: str) -> list[dict[str, Any]]:
    query = q.strip()
    if not query:
        return []
    if CATALOG is None:
        raise HTTPException(status_code=503, detail="KINOPOISK_API_KEY не настроен")
    try:
        return [catalog_payload(title) for title in CATALOG.search(query)]
    except Exception as exc:
        raise HTTPException(
            status_code=502, detail=f"Каталог недоступен: {exc}"
        ) from exc


@app.get("/search", dependencies=[Depends(require_token)])
def tracker_search(q: str) -> dict[str, Any]:
    query = q.strip()[: bot.MAX_QUERY_LENGTH]
    if len(query) < 2:
        raise HTTPException(status_code=400, detail="Запрос слишком короткий")
    if not PROVIDERS:
        raise HTTPException(
            status_code=503, detail="Ни один поисковый источник не настроен"
        )
    # Aggregator одноразовый: его last_status перезаписывается на каждый вызов
    aggregator = bot.SearchAggregator(list(PROVIDERS))
    releases = aggregator.search(query)
    top = bot.select_diverse_releases(releases, MAX_APP_RESULTS)
    stored = CACHE.put_all(top)
    LOG.info(
        "Поиск «%s»: %d релизов, статус %s", query, len(stored), aggregator.last_status
    )
    return {
        "status": dict(aggregator.last_status),
        "results": [
            release_payload(release_id, release) for release_id, release in stored
        ],
    }


@app.get("/meta/match", dependencies=[Depends(require_token)])
def meta_match(name: str) -> dict[str, Any]:
    """Карточка тайтла по имени раздачи: постер, обложка, описание, состав."""
    if not META.available:
        raise HTTPException(status_code=503, detail="KINOPOISK_API_KEY не настроен")
    query = name.strip()
    if len(query) < 2:
        raise HTTPException(status_code=400, detail="Слишком короткое имя")
    try:
        return dataclasses.asdict(META.match(query))
    except Exception as exc:
        raise HTTPException(
            status_code=502, detail=f"Каталог метаданных недоступен: {exc}"
        ) from exc


@app.get("/meta/{film_id}/episodes", dependencies=[Depends(require_token)])
def meta_episodes(film_id: int, season: int | None = None) -> list[dict[str, Any]]:
    """Серии тайтла: русские названия, описания и кадры (если настроен TMDB)."""
    if not META.available:
        raise HTTPException(status_code=503, detail="KINOPOISK_API_KEY не настроен")
    try:
        return [dataclasses.asdict(item) for item in META.episodes(film_id, season)]
    except Exception as exc:
        raise HTTPException(
            status_code=502, detail=f"Каталог метаданных недоступен: {exc}"
        ) from exc


@app.get("/release/{release_id}/details", dependencies=[Depends(require_token)])
def release_details(release_id: str) -> dict[str, str]:
    release = get_cached_release(release_id)
    provider = PROVIDER_BY_KEY.get(release.provider)
    details = ""
    if provider is not None:
        try:
            details = provider.topic_summary(release)
        except Exception:
            LOG.warning(
                "Не получилось прочитать тему релиза %s",
                release.source_url,
                exc_info=True,
            )
    return {
        "details_text": details,
        "quality_summary": bot.video_quality_summary(release, details),
        "voiceover_summary": bot.voiceover_summary(release, details),
        "audio_summary": bot.audio_track_summary(details),
    }


@app.get("/release/{release_id}/torrent", dependencies=[Depends(require_token)])
def release_torrent(release_id: str) -> Response:
    release = get_cached_release(release_id)
    provider = PROVIDER_BY_KEY.get(release.provider)
    if provider is None:
        raise HTTPException(
            status_code=404, detail="Источник релиза больше не настроен"
        )
    try:
        payload = provider.download_torrent(release)
    except Exception as exc:
        raise HTTPException(
            status_code=502, detail=f"Не получилось скачать .torrent: {exc}"
        ) from exc
    return Response(content=payload, media_type="application/x-bittorrent")
