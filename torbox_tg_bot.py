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
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, replace
from pathlib import Path
from threading import Event, Lock, RLock, Thread, Timer, get_ident, local
from typing import Any, Protocol
from urllib.parse import parse_qs, quote_plus, urljoin, urlparse

import bencodepy
import cloudscraper
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

APP_DIR = Path(__file__).resolve().parent
STATE_PATH = Path(os.getenv("STATE_PATH", str(APP_DIR / "state.json")))
CREDENTIALS_PATH = Path(os.getenv("CREDENTIALS_PATH", str(APP_DIR / "credentials.json")))
NNM_BASE = "https://nnmclub.to/forum/"
TORBOX_API = "https://api.torbox.app/v1/api"
TELEGRAM_API = "https://api.telegram.org/bot{token}/{method}"
MAX_RESULTS = 8
MAX_SEARCH_CANDIDATES = 24
MAX_PROVIDER_RESULTS = 80
TORBOX_PAGE_SIZE = 6
MAX_QUERY_LENGTH = 120
MAX_TORRENT_BYTES = 4 * 1024 * 1024
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
LOG = logging.getLogger("torbox_tg_bot")
STOP = Event()


def configure_read_retries(session: requests.Session) -> None:
    """Retry transient read failures without replaying mutations."""
    retry = Retry(
        total=2,
        connect=2,
        read=2,
        status=2,
        backoff_factor=0.4,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset({"GET", "HEAD", "OPTIONS"}),
        respect_retry_after_header=True,
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retry, pool_connections=8, pool_maxsize=8)
    session.mount("https://", adapter)
    session.mount("http://", adapter)

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
        return " ".join(part for part in (self.name, self.year) if part).strip()


class KinopoiskCatalog:
    """Optional Russian-language movie catalog used by Telegram inline mode."""

    API_URL = "https://kinopoiskapiunofficial.tech/api/v2.1/films/search-by-keyword"
    POPULAR_URL = "https://kinopoiskapiunofficial.tech/api/v2.2/films/collections"

    def __init__(self, api_key: str) -> None:
        self.http = requests.Session()
        configure_read_retries(self.http)
        self.http.headers.update({"X-API-KEY": api_key.strip(), "Accept": "application/json", "User-Agent": USER_AGENT})

    def search(self, query: str) -> list[CatalogTitle]:
        response = self.http.get(self.API_URL, params={"keyword": query, "page": 1}, timeout=15)
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
    if item.get("download_finished") or item.get("cached") or state in {"cached", "completed", "uploading"}:
        return "✅", "готово"
    if "error" in state or "missing" in state or "no seeds" in state:
        return "⚠️", "нужна проверка"
    if "pause" in state:
        return "⏸", "приостановлено"
    if state in {"metadl", "checkingresumedata", "queued", "checking"}:
        return "⏳", "подготовка"
    if item.get("active") or "down" in state:
        return "⬇️", "скачивается"
    return "⏳", state or "ожидание"


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


def select_diverse_releases(releases: list[Release], limit: int = MAX_RESULTS) -> list[Release]:
    """Keep a short Telegram list useful instead of filling it with clones."""
    selected: list[Release] = []
    selected_ids: set[int] = set()
    variant_counts: dict[str, int] = {}
    source_counts: dict[str, int] = {}

    for index, release in enumerate(releases):
        variant = release_variant_key(release)
        source = release.source_name.casefold()
        if variant_counts.get(variant, 0) >= 2 or source_counts.get(source, 0) >= 5:
            continue
        selected.append(release)
        selected_ids.add(index)
        variant_counts[variant] = variant_counts.get(variant, 0) + 1
        source_counts[source] = source_counts.get(source, 0) + 1
        if len(selected) >= limit:
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
        # Inline queries are handled on a Timer thread while long polling keeps
        # updating the same state. One re-entrant lock protects both mutations
        # and the JSON snapshot written to disk.
        self._lock = RLock()
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
            temporary = self.path.with_suffix(".tmp")
            temporary.write_text(json.dumps(self.data, ensure_ascii=False), encoding="utf-8")
            temporary.chmod(0o600)
            temporary.replace(self.path)

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
    ) -> None:
        with self._lock:
            self.data["searches"][str(chat_id)] = {
                "expires": int(time.time()) + 3600,
                "query": query,
                "source_status": source_status,
                "items": [release.to_dict() for release in releases],
            }
            self.save()

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
            if isinstance(users, dict):
                self.data = {
                    str(user_id): str(api_key)
                    for user_id, api_key in users.items()
                    if str(user_id).isdigit() and isinstance(api_key, str) and api_key
                }
            path.chmod(0o600)
        except FileNotFoundError:
            pass
        except (OSError, json.JSONDecodeError) as exc:
            LOG.warning("Credential store ignored: %s", exc)

    def save(self) -> None:
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.path.with_suffix(self.path.suffix + ".tmp")
            temporary.write_text(
                json.dumps({"users": self.data}, ensure_ascii=False),
                encoding="utf-8",
            )
            temporary.chmod(0o600)
            temporary.replace(self.path)

    def api_key(self, user_id: int) -> str | None:
        with self._lock:
            value = self.data.get(str(user_id))
            return value if value else None

    def set_api_key(self, user_id: int, api_key: str) -> None:
        with self._lock:
            self.data[str(user_id)] = api_key.strip()
            self.save()

    def remove_api_key(self, user_id: int) -> bool:
        with self._lock:
            removed = self.data.pop(str(user_id), None) is not None
            if removed:
                self.save()
            return removed

    def user_ids(self) -> set[int]:
        with self._lock:
            return {int(value) for value in self.data if value.isdigit()}


class NnmClient:
    key = "nnm"
    name = "NNM-Club"

    def __init__(self) -> None:
        self.http = cloudscraper.create_scraper()
        configure_read_retries(self.http)
        self.http.headers.update({"User-Agent": USER_AGENT})

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
        response = self.http.get(release.download_url, headers={"Referer": release.source_url}, timeout=30)
        response.raise_for_status()
        if len(response.content) > MAX_TORRENT_BYTES:
            raise RuntimeError("Torrent file is unexpectedly large")
        # Fast validity check before it reaches the TorBox account.
        try:
            decoded = bencodepy.decode(response.content)
            if b"info" not in decoded:
                raise ValueError("no info dictionary")
        except Exception as exc:  # bencode parser errors have varying concrete types
            raise RuntimeError(f"Source did not return a valid .torrent file: {exc}") from exc
        return response.content


class ProwlarrClient:
    """One connector for all torrent indexers configured in Prowlarr."""

    key = "prowlarr"
    name = "Prowlarr"

    def __init__(self, base_url: str, api_key: str, indexer_ids: tuple[int, ...] = ()) -> None:
        parsed = urlparse(base_url.strip())
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise RuntimeError("PROWLARR_URL must be a full http(s) URL")
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key.strip()
        if not self.api_key:
            raise RuntimeError("PROWLARR_API_KEY is empty")
        self.indexer_ids = indexer_ids
        self.http = requests.Session()
        configure_read_retries(self.http)
        self.http.headers.update({"X-Api-Key": self.api_key, "User-Agent": USER_AGENT, "Accept": "application/json"})

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
        response = self.http.get(f"{self.base_url}/api/v1/search", params=params, timeout=25)
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
        if same_origin:
            response = self.http.get(url, timeout=40)
        else:
            # Never leak the Prowlarr API key to a tracker-owned URL.
            response = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=40)
        response.raise_for_status()
        if len(response.content) > MAX_TORRENT_BYTES:
            raise RuntimeError("Torrent file is unexpectedly large")
        try:
            decoded = bencodepy.decode(response.content)
            if b"info" not in decoded:
                raise ValueError("no info dictionary")
        except Exception as exc:
            raise RuntimeError(f"Prowlarr did not return a valid .torrent file: {exc}") from exc
        return response.content


class SearchAggregator:
    """Search providers concurrently and return one deduplicated ranked list."""

    def __init__(self, providers: list[SearchProvider]) -> None:
        if not providers:
            raise RuntimeError("At least one search provider must be enabled")
        self.providers = providers
        self.by_key = {provider.key: provider for provider in providers}
        self.last_status: dict[str, str] = {}

    def search(self, query: str) -> list[Release]:
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
        return select_diverse_releases(ranked_items, MAX_SEARCH_CANDIDATES)

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
        response = self._session().request(method, f"{TORBOX_API}/{path.lstrip('/')}", timeout=timeout, **kwargs)
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
    ) -> None:
        self.token, self.owner_id, self.torbox, self.nnm, self.state = token, owner_id, torbox, nnm, state
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
        self.inline_cache: dict[str, tuple[float, list[dict[str, Any]]]] = {}
        self.inline_timers: dict[int, Timer] = {}
        self.inline_dispatch_lock = Lock()
        self.inline_search_lock = Lock()
        self.latest_inline_query_ids: dict[int, str] = {}
        self.callback_message_id: int | None = None
        self.callback_inline_message_id = ""
        self.capture_screen = False
        self.session = requests.Session()
        self._owner_thread_id = get_ident()
        self._thread_sessions = local()

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
        account_row: list[dict[str, str]] = []
        if self.multi_user_enabled or self.credentials:
            account_row.append({"text": BTN_ACCOUNT, "callback_data": "account"})
        account_row.append({"text": BTN_HELP, "callback_data": "help"})
        keyboard.append(account_row)
        return keyboard

    def search_button(self, text: str = BTN_SEARCH) -> dict[str, str]:
        if self.inline_enabled and self.catalog:
            return {"text": text, "switch_inline_query_current_chat": ""}
        return {"text": text, "callback_data": "search"}

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

    def _maintenance_state_user(self, user_id: int) -> int | None:
        # Legacy single-user tests and deployments keep their old state keys.
        return user_id if self.credentials else None

    def install_bottom_keyboard(self, chat_id: int, *, force: bool = False) -> int | None:
        if not force and self.state.bottom_keyboard_version(chat_id) >= BOTTOM_KEYBOARD_VERSION:
            return self.state.screen_message_id(chat_id)
        previous_screen_id = self.state.screen_message_id(chat_id)
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
            if force and previous_screen_id and previous_screen_id != message_id:
                self.delete_user_message(chat_id, previous_screen_id)
        self.state.set_bottom_keyboard_version(chat_id, BOTTOM_KEYBOARD_VERSION)
        return message_id

    def call(self, method: str, **payload: Any) -> Any:
        response = self._telegram_session().post(
            TELEGRAM_API.format(token=self.token, method=method), json=payload, timeout=65,
        )
        try:
            result = response.json()
        except ValueError as exc:
            response.raise_for_status()
            raise RuntimeError(f"Telegram {method} вернул ответ без JSON") from exc
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
        if keyboard:
            payload["reply_markup"] = {"inline_keyboard": keyboard}
        return payload

    def _send_new(
        self,
        chat_id: int,
        text: str,
        keyboard: list[list[dict[str, str]]] | None = None,
    ) -> Any:
        return self.call("sendMessage", chat_id=chat_id, **self._message_payload(text, keyboard))

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
            **self._message_payload(text, keyboard),
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

    def delete_user_message(self, chat_id: int, message_id: int) -> None:
        try:
            self.call("deleteMessage", chat_id=chat_id, message_id=message_id)
        except Exception:
            # The command still works when Telegram no longer permits deletion.
            LOG.debug("Could not remove the user's service message", exc_info=True)

    def answer_inline_query(self, inline_query_id: str, results: list[dict[str, Any]], *, cache_time: int = 30) -> None:
        # Inline searches run outside the long-poll thread. A separate HTTP
        # request avoids sharing the polling Session between threads.
        response = requests.post(
            TELEGRAM_API.format(token=self.token, method="answerInlineQuery"),
            json={
                "inline_query_id": inline_query_id,
                "results": results,
                "cache_time": cache_time,
                "is_personal": True,
            },
            timeout=20,
        )
        payload = response.json()
        if not response.ok or not payload.get("ok"):
            raise RuntimeError(f"Telegram answerInlineQuery: {payload.get('description') or response.status_code}")

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
            message_lines.append("\n⏳ Ищу лучшие релизы…")
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
                    "text": "🔎 Открыть результаты, если поиск не запустился",
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
        if not query_id:
            return
        user_id = query.get("from", {}).get("id")
        if not isinstance(user_id, int) or not self.user_allowed(user_id):
            self.answer_inline_query(query_id, [], cache_time=60)
            return
        if not self.catalog:
            self.answer_inline_query(query_id, [], cache_time=5)
            return
        text = re.sub(r"\s+", " ", str(query.get("query") or "")).strip()
        with self.inline_dispatch_lock:
            self.latest_inline_query_ids[user_id] = query_id
            previous_timer = self.inline_timers.pop(user_id, None)
            if previous_timer:
                previous_timer.cancel()
        # Telegram opens inline mode with an empty query. Show the popular
        # storefront there, then switch to regular relevance search as soon as
        # the user starts typing. The debounce below coalesces fast keystrokes.
        search_text = text
        cache_key = f"catalog:{search_text.casefold() or 'popular'}"
        cached = self.inline_cache.get(cache_key)
        if cached and cached[0] > time.monotonic():
            self.answer_inline_query(query_id, cached[1], cache_time=300 if not search_text else 30)
            return
        timer = Timer(
            0.05 if not search_text else 0.4,
            self._process_inline_query,
            args=(query_id, search_text, cache_key, user_id),
        )
        timer.daemon = True
        with self.inline_dispatch_lock:
            if self.latest_inline_query_ids.get(user_id) != query_id:
                return
            self.inline_timers[user_id] = timer
            timer.start()

    def chosen_inline_result(self, chosen: dict[str, Any]) -> None:
        """Turn the selected catalog card into release results immediately."""
        user_id = chosen.get("from", {}).get("id")
        if not isinstance(user_id, int) or not self.user_allowed(user_id):
            return
        token = str(chosen.get("result_id") or "")
        stored = self.state.get_inline_item(token)
        if not stored:
            return
        inline_message_id = str(chosen.get("inline_message_id") or "")
        if stored.get("kind") == "catalog":
            query = str(stored.get("query") or "").strip()
            if query:
                self.show_search(user_id, query, inline_message_id=inline_message_id)
        elif stored.get("kind") == "release" and isinstance(stored.get("release"), dict):
            release = Release.from_dict(stored["release"])
            self.state.put_search(user_id, [release], query=release.title)
            self.render_search_results(
                user_id,
                release.title,
                [release],
                "",
                inline_message_id=inline_message_id,
            )

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
            with self.inline_search_lock:
                with self.inline_dispatch_lock:
                    latest_query_id = self.latest_inline_query_ids.get(user_id)
                    if latest_query_id and latest_query_id != query_id:
                        return
                cached = self.inline_cache.get(cache_key)
                if cached and cached[0] > time.monotonic():
                    results = cached[1]
                elif self.catalog:
                    titles = self.catalog.search(text) if text else self.catalog.popular()
                    results = self._catalog_inline_results(titles)
                else:
                    results = self._release_inline_results(self._fast_inline_release_search(text))
            self.inline_cache[cache_key] = (time.monotonic() + (1800 if not text else 300), results)
            with self.inline_dispatch_lock:
                latest_query_id = self.latest_inline_query_ids.get(user_id)
                if latest_query_id and latest_query_id != query_id:
                    return
            self.answer_inline_query(query_id, results, cache_time=300 if not text else 30)
        except Exception:
            LOG.warning("Inline search failed", exc_info=True)
            with self.inline_dispatch_lock:
                latest_query_id = self.latest_inline_query_ids.get(user_id)
                is_latest = not latest_query_id or latest_query_id == query_id
            if is_latest:
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
            [[{"text": "➕ Подключить TorBox", "callback_data": "connect"}], [self.home_button()]],
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
            "Я сразу удалю сообщение, проверю ключ и сохраню его в защищённом файле сервера. "
            "Не отправляй ключ в групповой чат.\n\nДля отмены: /cancel",
            [[{"text": "Отмена", "callback_data": "account"}]],
        )

    def connect_torbox(self, chat_id: int, api_key: str) -> None:
        api_key = api_key.strip()
        if len(api_key) < 16 or any(character.isspace() for character in api_key):
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
        self.credentials.set_api_key(chat_id, api_key)
        self._torbox_clients[chat_id] = client
        self.state.set_mode(chat_id, None)
        self.send(
            chat_id,
            "✅ <b>Личный TorBox подключён</b>\n\nТеперь поиск, добавление, список загрузок и уведомления работают с твоим аккаунтом.",
            self.menu_keyboard(),
        )

    def show_home(self, chat_id: int) -> None:
        self.state.set_mode(chat_id, None)
        notification_line = "включены" if self.notifications_enabled else "выключены в конфигурации"
        account_line = "🟢 подключён" if self.has_torbox(chat_id) else "🟡 не подключён — нажми «TorBox-аккаунт»"
        self.send(
            chat_id,
            "<b>TorBox — личный пульт</b>\n\n"
            "Все основные действия находятся под этим сообщением.\n\n"
            "🔎 Поиск сразу открывает фильмы с постерами в строке ввода.\n"
            "➕ Сразу добавляет выбранный релиз.\n"
            "🗑 Удаление всегда просит подтверждение.\n\n"
            f"🔑 Аккаунт: <b>{account_line}</b>\n"
            f"🔔 Автоуведомления: <b>{notification_line}</b>.",
            self.menu_keyboard(),
        )

    def show_help(self, chat_id: int) -> None:
        self.send(
            chat_id,
            "<b>Как пользоваться</b>\n\n"
            "🔎 Нажми кнопку под сообщением, введи название и выбери фильм с постером. "
            "После выбора релизы откроются сразу.\n"
            "Сообщение с запросом бот убирает, поэтому экран не засоряется.\n"
            "➕ В списке сразу добавляет релиз, ℹ️ показывает подробности.\n"
            "↩️ Кнопка «Назад» возвращает к предыдущему экрану без нового сообщения.\n"
            "📥 <b>Мой TorBox</b> — список, прогресс, скорость, детали и удаление.\n"
            "🔔 <b>Уведомления</b> — последние сообщения TorBox. Новые сообщения бот присылает сам.\n\n"
            "🔑 <b>TorBox-аккаунт</b> — подключить или заменить личный API key.\n"
            "🔎 <b>Поиск</b> — открывает выдачу с постерами прямо в строке ввода.\n\n"
            "Команды: /start, /connect, /account, /search, /torrents, /notifications, /status, /help",
            [[self.home_button()]],
        )

    def prompt_search(
        self,
        chat_id: int,
        *,
        message_id: int | None = None,
        inline_message_id: str = "",
    ) -> None:
        self.state.set_mode(chat_id, "catalog_search" if self.catalog else "search")
        text = (
            "🔎 <b>Что найти?</b>\n\n"
            "Нажми кнопку и пиши название фильма или сериала прямо в строке ввода."
        )
        keyboard = [[self.search_button("🔎 Открыть поиск с постерами")], [self.home_button()]]
        self.render(
            chat_id,
            text,
            keyboard,
            message_id=message_id,
            inline_message_id=inline_message_id,
        )

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
        if not self.catalog:
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
        except Exception as exc:
            LOG.exception("Catalog search failed")
            self.render(
                chat_id,
                f"❌ Каталог сейчас недоступен: {html.escape(str(exc))}",
                [[self.search_button("🔄 Новый поиск"), self.home_button()]],
                message_id=message_id,
            )
            return
        if not titles:
            self.render(
                chat_id,
                "Ничего похожего не нашёл. Попробуй другое написание.",
                [[self.search_button("🔎 Другой запрос"), self.home_button()]],
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
        self,
        chat_id: int,
        query: str,
        releases: list[Release],
        source_status: str = "",
        *,
        message_id: int | None = None,
        inline_message_id: str = "",
    ) -> Any:
        lines = [
            f"🔎 <b>{html.escape(query)}</b>\n"
            f"Нашёл <b>{len(releases)} релизов</b> · ⭐ первый — лучший баланс.\n"
        ]
        if source_status:
            lines.append(f"📡 {html.escape(source_status)}\n")
        keyboard: list[list[dict[str, str]]] = []
        for index, release in enumerate(releases):
            quality = video_quality_summary(release, release.details_text)
            voiceover = voiceover_summary(release, release.details_text)
            number = f"⭐ {index + 1}" if index == 0 else str(index + 1)
            lines.append(
                f"<b>{number}. {html.escape(compact_title(release.title, 118))}</b>\n"
                f"🎞 {html.escape(quality)}\n"
                f"🎙 {html.escape(voiceover)}\n"
                f"📦 {html.escape(release.size_text)} · {availability_summary(release)} · "
                f"{html.escape(release.source_name)}\n"
            )
            keyboard.append([
                {"text": f"ℹ️ {index + 1} · Подробнее", "callback_data": f"info:{index}"},
                {"text": f"➕ {index + 1}", "callback_data": f"add:{index}"},
            ])
        keyboard.append([self.search_button("🔎 Другой фильм"), self.home_button()])
        return self.render(
            chat_id,
            "\n".join(lines),
            keyboard,
            message_id=message_id,
            inline_message_id=inline_message_id,
        )

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
        self.state.set_mode(chat_id, None)
        loading = self.render(
            chat_id,
            f"🔎 Ищу <b>{html.escape(query)}</b>…",
            message_id=message_id,
            inline_message_id=inline_message_id,
        )
        if message_id is None and not inline_message_id and isinstance(loading, dict):
            try:
                message_id = int(loading.get("message_id"))
            except (TypeError, ValueError):
                message_id = None
        try:
            releases = self.nnm.search(query)
        except requests.RequestException as exc:
            LOG.exception("NNM search failed")
            self.render(
                chat_id,
                f"❌ Источник сейчас недоступен: {html.escape(str(exc))}",
                [[{"text": "🔄 Повторить", "callback_data": "results"}]],
                message_id=message_id,
                inline_message_id=inline_message_id,
            )
            return
        source_status = ""
        status_summary = getattr(self.nnm, "status_summary", None)
        if callable(status_summary):
            source_status = str(status_summary())
        if releases:
            if self.has_torbox(chat_id):
                try:
                    cached = self.torbox_for(chat_id).cached_hashes([release.info_hash for release in releases])
                    releases = cache_ranked_releases(releases, cached)
                except Exception:
                    LOG.warning("Could not enrich search with TorBox cache", exc_info=True)
                    releases = select_diverse_releases(releases, MAX_RESULTS)
            else:
                releases = select_diverse_releases(releases, MAX_RESULTS)
        if not releases:
            self.render(
                chat_id,
                "Ничего подходящего не нашёл. Попробуй другое написание или добавь год."
                + (f"\n\nИсточники: {html.escape(source_status)}" if source_status else ""),
                [[self.search_button("🔎 Искать ещё"), self.home_button()]],
                message_id=message_id,
                inline_message_id=inline_message_id,
            )
            return
        self.state.put_search(chat_id, releases, query=query, source_status=source_status)
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
        return (
            f"{icon} <b>{html.escape(compact_title(str(item.get('name') or 'Без названия'), 90))}</b>\n"
            f"{html.escape(status)} · {progress:.0f}% · {size}{speed_text}"
        )

    def show_torrents(self, chat_id: int, page: int = 0, *, refresh: bool = False) -> None:
        try:
            items = self.torbox_for(chat_id).list_torrents(limit=100, bypass_cache=refresh)
        except Exception as exc:
            LOG.exception("Could not list TorBox torrents")
            self.send(chat_id, f"❌ Не удалось получить список: {html.escape(str(exc))}", self.menu_keyboard())
            return
        items.sort(key=lambda item: str(item.get("created_at") or ""), reverse=True)
        if not items:
            self.send(
                chat_id,
                "📥 <b>TorBox пока пуст</b>\n\nНайди релиз и добавь его — он появится здесь.",
                [[self.search_button(), {"text": "🔄 Обновить", "callback_data": "refresh:0"}],
                 [self.home_button()]],
            )
            return
        pages = max(1, (len(items) + TORBOX_PAGE_SIZE - 1) // TORBOX_PAGE_SIZE)
        page = max(0, min(page, pages - 1))
        visible = items[page * TORBOX_PAGE_SIZE:(page + 1) * TORBOX_PAGE_SIZE]
        lines = [f"📥 <b>Мой TorBox</b> · {len(items)} загрузок · страница {page + 1}/{pages}\n"]
        keyboard: list[list[dict[str, str]]] = []
        for item in visible:
            try:
                torrent_id = safe_torrent_id(item.get("id"))
            except ValueError:
                continue
            lines.append(self.torrent_line(item) + "\n")
            keyboard.append([
                {"text": f"ℹ️ {torrent_id}", "callback_data": f"tinfo:{torrent_id}"},
                {"text": "🗑 Удалить", "callback_data": f"delask:{torrent_id}"},
            ])
        navigation: list[dict[str, str]] = []
        if page > 0:
            navigation.append({"text": "←", "callback_data": f"list:{page - 1}"})
        navigation.append({"text": "🔄", "callback_data": f"refresh:{page}"})
        if page + 1 < pages:
            navigation.append({"text": "→", "callback_data": f"list:{page + 1}"})
        keyboard.append(navigation)
        keyboard.append([self.search_button(), self.home_button()])
        self.send(chat_id, "\n".join(lines), keyboard)

    def show_torrent(self, chat_id: int, torrent_id: int) -> None:
        try:
            item = self.torbox_for(chat_id).get_torrent(torrent_id, bypass_cache=True)
        except Exception as exc:
            self.send(chat_id, f"❌ Не удалось загрузить карточку: {html.escape(str(exc))}")
            return
        if not item:
            self.send(chat_id, "Загрузка уже удалена или недоступна.", [[{"text": "← К списку", "callback_data": "list:0"}]])
            return
        icon, status = status_label(item)
        progress = progress_percent(item.get("progress"))
        speed = int(item.get("download_speed") or 0)
        eta = int(item.get("eta") or 0)
        raw_state = str(item.get("download_state") or "").casefold()
        eta_text = "—" if "no seeds" in raw_state or "stalled" in raw_state else human_duration(eta)
        files = item.get("files") if isinstance(item.get("files"), list) else []
        text = (
            f"{icon} <b>{html.escape(str(item.get('name') or 'Без названия'))}</b>\n\n"
            f"Статус: <b>{html.escape(status)}</b>\n"
            f"Прогресс: <b>{progress:.1f}%</b>\n"
            f"Размер: <b>{human_size(int(item.get('size') or 0))}</b>\n"
            f"Скорость: <b>{human_size(speed)}/с</b>\n"
            f"Осталось: <b>{eta_text}</b>\n"
            f"Сиды / пиры: <b>{int(item.get('seeds') or 0)} / {int(item.get('peers') or 0)}</b>\n"
            f"Файлов: <b>{len(files)}</b>\n"
            f"TorBox ID: <code>{torrent_id}</code>"
        )
        keyboard: list[list[dict[str, str]]] = [[
            {"text": "🔄 Обновить", "callback_data": f"tinfo:{torrent_id}"},
            {"text": "📡 Переподключить", "callback_data": f"tctl:{torrent_id}:reannounce"},
        ]]
        if "pause" in str(item.get("download_state") or "").casefold():
            keyboard.append([{"text": "▶️ Продолжить", "callback_data": f"tctl:{torrent_id}:resume"}])
        keyboard.append([{"text": "🗑 Удалить", "callback_data": f"delask:{torrent_id}"}])
        keyboard.append([{"text": "← К списку", "callback_data": "list:0"}, self.home_button()])
        self.send(chat_id, text, keyboard)

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
        try:
            items = self.torbox_for(chat_id).notifications()
        except Exception as exc:
            self.send(chat_id, f"❌ Не удалось получить уведомления: {html.escape(str(exc))}", self.menu_keyboard())
            return
        if not items:
            self.send(
                chat_id, "🔔 <b>Новых уведомлений нет</b>",
                [[{"text": "🔄 Обновить", "callback_data": "notifications"}], [self.home_button()]],
            )
            return
        items.sort(key=lambda item: str(item.get("created_at") or ""), reverse=True)
        visible = items[:5]
        self.state.mark_notifications(
            [self.notification_key(item) for item in items],
            self._maintenance_state_user(chat_id),
        )
        text = "<b>Последние уведомления TorBox</b>\n\n" + "\n\n".join(self.notification_text(item) for item in visible)
        self.send(
            chat_id, text,
            [[{"text": "🔄 Обновить", "callback_data": "notifications"}, {"text": "🧹 Очистить", "callback_data": "nclearask"}],
             [self.home_button()]],
        )

    def poll_notifications(self, user_id: int | None = None, *, force: bool = False) -> bool:
        if not self.notifications_enabled:
            return True
        user_id = self.owner_id if user_id is None else user_id
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
            for item in new_items:
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

    def maintenance_loop(self) -> None:
        """Run slow TorBox checks away from Telegram's interactive update loop."""
        force = True
        while not STOP.is_set():
            notifications_ok = True
            health_ok = True
            for user_id in self.connected_user_ids():
                notifications_ok = self.poll_notifications(user_id, force=force) and notifications_ok
                health_ok = self.poll_torrent_health(user_id, force=force) and health_ok
            force = False
            # TorBox occasionally responds with long timeouts or Cloudflare
            # errors. Back off after a failure instead of continuously retrying.
            STOP.wait(5 if notifications_ok and health_ok else 60)

    def confirm_add(self, chat_id: int, release: Release | None = None) -> None:
        release = release or self.state.pop_pending(chat_id)
        if not release:
            self.send(chat_id, "Подтверждение устарело. Выбери раздачу ещё раз.", self.menu_keyboard())
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
            )
            return
        except Exception as exc:
            LOG.exception("Could not add torrent")
            self.state.put_pending(chat_id, release)
            self.send(
                chat_id,
                f"❌ Не добавлено в TorBox: {html.escape(str(exc))}",
                [[{"text": "🔄 Повторить", "callback_data": "confirm"},
                  {"text": "Отмена", "callback_data": "cancel"}]],
            )
            return

        # TorBox mutations above have already succeeded. Telegram delivery is
        # deliberately outside that try-block: a failed confirmation message
        # must never put the release back into the retry queue and create a duplicate.
        try:
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
            detail = result.get("detail") or result_data.get("detail") or "принято TorBox"
            quality = video_quality_summary(release, release.details_text)
            voiceover = voiceover_summary(release, release.details_text)
            self.send(
                chat_id,
                f"✅ <b>Добавлено в TorBox</b>\n\n"
                f"🎬 {html.escape(release.title)}\n\n"
                f"🎙 <b>Озвучка:</b> {html.escape(voiceover)}\n"
                f"🎞 <b>Качество:</b> {html.escape(quality)}\n"
                f"📦 <b>{html.escape(release.size_text)}</b>\n"
                f"🟢 {html.escape(friendly_torbox_status(str(detail)))}",
                [[{"text": "← К результатам", "callback_data": "results"}]],
            )
        except Exception:
            LOG.warning("Torrent was added but Telegram confirmation failed", exc_info=True)

    def callback(self, callback: dict[str, Any]) -> None:
        callback_id = str(callback.get("id") or "")
        message = callback.get("message", {})
        chat_id = message.get("chat", {}).get("id")
        inline_message_id = str(callback.get("inline_message_id") or "")
        sender_id = callback.get("from", {}).get("id")
        if not isinstance(chat_id, int) and inline_message_id and isinstance(sender_id, int) and self.user_allowed(sender_id):
            chat_id = sender_id
        data = str(callback.get("data") or "")
        self.answer_callback(callback_id)
        if not isinstance(chat_id, int):
            return
        try:
            self.callback_message_id = int(message.get("message_id")) if message.get("message_id") is not None else None
        except (TypeError, ValueError):
            self.callback_message_id = None
        self.callback_inline_message_id = inline_message_id
        self.capture_screen = not bool(inline_message_id)
        if self.callback_message_id:
            self.state.set_screen_message_id(chat_id, self.callback_message_id)
        parts = data.split(":")
        command = parts[0]
        try:
            if command == "home":
                self.show_home(chat_id)
            elif command == "help":
                self.show_help(chat_id)
            elif command == "account":
                self.show_account(chat_id)
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
                removed = self.credentials.remove_api_key(chat_id) if self.credentials else False
                self._torbox_clients.pop(chat_id, None)
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
            elif command == "notifications":
                self.show_notifications(chat_id)
            elif command in {"catalog", "release"}:
                token = parts[1]
                stored_inline = self.state.get_inline_item(token)
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
                index = int(parts[1])
                release = self.state.get_release(chat_id, index)
                if not release:
                    self.send(chat_id, "Результаты устарели. Выполни поиск заново.", self.menu_keyboard())
                    return
                if command == "info":
                    self.send(chat_id, "⏳ Загружаю карточку релиза…")
                    try:
                        details = self.nnm.topic_summary(release)
                    except Exception as exc:
                        details = f"Карточка источника временно недоступна: {exc}"
                    release = replace(release, details_text=details)
                    self.state.update_release(chat_id, index, release)
                    quality = video_quality_summary(release, details)
                    voiceover = voiceover_summary(release, details)
                    audio_track = audio_track_summary(details)
                    released_by = _labeled_value(details, "Релиз от")
                    detail_lines = [
                        f"🎬 <b>{html.escape(release.title)}</b>",
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
                        [{"text": "➕ Добавить сейчас", "callback_data": f"add:{index}"}],
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
                self.send(chat_id, "✅ Команда принята TorBox.")
                self.show_torrent(chat_id, torrent_id)
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
                self.send(chat_id, f"✅ Загрузка <code>{torrent_id}</code> удалена из TorBox.")
                self.show_torrents(chat_id, refresh=True)
            else:
                self.send(chat_id, "Эта кнопка устарела. Открыл главное меню.")
                self.show_home(chat_id)
        except (IndexError, TypeError, ValueError):
            self.send(chat_id, "Эта кнопка устарела. Открой нужный раздел заново.", self.menu_keyboard())
        except Exception as exc:
            LOG.exception("Callback action failed: %s", data)
            self.send(chat_id, f"❌ Операция не выполнена: {html.escape(str(exc))}", self.menu_keyboard())
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
                torbox_status = f"ошибка: {exc}"
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
        if incoming_message_id:
            self.delete_user_message(chat_id, incoming_message_id)

        normalized_text = BOTTOM_ACTIONS.get(text, text)
        first, _, raw_argument = normalized_text.partition(" ")
        command = first.split("@", 1)[0].casefold() if first.startswith("/") else ""
        argument = raw_argument if command else ""

        # After a user clears the private chat Telegram removes the visible
        # reply keyboard, but our state may still point at the deleted screen.
        # /start is the explicit recovery action, so always recreate the
        # keyboard and obtain a fresh message id.
        force_keyboard = command == "/start"
        if force_keyboard or self.state.bottom_keyboard_version(chat_id) < BOTTOM_KEYBOARD_VERSION:
            try:
                self.install_bottom_keyboard(chat_id, force=force_keyboard)
            except Exception:
                LOG.warning("Could not install persistent Telegram controls", exc_info=True)
        self.callback_message_id = self.state.screen_message_id(chat_id)
        self.callback_inline_message_id = ""
        self.capture_screen = True
        try:
            text = normalized_text
            if command == "/start":
                if argument == "connect":
                    self.prompt_connect(chat_id)
                elif argument:
                    inline_item = self.state.get_inline_item(argument)
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
                self.state.set_mode(chat_id, None)
                self.show_account(chat_id)
            elif command == "/connect":
                self.prompt_connect(chat_id)
            elif command in {"/account", "/settings"} or text == BTN_ACCOUNT:
                self.show_account(chat_id)
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
            ("setMyDescription", {"description": "Подключи свой TorBox API key, находи релизы с постерами и управляй личными загрузками и уведомлениями."}),
            ("setMyCommands", {"commands": [
                {"command": "start", "description": "Главное меню"},
                {"command": "connect", "description": "Подключить свой TorBox"},
                {"command": "account", "description": "Настройки TorBox-аккаунта"},
                {"command": "search", "description": "Найти фильм или сериал"},
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
        while not STOP.is_set():
            try:
                updates = self.call(
                    "getUpdates", offset=self.state.offset(), timeout=45,
                    allowed_updates=["message", "callback_query", "inline_query", "chosen_inline_result"],
                )
                for update in updates:
                    self.state.set_offset(update["update_id"] + 1)
                    # Store the offset first so a process restart cannot replay a
                    # destructive callback that already reached TorBox.
                    self.handle(update)
            except requests.RequestException as exc:
                LOG.warning("Telegram network error: %s", exc)
                STOP.wait(3)
            except Exception:
                LOG.exception("Unhandled bot error")
                STOP.wait(2)


def main() -> int:
    try:
        token = env_required("TELEGRAM_BOT_TOKEN")
        api_key = os.getenv("TORBOX_API_KEY", "").strip()
        owner_id = int(env_required("OWNER_TELEGRAM_ID"))
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
    ).run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
