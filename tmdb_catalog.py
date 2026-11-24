"""TMDB API v3 catalog engine (`language=ru-RU`) matching the Kinopoisk API contract.

Provides drop-in shelf, discover, search, title detail/card, briefs, similar,
episodes, and IMDb-lookup operations backed by TMDB while preserving legacy
Kinopoisk IDs (`< 100_000_000`) via non-overlapping movie/TV ID offsets.
"""

from __future__ import annotations

import datetime
import logging
import math
import re
import threading
from collections.abc import Callable, Iterable
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

import torbox_tg_bot as bot

LOG = logging.getLogger("tmdb_catalog")

TMDB_API_BASE = "https://api.themoviedb.org/3"
TMDB_MOVIE_OFFSET = 100_000_000
TMDB_TV_OFFSET = 200_000_000
_MAX_CATALOG_ID = 300_000_000

DISCOVER_TYPES = ("ALL", "FILM", "TV_SERIES", "MINI_SERIES", "TV_SHOW")
DISCOVER_ORDERS = ("NUM_VOTE", "RATING", "YEAR")

MAX_SHELF_PAGE = 50
MAX_DISCOVER_PAGE = 50
DEFAULT_SIMILAR_LIMIT = 12
MAX_SIMILAR_LIMIT = 20
MAX_BRIEF_IDS = 20
DEFAULT_TIMEOUT = 15

IMG_FILENAME_RE = re.compile(r"^[A-Za-z0-9_-]{8,64}\.(?:jpg|png)$")
IMG_SIZE_RE = re.compile(r"^(?:w\d{2,4}|original)$")
IMDB_ID_RE = re.compile(r"^tt[0-9]{7,12}$")

LOGO_LANGUAGES: tuple[str | None, ...] = ("ru", "en", None)
ANIME_KEYWORD_ID = "210024"
TV_NON_FICTION_GENRES = "10763,10764,10767"
TV_NON_FICTION_GENRE_IDS = frozenset({10763, 10764, 10767})
EXCLUDED_ORIGIN_CODES = frozenset({"RU", "SU"})

NON_RUSSIAN_ORIGIN_COUNTRIES = (
    "US|GB|CA|FR|DE|IT|ES|AU|NZ|IE|SE|DK|NO|NL|BE|CH|AT|KR|JP|CN|HK|MX|BR|IN|PL|CZ|FI"
)
KIDS_ORIGIN_COUNTRIES = "US|GB|CA|FR|AU|IE|DE|DK|BE|NZ"

# TMDB genre ID -> lowercase Russian genre names matching TV/iOS content policy & discover.
GENRE_MAP: dict[int, tuple[str, ...]] = {
    28: ("боевик",),
    12: ("приключения",),
    16: ("мультфильм",),
    35: ("комедия",),
    80: ("криминал",),
    99: ("документальный",),
    18: ("драма",),
    10751: ("семейный",),
    14: ("фэнтези",),
    36: ("история",),
    27: ("ужасы",),
    10402: ("музыка",),
    9648: ("детектив",),
    10749: ("мелодрама",),
    878: ("фантастика",),
    53: ("триллер",),
    10752: ("военный",),
    37: ("вестерн",),
    10770: ("телефильм",),
    # TV-specific & combo genres
    10759: ("боевик", "приключения"),
    10762: ("детский", "семейный"),
    10763: ("новости",),
    10764: ("реальное тв",),
    10765: ("фантастика", "фэнтези"),
    10766: ("мелодрама",),
    10767: ("ток-шоу",),
    10768: ("военный", "драма"),
}

MOVIE_GENRE_IDS: dict[str, int] = {
    "боевик": 28,
    "приключения": 12,
    "мультфильм": 16,
    "аниме": 16,
    "комедия": 35,
    "криминал": 80,
    "документальный": 99,
    "драма": 18,
    "семейный": 10751,
    "детский": 10751,
    "фэнтези": 14,
    "история": 36,
    "биография": 36,
    "ужасы": 27,
    "музыка": 10402,
    "мюзикл": 10402,
    "детектив": 9648,
    "мелодрама": 10749,
    "фантастика": 878,
    "триллер": 53,
    "военный": 10752,
    "вестерн": 37,
    "телефильм": 10770,
}

TV_GENRE_IDS: dict[str, int] = {
    "боевик": 10759,
    "приключения": 10759,
    "мультфильм": 16,
    "аниме": 16,
    "комедия": 35,
    "криминал": 80,
    "документальный": 99,
    "драма": 18,
    "семейный": 10751,
    "детский": 10762,
    "фантастика": 10765,
    "фэнтези": 10765,
    "детектив": 9648,
    # TMDB TV lacks genre 53 (Thriller); 9648 (Mystery) is the closest single TV ID,
    # and discover() broadens TV thriller queries to "9648|80" (Mystery OR Crime).
    "триллер": 9648,
    "мелодрама": 10766,
    "военный": 10768,
    "вестерн": 37,
    "новости": 10763,
    "реальное тв": 10764,
    "ток-шоу": 10767,
}

ISO_TO_COUNTRY: dict[str, str] = {
    "US": "США",
    "GB": "Великобритания",
    "FR": "Франция",
    "CA": "Канада",
    "AU": "Австралия",
    "ES": "Испания",
    "DE": "Германия",
    "IT": "Италия",
    "JP": "Япония",
    "KR": "Корея Южная",
    "RU": "Россия",
    "SU": "СССР",
    "CN": "Китай",
    "IN": "Индия",
    "IE": "Ирландия",
    "NZ": "Новая Зеландия",
    "SE": "Швеция",
    "DK": "Дания",
    "NO": "Норвегия",
    "BE": "Бельгия",
    "NL": "Нидерланды",
    "PL": "Польша",
    "CZ": "Чехия",
    "MX": "Мексика",
    "BR": "Бразилия",
    "TR": "Турция",
    "UA": "Украина",
    "BY": "Беларусь",
    "KZ": "Казахстан",
    "HK": "Гонконг",
    "TW": "Тайвань",
    "AT": "Австрия",
    "CH": "Швейцария",
    "FI": "Финляндия",
    "IL": "Израиль",
    "ZA": "ЮАР",
    "AR": "Аргентина",
    "PT": "Португалия",
    "HU": "Венгрия",
    "RO": "Румыния",
    "GR": "Греция",
    "TH": "Таиланд",
    "ID": "Индонезия",
    "IS": "Исландия",
    "CO": "Колумбия",
    "CL": "Чили",
    "PE": "Перу",
    "PH": "Филиппины",
    "VN": "Вьетнам",
    "SG": "Сингапур",
    "MY": "Малайзия",
    "IR": "Иран",
    "EG": "Египет",
    "SA": "Саудовская Аравия",
    "AE": "ОАЭ",
    "SK": "Словакия",
    "BG": "Болгария",
    "HR": "Хорватия",
    "RS": "Сербия",
    "SI": "Словения",
    "EE": "Эстония",
    "LV": "Латвия",
    "LT": "Литва",
    "GE": "Грузия",
    "AM": "Армения",
    "AZ": "Азербайджан",
    "UZ": "Узбекистан",
    "LU": "Люксембург",
    "MT": "Мальта",
    "CY": "Кипр",
    "NG": "Нигерия",
    "MA": "Марокко",
    "PK": "Пакистан",
    "KP": "Корея Северная",
    "XC": "Чехословакия",
    "XG": "ГДР",
    "YU": "Югославия",
}

COUNTRY_ALIASES: dict[str, str] = {
    "южная корея": "KR",
    "республика корея": "KR",
    "корея": "KR",
    "англия": "GB",
    "соединенное королевство": "GB",
    "соединенные штаты": "US",
    "соединенные штаты америки": "US",
    "рф": "RU",
    "российская федерация": "RU",
    "белоруссия": "BY",
    "голландия": "NL",
    "чешская республика": "CZ",
}

_COUNTRY_NAME_TO_ISO: dict[str, str] = {
    **{name.casefold().replace("ё", "е"): code for code, name in ISO_TO_COUNTRY.items()},
    **COUNTRY_ALIASES,
}

LANG_TO_COUNTRY: dict[str, str] = {
    "en": "США",
    "fr": "Франция",
    "es": "Испания",
    "de": "Германия",
    "it": "Италия",
    "ja": "Япония",
    "ko": "Корея Южная",
    "ru": "Россия",
    "zh": "Китай",
    "cn": "Китай",
    "hi": "Индия",
    "ta": "Индия",
    "te": "Индия",
    "ml": "Индия",
    "sv": "Швеция",
    "da": "Дания",
    "no": "Норвегия",
    "nb": "Норвегия",
    "nn": "Норвегия",
    "tr": "Турция",
    "pl": "Польша",
    "uk": "Украина",
    "pt": "Бразилия",
    "nl": "Нидерланды",
    "fi": "Финляндия",
    "cs": "Чехия",
    "th": "Таиланд",
    "id": "Индонезия",
    "hu": "Венгрия",
    "ro": "Румыния",
    "el": "Греция",
    "he": "Израиль",
    "is": "Исландия",
    "fa": "Иран",
    "ar": "Египет",
    "vi": "Вьетнам",
    "tl": "Филиппины",
    "ms": "Малайзия",
    "sr": "Сербия",
    "hr": "Хорватия",
    "bg": "Болгария",
    "sk": "Словакия",
    "be": "Беларусь",
    "kk": "Казахстан",
    "ka": "Грузия",
    "et": "Эстония",
    "lv": "Латвия",
    "lt": "Литва",
}


def _norm_key(value: str) -> str:
    return value.strip().casefold().replace("ё", "е")


def _norm_title(value: str) -> str:
    cleaned = re.sub(r"[^\w\s]+", " ", value.casefold().replace("ё", "е"), flags=re.UNICODE)
    return " ".join(cleaned.split())


def encode_id(media_kind: str, tmdb_id: int) -> int:
    """Encode a TMDB `(media_kind, tmdb_id)` pair into a non-overlapping catalog ID."""
    if isinstance(tmdb_id, bool) or not isinstance(tmdb_id, int) or tmdb_id <= 0 or tmdb_id >= TMDB_MOVIE_OFFSET:
        raise ValueError(f"Некорректный tmdb_id: {tmdb_id!r}")
    kind = media_kind.strip().lower()
    if kind == "movie":
        return TMDB_MOVIE_OFFSET + tmdb_id
    if kind == "tv":
        return TMDB_TV_OFFSET + tmdb_id
    raise ValueError(f"Неизвестный media_kind: {media_kind!r}")


def decode_id(catalog_id: int) -> tuple[str, int] | None:
    """Decode a catalog ID to `("movie"|"tv", tmdb_id)`, or `None` for legacy KP / invalid IDs."""
    if isinstance(catalog_id, bool) or not isinstance(catalog_id, int):
        return None
    if TMDB_MOVIE_OFFSET < catalog_id < TMDB_TV_OFFSET:
        return ("movie", catalog_id - TMDB_MOVIE_OFFSET)
    if TMDB_TV_OFFSET < catalog_id < _MAX_CATALOG_ID:
        return ("tv", catalog_id - TMDB_TV_OFFSET)
    return None


def img_path_url(tmdb_path: str | None, size: str, base_url: str = "") -> str:
    """Validate a TMDB image path and return `/kino/img/{size}/{filename}` (with optional base URL)."""
    if not tmdb_path or not isinstance(tmdb_path, str):
        return ""
    clean_size = size.strip() if isinstance(size, str) else ""
    if not IMG_SIZE_RE.fullmatch(clean_size):
        return ""
    raw = tmdb_path.strip()
    if raw.startswith("/"):
        raw = raw[1:]
    if "/" in raw or not IMG_FILENAME_RE.fullmatch(raw):
        return ""
    prefix = base_url.strip().rstrip("/") if isinstance(base_url, str) else ""
    return f"{prefix}/kino/img/{clean_size}/{raw}"


def map_genres(
    raw_genres: Any,
    *,
    original_language: str = "",
    origin_countries: Iterable[str] = (),
) -> list[str]:
    """Map TMDB genre IDs or genre dicts to lowercase Russian genre names (+ `аниме` tagging)."""
    ids: list[int] = []
    if isinstance(raw_genres, list):
        for entry in raw_genres:
            if isinstance(entry, int) and not isinstance(entry, bool):
                ids.append(entry)
            elif isinstance(entry, dict) and isinstance(entry.get("id"), int):
                ids.append(entry["id"])

    result: list[str] = []
    seen: set[str] = set()
    for gid in ids:
        for name in GENRE_MAP.get(gid, ()):
            if name not in seen:
                seen.add(name)
                result.append(name)

    countries_upper = {str(code).strip().upper() for code in origin_countries if code}
    if 16 in ids and (original_language.strip().lower() == "ja" or "JP" in countries_upper):
        if "аниме" not in seen:
            result.append("аниме")
    return result


def genre_id_for(name: str, media_kind: str = "movie") -> int | None:
    """Resolve a Russian genre name to its TMDB numeric genre ID for `"movie"` or `"tv"`."""
    key = _norm_key(name)
    if not key:
        return None
    kind = media_kind.strip().lower()
    if kind == "movie":
        return MOVIE_GENRE_IDS.get(key)
    if kind == "tv":
        return TV_GENRE_IDS.get(key)
    raise ValueError(f"Неизвестный media_kind: {media_kind!r}")


def country_iso_for(name: str) -> str | None:
    """Resolve a Russian country name, alias, or ISO-3166-1 alpha-2 code to an uppercase ISO code."""
    clean = name.strip()
    if not clean:
        return None
    upper = clean.upper()
    if len(upper) == 2 and upper in ISO_TO_COUNTRY:
        return upper
    return _COUNTRY_NAME_TO_ISO.get(_norm_key(clean))


def canonical_country_name(name: str) -> str | None:
    """Return the canonical Russian country name for any supported country string or ISO code."""
    iso = country_iso_for(name)
    return ISO_TO_COUNTRY.get(iso) if iso else None


def _extract_origin_codes(item: dict[str, Any]) -> list[str]:
    codes: list[str] = []
    seen: set[str] = set()
    raw_origin = item.get("origin_country")
    if isinstance(raw_origin, list):
        for code in raw_origin:
            if isinstance(code, str) and code.strip():
                upper = code.strip().upper()
                if upper not in seen:
                    seen.add(upper)
                    codes.append(upper)
    raw_prod = item.get("production_countries")
    if isinstance(raw_prod, list):
        for entry in raw_prod:
            if isinstance(entry, dict):
                code = entry.get("iso_3166_1")
                if isinstance(code, str) and code.strip():
                    upper = code.strip().upper()
                    if upper not in seen:
                        seen.add(upper)
                        codes.append(upper)
    return codes


def map_countries(item: dict[str, Any], *, fallback_country: str = "") -> list[str]:
    """Resolve Russian country names for a TMDB movie/TV object, never returning an empty list."""
    codes = _extract_origin_codes(item)
    mapped: list[str] = []
    seen: set[str] = set()
    for code in codes:
        name = ISO_TO_COUNTRY.get(code)
        if name and name not in seen:
            seen.add(name)
            mapped.append(name)
    if mapped:
        return mapped

    lang = str(item.get("original_language") or "").strip().lower()
    if lang == "ru":
        return ["Россия"]

    if fallback_country.strip():
        canonical = canonical_country_name(fallback_country) or fallback_country.strip()
        return [canonical]

    lang_country = LANG_TO_COUNTRY.get(lang)
    if lang_country:
        return [lang_country]

    return ["Другие"]


def _fmt_rating(value: Any) -> str:
    if value in (None, "", 0):
        return ""
    try:
        num = float(value)
    except (TypeError, ValueError):
        return ""
    if not math.isfinite(num) or num <= 0:
        return ""
    return f"{num:.1f}"


def resolve_ratings(
    vote_average: Any,
    vote_count: Any,
    *,
    catalog_id: int = 0,
    imdb_id: str = "",
    imdb_lookup: Callable[[str], tuple[float, int] | None] | None = None,
    imdb_id_resolver: Callable[[int], str] | None = None,
) -> tuple[str, str, int]:
    """Compute `(rating, rating_imdb, votes)` avoiding false Kinopoisk-only `-0.4` penalties."""
    tmdb_rating = _fmt_rating(vote_average)
    try:
        tmdb_votes = max(0, int(vote_count or 0))
    except (TypeError, ValueError):
        tmdb_votes = 0

    resolved_imdb_id = imdb_id.strip() if isinstance(imdb_id, str) else ""
    if not resolved_imdb_id and imdb_id_resolver is not None and catalog_id > 0:
        try:
            resolved_imdb_id = str(imdb_id_resolver(catalog_id) or "").strip()
        except Exception:
            LOG.warning("imdb_id_resolver failed for %s", catalog_id, exc_info=True)
            resolved_imdb_id = ""

    if resolved_imdb_id and imdb_lookup is not None:
        try:
            found = imdb_lookup(resolved_imdb_id)
        except Exception:
            LOG.warning("imdb_lookup failed for %s", resolved_imdb_id, exc_info=True)
            found = None
        if found is not None:
            imdb_score, imdb_votes = found
            imdb_text = _fmt_rating(imdb_score)
            if imdb_text:
                try:
                    imdb_votes_int = max(0, int(imdb_votes or 0))
                except (TypeError, ValueError):
                    imdb_votes_int = 0
                return (tmdb_rating or imdb_text, imdb_text, max(tmdb_votes, imdb_votes_int))

    if tmdb_votes >= 50 and tmdb_rating:
        return (tmdb_rating, tmdb_rating, tmdb_votes)
    return (tmdb_rating, "", tmdb_votes)


def _best_image(
    images: list[Any],
    languages: tuple[str | None, ...],
    *,
    skip_svg: bool = True,
) -> str:
    """Pick highest-rated TMDB image by language preference, skipping SVGs & invalid paths."""
    usable = []
    for image in images:
        if not isinstance(image, dict):
            continue
        file_path = str(image.get("file_path") or "").strip()
        if not file_path.startswith("/"):
            continue
        if skip_svg and file_path.lower().endswith(".svg"):
            continue
        if not IMG_FILENAME_RE.fullmatch(file_path[1:]):
            continue
        usable.append(image)

    for language in languages:
        same = [image for image in usable if image.get("iso_639_1") == language]
        if same:
            best = max(
                same,
                key=lambda image: (float(image.get("vote_average") or 0), int(image.get("vote_count") or 0)),
            )
            return str(best["file_path"])
    return ""


def _parse_iso_date(value: Any) -> datetime.date | None:
    text = str(value or "").strip()[:10]
    if len(text) != 10:
        return None
    try:
        return datetime.date.fromisoformat(text)
    except ValueError:
        return None


def _is_russian_item(item: dict[str, Any]) -> bool:
    if str(item.get("original_language") or "").strip().lower() == "ru":
        return True
    codes = set(_extract_origin_codes(item))
    return bool(codes & EXCLUDED_ORIGIN_CODES)


def _has_excluded_tv_genre(item: dict[str, Any]) -> bool:
    raw = item.get("genre_ids")
    if isinstance(raw, list):
        return any(gid in TV_NON_FICTION_GENRE_IDS for gid in raw if isinstance(gid, int))
    raw_objs = item.get("genres")
    if isinstance(raw_objs, list):
        return any(
            isinstance(g, dict) and g.get("id") in TV_NON_FICTION_GENRE_IDS
            for g in raw_objs
        )
    return False


def to_catalog_title(card: dict[str, Any]) -> bot.CatalogTitle:
    """Convert a `catalog_card` dict into a `torbox_tg_bot.CatalogTitle` dataclass."""
    return bot.CatalogTitle(
        catalog_id=int(card.get("catalog_id") or 0),
        name=str(card.get("name") or "Без названия"),
        original_name=str(card.get("original_name") or ""),
        media_type=str(card.get("media_type") or "UNKNOWN"),
        year=str(card.get("year") or ""),
        rating=str(card.get("rating") or ""),
        countries=tuple(str(c) for c in (card.get("countries") or ()) if c),
        genres=tuple(str(g) for g in (card.get("genres") or ()) if g),
        poster_url=str(card.get("poster_url") or ""),
    )


def _build_session() -> requests.Session:
    session = requests.Session()
    retry = Retry(
        total=2,
        connect=2,
        read=2,
        backoff_factor=0.3,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset({"GET"}),
        raise_on_status=False,
    )
    adapter = HTTPAdapter(pool_connections=32, pool_maxsize=32, max_retries=retry)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    user_agent = getattr(bot, "USER_AGENT", "torbox-one/tmdb-catalog")
    session.headers.update({"Accept": "application/json", "User-Agent": user_agent})
    return session


class TmdbCatalog:
    """Production TMDB API v3 catalog engine matching the Kinopoisk contract."""

    def __init__(
        self,
        api_key: str,
        *,
        session: requests.Session | None = None,
        img_base_url: str = "",
        imdb_lookup: Callable[[str], tuple[float, int] | None] | None = None,
        imdb_id_resolver: Callable[[int], str] | None = None,
        cache: Any | None = None,
    ) -> None:
        self._api_key = api_key.strip()
        self._is_token = self._api_key.startswith("ey") and self._api_key.count(".") == 2
        self.http = session if session is not None else _build_session()
        if self._is_token and hasattr(self.http, "headers"):
            try:
                self.http.headers["Authorization"] = f"Bearer {self._api_key}"
            except TypeError:
                pass
        self.img_base_url = img_base_url.strip().rstrip("/")
        self.imdb_lookup = imdb_lookup
        self.imdb_id_resolver = imdb_id_resolver
        self.cache = cache
        self._imdb_lock = threading.Lock()
        self._known_imdb_ids: dict[int, str] = {}

    to_catalog_title = staticmethod(to_catalog_title)

    # ------------------------------------------------------------------
    # Low-level HTTP & helpers
    # ------------------------------------------------------------------

    def _params(self, extra: dict[str, Any] | None = None) -> dict[str, Any]:
        params: dict[str, Any] = dict(extra or {})
        if not self._is_token and self._api_key:
            params["api_key"] = self._api_key
        return params

    def _get(
        self,
        path: str,
        params: dict[str, Any] | None = None,
        *,
        timeout: int = DEFAULT_TIMEOUT,
        allow_404: bool = False,
    ) -> dict[str, Any]:
        url = f"{TMDB_API_BASE}/{path.lstrip('/')}"
        response = self.http.get(url, params=self._params(params), timeout=timeout)
        if allow_404 and response.status_code == 404:
            return {}
        response.raise_for_status()
        payload = response.json()
        return payload if isinstance(payload, dict) else {}

    def _img(self, tmdb_path: str | None, size: str) -> str:
        return img_path_url(tmdb_path, size, self.img_base_url)

    def _remember_imdb(self, catalog_id: int, imdb_id: str) -> None:
        if catalog_id > 0 and imdb_id:
            with self._imdb_lock:
                self._known_imdb_ids[catalog_id] = imdb_id

    def _resolve_known_imdb_id(self, catalog_id: int) -> str:
        if self.imdb_id_resolver is not None:
            resolved = str(self.imdb_id_resolver(catalog_id) or "").strip()
            if resolved:
                return resolved
        with self._imdb_lock:
            return self._known_imdb_ids.get(catalog_id, "")

    def _extract_imdb_id(self, detail: dict[str, Any]) -> str:
        ext = detail.get("external_ids")
        ext_imdb = str(ext.get("imdb_id") or "").strip() if isinstance(ext, dict) else ""
        top_imdb = str(detail.get("imdb_id") or "").strip()
        candidate = ext_imdb or top_imdb
        return candidate if IMDB_ID_RE.fullmatch(candidate) else ""

    def _duration_minutes(self, media_kind: str, detail: dict[str, Any]) -> int:
        if media_kind == "movie":
            runtime = detail.get("runtime")
            return int(runtime) if isinstance(runtime, int) and runtime > 0 else 0
        run_times = detail.get("episode_run_time")
        if isinstance(run_times, list):
            for val in run_times:
                if isinstance(val, int) and val > 0:
                    return val
        for ep_key in ("last_episode_to_air", "next_episode_to_air"):
            ep = detail.get(ep_key)
            if isinstance(ep, dict):
                rt = ep.get("runtime")
                if isinstance(rt, int) and rt > 0:
                    return rt
        return 0

    def _media_type_for(
        self,
        media_kind: str,
        item: dict[str, Any],
        *,
        override: str = "",
    ) -> str:
        if override:
            return override
        if media_kind == "movie":
            return "FILM"
        tv_type = str(item.get("type") or "").strip().casefold()
        if tv_type == "miniseries":
            return "MINI_SERIES"
        return "TV_SERIES"

    def _build_card(
        self,
        item: dict[str, Any],
        media_kind: str,
        *,
        detailed: bool = False,
        fallback_country: str = "",
        media_type_override: str = "",
    ) -> dict[str, Any]:
        tmdb_id = int(item.get("id") or 0)
        catalog_id = encode_id(media_kind, tmdb_id)

        if media_kind == "movie":
            raw_title = str(item.get("title") or "").strip()
            raw_original = str(item.get("original_title") or "").strip()
            date_str = str(item.get("release_date") or "").strip()
        else:
            raw_title = str(item.get("name") or "").strip()
            raw_original = str(item.get("original_name") or "").strip()
            date_str = str(item.get("first_air_date") or "").strip()

        name = raw_title or raw_original or "Без названия"
        original_name = raw_original if raw_original and raw_original.casefold() != name.casefold() else ""
        year = date_str[:4] if len(date_str) >= 4 and date_str[:4].isdigit() else ""

        origin_codes = _extract_origin_codes(item)
        orig_lang = str(item.get("original_language") or "").strip().lower()
        genres_src = item.get("genres") if item.get("genres") is not None else item.get("genre_ids")
        genres = map_genres(genres_src, original_language=orig_lang, origin_countries=origin_codes)
        countries = map_countries(item, fallback_country=fallback_country)

        imdb_id = self._extract_imdb_id(item) if detailed else ""
        if imdb_id:
            self._remember_imdb(catalog_id, imdb_id)

        rating, rating_imdb, votes = resolve_ratings(
            item.get("vote_average"),
            item.get("vote_count"),
            catalog_id=catalog_id,
            imdb_id=imdb_id,
            imdb_lookup=self.imdb_lookup,
            imdb_id_resolver=self._resolve_known_imdb_id,
        )

        logo_url = ""
        art_url = ""
        if detailed:
            images = item.get("images") if isinstance(item.get("images"), dict) else {}
            logo_path = _best_image(images.get("logos") or [], LOGO_LANGUAGES, skip_svg=True)
            art_path = _best_image(images.get("posters") or [], (None,), skip_svg=True)
            logo_url = self._img(logo_path, "w500")
            art_url = self._img(art_path, "w780")

        return {
            "catalog_id": catalog_id,
            "name": name,
            "original_name": original_name,
            "media_type": self._media_type_for(media_kind, item, override=media_type_override),
            "year": year,
            "rating": rating,
            "countries": countries,
            "genres": genres,
            "poster_url": self._img(item.get("poster_path"), "w500"),
            "search_query": " ".join(part for part in (name, year) if part).strip(),
            "rating_imdb": rating_imdb,
            "votes": votes,
            "tagline": str(item.get("tagline") or "").strip() if detailed else "",
            "overview": str(item.get("overview") or "").strip(),
            "backdrop_url": self._img(item.get("backdrop_path"), "w1280"),
            "logo_url": logo_url,
            "art_url": art_url,
            "duration_minutes": self._duration_minutes(media_kind, item) if detailed else 0,
            "detailed": detailed,
        }

    def _build_cast(self, media_kind: str, detail: dict[str, Any]) -> list[dict[str, str]]:
        credits = detail.get("credits") if isinstance(detail.get("credits"), dict) else {}
        directors: list[dict[str, str]] = []
        seen_directors: set[str] = set()

        def add_director(person: Any) -> None:
            if len(directors) >= 2 or not isinstance(person, dict):
                return
            name = str(person.get("name") or "").strip()
            if not name or name.casefold() in seen_directors:
                return
            seen_directors.add(name.casefold())
            directors.append(
                {
                    "name": name,
                    "role": "",
                    "photo_url": self._img(person.get("profile_path"), "w500"),
                    "profession": "director",
                }
            )

        if media_kind == "tv":
            for creator in detail.get("created_by") or []:
                add_director(creator)
        for member in credits.get("crew") or []:
            if isinstance(member, dict) and member.get("job") in ("Director", "Series Director"):
                add_director(member)

        actors: list[dict[str, str]] = []
        for member in (credits.get("cast") or [])[:15]:
            if not isinstance(member, dict):
                continue
            name = str(member.get("name") or "").strip()
            if not name:
                continue
            actors.append(
                {
                    "name": name,
                    "role": str(member.get("character") or "").strip(),
                    "photo_url": self._img(member.get("profile_path"), "w500"),
                    "profession": "actor",
                }
            )
        return directors + actors

    def _build_seasons(
        self,
        detail: dict[str, Any],
        *,
        today: datetime.date | None = None,
    ) -> list[dict[str, Any]]:
        day = today or datetime.date.today()
        seasons: list[dict[str, Any]] = []
        for entry in detail.get("seasons") or []:
            if not isinstance(entry, dict):
                continue
            number = entry.get("season_number")
            if not isinstance(number, int) or number <= 0:
                continue
            episode_count = int(entry.get("episode_count") or 0)
            air_date = _parse_iso_date(entry.get("air_date"))
            is_unreleased = air_date is None or air_date > day or episode_count <= 0
            seasons.append(
                {
                    "season": number,
                    "episodes": episode_count,
                    "released": False if is_unreleased else None,
                }
            )
        seasons.sort(key=lambda item: item["season"])
        return seasons

    # ------------------------------------------------------------------
    # Public catalog methods
    # ------------------------------------------------------------------

    def shelf(self, kind: str, page: int = 1) -> list[dict[str, Any]]:
        """Return `catalog_card`-shaped dicts for a named home shelf, genre, or year."""
        key = kind.strip().strip("/").casefold()
        safe_page = min(max(int(page), 1), MAX_SHELF_PAGE)
        today = datetime.date.today()

        if key == "popular":
            payload = self._get("trending/all/week", {"language": "ru-RU", "page": safe_page})
            cards: list[dict[str, Any]] = []
            for item in payload.get("results") or []:
                if not isinstance(item, dict) or not item.get("id"):
                    continue
                mtype = str(item.get("media_type") or "").lower()
                if mtype not in ("movie", "tv"):
                    continue
                if _is_russian_item(item):
                    continue
                if mtype == "tv" and _has_excluded_tv_genre(item):
                    continue
                cards.append(self._build_card(item, mtype))
            return cards

        if key == "popular_movies":
            payload = self._get("trending/movie/week", {"language": "ru-RU", "page": safe_page})
            return [
                self._build_card(item, "movie")
                for item in payload.get("results") or []
                if isinstance(item, dict) and item.get("id") and not _is_russian_item(item)
            ]

        if key == "popular_series":
            payload = self._get("trending/tv/week", {"language": "ru-RU", "page": safe_page})
            return [
                self._build_card(item, "tv")
                for item in payload.get("results") or []
                if isinstance(item, dict)
                and item.get("id")
                and not _is_russian_item(item)
                and not _has_excluded_tv_genre(item)
            ]

        if key in ("top", "top_movies"):
            return self._discover_raw(
                "movie",
                {
                    "with_origin_country": NON_RUSSIAN_ORIGIN_COUNTRIES,
                    "without_genres": "99,10770",
                    "vote_count.gte": 1500,
                    "sort_by": "vote_average.desc",
                    "page": safe_page,
                },
                exclude_russian=True,
            )

        if key == "top_series":
            return self._discover_raw(
                "tv",
                {
                    "with_origin_country": NON_RUSSIAN_ORIGIN_COUNTRIES,
                    "without_genres": f"{TV_NON_FICTION_GENRES},99",
                    "vote_count.gte": 500,
                    "sort_by": "vote_average.desc",
                    "page": safe_page,
                },
                exclude_russian=True,
            )

        if key == "top_xxi":
            return self._discover_raw(
                "movie",
                {
                    "primary_release_date.gte": "2001-01-01",
                    "with_origin_country": NON_RUSSIAN_ORIGIN_COUNTRIES,
                    "without_genres": "99,10770",
                    "vote_count.gte": 1500,
                    "sort_by": "vote_average.desc",
                    "page": safe_page,
                },
                exclude_russian=True,
            )

        if key in ("premieres", "closest_releases"):
            since = (today - datetime.timedelta(days=90)).isoformat()
            return self._discover_raw(
                "movie",
                {
                    "primary_release_date.gte": since,
                    "primary_release_date.lte": today.isoformat(),
                    "with_origin_country": NON_RUSSIAN_ORIGIN_COUNTRIES,
                    "without_genres": "99,10770",
                    "vote_count.gte": 20,
                    "sort_by": "popularity.desc",
                    "page": safe_page,
                },
                exclude_russian=True,
            )

        if key == "kids_animation":
            return self._discover_raw(
                "movie",
                {
                    "with_genres": "16,10751",
                    "with_origin_country": KIDS_ORIGIN_COUNTRIES,
                    "without_keywords": ANIME_KEYWORD_ID,
                    "vote_count.gte": 150,
                    "vote_average.gte": 6.5,
                    "sort_by": "popularity.desc",
                    "page": safe_page,
                },
                exclude_russian=True,
            )

        if key == "family":
            return self._discover_raw(
                "movie",
                {
                    "with_genres": "10751",
                    "with_origin_country": NON_RUSSIAN_ORIGIN_COUNTRIES,
                    "without_keywords": ANIME_KEYWORD_ID,
                    "vote_count.gte": 150,
                    "vote_average.gte": 6.5,
                    "sort_by": "popularity.desc",
                    "page": safe_page,
                },
                exclude_russian=True,
            )

        themed_shelves: dict[str, dict[str, Any]] = {
            "comics": {
                "with_keywords": "9715",
                "vote_count.gte": 200,
                "sort_by": "popularity.desc",
            },
            "vampires": {
                "with_keywords": "3133",
                "vote_count.gte": 100,
                "sort_by": "popularity.desc",
            },
            "zombies": {
                "with_keywords": "12377|186565",
                "vote_count.gte": 100,
                "sort_by": "popularity.desc",
            },
            "catastrophe": {
                "with_keywords": "10617|5096",
                "vote_count.gte": 100,
                "sort_by": "popularity.desc",
            },
            "love": {
                "with_genres": "10749",
                "vote_count.gte": 200,
                "vote_average.gte": 6.5,
                "sort_by": "popularity.desc",
            },
            "programmers": {
                "with_keywords": "2157|202838|224393",
                "vote_count.gte": 50,
                "sort_by": "popularity.desc",
            },
            "oscar_2021": {
                "primary_release_date.gte": "2020-01-01",
                "primary_release_date.lte": "2021-12-31",
                "without_genres": "99,10770",
                "vote_count.gte": 500,
                "vote_average.gte": 7.2,
                "sort_by": "vote_average.desc",
            },
        }
        if key in themed_shelves:
            params = {
                "with_origin_country": NON_RUSSIAN_ORIGIN_COUNTRIES,
                **themed_shelves[key],
                "page": safe_page,
            }
            return self._discover_raw("movie", params, exclude_russian=True)

        prefix, _, value = key.partition("/")
        if prefix == "genre" and value:
            if genre_id_for(value, "movie") is None:
                raise ValueError(f"Каталог не знает жанра «{value}»")
            return self.discover(media_type="FILM", genre=value, order="RATING", page=safe_page)
        if prefix == "year" and value.isdigit():
            year = int(value)
            return self.discover(
                media_type="FILM",
                year_from=year,
                year_to=year,
                order="RATING",
                page=safe_page,
            )

        raise ValueError(f"Неизвестная полка «{kind}»")

    def _discover_raw(
        self,
        media_kind: str,
        params: dict[str, Any],
        *,
        exclude_russian: bool = False,
        fallback_country: str = "",
        media_type_override: str = "",
    ) -> list[dict[str, Any]]:
        req_params: dict[str, Any] = {"language": "ru-RU", **params}
        if media_kind == "movie":
            req_params.setdefault("include_adult", "false")
        payload = self._get(f"discover/{media_kind}", req_params)
        cards: list[dict[str, Any]] = []
        for item in payload.get("results") or []:
            if not isinstance(item, dict) or not item.get("id"):
                continue
            if exclude_russian and _is_russian_item(item):
                continue
            cards.append(
                self._build_card(
                    item,
                    media_kind,
                    fallback_country=fallback_country,
                    media_type_override=media_type_override,
                )
            )
        return cards

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
        """Filter movies or series via TMDB `/discover/{movie|tv}` matching Kinopoisk contract."""
        kind = media_type.strip().upper() or "ALL"
        if kind not in DISCOVER_TYPES:
            raise ValueError(f"Неизвестный тип «{media_type}»")
        sort = order.strip().upper() or "NUM_VOTE"
        if sort not in DISCOVER_ORDERS:
            raise ValueError(f"Неизвестный порядок «{order}»")

        safe_page = min(max(int(page), 1), MAX_DISCOVER_PAGE)
        media_kind = "tv" if kind in ("TV_SERIES", "MINI_SERIES", "TV_SHOW") else "movie"
        media_type_override = "MINI_SERIES" if kind == "MINI_SERIES" else ""

        params: dict[str, Any] = {"page": safe_page}
        if kind == "MINI_SERIES":
            params["with_type"] = 2

        genre_key = _norm_key(genre)
        if media_kind == "tv" and genre_key not in ("новости", "реальное тв", "ток-шоу"):
            params["without_genres"] = TV_NON_FICTION_GENRES

        if genre_key:
            if genre_key == "аниме":
                params["with_genres"] = "16"
                params["with_original_language"] = "ja"
            elif media_kind == "tv" and genre_key == "триллер":
                params["with_genres"] = "9648|80"
            else:
                gid = genre_id_for(genre_key, media_kind)
                if gid is None:
                    other_kind = "movie" if media_kind == "tv" else "tv"
                    if genre_id_for(genre_key, other_kind) is None:
                        raise ValueError(f"Каталог не знает жанра «{genre.strip()}»")
                    return []
                params["with_genres"] = str(gid)

            if genre_key == "мультфильм" and _norm_key(country) != "япония":
                params["without_keywords"] = ANIME_KEYWORD_ID

        explicit_country = ""
        exclude_russian = False
        iso: str | None = None
        if country.strip():
            iso = country_iso_for(country)
            if iso is None:
                raise ValueError(f"Каталог не знает страны «{country.strip()}»")
            params["with_origin_country"] = iso
            explicit_country = ISO_TO_COUNTRY.get(iso, country.strip())
        else:
            params["with_origin_country"] = NON_RUSSIAN_ORIGIN_COUNTRIES
            exclude_russian = True

        date_field = "primary_release_date" if media_kind == "movie" else "first_air_date"
        if year_from:
            params[f"{date_field}.gte"] = f"{int(year_from):04d}-01-01"
        if year_to:
            params[f"{date_field}.lte"] = f"{int(year_to):04d}-12-31"
        if rating_from is not None:
            params["vote_average.gte"] = float(rating_from)

        current_year = datetime.date.today().year
        is_recent = year_from is not None and int(year_from) >= current_year - 2
        is_non_us = iso is not None and iso != "US"
        if media_kind == "movie":
            vote_min = 40 if (is_recent or is_non_us) else 100
        else:
            vote_min = 25 if (is_recent or is_non_us or kind == "MINI_SERIES") else 50
        params["vote_count.gte"] = vote_min

        if sort == "NUM_VOTE":
            params["sort_by"] = "popularity.desc" if is_recent else "vote_count.desc"
        elif sort == "RATING":
            params["sort_by"] = "vote_average.desc"
        else:
            params["sort_by"] = f"{date_field}.desc"

        return self._discover_raw(
            media_kind,
            params,
            exclude_russian=exclude_russian,
            fallback_country=explicit_country,
            media_type_override=media_type_override,
        )

    def search(self, query: str) -> list[dict[str, Any]]:
        """Search movies & TV via TMDB `search/multi` with relevance + popularity re-ranking."""
        clean = " ".join(query.strip().split())
        if not clean:
            return []
        payload = self._get(
            "search/multi",
            {"language": "ru-RU", "include_adult": "false", "query": clean, "page": 1},
        )
        raw_items = [
            item
            for item in payload.get("results") or []
            if isinstance(item, dict)
            and item.get("id")
            and str(item.get("media_type") or "").lower() in ("movie", "tv")
        ]

        norm_q = _norm_title(clean)
        q_words = set(norm_q.split())

        def rank_key(entry: tuple[int, dict[str, Any]]) -> tuple[float, int]:
            idx, item = entry
            mtype = str(item.get("media_type") or "").lower()
            titles = (
                [str(item.get("title") or ""), str(item.get("original_title") or "")]
                if mtype == "movie"
                else [str(item.get("name") or ""), str(item.get("original_name") or "")]
            )
            match_bonus = 0.0
            for raw_t in titles:
                nt = _norm_title(raw_t)
                if not nt:
                    continue
                if nt == norm_q:
                    match_bonus = max(match_bonus, 2.2)
                elif nt.startswith(norm_q + " "):
                    match_bonus = max(match_bonus, 1.9)
                elif f" {norm_q} " in f" {nt} ":
                    match_bonus = max(match_bonus, 1.0)
                elif q_words and q_words.issubset(set(nt.split())):
                    match_bonus = max(match_bonus, 0.9)

            votes = max(0, int(item.get("vote_count") or 0))
            pop = max(0.0, float(item.get("popularity") or 0.0))
            poster_bonus = 0.2 if item.get("poster_path") else 0.0
            score = (
                match_bonus
                + 1.6 * math.log10(votes + 1)
                + 0.35 * math.log10(pop + 1)
                + poster_bonus
            )
            return (-score, idx)

        ranked = [item for _, item in sorted(enumerate(raw_items), key=rank_key)]
        return [
            self._build_card(item, str(item["media_type"]).lower())
            for item in ranked[:20]
        ]

    def title_detail(self, catalog_id: int) -> dict[str, Any]:
        """Fetch full TMDB movie/TV detail with credits, external_ids, and images."""
        decoded = decode_id(catalog_id)
        if decoded is None:
            raise ValueError(f"Некорректный идентификатор TMDB: {catalog_id}")
        media_kind, tmdb_id = decoded
        cache_key = f"tmdb:detail:{catalog_id}"
        if self.cache is not None:
            cached = self.cache.get(cache_key)
            if isinstance(cached, dict) and cached.get("id"):
                imdb_id = self._extract_imdb_id(cached)
                if imdb_id:
                    self._remember_imdb(catalog_id, imdb_id)
                return cached

        detail = self._get(
            f"{media_kind}/{tmdb_id}",
            {
                "language": "ru-RU",
                "append_to_response": "credits,external_ids,images",
                "include_image_language": "ru,en,null",
            },
            allow_404=True,
        )
        if detail and detail.get("id"):
            imdb_id = self._extract_imdb_id(detail)
            if imdb_id:
                self._remember_imdb(catalog_id, imdb_id)
            if self.cache is not None:
                self.cache.put(cache_key, detail)
        return detail

    def title_card(self, catalog_id: int) -> dict[str, Any]:
        """Return `{meta, imdb_id, seasons, raw}` matching `meta_service.TitleCard`."""
        decoded = decode_id(catalog_id)
        if decoded is None:
            raise ValueError(f"Некорректный идентификатор TMDB: {catalog_id}")
        media_kind, tmdb_id = decoded
        detail = self.title_detail(catalog_id)
        if not detail or not detail.get("id"):
            return {
                "meta": {
                    "matched": False,
                    "source": "",
                    "source_id": 0,
                    "tmdb_id": None,
                    "tmdb_media_type": None,
                    "tmdb_lookup_version": 1,
                    "title": "",
                    "original_title": "",
                    "year": "",
                    "media_type": "",
                    "rating": "",
                    "overview": "",
                    "tagline": "",
                    "poster_url": "",
                    "backdrop_url": "",
                    "logo_url": "",
                    "genres": [],
                    "countries": [],
                    "seasons_count": 0,
                    "season_hint": None,
                    "duration_minutes": 0,
                    "cast": [],
                },
                "imdb_id": "",
                "seasons": [],
                "raw": {},
            }

        card = self._build_card(detail, media_kind, detailed=True)
        imdb_id = self._extract_imdb_id(detail)
        seasons = self._build_seasons(detail) if media_kind == "tv" else []
        raw_original = (
            str(detail.get("original_title") or "").strip()
            if media_kind == "movie"
            else str(detail.get("original_name") or "").strip()
        )
        meta = {
            "matched": True,
            "source": "tmdb",
            "source_id": catalog_id,
            "tmdb_id": tmdb_id,
            "tmdb_media_type": media_kind,
            "tmdb_lookup_version": 1,
            "title": card["name"],
            "original_title": raw_original,
            "year": card["year"],
            "media_type": card["media_type"],
            "rating": card["rating"],
            "overview": card["overview"],
            "tagline": card["tagline"],
            "poster_url": card["poster_url"],
            "backdrop_url": card["backdrop_url"],
            "logo_url": card["logo_url"],
            "genres": card["genres"],
            "countries": card["countries"],
            "seasons_count": len(seasons),
            "season_hint": None,
            "duration_minutes": card["duration_minutes"],
            "cast": self._build_cast(media_kind, detail),
        }
        return {
            "meta": meta,
            "imdb_id": imdb_id,
            "seasons": seasons,
            "raw": detail,
        }

    def briefs(self, catalog_ids: list[int], max_workers: int = 4) -> list[dict[str, Any]]:
        """Fetch full detailed `catalog_card` dicts for up to `MAX_BRIEF_IDS` IDs in parallel."""
        wanted: list[tuple[int, str]] = []
        seen: set[int] = set()
        for cid in catalog_ids:
            decoded = decode_id(cid)
            if decoded is None or cid in seen:
                continue
            seen.add(cid)
            wanted.append((cid, decoded[0]))
            if len(wanted) >= MAX_BRIEF_IDS:
                break
        if not wanted:
            return []

        details: dict[int, dict[str, Any]] = {}
        missing: list[int] = []
        for cid, _ in wanted:
            cached = self.cache.get(f"tmdb:detail:{cid}") if self.cache is not None else None
            if isinstance(cached, dict) and cached.get("id"):
                details[cid] = cached
            else:
                missing.append(cid)

        if missing:
            workers = max(1, min(int(max_workers), len(missing)))
            if workers == 1:
                for cid in missing:
                    try:
                        detail = self.title_detail(cid)
                    except Exception:
                        LOG.warning("TMDB detail failed for %s", cid, exc_info=True)
                        continue
                    if detail and detail.get("id"):
                        details[cid] = detail
            else:
                with ThreadPoolExecutor(max_workers=workers) as pool:
                    futures = {pool.submit(self.title_detail, cid): cid for cid in missing}
                    for future in as_completed(futures):
                        cid = futures[future]
                        try:
                            detail = future.result()
                        except Exception:
                            LOG.warning("TMDB detail failed for %s", cid, exc_info=True)
                            continue
                        if detail and detail.get("id"):
                            details[cid] = detail

        return [
            self._build_card(details[cid], media_kind, detailed=True)
            for cid, media_kind in wanted
            if cid in details
        ]

    def similar(self, catalog_id: int, limit: int = DEFAULT_SIMILAR_LIMIT) -> list[dict[str, Any]]:
        """Fetch similar/recommended titles for an encoded TMDB `catalog_id` in 1 call."""
        decoded = decode_id(catalog_id)
        if decoded is None:
            raise ValueError(f"Некорректный идентификатор TMDB: {catalog_id}")
        media_kind, tmdb_id = decoded
        return self.similar_by_tmdb(media_kind, tmdb_id, limit=limit)

    def similar_by_tmdb(
        self,
        media_kind: str,
        tmdb_id: int,
        limit: int = DEFAULT_SIMILAR_LIMIT,
    ) -> list[dict[str, Any]]:
        """Fetch TMDB `/recommendations` (falling back to `/similar` if empty) as `catalog_card` dicts."""
        kind = media_kind.strip().lower()
        if kind not in ("movie", "tv") or isinstance(tmdb_id, bool) or not isinstance(tmdb_id, int) or tmdb_id <= 0:
            raise ValueError(f"Некорректный TMDB объект: ({media_kind!r}, {tmdb_id!r})")
        safe_limit = min(max(int(limit), 1), MAX_SIMILAR_LIMIT)

        def parse_results(payload: dict[str, Any]) -> list[dict[str, Any]]:
            cards: list[dict[str, Any]] = []
            for item in payload.get("results") or []:
                if not isinstance(item, dict) or not item.get("id"):
                    continue
                if _is_russian_item(item):
                    continue
                item_kind = str(item.get("media_type") or kind).lower()
                if item_kind not in ("movie", "tv"):
                    item_kind = kind
                if item_kind == "tv" and _has_excluded_tv_genre(item):
                    continue
                cards.append(self._build_card(item, item_kind))
                if len(cards) >= safe_limit:
                    break
            return cards

        rec_payload = self._get(
            f"{kind}/{tmdb_id}/recommendations",
            {"language": "ru-RU", "page": 1},
            allow_404=True,
        )
        cards = parse_results(rec_payload)
        if cards:
            return cards

        sim_payload = self._get(
            f"{kind}/{tmdb_id}/similar",
            {"language": "ru-RU", "page": 1},
            allow_404=True,
        )
        return parse_results(sim_payload)

    def episodes(self, catalog_id: int, season: int | None = None) -> list[dict[str, Any]]:
        """Fetch Russian episode metadata and stills matching `meta_service.EpisodeMeta`."""
        decoded = decode_id(catalog_id)
        if decoded is None:
            raise ValueError(f"Некорректный идентификатор TMDB: {catalog_id}")
        media_kind, tv_id = decoded
        if media_kind != "tv":
            return []

        today = datetime.date.today()
        if season is not None:
            season_numbers = [int(season)] if int(season) > 0 else []
        else:
            detail = self.title_detail(catalog_id)
            season_numbers = [
                int(entry["season_number"])
                for entry in detail.get("seasons") or []
                if isinstance(entry, dict)
                and isinstance(entry.get("season_number"), int)
                and entry["season_number"] > 0
            ]
            season_numbers.sort()

        if not season_numbers:
            return []

        def fetch_season(num: int) -> list[dict[str, Any]]:
            payload = self._get(
                f"tv/{tv_id}/season/{num}",
                {"language": "ru-RU"},
                timeout=20,
                allow_404=True,
            )
            season_air = _parse_iso_date(payload.get("air_date"))
            season_future = season_air is not None and season_air > today
            items: list[dict[str, Any]] = []
            for ep in payload.get("episodes") or []:
                if not isinstance(ep, dict):
                    continue
                ep_num = int(ep.get("episode_number") or 0)
                if ep_num <= 0:
                    continue
                air_str = str(ep.get("air_date") or "").strip()
                air_date = _parse_iso_date(air_str)
                unreleased = season_future or air_date is None or air_date > today
                items.append(
                    {
                        "season": int(ep.get("season_number") or num),
                        "episode": ep_num,
                        "title": str(ep.get("name") or "").strip(),
                        "overview": str(ep.get("overview") or "").strip(),
                        "air_date": air_str,
                        "still_url": self._img(ep.get("still_path"), "w780"),
                        "released": False if unreleased else None,
                    }
                )
            return items

        if len(season_numbers) == 1:
            return fetch_season(season_numbers[0])

        by_season: dict[int, list[dict[str, Any]]] = {}
        with ThreadPoolExecutor(max_workers=min(4, len(season_numbers))) as pool:
            futures = {pool.submit(fetch_season, num): num for num in season_numbers}
            for future in as_completed(futures):
                num = futures[future]
                try:
                    by_season[num] = future.result()
                except Exception:
                    LOG.warning("TMDB season %s/%s failed", tv_id, num, exc_info=True)
                    by_season[num] = []

        result: list[dict[str, Any]] = []
        for num in season_numbers:
            result.extend(by_season.get(num, []))
        return result

    def find_by_imdb(
        self,
        imdb_id: str,
        *,
        prefer_kind: str | None = None,
    ) -> tuple[str, int] | None:
        """Resolve an IMDb ID (`tt...`) via TMDB `/find/{imdb_id}` to `("movie"|"tv", tmdb_id)`."""
        clean = imdb_id.strip() if isinstance(imdb_id, str) else ""
        if not IMDB_ID_RE.fullmatch(clean):
            return None
        payload = self._get(
            f"find/{clean}",
            {"external_source": "imdb_id", "language": "ru-RU"},
            allow_404=True,
        )
        order = (
            (("tv_results", "tv"), ("movie_results", "movie"))
            if prefer_kind == "tv"
            else (("movie_results", "movie"), ("tv_results", "tv"))
        )
        for field_name, kind in order:
            results = payload.get(field_name)
            if isinstance(results, list) and results and isinstance(results[0], dict):
                tmdb_id = results[0].get("id")
                if isinstance(tmdb_id, int) and tmdb_id > 0:
                    return (kind, tmdb_id)
        return None
