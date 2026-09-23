"""Метаданные для приложения: постеры, бэкдропы и названия серий.

Имя раздачи («Игра Престолов / Game of Thrones (Сезон 4) [2014, BDRip]»)
превращается в карточку Кинопоиска с постером, обложкой и списком серий.
Кадры серий отдаёт TMDB, если задан `TMDB_API_KEY` — у Кинопоиска их нет.
"""

from __future__ import annotations

import datetime
import json
import logging
import os
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import requests

import torbox_tg_bot as bot

LOG = logging.getLogger("meta_service")

# Каталог рабочих файлов. На VPS сервис живёт под DynamicUser с ProtectSystem,
# и рабочий каталог у него только для чтения — кэши кладутся в StateDirectory.
STATE_DIR = Path(os.getenv("SEARCH_API_STATE_DIR", "").strip() or ".")

CACHE_TTL_SECONDS = 30 * 24 * 3600
CACHE_PATH = Path(os.getenv("META_CACHE_PATH", "").strip() or STATE_DIR / "meta_cache.json")

# Полки живут отдельным кэшем: топы и премьеры меняются за сутки, а карточка
# фильма — нет. Один TTL на оба не подобрать, поэтому и файла два.
SHELF_TTL_SECONDS = 12 * 3600
SHELF_CACHE_PATH = Path(os.getenv("SHELF_CACHE_PATH", "").strip() or STATE_DIR / "shelf_cache.json")

# Полки Главного — ровно то, что даёт API Кинопоиска. `top` без уточнения
# считается топом фильмов: так называется полка в интерфейсе приложения.
# Отдельного эндпоинта «топ фильмов» у API больше нет — v2.2/films/top убран
# из документации, его заменили подборки.
SHELF_COLLECTIONS = {
    "popular": "TOP_POPULAR_ALL",
    "popular_movies": "TOP_POPULAR_MOVIES",
    "popular_series": "POPULAR_SERIES",
    "top": "TOP_250_MOVIES",
    "top_movies": "TOP_250_MOVIES",
    "top_series": "TOP_250_TV_SHOWS",
    "top_xxi": "TOP_100_GREATEST_MOVIES_XXI",
    "closest_releases": "CLOSES_RELEASES",
    "family": "FAMILY",
    "comics": "COMICS_THEME",
    "vampires": "VAMPIRE_THEME",
    "zombies": "ZOMBIE_THEME",
    "catastrophe": "CATASTROPHE_THEME",
    "love": "LOVE_THEME",
    "kids_animation": "KIDS_ANIMATION_THEME",
    "programmers": "PROGRAMMERS_THEME",
    "oscar_2021": "OSKAR_WINNERS_2021",
}

MONTH_NAMES = (
    "JANUARY", "FEBRUARY", "MARCH", "APRIL", "MAY", "JUNE",
    "JULY", "AUGUST", "SEPTEMBER", "OCTOBER", "NOVEMBER", "DECEMBER",
)

# v2.2/films по документации отдаёт не больше 400 карточек (20 страниц по 20),
# подборки — 50 страниц. Дальше каталог просто отвечает пустым списком.
# Живьём 23.09.2026 фильтр отдавал меньше: `total: 100`, `totalPages: 5` на
# каждом из десяти разных запросов, включая «все фильмы с оценкой от 7».
MAX_FILTER_PAGE = 20
MAX_COLLECTION_PAGE = 50

# Подбор для «Что смотреть» в KinoApp (`MetaService.discover`). Значения —
# перечисления фильтра v2.2/films из openapi.json Кинопоиска.
DISCOVER_TYPES = ("ALL", "FILM", "TV_SERIES", "MINI_SERIES", "TV_SHOW")
DISCOVER_ORDERS = ("NUM_VOTE", "RATING", "YEAR")
# У «похожих» в ответе только id, названия и постер, и каждый дополняется
# карточкой фильма — запросом из суточной квоты на холодном кэше. Потолок по
# умолчанию держит цену одного вызова в 13 запросах.
DEFAULT_SIMILAR_LIMIT = 12
MAX_SIMILAR_LIMIT = 20
# Карточек в одном `briefs`: лента просит их порциями по мере прокрутки.
MAX_BRIEF_IDS = 20
# Карточки фильмов пачкой идут параллельно. Лимит эндпоинта карточки — общий,
# 25 запросов в секунду; четырёх потоков хватает с запасом.
DETAIL_WORKERS = 4

# Технический хвост в именах раздач: до него — название, после — мусор релиза.
NOISE_WORDS = {
    "web", "webrip", "webdl", "web-dl", "hdrip", "bdrip", "bluray", "blu-ray", "brrip",
    "dvdrip", "dvd", "hdtv", "remux", "proper", "repack", "extended", "unrated", "uhd",
    "x264", "x265", "h264", "h265", "avc", "hevc", "xvid", "divx", "10bit", "8bit", "hdr",
    "dolby", "vision", "atmos", "aac", "ac3", "eac3", "dts", "dtshd", "truehd", "flac",
    "mvo", "dvo", "avo", "sub", "subs", "dub", "lostfilm", "kravec", "alexfilm", "kerob",
    "amzn", "nf", "netflix", "hulu", "dsnp", "hmax", "atvp", "ivi", "kinopoisk",
}

SEASON_PATTERNS = (
    r"[Сс]езон\w*\s*[:\-]?\s*(\d{1,2})",
    r"\bS(\d{1,2})(?:\D|$)",
    r"\bseason\s*(\d{1,2})",
)

RESOLUTION_RE = re.compile(r"^\d{3,4}[pi]$", re.IGNORECASE)
YEAR_RE = re.compile(r"(?:19|20)\d{2}")


@dataclass
class ParsedName:
    """Что удалось вытащить из имени раздачи."""

    queries: list[str]
    year: int | None
    season: int | None
    # None — по имени не понять, сериал это или фильм
    is_series: bool | None


@dataclass
class TitleMeta:
    """Карточка тайтла для приложения."""

    matched: bool
    source: str = ""
    source_id: int = 0
    tmdb_id: int | None = None
    tmdb_media_type: str | None = None
    tmdb_lookup_version: int = 1
    title: str = ""
    original_title: str = ""
    year: str = ""
    media_type: str = ""
    rating: str = ""
    overview: str = ""
    tagline: str = ""
    poster_url: str = ""
    backdrop_url: str = ""
    logo_url: str = ""
    genres: list[str] = field(default_factory=list)
    countries: list[str] = field(default_factory=list)
    seasons_count: int = 0
    season_hint: int | None = None
    duration_minutes: int = 0
    cast: list[dict[str, str]] = field(default_factory=list)

    @staticmethod
    def empty() -> TitleMeta:
        return TitleMeta(matched=False)


@dataclass
class PersonMeta:
    """Актёр или режиссёр для карусели «актёрский состав»."""

    name: str
    role: str = ""
    photo_url: str = ""
    profession: str = ""


@dataclass
class EpisodeMeta:
    season: int
    episode: int
    title: str = ""
    overview: str = ""
    air_date: str = ""
    still_url: str = ""


@dataclass
class SeasonBrief:
    """Сколько серий в сезоне: приложению этого хватает, чтобы нарисовать выбор."""

    season: int
    episodes: int


@dataclass
class TitleCard:
    """Карточка по идентификатору каталога: TitleMeta плюс внешние id и сезоны.

    Отдельный тип, а не поля в TitleMeta: состава сезонов `meta/match` не
    знает, а форму его ответа уже разбирает выпущенное приложение. Своего
    `tmdb_id` здесь нет намеренно — он приходит внутри `meta` вместе с
    `tmdb_media_type`, и двух источников правды для него быть не должно.
    """

    meta: TitleMeta
    imdb_id: str = ""
    seasons: list[SeasonBrief] = field(default_factory=list)


class TTLCache:
    """Потокобезопасный кэш с TTL и persist в JSON: метаданные почти не меняются."""

    def __init__(self, path: Path = CACHE_PATH, ttl: int = CACHE_TTL_SECONDS) -> None:
        self._path = path
        self._ttl = ttl
        self._lock = threading.Lock()
        self._items: dict[str, tuple[float, Any]] = {}
        self._load()

    def get(self, key: str) -> Any | None:
        with self._lock:
            entry = self._items.get(key)
            if entry is None:
                return None
            stamp, value = entry
            if time.time() - stamp > self._ttl:
                del self._items[key]
                return None
            return value

    def put(self, key: str, value: Any) -> None:
        with self._lock:
            self._items[key] = (time.time(), value)
            self._save_locked()

    def _load(self) -> None:
        if not self._path.exists():
            return
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except Exception:
            LOG.warning("Кэш метаданных повреждён, начинаю заново", exc_info=True)
            return
        now = time.time()
        self._items = {
            key: (stamp, value)
            for key, (stamp, value) in (
                (key, (entry.get("stamp", 0), entry.get("value")))
                for key, entry in raw.items()
                if isinstance(entry, dict)
            )
            if now - stamp <= self._ttl
        }

    def _save_locked(self) -> None:
        payload = {
            key: {"stamp": stamp, "value": value}
            for key, (stamp, value) in self._items.items()
        }
        try:
            tmp = self._path.with_suffix(".tmp")
            tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            tmp.replace(self._path)
        except OSError:
            LOG.warning("Не получилось сохранить кэш метаданных", exc_info=True)


def parse_release_name(name: str) -> ParsedName:
    """Разбирает имя раздачи на запросы к каталогу, год и номер сезона."""
    raw = name.strip()
    season = _first_int(SEASON_PATTERNS, raw)
    # Сезоны диапазоном («Сезон 1-8») сезон-подсказку не дают
    if season is not None and re.search(
        rf"[Сс]езон\w*\s*[:\-]?\s*{season}\s*[-–]\s*\d", raw
    ):
        season = None

    head = re.split(r"[\[({]", raw, maxsplit=1)[0]
    year = _match_year(raw)
    has_series_marker = bool(
        re.search(r"[Сс]езон|\bS\d{1,2}\b|season|[Сс]ери[яий]", raw)
    ) or season is not None
    # Без маркеров тип неизвестен: наказывать сериалы за это нельзя
    is_series = True if has_series_marker else None

    candidates: list[str] = []
    for part in re.split(r"\s*/\s*|\s+\|\s+", head):
        cleaned = _clean_title(part)
        if len(cleaned) >= 2:
            candidates.append(cleaned)
    if not candidates:
        cleaned = _clean_title(head)
        if cleaned:
            candidates.append(cleaned)

    # Латиницу пробуем первой: у Кинопоиска по ней меньше ложных совпадений
    candidates.sort(key=lambda text: 0 if _is_latin(text) else 1)
    unique: list[str] = []
    for candidate in candidates:
        if candidate.casefold() not in {item.casefold() for item in unique}:
            unique.append(candidate)
    return ParsedName(queries=unique, year=year, season=season, is_series=is_series)


def _clean_title(text: str) -> str:
    text = re.sub(r"[._]+", " ", text)
    text = re.sub(r"\b(19|20)\d{2}\b", " ", text)
    for pattern in SEASON_PATTERNS:
        text = re.sub(pattern, " ", text)
    text = re.sub(r"\bS\d{1,2}E\d{1,3}\b", " ", text, flags=re.IGNORECASE)
    words: list[str] = []
    for word in re.split(r"[\s,\-–—:]+", text):
        stripped = word.strip(" .,:;!()[]{}")
        if not stripped:
            continue
        lowered = stripped.casefold()
        if lowered in NOISE_WORDS or RESOLUTION_RE.match(stripped):
            break
        words.append(stripped)
    return " ".join(words).strip()


def _match_year(text: str) -> int | None:
    years = YEAR_RE.findall(text)
    return int(years[0]) if years else None


def _first_int(patterns: tuple[str, ...], text: str) -> int | None:
    for pattern in patterns:
        found = re.search(pattern, text)
        if found:
            try:
                return int(found.group(1))
            except (TypeError, ValueError):
                continue
    return None


def _is_latin(text: str) -> bool:
    letters = [char for char in text if char.isalpha()]
    if not letters:
        return False
    latin = sum(1 for char in letters if char.isascii())
    return latin >= len(letters) / 2


@dataclass
class ReleaseScope:
    """Границы сезонов и серий, объявленные в имени раздачи.

    Это разбор имени *раздачи*, а не имени файла: он отвечает на вопрос «что
    лежит внутри», а не «какая это серия». Чего в имени нет — остаётся None:
    на этих полях клиент строит ранжирование, и «не знаю» там честнее догадки.
    """

    season_from: int | None = None
    season_to: int | None = None
    episode_from: int | None = None
    episode_to: int | None = None
    episodes_declared: int | None = None


# «Сезоны 1-8», «Season 1-5», «S01-S03». Диапазон проверяется раньше одиночного
# шаблона из SEASON_PATTERNS: тот откусил бы от диапазона только левую границу.
SEASON_RANGE_PATTERNS = (
    r"[Сс]езон\w*\s*[:\-]?\s*(\d{1,2})\s*[-–—]\s*(\d{1,2})",
    r"\bseasons?\s*(\d{1,2})\s*[-–—]\s*(\d{1,2})",
    # «S01-S03», «S01-08» и «S1-8E1-73»: у последнего после правой границы
    # сразу идёт маркер серий, поэтому здесь не \b, а «лишь бы не цифра»
    r"\bS(\d{1,2})\s*[-–—]\s*S?(\d{1,2})(?!\d)",
)

# «серии 1-8 из 10», «(07 из 24)», «E01-E12», «Серия 5». Порядок важен:
# без него «8 из 10» перебило бы «серии 1-8 из 10» и дало бы первую серию 8.
EPISODE_PATTERNS = (
    r"[Сс]ери[июяй]\w*\s*[:\-]?\s*(\d{1,3})\s*[-–—]\s*(\d{1,3})\s*(?:из|of)\s*(\d{1,3})",
    r"(?<!\d)(\d{1,3})\s*[-–—]\s*(\d{1,3})\s*(?:из|of)\s*(\d{1,3})(?!\d)",
    r"[Сс]ери[июяй]\w*\s*[:\-]?\s*(\d{1,3})\s*[-–—]\s*(\d{1,3})(?!\s*(?:из|of))",
    # «S02E01-E12»: перед E стоит цифра сезона, поэтому не \b, а «не буква»
    r"(?<![^\W\d_])[eEеЕ](\d{1,3})\s*[-–—]\s*[eEеЕ]?(\d{1,3})\b",
    r"[Сс]ери[июяй]\w*\s*[:\-]?\s*(\d{1,3})\s*(?:из|of)\s*(\d{1,3})",
    r"(?<!\d)(\d{1,3})\s*(?:из|of)\s*(\d{1,3})(?!\d)",
    r"\bS\d{1,2}\s*[eEхx](\d{1,3})\b",
    r"[Сс]ери[яи]\s*[:\-]?\s*(\d{1,3})\b",
)


def parse_release_scope(title: str) -> ReleaseScope:
    """Разбирает имя раздачи на границы сезонов и серий.

    Args:
        title: Имя раздачи с трекера целиком, как его отдал источник.

    Returns:
        Найденные границы; поля, которых в имени нет, остаются None.
    """
    raw = title.strip()
    season_from, season_to = _season_bounds(raw)
    episode_from, episode_to, declared = _episode_bounds(raw)
    return ReleaseScope(
        season_from=season_from, season_to=season_to,
        episode_from=episode_from, episode_to=episode_to, episodes_declared=declared,
    )


def _season_bounds(raw: str) -> tuple[int | None, int | None]:
    for pattern in SEASON_RANGE_PATTERNS:
        found = re.search(pattern, raw)
        if found:
            start, end = int(found.group(1)), int(found.group(2))
            # «Сезон 3-1» — опечатка источника, такие границы не берём
            if start <= end:
                return start, end
    single = _first_int(SEASON_PATTERNS, raw)
    return (single, single) if single is not None else (None, None)


def _episode_bounds(raw: str) -> tuple[int | None, int | None, int | None]:
    for pattern in EPISODE_PATTERNS:
        found = re.search(pattern, raw)
        if not found:
            continue
        numbers = [int(value) for value in found.groups() if value is not None]
        if len(numbers) == 3:
            start, end, declared = numbers
            if start <= end <= declared:
                return start, end, declared
        elif len(numbers) == 2:
            first, second = numbers
            # «07 из 24» — одна серия из объявленных 24, а не диапазон 7…24
            if re.search(r"\bиз\b|\bof\b", found.group(0), re.I):
                if first <= second:
                    return first, first, second
            elif first <= second:
                return first, second, None
        elif numbers:
            return numbers[0], numbers[0], None
    return None, None, None


# Структурное качество: те же маркеры, что у video_quality_summary в боте, но
# значениями, а не строкой для человека. Порядок — от точного к общему:
# «BDRemux 1080p» это remux, хотя подходит и под bdrip, и под bluray.
SOURCE_TYPE_PATTERNS = (
    ("bdremux", r"\bbd[ .-]?remux\b"), ("remux", r"\bremux\b"),
    ("bluray", r"\bblu[ .-]?ray\b|\bbdmv\b|\bbd(?:25|50)\b"),
    ("bdrip", r"\bbd[ .-]?rip\b|\bbr[ .-]?rip\b"),
    ("web-dl", r"\bweb[ .-]?dl\b"), ("webrip", r"\bweb[ .-]?rip\b"), ("web", r"\bweb\b"),
    ("hdtv", r"\bhd[ .-]?tv(?:rip)?\b"), ("hdrip", r"\bhd[ .-]?rip\b"),
    ("dvdrip", r"\bdvd[ .-]?rip\b"), ("dvd", r"\bdvd(?:5|9)?\b"),
    ("satrip", r"\bsat[ .-]?rip\b"), ("ts", r"\bts\b|\btelesync\b"),
    ("camrip", r"\bcam[ .-]?rip\b"),
)

VIDEO_CODEC_PATTERNS = (
    ("av1", r"\bav1\b"),
    ("h265", r"\bh[ .]?265\b|\bhevc\b|\bx265\b"),
    ("h264", r"\bh[ .]?264\b|\bavc\b|\bx264\b"),
    ("xvid", r"\bxvid\b"), ("divx", r"\bdivx\b"), ("mpeg2", r"\bmpeg[ .-]?2\b"),
)

HDR_PATTERNS = (
    ("dolby_vision", r"\bdolby[ .-]?vision\b|\bdo?vi\b"),
    ("hdr10plus", r"\bhdr10\+"), ("hdr10", r"\bhdr10\b"), ("hdr", r"\bhdr\b"),
)

KNOWN_RESOLUTIONS = (2160, 1440, 1080, 720, 576, 480, 360)


@dataclass
class VideoSpec:
    """Разрешение, источник, кодек и HDR — значениями, а не одной строкой."""

    resolution: int | None = None
    source_type: str | None = None
    video_codec: str | None = None
    hdr: str | None = None


def parse_video_spec(title: str, details: str = "") -> VideoSpec:
    """Достаёт структурное качество из имени раздачи и описания темы.

    Args:
        title: Имя раздачи.
        details: Текст темы с трекера, если он уже прочитан.

    Returns:
        Поля, которых в тексте нет, остаются None: «не указано» и «720p» —
        разные ответы, и клиент обязан их различать.
    """
    text = f"{title}\n{details}"
    return VideoSpec(
        resolution=_resolution(text),
        source_type=_first_label(SOURCE_TYPE_PATTERNS, text),
        video_codec=_first_label(VIDEO_CODEC_PATTERNS, text),
        hdr=_first_label(HDR_PATTERNS, text),
    )


def _resolution(text: str) -> int | None:
    if re.search(r"\b4k\b|\buhd\b|\b2160[pi]\b", text, re.I):
        return 2160
    found = re.search(r"\b(\d{3,4})[pi]\b", text, re.I)
    if found and int(found.group(1)) in KNOWN_RESOLUTIONS:
        return int(found.group(1))
    # «1920x1080» пишут в поле «Видео» темы, когда в имени разрешения нет
    frame = re.search(r"\b\d{3,4}\s*[xх×]\s*(\d{3,4})\b", text, re.I)
    if frame and int(frame.group(1)) in KNOWN_RESOLUTIONS:
        return int(frame.group(1))
    return None


def _first_label(patterns: tuple[tuple[str, str], ...], text: str) -> str | None:
    return next((label for label, pattern in patterns if re.search(pattern, text, re.I)), None)


# Вид перевода отдельно от студии: клиенту нужно «есть ли дубляж», а строка
# «Дубляж · LostFilm» отвечает на это только после разбора на его стороне.
VOICEOVER_KIND_PATTERNS = (
    ("dub", r"дублированн\w*|дубляж\w*|\bdub\b"),
    ("multi", r"многоголос\w*|\bmvo\b"),
    ("dual", r"двухголос\w*|\bdvo\b"),
    ("single", r"одноголос\w*|\bavo\b"),
    ("author", r"авторск\w*"),
    ("professional", r"профессиональн\w*"),
    ("licensed", r"лицензи\w*|\bitunes\b"),
    ("subtitles", r"субтитр\w*|\bsub(?:s|title)?\b"),
)

VOICEOVER_STUDIO_PATTERNS = (
    ("Кравец", r"\b(?:kravec|кравец)\b"), ("AlexFilm", r"\balexfilm\b"),
    ("LostFilm", r"\blostfilm\b"), ("Amedia", r"\b(?:amedia|амедиа)\b"),
    ("NewStudio", r"\bnewstudio\b"), ("HDRezka Studio", r"\bhdrezka(?:[ .-]?studio)?\b"),
    ("Red Head Sound", r"\bred[ .-]?head[ .-]?sound\b"), ("Jaskier", r"\bjaskier\b"),
    ("Кубик в Кубе", r"кубик\s+в\s+кубе"), ("TVShows", r"\btvshows\b"),
    ("BaibaKo", r"\bbaibako\b"), ("ColdFilm", r"\bcoldfilm\b"), ("РенТВ", r"\bрентв\b"),
)

# «| D, P, A» в конце имени — компактная запись дорожек, её пишет половина
# раздач вместо слов. Расшифровка та же, что у voiceover_summary в боте.
TRACK_CODE_KINDS = {"D": "dub", "P": "professional", "A": "author"}


@dataclass
class VoiceoverSpec:
    """Виды перевода и названные студии, найденные в имени раздачи."""

    kinds: list[str] = field(default_factory=list)
    studios: list[str] = field(default_factory=list)


def parse_voiceover_spec(title: str, details: str = "") -> VoiceoverSpec:
    """Достаёт структурную озвучку из имени раздачи и описания темы.

    Args:
        title: Имя раздачи.
        details: Текст темы с трекера, если он уже прочитан.

    Returns:
        Списки видов перевода и студий; пустые значат «источник не указал».
    """
    text = f"{title}\n{details}"
    kinds = [label for label, pattern in VOICEOVER_KIND_PATTERNS if re.search(pattern, text, re.I)]
    coded = re.search(r"(?:\||\[)\s*((?:D|P|A)(?:\s*,\s*(?:D|P|A))+)", title, re.I)
    if coded:
        codes = {value.upper() for value in re.findall(r"[DPA]", coded.group(1), re.I)}
        kinds.extend(TRACK_CODE_KINDS[code] for code in sorted(codes))
    if re.search(r"\|\s*D(?:\s*[,|]|$)", title, re.I):
        kinds.append("dub")
    studios = [label for label, pattern in VOICEOVER_STUDIO_PATTERNS if re.search(pattern, text, re.I)]
    return VoiceoverSpec(kinds=list(dict.fromkeys(kinds)), studios=studios)


def normalize_title(text: str) -> list[str]:
    """Слова названия без пунктуации и регистра — для сравнения."""
    cleaned = re.sub(r"[^\w\s]+", " ", text, flags=re.UNICODE)
    return [word for word in cleaned.casefold().split() if word]


def title_similarity(query: str, names: list[str]) -> float:
    """Доля слов запроса, встретившихся в названии: 1.0 — полное совпадение."""
    query_words = normalize_title(query)
    if not query_words:
        return 0.0
    best = 0.0
    for name in names:
        name_words = normalize_title(name)
        if not name_words:
            continue
        common = sum(1 for word in query_words if word in name_words)
        ratio = common / len(query_words)
        # Длинное название против короткого запроса — совпадение слабее
        if len(name_words) > len(query_words):
            ratio *= len(query_words) / len(name_words)
        best = max(best, ratio)
    return best


def score_candidate(item: dict[str, Any], parsed: ParsedName, query: str) -> float:
    """Насколько найденный тайтл похож на то, что мы искали."""
    names = [
        str(item.get(key) or "")
        for key in ("nameRu", "nameEn", "nameOriginal")
    ]
    query_folded = query.casefold()
    similarity = title_similarity(query, [name for name in names if name])
    # «Sharp Obsession» против «Sharp Corner» — половина слов совпала,
    # но это другой фильм. Ниже 0.6 считаем несовпадением, а не «почти».
    if similarity < 0.6:
        return -10
    score = 0.0
    if any(name.casefold() == query_folded for name in names if name):
        score += 6
    elif similarity >= 0.75:
        score += 4
    elif similarity >= 0.5:
        score += 2

    item_year = str(item.get("year") or "")
    if parsed.year and item_year[:4].isdigit():
        delta = abs(int(item_year[:4]) - parsed.year)
        if delta == 0:
            score += 3
        elif delta <= 1:
            score += 2
        elif delta <= 3:
            score += 1
        else:
            score -= 1

    kind = str(item.get("type") or "").upper()
    is_series_item = "SERIAL" in kind or "SERIES" in kind or "TV" in kind
    if parsed.is_series is True:
        score += 2 if is_series_item else -2

    rating = str(item.get("rating") or "").replace(",", ".")
    try:
        score += min(float(rating), 10) / 20
    except ValueError:
        pass
    return score


class CatalogLimitError(RuntimeError):
    """Каталог отказал по лимиту: суточная квота (402) или запросы в секунду (429).

    Отдельный тип, потому что лечится он не повтором запроса, а ожиданием, и
    приложению об этом надо сказать словами, а не «каталог недоступен».
    """


class KinopoiskMeta:
    """Кинопоиск как источник карточек, полок и русских названий серий."""

    SEARCH_URL = "https://kinopoiskapiunofficial.tech/api/v2.1/films/search-by-keyword"
    FILM_URL = "https://kinopoiskapiunofficial.tech/api/v2.2/films/{film_id}"
    SEASONS_URL = "https://kinopoiskapiunofficial.tech/api/v2.2/films/{film_id}/seasons"
    STAFF_URL = "https://kinopoiskapiunofficial.tech/api/v1/staff"
    COLLECTIONS_URL = "https://kinopoiskapiunofficial.tech/api/v2.2/films/collections"
    FILMS_URL = "https://kinopoiskapiunofficial.tech/api/v2.2/films"
    PREMIERES_URL = "https://kinopoiskapiunofficial.tech/api/v2.2/films/premieres"
    FILTERS_URL = "https://kinopoiskapiunofficial.tech/api/v2.2/films/filters"
    SIMILARS_URL = "https://kinopoiskapiunofficial.tech/api/v2.2/films/{film_id}/similars"
    QUOTA_URL = "https://kinopoiskapiunofficial.tech/api/v1/api_keys/{api_key}"

    def __init__(self, api_key: str) -> None:
        self.http = requests.Session()
        bot.configure_read_retries(self.http)
        self._api_key = api_key.strip()
        self.http.headers.update(
            {
                "X-API-KEY": self._api_key,
                "Accept": "application/json",
                "User-Agent": bot.USER_AGENT,
            }
        )

    def _get(self, url: str, params: dict[str, Any] | None = None, timeout: int = 15) -> Any:
        """Один запрос к каталогу с разбором лимитов.

        Args:
            url: Полный адрес эндпоинта.
            params: Параметры запроса.
            timeout: Таймаут в секундах.

        Returns:
            Разобранный JSON ответа.

        Raises:
            CatalogLimitError: Суточная квота (402) или запросы в секунду (429).
        """
        response = self.http.get(url, params=params, timeout=timeout)
        # 402 — кончилась суточная или общая квота, 429 — слишком часто.
        # Документация лимитов: kinopoiskapiunofficial.tech/documentation/api/
        if response.status_code == 402:
            raise CatalogLimitError("Дневная квота Кинопоиска исчерпана")
        if response.status_code == 429:
            raise CatalogLimitError("Кинопоиск просит сбавить темп запросов")
        response.raise_for_status()
        return response.json()

    def search(self, query: str) -> list[dict[str, Any]]:
        payload = self._get(self.SEARCH_URL, {"keyword": query, "page": 1})
        films = payload.get("films") if isinstance(payload, dict) else None
        return [item for item in films or [] if isinstance(item, dict)]

    def film(self, film_id: int) -> dict[str, Any]:
        payload = self._get(self.FILM_URL.format(film_id=film_id))
        return payload if isinstance(payload, dict) else {}

    def staff(self, film_id: int) -> list[dict[str, Any]]:
        payload = self._get(self.STAFF_URL, {"filmId": film_id}, timeout=20)
        return [item for item in payload or [] if isinstance(item, dict)]

    def seasons(self, film_id: int) -> list[dict[str, Any]]:
        payload = self._get(self.SEASONS_URL.format(film_id=film_id), timeout=20)
        items = payload.get("items") if isinstance(payload, dict) else None
        return [item for item in items or [] if isinstance(item, dict)]

    def collection(self, collection_type: str, page: int = 1) -> list[dict[str, Any]]:
        """Подборка Кинопоиска: топы, популярное, тематические списки.

        Args:
            collection_type: Константа из COLLECTION_TYPES.
            page: Страница, 1…50; на странице не больше 20 карточек.

        Returns:
            Карточки подборки в том виде, в каком их отдал каталог.
        """
        payload = self._get(self.COLLECTIONS_URL, {"type": collection_type, "page": page})
        return _items(payload)

    def films(self, params: dict[str, Any]) -> list[dict[str, Any]]:
        """Фильтр каталога: жанр, страна, годы, тип, сортировка.

        Args:
            params: Параметры v2.2/films. Жанр и страна — не больше одного
                значения каждая: это ограничение самого API, не наше.

        Returns:
            Карточки выдачи фильтра.
        """
        payload = self._get(self.FILMS_URL, params)
        return _items(payload)

    def premieres(self, year: int, month: str) -> list[dict[str, Any]]:
        """Премьеры месяца.

        Args:
            year: Год премьеры.
            month: Месяц заглавными латиницей, JANUARY…DECEMBER.

        Returns:
            Карточки премьер; пагинации у этого эндпоинта нет.
        """
        payload = self._get(self.PREMIERES_URL, {"year": year, "month": month})
        return _items(payload)

    def filters(self) -> dict[str, Any]:
        """Справочник жанров и стран: числовые id для фильтра v2.2/films."""
        payload = self._get(self.FILTERS_URL)
        return payload if isinstance(payload, dict) else {}

    def similars(self, film_id: int) -> list[dict[str, Any]]:
        """«Похожие» Кинопоиска.

        Args:
            film_id: Идентификатор Кинопоиска.

        Returns:
            Элементы в порядке каталога. В элементе только `filmId`, названия,
            постер и `relationType` — ни страны, ни жанров, ни оценки.
        """
        payload = self._get(self.SIMILARS_URL.format(film_id=film_id))
        return _items(payload)

    def quota(self) -> dict[str, Any]:
        """Остаток квоты ключа — бесплатный способ узнать её до платных вызовов.

        Ключ у этого эндпоинта идёт в путь: так устроен сам API. Адрес с ним
        не логируется — в лог уходит только результат.
        """
        payload = self._get(self.QUOTA_URL.format(api_key=self._api_key))
        return payload if isinstance(payload, dict) else {}


def _items(payload: Any) -> list[dict[str, Any]]:
    """Список `items` из ответа каталога; у всех списочных ответов он такой."""
    items = payload.get("items") if isinstance(payload, dict) else None
    return [item for item in items or [] if isinstance(item, dict)]


def catalog_card(item: dict[str, Any], details: dict[str, Any] | None = None) -> dict[str, Any]:
    """Расширенная карточка для ленты «Что смотреть».

    Первые десять полей — та же форма, что у `/catalog/popular` и полок
    (`search_api.catalog_payload`): приложение разбирает их одним типом.
    Дальше — то, чего в карточке полки нет: оценка IMDb, а когда известна
    карточка фильма — число голосов, обложка, логотип и «о чём».

    Ловушки ответов Кинопоиска здесь сглажены: `year` строкой в подборках и
    числом в фильтре, `ratingImbd` с опечаткой в подборках, `filmId` вместо
    `kinopoiskId` у «похожих». `year`, `rating` и `rating_imdb` — строки,
    как у остальных карточек сервера.

    Args:
        item: Элемент списочного ответа (фильтр, подборка, «похожие»).
        details: Ответ v2.2/films/{id}; его непустые поля главнее.

    Returns:
        Словарь карточки. `detailed` — пришла ли карточка фильма: без неё
        пустые `tagline` и `backdrop_url` значат «не спрашивали», а не «нет».
    """
    extra = {key: value for key, value in (details or {}).items() if value not in (None, "", [])}
    merged = {**item, **extra}
    name = str(merged.get("nameRu") or merged.get("nameEn") or merged.get("nameOriginal") or "Без названия").strip()
    original_name = str(merged.get("nameEn") or merged.get("nameOriginal") or "").strip()
    year = str(merged.get("year") or "").strip()
    length = merged.get("filmLength")
    return {
        "catalog_id": int(merged.get("kinopoiskId") or merged.get("filmId") or 0),
        "name": name,
        "original_name": original_name if original_name.casefold() != name.casefold() else "",
        "media_type": str(merged.get("type") or "UNKNOWN"),
        "year": year,
        "rating": _rating_text(merged.get("ratingKinopoisk")),
        "countries": [
            str(entry.get("country"))
            for entry in merged.get("countries", []) or []
            if isinstance(entry, dict) and entry.get("country")
        ],
        "genres": [
            str(entry.get("genre"))
            for entry in merged.get("genres", []) or []
            if isinstance(entry, dict) and entry.get("genre")
        ],
        "poster_url": _https(str(merged.get("posterUrlPreview") or merged.get("posterUrl") or "")),
        "search_query": " ".join(part for part in (name, year) if part).strip(),
        "rating_imdb": _rating_text(merged.get("ratingImdb") or merged.get("ratingImbd")),
        "votes": int(merged.get("ratingKinopoiskVoteCount") or 0),
        "tagline": str(merged.get("shortDescription") or "").strip(),
        "overview": str(merged.get("description") or "").strip(),
        "backdrop_url": _https(str(merged.get("coverUrl") or "")),
        "logo_url": _https(str(merged.get("logoUrl") or "")),
        "duration_minutes": int(length) if isinstance(length, int) else 0,
        "detailed": bool(details),
    }


def _rating_text(value: Any) -> str:
    """Оценка строкой, как у остальных карточек сервера; нет оценки — пустая строка."""
    if value in (None, "", 0):
        return ""
    return str(value).strip()


class TMDBStills:
    """Кадры серий: у Кинопоиска их нет, поэтому опционально берём из TMDB."""

    SEARCH_URL = "https://api.themoviedb.org/3/search/tv"
    SEASON_URL = "https://api.themoviedb.org/3/tv/{tv_id}/season/{season}"
    IMAGE_BASE = "https://image.tmdb.org/t/p/w780"

    def __init__(self, api_key: str) -> None:
        self.http = requests.Session()
        bot.configure_read_retries(self.http)
        self.http.headers.update({"Accept": "application/json", "User-Agent": bot.USER_AGENT})
        self._api_key = api_key.strip()
        # v4 read-token — это JWT и передаётся заголовком, v3-ключ — параметром
        self._is_token = self._api_key.startswith("ey") and self._api_key.count(".") == 2
        if self._is_token:
            self.http.headers["Authorization"] = f"Bearer {self._api_key}"

    def _params(self, extra: dict[str, Any] | None = None) -> dict[str, Any]:
        params: dict[str, Any] = dict(extra or {})
        if not self._is_token:
            params["api_key"] = self._api_key
        return params

    def find_series(self, query: str, year: int | None) -> int | None:
        extra: dict[str, Any] = {"query": query}
        if year:
            extra["first_air_date_year"] = year
        response = self.http.get(self.SEARCH_URL, params=self._params(extra), timeout=15)
        response.raise_for_status()
        results = response.json().get("results") or []
        return int(results[0]["id"]) if results and results[0].get("id") else None

    def stills(self, tv_id: int, season: int) -> dict[int, str]:
        response = self.http.get(
            self.SEASON_URL.format(tv_id=tv_id, season=season),
            params=self._params(),
            timeout=20,
        )
        response.raise_for_status()
        episodes = response.json().get("episodes") or []
        result: dict[int, str] = {}
        for episode in episodes:
            number = episode.get("episode_number")
            still = episode.get("still_path")
            if isinstance(number, int) and still:
                result[number] = f"{self.IMAGE_BASE}{still}"
        return result


def resolve_tmdb_library_id(imdb_id: str, expected_type: str) -> tuple[int | None, str | None]:
    """Resolve an exact external ID using TMDB's official redirect, never a fuzzy title search.

    No redirect is followed: only a validated TMDB movie/TV Location is accepted.
    A missing match or temporary upstream failure leaves playback unavailable.
    """
    if not re.fullmatch(r"tt[0-9]{7,12}", imdb_id):
        return None, None
    try:
        response = requests.head(
            "https://www.themoviedb.org/redirect",
            params={"external_source": "imdb_id", "external_id": imdb_id},
            allow_redirects=False, timeout=8,
        )
        if response.status_code not in (301, 302, 303, 307, 308):
            return None, None
        location = urlparse(response.headers.get("Location", ""))
        match = re.fullmatch(r"/(movie|tv)/([1-9][0-9]*)/?", location.path)
        if (location.scheme != "https" or location.netloc != "www.themoviedb.org"
                or location.query or location.fragment or not match
                or match[1] != expected_type):
            return None, None
        return int(match[2]), match[1]
    except requests.RequestException:
        LOG.warning("TMDB external-ID lookup unavailable")
        return None, None


class MetaService:
    """Фасад: имя раздачи → карточка и серии, всё с кэшем."""

    def __init__(
        self,
        kinopoisk: KinopoiskMeta | None,
        tmdb: TMDBStills | None = None,
        cache: TTLCache | None = None,
        shelf_cache: TTLCache | None = None,
    ) -> None:
        self.kinopoisk = kinopoisk
        self.tmdb = tmdb
        self.cache = cache or TTLCache()
        self.shelf_cache = shelf_cache or TTLCache(SHELF_CACHE_PATH, SHELF_TTL_SECONDS)

    @property
    def available(self) -> bool:
        return self.kinopoisk is not None

    def match(self, name: str) -> TitleMeta:
        if self.kinopoisk is None:
            return TitleMeta.empty()
        cache_key = f"match-tmdb-v1:{name.strip().casefold()}"
        cached = self.cache.get(cache_key)
        if cached is not None:
            return TitleMeta(**cached)

        parsed = parse_release_name(name)
        best = self._search_best(parsed, parsed.queries[:2], strict=False)
        if best is None:
            # Укороченный запрос («Obsession MA» → «Obsession») легко находит
            # чужой тайтл, поэтому тут требуем совпадение года
            best = self._search_best(parsed, _short_variants(parsed.queries), strict=True)

        if best is None:
            meta = TitleMeta.empty()
            self.cache.put(cache_key, asdict(meta))
            return meta

        film_id = int(best[1].get("filmId") or best[1].get("kinopoiskId") or 0)
        meta = self._build_meta(film_id, best[1], parsed)
        self.remember_title(film_id, meta.original_title or meta.title, meta.year)
        self.cache.put(cache_key, asdict(meta))
        return meta

    def _search_best(
        self, parsed: ParsedName, queries: list[str], strict: bool
    ) -> tuple[float, dict[str, Any]] | None:
        if self.kinopoisk is None:
            return None
        best: tuple[float, dict[str, Any]] | None = None
        for query in queries:
            try:
                items = self.kinopoisk.search(query)
            except Exception:
                LOG.warning("Каталог не ответил на «%s»", query, exc_info=True)
                continue
            for item in items[:10]:
                score = score_candidate(item, parsed, query)
                if score < 3.5:
                    continue
                if strict and not _year_matches(item, parsed):
                    continue
                if best is None or score > best[0]:
                    best = (score, item)
            # Точное совпадение по названию — дальше искать смысла нет
            if best and best[0] >= 6:
                break
        return best

    def _film(self, film_id: int) -> dict[str, Any]:
        """Карточка фильма из каталога с кэшем.

        Args:
            film_id: Идентификатор Кинопоиска.

        Returns:
            Ответ v2.2/films/{id} или пустой словарь, если каталог не ответил.
        """
        if not film_id or self.kinopoisk is None:
            return {}
        cache_key = f"film:{film_id}"
        cached = self.cache.get(cache_key)
        if cached is not None:
            return cached
        try:
            details = self.kinopoisk.film(film_id)
        except CatalogLimitError:
            raise
        except Exception:
            LOG.warning("Не получилось прочитать карточку %s", film_id, exc_info=True)
            return {}
        self.cache.put(cache_key, details)
        return details

    def _build_meta(self, film_id: int, item: dict[str, Any], parsed: ParsedName) -> TitleMeta:
        # Через _film, а не напрямую: одна и та же карточка нужна и match,
        # и title_card, и _tmdb_query, а суточная квота каталога — 500 запросов
        details = self._film(film_id)

        media_type = str(details.get("type") or item.get("type") or "").upper()
        seasons_count = 0
        if details.get("serial") or "SERIAL" in media_type or "TV" in media_type:
            seasons_count = self._seasons_count(film_id)

        expected_type = "tv" if details.get("serial") or media_type in {"TV_SERIES", "MINI_SERIES", "TV_SHOW"} else "movie"
        tmdb_id, tmdb_media_type = resolve_tmdb_library_id(str(details.get("imdbId") or ""), expected_type)
        rating = details.get("ratingKinopoisk") or item.get("rating") or ""
        return TitleMeta(
            matched=True,
            source="kinopoisk",
            source_id=film_id,
            tmdb_id=tmdb_id,
            tmdb_media_type=tmdb_media_type,
            title=str(details.get("nameRu") or item.get("nameRu") or item.get("nameEn") or "").strip(),
            original_title=str(
                details.get("nameOriginal") or item.get("nameEn") or item.get("nameOriginal") or ""
            ).strip(),
            year=str(details.get("year") or item.get("year") or "").strip(),
            media_type=media_type,
            rating=str(rating).strip(),
            overview=str(details.get("description") or item.get("description") or "").strip(),
            tagline=str(details.get("shortDescription") or "").strip(),
            poster_url=_https(str(details.get("posterUrl") or item.get("posterUrl") or "")),
            backdrop_url=_https(str(details.get("coverUrl") or "")),
            logo_url=_https(str(details.get("logoUrl") or "")),
            genres=[
                str(entry.get("genre"))
                for entry in details.get("genres", []) or []
                if isinstance(entry, dict) and entry.get("genre")
            ],
            countries=[
                str(entry.get("country"))
                for entry in details.get("countries", []) or []
                if isinstance(entry, dict) and entry.get("country")
            ],
            seasons_count=seasons_count,
            season_hint=parsed.season,
            duration_minutes=int(details.get("filmLength") or 0)
            if str(details.get("filmLength") or "").isdigit()
            else 0,
            cast=self._cast(film_id),
        )

    def _cast(self, film_id: int) -> list[dict[str, str]]:
        """Режиссёры и первые роли: у сериалов список персон бывает на сотни строк."""
        if not film_id or self.kinopoisk is None:
            return []
        cache_key = f"staff:{film_id}"
        cached = self.cache.get(cache_key)
        if cached is not None:
            return cached
        try:
            staff = self.kinopoisk.staff(film_id)
        except Exception:
            LOG.warning("Не получилось прочитать состав %s", film_id, exc_info=True)
            return []
        directors = [item for item in staff if item.get("professionKey") == "DIRECTOR"][:2]
        actors = [item for item in staff if item.get("professionKey") == "ACTOR"][:15]
        people = [
            asdict(
                PersonMeta(
                    name=str(item.get("nameRu") or item.get("nameEn") or "").strip(),
                    role=str(item.get("description") or "").strip(),
                    photo_url=_https(str(item.get("posterUrl") or "")),
                    profession="director" if item.get("professionKey") == "DIRECTOR" else "actor",
                )
            )
            for item in directors + actors
        ]
        people = [person for person in people if person["name"]]
        self.cache.put(cache_key, people)
        return people

    def _seasons_count(self, film_id: int) -> int:
        seasons = self._seasons(film_id)
        return len(seasons)

    def _seasons(self, film_id: int) -> list[dict[str, Any]]:
        if not film_id or self.kinopoisk is None:
            return []
        cache_key = f"seasons:{film_id}"
        cached = self.cache.get(cache_key)
        if cached is not None:
            return cached
        try:
            seasons = self.kinopoisk.seasons(film_id)
        except Exception:
            LOG.warning("Не получилось прочитать сезоны %s", film_id, exc_info=True)
            return []
        self.cache.put(cache_key, seasons)
        return seasons

    def title_card(self, film_id: int) -> TitleCard:
        """Карточка тайтла по идентификатору каталога.

        Args:
            film_id: Идентификатор Кинопоиска.

        Returns:
            TitleMeta (внутри уже есть tmdb_id и tmdb_media_type), imdb_id и
            состав сезонов. Если каталог тайтла не знает, внутри лежит
            TitleMeta.empty().

        Raises:
            CatalogLimitError: Квота каталога исчерпана.
        """
        if self.kinopoisk is None:
            return TitleCard(meta=TitleMeta.empty())
        details = self._film(film_id)
        if not details:
            return TitleCard(meta=TitleMeta.empty())
        parsed = ParsedName(queries=[], year=None, season=None, is_series=None)
        meta = self._build_meta(film_id, details, parsed)
        self.remember_title(film_id, meta.original_title or meta.title, meta.year)
        seasons = [
            SeasonBrief(
                season=int(entry.get("number") or 0),
                episodes=len([item for item in entry.get("episodes", []) or [] if isinstance(item, dict)]),
            )
            for entry in self._seasons(film_id)
        ]
        return TitleCard(
            meta=meta,
            imdb_id=str(details.get("imdbId") or "").strip(),
            seasons=[item for item in seasons if item.season],
        )

    def _tmdb_series_id(self, film_id: int) -> int:
        """Идентификатор сериала в TMDB, под которым лежат кадры серий.

        Сначала точный путь: по IMDb-идентификатору через редирект TMDB —
        ключ для этого не нужен и промахнуться нельзя. Поиск по названию
        остаётся запасным: он требует ключа и способен найти не тот сериал.

        Args:
            film_id: Идентификатор Кинопоиска.

        Returns:
            Идентификатор сериала в TMDB или 0, если его не удалось узнать.
        """
        cached = self.cache.get(f"tmdb:{film_id}")
        if cached is not None:
            return int(cached or 0)

        details = self._film(film_id)
        tmdb_id, _ = resolve_tmdb_library_id(str(details.get("imdbId") or ""), "tv")
        if not tmdb_id and self.tmdb is not None:
            query, year = self._tmdb_query(film_id)
            if query:
                try:
                    tmdb_id = self.tmdb.find_series(query, year)
                except Exception:
                    LOG.warning("TMDB не ответил на «%s»", query, exc_info=True)
                    return 0
        self.cache.put(f"tmdb:{film_id}", tmdb_id or 0)
        return int(tmdb_id or 0)

    def shelf(self, kind: str, page: int = 1) -> list[bot.CatalogTitle]:
        """Полка Главного экрана.

        Args:
            kind: Ключ полки: значение из SHELF_COLLECTIONS, `premieres`,
                `genre/<название>` или `year/<год>`.
            page: Страница выдачи, начиная с 1.

        Returns:
            Карточки полки в той же форме, что и `/catalog/popular`.

        Raises:
            ValueError: Полки с таким ключом у каталога нет.
            CatalogLimitError: Квота каталога исчерпана.
        """
        if self.kinopoisk is None:
            return []
        cache_key = f"shelf:{kind.casefold()}:{page}"
        cached = self.shelf_cache.get(cache_key)
        if cached is None:
            cached = self._load_shelf(kind, page)
            self.shelf_cache.put(cache_key, cached)
        # Тот же маппер, что у бота: карточка полки и карточка каталога
        # обязаны быть одной формы, иначе приложение разбирает их дважды
        return bot.KinopoiskCatalog._titles_from_items(cached)

    def _load_shelf(self, kind: str, page: int) -> list[dict[str, Any]]:
        assert self.kinopoisk is not None
        key = kind.strip().strip("/").casefold()
        if key in SHELF_COLLECTIONS:
            return self.kinopoisk.collection(SHELF_COLLECTIONS[key], min(max(page, 1), MAX_COLLECTION_PAGE))
        if key == "premieres":
            today = datetime.date.today()
            return self.kinopoisk.premieres(today.year, MONTH_NAMES[today.month - 1])
        prefix, _, value = key.partition("/")
        safe_page = min(max(page, 1), MAX_FILTER_PAGE)
        if prefix == "genre" and value:
            genre_id = self.genre_id(value)
            if genre_id is None:
                raise ValueError(f"Каталог не знает жанра «{value}»")
            return self.kinopoisk.films({"genres": genre_id, "order": "RATING", "page": safe_page})
        if prefix == "year" and value.isdigit():
            year = int(value)
            return self.kinopoisk.films(
                {"yearFrom": year, "yearTo": year, "order": "RATING", "page": safe_page}
            )
        raise ValueError(f"Неизвестная полка «{kind}»")

    def genres(self) -> list[dict[str, Any]]:
        """Справочник жанров каталога: числовые id для фильтра по жанру."""
        return self._directory("genres")

    def countries(self) -> list[dict[str, Any]]:
        """Справочник стран каталога: числовые id для фильтра по стране."""
        return self._directory("countries")

    def _directory(self, kind: str) -> list[dict[str, Any]]:
        """Справочник `genres` или `countries` из v2.2/films/filters.

        Оба приходят одним ответом и кэшируются разом: иначе второй
        справочник стоил бы второго запроса из той же суточной квоты.
        """
        if self.kinopoisk is None:
            return []
        cached = self.cache.get(f"filters:{kind}")
        if cached is not None:
            return cached
        try:
            payload = self.kinopoisk.filters()
        except CatalogLimitError:
            raise
        except Exception:
            LOG.warning("Не получилось прочитать справочник жанров и стран", exc_info=True)
            return []
        wanted: list[dict[str, Any]] = []
        for name, field_name in (("genres", "genre"), ("countries", "country")):
            entries = [
                {"id": int(entry["id"]), field_name: str(entry.get(field_name) or "")}
                for entry in payload.get(name, []) or []
                if isinstance(entry, dict) and str(entry.get("id") or "").isdigit() and entry.get(field_name)
            ]
            self.cache.put(f"filters:{name}", entries)
            if name == kind:
                wanted = entries
        return wanted

    def genre_id(self, name: str) -> int | None:
        """Числовой id жанра по русскому названию; без него фильтр не работает."""
        wanted = name.strip().casefold()
        return next(
            (entry["id"] for entry in self.genres() if str(entry["genre"]).casefold() == wanted),
            None,
        )

    def country_id(self, name: str) -> int | None:
        """Числовой id страны по русскому названию, как в карточке: «США», «Корея Южная»."""
        wanted = name.strip().casefold()
        return next(
            (entry["id"] for entry in self.countries() if str(entry["country"]).casefold() == wanted),
            None,
        )

    def discover(
        self,
        media_type: str = "ALL",
        genre: str = "",
        country: str = "",
        year_from: int | None = None,
        year_to: int | None = None,
        rating_from: float | None = None,
        order: str = "NUM_VOTE",
        page: int = 1,
    ) -> list[dict[str, Any]]:
        """Подбор по фильтру каталога — лента «Что смотреть» в KinoApp.

        В отличие от полок жанра и года, по умолчанию идёт по числу голосов.
        Сортировка по оценке без порога голосов поднимает наверх то, что
        оценили двадцать человек: живьём 23.09.2026 первые двадцать фильмов
        2025–2026 по оценке оказались короткометражками, «фильмами о фильме»
        и спортивной документалкой. По голосам сверху — то, что смотрели.

        Страна — главный рычаг: без неё верх новинок по голосам занят тем, за
        что голосует аудитория Кинопоиска. Живьём все двадцать сериалов
        2024–2026 на первой странице оказались российскими, а с `country=США`
        первая страница — «Сёгун», «Фоллаут» и «Питт».

        Args:
            media_type: `ALL`, `FILM`, `TV_SERIES`, `MINI_SERIES` или `TV_SHOW`.
            genre: Жанр по-русски, как в карточке; пусто — любой.
            country: Страна по-русски, как в карточке; пусто — любая.
            year_from: Нижняя граница года.
            year_to: Верхняя граница года.
            rating_from: Порог оценки Кинопоиска.
            order: `NUM_VOTE`, `RATING` или `YEAR`.
            page: Страница, с 1.

        Returns:
            Расширенные карточки (`catalog_card`) без полей карточки фильма.

        Raises:
            ValueError: Незнакомые каталогу тип, порядок, жанр или страна.
            CatalogLimitError: Квота каталога исчерпана.
        """
        if self.kinopoisk is None:
            return []
        kind = media_type.strip().upper() or "ALL"
        if kind not in DISCOVER_TYPES:
            raise ValueError(f"Неизвестный тип «{media_type}»")
        sort = order.strip().upper() or "NUM_VOTE"
        if sort not in DISCOVER_ORDERS:
            raise ValueError(f"Неизвестный порядок «{order}»")
        params: dict[str, Any] = {"order": sort, "page": min(max(page, 1), MAX_FILTER_PAGE)}
        if kind != "ALL":
            params["type"] = kind
        if genre.strip():
            genre_id = self.genre_id(genre)
            if genre_id is None:
                raise ValueError(f"Каталог не знает жанра «{genre.strip()}»")
            params["genres"] = genre_id
        if country.strip():
            country_id = self.country_id(country)
            if country_id is None:
                raise ValueError(f"Каталог не знает страны «{country.strip()}»")
            params["countries"] = country_id
        if year_from:
            params["yearFrom"] = year_from
        if year_to:
            params["yearTo"] = year_to
        if rating_from is not None:
            params["ratingFrom"] = rating_from
        # Ключ — сами параметры запроса: по ним одна выдача отличается от другой
        cache_key = "discover:" + json.dumps(params, sort_keys=True, ensure_ascii=False)
        cached = self.shelf_cache.get(cache_key)
        if cached is None:
            cached = self.kinopoisk.films(params)
            self.shelf_cache.put(cache_key, cached)
        return [catalog_card(item) for item in cached]

    def similar(self, film_id: int, limit: int = DEFAULT_SIMILAR_LIMIT) -> list[dict[str, Any]]:
        """«Похожие» Кинопоиска — каждый с карточкой фильма.

        У самих «похожих» в элементе нет ни страны, ни жанров, ни оценки, а
        без страны приложение не может отсеять то, чего не смотрит. Поэтому
        каждый дополняется карточкой v2.2/films/{id} — той же, что кэширует
        страница тайтла, так что открытие похожего потом ничего не стоит.

        Цена на холодном кэше — 1 + limit запросов, дальше 0 в течение 30 дней.

        Args:
            film_id: Тайтл, к которому ищем похожие.
            limit: Сколько первых похожих отдать, 1…MAX_SIMILAR_LIMIT.

        Returns:
            Расширенные карточки в порядке каталога. Похожий, чья карточка не
            пришла, остаётся с `detailed: false` и без страны.

        Raises:
            CatalogLimitError: Квота исчерпана раньше, чем пришёл список.
        """
        if self.kinopoisk is None or film_id <= 0:
            return []
        cache_key = f"similars:{film_id}"
        items = self.cache.get(cache_key)
        if items is None:
            items = self.kinopoisk.similars(film_id)
            self.cache.put(cache_key, items)
        head = [item for item in items if str(item.get("filmId") or "").isdigit()]
        head = head[: min(max(limit, 1), MAX_SIMILAR_LIMIT)]
        details = self._films([int(item["filmId"]) for item in head])
        return [catalog_card(item, details.get(int(item["filmId"]))) for item in head]

    def briefs(self, film_ids: list[int]) -> list[dict[str, Any]]:
        """Карточки для ленты: обложка, логотип и «о чём» пачкой.

        Args:
            film_ids: Идентификаторы Кинопоиска; дальше MAX_BRIEF_IDS не читаются.

        Returns:
            Расширенные карточки в порядке запроса. Тайтлов, которых каталог
            не отдал, в ответе нет.

        Raises:
            CatalogLimitError: Квота исчерпана, и ни одной карточки нет в кэше.
        """
        if self.kinopoisk is None:
            return []
        wanted = list(dict.fromkeys(film_id for film_id in film_ids if film_id > 0))[:MAX_BRIEF_IDS]
        details = self._films(wanted)
        return [catalog_card(details[film_id], details[film_id]) for film_id in wanted if film_id in details]

    def _films(self, film_ids: list[int]) -> dict[int, dict[str, Any]]:
        """Карточки фильмов пачкой: кэш сразу, недостающие — параллельно.

        Квота, кончившаяся посреди пачки, не роняет её целиком: что успели,
        то и отдаём. CatalogLimitError поднимается, только если не нашлось
        ни одной карточки — иначе пустой ответ выглядел бы «похожих нет».
        """
        found: dict[int, dict[str, Any]] = {}
        missing: list[int] = []
        for film_id in dict.fromkeys(film_ids):
            cached = self.cache.get(f"film:{film_id}")
            if cached:
                found[film_id] = cached
            else:
                missing.append(film_id)
        if not missing:
            return found
        limit_error: CatalogLimitError | None = None
        with ThreadPoolExecutor(max_workers=min(DETAIL_WORKERS, len(missing))) as pool:
            futures = {pool.submit(self._film, film_id): film_id for film_id in missing}
            for future in as_completed(futures):
                try:
                    details = future.result()
                except CatalogLimitError as exc:
                    limit_error = exc
                    continue
                if details:
                    found[futures[future]] = details
        if limit_error is not None and not found:
            raise limit_error
        return found

    def quota(self) -> dict[str, Any]:
        """Остаток суточной квоты каталога — без кэша, иначе цифра врёт."""
        if self.kinopoisk is None:
            return {}
        return self.kinopoisk.quota()

    def episodes(self, film_id: int, season: int | None = None) -> list[EpisodeMeta]:
        """Серии тайтла: названия и описания из Кинопоиска, кадры — из TMDB."""
        seasons = self._seasons(film_id)
        result: list[EpisodeMeta] = []
        for entry in seasons:
            number = entry.get("number")
            if season is not None and number != season:
                continue
            for episode in entry.get("episodes", []) or []:
                if not isinstance(episode, dict):
                    continue
                result.append(
                    EpisodeMeta(
                        season=int(episode.get("seasonNumber") or number or 0),
                        episode=int(episode.get("episodeNumber") or 0),
                        title=str(episode.get("nameRu") or episode.get("nameEn") or "").strip(),
                        overview=str(episode.get("synopsis") or "").strip(),
                        air_date=str(episode.get("releaseDate") or "").strip(),
                    )
                )
        self._attach_stills(film_id, result)
        return result

    def _attach_stills(self, film_id: int, episodes: list[EpisodeMeta]) -> None:
        # Сами кадры без ключа TMDB не отдаёт: /3/tv/{id}/season/{n} требует
        # api_key. Ключ-фри тут только определение идентификатора сериала.
        if self.tmdb is None or not episodes:
            return
        tv_id = self._tmdb_series_id(film_id)
        if not tv_id:
            return
        seasons = {episode.season for episode in episodes}
        for season in seasons:
            cache_key = f"stills:{film_id}:{season}"
            stills = self.cache.get(cache_key)
            if stills is None:
                try:
                    stills = {str(key): value for key, value in self.tmdb.stills(int(tv_id), season).items()}
                except Exception:
                    LOG.warning("TMDB не отдал кадры %s/%s", tv_id, season, exc_info=True)
                    stills = {}
                self.cache.put(cache_key, stills)
            for episode in episodes:
                if episode.season == season:
                    episode.still_url = stills.get(str(episode.episode), "")

    def remember_title(self, film_id: int, title: str, year: str = "") -> None:
        """Название нужно для поиска в TMDB — кладём рядом с карточкой."""
        if film_id and title:
            self.cache.put(f"tmdbq:{film_id}", {"title": title, "year": year})

    def _tmdb_query(self, film_id: int) -> tuple[str, int | None]:
        """Оригинальное название и год: по ним TMDB находит сериал точнее."""
        cached = self.cache.get(f"tmdbq:{film_id}")
        if isinstance(cached, dict) and cached.get("title"):
            year = str(cached.get("year") or "")[:4]
            return str(cached["title"]), int(year) if year.isdigit() else None
        if self.kinopoisk is None or not film_id:
            return "", None
        try:
            details = self.kinopoisk.film(film_id)
        except Exception:
            LOG.warning("Не получилось прочитать карточку %s для TMDB", film_id, exc_info=True)
            return "", None
        title = str(details.get("nameOriginal") or details.get("nameRu") or "").strip()
        year = str(details.get("startYear") or details.get("year") or "")[:4]
        self.remember_title(film_id, title, year)
        return title, int(year) if year.isdigit() else None


def _short_variants(queries: list[str]) -> list[str]:
    """Запросы без последнего слова: «Obsession MA» → «Obsession»."""
    variants: list[str] = []
    for query in queries[:2]:
        words = query.split()
        if len(words) > 1:
            variants.append(" ".join(words[:-1]))
    seen: set[str] = set()
    unique: list[str] = []
    for variant in variants:
        folded = variant.casefold()
        if folded and folded not in seen:
            seen.add(folded)
            unique.append(variant)
    return unique


def _year_matches(item: dict[str, Any], parsed: ParsedName) -> bool:
    item_year = str(item.get("year") or "")[:4]
    return bool(parsed.year) and item_year.isdigit() and int(item_year) == parsed.year


def _https(url: str) -> str:
    url = url.strip()
    if url.startswith("http://"):
        return "https://" + url.removeprefix("http://")
    return url


def build_meta_service() -> MetaService:
    """Собирает сервис из тех же переменных окружения, что и бот."""
    kinopoisk_key = os.getenv("KINOPOISK_API_KEY", "").strip()
    tmdb_key = os.getenv("TMDB_API_KEY", "").strip()
    return MetaService(
        kinopoisk=KinopoiskMeta(kinopoisk_key) if kinopoisk_key else None,
        tmdb=TMDBStills(tmdb_key) if tmdb_key else None,
    )
