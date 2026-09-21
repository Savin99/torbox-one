#!/usr/bin/env python3
"""Telegram bot: aggregate torrent search and use each user's TorBox account.

The bot uses Telegram long-polling, so it needs no public HTTP port. The owner
can keep using TORBOX_API_KEY while other users connect their own keys in a
private chat with the bot.
"""
from __future__ import annotations

import hashlib
import html
import json
import logging
import os
import re
import signal
import sys
import time
from collections import OrderedDict
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, replace
from pathlib import Path
from threading import Event, Lock, RLock, Thread, get_ident, local
from typing import Any, Protocol
from urllib.parse import parse_qs, quote_plus, urljoin, urlparse
from weakref import WeakValueDictionary

import bencodepy
import cloudscraper
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from torbox_runtime import (
    PRIVATE_LOG, OutboundThrottle, RateLimiter, ThreadLocalHTTP, UserDispatcher,
    configure_private_logging, write_private_json,
)

APP_DIR = Path(__file__).resolve().parent
STATE_PATH = Path(os.getenv("STATE_PATH", str(APP_DIR / "state.json")))
CREDENTIALS_PATH = Path(os.getenv("CREDENTIALS_PATH", str(APP_DIR / "credentials.json")))
NNM_BASE = "https://nnmclub.to/forum/"
TORBOX_API = "https://api.torbox.app/v1/api"
SETUP_GUIDE_URL = "https://telegra.ph/TorBox--Infuse-nastrojka-s-nulya-09-13"
TELEGRAM_API = "https://api.telegram.org/bot{token}/{method}"
MAX_RESULTS = 8
MAX_SEARCH_CANDIDATES = 24
MAX_PROVIDER_RESULTS = 80
TORBOX_PAGE_SIZE = 6
SEARCH_PAGE_SIZE = 3
FILE_PAGE_SIZE = 6
MAX_QUERY_LENGTH = 120
MAX_TORRENT_BYTES = 4 * 1024 * 1024
MAX_SEARCH_CACHE = 64
USER_AGENT = "Mozilla/5.0 (compatible; TorBoxPrivateTelegramBot/1.0)"

BTN_SEARCH = "🔎 Поиск"
BTN_TORRENTS = "📥 Мой TorBox"
BTN_NOTIFICATIONS = "🔔 Уведомления"
BTN_HELP = "ℹ️ Помощь"
BTN_ACCOUNT = "🔑 TorBox-аккаунт"
BTN_HOME = "🏠 Меню"
BOTTOM_SEARCH = "🔎 Поиск"
BOTTOM_TORRENTS = "📥 TorBox"
BOTTOM_NOTIFICATIONS = "🔔 Уведомления"
BOTTOM_HELP = "ℹ️ Помощь"
BOTTOM_ACCOUNT = "🔑 Аккаунт"
BOTTOM_HOME = "🏠 Меню"
BOTTOM_ACTIONS = {
    BOTTOM_SEARCH: BTN_SEARCH,
    BOTTOM_TORRENTS: BTN_TORRENTS,
    BOTTOM_NOTIFICATIONS: BTN_NOTIFICATIONS,
    BOTTOM_HELP: BTN_HELP,
    BOTTOM_ACCOUNT: BTN_ACCOUNT,
    BOTTOM_HOME: BTN_HOME,
    # Accept taps from an older keyboard while Telegram is replacing it.
    "🔎": BTN_SEARCH,
    "📥": BTN_TORRENTS,
    "🔔": BTN_NOTIFICATIONS,
    "ℹ️": BTN_HELP,
    "🏠": BTN_HOME,
}
BOTTOM_KEYBOARD_VERSION = 7

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(message)s")
configure_private_logging()
LOG = logging.getLogger("torbox_tg_bot")
STOP = Event()


def configure_read_retries(session: requests.Session) -> None:
    """Retry transient read failures without replaying mutations."""
    retry = Retry(
        total=1,
        connect=1,
        read=0,
        status=1,
        backoff_factor=0.4,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset({"GET", "HEAD", "OPTIONS"}),
        respect_retry_after_header=False,
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retry, pool_connections=8, pool_maxsize=8)
    session.mount("https://", adapter)
    session.mount("http://", adapter)


def http_session() -> requests.Session:
    session = requests.Session()
    configure_read_retries(session)
    return session


def scraper_session() -> requests.Session:
    session = cloudscraper.create_scraper()
    configure_read_retries(session)
    return session


def public_error(exc: Exception) -> str:
    if isinstance(exc, requests.RequestException):
        return "Сервис временно не отвечает. Попробуй ещё раз через минуту."
    if str(exc).startswith("Сначала подключи"):
        return "Сначала подключи свой TorBox через /connect."
    return "Не удалось выполнить запрос. Проверь /status и попробуй ещё раз."


def torrent_response_bytes(response: requests.Response) -> bytes:
    try:
        response.raise_for_status()
        content = bytearray()
        for chunk in response.iter_content(16 * 1024):
            if len(content) + len(chunk) > MAX_TORRENT_BYTES:
                raise RuntimeError("Torrent file is unexpectedly large")
            content.extend(chunk)
        return bytes(content)
    finally:
        response.close()

QUALITY_POSITIVE = {
    "дублированный": 50, "официальный": 35, "lostfilm": 30,
    "alexfilm": 25, "амедиа": 25, "kravec": 22, "кравец": 22,
    "лицензи": 28, "itunes": 12,
    "h.265": 15, "hevc": 15, "1080p": 12, "2160p": 10,
    "hdr10": 6, "imax": 4, "bdremux": 15, "bd" : 8,
}
QUALITY_NEGATIVE = {
    "одноголос": -45, "двухголос": -35, "любительск": -35,
    "line": -45, "mic": -45, "camrip": -70, "ts": -55,
    "handmade": -20, "upscale ai": -25,
    # A well-seeded LQ rip should never eclipse a proper 1080p/HEVC release.
    "lq": -80,
}


@dataclass(frozen=True)
class Release:
    torrent_id: str
    topic_id: str
    title: str
    category: str
    size_text: str
    size_bytes: int
    seeders: int
    leechers: int
    source_url: str
    download_url: str
    score: int
    provider: str = "nnm"
    source_name: str = "NNM-Club"
    info_hash: str = ""
    magnet_url: str = ""
    details_text: str = ""
    cached: bool | None = None

    def to_dict(self) -> dict[str, Any]:
        return self.__dict__.copy()

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> Release:
        return cls(**raw)


@dataclass(frozen=True)
class CatalogTitle:
    catalog_id: int
    name: str
    original_name: str
    media_type: str
    year: str
    rating: str
    countries: tuple[str, ...]
    genres: tuple[str, ...]
    poster_url: str

    @property
    def search_query(self) -> str:
        return self.name if self.media_type.upper() in {"TV_SERIES", "MINI_SERIES", "SERIES", "TV_SHOW"} else " ".join(part for part in (self.name, self.year) if part).strip()


class KinopoiskCatalog(ThreadLocalHTTP):
    """Optional Russian-language movie catalog used by Telegram inline mode."""

    API_URL = "https://kinopoiskapiunofficial.tech/api/v2.1/films/search-by-keyword"
    POPULAR_URL = "https://kinopoiskapiunofficial.tech/api/v2.2/films/collections"

    def __init__(self, api_key: str) -> None:
        PRIVATE_LOG.add_secret(api_key)
        self.init_http(http_session, {"X-API-KEY": api_key.strip(), "Accept": "application/json", "User-Agent": USER_AGENT})

    def search(self, query: str) -> list[CatalogTitle]:
        response = self.http.get(self.API_URL, params={"keyword": query, "page": 1}, timeout=(5, 15), allow_redirects=False)
        response.raise_for_status()
        payload = response.json()
        films = payload.get("films") if isinstance(payload, dict) else None
        if not isinstance(films, list):
            raise RuntimeError("Каталог фильмов вернул неожиданный ответ")
        return self._titles_from_items(films)

    def popular(self) -> list[CatalogTitle]:
        response = self.http.get(
            self.POPULAR_URL,
            params={"type": "TOP_POPULAR_ALL", "page": 1},
            timeout=15,
            allow_redirects=False,
        )
        response.raise_for_status()
        payload = response.json()
        items = payload.get("items") if isinstance(payload, dict) else None
        if not isinstance(items, list):
            raise RuntimeError("Каталог популярных фильмов вернул неожиданный ответ")
        return self._titles_from_items(items)

    @staticmethod
    def _titles_from_items(items: list[Any]) -> list[CatalogTitle]:
        results: list[CatalogTitle] = []
        for item in items[:20]:
            if not isinstance(item, dict):
                continue
            catalog_id = item.get("filmId") or item.get("kinopoiskId")
            if not catalog_id:
                continue
            name = str(item.get("nameRu") or item.get("nameEn") or item.get("nameOriginal") or "Без названия").strip()
            original_name = str(item.get("nameEn") or item.get("nameOriginal") or "").strip()
            countries = tuple(
                str(country.get("country"))
                for country in item.get("countries", [])
                if isinstance(country, dict) and country.get("country")
            )
            genres = tuple(
                str(genre.get("genre"))
                for genre in item.get("genres", [])
                if isinstance(genre, dict) and genre.get("genre")
            )
            poster_url = str(item.get("posterUrlPreview") or item.get("posterUrl") or "").strip()
            if poster_url.startswith("http://"):
                poster_url = "https://" + poster_url.removeprefix("http://")
            results.append(CatalogTitle(
                catalog_id=int(catalog_id),
                name=name,
                original_name=original_name if original_name.casefold() != name.casefold() else "",
                media_type=str(item.get("type") or "UNKNOWN"),
                year=str(item.get("year") or "").strip(),
                rating=str(item.get("rating") or item.get("ratingKinopoisk") or "").strip(),
                countries=countries,
                genres=genres,
                poster_url=poster_url,
            ))
        return results


class SearchProvider(Protocol):
    key: str
    name: str

    def search(self, query: str) -> list[Release]: ...

    def topic_summary(self, release: Release) -> str: ...

    def download_torrent(self, release: Release) -> bytes: ...


def env_required(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


def env_bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    normalized = value.strip().casefold()
    if normalized in {"1", "true", "yes", "on", "да"}:
        return True
    if normalized in {"0", "false", "no", "off", "нет"}:
        return False
    raise RuntimeError(f"Invalid boolean environment variable: {name}")


def env_int(name: str, default: int, minimum: int = 1) -> int:
    raw = os.getenv(name, str(default)).strip()
    try:
        value = int(raw)
    except ValueError as exc:
        raise RuntimeError(f"Invalid integer environment variable: {name}") from exc
    if value < minimum:
        raise RuntimeError(f"{name} must be at least {minimum}")
    return value


def env_int_tuple(name: str) -> tuple[int, ...]:
    raw = os.getenv(name, "").strip()
    if not raw:
        return ()
    try:
        values = tuple(dict.fromkeys(int(part.strip()) for part in raw.split(",") if part.strip()))
    except ValueError as exc:
        raise RuntimeError(f"Invalid comma-separated integer list: {name}") from exc
    if any(value < 1 for value in values):
        raise RuntimeError(f"{name} values must be positive")
    return values


def human_size(value: int) -> str:
    units = ("B", "KB", "MB", "GB", "TB")
    size = float(value)
    for unit in units:
        if size < 1024 or unit == units[-1]:
            return f"{size:.1f} {unit}" if unit != "B" else f"{int(size)} B"
        size /= 1024
    return f"{value} B"


def human_duration(seconds: int) -> str:
    if seconds <= 0:
        return "—"
    minutes = seconds // 60
    hours, minutes = divmod(minutes, 60)
    days, hours = divmod(hours, 24)
    if days:
        return f"{days} д {hours} ч"
    if hours:
        return f"{hours} ч {minutes} мин"
    return f"{max(1, minutes)} мин"


def progress_percent(raw: Any) -> float:
    try:
        value = max(0.0, float(raw))
    except (TypeError, ValueError):
        return 0.0
    # qBittorrent (the source of this field) uses a 0..1 fraction.
    if value <= 1:
        value *= 100
    return min(value, 100.0)


def status_label(item: dict[str, Any]) -> tuple[str, str]:
    state = str(item.get("download_state") or "unknown").strip().casefold()
    if item.get("download_finished") and item.get("download_present") is False:
        return "⏳", "подготовка файлов"
    if item.get("download_finished") or item.get("cached") or state in {"cached", "completed", "uploading"}:
        return "✅", "готово"
    if "error" in state or "missing" in state or "no seeds" in state or "stalled" in state:
        return "⚠️", "нужна проверка"
    if "pause" in state:
        return "⏸", "приостановлено"
    if state in {"metadl", "checkingresumedata", "queued", "checking"}:
        return "⏳", "подготовка"
    if item.get("active") or "down" in state:
        return "⬇️", "скачивается"
    return "⏳", state or "ожидание"


def torrent_ready(item: dict[str, Any]) -> bool:
    if "download_present" in item:
        return item["download_present"] is True
    return status_label(item)[0] == "✅"


def natural_file_key(name: str) -> tuple[Any, ...]:
    return tuple(int(part) if part.isdigit() else part.casefold() for part in re.split(r"(\d+)", name))


def safe_torrent_id(raw: Any) -> int:
    try:
        value = int(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError("Некорректный ID загрузки") from exc
    if value < 0:
        raise ValueError("Некорректный ID загрузки")
    return value


def is_magnet_url(value: str) -> bool:
    """Accept only actual magnet URIs, not Prowlarr's HTTP redirect wrappers."""
    return value.strip().casefold().startswith("magnet:?")


def parse_size(size_text: str) -> int:
    match = re.search(r"([\d.,]+)\s*(KB|MB|GB|TB)", size_text, re.I)
    if not match:
        return 0
    amount = float(match.group(1).replace(",", "."))
    multipliers = {"KB": 1024, "MB": 1024**2, "GB": 1024**3, "TB": 1024**4}
    return int(amount * multipliers[match.group(2).upper()])


def score_release(title: str, seeders: int, leechers: int) -> int:
    """Prefer playable, seeded Russian releases. Score is explainable, not magic."""
    text = title.casefold()
    score = min(seeders, 100) * 2 - min(leechers, 30)
    # A premium-looking remux is not useful when TorBox cannot fetch it. Cache
    # availability is not known for every tracker result, so keep low-seed
    # releases visible but never let them dominate healthy alternatives.
    if seeders <= 0:
        score -= 80
    elif seeders == 1:
        score -= 30
    elif seeders <= 3:
        score -= 10
    for needle, points in QUALITY_POSITIVE.items():
        if needle in text:
            score += points
    for needle, points in QUALITY_NEGATIVE.items():
        if needle in text:
            score += points
    return score


def source_bonus(source_name: str) -> int:
    """Small trust bonus; quality and seed health still dominate the ranking."""
    name = source_name.casefold()
    priorities = {
        "rutracker": 35,
        "kinozal": 30,
        "nnm": 25,
        "selezen": 20,
        "bigfangroup": 18,
        "megapeer": 16,
        "newstudio": 14,
        "rutor": 12,
        "byrutor": 9,
        "pc-torrent": 9,
        "torrent.by": 8,
    }
    return next((bonus for needle, bonus in priorities.items() if needle in name), 5)


def query_relevance(query: str, title: str) -> int:
    query_tokens = {token for token in re.findall(r"[\w]+", query.casefold(), re.U) if len(token) > 1}
    if not query_tokens:
        return 0
    title_tokens = set(re.findall(r"[\w]+", title.casefold(), re.U))
    ratio = len(query_tokens & title_tokens) / len(query_tokens)
    score = round(ratio * 45)
    return score - 60 if ratio < 0.5 else score


QUERY_STOP_WORDS = {
    "the", "a", "an", "of", "and", "or", "in", "on",
    "и", "или", "в", "на", "из", "с", "к",
}


def search_tokens(text: str) -> list[str]:
    normalized = text.casefold().replace("ё", "е")
    return [
        token for token in re.findall(r"[\w]+", normalized, re.U)
        if len(token) > 1 and token not in QUERY_STOP_WORDS
    ]


def token_matches(query_token: str, title_tokens: list[str]) -> bool:
    if query_token in title_tokens:
        return True
    if len(query_token) >= 6:
        stem = query_token[: max(4, len(query_token) - 2)]
        return any(token.startswith(stem) for token in title_tokens)
    return False


def title_matches_query(query: str, title: str) -> bool:
    """Reject high-seed false positives before they can enter a short result list."""
    wanted = search_tokens(query)
    if not wanted:
        return False
    available = search_tokens(title)
    return all(token_matches(token, available) for token in wanted)


def release_dedupe_key(release: Release) -> str:
    normalized_hash = re.sub(r"[^a-zA-Z0-9]", "", release.info_hash).casefold()
    if normalized_hash:
        return f"hash:{normalized_hash}"
    normalized_title = re.sub(r"[^\w]+", "", release.title.casefold(), flags=re.U)[:180]
    # Exact byte sizes differ slightly between some indexers; use a 16 MiB bucket.
    size_bucket = release.size_bytes // (16 * 1024 * 1024) if release.size_bytes else 0
    return f"title:{normalized_title}:{size_bucket}"


def normalize_info_hash(value: str) -> str:
    return re.sub(r"[^a-zA-Z0-9]", "", str(value or "")).casefold()


def cache_ranked_releases(releases: list[Release], cached_hashes: set[str]) -> list[Release]:
    """Mark cached candidates and make instant playback the strongest signal."""
    normalized_cached = {normalize_info_hash(value) for value in cached_hashes if value}
    enriched: list[Release] = []
    for release in releases:
        normalized_hash = normalize_info_hash(release.info_hash)
        cached = normalized_hash in normalized_cached if normalized_hash else None
        # Cache is a strong reliability signal, but it must not turn a 50+ GB
        # 4K remux into the default recommendation for a phone connection.
        enriched.append(replace(release, cached=cached, score=release.score + (120 if cached else 0)))
    enriched.sort(key=lambda item: (item.score, item.seeders), reverse=True)
    return select_diverse_releases(enriched, MAX_RESULTS)


def release_variant_key(release: Release) -> str:
    """Group near-identical editions without hiding genuinely different audio."""
    quality = video_quality_summary(release, release.details_text).casefold()
    voiceover = voiceover_summary(release, release.details_text).casefold()
    if voiceover == "не указана источником":
        voiceover = "unknown"
    return f"{quality}|{voiceover}"


def release_season_numbers(title: str) -> tuple[int, ...]:
    """Read explicit EN/RU season labels without mistaking episode counts for seasons."""
    seasons: set[int] = set()
    def add_numbers(value: str) -> None:
        for match in re.finditer(r"(?<!\d)(\d{1,2})(?:\s*[-–—]\s*[sS]?(\d{1,2}))?(?!\d)", value):
            first = int(match[1])
            last = int(match[2] or first)
            if 1 <= first <= last <= 99:
                seasons.update(range(first, last + 1))
    for match in re.finditer(r"(?i)\bS(\d{1,2}(?:\s*[-–—]\s*S?\d{1,2})?)(?!\d)", title):
        add_numbers(match[1])
    numbers = r"\d{1,2}(?:\s*[-–—,]\s*\d{1,2}|\s+и\s+\d{1,2})*"
    for pattern in [rf"(?i)сезон(?:ы|ов)?\s*[:№#]?\s*({numbers})(?!\d)",
                    rf"(?i)(?<!\d)({numbers})\s*(?:-?й\s+)?сезон"]:
        for match in re.finditer(pattern, title):
            add_numbers(match[1])
    return tuple(sorted(seasons))


def release_is_partial_season(title: str) -> bool:
    """Episode-only batches need explicit full coverage before being treated as a season."""
    match = re.search(
        r"(?i)(?:\bS\d{1,2}\s*E|\bE|серии?\s*[:№]?\s*)(\d{1,3})"
        r"(?:\s*[-–—]\s*E?(\d{1,3}))?\s*(?:(?:of|из|/)\s*(\d{1,3}))?", title,
    )
    if match:
        first, last = int(match[1]), int(match[2] or match[1])
        total = int(match[3]) if match[3] else None
        return not (total is not None and first == 1 and last == total)
    return bool(re.search(r"(?i)неполны[йе]|пополняем|ongoing", title))


SEARCH_DEFAULTS = {"quality": "1080", "audio": "dub", "complete": True, "fallback": True}
SEARCH_QUALITIES = {"1080": "1080p", "720": "720p", "2160": "4K", "any": "Любое качество"}
SEARCH_AUDIO = {"dub": "Дубляж", "multi": "Многоголосая", "dvo": "Двухголосая",
                "lostfilm": "LostFilm", "hdrezka": "HDRezka", "any": "Любая озвучка"}


def search_preferences(raw: Any = None) -> dict[str, Any]:
    raw = raw if isinstance(raw, dict) else {}
    return {"quality": raw.get("quality") if raw.get("quality") in SEARCH_QUALITIES else "1080",
            "audio": raw.get("audio") if raw.get("audio") in SEARCH_AUDIO else "dub",
            "complete": raw.get("complete", True) is not False,
            "fallback": raw.get("fallback", True) is not False}


def release_resolution(release: Release) -> str:
    match = re.search(r"(?i)\b(2160|1080|720)[pi]\b|\b(4k)\b", release.title + " " + release.details_text)
    return (match[1] or "2160") if match else "unknown"


def release_audio_tags(release: Release) -> set[str]:
    # A tracker's translation field is stronger evidence than the filename.
    translation = _labeled_value(release.details_text, "Перевод")
    text = translation or release.title
    tags: set[str] = set()
    no_dub = re.search(r"(?i)без\s+дубляжа|не\s+дублирован", text)
    if not no_dub and re.search(r"(?i)\bдублирован\w*|\bдублирование\b|\bдубляж\w*|\bdub(?:bed)?\b|(?:\[|\||,)\s*D(?=\s*[,|\]])", text):
        tags.add("dub")
    for tag, pattern in [("multi", r"\bMVO\b|многоголос"), ("dvo", r"\bDVO\b|двухголос")]:
        if re.search(pattern, text, re.I):
            tags.add(tag)
    for tag in ("lostfilm", "hdrezka"):
        if re.search(r"\b" + tag + r"\b", release.title + " " + text, re.I):
            tags.add(tag)
    # DUAL denotes multiple audio tracks; it is not proof of Russian dubbing.
    return tags


def release_matches_preferences(release: Release, prefs: dict[str, Any]) -> bool:
    return (not re.search(r"(?i)\b(?:camrip|hdcam|cam|telesync|tsrip|ts)\b", release.title) and
            (prefs["quality"] == "any" or release_resolution(release) == prefs["quality"]) and
            (prefs["audio"] == "any" or prefs["audio"] in release_audio_tags(release)))


def personalized_releases(releases: list[Release], prefs: dict[str, Any]) -> list[Release]:
    """Rank by explicit preferences; keep every candidate for season/alternate selection."""
    unique: dict[str, Release] = {}
    for item in releases:
        key = release_dedupe_key(item)
        if key not in unique or item.score > unique[key].score:
            unique[key] = item
    def rank(item: Release) -> tuple[Any, ...]:
        quality = release_resolution(item)
        requested = prefs["quality"]
        quality_fit = 3 if requested == "any" or quality == requested else (2 if quality == "1080" else 1 if quality in {"720", "2160"} else 0)
        audio = release_audio_tags(item)
        audio_fit = 3 if prefs["audio"] == "any" or prefs["audio"] in audio else 1 if audio else 0
        seasons = release_season_numbers(item.title)
        partial = release_is_partial_season(item.title) if seasons else False
        safe = not bool(re.search(r"(?i)\b(?:camrip|hdcam|cam|telesync|tsrip|ts)\b", item.title))
        available = item.cached is True or item.seeders > 0
        complete = not partial if prefs["complete"] else True
        coverage = len(seasons) if prefs["complete"] and not partial else 0
        # Preferences dominate seed counts and generic 4K/remux bonuses.
        return (safe, available, quality_fit, complete, audio_fit, coverage, item.cached is True,
                min(item.seeders, 100), max(-200, min(item.score, 400)), -max(item.size_bytes, 0))
    ranked = sorted(unique.values(), key=rank, reverse=True)
    if not prefs["complete"] or not ranked:
        return ranked
    # Spotlight one useful choice for each uncovered season before redundant editions.
    front = [ranked[0]]
    covered = set(release_season_numbers(ranked[0].title)) if not release_is_partial_season(ranked[0].title) else set()
    seasons = sorted({n for item in ranked for n in release_season_numbers(item.title)})
    for season in seasons:
        if season in covered:
            continue
        candidates = [r for r in ranked if season in release_season_numbers(r.title)]
        # Preserve availability and quality priorities when spotlighting a season.
        best = candidates[0]
        if best not in front:
            front.append(best)
        covered.update(release_season_numbers(best.title))
    return front + [r for r in ranked if r not in front]


def season_summary(seasons: Any) -> str:
    numbers = sorted(set(seasons))
    groups = []
    for number in numbers:
        if groups and number == groups[-1][-1] + 1:
            groups[-1].append(number)
        else:
            groups.append([number])
    return ", ".join(str(g[0]) if len(g) == 1 else f"{g[0]}–{g[-1]}" for g in groups)


def select_diverse_releases(releases: list[Release], limit: int = MAX_RESULTS) -> list[Release]:
    """Keep a short list useful, preserving season coverage before edition variety."""
    selected: list[Release] = []
    selected_ids: set[int] = set()
    variant_counts: dict[str, int] = {}
    source_counts: dict[str, int] = {}

    def append(index: int) -> None:
        release = releases[index]
        selected.append(release)
        selected_ids.add(index)
        variant = release_variant_key(release)
        source = release.source_name.casefold()
        variant_counts[variant] = variant_counts.get(variant, 0) + 1
        source_counts[source] = source_counts.get(source, 0) + 1

    coverage_by_index = {
        index: release_season_numbers(release.title)
        for index, release in enumerate(releases)
    }
    seasons = sorted({season for coverage in coverage_by_index.values() for season in coverage})
    season_aware = len(seasons) >= 2

    if season_aware:
        priority_indices: set[int] = set()

        # Keep the strongest complete multi-season pack as the simplest choice.
        pack_candidates = [
            index for index, coverage in coverage_by_index.items()
            if len(coverage) > 1 and not release_is_partial_season(releases[index].title)
        ]
        if not pack_candidates:
            pack_candidates = [index for index, coverage in coverage_by_index.items() if len(coverage) > 1]
        if pack_candidates:
            priority_indices.add(pack_candidates[0])

        # Also retain one exact result for every season. Quality/voice similarity
        # must not let S01 and S02 push S03 out of a short list.
        for season in seasons:
            exact = [
                index for index, coverage in coverage_by_index.items()
                if coverage == (season,)
            ]
            if exact:
                complete = [index for index in exact if not release_is_partial_season(releases[index].title)]
                priority_indices.add((complete or exact)[0])

        for index in sorted(priority_indices):
            if len(selected) >= limit:
                return selected
            append(index)

    for index, release in enumerate(releases):
        if index in selected_ids:
            continue
        if season_aware and release.seeders <= 0:
            continue
        variant = release_variant_key(release)
        source = release.source_name.casefold()
        if variant_counts.get(variant, 0) >= 2 or source_counts.get(source, 0) >= 5:
            continue
        append(index)
        if len(selected) >= limit:
            return selected

    # A season-aware list can be shorter than the hard limit. Filling it with
    # dead S01 clones makes coverage less clear and adds no useful choice.
    if season_aware:
        return selected

    # Do not return an artificially short list when only one edition exists.
    for index, release in enumerate(releases):
        if index in selected_ids:
            continue
        selected.append(release)
        if len(selected) >= limit:
            break
    return selected


def magnet_info_hash(magnet_url: str) -> str:
    try:
        values = parse_qs(urlparse(magnet_url).query).get("xt", [])
    except ValueError:
        return ""
    for value in values:
        prefix = "urn:btih:"
        if value.casefold().startswith(prefix):
            return value[len(prefix):]
    return ""


def compact_title(title: str, limit: int = 53) -> str:
    cleaned = re.sub(r"\s+", " ", title).strip()
    return cleaned if len(cleaned) <= limit else cleaned[: limit - 1] + "…"


def _labeled_value(details: str, label: str) -> str:
    match = re.search(rf"(?im)^{re.escape(label)}:\s*(.+)$", details)
    return re.sub(r"\s+", " ", match.group(1)).strip() if match else ""


def video_quality_summary(release: Release, details: str = "") -> str:
    """Return compact, user-facing video specs from tracker text."""
    text = f"{release.title}\n{details}"
    markers = (
        ("BDRemux", r"\bbdremux\b"),
        ("Blu-ray", r"\bblu[ -]?ray\b"),
        ("BDRip", r"\bbdrip\b"),
        ("WEB-DL", r"\bweb[ .-]?dl\b"),
        ("WEBRip", r"\bwebrip\b"),
        ("HDTV", r"\bhdtv\b"),
        ("HDRip", r"\bhdrip\b"),
        ("DVDRip", r"\bdvdrip\b"),
        ("CAM — съёмка с экрана", r"\b(?:camrip|hdcam|cam)\b"),
        ("TS — экранная копия", r"\b(?:telesync|tsrip|ts)\b"),
        ("4K / 2160p", r"\b(?:4k|2160p)\b"),
        ("1080p", r"\b1080p\b"),
        ("720p", r"\b720p\b"),
        ("H.265 / HEVC", r"\b(?:h[ .]?265|hevc)\b"),
        ("H.264 / AVC", r"\b(?:h[ .]?264|avc)\b"),
        ("Dolby Vision", r"\bdolby[ .-]?vision\b|\bdo?vi\b"),
        ("HDR10", r"\bhdr10\+?\b"),
        ("HDR", r"\bhdr(?!10)\b"),
        ("10-bit", r"\b10[ -]?bit\b"),
    )
    found = [label for label, pattern in markers if re.search(pattern, text, re.I)]
    if found:
        return " · ".join(found[:6])
    return compact_title(_labeled_value(details, "Качество видео") or "не указано", 120)


def voiceover_summary(release: Release, details: str = "") -> str:
    """Prefer the tracker's translation field, then infer common RU voiceovers."""
    translation = _labeled_value(details, "Перевод")
    if translation:
        return compact_title(translation, 170)

    text = f"{release.title}\n{details}"
    markers = (
        ("Дубляж", r"дублированн\w*|\bдубляж\w*"),
        ("Многоголосая", r"многоголос\w*"),
        ("Двухголосая", r"двухголос\w*"),
        ("Одноголосая", r"одноголос\w*"),
        ("Авторская", r"авторск\w*"),
        ("Кравец", r"\b(?:kravec|кравец)\b"),
        ("Пифагор", r"\b(?:pifagor|пифагор)\b"),
        ("AlexFilm", r"\balexfilm\b"),
        ("LostFilm", r"\blostfilm\b"),
        ("Amedia", r"\b(?:amedia|амедиа)\b"),
        ("NewStudio", r"\bnewstudio\b"),
        ("HDRezka Studio", r"\bhdrezka(?:[ .-]?studio)?\b"),
        ("Red Head Sound", r"\bred[ .-]?head[ .-]?sound\b"),
        ("Jaskier", r"\bjaskier\b"),
        ("Кубик в Кубе", r"кубик\s+в\s+кубе"),
        ("TVShows", r"\btvshows\b"),
        ("BaibaKo", r"\bbaibako\b"),
        ("ColdFilm", r"\bcoldfilm\b"),
        ("РенТВ", r"\bрентв\b"),
        ("Лицензия", r"лицензи\w*"),
        ("iTunes", r"\bitunes\b"),
    )
    found = [label for label, pattern in markers if re.search(pattern, text, re.I)]
    coded_tracks = re.search(
        r"(?:\||\[)\s*((?:D|P|A)(?:\s*,\s*(?:D|P|A))+)",
        release.title,
        re.I,
    )
    if coded_tracks:
        codes = {code.upper() for code in re.findall(r"[DPA]", coded_tracks.group(1), re.I)}
        for code, label in (("D", "Дубляж"), ("P", "Профессиональная"), ("A", "Авторская")):
            if code in codes and label not in found:
                found.insert(0, label)
    if re.search(r"\|\s*D(?:\s*[,|]|$)", release.title, re.I) and "Дубляж" not in found:
        found.insert(0, "Дубляж")
    if re.search(r"\|[^\n]*\bP(?:\s*[,|]|$)", release.title, re.I) and not any("голос" in item for item in found):
        found.insert(1 if found else 0, "Профессиональная")
    audio_tags = release_audio_tags(replace(release, details_text=details or release.details_text))
    for key, label in (("dub", "Дубляж"), ("multi", "Многоголосая"), ("dvo", "Двухголосая")):
        if key in audio_tags and label not in found:
            found.insert(0, label)
    return " · ".join(dict.fromkeys(found)) if found else "не указана источником"


def seed_health_summary(seeders: int) -> str:
    value = max(0, int(seeders))
    remainder = value % 100
    if 11 <= remainder <= 14:
        noun = "сидов"
    elif value % 10 == 1:
        noun = "сид"
    elif 2 <= value % 10 <= 4:
        noun = "сида"
    else:
        noun = "сидов"
    if value == 0:
        return "⚠️ 0 сидов"
    if value <= 3:
        return f"🟠 {value} {noun}"
    return f"🟢 {value} {noun}"


def availability_summary(release: Release) -> str:
    if release.cached is True:
        return "⚡ в кэше TorBox"
    if release.cached is False:
        return f"⏳ не в кэше · {seed_health_summary(release.seeders)}"
    return seed_health_summary(release.seeders)


def audio_track_summary(details: str) -> str:
    return compact_title(
        _labeled_value(details, "Аудио 1") or _labeled_value(details, "Язык озвучки") or "",
        180,
    )


def release_content_kind(release: Release) -> str:
    """Classify obvious non-video matches so movie/series searches stay clean."""
    text = f"{release.title} {release.category}"
    if re.search(r"\bpc\b.*(?:repack|лицензи|steam|gog)|repack\s+от|steam[ -]?rip|\b(?:xbox|playstation|ps[345])\b", text, re.I):
        return "game"
    if re.search(r"\b(?:fb2|epub|mobi|djvu)\b", text, re.I):
        return "book"
    if re.search(r"\b(?:bdrip|bdremux|web[ .-]?dl|webrip|hdtv|dvdrip|hdrip|blu[ -]?ray|uhd|2160p|1080p|720p|s\d{1,2}(?:e\d{1,3})?)\b", text, re.I):
        return "video"
    if re.search(r"\b(?:mp3|flac|alac|дискография|саундтрек|soundtrack)\b", text, re.I):
        return "music"
    return "other"


def friendly_torbox_status(detail: str) -> str:
    normalized = detail.casefold()
    if "cached" in normalized:
        return "Найдено в кэше — доступно сразу"
    if "created" in normalized or "added" in normalized:
        return "Загрузка принята"
    return compact_title(detail or "Принято TorBox", 160)


class State:
    """Small local cache for search results and Telegram update offset; no secrets."""

    def __init__(self, path: Path = STATE_PATH) -> None:
        self.path = path
        # Interactive and background workers share one atomic state snapshot.
        self._lock = RLock()
        self._last_prune = 0.0
        self.data: dict[str, Any] = {
            "offset": 0,
            "searches": {},
            "pending": {},
            "modes": {},
            "notifications_initialized": False,
            "seen_notifications": [],
            "inline_items": {},
            "ui": {},
            "stalled_downloads": {},
            "reported_stalls": [],
            "torbox_accounts": {},
        }
        try:
            self.data.update(json.loads(path.read_text(encoding="utf-8")))
        except FileNotFoundError:
            pass
        except (OSError, json.JSONDecodeError) as exc:
            LOG.warning("State ignored: %s", exc)
        # Keep old state.json files forward-compatible when new sections appear.
        self.data.setdefault("offset", 0)
        self.data.setdefault("searches", {})
        self.data.setdefault("pending", {})
        self.data.setdefault("modes", {})
        self.data.setdefault("notifications_initialized", False)
        self.data.setdefault("seen_notifications", [])
        self.data.setdefault("inline_items", {})
        self.data.setdefault("ui", {})
        self.data.setdefault("stalled_downloads", {})
        self.data.setdefault("reported_stalls", [])
        self.data.setdefault("torbox_accounts", {})
        for ui in self.data["ui"].values():
            ui.setdefault("last_active", int(time.time()))

    def _torbox_account(self, user_id: int | None) -> dict[str, Any]:
        """Return per-user maintenance state, retaining the legacy owner state."""
        if user_id is None:
            return self.data
        return self.data.setdefault("torbox_accounts", {}).setdefault(str(user_id), {
            "notifications_initialized": False,
            "seen_notifications": [],
            "stalled_downloads": {},
            "reported_stalls": [],
        })

    def save(self) -> None:
        with self._lock:
            now = time.time()
            if now - self._last_prune > 60:
                for section in ("searches", "pending", "inline_items"):
                    self.data[section] = {key: value for key, value in self.data[section].items()
                                          if value.get("expires", 0) >= now}
                for user_id, ui in self.data["ui"].items():
                    if now - ui.get("last_active", now) > 30 * 86_400:
                        for key in ("history", "favorites"):
                            ui.pop(key, None)
                        self.data["modes"].pop(user_id, None)
                self._last_prune = now
            write_private_json(self.path, self.data)

    def forget_user(self, user_id: int) -> None:
        with self._lock:
            for section in ("searches", "pending", "modes", "ui", "torbox_accounts"):
                self.data[section].pop(str(user_id), None)
            self.data["inline_items"] = {key: value for key, value in self.data["inline_items"].items()
                                          if value.get("payload", {}).get("owner") != user_id}
            self.save()

    def reset_account(self, user_id: int) -> None:
        with self._lock:
            self.data["torbox_accounts"].pop(str(user_id), None)
            self.set_ui(user_id, recent_adds={})

    def offset(self) -> int:
        with self._lock:
            return int(self.data.get("offset", 0))

    def set_offset(self, offset: int) -> None:
        with self._lock:
            self.data["offset"] = max(0, int(offset))
            self.save()

    def notifications_initialized(self, user_id: int | None = None) -> bool:
        with self._lock:
            return bool(self._torbox_account(user_id).get("notifications_initialized"))

    def put_search(
        self,
        chat_id: int,
        releases: list[Release],
        *,
        query: str = "",
        source_status: str = "",
        candidates: list[Release] | None = None,
    ) -> None:
        with self._lock:
            self.data["searches"][str(chat_id)] = {
                "revision": os.urandom(5).hex(),
                "expires": int(time.time()) + 3600,
                "query": query,
                "source_status": source_status,
                "items": [release.to_dict() for release in releases],
                "candidates": [release.to_dict() for release in (candidates if candidates is not None else releases)],
            }
            if len(self.data["searches"]) > MAX_SEARCH_CACHE:
                overflow = len(self.data["searches"]) - MAX_SEARCH_CACHE
                oldest = sorted((key for key in self.data["searches"] if key != str(chat_id)),
                                key=lambda key: self.data["searches"][key].get("expires", 0))
                for key in oldest[:overflow]:
                    self.data["searches"].pop(key, None)
            self.data.setdefault("ui", {}).setdefault(str(chat_id), {}).update(search_page=0, search_filter="all", search_season="all")
            self.save()

    def search_revision(self, chat_id: int) -> str:
        with self._lock:
            return str(self.data["searches"].get(str(chat_id), {}).get("revision") or "")

    def search_candidates(self, chat_id: int) -> list[Release]:
        with self._lock:
            stored = self.data["searches"].get(str(chat_id), {})
            if stored.get("expires", 0) < time.time():
                return []
            return [Release.from_dict(item) for item in stored.get("candidates", stored.get("items", []))]

    def ui_value(self, chat_id: int, key: str, default: Any = None) -> Any:
        with self._lock:
            return self.data.get("ui", {}).get(str(chat_id), {}).get(key, default)

    def set_ui(self, chat_id: int, **values: Any) -> None:
        with self._lock:
            self.data.setdefault("ui", {}).setdefault(str(chat_id), {}).update(values)
            self.save()

    def remember_query(self, chat_id: int, query: str) -> None:
        with self._lock:
            history = self.ui_value(chat_id, "history", [])
            self.set_ui(chat_id, history=[query, *[q for q in history if q.casefold() != query.casefold()]][:8])

    def get_search(self, chat_id: int) -> tuple[str, str, list[Release]] | None:
        with self._lock:
            stored = self.data["searches"].get(str(chat_id), {})
            if stored.get("expires", 0) < time.time():
                return None
            items = stored.get("items", [])
            if not isinstance(items, list):
                return None
            return (
                str(stored.get("query") or ""),
                str(stored.get("source_status") or ""),
                [Release.from_dict(item) for item in items if isinstance(item, dict)],
            )

    def get_release(self, chat_id: int, index: int) -> Release | None:
        with self._lock:
            stored = self.data["searches"].get(str(chat_id), {})
            if stored.get("expires", 0) < time.time():
                return None
            items = stored.get("items", [])
            return Release.from_dict(items[index]) if 0 <= index < len(items) else None

    def update_release(self, chat_id: int, index: int, release: Release) -> None:
        with self._lock:
            stored = self.data["searches"].get(str(chat_id), {})
            items = stored.get("items", [])
            if 0 <= index < len(items):
                items[index] = release.to_dict()
                for candidate_index, raw in enumerate(stored.get("candidates", [])):
                    if release_dedupe_key(Release.from_dict(raw)) == release_dedupe_key(release):
                        stored["candidates"][candidate_index] = release.to_dict()
                self.save()

    def put_inline_item(self, token: str, payload: dict[str, Any]) -> None:
        self.put_inline_items({token: payload})

    def put_inline_items(self, payloads: dict[str, dict[str, Any]]) -> None:
        with self._lock:
            now = int(time.time())
            items = self.data["inline_items"]
            for token, payload in payloads.items():
                items[token] = {"expires": now + 86_400, "payload": payload}
            fresh = {
                key: value for key, value in items.items()
                if isinstance(value, dict) and int(value.get("expires") or 0) >= now
            }
            if len(fresh) > 500:
                ordered = sorted(fresh.items(), key=lambda pair: int(pair[1].get("expires") or 0), reverse=True)
                fresh = dict(ordered[:500])
            self.data["inline_items"] = fresh
            self.save()

    def get_inline_item(self, token: str) -> dict[str, Any] | None:
        with self._lock:
            stored = self.data["inline_items"].get(token)
            if not isinstance(stored, dict) or int(stored.get("expires") or 0) < time.time():
                return None
            payload = stored.get("payload")
            return payload.copy() if isinstance(payload, dict) else None

    def put_pending(self, chat_id: int, release: Release) -> None:
        with self._lock:
            self.data["pending"][str(chat_id)] = {"expires": int(time.time()) + 900, "item": release.to_dict()}
            if len(self.data["pending"]) > 128:
                oldest = min((key for key in self.data["pending"] if key != str(chat_id)),
                             key=lambda key: self.data["pending"][key]["expires"])
                self.data["pending"].pop(oldest, None)
            self.save()

    def pop_pending(self, chat_id: int) -> Release | None:
        with self._lock:
            pending = self.data["pending"].pop(str(chat_id), None)
            self.save()
            if not pending or pending.get("expires", 0) < time.time():
                return None
            return Release.from_dict(pending["item"])

    def set_mode(self, chat_id: int, mode: str | None) -> None:
        with self._lock:
            key = str(chat_id)
            if mode:
                self.data["modes"][key] = mode
            else:
                self.data["modes"].pop(key, None)
            self.save()

    def mode(self, chat_id: int) -> str | None:
        with self._lock:
            value = self.data["modes"].get(str(chat_id))
            return str(value) if value else None

    def screen_message_id(self, chat_id: int) -> int | None:
        with self._lock:
            raw = self.data.get("ui", {}).get(str(chat_id), {}).get("screen_message_id")
            try:
                value = int(raw)
            except (TypeError, ValueError):
                return None
            return value if value > 0 else None

    def set_screen_message_id(self, chat_id: int, message_id: int) -> None:
        with self._lock:
            ui = self.data.setdefault("ui", {}).setdefault(str(chat_id), {})
            ui["screen_message_id"] = max(1, int(message_id))
            self.save()

    def bottom_keyboard_version(self, chat_id: int) -> int:
        with self._lock:
            raw = self.data.get("ui", {}).get(str(chat_id), {}).get("bottom_keyboard_version", 0)
            try:
                return max(0, int(raw))
            except (TypeError, ValueError):
                return 0

    def set_bottom_keyboard_version(self, chat_id: int, version: int) -> None:
        with self._lock:
            ui = self.data.setdefault("ui", {}).setdefault(str(chat_id), {})
            ui["bottom_keyboard_version"] = max(0, int(version))
            self.save()

    def notification_ids(self, user_id: int | None = None) -> set[str]:
        with self._lock:
            return {str(item) for item in self._torbox_account(user_id).get("seen_notifications", [])}

    def mark_notifications(self, notification_ids: list[str], user_id: int | None = None) -> None:
        with self._lock:
            account = self._torbox_account(user_id)
            combined = list(dict.fromkeys([*account.get("seen_notifications", []), *notification_ids]))
            account["seen_notifications"] = combined[-500:]
            account["notifications_initialized"] = True
            self.save()

    def update_stalled_downloads(
        self,
        stalled_ids: set[str],
        now: int,
        user_id: int | None = None,
    ) -> tuple[dict[str, int], set[str]]:
        with self._lock:
            account = self._torbox_account(user_id)
            previous = {
                str(key): int(value) for key, value in account.get("stalled_downloads", {}).items()
                if str(value).isdigit()
            }
            current = {torrent_id: previous.get(torrent_id, now) for torrent_id in stalled_ids}
            reported = {str(value) for value in account.get("reported_stalls", [])}
            reported -= reported - stalled_ids
            account["stalled_downloads"] = current
            account["reported_stalls"] = sorted(reported)
            self.save()
            return current, reported

    def mark_stall_reported(self, torrent_id: str, user_id: int | None = None) -> None:
        with self._lock:
            account = self._torbox_account(user_id)
            reported = {str(value) for value in account.get("reported_stalls", [])}
            reported.add(str(torrent_id))
            account["reported_stalls"] = sorted(reported)[-500:]
            self.save()


class CredentialStore:
    """Protected local storage for per-user TorBox API keys."""

    def __init__(self, path: Path = CREDENTIALS_PATH) -> None:
        self.path = path
        self._lock = RLock()
        self.data: dict[str, str] = {}
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            users = raw.get("users") if isinstance(raw, dict) else None
            if not isinstance(users, dict) or any(
                not str(user_id).isdigit() or not isinstance(api_key, str) or not api_key
                for user_id, api_key in users.items()
            ):
                raise ValueError("Invalid credential store format")
            self.data = dict(users)
            path.chmod(0o600)
        except FileNotFoundError:
            pass
        except (OSError, ValueError) as exc:
            raise RuntimeError("Credential store could not be loaded; restore it before starting the bot") from exc

    def save(self) -> None:
        with self._lock:
            write_private_json(self.path, {"users": self.data})

    def api_key(self, user_id: int) -> str | None:
        with self._lock:
            value = self.data.get(str(user_id))
            return value if value else None

    def set_api_key(self, user_id: int, api_key: str) -> None:
        with self._lock:
            updated = {**self.data, str(user_id): api_key.strip()}
            write_private_json(self.path, {"users": updated})
            self.data = updated

    def remove_api_key(self, user_id: int) -> bool:
        with self._lock:
            updated = self.data.copy()
            removed = updated.pop(str(user_id), None) is not None
            if removed:
                write_private_json(self.path, {"users": updated})
                self.data = updated
            return removed

    def user_ids(self) -> set[int]:
        with self._lock:
            return {int(value) for value in self.data if value.isdigit()}


class NnmClient(ThreadLocalHTTP):
    key = "nnm"
    name = "NNM-Club"

    def __init__(self) -> None:
        self.init_http(scraper_session, {"User-Agent": USER_AGENT})

    def search(self, query: str) -> list[Release]:
        url = f"{NNM_BASE}tracker.php?nm={quote_plus(query)}"
        response = self.http.get(url, timeout=25)
        response.raise_for_status()
        # Each result has category, topic and DL links within the same table row.
        rows = re.findall(r"<tr[^>]*>(.*?)</tr>", response.text, flags=re.S | re.I)
        releases: list[Release] = []
        seen: set[str] = set()
        for row in rows:
            dl_match = re.search(r'href=["\']download\.php\?id=(\d+)["\']', row, re.I)
            topic_match = re.search(r'href=["\']viewtopic\.php\?t=(\d+)["\'][^>]*>\s*(?:<b>)?(.*?)(?:</b>)?\s*</a>', row, re.S | re.I)
            if not dl_match or not topic_match:
                continue
            torrent_id, topic_id = dl_match.group(1), topic_match.group(1)
            if torrent_id in seen:
                continue
            seen.add(torrent_id)
            title = re.sub(r"<[^>]+>", "", html.unescape(topic_match.group(2)))
            title = re.sub(r"\s+", " ", title).strip()
            category_match = re.search(r'tracker\.php\?f=\d+[^>]*>(.*?)</a>', row, re.S | re.I)
            category = re.sub(r"<[^>]+>", "", html.unescape(category_match.group(1))).strip() if category_match else "NNM-Club"
            size_match = re.search(r"<u>\d+</u>\s*([^<]{1,24}(?:KB|MB|GB|TB))", row, re.I)
            size_text = re.sub(r"\s+", " ", html.unescape(size_match.group(1))).strip() if size_match else "размер не указан"
            seed_match = re.search(r'title=["\']Seeders["\'][^>]*>\s*<b>(\d+)</b>', row, re.I)
            leech_match = re.search(r'title=["\']Leechers["\'][^>]*>\s*<b>(\d+)</b>', row, re.I)
            seeders = int(seed_match.group(1)) if seed_match else 0
            leechers = int(leech_match.group(1)) if leech_match else 0
            releases.append(Release(
                torrent_id=torrent_id, topic_id=topic_id, title=title, category=category,
                size_text=size_text, size_bytes=parse_size(size_text), seeders=seeders, leechers=leechers,
                source_url=urljoin(NNM_BASE, f"viewtopic.php?t={topic_id}"),
                download_url=urljoin(NNM_BASE, f"download.php?id={torrent_id}"),
                score=score_release(title, seeders, leechers),
                provider=self.key,
                source_name=self.name,
            ))
        return sorted(releases, key=lambda item: item.score, reverse=True)[:MAX_PROVIDER_RESULTS]

    def topic_summary(self, release: Release) -> str:
        response = self.http.get(release.source_url, timeout=25)
        response.raise_for_status()
        text = re.sub(r"<[^>]+>", " ", html.unescape(response.text))
        text = re.sub(r"\s+", " ", text)
        keys = ("Перевод:", "Язык озвучки:", "Видео:", "Аудио 1:", "Качество видео:", "Релиз от:")
        fragments: list[str] = []
        for key in keys:
            match = re.search(re.escape(key) + r"\s*(.{1,220}?)(?=\s+(?:Перевод:|Язык озвучки:|Видео:|Аудио \d+:|Субтитры:|Просмотр|Дополнительная информация:)|$)", text, re.I)
            if match:
                fragments.append(f"{key} {match.group(1).strip()}")
        return "\n".join(fragments[:6]) or "Подробное описание у источника не распознано."

    def download_torrent(self, release: Release) -> bytes:
        response = self.http.get(release.download_url, headers={"Referer": release.source_url}, timeout=(5, 30), stream=True)
        content = torrent_response_bytes(response)
        # Fast validity check before it reaches the TorBox account.
        try:
            decoded = bencodepy.decode(content)
            if b"info" not in decoded:
                raise ValueError("no info dictionary")
        except Exception as exc:  # bencode parser errors have varying concrete types
            raise RuntimeError(f"Source did not return a valid .torrent file: {exc}") from exc
        return content


class ProwlarrClient(ThreadLocalHTTP):
    """One connector for all torrent indexers configured in Prowlarr."""

    key = "prowlarr"
    name = "Prowlarr"

    def __init__(self, base_url: str, api_key: str, indexer_ids: tuple[int, ...] = ()) -> None:
        parsed = urlparse(base_url.strip())
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise RuntimeError("PROWLARR_URL must be a full http(s) URL")
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key.strip()
        PRIVATE_LOG.add_secret(self.api_key)
        if not self.api_key:
            raise RuntimeError("PROWLARR_API_KEY is empty")
        self.indexer_ids = indexer_ids
        self.init_http(http_session, {"X-Api-Key": self.api_key, "User-Agent": USER_AGENT, "Accept": "application/json"})

    @staticmethod
    def _http_url(*candidates: Any) -> str:
        for candidate in candidates:
            value = str(candidate or "").strip()
            if urlparse(value).scheme in {"http", "https"}:
                return value
        return ""

    def search(self, query: str) -> list[Release]:
        params: list[tuple[str, Any]] = [
            ("query", query), ("type", "search"), ("limit", MAX_PROVIDER_RESULTS), ("offset", 0),
        ]
        params.extend(("indexerIds", indexer_id) for indexer_id in self.indexer_ids)
        response = self.http.get(f"{self.base_url}/api/v1/search", params=params, timeout=(5, 25), allow_redirects=False)
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, list):
            raise RuntimeError("Prowlarr returned an unexpected search response")

        releases: list[Release] = []
        for item in payload:
            if not isinstance(item, dict) or str(item.get("protocol") or "torrent").casefold() != "torrent":
                continue
            title = re.sub(r"\s+", " ", str(item.get("title") or "")).strip()
            if not title:
                continue
            magnet_url = str(item.get("magnetUrl") or "").strip()
            download_url = str(item.get("downloadUrl") or "").strip()
            if is_magnet_url(download_url) and not magnet_url:
                magnet_url, download_url = download_url, ""
            # Some indexers expose an HTTP Prowlarr proxy in magnetUrl. That
            # endpoint redirects to a magnet URI, but downloadUrl beside it
            # serves a valid .torrent file. Never pass the proxy string to
            # TorBox as though it were a magnet.
            if magnet_url and not is_magnet_url(magnet_url):
                if not download_url and urlparse(magnet_url).scheme in {"http", "https"}:
                    download_url = magnet_url
                magnet_url = ""
            if not download_url and not magnet_url:
                continue
            indexer = str(item.get("indexer") or f"Prowlarr #{item.get('indexerId', '?')}").strip()
            size_bytes = max(0, int(item.get("size") or 0))
            seeders = max(0, int(item.get("seeders") or 0))
            leechers = max(0, int(item.get("leechers") or 0))
            info_hash = str(item.get("infoHash") or item.get("releaseHash") or "").strip()
            if not info_hash and magnet_url:
                info_hash = magnet_info_hash(magnet_url)
            categories = item.get("categories") if isinstance(item.get("categories"), list) else []
            category_names = [str(category.get("name")) for category in categories if isinstance(category, dict) and category.get("name")]
            category = ", ".join(category_names[:2]) or "Torrent"
            source_url = self._http_url(item.get("commentUrl"), item.get("infoUrl"), item.get("guid"))
            publish_date = str(item.get("publishDate") or "").replace("T", " ").replace("Z", " UTC")
            details = [f"Источник: {indexer}", f"Категория: {category}"]
            if publish_date:
                details.append(f"Опубликовано: {publish_date}")
            if info_hash:
                details.append(f"Info hash: {info_hash}")
            stable_id = item.get("id") or item.get("guid") or info_hash or hashlib.sha1(title.encode()).hexdigest()[:16]
            releases.append(Release(
                torrent_id=str(stable_id),
                topic_id=str(item.get("id") or stable_id),
                title=title,
                category=category,
                size_text=human_size(size_bytes) if size_bytes else "размер не указан",
                size_bytes=size_bytes,
                seeders=seeders,
                leechers=leechers,
                source_url=source_url,
                download_url=download_url,
                score=score_release(title, seeders, leechers),
                provider=self.key,
                source_name=indexer,
                info_hash=info_hash,
                magnet_url=magnet_url,
                details_text="\n".join(details),
            ))
        return sorted(releases, key=lambda item: item.score, reverse=True)[:MAX_PROVIDER_RESULTS]

    def topic_summary(self, release: Release) -> str:
        return release.details_text or f"Источник: {release.source_name}\nКатегория: {release.category}"

    def _download_endpoint(self, raw_url: str) -> tuple[str, bool]:
        configured = urlparse(self.base_url)
        if raw_url.startswith("/") and configured.path not in {"", "/"} and not raw_url.startswith(f"{configured.path.rstrip('/')}/"):
            url = f"{configured.scheme}://{configured.netloc}{configured.path.rstrip('/')}{raw_url}"
        else:
            url = urljoin(f"{self.base_url}/", raw_url)
        target = urlparse(url)
        same_origin = (configured.scheme, configured.hostname, configured.port) == (target.scheme, target.hostname, target.port)
        return url, same_origin

    def download_torrent(self, release: Release) -> bytes:
        if not release.download_url:
            raise RuntimeError("Prowlarr did not provide a torrent download URL")
        url, same_origin = self._download_endpoint(release.download_url)
        if urlparse(url).scheme not in {"http", "https"}:
            raise RuntimeError("Prowlarr returned an unsupported download URL")
        for _ in range(5):
            # requests preserves custom X-Api-Key headers on redirects. Follow
            # redirects explicitly, deciding whether to send credentials at every hop.
            get = self.http.get if same_origin else requests.get
            response = get(url, headers={"User-Agent": USER_AGENT}, timeout=(5, 30),
                           stream=True, allow_redirects=False)
            if response.status_code not in {301, 302, 303, 307, 308}:
                break
            location = response.headers.get("Location", "")
            response.close()
            url, same_origin = self._download_endpoint(urljoin(url, location))
            if not location or urlparse(url).scheme not in {"http", "https"}:
                raise RuntimeError("Prowlarr returned an invalid redirect")
        else:
            raise RuntimeError("Too many torrent download redirects")
        content = torrent_response_bytes(response)
        try:
            decoded = bencodepy.decode(content)
            if b"info" not in decoded:
                raise ValueError("no info dictionary")
        except Exception as exc:
            raise RuntimeError(f"Prowlarr did not return a valid .torrent file: {exc}") from exc
        return content


class SearchAggregator:
    """Search providers concurrently and return one deduplicated ranked list."""

    def __init__(self, providers: list[SearchProvider]) -> None:
        if not providers:
            raise RuntimeError("At least one search provider must be enabled")
        self.providers = providers
        self.by_key = {provider.key: provider for provider in providers}
        self._status = local()
        self.last_status: dict[str, str] = {}

    @property
    def last_status(self) -> dict[str, str]:
        return getattr(self._status, "value", {})

    @last_status.setter
    def last_status(self, value: dict[str, str]) -> None:
        self._status.value = value

    def search(self, query: str) -> list[Release]:
        return select_diverse_releases(self.search_candidates(query), MAX_SEARCH_CANDIDATES)

    def search_candidates(self, query: str) -> list[Release]:
        found: list[Release] = []
        self.last_status = {}
        with ThreadPoolExecutor(max_workers=min(4, len(self.providers)), thread_name_prefix="search") as pool:
            futures = {pool.submit(provider.search, query): provider for provider in self.providers}
            for future in as_completed(futures):
                provider = futures[future]
                try:
                    items = future.result()
                    self.last_status[provider.name] = str(len(items))
                    found.extend(items)
                except Exception as exc:
                    self.last_status[provider.name] = "ошибка"
                    LOG.warning("Search provider %s failed: %s", provider.name, exc, exc_info=True)

        unique: dict[str, Release] = {}
        for item in found:
            if not title_matches_query(query, item.title):
                continue
            ranked = replace(
                item,
                score=item.score + source_bonus(item.source_name) + query_relevance(query, item.title),
            )
            key = release_dedupe_key(ranked)
            previous = unique.get(key)
            if previous is None or (ranked.score, ranked.seeders) > (previous.score, previous.seeders):
                unique[key] = ranked
        ranked_items = sorted(unique.values(), key=lambda item: (item.score, item.seeders), reverse=True)
        video_items = [item for item in ranked_items if release_content_kind(item) == "video"]
        # This bot is optimized for films and series. When there are enough
        # genuine video matches, do not let similarly named games, books or
        # soundtracks displace them in the short Telegram result list.
        if len(video_items) >= 4:
            ranked_items = video_items
        return ranked_items

    def status_summary(self) -> str:
        return " · ".join(f"{name}: {status}" for name, status in self.last_status.items())

    def topic_summary(self, release: Release) -> str:
        provider = self.by_key.get(release.provider)
        if not provider:
            return release.details_text or "Источник этого результата больше не подключён."
        return provider.topic_summary(release)

    def download_torrent(self, release: Release) -> bytes:
        provider = self.by_key.get(release.provider)
        if not provider:
            raise RuntimeError("Источник этого результата больше не подключён")
        return provider.download_torrent(release)


class TorBoxClient:
    def __init__(self, api_key: str) -> None:
        self.api_key = api_key
        self.session = self._new_session()
        self._owner_thread_id = get_ident()
        self._thread_sessions = local()

    def _new_session(self) -> requests.Session:
        session = requests.Session()
        configure_read_retries(session)
        session.headers.update({"Authorization": f"Bearer {self.api_key}", "User-Agent": USER_AGENT})
        return session

    def _session(self) -> requests.Session:
        """Keep maintenance traffic isolated from interactive TorBox requests."""
        if get_ident() == self._owner_thread_id:
            return self.session
        session = getattr(self._thread_sessions, "session", None)
        if session is None:
            session = self._new_session()
            self._thread_sessions.session = session
        return session

    def request(self, method: str, path: str, *, timeout: int = 30, **kwargs: Any) -> dict[str, Any]:
        response = self._session().request(method, f"{TORBOX_API}/{path.lstrip('/')}", timeout=(5, timeout),
                                          allow_redirects=False, **kwargs)
        try:
            payload = response.json()
        except ValueError as exc:
            raise RuntimeError(f"TorBox вернул ответ {response.status_code} без JSON") from exc
        if not isinstance(payload, dict):
            raise RuntimeError("TorBox вернул неожиданный формат ответа")
        # TorBox historically returned HTTP 404 with success=true for an empty list.
        if payload.get("success") is not True and (not response.ok or payload.get("success") is False):
            detail = payload.get("detail") or payload.get("error") or f"HTTP {response.status_code}"
            raise RuntimeError(f"TorBox: {detail}")
        return payload

    def add_file(self, torrent_bytes: bytes, release: Release) -> dict[str, Any]:
        safe_id = re.sub(r"[^a-zA-Z0-9_-]+", "-", release.torrent_id).strip("-")[:48]
        if not safe_id:
            safe_id = hashlib.sha1(torrent_bytes).hexdigest()[:16]
        files = {"file": (f"release-{safe_id}.torrent", torrent_bytes, "application/x-bittorrent")}
        data: dict[str, Any] = {"name": release.title[:200]}
        if release.cached is True:
            data["add_only_if_cached"] = "true"
        return self.request(
            "POST", "torrents/createtorrent", timeout=60,
            files=files, data=data,
        )

    def add_magnet(self, magnet_url: str, release: Release) -> dict[str, Any]:
        magnet_url = magnet_url.strip()
        if not is_magnet_url(magnet_url) or len(magnet_url) > 16_000:
            raise ValueError("Источник вернул некорректную magnet-ссылку")
        data: dict[str, Any] = {"magnet": magnet_url, "name": release.title[:200]}
        if release.cached is True:
            data["add_only_if_cached"] = "true"
        return self.request(
            "POST", "torrents/createtorrent", timeout=60,
            data=data,
        )

    def cached_hashes(self, info_hashes: list[str]) -> set[str]:
        normalized = list(dict.fromkeys(normalize_info_hash(value) for value in info_hashes if value))
        if not normalized:
            return set()
        cached: set[str] = set()
        for offset in range(0, len(normalized), 100):
            batch = normalized[offset:offset + 100]
            payload = self.request(
                "GET", "torrents/checkcached",
                params={"hash": ",".join(batch), "format": "object", "list_files": "false"},
            )
            data = payload.get("data")
            if isinstance(data, dict):
                cached.update(normalize_info_hash(key) for key, value in data.items() if value)
            elif isinstance(data, list):
                for item in data:
                    if isinstance(item, dict) and item.get("hash"):
                        cached.add(normalize_info_hash(str(item["hash"])))
                    elif isinstance(item, str):
                        cached.add(normalize_info_hash(item))
        return cached

    def find_torrent_by_hash(self, info_hash: str) -> dict[str, Any] | None:
        wanted = normalize_info_hash(info_hash)
        if not wanted:
            return None
        for item in self.list_torrents(limit=1000):
            alternatives = item.get("alternative_hashes")
            if not isinstance(alternatives, list):
                alternatives = []
            hashes = [item.get("hash"), *alternatives]
            if any(normalize_info_hash(str(value or "")) == wanted for value in hashes):
                return item
        return None

    def list_torrents(self, *, limit: int = 100, bypass_cache: bool = False) -> list[dict[str, Any]]:
        payload = self.request(
            "GET", "torrents/mylist",
            params={"limit": max(1, min(limit, 1000)), "offset": 0, "bypass_cache": str(bypass_cache).lower()},
        )
        data = payload.get("data")
        if data is None:
            return []
        if isinstance(data, dict):
            return [data]
        if not isinstance(data, list):
            raise RuntimeError("TorBox вернул неожиданный список загрузок")
        return [item for item in data if isinstance(item, dict)]

    def get_torrent(self, torrent_id: int, *, bypass_cache: bool = False) -> dict[str, Any] | None:
        payload = self.request(
            "GET", "torrents/mylist",
            params={"id": safe_torrent_id(torrent_id), "bypass_cache": str(bypass_cache).lower()},
        )
        data = payload.get("data")
        if isinstance(data, list):
            return next((item for item in data if safe_torrent_id(item.get("id")) == torrent_id), None)
        return data if isinstance(data, dict) else None

    def control_torrent(self, torrent_id: int, operation: str) -> dict[str, Any]:
        if operation not in {"delete", "resume", "reannounce", "stop_seeding"}:
            raise ValueError("Недопустимая операция с загрузкой")
        return self.request(
            "POST", "torrents/controltorrent",
            json={"torrent_id": safe_torrent_id(torrent_id), "operation": operation, "all": False},
        )

    def download_link(self, torrent_id: int, file_id: int) -> str:
        # Resolve on the server: never put the API-key-bearing request URL in Telegram.
        try:
            data = self.request("GET", "torrents/requestdl", params={
                "token": self.api_key, "torrent_id": safe_torrent_id(torrent_id),
                "file_id": safe_torrent_id(file_id), "redirect": "false",
            }).get("data")
        except Exception:
            raise RuntimeError("Не удалось получить ссылку. Попробуй ещё раз чуть позже.") from None
        if not isinstance(data, str) or urlparse(data).scheme != "https" or self.api_key in data:
            raise RuntimeError("TorBox пока не вернул ссылку на файл. Обнови загрузку и попробуй снова.")
        return data

    def notifications(self) -> list[dict[str, Any]]:
        data = self.request("GET", "notifications/mynotifications").get("data")
        if data is None:
            return []
        if not isinstance(data, list):
            raise RuntimeError("TorBox вернул неожиданный список уведомлений")
        return [item for item in data if isinstance(item, dict)]

    def clear_notifications(self) -> None:
        self.request("POST", "notifications/clear")


def torrent_fingerprint(torrent_bytes: bytes) -> str:
    decoded = bencodepy.decode(torrent_bytes)
    return hashlib.sha1(bencodepy.encode(decoded[b"info"])).hexdigest()


class TelegramBot:
    def __init__(
        self,
        token: str,
        owner_id: int,
        torbox: Any | None,
        nnm: Any,
        state: State,
        *,
        catalog: KinopoiskCatalog | None = None,
        notifications_enabled: bool = True,
        notification_poll_seconds: int = 60,
        credentials: CredentialStore | None = None,
        multi_user_enabled: bool = False,
        torbox_factory: Callable[[str], Any] = TorBoxClient,
        workers: int = 8,
    ) -> None:
        self.token, self.owner_id, self.torbox, self.nnm, self.state = token, owner_id, torbox, nnm, state
        PRIVATE_LOG.add_secret(token)
        self.catalog = catalog
        self.notifications_enabled = notifications_enabled
        self.notification_poll_seconds = notification_poll_seconds
        self.credentials = credentials
        self.multi_user_enabled = multi_user_enabled
        self.torbox_factory = torbox_factory
        self._torbox_clients: dict[int, Any] = {}
        self.last_notification_poll: dict[int, float] = {}
        self.last_health_poll: dict[int, float] = {}
        self.username = ""
        self.bot_id = 0
        self.inline_enabled = False
        self.inline_cache: OrderedDict[str, tuple[float, list[dict[str, Any]]]] = OrderedDict()
        self.inline_dispatch_lock = Lock()
        self.latest_inline_query_ids: dict[int, str] = {}
        self._render_context = local()
        self.callback_message_id: int | None = None
        self.callback_inline_message_id = ""
        self.capture_screen = False
        self.session = requests.Session()
        self._owner_thread_id = get_ident()
        self._thread_sessions = local()
        self._user_locks: WeakValueDictionary[int, Any] = WeakValueDictionary()
        self._user_locks_guard = Lock()
        self.updates = UserDispatcher(workers=workers, name="telegram-user")
        self.inline_jobs = UserDispatcher(workers=4, capacity=32, per_user=2, name="inline")
        self.control_jobs = UserDispatcher(workers=2, capacity=32, name="telegram-control")
        self.maintenance_jobs = UserDispatcher(workers=4, capacity=32, per_user=1, name="torbox-health")
        self.message_limit = RateLimiter(12, 30)
        self.inline_limit = RateLimiter(15, 30)
        self.search_limit = RateLimiter(4, 60)
        self.global_search_limit = RateLimiter(40, 60, max_keys=1)
        self.catalog_limit = RateLimiter(90, 60, max_keys=1)
        self.notice_limit = RateLimiter(1, 15)
        self._closing = Event()
        self.outbound = OutboundThrottle()

    @property
    def callback_message_id(self) -> int | None:
        return getattr(self._render_context, "message_id", None)

    @callback_message_id.setter
    def callback_message_id(self, value: int | None) -> None:
        self._render_context.message_id = value

    @property
    def callback_inline_message_id(self) -> str:
        return getattr(self._render_context, "inline_id", "")

    @callback_inline_message_id.setter
    def callback_inline_message_id(self, value: str) -> None:
        self._render_context.inline_id = value

    @property
    def capture_screen(self) -> bool:
        return getattr(self._render_context, "capture", False)

    @capture_screen.setter
    def capture_screen(self, value: bool) -> None:
        self._render_context.capture = value

    def user_lock(self, user_id: int) -> Any:
        with self._user_locks_guard:
            lock = self._user_locks.get(user_id)
            if lock is None:
                lock = RLock()
                self._user_locks[user_id] = lock
            return lock

    def update_user(self, update: dict[str, Any]) -> int | None:
        event = (update.get("message") or update.get("callback_query") or
                 update.get("inline_query") or update.get("chosen_inline_result") or {})
        user_id = event.get("from", {}).get("id")
        if not isinstance(user_id, int) or user_id <= 0 or not self.user_allowed(user_id):
            return None
        if update.get("message") or update.get("callback_query"):
            message = update.get("message") or event.get("message") or {}
            if not event.get("inline_message_id") and message.get("chat", {}).get("id") != user_id:
                return None
        return user_id

    def dispatch_update(self, update: dict[str, Any]) -> bool:
        """Keep polling free of user network I/O and preserve each user's ordering."""
        user_id = self.update_user(update)
        if user_id is None:
            return False
        if query := update.get("inline_query"):
            self.inline_query(query)
            return True
        if not self.message_limit.allow(user_id):
            self.reject_update(user_id, update)
            return False
        accepted = self.updates.submit(user_id, lambda: self._run_update(user_id, update))
        if not accepted:
            self.reject_update(user_id, update)
        return accepted

    def reject_update(self, user_id: int, update: dict[str, Any]) -> None:
        # Notices have their own small queue and cooldown, so spam cannot block polling.
        if not self.notice_limit.allow(user_id):
            return
        def notify() -> None:
            message = update.get("message", {})
            if self.state.mode(user_id) == "torbox_api_key" and message.get("message_id"):
                self.delete_user_message(user_id, message["message_id"])
            text = "Слишком много запросов. Подожди немного и повтори последнее действие."
            if callback := update.get("callback_query"):
                self.answer_callback(str(callback.get("id") or ""), text, alert=True)
            else:
                self._send_new(user_id, text)
        self.control_jobs.submit(user_id, notify)

    def _run_update(self, user_id: int, update: dict[str, Any]) -> None:
        with self.user_lock(user_id):
            try:
                self._render_context.search_permitted = False
                self.state.set_ui(user_id, last_active=int(time.time()))
                self.handle(update)
            except Exception as exc:
                LOG.exception("User request failed for Telegram user %s", user_id)
                try:
                    self._send_new(user_id, public_error(exc), [[self.home_button()]])
                except Exception:
                    LOG.warning("Could not deliver error response to Telegram user %s", user_id)
            finally:
                self.callback_message_id = None
                self.callback_inline_message_id = ""
                self.capture_screen = False

    def close(self) -> None:
        self._closing.set()
        # Drain accepted work before exit; queued destructive callbacks must not be replayed.
        self.updates.close()
        self.inline_jobs.close()
        self.maintenance_jobs.close()
        self.control_jobs.close()

    def _telegram_session(self) -> requests.Session:
        """Use one Telegram connection pool per worker thread."""
        if get_ident() == self._owner_thread_id:
            return self.session
        session = getattr(self._thread_sessions, "session", None)
        if session is None:
            session = requests.Session()
            self._thread_sessions.session = session
        return session

    def menu_keyboard(self) -> list[list[dict[str, str]]]:
        keyboard: list[list[dict[str, str]]] = [
            [self.search_button()],
            [
                {"text": BTN_TORRENTS, "callback_data": "list:0"},
                {"text": BTN_NOTIFICATIONS, "callback_data": "notifications"},
            ],
        ]
        keyboard.append([
            {"text": "🕘 История", "callback_data": "history"},
            {"text": "⭐ Избранное", "callback_data": "favorites"},
        ])
        keyboard.append([self.search_settings_button()])
        account_row: list[dict[str, str]] = []
        if self.multi_user_enabled or self.credentials:
            account_row.append({"text": BTN_ACCOUNT, "callback_data": "account"})
        account_row.append({"text": BTN_HELP, "callback_data": "help"})
        keyboard.append(account_row)
        keyboard.append([self.guide_button()])
        return keyboard

    def search_button(self, text: str = BTN_SEARCH) -> dict[str, str]:
        if self.inline_enabled and self.catalog:
            return {"text": text, "switch_inline_query_current_chat": ""}
        return {"text": text, "callback_data": "search"}

    @staticmethod
    def guide_button() -> dict[str, str]:
        return {"text": "📖 Инструкция", "url": SETUP_GUIDE_URL}

    @staticmethod
    def home_button() -> dict[str, str]:
        return {"text": BTN_HOME, "callback_data": "home"}

    def bottom_keyboard_markup(self: TelegramBot | None = None) -> dict[str, Any]:
        """Remove the obsolete persistent keyboard below Telegram's input."""
        return {"remove_keyboard": True}

    def user_allowed(self, user_id: int) -> bool:
        return user_id == self.owner_id or self.multi_user_enabled

    def has_torbox(self, user_id: int) -> bool:
        if self.credentials and self.credentials.api_key(user_id):
            return True
        return user_id == self.owner_id and self.torbox is not None

    def torbox_for(self, user_id: int) -> Any:
        if self.credentials:
            api_key = self.credentials.api_key(user_id)
            if api_key:
                PRIVATE_LOG.add_secret(api_key)
                client = self._torbox_clients.get(user_id)
                if client is None:
                    client = self.torbox_factory(api_key)
                    self._torbox_clients[user_id] = client
                return client
        if user_id == self.owner_id and self.torbox is not None:
            return self.torbox
        raise RuntimeError("Сначала подключи свой TorBox API key через /connect.")

    def connected_user_ids(self) -> list[int]:
        users = self.credentials.user_ids() if self.credentials and self.multi_user_enabled else set()
        if self.credentials and self.credentials.api_key(self.owner_id):
            users.add(self.owner_id)
        if self.torbox is not None:
            users.add(self.owner_id)
        return sorted(users)

    def forget_log_key(self, api_key: str | None) -> None:
        if not api_key or api_key == getattr(self.torbox, "api_key", None):
            return
        if self.credentials and any(self.credentials.api_key(user) == api_key for user in self.credentials.user_ids()):
            return
        PRIVATE_LOG.remove_secret(api_key)

    def _maintenance_state_user(self, user_id: int) -> int | None:
        # Legacy single-user tests and deployments keep their old state keys.
        return user_id if self.credentials else None

    def install_bottom_keyboard(self, chat_id: int, *, force: bool = False) -> int | None:
        if not force and self.state.bottom_keyboard_version(chat_id) >= BOTTOM_KEYBOARD_VERSION:
            return self.state.screen_message_id(chat_id)
        result = self.call(
            "sendMessage",
            chat_id=chat_id,
            text=(
                "✅ <b>Пульт обновлён</b>\n\n"
                "Нижняя клавиатура скрыта. Все действия теперь находятся под сообщением бота."
            ),
            parse_mode="HTML",
            link_preview_options={"is_disabled": True},
            reply_markup=self.bottom_keyboard_markup(),
        )
        message_id: int | None = None
        if isinstance(result, dict):
            try:
                message_id = int(result.get("message_id"))
            except (TypeError, ValueError):
                message_id = None
        if message_id:
            self.state.set_screen_message_id(chat_id, message_id)
        self.state.set_bottom_keyboard_version(chat_id, BOTTOM_KEYBOARD_VERSION)
        return message_id

    def call(self, method: str, **payload: Any) -> Any:
        if method != "getUpdates":
            chat_id = payload.get("chat_id") if method in {"sendMessage", "editMessageText"} else None
            self.outbound.wait(chat_id)
        response = self._telegram_session().post(
            TELEGRAM_API.format(token=self.token, method=method), json=payload,
            timeout=(5, 55 if method == "getUpdates" else 15),
        )
        try:
            result = response.json()
        except ValueError as exc:
            response.raise_for_status()
            raise RuntimeError(f"Telegram {method} вернул ответ без JSON") from exc
        if result.get("error_code") == 403 and isinstance(payload.get("chat_id"), int):
            self.state.set_ui(payload["chat_id"], delivery_blocked=True)
        if result.get("error_code") == 429:
            retry_after = float(result.get("parameters", {}).get("retry_after") or 5)
            self.outbound.defer(retry_after)
        if not response.ok or not result.get("ok"):
            description = result.get("description") or f"HTTP {response.status_code}"
            raise RuntimeError(f"Telegram {method}: {description}")
        return result.get("result")

    @staticmethod
    def _message_payload(text: str, keyboard: list[list[dict[str, str]]] | None = None) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "text": text,
            "parse_mode": "HTML",
            "link_preview_options": {"is_disabled": True},
        }
        payload["reply_markup"] = {"inline_keyboard": keyboard or []}
        return payload

    def _send_new(
        self,
        chat_id: int,
        text: str,
        keyboard: list[list[dict[str, str]]] | None = None,
    ) -> Any:
        return self.call("sendMessage", chat_id=chat_id, **self._message_payload(text, self.account_keyboard(chat_id, keyboard)))

    def account_revision(self, user_id: int) -> str:
        key = self.credentials.api_key(user_id) if self.credentials else None
        if not key and user_id == self.owner_id and self.torbox is not None:
            key = getattr(self.torbox, "api_key", "owner")
        return hashlib.sha256(f"{user_id}:{key or 'disconnected'}".encode()).hexdigest()[:12]

    def account_keyboard(self, user_id: int, keyboard: Any) -> Any:
        if not self.credentials or not keyboard:
            return keyboard
        protected = {"tctl", "delconfirm", "nclearconfirm", "file", "files", "tinfo", "delask"}
        result = []
        for row in keyboard:
            buttons = []
            for button in row:
                button = dict(button)
                data = button.get("callback_data", "")
                if data.split(":", 1)[0] in protected:
                    button["callback_data"] = data + ":a" + self.account_revision(user_id)
                buttons.append(button)
            result.append(buttons)
        return result

    def edit_message(
        self,
        chat_id: int,
        message_id: int,
        text: str,
        keyboard: list[list[dict[str, str]]] | None = None,
    ) -> Any:
        return self.call(
            "editMessageText",
            chat_id=chat_id,
            message_id=message_id,
            **self._message_payload(text, self.account_keyboard(chat_id, keyboard)),
        )

    def edit_inline_message(
        self,
        inline_message_id: str,
        text: str,
        keyboard: list[list[dict[str, str]]] | None = None,
    ) -> Any:
        return self.call(
            "editMessageText",
            inline_message_id=inline_message_id,
            **self._message_payload(text, keyboard),
        )

    def render(
        self,
        chat_id: int,
        text: str,
        keyboard: list[list[dict[str, str]]] | None = None,
        *,
        message_id: int | None = None,
        inline_message_id: str = "",
    ) -> Any:
        if message_id is None and not inline_message_id:
            message_id = self.callback_message_id
            inline_message_id = self.callback_inline_message_id
        try:
            if inline_message_id:
                return self.edit_inline_message(inline_message_id, text, keyboard)
            if message_id is not None:
                result = self.edit_message(chat_id, message_id, text, keyboard)
                self._remember_screen(chat_id, result, fallback=message_id)
                return result
        except Exception as exc:
            if "message is not modified" in str(exc).casefold():
                self._remember_screen(chat_id, None, fallback=message_id)
                return None
            LOG.debug("Could not update the existing Telegram screen", exc_info=True)
        result = self._send_new(chat_id, text, keyboard)
        self._remember_screen(chat_id, result)
        return result

    def _remember_screen(self, chat_id: int, result: Any, *, fallback: int | None = None) -> None:
        if not self.capture_screen or self.callback_inline_message_id:
            return
        message_id = fallback
        if isinstance(result, dict) and result.get("message_id") is not None:
            try:
                message_id = int(result["message_id"])
            except (TypeError, ValueError):
                pass
        if message_id:
            self.state.set_screen_message_id(chat_id, message_id)
            self.callback_message_id = message_id

    def send(self, chat_id: int, text: str, keyboard: list[list[dict[str, str]]] | None = None) -> Any:
        """Render callback navigation in place; send only for a genuinely new flow."""
        return self.render(
            chat_id,
            text,
            keyboard,
            message_id=self.callback_message_id,
            inline_message_id=self.callback_inline_message_id,
        )

    def answer_callback(self, callback_id: str, text: str | None = None, *, alert: bool = False) -> None:
        payload: dict[str, Any] = {"callback_query_id": callback_id}
        if text:
            payload.update({"text": text[:200], "show_alert": alert})
        try:
            self.call("answerCallbackQuery", **payload)
        except Exception:
            LOG.debug("Could not answer an old callback query", exc_info=True)

    def delete_user_message(self, chat_id: int, message_id: int) -> bool:
        try:
            self.call("deleteMessage", chat_id=chat_id, message_id=message_id)
            return True
        except Exception:
            LOG.debug("Could not remove the user's service message", exc_info=True)
            return False

    def answer_inline_query(self, inline_query_id: str, results: list[dict[str, Any]], *, cache_time: int = 30) -> None:
        # Inline searches run outside the long-poll thread. A separate HTTP
        # request avoids sharing the polling Session between threads.
        self.call("answerInlineQuery", inline_query_id=inline_query_id, results=results,
                  cache_time=cache_time, is_personal=True)

    def _deep_link(self, token: str) -> str:
        username = self.username or "tor_boxai_bot"
        return f"https://t.me/{username}?start={token}"

    @staticmethod
    def _catalog_type_label(media_type: str) -> str:
        return {
            "FILM": "фильм",
            "TV_SHOW": "телешоу",
            "VIDEO": "видео",
            "MINI_SERIES": "мини-сериал",
            "TV_SERIES": "сериал",
        }.get(media_type.upper(), "кино")

    def _catalog_inline_results(self, titles: list[CatalogTitle]) -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []
        stored: dict[str, dict[str, Any]] = {}
        for title in titles:
            token = f"kp_{title.catalog_id}"
            stored[token] = {"kind": "catalog", "query": title.search_query, "title": title.name}
            display_name = title.name
            if title.original_name:
                display_name += f" ({title.original_name})"
            media_type = self._catalog_type_label(title.media_type)
            first_meta = [media_type]
            if title.rating and title.rating.casefold() not in {"null", "none"}:
                first_meta.append(f"КП {title.rating}")
            if title.year:
                first_meta.append(title.year)
            second_meta = [*title.countries[:2], *title.genres[:3]]
            description = " · ".join(first_meta)
            if second_meta:
                description += "\n" + " · ".join(second_meta)
            message_lines = [
                f"🎬 <b>{html.escape(display_name)}</b>",
                html.escape(" · ".join(first_meta)),
            ]
            if second_meta:
                message_lines.append(f"🌍 {html.escape(' · '.join(second_meta))}")
            message_lines.append("\nРезультаты придут отдельным сообщением от бота в личный чат.")
            result: dict[str, Any] = {
                "type": "article",
                "id": token,
                "title": compact_title(display_name, 180),
                "description": compact_title(description, 400),
                "input_message_content": {
                    "message_text": "\n".join(message_lines),
                    "parse_mode": "HTML",
                    "link_preview_options": {"is_disabled": True},
                },
                "reply_markup": {"inline_keyboard": [[{
                    "text": "🔎 Получить ответ от бота",
                    "callback_data": f"catalog:{token}",
                }]]},
            }
            if urlparse(title.poster_url).scheme == "https":
                result.update({"thumbnail_url": title.poster_url, "thumbnail_width": 80, "thumbnail_height": 120})
            results.append(result)
        if stored:
            self.state.put_inline_items(stored)
        return results

    def _release_inline_results(self, releases: list[Release]) -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []
        stored: dict[str, dict[str, Any]] = {}
        for release in releases:
            digest = hashlib.sha1(release_dedupe_key(release).encode()).hexdigest()[:20]
            token = f"rel_{digest}"
            stored[token] = {"kind": "release", "release": release.to_dict()}
            quality = video_quality_summary(release, release.details_text)
            voiceover = voiceover_summary(release, release.details_text)
            description = (
                f"{quality} · {voiceover}\n"
                f"{release.size_text} · {release.seeders} сидов · {release.source_name}"
            )
            results.append({
                "type": "article",
                "id": token,
                "title": compact_title(release.title, 180),
                "description": compact_title(description, 400),
                "input_message_content": {
                    "message_text": (
                        f"🎬 <b>{html.escape(release.title)}</b>\n\n"
                        f"🎙 <b>Озвучка:</b> {html.escape(voiceover)}\n"
                        f"🎞 <b>Качество:</b> {html.escape(quality)}\n"
                        f"📦 <b>{html.escape(release.size_text)}</b> · 🌱 <b>{release.seeders} сидов</b>\n"
                        f"📡 {html.escape(release.source_name)}"
                    ),
                    "parse_mode": "HTML",
                    "link_preview_options": {"is_disabled": True},
                },
                "reply_markup": {"inline_keyboard": [[{
                    "text": "➕ Открыть в TorBox",
                    "callback_data": f"release:{token}",
                }]]},
            })
        if stored:
            self.state.put_inline_items(stored)
        return results

    def _fast_inline_release_search(self, text: str) -> list[Release]:
        """Use the quickest direct provider while the user is still typing."""
        providers = getattr(self.nnm, "providers", [])
        provider = next((item for item in providers if getattr(item, "key", "") == "nnm"), None)
        releases = provider.search(text) if provider else self.nnm.search(text)
        matching = [release for release in releases if title_matches_query(text, release.title)]
        video = [release for release in matching if release_content_kind(release) == "video"]
        return (video or matching)[:MAX_RESULTS]

    def inline_query(self, query: dict[str, Any]) -> None:
        query_id = str(query.get("id") or "")
        user_id = query.get("from", {}).get("id")
        if not query_id or not isinstance(user_id, int):
            return
        text = re.sub(r"\s+", " ", str(query.get("query") or "")).strip()
        if (not self.user_allowed(user_id) or not self.catalog or
                len(text) > MAX_QUERY_LENGTH or not self.inline_limit.allow(user_id)):
            self.control_jobs.submit(user_id, lambda: self.answer_inline_query(query_id, [], cache_time=5), coalesce=True)
            return
        with self.inline_dispatch_lock:
            self.latest_inline_query_ids[user_id] = query_id
            accepted = self.inline_jobs.submit(
                user_id, lambda: self._debounced_inline_query(query_id, text, user_id), coalesce=True,
            )
            if not accepted:
                self.latest_inline_query_ids.pop(user_id, None)
        if not accepted:
            self.control_jobs.submit(user_id, lambda: self.answer_inline_query(query_id, [], cache_time=2), coalesce=True)

    def _debounced_inline_query(self, query_id: str, text: str, user_id: int) -> None:
        try:
            if not self._closing.wait(0.35 if text else 0.05):
                self._process_inline_query(query_id, text, user_id=user_id)
        finally:
            with self.inline_dispatch_lock:
                if self.latest_inline_query_ids.get(user_id) == query_id:
                    self.latest_inline_query_ids.pop(user_id, None)

    def chosen_inline_result(self, chosen: dict[str, Any]) -> None:
        """Keep the user's inline card intact and answer from the bot privately."""
        user_id = chosen.get("from", {}).get("id")
        if not isinstance(user_id, int) or not self.user_allowed(user_id):
            return
        token = str(chosen.get("result_id") or "")
        stored = self.state.get_inline_item(token)
        if not stored:
            return
        self.callback_message_id = None
        self.callback_inline_message_id = ""
        self.capture_screen = True
        try:
            if stored.get("kind") == "catalog":
                query = str(stored.get("query") or "").strip()
                if query:
                    self.show_search(user_id, query)
            elif stored.get("kind") == "release" and isinstance(stored.get("release"), dict):
                release = Release.from_dict(stored["release"])
                self.state.put_search(user_id, [release], query=release.title)
                self.render_search_results(user_id, release.title, [release])
        finally:
            self.callback_message_id = None
            self.callback_inline_message_id = ""
            self.capture_screen = False

    def _process_inline_query(
        self,
        query_id: str,
        text: str,
        cache_key: str | None = None,
        user_id: int | None = None,
    ) -> None:
        user_id = self.owner_id if user_id is None else user_id
        cache_key = cache_key or f"catalog:{text.casefold() or 'popular'}"
        try:
            with self.inline_dispatch_lock:
                latest = self.latest_inline_query_ids.get(user_id)
                if latest and latest != query_id:
                    return
                cached = self.inline_cache.get(cache_key)
            if (cached and cached[0] > time.monotonic() and
                    all(self.state.get_inline_item(item["id"]) for item in cached[1])):
                results = cached[1]
            elif not self.catalog_limit.allow("catalog"):
                results = []
            elif self.catalog:
                titles = self.catalog.search(text) if text else self.catalog.popular()
                results = self._catalog_inline_results(titles)
            else:
                results = self._release_inline_results(self._fast_inline_release_search(text))
            with self.inline_dispatch_lock:
                now = time.monotonic()
                self.inline_cache = OrderedDict((key, value) for key, value in self.inline_cache.items() if value[0] > now)
                if results:
                    self.inline_cache[cache_key] = (now + (1800 if not text else 300), results)
                    self.inline_cache.move_to_end(cache_key)
                while len(self.inline_cache) > 256:
                    self.inline_cache.popitem(last=False)
                latest = self.latest_inline_query_ids.get(user_id)
                if latest and latest != query_id:
                    return
            self.answer_inline_query(query_id, results, cache_time=300 if not text else 30)
        except Exception:
            LOG.warning("Inline search failed", exc_info=True)
            try:
                self.answer_inline_query(query_id, [], cache_time=1)
            except Exception:
                LOG.debug("Could not send empty inline response", exc_info=True)

    def is_owner(self, update: dict[str, Any]) -> bool:
        callback = update.get("callback_query", {})
        message = update.get("message") or callback.get("message") or {}
        sender = update.get("message", {}).get("from") or callback.get("from") or {}
        chat_id = message.get("chat", {}).get("id")
        inline_callback = bool(callback.get("inline_message_id"))
        sender_id = sender.get("id")
        allowed = isinstance(sender_id, int) and self.user_allowed(sender_id)
        private_chat = chat_id == sender_id
        if not allowed or (not private_chat and not inline_callback):
            if callback.get("id"):
                self.answer_callback(callback["id"], "Открой личный чат с ботом.", alert=True)
            elif isinstance(chat_id, int):
                self.send(chat_id, "⛔ Используй бота в личном чате.")
            return False
        return True

    def show_account(self, chat_id: int) -> None:
        self.state.set_mode(chat_id, None)
        if self.has_torbox(chat_id):
            source = "ключ из конфигурации владельца" if chat_id == self.owner_id and not (
                self.credentials and self.credentials.api_key(chat_id)
            ) else "личный API key"
            keyboard = [[{"text": "🔄 Заменить ключ", "callback_data": "connect"}]]
            if self.credentials and self.credentials.api_key(chat_id):
                keyboard.append([{"text": "🗑 Отключить TorBox", "callback_data": "disconnectask"}])
            keyboard.append([{"text": "🔐 Мои данные", "callback_data": "privacy"}])
            keyboard.append([self.home_button()])
            self.send(
                chat_id,
                "🔑 <b>TorBox подключён</b>\n\n"
                f"Используется {source}. Сам ключ бот никогда не показывает в сообщениях.",
                keyboard,
            )
            return
        self.send(
            chat_id,
            "🔑 <b>Подключи свой TorBox</b>\n\n"
            "У каждого пользователя свой список загрузок, лимиты и уведомления. "
            "Нажми кнопку ниже и отправь API key из TorBox → Settings → API Keys.",
            [[{"text": "➕ Подключить TorBox", "callback_data": "connect"}],
             [{"text": "🔐 Мои данные", "callback_data": "privacy"}], [self.home_button()]],
        )

    def prompt_connect(self, chat_id: int) -> None:
        if not self.credentials:
            self.send(chat_id, "Подключение пользовательских ключей выключено администратором.")
            return
        if self.callback_inline_message_id:
            self.send(
                chat_id,
                "🔐 Подключение TorBox выполняется только в личном чате с ботом.",
                [[{"text": "Открыть личный чат", "url": self._deep_link("connect")}]],
            )
            return
        self.state.set_mode(chat_id, "torbox_api_key")
        self.send(
            chat_id,
            "🔐 <b>Отправь TorBox API key</b>\n\n"
            "Открой сайт TorBox → Settings → API Keys и скопируй свой ключ. "
            "Отправь его следующим сообщением в этот чат.\n\n"
            "Я сразу удалю сообщение, проверю ключ и сохраню его в защищённом файле сервера. "
            "Администратор сервера имеет доступ к ключу. "
            "Не отправляй ключ в групповой чат.\n\nДля отмены: /cancel",
            [[self.guide_button()],
             [{"text": "Отмена", "callback_data": "account"}, {"text": "🔐 Мои данные", "callback_data": "privacy"}]],
        )

    def connect_torbox(self, chat_id: int, api_key: str) -> None:
        api_key = api_key.strip()
        if not 16 <= len(api_key) <= 256 or any(character.isspace() for character in api_key):
            self.send(
                chat_id,
                "❌ Ключ выглядит некорректно. Отправь API key без пробелов или нажми /cancel.",
                [[{"text": "Отмена", "callback_data": "account"}]],
            )
            return
        if not self.credentials:
            self.state.set_mode(chat_id, None)
            self.send(chat_id, "Подключение пользовательских ключей выключено администратором.")
            return
        try:
            client = self.torbox_factory(api_key)
            client.list_torrents(limit=1)
        except Exception:
            LOG.warning("TorBox credential validation failed for Telegram user %s", chat_id)
            self.send(
                chat_id,
                "❌ TorBox не принял этот ключ. Проверь его в Settings → API Keys и отправь ещё раз.\n\n"
                "Если TorBox временно недоступен, можно повторить позже или нажать /cancel.",
                [[{"text": "Отмена", "callback_data": "account"}]],
            )
            return
        previous_key = self.credentials.api_key(chat_id)
        self.credentials.set_api_key(chat_id, api_key)
        self.forget_log_key(previous_key)
        PRIVATE_LOG.add_secret(api_key)
        self._torbox_clients[chat_id] = client
        self.state.reset_account(chat_id)
        self.last_notification_poll.pop(chat_id, None)
        self.last_health_poll.pop(chat_id, None)
        self.state.set_mode(chat_id, None)
        self.send(
            chat_id,
            "✅ <b>Личный TorBox подключён</b>\n\nТеперь поиск, добавление, список загрузок и уведомления работают с твоим аккаунтом.",
            ([[{"text": "＋ Добавить выбранный релиз", "callback_data": "confirm"}]]
             if self.state.data["pending"].get(str(chat_id)) else []) + self.menu_keyboard(),
        )

    def show_home(self, chat_id: int) -> None:
        self.state.set_mode(chat_id, None)
        connected = self.has_torbox(chat_id)
        if connected:
            self.send(
                chat_id,
                "👋 Привет! Это <b>TorBox</b>.\nНайти. Добавить. Открыть.\n\n"
                "Отправь название фильма или сериала — подберу релизы. "
                "Готовые файлы открываются в «Мой TorBox».\n\n"
                "Хочешь смотреть через Infuse? Нажми «📖 Инструкция» — "
                "там ссылки и настройка по шагам.\n\n"
                "🟢 Аккаунт подключён",
                self.menu_keyboard(),
            )
            return
        self.send(
            chat_id,
            "👋 Привет! Это <b>TorBox</b>.\n\n"
            "Помогу найти фильм или сериал и добавить его в твой TorBox. "
            "TorBox загрузит файлы в облако, а Infuse поможет смотреть их "
            "на iPhone, iPad, Mac и Apple TV.\n\n"
            "<b>С чего начать</b>\n"
            "1. Создай аккаунт TorBox — можно начать с Essential за $3/месяц.\n"
            "2. Подключи свой TorBox к боту по API-ключу.\n"
            "3. Настрой Infuse по инструкции ниже.\n\n"
            "📖 В инструкции — что скачать, ссылки и все настройки по шагам. "
            "Infuse Pro при необходимости оплачивается отдельно.\n\n"
            "Поиск доступен ещё до подключения TorBox.",
            [[self.guide_button()],
             [{"text": "🔑 Подключить TorBox", "callback_data": "connect"}],
             [self.search_button()],
             [{"text": BTN_HELP, "callback_data": "help"}]],
        )

    def show_privacy(self, chat_id: int) -> None:
        self.state.set_mode(chat_id, None)
        self.send(chat_id,
            "🔐 <b>Твои данные</b>\n\n"
            "Бот хранит твой Telegram ID, ключ TorBox, настройки, последние 8 поисков, "
            "избранное и состояние уведомлений. Ключ даёт доступ к твоему TorBox: "
            "он хранится на сервере без шифрования, в файле с ограниченными правами. "
            "Администратор сервера технически может его прочитать.\n\n"
            "Сообщение с ключом удаляется перед проверкой. Запросы поиска передаются "
            "каталогу фильмов и подключённым трекерам; ключ TorBox отправляется только TorBox.\n\n"
            "История поиска и избранное неактивных пользователей очищаются через 30 дней. "
            "Ключ остаётся до отключения аккаунта. /delete_data удаляет личные данные из бота; "
            "файлы в TorBox остаются. Ключ также можно отозвать в настройках TorBox.",
            [[{"text": "Удалить мои данные", "callback_data": "forgetask"}], [self.home_button()]],
        )

    def forget_prompt(self, chat_id: int) -> None:
        self.send(chat_id, "Удалить ключ, историю, избранное и настройки из бота? Загрузки в TorBox останутся.",
                  [[{"text": "Да, удалить мои данные", "callback_data": "forgetconfirm"},
                    {"text": "Отмена", "callback_data": "account"}]])

    def forget_user(self, chat_id: int) -> None:
        previous_key = self.credentials.api_key(chat_id) if self.credentials else None
        if self.credentials:
            self.credentials.remove_api_key(chat_id)
        self.forget_log_key(previous_key)
        self._torbox_clients.pop(chat_id, None)
        self.last_notification_poll.pop(chat_id, None)
        self.last_health_poll.pop(chat_id, None)
        self.state.forget_user(chat_id)
        self.capture_screen = False
        text = "✅ Ключ и личные данные удалены из бота. Загрузки в TorBox сохранены."
        if chat_id == self.owner_id and self.torbox is not None:
            text += " Резервный ключ владельца из конфигурации сервера остаётся активным."
        self._send_new(chat_id, text)

    def show_help(self, chat_id: int) -> None:
        self.state.set_mode(chat_id, None)
        self.send(chat_id,
            "<b>От поиска до просмотра</b>\n\n"
            "Впервые здесь? «📖 Инструкция» объяснит, как подключить TorBox "
            "и настроить Infuse для просмотра.\n\n"
            "<b>1. Найди</b>\nОтправь название, при необходимости добавь год. "
            "Поиск с постерами доступен отдельной кнопкой, если каталог подключён.\n\n"
            "<b>2. Выбери релиз</b>\nСравни качество, озвучку и размер. "
            "«В кэше» означает, что TorBox уже хранит файлы. "
            "CAM и TS — экранные копии, даже если указано 1080p.\n\n"
            "<b>3. Добавь и открой</b>\n«Добавить» отправляет релиз в твой TorBox. "
            "Когда он готов, открой загрузку → «Файлы» → нужную серию. "
            "Ссылка открывается в браузере; поддержку видео определяет устройство.\n\n"
            "⭐ Избранное сохраняет название для повторного поиска.\n"
            "🕘 История — последние 8 запросов.\n"
            "🗑 Удаление загрузки требует подтверждения.\n\n"
            "/search — поиск · /torrents — загрузки · /cancel — выйти из ввода",
            [[self.guide_button()], [self.search_button(), self.home_button()]],
        )

    def prompt_search(
        self, chat_id: int, *, message_id: int | None = None, inline_message_id: str = "",
    ) -> None:
        self.state.set_mode(chat_id, "catalog_search" if self.catalog else "search")
        keyboard = []
        if self.inline_enabled and self.catalog:
            keyboard.append([self.search_button("🔎 Поиск с постерами")])
        keyboard.append([
            {"text": "🕘 История", "callback_data": "history"},
            {"text": "⭐ Избранное", "callback_data": "favorites"},
        ])
        keyboard.append([self.search_settings_button(), self.home_button()])
        self.render(chat_id,
            "🔎 <b>Что найти?</b>\n\nОтправь название фильма или сериала обычным сообщением.\n"
            "Например: <i>Дюна 2021</i> или <i>Разделение 2 сезон</i>.",
            keyboard, message_id=message_id, inline_message_id=inline_message_id,
        )

    def query_button(self, chat_id: int, query: str, text: str) -> dict[str, str]:
        token = "q_" + hashlib.sha256(f"{chat_id}:{query}".encode()).hexdigest()[:16]
        self.state.put_inline_item(token, {"kind": "query", "query": query, "owner": chat_id})
        return {"text": text, "callback_data": f"retry:{token}"}

    def search_prefs(self, chat_id: int) -> dict[str, Any]:
        return search_preferences(self.state.ui_value(chat_id, "search_preferences", {}))

    def search_settings_button(self) -> dict[str, str]:
        return {"text": "⚙️ Настройки поиска", "callback_data": "searchsettings"}

    def show_search_settings(self, chat_id: int) -> None:
        self.state.set_mode(chat_id, None)
        prefs = self.search_prefs(chat_id)
        keyboard = []
        for values, field in [(SEARCH_QUALITIES, "quality"), (SEARCH_AUDIO, "audio")]:
            buttons = [{"text": ("✓ " if prefs[field] == key else "") + label,
                        "callback_data": f"sp:{field}:{key}"} for key, label in values.items()]
            keyboard.extend(buttons[i:i+2] for i in range(0, len(buttons), 2))
        keyboard.extend([
            [{"text": ("✓ " if prefs["complete"] else "") + "Комплекты и полные сезоны первыми",
              "callback_data": f"sp:complete:{int(not prefs['complete'])}"}],
            [{"text": ("✓ " if prefs["fallback"] else "") + "Показывать другие качество / озвучку",
              "callback_data": f"sp:fallback:{int(not prefs['fallback'])}"}],
            [{"text": "Сбросить: 1080p и дубляж", "callback_data": "sp:reset:1"}],
        ])
        if self.state.get_search(chat_id):
            keyboard.append([{"text": "← К результатам", "callback_data": "results"}])
        keyboard.append([self.search_button(), self.home_button()])
        self.send(chat_id,
                  "⚙️ <b>Настройки поиска</b>\n\n"
                  f"Качество: <b>{SEARCH_QUALITIES[prefs['quality']]}</b>\n"
                  f"Озвучка: <b>{SEARCH_AUDIO[prefs['audio']]}</b>\n\n"
                  "Приоритет — доступные раздачи в выбранном качестве. "
                  + ("Полные сезоны выше отдельных серий, а среди них — выбранная озвучка и комплекты сезонов."
                     if prefs["complete"] else "Затем — выбранная озвучка, кэш и сиды. Приоритет полных сезонов выключен.")
                  + "\n\n"
                  + ("Если точного совпадения нет, покажу подписанные альтернативы."
                     if prefs["fallback"] else "Другие качество и озвучка скрыты. Некоторые сезоны могут не попасть под настройки.")
                  + "\n\nИзменения сохраняются сразу и перестраивают текущую выдачу. Дубляж определяется по данным источника; DUAL сам по себе его не подтверждает.",
                  keyboard)

    def change_search_preference(self, chat_id: int, field: str, value: str) -> None:
        prefs = self.search_prefs(chat_id)
        if (field == "quality" and value in SEARCH_QUALITIES) or (field == "audio" and value in SEARCH_AUDIO):
            prefs[field] = value
        elif field in {"complete", "fallback"} and value in {"0", "1"}:
            prefs[field] = value == "1"
        elif field == "reset":
            prefs = dict(SEARCH_DEFAULTS)
        else:
            raise ValueError("Invalid search preference")
        self.state.set_ui(chat_id, search_preferences=prefs)
        stored = self.state.get_search(chat_id)
        if stored:
            query, sources, _ = stored
            candidates = self.state.search_candidates(chat_id)
            season_scope = self.state.ui_value(chat_id, "search_season", "all")
            self.state.put_search(chat_id, personalized_releases(candidates, prefs), query=query,
                                  source_status=sources, candidates=candidates)
            self.state.set_ui(chat_id, search_season=season_scope)
        self.show_search_settings(chat_id)

    def show_seasons(self, chat_id: int, revision: str, page: int = 0) -> None:
        stored = self.state.get_search(chat_id)
        if not stored or revision != self.state.search_revision(chat_id):
            self.send(chat_id, "Этот поиск устарел. Открой актуальные результаты.",
                      [[{"text": "К результатам", "callback_data": "results"}]])
            return
        query, _, releases = stored
        seasons = sorted({n for r in releases for n in release_season_numbers(r.title)})
        pages = max(1, (len(seasons) + 11) // 12)
        page = max(0, min(page, pages - 1))
        lines = [f"📺 <b>{html.escape(query)}</b>", "\nВыбери сезон — повторный поиск не нужен."]
        keyboard = [[{"text": "Все найденные сезоны", "callback_data": f"season:{revision}:all"}]]
        if any(len(release_season_numbers(r.title)) > 1 for r in releases):
            keyboard.append([{"text": "📦 Комплекты сезонов", "callback_data": f"season:{revision}:packs"}])
        for season in seasons[page*12:(page+1)*12]:
            matching = [r for r in releases if season in release_season_numbers(r.title)]
            prefs = self.search_prefs(chat_id)
            suitable = [r for r in matching if release_matches_preferences(r, prefs)]
            note = "" if suitable else " · другое качество / озвучка"
            if all(release_is_partial_season(r.title) for r in matching):
                note += " · отдельные серии"
            lines.append(f"Сезон {season}: вариантов {len(matching)}{note}")
            keyboard.append([{"text": f"Сезон {season}" + (" · есть совпадение" if suitable else " · альтернативы"),
                              "callback_data": f"season:{revision}:{season}"}])
        gaps = sorted(set(range(1, max(seasons, default=0)+1)) - set(seasons))
        if gaps:
            lines.append(f"\nВ выдаче не найдены сезоны: {season_summary(gaps)}.")
        lines.append("\nПоказано покрытие найденных релизов. Это не подтверждение, что вышли и найдены все сезоны сериала.")
        nav = []
        if page:
            nav.append({"text": "← Назад", "callback_data": f"seasons:{revision}:{page-1}"})
        if page+1 < pages:
            nav.append({"text": "Далее →", "callback_data": f"seasons:{revision}:{page+1}"})
        if nav:
            keyboard.append(nav)
        keyboard.append([{"text": "← К результатам", "callback_data": "results"}, self.search_settings_button()])
        self.send(chat_id, "\n".join(lines), keyboard)

    def show_saved_queries(self, chat_id: int, *, favorites: bool = False) -> None:
        self.state.set_mode(chat_id, None)
        queries = self.state.ui_value(chat_id, "favorites" if favorites else "history", [])
        title = "⭐ Избранное" if favorites else "🕘 История поиска"
        text = f"<b>{title}</b>\n\n"
        text += ("Выбери название, чтобы найти актуальные релизы." if queries else
                 "Здесь появятся сохранённые названия. Нажми «В избранное» под результатами поиска."
                 if favorites else "Здесь появятся последние 8 поисков. Начни с названия фильма или сериала.")
        keyboard = [[self.query_button(chat_id, q, compact_title(q, 48))] for q in queries]
        if queries and not favorites:
            keyboard.append([{"text": "Очистить историю", "callback_data": "historyclear"}])
        keyboard.append([self.search_button(), self.home_button()])
        self.send(chat_id, text, keyboard)

    def show_catalog_choices(
        self,
        chat_id: int,
        query: str,
        *,
        message_id: int | None = None,
    ) -> None:
        query = re.sub(r"\s+", " ", query).strip()
        if not query:
            self.prompt_search(chat_id, message_id=message_id)
            return
        if len(query) > MAX_QUERY_LENGTH:
            self.render(
                chat_id,
                f"Название слишком длинное. Оставь не больше {MAX_QUERY_LENGTH} символов.",
                [[self.search_button("🔎 Новый поиск"), self.home_button()]],
                message_id=message_id,
            )
            return
        if not self.allow_search(chat_id):
            return
        if not self.catalog:
            self.show_search(chat_id, query, message_id=message_id)
            return
        if not self.catalog_limit.allow("catalog"):
            self.show_search(chat_id, query, message_id=message_id)
            return
        loading = self.render(chat_id, f"🎬 Ищу фильм <b>{html.escape(query)}</b>…", message_id=message_id)
        if message_id is None and isinstance(loading, dict):
            try:
                message_id = int(loading.get("message_id"))
            except (TypeError, ValueError):
                message_id = None
        try:
            titles = self.catalog.search(query)[:8]
        except Exception:
            LOG.exception("Catalog search failed")
            self.render(
                chat_id,
                "Каталог фильмов временно недоступен. Можно поискать название сразу на трекерах.",
                [[self.query_button(chat_id, query, "🔎 Искать на трекерах")],
                 [self.search_button("Другой запрос"), self.home_button()]],
                message_id=message_id,
            )
            return
        if not titles:
            self.render(
                chat_id,
                "Ничего похожего не нашёл. Попробуй другое написание.",
                [[self.query_button(chat_id, query, "🔎 Искать на трекерах")],
                 [self.search_button("Другой запрос"), self.home_button()]],
                message_id=message_id,
            )
            return

        self.state.set_mode(chat_id, None)
        lines = [f"🎬 <b>Выбери фильм</b>\nПо запросу «{html.escape(query)}» нашлось:"]
        keyboard: list[list[dict[str, str]]] = []
        stored: dict[str, dict[str, Any]] = {}
        for index, title in enumerate(titles, start=1):
            token = f"kp_{title.catalog_id}"
            stored[token] = {"kind": "catalog", "query": title.search_query, "title": title.name}
            display_name = title.name
            if title.original_name:
                display_name += f" ({title.original_name})"
            meta = [self._catalog_type_label(title.media_type)]
            if title.rating and title.rating.casefold() not in {"null", "none"}:
                meta.append(f"КП {title.rating}")
            if title.year:
                meta.append(title.year)
            extra = [*title.countries[:1], *title.genres[:2]]
            lines.append(
                f"\n<b>{index}. {html.escape(compact_title(display_name, 90))}</b>\n"
                f"{html.escape(' · '.join(meta))}"
                + (f"\n{html.escape(' · '.join(extra))}" if extra else "")
            )
            keyboard.append([{
                "text": f"{index} · {compact_title(title.name, 42)}" + (f" ({title.year})" if title.year else ""),
                "callback_data": f"catalog:{token}",
            }])
        self.state.put_inline_items(stored)
        keyboard.append([self.search_button("🔎 Другой запрос"), self.home_button()])
        self.render(chat_id, "\n".join(lines), keyboard, message_id=message_id)

    def render_search_results(
        self, chat_id: int, query: str, releases: list[Release], source_status: str = "", *,
        message_id: int | None = None, inline_message_id: str = "",
        page: int | None = None, filter_by: str | None = None,
    ) -> Any:
        page = self.state.ui_value(chat_id, "search_page", 0) if page is None else page
        filter_by = self.state.ui_value(chat_id, "search_filter", "all") if filter_by is None else filter_by
        revision = self.state.search_revision(chat_id)
        prefs = self.search_prefs(chat_id)
        indexed = list(enumerate(releases))
        season_scope = self.state.ui_value(chat_id, "search_season", "all")
        if season_scope == "packs":
            indexed = [(i, r) for i, r in indexed if len(release_season_numbers(r.title)) > 1]
        elif str(season_scope).isdigit():
            indexed = [(i, r) for i, r in indexed if int(season_scope) in release_season_numbers(r.title)]
        if not prefs["fallback"]:
            indexed = [(i, r) for i, r in indexed if release_matches_preferences(r, prefs)]
        if filter_by == "cached":
            indexed = [(i, r) for i, r in indexed if r.cached is True]
        elif filter_by == "hd":
            indexed = [(i, r) for i, r in indexed if re.search(r"\b(?:1080p|2160p|4k)\b", r.title, re.I)
                       and not re.search(r"\b(?:camrip|hdcam|cam|ts|tsrip|telesync)\b", r.title, re.I)]
        elif filter_by == "small":
            indexed = sorted(indexed, key=lambda pair: pair[1].size_bytes if pair[1].size_bytes > 0 else float("inf"))
        pages = max(1, (len(indexed) + SEARCH_PAGE_SIZE - 1) // SEARCH_PAGE_SIZE)
        page = max(0, min(page, pages - 1))
        self.state.set_ui(chat_id, search_page=page, search_filter=filter_by)
        lines = [f"🔎 <b>{html.escape(query)}</b>", f"Нашёл релизов: <b>{len(releases)}</b> · в подборке {len(indexed)}",
                 f"Приоритет: {SEARCH_QUALITIES[prefs['quality']]} · {SEARCH_AUDIO[prefs['audio']]}"]
        seasons = sorted({n for r in releases for n in release_season_numbers(r.title)})
        if seasons:
            lines.append(f"Найдены сезоны: {season_summary(seasons)}")
        if season_scope != "all":
            lines.append("📦 Комплекты сезонов" if season_scope == "packs" else f"📺 Сезон {season_scope}")
        if "ошибка" in source_status:
            lines.append("Часть источников недоступна. Здесь результаты тех, что ответили.")
        keyboard = []
        for index, release in indexed[page * SEARCH_PAGE_SIZE:(page + 1) * SEARCH_PAGE_SIZE]:
            quality = compact_title(video_quality_summary(release, release.details_text), 90)
            voice = compact_title(voiceover_summary(release, release.details_text), 80)
            availability = "⚡ В кэше TorBox" if release.cached is True else seed_health_summary(release.seeders)
            coverage = release_season_numbers(release.title)
            coverage_line = (f"📦 Сезоны {season_summary(coverage)}" if len(coverage) > 1 else f"📺 Сезон {coverage[0]}") if coverage else ""
            if coverage and release_is_partial_season(release.title):
                coverage_line += " · отдельные серии"
            alternate = "Альтернатива по качеству / озвучке" if not release_matches_preferences(release, prefs) else ""
            lines.append(
                f"\n<b>{index + 1}. {html.escape(compact_title(release.title, 85))}</b>\n"
                + (coverage_line + "\n" if coverage_line else "")
                + f"{html.escape(quality)} · {html.escape(release.size_text)}\n"
                f"Озвучка: {html.escape(voice)}\n"
                f"{availability} · {html.escape(compact_title(release.source_name, 30))}"
                + (f"\n<i>{alternate}</i>" if alternate else "")
            )
            keyboard.append([
                {"text": f"{index + 1} · Подробнее", "callback_data": f"info:{index}:{revision}"},
                {"text": f"＋ {index + 1} · Добавить", "callback_data": f"add:{index}:{revision}"},
            ])
        if not indexed:
            lines.append("\nПод эти настройки и фильтры релизов нет. Выбери «Все» или разреши альтернативы в настройках поиска.")
        nav = []
        if page:
            nav.append({"text": "← Назад", "callback_data": f"sr:{revision}:{page - 1}:{filter_by}"})
        if pages > 1:
            nav.append({"text": f"{page + 1} / {pages}", "callback_data": "noop"})
        if page + 1 < pages:
            nav.append({"text": "Далее →", "callback_data": f"sr:{revision}:{page + 1}:{filter_by}"})
        if nav:
            keyboard.append(nav)
        if seasons:
            keyboard.append([{"text": "📺 Выбрать сезон / комплект", "callback_data": f"seasons:{revision}:0"}])
        for row in [[("all", "Все"), ("cached", "В кэше")], [("hd", "1080p+ без CAM/TS"), ("small", "Меньше размер")]]:
            keyboard.append([{"text": ("✓ " if filter_by == key else "") + label,
                              "callback_data": f"sr:{revision}:0:{key}"} for key, label in row])
        saved = query in self.state.ui_value(chat_id, "favorites", [])
        keyboard.append([{"text": "★ Убрать из избранного" if saved else "☆ В избранное",
                          "callback_data": f"favorite:{revision}"}])
        keyboard.append([self.search_settings_button()])
        keyboard.append([self.search_button("🔎 Новый поиск"), self.home_button()])
        return self.render(chat_id, "\n".join(lines), keyboard,
                           message_id=message_id, inline_message_id=inline_message_id)

    def allow_search(self, chat_id: int) -> bool:
        if getattr(self._render_context, "search_permitted", False):
            return True
        if not self.search_limit.allow(chat_id):
            self.send(chat_id, "Слишком частый поиск. Подожди 15 секунд и повтори запрос.", [[self.home_button()]])
            return False
        if not self.global_search_limit.allow("search"):
            self.send(chat_id, "Сейчас много поисковых запросов. Попробуй через минуту.", [[self.home_button()]])
            return False
        self._render_context.search_permitted = True
        return True

    def show_search(
        self,
        chat_id: int,
        query: str,
        *,
        message_id: int | None = None,
        inline_message_id: str = "",
    ) -> None:
        query = re.sub(r"\s+", " ", query).strip()
        if not query:
            self.prompt_search(chat_id, message_id=message_id, inline_message_id=inline_message_id)
            return
        if len(query) > MAX_QUERY_LENGTH:
            self.render(
                chat_id,
                f"Название слишком длинное. Оставь не больше {MAX_QUERY_LENGTH} символов.",
                [[self.search_button("🔎 Новый поиск"), self.home_button()]],
                message_id=message_id,
                inline_message_id=inline_message_id,
            )
            return
        if not self.allow_search(chat_id):
            return
        self.state.set_mode(chat_id, None)
        self.state.remember_query(chat_id, query)
        self.state.put_search(chat_id, [], query=query)
        self.state.set_ui(chat_id, search_page=0, search_filter="all")
        retry_keyboard = [[self.query_button(chat_id, query, "🔄 Повторить поиск")],
                          [self.search_button("Изменить запрос"), self.home_button()]]
        loading = self.render(
            chat_id,
            f"🔎 <b>{html.escape(query)}</b>\n\nИщу релизы на трекерах…\nОбычно это занимает до минуты.",
            message_id=message_id,
            inline_message_id=inline_message_id,
        )
        if message_id is None and not inline_message_id and isinstance(loading, dict):
            try:
                message_id = int(loading.get("message_id"))
            except (TypeError, ValueError):
                message_id = None
        try:
            search = getattr(self.nnm, "search_candidates", self.nnm.search)
            releases = search(query)
        except Exception:
            LOG.exception("NNM search failed")
            self.render(
                chat_id,
                "Источники поиска сейчас не отвечают. Запрос сохранён — попробуй ещё раз.",
                retry_keyboard,
                message_id=message_id,
                inline_message_id=inline_message_id,
            )
            return
        source_status = ""
        status_summary = getattr(self.nnm, "status_summary", None)
        if callable(status_summary):
            source_status = str(status_summary())
        if releases and self.has_torbox(chat_id):
            try:
                cached = self.torbox_for(chat_id).cached_hashes([release.info_hash for release in releases])
                cached = {normalize_info_hash(value) for value in cached}
                releases = [replace(r, cached=normalize_info_hash(r.info_hash) in cached if r.info_hash else r.cached) for r in releases]
            except Exception:
                LOG.warning("Could not enrich search with TorBox cache")
        if not releases:
            statuses = getattr(self.nnm, "last_status", {})
            all_failed = bool(statuses) and all(value == "ошибка" for value in statuses.values())
            self.render(
                chat_id,
                ("Источники поиска сейчас не отвечают. Запрос сохранён — попробуй ещё раз." if all_failed else
                 f"По запросу «{html.escape(query)}» релизов пока нет.\n\nПопробуй оригинальное название, добавь год или убери номер серии."),
                retry_keyboard,
                message_id=message_id,
                inline_message_id=inline_message_id,
            )
            return
        candidates = releases
        releases = personalized_releases(candidates, self.search_prefs(chat_id))
        self.state.put_search(chat_id, releases, query=query, source_status=source_status, candidates=candidates)
        self.render_search_results(
            chat_id,
            query,
            releases,
            source_status,
            message_id=message_id,
            inline_message_id=inline_message_id,
        )

    def torrent_line(self, item: dict[str, Any]) -> str:
        icon, status = status_label(item)
        progress = progress_percent(item.get("progress"))
        size = human_size(int(item.get("size") or 0))
        speed = int(item.get("download_speed") or 0)
        speed_text = f" · {human_size(speed)}/с" if speed > 0 else ""
        summary = f"Готово · {size}" if torrent_ready(item) else f"{html.escape(status)} · {progress:.0f}% · {size}{speed_text}"
        return (
            f"{icon} <b>{html.escape(compact_title(str(item.get('name') or 'Без названия'), 90))}</b>\n"
            f"{summary}"
        )

    def library_back(self, chat_id: int) -> dict[str, str]:
        return {"text": "← К загрузкам", "callback_data": f"list:{self.state.ui_value(chat_id, 'library_page', 0)}"}

    def show_torrents(self, chat_id: int, page: int = 0, *, refresh: bool = False, filter_by: str | None = None) -> None:
        self.state.set_mode(chat_id, None)
        if not self.has_torbox(chat_id):
            self.show_account(chat_id)
            return
        try:
            items = self.torbox_for(chat_id).list_torrents(limit=1000, bypass_cache=refresh)
        except Exception:
            LOG.warning("Could not list TorBox torrents")
            self.send(chat_id, "Не удалось загрузить список TorBox. Попробуй обновить его чуть позже.",
                      [[{"text": "🔄 Повторить", "callback_data": f"refresh:{page}"}], [self.home_button()]])
            return
        items = sorted(items, key=lambda item: str(item.get("created_at") or ""), reverse=True)
        filter_by = filter_by or self.state.ui_value(chat_id, "library_filter", "all")
        ready_count = sum(torrent_ready(item) for item in items)
        visible_items = [item for item in items if filter_by == "all" or
                         (torrent_ready(item) if filter_by == "ready" else not torrent_ready(item))]
        pages = max(1, (len(visible_items) + TORBOX_PAGE_SIZE - 1) // TORBOX_PAGE_SIZE)
        page = max(0, min(page, pages - 1))
        self.state.set_ui(chat_id, library_page=page, library_filter=filter_by)
        lines = ["📥 <b>Мой TorBox</b>", f"Готово: {ready_count} · В процессе: {len(items) - ready_count}"]
        keyboard = [[{"text": ("✓ " if filter_by == key else "") + label,
                      "callback_data": f"library:{key}:0"}
                     for key, label in [("all", "Все"), ("ready", "Готово"), ("active", "В процессе")]]]
        if not items:
            lines = ["📥 <b>TorBox пока пуст</b>", "\nНайди фильм или сериал и нажми «Добавить». Загрузка появится здесь."]
        elif not visible_items:
            lines.append("\nВ этом разделе пока нет загрузок. Все файлы доступны во вкладке «Все».")
        for number, item in enumerate(visible_items[page * TORBOX_PAGE_SIZE:(page + 1) * TORBOX_PAGE_SIZE], 1):
            try:
                torrent_id = safe_torrent_id(item.get("id"))
            except ValueError:
                continue
            lines.append("\n" + self.torrent_line(item))
            keyboard.append([{"text": f"{number} · {compact_title(str(item.get('name') or 'Без названия'), 43)}",
                              "callback_data": f"tinfo:{torrent_id}"}])
        navigation = []
        if page:
            navigation.append({"text": "← Назад", "callback_data": f"list:{page - 1}"})
        if pages > 1:
            navigation.append({"text": f"{page + 1} / {pages}", "callback_data": "noop"})
        if page + 1 < pages:
            navigation.append({"text": "Далее →", "callback_data": f"list:{page + 1}"})
        if navigation:
            keyboard.append(navigation)
        keyboard.append([{"text": "🔄 Обновить", "callback_data": f"refresh:{page}"}])
        keyboard.append([self.search_button(), self.home_button()])
        self.send(chat_id, "\n".join(lines), keyboard)

    def show_torrent(self, chat_id: int, torrent_id: int, *, notice: str = "") -> None:
        back = [self.library_back(chat_id), self.home_button()]
        try:
            item = self.torbox_for(chat_id).get_torrent(torrent_id, bypass_cache=True)
        except Exception:
            self.send(chat_id, "Не удалось загрузить карточку. Попробуй ещё раз.",
                      [[{"text": "🔄 Повторить", "callback_data": f"tinfo:{torrent_id}"}], back])
            return
        if not item:
            self.send(chat_id, "Загрузка уже удалена или недоступна.", [back])
            return
        icon, status = status_label(item)
        ready = torrent_ready(item)
        files = item.get("files") if isinstance(item.get("files"), list) else []
        lines = [f"{icon} <b>{html.escape(compact_title(str(item.get('name') or 'Без названия'), 220))}</b>"]
        if notice:
            lines.append(notice)
        lines.append(f"\n{human_size(int(item.get('size') or 0))} · Файлов: {len(files)}")
        keyboard = []
        raw_state = str(item.get("download_state") or "").casefold()
        if ready:
            lines.append("Готово в TorBox. Выбери файл, чтобы получить ссылку.")
            keyboard.append([{"text": f"📂 Открыть файлы · {len(files)}", "callback_data": f"files:{torrent_id}:0"}])
        else:
            progress = progress_percent(item.get("progress"))
            lines.append(f"{html.escape(status.capitalize())} · {progress:.0f}%")
            if status == "подготовка файлов":
                lines.append("Загрузка завершена. TorBox подготавливает файлы — обнови карточку чуть позже.")
            elif "no seeds" in raw_state or "stalled" in raw_state:
                lines.append("Нет раздающих. Можно обновить подключение или выбрать другой релиз.")
            elif "pause" in raw_state:
                lines.append("Загрузка приостановлена. Нажми «Продолжить».")
            else:
                speed = int(item.get("download_speed") or 0)
                lines.append(f"{human_size(speed)}/с · Осталось: {human_duration(int(item.get('eta') or 0))}")
                lines.append("Файлы можно открыть после завершения загрузки.")
            if "pause" in raw_state:
                keyboard.append([{"text": "▶️ Продолжить", "callback_data": f"tctl:{torrent_id}:resume"}])
            if status != "подготовка файлов":
                keyboard.append([{"text": "📡 Обновить подключение", "callback_data": f"tctl:{torrent_id}:reannounce"}])
        keyboard.append([{"text": "🔄 Обновить", "callback_data": f"tinfo:{torrent_id}"},
                         {"text": "🗑 Удалить", "callback_data": f"delask:{torrent_id}"}])
        keyboard.append(back)
        self.send(chat_id, "\n".join(lines), keyboard)

    def show_files(self, chat_id: int, torrent_id: int, page: int = 0, *, file_id: int | None = None) -> None:
        back = [{"text": "← К загрузке", "callback_data": f"tinfo:{torrent_id}"}]
        try:
            torbox = self.torbox_for(chat_id)
            item = torbox.get_torrent(torrent_id, bypass_cache=True)
            if not item or not torrent_ready(item):
                self.show_torrent(chat_id, torrent_id)
                return
            files = [f for f in item.get("files", []) if isinstance(f, dict) and f.get("id") is not None]
            files.sort(key=lambda f: natural_file_key(str(f.get("name") or f.get("short_name") or "")))
            if file_id is not None:
                selected = next((f for f in files if safe_torrent_id(f["id"]) == file_id), None)
                if selected is None:
                    self.send(chat_id, "Этот файл больше не найден. Открой список заново.",
                              [[{"text": "📂 Файлы", "callback_data": f"files:{torrent_id}:{page}"}], back])
                    return
                link = torbox.download_link(torrent_id, file_id)
                name = str(selected.get("short_name") or selected.get("name") or "Файл")
                self.send(chat_id,
                          f"📄 <b>{html.escape(compact_title(name, 220))}</b>\n"
                          f"{human_size(int(selected.get('size') or 0))}\n\n"
                          "Открой ссылку в браузере или скопируй её из кнопки в свой плеер. "
                          "Если ссылка перестала работать, получи новую.",
                          [[{"text": "↗ Открыть / скачать", "url": link}],
                           [{"text": "🔄 Новая ссылка", "callback_data": f"file:{torrent_id}:{file_id}:{page}"}],
                           [{"text": "← К файлам", "callback_data": f"files:{torrent_id}:{page}"}]])
                return
            pages = max(1, (len(files) + FILE_PAGE_SIZE - 1) // FILE_PAGE_SIZE)
            page = max(0, min(page, pages - 1))
            lines = [f"📂 <b>{html.escape(compact_title(str(item.get('name') or 'Файлы'), 140))}</b>",
                     f"Файлов: {len(files)} · {page + 1}/{pages}"]
            keyboard = []
            for file in files[page * FILE_PAGE_SIZE:(page + 1) * FILE_PAGE_SIZE]:
                name = str(file.get("name") or file.get("short_name") or "Файл")
                label = compact_title(name, 100)
                lines.append(f"\n{html.escape(label)}\n{human_size(int(file.get('size') or 0))}")
                keyboard.append([{"text": compact_title(str(file.get('short_name') or name), 52),
                                  "callback_data": f"file:{torrent_id}:{safe_torrent_id(file['id'])}:{page}"}])
            if not files:
                lines.append("\nTorBox ещё подготавливает список файлов. Обнови его чуть позже.")
            nav = []
            if page:
                nav.append({"text": "← Назад", "callback_data": f"files:{torrent_id}:{page - 1}"})
            if page + 1 < pages:
                nav.append({"text": "Далее →", "callback_data": f"files:{torrent_id}:{page + 1}"})
            if nav:
                keyboard.append(nav)
            keyboard.append([{"text": "🔄 Обновить файлы", "callback_data": f"files:{torrent_id}:{page}"}])
            keyboard.append(back)
            self.send(chat_id, "\n".join(lines), keyboard)
        except Exception:
            LOG.warning("Could not open TorBox files for user %s", chat_id)
            retry = f"file:{torrent_id}:{file_id}:{page}" if file_id is not None else f"files:{torrent_id}:{page}"
            self.send(chat_id, "Не удалось получить файл из TorBox. Попробуй ещё раз чуть позже.",
                      [[{"text": "🔄 Повторить", "callback_data": retry}], back])

    @staticmethod
    def notification_key(item: dict[str, Any]) -> str:
        if item.get("id") is not None:
            return str(item["id"])
        raw = f"{item.get('created_at')}|{item.get('title')}|{item.get('message')}"
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]

    @staticmethod
    def notification_text(item: dict[str, Any]) -> str:
        title = html.escape(compact_title(str(item.get("title") or "Уведомление TorBox"), 140))
        raw_message = re.sub(r"\s+", " ", str(item.get("message") or "Без текста")).strip()
        message = html.escape(compact_title(raw_message, 480))
        created = html.escape(str(item.get("created_at") or "").replace("T", " ").replace("Z", " UTC"))
        date_line = f"\n<i>{created}</i>" if created else ""
        return f"🔔 <b>{title}</b>\n{message}{date_line}"

    def show_notifications(self, chat_id: int) -> None:
        self.state.set_mode(chat_id, None)
        if not self.has_torbox(chat_id):
            self.show_account(chat_id)
            return
        enabled = self.state.ui_value(chat_id, "notifications_enabled", True)
        toggle = [{"text": "🔕 Выключить уведомления" if enabled else "🔔 Включить уведомления",
                   "callback_data": "notifytoggle"}]
        preference = ("Автоуведомления включены." if enabled else "Автоуведомления выключены.") if self.notifications_enabled else "Автоуведомления отключены на сервере."
        try:
            items = self.torbox_for(chat_id).notifications()
        except Exception as exc:
            self.send(chat_id, f"❌ Не удалось получить уведомления: {public_error(exc)}", self.menu_keyboard())
            return
        if not items:
            self.send(
                chat_id, "🔔 <b>Уведомлений пока нет</b>\n\n" + preference,
                [[{"text": "🔄 Обновить", "callback_data": "notifications"}]] +
                ([toggle] if self.notifications_enabled else []) + [[self.home_button()]],
            )
            return
        items.sort(key=lambda item: str(item.get("created_at") or ""), reverse=True)
        visible = items[:5]
        self.state.mark_notifications(
            [self.notification_key(item) for item in items],
            self._maintenance_state_user(chat_id),
        )
        text = "<b>Последние уведомления TorBox</b>\n" + preference + "\n\n" + "\n\n".join(self.notification_text(item) for item in visible)
        self.send(
            chat_id, text,
            [[{"text": "🔄 Обновить", "callback_data": "notifications"}, {"text": "🧹 Очистить", "callback_data": "nclearask"}]] +
            ([toggle] if self.notifications_enabled else []) + [[self.home_button()]],
        )

    def poll_notifications(self, user_id: int | None = None, *, force: bool = False) -> bool:
        if not self.notifications_enabled:
            return True
        user_id = self.owner_id if user_id is None else user_id
        if not self.state.ui_value(user_id, "notifications_enabled", True):
            return True
        state_user = self._maintenance_state_user(user_id)
        now = time.monotonic()
        if not force and now - self.last_notification_poll.get(user_id, 0.0) < self.notification_poll_seconds:
            return True
        self.last_notification_poll[user_id] = now
        try:
            items = self.torbox_for(user_id).notifications()
            keys = [self.notification_key(item) for item in items]
            if not self.state.notifications_initialized(state_user):
                self.state.mark_notifications(keys, state_user)
                return True
            seen = self.state.notification_ids(state_user)
            new_items = [item for item in items if self.notification_key(item) not in seen]
            new_items.sort(key=lambda item: str(item.get("created_at") or ""))
            delivered: list[str] = []
            for item in new_items[:10]:
                self._send_new(
                    user_id, self.notification_text(item),
                    [[{"text": BTN_TORRENTS, "callback_data": "list:0"}, {"text": BTN_NOTIFICATIONS, "callback_data": "notifications"}]],
                )
                delivered.append(self.notification_key(item))
                # Persist each successful delivery so a later Telegram error
                # cannot cause an already sent notification to be repeated.
                self.state.mark_notifications(delivered, state_user)
            return True
        except Exception:
            LOG.warning("Could not poll TorBox notifications", exc_info=True)
            return False

    def poll_torrent_health(self, user_id: int | None = None, *, force: bool = False) -> bool:
        user_id = self.owner_id if user_id is None else user_id
        if not self.state.ui_value(user_id, "notifications_enabled", True):
            return True
        state_user = self._maintenance_state_user(user_id)
        now_monotonic = time.monotonic()
        if not force and now_monotonic - self.last_health_poll.get(user_id, 0.0) < 60:
            return True
        self.last_health_poll[user_id] = now_monotonic
        try:
            items = self.torbox_for(user_id).list_torrents(limit=100, bypass_cache=True)
            stalled: dict[str, dict[str, Any]] = {}
            for item in items:
                raw_state = str(item.get("download_state") or "").casefold()
                if item.get("download_finished") or item.get("cached"):
                    continue
                if "stalled" in raw_state or "no seeds" in raw_state:
                    stalled[str(safe_torrent_id(item.get("id")))] = item
            first_seen, reported = self.state.update_stalled_downloads(
                set(stalled), int(time.time()), state_user,
            )
            for torrent_id, item in stalled.items():
                if torrent_id in reported or int(time.time()) - first_seen[torrent_id] < 300:
                    continue
                name = html.escape(compact_title(str(item.get("name") or "Без названия"), 150))
                self._send_new(
                    user_id,
                    f"⚠️ <b>Загрузка не началась</b>\n\n{name}\n\n"
                    "TorBox не видит доступных сидов. Можно переподключиться "
                    "или удалить задание и выбрать другой релиз.",
                    [[
                        {"text": "📡 Переподключить", "callback_data": f"tctl:{torrent_id}:reannounce"},
                        {"text": "🗑 Удалить", "callback_data": f"delask:{torrent_id}"},
                    ], [{"text": "ℹ️ Подробнее", "callback_data": f"tinfo:{torrent_id}"}]],
                )
                self.state.mark_stall_reported(torrent_id, state_user)
            return True
        except Exception:
            LOG.warning("Could not poll TorBox torrent health", exc_info=True)
            return False

    def _maintain_user(self, user_id: int) -> None:
        lock = self.user_lock(user_id)
        if not lock.acquire(blocking=False):
            return
        try:
            if not self.has_torbox(user_id) or self.state.ui_value(user_id, "delivery_blocked", False):
                return
            self.poll_notifications(user_id)
            if not self.state.ui_value(user_id, "delivery_blocked", False):
                self.poll_torrent_health(user_id)
        finally:
            lock.release()

    def maintenance_loop(self) -> None:
        """Slow or invalid accounts never hold up other users' background checks."""
        while not STOP.is_set() and not self._closing.is_set():
            now = time.monotonic()
            for user_id in self.connected_user_ids():
                if STOP.is_set() or self._closing.is_set():
                    break
                if (self.state.ui_value(user_id, "delivery_blocked", False) or
                        not self.state.ui_value(user_id, "notifications_enabled", True)):
                    continue
                if (now - self.last_notification_poll.get(user_id, 0.0) >= self.notification_poll_seconds or
                        now - self.last_health_poll.get(user_id, 0.0) >= 60):
                    self.maintenance_jobs.submit(user_id, lambda uid=user_id: self._maintain_user(uid))
            STOP.wait(5)

    def confirm_add(self, chat_id: int, release: Release | None = None) -> None:
        release = release or self.state.pop_pending(chat_id)
        if not release:
            self.send(chat_id, "Подтверждение устарело. Выбери раздачу ещё раз.", self.menu_keyboard())
            return
        added_key = release_dedupe_key(release)
        recent_adds = {key: value for key, value in self.state.ui_value(chat_id, "recent_adds", {}).items()
                       if value.get("expires", 0) > time.time()}
        if added_key in recent_adds:
            torrent_id = recent_adds[added_key].get("torrent_id")
            button = {"text": "📂 Открыть загрузку", "callback_data": f"tinfo:{torrent_id}"} if torrent_id is not None else {"text": "📥 Мои загрузки", "callback_data": "list:0"}
            self.send(chat_id, "✅ Этот релиз уже добавлен. TorBox обрабатывает загрузку.",
                      [[button], [{"text": "← К результатам", "callback_data": "results"}]])
            return
        try:
            torbox = self.torbox_for(chat_id)
        except RuntimeError:
            if release:
                self.state.put_pending(chat_id, release)
            self.send(
                chat_id,
                "🔑 Чтобы добавить релиз, сначала подключи свой TorBox API key.",
                [[{"text": "➕ Подключить TorBox", "callback_data": "connect"},
                  {"text": "Отмена", "callback_data": "cancel"}]],
            )
            return
        self.send(chat_id, "⏳ Получаю релиз и отправляю в TorBox…")
        existing: dict[str, Any] | None = None
        try:
            torrent_bytes: bytes | None = None
            # Recover old search results saved before Prowlarr HTTP magnet
            # wrappers were normalized. Their download_url still points to a
            # valid .torrent file, so existing confirmation buttons keep working.
            if release.magnet_url and not is_magnet_url(release.magnet_url):
                if urlparse(release.download_url).scheme in {"http", "https"}:
                    release = replace(release, magnet_url="")
                else:
                    raise ValueError("Источник вернул битую ссылку. Выполни поиск заново.")
            info_hash = normalize_info_hash(release.info_hash)
            if not release.magnet_url:
                torrent_bytes = self.nnm.download_torrent(release)
                info_hash = torrent_fingerprint(torrent_bytes)
            if info_hash and release.cached is None:
                cached = torbox.cached_hashes([info_hash])
                release = replace(release, info_hash=info_hash, cached=info_hash in cached)
            existing = torbox.find_torrent_by_hash(info_hash) if info_hash else None
            if not existing and release.magnet_url:
                result = torbox.add_magnet(release.magnet_url, release)
            elif not existing:
                if torrent_bytes is None:
                    raise RuntimeError("Не удалось получить torrent-файл")
                result = torbox.add_file(torrent_bytes, release)
            else:
                result = {}
        except ValueError as exc:
            LOG.warning("Release cannot be retried without a new search: %s", exc)
            self.send(
                chat_id,
                f"❌ Не добавлено в TorBox: {html.escape(str(exc))}",
                [[self.search_button("Новый поиск"), self.home_button()]],
            )
            return
        except Exception:
            LOG.exception("Could not add torrent")
            self.state.put_pending(chat_id, release)
            self.send(
                chat_id,
                "Не удалось подтвердить добавление. Повтори попытку — сначала проверю, появился ли релиз в TorBox.",
                [[{"text": "🔄 Повторить", "callback_data": "confirm"},
                  {"text": "Отмена", "callback_data": "cancel"}]],
            )
            return

        # TorBox mutations above have already succeeded. Telegram delivery is
        # deliberately outside that try-block: a failed confirmation message
        # must never put the release back into the retry queue and create a duplicate.
        try:
            result_data = result.get("data") if isinstance(result.get("data"), dict) else {}
            recent_adds[added_key] = {"expires": time.time() + 300,
                                     "torrent_id": existing.get("id") if existing else result_data.get("torrent_id")}
            self.state.set_ui(chat_id, recent_adds=dict(list(recent_adds.items())[-20:]))
            if existing:
                torrent_id = safe_torrent_id(existing.get("id"))
                icon, status = status_label(existing)
                self.send(
                    chat_id,
                    f"ℹ️ <b>Этот релиз уже есть в TorBox</b>\n\n"
                    f"{icon} {html.escape(str(existing.get('name') or release.title))}\n"
                    f"Статус: <b>{html.escape(status)}</b>",
                    [[{"text": "ℹ️ Открыть загрузку", "callback_data": f"tinfo:{torrent_id}"}],
                     [{"text": "← К результатам", "callback_data": "results"}]],
                )
                return
            result_data = result.get("data") if isinstance(result.get("data"), dict) else {}
            torrent_id = result_data.get("torrent_id")
            next_step = {"text": "📥 Мои загрузки", "callback_data": "list:0"}
            if torrent_id is not None:
                next_step = {"text": "📂 Открыть загрузку", "callback_data": f"tinfo:{safe_torrent_id(torrent_id)}"}
            detail = result.get("detail") or result_data.get("detail") or "принято TorBox"
            quality = video_quality_summary(release, release.details_text)
            voiceover = voiceover_summary(release, release.details_text)
            self.send(
                chat_id,
                f"✅ <b>Добавлено в TorBox</b>\n\n"
                f"🎬 {html.escape(compact_title(release.title, 220))}\n\n"
                f"🎙 <b>Озвучка:</b> {html.escape(voiceover)}\n"
                f"🎞 <b>Качество:</b> {html.escape(quality)}\n"
                f"📦 <b>{html.escape(release.size_text)}</b>\n"
                f"🟢 {html.escape(friendly_torbox_status(str(detail)))}",
                [[next_step], [{"text": "← К результатам", "callback_data": "results"}, self.home_button()]],
            )
        except Exception:
            LOG.warning("Torrent was added but Telegram confirmation failed", exc_info=True)

    def callback(self, callback: dict[str, Any]) -> None:
        callback_id = str(callback.get("id") or "")
        message = callback.get("message", {})
        chat_id = message.get("chat", {}).get("id")
        inline_message_id = str(callback.get("inline_message_id") or "")
        sender_id = callback.get("from", {}).get("id")
        user_owned_card = bool(inline_message_id or message.get("via_bot") or
                               message.get("from", {}).get("is_bot") is False)
        if user_owned_card and isinstance(sender_id, int) and self.user_allowed(sender_id):
            chat_id = sender_id
        data = str(callback.get("data") or "")
        self.answer_callback(callback_id)
        if not isinstance(chat_id, int):
            return
        try:
            self.callback_message_id = int(message.get("message_id")) if not user_owned_card and message.get("message_id") is not None else None
        except (TypeError, ValueError):
            self.callback_message_id = None
        # Inline cards were sent by the user, possibly into a group. Never edit
        # them: send a fresh bot-owned response in the sender's private chat.
        self.callback_inline_message_id = ""
        self.capture_screen = True
        if self.callback_message_id:
            self.state.set_screen_message_id(chat_id, self.callback_message_id)
        parts = data.split(":")
        command = parts[0]
        try:
            # Inline messages may live in a group. Account contents and download
            # links belong exclusively in the user's private bot conversation.
            if user_owned_card and command not in {"search", "catalog", "release", "info", "results", "sr", "retry", "noop", "seasons", "season"}:
                destination = "search_settings" if command == "searchsettings" else "start"
                if command in {"add", "queue"} and len(parts) == 3 and parts[2] == self.state.search_revision(chat_id):
                    release = self.state.get_release(chat_id, int(parts[1]))
                    if release:
                        destination = "private_" + os.urandom(8).hex()
                        self.state.put_inline_item(destination, {"kind": "release", "owner": chat_id, "release": release.to_dict()})
                self.send(chat_id, "Продолжи в личном чате — там доступны твои загрузки и настройки.",
                          [[{"text": "Открыть TorBox", "url": self._deep_link(destination)}]])
                return
            if self.credentials and command in {"tctl", "delconfirm", "nclearconfirm", "file", "files", "tinfo", "delask"}:
                if parts[-1] != "a" + self.account_revision(chat_id):
                    self.send(chat_id, "Эта кнопка относится к прежнему подключению. Открой актуальные загрузки.",
                              [[{"text": "Мои загрузки", "callback_data": "list:0"}]])
                    return
                parts.pop()
            if command == "noop":
                return
            if command not in {"connect"}:
                self.state.set_mode(chat_id, None)
            if command == "home":
                self.show_home(chat_id)
            elif command == "searchsettings":
                self.show_search_settings(chat_id)
            elif command == "sp":
                self.change_search_preference(chat_id, parts[1], parts[2])
            elif command == "seasons":
                self.show_seasons(chat_id, parts[1], int(parts[2]) if len(parts) > 2 else 0)
            elif command == "season":
                stored = self.state.get_search(chat_id)
                if not stored or parts[1] != self.state.search_revision(chat_id):
                    self.show_seasons(chat_id, parts[1])
                    return
                if parts[2] not in {"all", "packs"} and not (parts[2].isdigit() and 1 <= int(parts[2]) <= 99):
                    raise ValueError("Invalid season")
                self.state.set_ui(chat_id, search_season=parts[2], search_page=0, search_filter="all")
                query, sources, releases = stored
                self.render_search_results(chat_id, query, releases, sources)
            elif command in {"history", "favorites"}:
                self.show_saved_queries(chat_id, favorites=command == "favorites")
            elif command == "historyclear":
                self.state.set_ui(chat_id, history=[])
                self.show_saved_queries(chat_id)
            elif command == "retry":
                stored_query = self.state.get_inline_item(parts[1])
                if stored_query and stored_query.get("owner") == chat_id and stored_query.get("kind") == "query":
                    self.show_search(chat_id, str(stored_query["query"]))
                else:
                    self.prompt_search(chat_id)
            elif command in {"sr", "favorite"}:
                stored = self.state.get_search(chat_id)
                if not stored or parts[1] != self.state.search_revision(chat_id):
                    self.send(chat_id, "Этот поиск устарел. Открой актуальные результаты или начни новый поиск.",
                              [[{"text": "Последние результаты", "callback_data": "results"}], [self.search_button()]])
                    return
                query, source_status, releases = stored
                if command == "favorite":
                    favorites = list(self.state.ui_value(chat_id, "favorites", []))
                    if query in favorites:
                        favorites.remove(query)
                    elif len(favorites) < 20:
                        favorites.insert(0, query)
                    else:
                        self.send(chat_id, "В избранном уже 20 названий. Убери одно через его результаты поиска.",
                                  [[{"text": "⭐ Избранное", "callback_data": "favorites"}], [self.home_button()]])
                        return
                    self.state.set_ui(chat_id, favorites=favorites)
                    self.render_search_results(chat_id, query, releases, source_status)
                else:
                    if parts[3] not in {"all", "cached", "hd", "small"}:
                        raise ValueError("Unknown filter")
                    self.render_search_results(chat_id, query, releases, source_status, page=int(parts[2]), filter_by=parts[3])
            elif command == "help":
                self.show_help(chat_id)
            elif command == "account":
                self.show_account(chat_id)
            elif command == "privacy":
                self.show_privacy(chat_id)
            elif command == "forgetask":
                self.forget_prompt(chat_id)
            elif command == "forgetconfirm":
                self.forget_user(chat_id)
            elif command == "connect":
                self.prompt_connect(chat_id)
            elif command == "disconnectask":
                self.send(
                    chat_id,
                    "🗑 <b>Отключить личный TorBox?</b>\n\n"
                    "Ключ будет удалён с сервера. Загрузки в самом TorBox останутся без изменений.",
                    [[{"text": "Да, удалить ключ", "callback_data": "disconnectconfirm"},
                      {"text": "Отмена", "callback_data": "account"}]],
                )
            elif command == "disconnectconfirm":
                previous_key = self.credentials.api_key(chat_id) if self.credentials else None
                removed = self.credentials.remove_api_key(chat_id) if self.credentials else False
                self.forget_log_key(previous_key)
                self._torbox_clients.pop(chat_id, None)
                self.state.reset_account(chat_id)
                self.last_notification_poll.pop(chat_id, None)
                self.last_health_poll.pop(chat_id, None)
                self.state.set_mode(chat_id, None)
                if removed:
                    self.send(chat_id, "✅ Личный API key удалён.", self.menu_keyboard())
                else:
                    self.show_account(chat_id)
            elif command == "search":
                self.prompt_search(chat_id)
            elif command in {"list", "refresh"}:
                page = max(0, int(parts[1])) if len(parts) > 1 else 0
                self.show_torrents(chat_id, page, refresh=command == "refresh")
            elif command == "library":
                if parts[1] not in {"all", "active", "ready"}:
                    raise ValueError("Unknown filter")
                self.show_torrents(chat_id, int(parts[2]), filter_by=parts[1])
            elif command == "files":
                self.show_files(chat_id, safe_torrent_id(parts[1]), int(parts[2]))
            elif command == "file":
                self.show_files(chat_id, safe_torrent_id(parts[1]), int(parts[3]), file_id=safe_torrent_id(parts[2]))
            elif command == "notifications":
                self.show_notifications(chat_id)
            elif command == "notifytoggle":
                enabled = not self.state.ui_value(chat_id, "notifications_enabled", True)
                self.state.set_ui(chat_id, notifications_enabled=enabled)
                self.show_notifications(chat_id)
            elif command in {"catalog", "release"}:
                token = parts[1]
                stored_inline = self.state.get_inline_item(token)
                if stored_inline and stored_inline.get("owner", chat_id) != chat_id:
                    stored_inline = None
                if not stored_inline:
                    self.send(
                        chat_id,
                        "Карточка устарела. Запусти поиск заново.",
                    )
                elif command == "catalog" and stored_inline.get("kind") == "catalog":
                    query = str(stored_inline.get("query") or "").strip()
                    self.show_search(chat_id, query) if query else self.prompt_search(chat_id)
                elif command == "release" and isinstance(stored_inline.get("release"), dict):
                    release = Release.from_dict(stored_inline["release"])
                    self.state.put_search(chat_id, [release], query=release.title)
                    self.render_search_results(chat_id, release.title, [release])
                else:
                    self.prompt_search(chat_id)
            elif command == "results":
                stored = self.state.get_search(chat_id)
                if not stored:
                    self.send(
                        chat_id,
                        "Результаты устарели. Запусти поиск заново.",
                        [[self.search_button(), self.home_button()]],
                    )
                else:
                    query, source_status, releases = stored
                    self.render_search_results(chat_id, query or "Последний поиск", releases, source_status)
            elif command == "nclearask":
                self.send(
                    chat_id,
                    "🧹 <b>Очистить все уведомления TorBox?</b>\n\nОни будут удалены из ленты TorBox без возможности восстановления.",
                    [[{"text": "Да, очистить", "callback_data": "nclearconfirm"}, {"text": "Отмена", "callback_data": "notifications"}]],
                )
            elif command == "nclearconfirm":
                self.torbox_for(chat_id).clear_notifications()
                self.send(chat_id, "✅ Лента уведомлений очищена.", self.menu_keyboard())
            elif command in {"info", "queue", "add"}:
                # An index alone could silently select a different film after a
                # later search. Old keyboards must never cause that mutation.
                if len(parts) != 3 or parts[2] != self.state.search_revision(chat_id):
                    self.send(chat_id, "Эта кнопка из старого поиска. Выбери релиз в актуальной выдаче.",
                              [[{"text": "Открыть результаты", "callback_data": "results"}], [self.search_button()]])
                    return
                index = int(parts[1])
                release = self.state.get_release(chat_id, index)
                if not release:
                    self.send(chat_id, "Результаты устарели. Выполни поиск заново.", self.menu_keyboard())
                    return
                if command == "info":
                    self.send(chat_id, "⏳ Загружаю карточку релиза…")
                    try:
                        details = self.nnm.topic_summary(release)
                    except Exception:
                        details = release.details_text
                    release = replace(release, details_text=details)
                    self.state.update_release(chat_id, index, release)
                    quality = video_quality_summary(release, details)
                    voiceover = voiceover_summary(release, details)
                    audio_track = audio_track_summary(details)
                    released_by = _labeled_value(details, "Релиз от")
                    detail_lines = [
                        f"🎬 <b>{html.escape(compact_title(release.title, 220))}</b>",
                        f"🎙 <b>Озвучка:</b> {html.escape(voiceover)}",
                        f"🎞 <b>Качество:</b> {html.escape(quality)}",
                    ]
                    if audio_track:
                        detail_lines.append(f"🔊 <b>Аудио:</b> {html.escape(audio_track)}")
                    if released_by:
                        detail_lines.append(f"👤 <b>Релиз:</b> {html.escape(compact_title(released_by, 140))}")
                    detail_lines.extend([
                        f"📦 <b>{html.escape(release.size_text)}</b> · 🌱 <b>{release.seeders} сидов</b>",
                        f"📡 {html.escape(release.source_name)}",
                    ])
                    details_keyboard: list[list[dict[str, str]]] = [
                        [{"text": "＋ Добавить в TorBox", "callback_data": f"add:{index}:{parts[2]}"}],
                    ]
                    if release.source_url:
                        details_keyboard.append([{"text": "↗️ Открыть источник", "url": release.source_url}])
                    details_keyboard.append([
                        {"text": "← К результатам", "callback_data": "results"},
                    ])
                    self.send(
                        chat_id,
                        "\n\n".join((detail_lines[0], "\n".join(detail_lines[1:]))),
                        details_keyboard,
                    )
                else:
                    # The button itself says "Добавить", so this tap is the
                    # explicit authorization. A second confirmation only added friction.
                    self.confirm_add(chat_id, release)
            elif command == "cancel":
                self.state.pop_pending(chat_id)
                stored = self.state.get_search(chat_id)
                if stored:
                    query, source_status, releases = stored
                    self.render_search_results(chat_id, query or "Последний поиск", releases, source_status)
                else:
                    self.show_home(chat_id)
            elif command == "confirm":
                self.confirm_add(chat_id)
            elif command == "tinfo":
                self.show_torrent(chat_id, safe_torrent_id(parts[1]))
            elif command == "tctl":
                torrent_id, operation = safe_torrent_id(parts[1]), parts[2]
                self.torbox_for(chat_id).control_torrent(torrent_id, operation)
                self.show_torrent(chat_id, torrent_id, notice="✅ Команда принята. Статус обновится после обработки TorBox.")
            elif command == "delask":
                torrent_id = safe_torrent_id(parts[1])
                item = self.torbox_for(chat_id).get_torrent(torrent_id)
                name = str(item.get("name") or f"TorBox #{torrent_id}") if item else f"TorBox #{torrent_id}"
                self.send(
                    chat_id,
                    f"🗑 <b>Удалить загрузку?</b>\n\n{html.escape(name)}\n\n"
                    "Она будет окончательно удалена из аккаунта TorBox.",
                    [[{"text": "🗑 Да, удалить", "callback_data": f"delconfirm:{torrent_id}"},
                      {"text": "Отмена", "callback_data": f"tinfo:{torrent_id}"}]],
                )
            elif command == "delconfirm":
                torrent_id = safe_torrent_id(parts[1])
                self.torbox_for(chat_id).control_torrent(torrent_id, "delete")
                self.state.set_ui(chat_id, recent_adds={k: v for k, v in self.state.ui_value(chat_id, "recent_adds", {}).items()
                                                       if v.get("torrent_id") != torrent_id})
                self.send(chat_id, f"✅ Загрузка <code>{torrent_id}</code> удалена из TorBox.")
                self.show_torrents(chat_id, self.state.ui_value(chat_id, "library_page", 0), refresh=True)
            else:
                self.send(chat_id, "Эта кнопка устарела. Открыл главное меню.")
                self.show_home(chat_id)
        except (IndexError, TypeError, ValueError):
            self.send(chat_id, "Эта кнопка устарела. Открой нужный раздел заново.", self.menu_keyboard())
        except Exception as exc:
            LOG.exception("Callback action failed: %s", data)
            self.send(chat_id, f"❌ Операция не выполнена: {public_error(exc)}", self.menu_keyboard())
        finally:
            self.callback_message_id = None
            self.callback_inline_message_id = ""
            self.capture_screen = False

    def show_status(self, chat_id: int) -> None:
        if not self.has_torbox(chat_id):
            torbox_status = "не подключён"
        else:
            try:
                count = len(self.torbox_for(chat_id).list_torrents(limit=100))
                torbox_status = f"доступен, загрузок: {count}"
            except Exception as exc:
                torbox_status = public_error(exc)
        notification_status = "включены" if self.notifications_enabled else "выключены"
        configured_providers = getattr(self.nnm, "providers", [self.nnm])
        search_names = ", ".join(str(getattr(provider, "name", "неизвестный")) for provider in configured_providers)
        self.send(
            chat_id,
            "✅ <b>Бот работает</b>\n\n"
            f"TorBox: {html.escape(torbox_status)}\n"
            f"Поиск: {html.escape(search_names)}\n"
            f"Автоуведомления: {notification_status}",
            self.menu_keyboard(),
        )

    def handle(self, update: dict[str, Any]) -> None:
        if inline_query := update.get("inline_query"):
            self.inline_query(inline_query)
            return
        if chosen := update.get("chosen_inline_result"):
            self.chosen_inline_result(chosen)
            return
        if not self.is_owner(update):
            return
        user_id = self.update_user(update)
        if user_id and self.state.ui_value(user_id, "delivery_blocked", False):
            self.state.set_ui(user_id, delivery_blocked=False)
        if callback := update.get("callback_query"):
            self.callback(callback)
            return
        message = update.get("message", {})
        # Selecting an inline result sends its prepared card into the current
        # chat with via_bot set. It is output, not a fresh user search query.
        if isinstance(message.get("via_bot"), dict):
            return
        text = (message.get("text") or "").strip()
        chat_id = message.get("chat", {}).get("id")
        if not isinstance(chat_id, int) or not text:
            return
        try:
            incoming_message_id = int(message.get("message_id"))
        except (TypeError, ValueError):
            incoming_message_id = 0
        normalized_text = BOTTOM_ACTIONS.get(text, text)
        first, _, raw_argument = normalized_text.partition(" ")
        command = first.split("@", 1)[0].casefold() if first.startswith("/") else ""
        argument = raw_argument if command else ""
        # After a user clears the private chat Telegram removes the visible
        # reply keyboard, but our state may still point at the deleted screen.
        # /start is the explicit recovery action, so always recreate the
        # keyboard and obtain a fresh message id.
        force_keyboard = command == "/start"
        fresh_screen_id = None
        if force_keyboard or self.state.bottom_keyboard_version(chat_id) < BOTTOM_KEYBOARD_VERSION:
            try:
                fresh_screen_id = self.install_bottom_keyboard(chat_id, force=force_keyboard)
            except Exception:
                LOG.warning("Could not install persistent Telegram controls", exc_info=True)
        # Each typed request gets its own bot response after that request.
        # Loading and button navigation then edit only that response.
        self.callback_message_id = fresh_screen_id
        self.callback_inline_message_id = ""
        self.capture_screen = True
        try:
            text = normalized_text
            if command == "/start":
                if argument == "connect":
                    self.prompt_connect(chat_id)
                elif argument == "search_settings":
                    self.show_search_settings(chat_id)
                elif argument:
                    inline_item = self.state.get_inline_item(argument)
                    if inline_item and inline_item.get("owner", chat_id) != chat_id:
                        inline_item = None
                    if inline_item and inline_item.get("kind") == "catalog":
                        query = str(inline_item.get("query") or "").strip()
                        self.show_search(chat_id, query) if query else self.show_home(chat_id)
                    elif inline_item and inline_item.get("kind") == "release" and isinstance(inline_item.get("release"), dict):
                        release = Release.from_dict(inline_item["release"])
                        self.state.put_search(chat_id, [release], query=release.title)
                        self.render_search_results(chat_id, release.title, [release])
                    else:
                        self.show_home(chat_id)
                else:
                    self.show_home(chat_id)
            elif command == "/cancel":
                connecting = self.state.mode(chat_id) == "torbox_api_key"
                self.state.set_mode(chat_id, None)
                self.show_account(chat_id) if connecting else self.show_home(chat_id)
            elif command == "/connect":
                self.prompt_connect(chat_id)
            elif command in {"/settings", "/searchsettings"}:
                self.show_search_settings(chat_id)
            elif command == "/account" or text == BTN_ACCOUNT:
                self.show_account(chat_id)
            elif command == "/privacy":
                self.show_privacy(chat_id)
            elif command == "/delete_data":
                self.forget_prompt(chat_id)
            elif command == "/help" or text == BTN_HELP:
                self.show_help(chat_id)
            elif command == "/search" or text == BTN_SEARCH:
                if argument:
                    self.show_catalog_choices(chat_id, argument) if self.catalog else self.show_search(chat_id, argument)
                else:
                    self.prompt_search(chat_id)
            elif command in {"/torrents", "/list"} or text == BTN_TORRENTS:
                self.show_torrents(chat_id)
            elif command in {"/notifications", "/notify"} or text == BTN_NOTIFICATIONS:
                self.show_notifications(chat_id)
            elif text == BTN_HOME:
                self.show_home(chat_id)
            elif command == "/status":
                self.show_status(chat_id)
            elif self.state.mode(chat_id) == "torbox_api_key":
                # Keep ordinary messages; remove only submitted credentials.
                if not incoming_message_id or not self.delete_user_message(chat_id, incoming_message_id):
                    self.send(chat_id, "Не удалось удалить сообщение с ключом. Удали его вручную и повтори /connect. Ключ не сохранён.")
                    return
                self.connect_torbox(chat_id, text)
            elif command:
                self.send(chat_id, "Не знаю такой команды. Вот доступные действия:", self.menu_keyboard())
            elif self.state.mode(chat_id) == "catalog_search":
                self.show_catalog_choices(chat_id, text)
            elif self.state.mode(chat_id) == "search":
                self.show_search(chat_id, text)
            else:
                # Plain text remains a fast search shortcut, with catalog
                # disambiguation when the movie catalog is configured.
                self.show_catalog_choices(chat_id, text) if self.catalog else self.show_search(chat_id, text)
        finally:
            self.callback_message_id = None
            self.callback_inline_message_id = ""
            self.capture_screen = False

    def setup(self) -> None:
        self.call("deleteWebhook", drop_pending_updates=False)
        me = self.call("getMe")
        self.bot_id = int(me.get("id") or 0)
        self.username = str(me.get("username") or "tor_boxai_bot")
        self.inline_enabled = bool(me.get("supports_inline_queries"))
        cosmetic_calls = [
            ("setMyName", {"name": os.getenv("TELEGRAM_BOT_NAME", "TorBox • Личный пульт")[:64]}),
            ("setMyShortDescription", {"short_description": "Поиск релизов и управление своим TorBox"}),
            ("setMyDescription", {"description": "Находи фильмы и сериалы, добавляй релизы в свой TorBox и смотри готовые файлы через Infuse. Нажми Start — внутри инструкция по настройке TorBox, бота и Infuse."}),
            ("setMyCommands", {"commands": [
                {"command": "start", "description": "Начать и открыть инструкцию"},
                {"command": "connect", "description": "Подключить свой TorBox"},
                {"command": "account", "description": "Настройки TorBox-аккаунта"},
                {"command": "privacy", "description": "Как хранятся мои данные"},
                {"command": "delete_data", "description": "Удалить мои данные из бота"},
                {"command": "search", "description": "Найти фильм или сериал"},
                {"command": "settings", "description": "Качество, озвучка и сезоны"},
                {"command": "torrents", "description": "Мои загрузки TorBox"},
                {"command": "notifications", "description": "Уведомления TorBox"},
                {"command": "status", "description": "Проверить соединение"},
                {"command": "help", "description": "Помощь"},
            ]}),
            ("setChatMenuButton", {"menu_button": {"type": "commands"}}),
        ]
        for method, payload in cosmetic_calls:
            try:
                self.call(method, **payload)
            except Exception:
                # A temporary profile-decoration failure must not stop polling.
                LOG.warning("Could not apply Telegram setting %s", method, exc_info=True)
        LOG.info(
            "Bot @%s started for owner %s (multi-user: %s, inline mode: %s, catalog: %s)",
            self.username,
            self.owner_id,
            "enabled" if self.multi_user_enabled else "disabled",
            "enabled" if self.inline_enabled else "disabled",
            "enabled" if self.catalog else "disabled",
        )

    def run(self) -> None:
        self.setup()
        maintenance = Thread(target=self.maintenance_loop, name="torbox-maintenance", daemon=True)
        maintenance.start()
        try:
            while not STOP.is_set():
                try:
                    updates = self.call(
                        "getUpdates", offset=self.state.offset(), timeout=45,
                        allowed_updates=["message", "callback_query", "inline_query", "chosen_inline_result"],
                    )
                    for update in updates:
                        if STOP.is_set():
                            break
                        # Persist before dispatch: a restart must not replay mutations.
                        self.state.set_offset(update["update_id"] + 1)
                        self.dispatch_update(update)
                except requests.RequestException as exc:
                    LOG.warning("Telegram network error: %s", exc)
                    STOP.wait(3)
                except Exception:
                    LOG.exception("Unhandled bot error")
                    STOP.wait(2)
        finally:
            self._closing.set()
            maintenance.join(timeout=6)
            self.close()


def main() -> int:
    try:
        token = env_required("TELEGRAM_BOT_TOKEN")
        api_key = os.getenv("TORBOX_API_KEY", "").strip()
        owner_id = int(env_required("OWNER_TELEGRAM_ID"))
        if owner_id <= 0:
            raise ValueError("OWNER_TELEGRAM_ID must be positive")
        workers = min(16, env_int("BOT_WORKERS", 8))
        multi_user_enabled = env_bool("MULTI_USER_ENABLED", True)
        notifications_enabled = env_bool("TORBOX_NOTIFICATIONS_ENABLED", True)
        notification_poll_seconds = env_int("NOTIFICATION_POLL_SECONDS", 60, minimum=30)
        kinopoisk_api_key = os.getenv("KINOPOISK_API_KEY", "").strip()
        nnm_enabled = env_bool("NNM_ENABLED", True)
        prowlarr_url = os.getenv("PROWLARR_URL", "").strip()
        prowlarr_api_key = os.getenv("PROWLARR_API_KEY", "").strip()
        prowlarr_indexer_ids = env_int_tuple("PROWLARR_INDEXER_IDS")
        if bool(prowlarr_url) != bool(prowlarr_api_key):
            raise RuntimeError("PROWLARR_URL and PROWLARR_API_KEY must be configured together")
        providers: list[SearchProvider] = []
        if nnm_enabled:
            providers.append(NnmClient())
        if prowlarr_url:
            providers.append(ProwlarrClient(prowlarr_url, prowlarr_api_key, prowlarr_indexer_ids))
        search_client = SearchAggregator(providers)
    except (RuntimeError, ValueError) as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 2
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: STOP.set())
    TelegramBot(
        token, owner_id, TorBoxClient(api_key) if api_key else None, search_client, State(),
        catalog=KinopoiskCatalog(kinopoisk_api_key) if kinopoisk_api_key else None,
        notifications_enabled=notifications_enabled,
        notification_poll_seconds=notification_poll_seconds,
        credentials=CredentialStore(),
        multi_user_enabled=multi_user_enabled,
        workers=workers,
    ).run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
