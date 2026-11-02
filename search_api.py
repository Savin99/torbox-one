"""HTTP-мост между iOS-приложениями и поисковой логикой бота.

Отдаёт каталог Кинопоиска, поиск по трекерам с рейтингом и `.torrent`-файлы.
Управление TorBox сюда не входит: приложение ходит в TorBox API напрямую,
и ключ TorBox этому серверу не передаётся.

Контракт всех эндпоинтов — `docs/search-api.md`. Прежние эндпоинты и поля не
меняются: этим же сервером пользуются Telegram-бот и выпущенный TorBox One.
"""

from __future__ import annotations

import base64
import binascii
import contextlib
import dataclasses
import hashlib
import html
import logging
import os
import re
import threading
import time
from collections.abc import Callable, Iterable, Iterator
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
# До Python 3.11 concurrent.futures.TimeoutError — отдельный класс, а не
# встроенный TimeoutError. На сервере 3.10, и `except TimeoutError` там
# дедлайн не ловил вовсе: вместо partial клиент получал 500.
from concurrent.futures import TimeoutError as FuturesTimeout
from typing import Any
from urllib.parse import quote, urljoin

LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"

# TMDB принимает ключ параметром адреса, а не заголовком. Поэтому его адрес
# с ключом попадает и в текст ошибки requests, и в предупреждения urllib3 о
# повторах — то есть в журнал сервиса открытым текстом. Вырезаем в
# форматтере, а не фильтре: фильтр не видит трассировку исключения.
SECRET_IN_TEXT_RE = re.compile(r"(api_key|apikey|token|passkey|key)=[^&\s\"'<>]+", re.IGNORECASE)


class RedactingFormatter(logging.Formatter):
    """Форматтер, убирающий ключи из всего, что уходит в журнал."""

    def format(self, record: logging.LogRecord) -> str:
        return SECRET_IN_TEXT_RE.sub(r"\1=<вырезано>", super().format(record))


def configure_logging() -> None:
    """Настраивает журнал до импорта бота.

    torbox_tg_bot зовёт logging.basicConfig на импорте и иначе перехватил бы
    конфигурацию uvicorn, поэтому порядок здесь важен.
    """
    handler = logging.StreamHandler()
    handler.setFormatter(RedactingFormatter(LOG_FORMAT))
    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"), handlers=[handler])


configure_logging()

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request, Response
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field, SerializerFunctionWrapHandler, model_serializer

import meta_service
import torbox_tg_bot as bot

LOG = logging.getLogger("search_api")

API_VERSION = "1.4"
API_TOKEN = os.getenv("SEARCH_API_TOKEN", "").strip()
RESULTS_TTL_SECONDS = 3600
MAX_APP_RESULTS = bot.MAX_RESULTS

# Восемь результатов хватает короткому списку в Telegram, но мало клиенту,
# который ранжирует сам: ему нужны кандидаты, а не уже выбранная тройка.
MAX_SEARCH_LIMIT = 60
# Больше трёх проходов по трекерам на одну кнопку «Смотреть» — это уже
# заметная нагрузка на источник ради всё тех же раздач.
MAX_SEARCH_QUERIES = 3

# Хэш считается скачиванием .torrent, то есть одним запросом к трекеру на
# релиз. Верхние кандидаты клиенту нужны, вся выдача — нет.
DEFAULT_HASH_IDS = 12
MAX_HASH_IDS = 24
HASH_WORKERS = 4
HASH_DEADLINE_SECONDS = 25.0
HASH_TTL_SECONDS = 24 * 3600
HASH_CACHE_PATH = meta_service.STATE_DIR / "hash_cache.json"

# Привязка «тайтл → раздача» живёт в телефоне месяцами, поэтому и релиз по
# стабильному id обязан находиться после перезапуска процесса.
RELEASE_STORE_TTL_SECONDS = 30 * 24 * 3600
RELEASE_STORE_PATH = meta_service.STATE_DIR / "release_store.json"

INFO_HASH_RE = re.compile(r"^[0-9a-f]{40}$")

# Фоновый расчёт хэшей медленных трекеров (RuTracker, Kinozal через Byparr).
# Страница — 20 секунд, браузер один, поэтому на запрос `/release/hashes` со
# сроком 8 секунд хэш почти никогда не успевает. Очередь считает их заранее,
# по одному, сразу после поиска.
PREFETCH_PER_SEARCH = 10
PREFETCH_QUEUE_SIZE = 40
PREFETCH_BACKOFF_SECONDS = 60.0
PREFETCH_BACKOFF_MAX_SECONDS = 3600.0
# Трекеры, чей стабильный id — «провайдер:номер темы»
TOPIC_ID_PROVIDERS = (bot.NnmClient.key, bot.RutrackerClient.key, bot.KinozalClient.key)


def stable_release_id(release: bot.Release) -> str:
    """Постоянный идентификатор релиза вида «провайдер:тема».

    uuid4 из прежней версии жил час в памяти процесса, и сохранённая в
    телефоне привязка «тайтл → раздача» превращалась в 404.

    Args:
        release: Релиз от любого источника.

    Returns:
        Строку без пробелов, пригодную как сегмент пути URL.
    """
    if release.provider in TOPIC_ID_PROVIDERS:
        topic = re.sub(r"\D", "", release.topic_id)
        if topic:
            return f"{release.provider}:{topic}"
    if release.provider == bot.ProwlarrClient.key:
        # `id` в выдаче Prowlarr считается от guid и между версиями меняется.
        # Стабилен адрес темы: commentUrl -> infoUrl -> guid, он же source_url.
        anchor = release.source_url or release.magnet_url or release.download_url or release.title
        return f"{bot.ProwlarrClient.key}:{_slug(release.source_name)}:{_digest(anchor)}"
    anchor = release.source_url or release.download_url or release.title
    return f"{release.provider or 'release'}:{_digest(anchor)}"


def _slug(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", value.casefold()).strip("-")[:32] or "unknown"


def _digest(value: str) -> str:
    return hashlib.sha1(value.encode("utf-8", "replace")).hexdigest()[:16]


class ReleaseCache:
    """Кэш найденных релизов: горячий в памяти, долгий — на диске.

    Память отвечает за скорость, и в ней же доживают свой час прежние
    uuid-идентификаторы из уже выданных ответов. Диск отвечает за то, чтобы
    релиз нашёлся после перезапуска, когда телефон придёт за `.torrent`
    по идентификатору, сохранённому неделю назад.
    """

    def __init__(
        self, ttl_seconds: int = RESULTS_TTL_SECONDS, store: meta_service.TTLCache | None = None
    ) -> None:
        self._ttl = ttl_seconds
        self._lock = threading.Lock()
        self._items: dict[str, tuple[float, bot.Release]] = {}
        self._store = store

    def put_all(self, releases: list[bot.Release]) -> list[tuple[str, bot.Release]]:
        now = time.monotonic()
        stored: list[tuple[str, bot.Release]] = []
        with self._lock:
            self._prune(now)
            for release in releases:
                release_id = stable_release_id(release)
                self._items[release_id] = (now, release)
                stored.append((release_id, release))
        for release_id, release in stored if self._store is not None else []:
            self._store.put(f"release:{release_id}", release.to_dict())
        return stored

    def get(self, release_id: str) -> bot.Release | None:
        now = time.monotonic()
        with self._lock:
            self._prune(now)
            entry = self._items.get(release_id)
        return entry[1] if entry else self._from_store(release_id)

    def _from_store(self, release_id: str) -> bot.Release | None:
        if self._store is None:
            return None
        raw = self._store.get(f"release:{release_id}")
        if not isinstance(raw, dict):
            return None
        try:
            return bot.Release.from_dict(raw)
        except TypeError:
            # Набор полей Release поменялся между версиями — запись устарела
            LOG.warning("Сохранённый релиз %s не читается, пропускаю", release_id)
            return None

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
    # RuTracker и Kinozal — только с кукой входа и через Byparr
    providers.extend(bot.build_byparr_providers())
    return providers


def is_slow_provider(provider: Any) -> bool:
    """Источник читается через Byparr: один запрос на поиск, хэш — фоном."""
    return bool(getattr(provider, "slow", False))


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
    """Строка выдачи поиска.

    Первые тринадцать полей — прежние, их разбирает выпущенный TorBox One.
    Дальше идут структурные поля для KinoApp: он ранжирует раздачи сам и
    разбирать обратно строки «WEB-DL · 1080p» для этого не должен.

    Args:
        release_id: Идентификатор релиза в этом ответе.
        release: Релиз от источника.

    Returns:
        Словарь для ReleaseItem; словарь, а не модель, потому что форму
        первых полей проверяют тесты по ключам.
    """
    scope = meta_service.parse_release_scope(release.title)
    video = meta_service.parse_video_spec(release.title, release.details_text)
    voiceover = meta_service.parse_voiceover_spec(release.title, release.details_text)
    return {
        "id": release_id,
        "title": release.title,
        "size_text": release.size_text,
        "size_bytes": release.size_bytes,
        "seeders": release.seeders,
        "leechers": release.leechers,
        "source_name": release.source_name,
        "category": release.category,
        # Хэш известен, а magnet нет (Knaben через Prowlarr отдаёт ссылку-редирект на magnet):
        # magnet из хэша — иначе клиент пойдёт за .torrent, которого у источника нет.
        "magnet_url": release.magnet_url or (info_hash_magnet(release.info_hash, release.title)
                                             if release.provider == bot.ProwlarrClient.key and INFO_HASH_RE.match(release.info_hash or "") else ""),
        "info_hash": release.info_hash,
        "quality_summary": bot.video_quality_summary(release),
        "voiceover_summary": bot.voiceover_summary(release),
        "source_url": release.source_url,
        "provider": release.provider,
        "score": release.score,
        "content_kind": bot.release_content_kind(release),
        "resolution": video.resolution,
        "source_type": video.source_type,
        "video_codec": video.video_codec,
        "hdr": video.hdr,
        "voiceover_kinds": voiceover.kinds,
        "voiceover_studios": voiceover.studios,
        "season_from": scope.season_from,
        "season_to": scope.season_to,
        "episode_from": scope.episode_from,
        "episode_to": scope.episode_to,
        "episodes_declared": scope.episodes_declared,
        "published_at": release.published_at or None,
    }


def restore_release(release_id: str) -> bot.Release | None:
    """Восстанавливает релиз по стабильному id, когда кэш промахнулся.

    Args:
        release_id: Идентификатор из прежней выдачи поиска.

    Returns:
        Релиз или None, если по этому id его не собрать.
    """
    provider_key, _, rest = release_id.partition(":")
    provider = PROVIDER_BY_KEY.get(provider_key)
    restore = RESTORERS.get(provider_key)
    if provider is None or restore is None or not rest.isdigit():
        # У Prowlarr нет запроса «отдай релиз по guid»: его релизы
        # восстанавливаются только из дискового хранилища ReleaseCache
        return None
    try:
        return restore(provider, rest)
    except Exception as exc:
        # Без exc_info: у Byparr-трекеров текст ошибки уже вычищен, а
        # трассировка ничего к нему не добавит
        LOG.warning("Не получилось восстановить тему %s %s: %s", provider.name, rest, exc)
        return None


def restore_nnm_topic(client: Any, topic_id: str) -> bot.Release | None:
    """Собирает релиз NNM-Club заново по номеру темы.

    Ссылка на `.torrent` у NNM-Club живёт не под номером темы, а под номером
    вложения, поэтому страницу темы приходится читать. Заодно со страницы
    берётся magnet, если он там есть: это бесплатный `info_hash`.

    Разбор написан по устройству форума и живьём не проверялся — проверка
    в `scripts/live-check-search-api.sh`.

    Args:
        client: Клиент NNM-Club с готовой HTTP-сессией.
        topic_id: Номер темы (`viewtopic.php?t=`).

    Returns:
        Релиз или None, если на странице нет ссылки на `.torrent`.
    """
    source_url = urljoin(bot.NNM_BASE, f"viewtopic.php?t={topic_id}")
    response = client.http.get(source_url, timeout=25)
    response.raise_for_status()
    page = response.text
    download = re.search(r"download\.php\?id=(\d+)", page, re.I)
    if not download:
        return None
    magnet = re.search(r"magnet:\?[^\s\"'<>]+", page, re.I)
    magnet_url = html.unescape(magnet.group(0)) if magnet else ""
    return bot.Release(
        torrent_id=download.group(1),
        topic_id=topic_id,
        title=_nnm_topic_title(page) or f"NNM-Club, тема {topic_id}",
        category=client.name,
        size_text="",
        size_bytes=0,
        seeders=0,
        leechers=0,
        source_url=source_url,
        download_url=urljoin(bot.NNM_BASE, f"download.php?id={download.group(1)}"),
        score=0,
        provider=client.key,
        source_name=client.name,
        info_hash=bot.magnet_info_hash(magnet_url) if magnet_url else "",
        magnet_url=magnet_url if bot.is_magnet_url(magnet_url) else "",
    )


def _nnm_topic_title(page: str) -> str:
    """Заголовок темы: сначала phpBB-заголовок, потом `<title>` страницы."""
    for pattern in (r'class=["\']maintitle["\'][^>]*>(.*?)<', r"<title>(.*?)</title>"):
        found = re.search(pattern, page, re.S | re.I)
        if not found:
            continue
        text = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", html.unescape(found.group(1)))).strip()
        text = re.sub(r"^NNM-Club\s*[:—-]*\s*|\s*[:—-]*\s*NNM-Club$", "", text).strip()
        if text:
            return text
    return ""


def restore_rutracker_topic(client: Any, topic_id: str) -> bot.Release | None:
    """Собирает релиз RuTracker заново по номеру темы.

    То же, что `restore_nnm_topic`: страница темы читается одна (через
    Byparr, ~20 с) и даёт заголовок, размер и magnet, то есть `info_hash`.

    Args:
        client: `bot.RutrackerClient`.
        topic_id: Номер темы (`viewtopic.php?t=`).

    Returns:
        Релиз или None, если на странице нет ни magnet, ни заголовка.
    """
    page = client.fetch(urljoin(bot.RUTRACKER_BASE, f"viewtopic.php?t={topic_id}"))
    title = _tracker_page_title(page, r"\s*::\s*RuTracker\.org\s*$")
    info_hash = bot.rutracker_topic_hash(page)
    if not title and not info_hash:
        return None
    size = re.search(r'id="tor-size-humn"[^>]*title="(\d+)"', page)
    size_bytes = int(size.group(1)) if size else 0
    return bot.Release(
        torrent_id=topic_id,
        topic_id=topic_id,
        title=title or f"RuTracker, тема {topic_id}",
        category=client.name,
        size_text=bot.human_size(size_bytes) if size_bytes else "",
        size_bytes=size_bytes,
        seeders=0,
        leechers=0,
        source_url=urljoin(bot.RUTRACKER_BASE, f"viewtopic.php?t={topic_id}"),
        download_url=urljoin(bot.RUTRACKER_BASE, f"dl.php?t={topic_id}"),
        score=0,
        provider=client.key,
        source_name=client.name,
        info_hash=info_hash,
        magnet_url=info_hash_magnet(info_hash, title) if info_hash else "",
    )


def restore_kinozal_details(client: Any, details_id: str) -> bot.Release | None:
    """Собирает релиз Kinozal заново по номеру раздачи.

    Карточка `details.php` даёт заголовок и вес; хэша на ней нет, его
    приносит фоновая очередь со страницы `get_srv_details.php`.

    Args:
        client: `bot.KinozalClient`.
        details_id: Номер раздачи (`details.php?id=`).

    Returns:
        Релиз или None, если на странице нет заголовка раздачи.
    """
    page = client.fetch(urljoin(bot.KINOZAL_BASE, f"details.php?id={details_id}"))
    heading = re.search(
        rf'<h1>\s*<a[^>]*details\.php\?id={details_id}"[^>]*>(.*?)</a>', page, re.S | re.I
    )
    title = bot.strip_tags(heading.group(1)) if heading else _tracker_page_title(page, r"\s*::\s*Кинозал\.\w+\s*$")
    if not title:
        return None
    weight = re.search(r"Вес<span[^>]*>([^<(]+)", page)
    size_text = weight.group(1).strip() if weight else ""
    return bot.Release(
        torrent_id=details_id,
        topic_id=details_id,
        title=title,
        category=client.name,
        size_text=size_text,
        size_bytes=bot.parse_ru_size(size_text),
        seeders=0,
        leechers=0,
        source_url=urljoin(bot.KINOZAL_BASE, f"details.php?id={details_id}"),
        download_url=urljoin(bot.KINOZAL_BASE, f"download.php?id={details_id}"),
        score=0,
        provider=client.key,
        source_name=client.name,
    )


def _tracker_page_title(page: str, suffix_re: str) -> str:
    """Заголовок темы: `h1.maintitle`, иначе `<title>` без имени сайта."""
    for pattern in (r'<h1 class="maintitle"[^>]*>(.*?)</h1>', r"<title>(.*?)</title>"):
        found = re.search(pattern, page, re.S | re.I)
        if found:
            text = re.sub(suffix_re, "", bot.strip_tags(found.group(1))).strip()
            if text:
                return text
    return ""


def info_hash_magnet(info_hash: str, title: str) -> str:
    """Magnet по хэшу: так раздачу Byparr-трекера добавляют в TorBox."""
    magnet = f"magnet:?xt=urn:btih:{info_hash}"
    return f"{magnet}&dn={quote(title)}" if title else magnet


RESTORERS: dict[str, Callable[[Any, str], bot.Release | None]] = {
    bot.NnmClient.key: restore_nnm_topic,
    bot.RutrackerClient.key: restore_rutracker_topic,
    bot.KinozalClient.key: restore_kinozal_details,
}


def normalized_info_hash(value: str) -> str:
    """Приводит хэш к сорока символам нижнего регистра, как ждёт TorBox.

    Args:
        value: Хэш из magnet-ссылки или из ответа индексера.

    Returns:
        hex40 в нижнем регистре или пустую строку, если это не хэш.
    """
    cleaned = bot.normalize_info_hash(value)
    if INFO_HASH_RE.match(cleaned):
        return cleaned
    # base32 из magnet-ссылок старых трекеров: 32 символа вместо сорока
    if len(cleaned) == 32:
        try:
            return base64.b32decode(cleaned.upper()).hex()
        except (binascii.Error, ValueError):
            return ""
    return ""


def hash_cache_key(release_id: str, release: bot.Release) -> str:
    """Ключ кэша хэша: не только релиз, но и сам файл раздачи.

    Онгоинг на NNM-Club обновляется в той же теме новым `.torrent`: номер
    темы, а с ним и стабильный id, остаются прежними, а вложение и info_hash
    — новые. Под ключом одного id кэш сутки отдавал хэш прежнего файла:
    KinoApp видел раздачу «в кэше TorBox», а TorBox получал уже другой
    `.torrent`, и файлы так и не появлялись.

    Файл называет `torrent_id`: у NNM-Club это номер вложения из
    `download.php?id=`, у Prowlarr — guid индексера (у RuTor и BigFANGroup
    это ссылка на сам файл). Адрес закачки не годится: у Prowlarr в нём
    ключ API.

    Args:
        release_id: Стабильный идентификатор релиза.
        release: Релиз, чей файл отдаст `/release/{id}/torrent`.

    Returns:
        Ключ или пустую строку, если файл назвать нечем, — тогда хэш не
        кэшируется вовсе.
    """
    file_id = release.torrent_id.strip()
    if not file_id:
        return ""
    return f"hash:{release_id}:{file_id if file_id.isdigit() else _digest(file_id)}"


def release_info_hash(
    release_id: str, release: bot.Release, allow_slow: bool = True
) -> str | None:
    """Считает info_hash релиза, скачивая `.torrent`, и кэширует результат.

    NNM-Club не отдаёт в выдаче ни magnet, ни хэш, а без хэша приложение не
    может спросить TorBox, лежит ли раздача в кэше. Считать хэш на поиске
    нельзя — это десятки лишних запросов к трекеру, поэтому он считается
    лениво и только для тех релизов, о которых спросил клиент.

    Хэш, который релиз принёс сам — от индексера или в magnet, — главнее
    кэша: он из той же выдачи, что и файл. Кэш нужен только закачке.

    У RuTracker и Kinozal `.torrent` через Byparr не скачать: хэш берётся
    со страницы трекера (`resolve_info_hash`), и это 20 секунд браузера.
    Поэтому их хэш считает только фоновая очередь, а запрос клиента с
    `allow_slow=False` довольствуется кэшем.

    Args:
        release_id: Стабильный идентификатор релиза.
        release: Релиз, для которого нужен хэш.
        allow_slow: Можно ли идти за хэшем к медленному трекеру.

    Returns:
        hex40 в нижнем регистре или None, если источник хэш не отдал.
    """
    cache_key = hash_cache_key(release_id, release)
    known = normalized_info_hash(release.info_hash) or normalized_info_hash(
        bot.magnet_info_hash(release.magnet_url)
    )
    if known:
        # Запоминаем под файлом: у того же вложения из поиска NNM-Club
        # magnet уже не будет, а хэш у файла тот же
        if cache_key and HASH_CACHE.get(cache_key) != known:
            HASH_CACHE.put(cache_key, known)
        return known

    cached = HASH_CACHE.get(cache_key) if cache_key else None
    if isinstance(cached, str) and cached:
        return cached

    provider = PROVIDER_BY_KEY.get(release.provider)
    if provider is None:
        return None
    if is_slow_provider(provider):
        if not allow_slow:
            return None
        value = normalized_info_hash(provider.resolve_info_hash(release))
    else:
        try:
            payload = provider.download_torrent(release)
            value = bot.torrent_fingerprint(payload).casefold()
        except bot.MagnetRedirect as redirect:
            value = bot.magnet_info_hash(redirect.magnet).casefold()
    if not INFO_HASH_RE.match(value):
        return None
    if cache_key:
        HASH_CACHE.put(cache_key, value)
    return value


def collect_info_hashes(ids: list[str], deadline: float) -> dict[str, str | None]:
    """Считает хэши пачки релизов параллельно, с общим сроком.

    Отказ одного релиза не мешает остальным: у него в ответе будет None.
    Неудачи не кэшируются — иначе разовая ошибка трекера держалась бы сутки.

    Релизы RuTracker и Kinozal не считаются здесь же: готовый хэш берётся
    из кэша сразу, остальные встают в начало фоновой очереди, и запрос ждёт
    её до своего срока. Так один и тот же хэш не считается дважды, а Byparr
    не получает пачку параллельных запросов, которые всё равно выстроит в ряд.

    Args:
        ids: Стабильные идентификаторы релизов.
        deadline: Общий срок на всю пачку, в секундах.

    Returns:
        Словарь «идентификатор → hex40 или None».
    """
    started = time.monotonic()
    result: dict[str, str | None] = {release_id: None for release_id in ids}
    pool = ThreadPoolExecutor(max_workers=max(1, HASH_WORKERS), thread_name_prefix="hash")
    futures: dict[Future[str | None], str] = {}
    slow: list[tuple[str, bot.Release]] = []
    for release_id in ids:
        release = CACHE.get(release_id) or slow_release_stub(release_id) or restore_release(release_id)
        if release is None:
            LOG.info("Релиз %s не найден, хэш не считаю", release_id)
            continue
        if is_slow_provider(PROVIDER_BY_KEY.get(release.provider)):
            result[release_id] = release_info_hash(release_id, release, allow_slow=False)
            if result[release_id] is None:
                slow.append((release_id, release))
            continue
        futures[pool.submit(release_info_hash, release_id, release)] = release_id
    if slow:
        PREFETCHER.enqueue(slow)
    try:
        for future in as_completed(futures, timeout=max(1.0, deadline)):
            release_id = futures[future]
            try:
                result[release_id] = future.result()
            except Exception as exc:
                LOG.warning("Хэш релиза %s не посчитан: %s", release_id, exc)
    except FuturesTimeout:
        LOG.warning("Хэши: не успели к сроку %s с, отдаю что посчитали", deadline)
    finally:
        # Ждать отставших нельзя: срок общий, и ответ уходит сейчас
        pool.shutdown(wait=False, cancel_futures=True)
    if slow:
        remaining = deadline - (time.monotonic() - started)
        if remaining > 0:
            PREFETCHER.wait_for([release_id for release_id, _ in slow], remaining)
        for release_id, release in slow:
            result[release_id] = release_info_hash(release_id, release, allow_slow=False)
    return result


def slow_release_stub(release_id: str) -> bot.Release | None:
    """Релиз медленного трекера по одному id — без обращения к трекеру.

    Хэшу RuTracker и Kinozal нужен только номер темы, а честное
    восстановление (`restore_release`) — это ещё 20 секунд Byparr прямо в
    запросе. Заглушка живёт только внутри расчёта хэша и никуда не сохраняется.

    Args:
        release_id: Идентификатор вида `rutracker:<тема>` или `kinozal:<id>`.

    Returns:
        Релиз-заглушку или None, если id не от медленного трекера.
    """
    provider_key, _, topic = release_id.partition(":")
    provider = PROVIDER_BY_KEY.get(provider_key)
    if not is_slow_provider(provider) or not topic.isdigit():
        return None
    base = bot.RUTRACKER_BASE if provider_key == bot.RutrackerClient.key else bot.KINOZAL_BASE
    page = f"viewtopic.php?t={topic}" if provider_key == bot.RutrackerClient.key else f"details.php?id={topic}"
    return bot.Release(
        torrent_id=topic, topic_id=topic, title="", category=provider.name, size_text="",
        size_bytes=0, seeders=0, leechers=0, source_url=urljoin(base, page), download_url="",
        score=0, provider=provider_key, source_name=provider.name,
    )


def with_known_hashes(release_id: str, release: bot.Release) -> bot.Release:
    """Подставляет медленному релизу хэш и magnet, если они уже посчитаны.

    Выдача RuTracker и Kinozal приходит без хэша, а `.torrent` у них не
    скачать. С хэшем из кэша (его считает фоновая очередь) повторный поиск
    сразу отдаёт и `info_hash`, и magnet — по нему раздачу и добавляют.

    Args:
        release_id: Стабильный идентификатор релиза.
        release: Релиз из выдачи.

    Returns:
        Тот же релиз, с хэшем и magnet, если они известны.
    """
    if release.info_hash or not is_slow_provider(PROVIDER_BY_KEY.get(release.provider)):
        return release
    cache_key = hash_cache_key(release_id, release)
    cached = HASH_CACHE.get(cache_key) if cache_key else None
    if not isinstance(cached, str) or not INFO_HASH_RE.match(cached):
        return release
    return dataclasses.replace(
        release, info_hash=cached, magnet_url=info_hash_magnet(cached, release.title)
    )


class HashPrefetcher:
    """Фоновая очередь хэшей медленных трекеров: один поток, по одному релизу.

    Очередь ограничена и без повторов. Свежий поиск и явный запрос хэшей
    встают в начало: клиенту нужнее то, что он смотрит сейчас, а хвост
    старых поисков отбрасывается при переполнении. Релиз, хэш которого не
    дался, какое-то время не принимается снова: пауза удваивается с каждой
    неудачей, от минуты до часа. Пока идёт поиск, очередь не берёт новую
    задачу, чтобы не занимать Byparr перед запросом пользователя.
    """

    def __init__(
        self,
        resolve: Callable[[str, bot.Release], str | None],
        known: Callable[[str, bot.Release], bool],
        max_queue: int = PREFETCH_QUEUE_SIZE,
        backoff_seconds: float = PREFETCH_BACKOFF_SECONDS,
        backoff_max_seconds: float = PREFETCH_BACKOFF_MAX_SECONDS,
        clock: Callable[[], float] = time.monotonic,
        autostart: bool = True,
    ) -> None:
        self._resolve = resolve
        self._known = known
        self._max_queue = max(1, max_queue)
        self._backoff = backoff_seconds
        self._backoff_max = backoff_max_seconds
        self._clock = clock
        self._autostart = autostart
        self._cond = threading.Condition()
        self._queue: dict[str, bot.Release] = {}
        self._current: str | None = None
        self._failures: dict[str, tuple[int, float]] = {}
        self._foreground = 0
        self._thread: threading.Thread | None = None

    def enqueue(self, items: Iterable[tuple[str, bot.Release]]) -> list[str]:
        """Ставит релизы в начало очереди в данном порядке.

        Args:
            items: Пары «стабильный id, релиз» в порядке важности.

        Returns:
            Идентификаторы, которые попали в очередь (новые и поднятые).
        """
        unknown = [(release_id, release) for release_id, release in items if not self._known(release_id, release)]
        now = self._clock()
        with self._cond:
            fresh: dict[str, bot.Release] = {}
            for release_id, release in unknown:
                failure = self._failures.get(release_id)
                if release_id in fresh or release_id == self._current or (failure and now < failure[1]):
                    continue
                fresh[release_id] = release
            if not fresh:
                return []
            rest = {key: value for key, value in self._queue.items() if key not in fresh}
            merged = {**fresh, **rest}
            self._queue = dict(list(merged.items())[: self._max_queue])
            accepted = [release_id for release_id in fresh if release_id in self._queue]
            self._cond.notify_all()
        if self._autostart:
            self._ensure_worker()
        return accepted

    def pending(self) -> list[str]:
        """Идентификаторы в очереди, по порядку; для журнала и тестов."""
        with self._cond:
            return list(self._queue)

    @contextlib.contextmanager
    def foreground(self) -> Iterator[None]:
        """На время поиска очередь не начинает новых запросов к Byparr."""
        with self._cond:
            self._foreground += 1
        try:
            yield
        finally:
            with self._cond:
                self._foreground -= 1
                self._cond.notify_all()

    def wait_for(self, release_ids: Iterable[str], timeout: float) -> bool:
        """Ждёт, пока эти релизы не уйдут из очереди, но не дольше срока.

        Args:
            release_ids: Кого ждём.
            timeout: Секунды.

        Returns:
            True, если все обработаны (удачно или нет) к сроку.
        """
        wanted = set(release_ids)
        with self._cond:
            return self._cond.wait_for(
                lambda: not (wanted & self._queue.keys()) and self._current not in wanted,
                timeout=max(0.0, timeout),
            )

    def run_once(self) -> bool:
        """Обрабатывает первый релиз очереди в текущем потоке.

        Returns:
            False, если очередь пуста.
        """
        with self._cond:
            if not self._queue:
                return False
            release_id = next(iter(self._queue))
            release = self._queue.pop(release_id)
            self._current = release_id
        value: str | None = None
        try:
            value = self._resolve(release_id, release)
        except Exception as exc:
            # Текст ошибки Byparr-трекера уже без кук, трассировка не нужна
            LOG.warning("Фоновый хэш %s не получен: %s", release_id, exc)
        with self._cond:
            if value:
                self._failures.pop(release_id, None)
            else:
                count = self._failures.get(release_id, (0, 0.0))[0] + 1
                pause = min(self._backoff * 2 ** (count - 1), self._backoff_max)
                self._failures[release_id] = (count, self._clock() + pause)
                if len(self._failures) > 1000:
                    self._failures.pop(next(iter(self._failures)))
            self._current = None
            self._cond.notify_all()
        return True

    def _ensure_worker(self) -> None:
        with self._cond:
            if self._thread is not None and self._thread.is_alive():
                return
            self._thread = threading.Thread(target=self._work, name="hash-prefetch", daemon=True)
            self._thread.start()

    def _work(self) -> None:
        while True:
            with self._cond:
                self._cond.wait_for(lambda: bool(self._queue) and self._foreground == 0)
            try:
                self.run_once()
            except Exception:
                LOG.exception("Фоновая очередь хэшей споткнулась")


def prefetch_resolve(release_id: str, release: bot.Release) -> str | None:
    """Хэш для фоновой очереди: можно идти к медленному трекеру."""
    return release_info_hash(release_id, release, allow_slow=True)


def prefetch_known(release_id: str, release: bot.Release) -> bool:
    """Хэш уже есть — у релиза или в кэше хэшей."""
    return release_info_hash(release_id, release, allow_slow=False) is not None


def build_search_queries(
    q: str, kp_id: int | None, season: int | None, year: int | None, kind: str | None
) -> list[str]:
    """Собирает запросы к трекерам из параметров приложения.

    Свободная строка идёт первой и одна — так работал прежний вызов. По
    `kp_id` сервер строит запросы сам: имя и год, для сериала — ещё и
    «имя сезон N», плюс оригинальное название. Больше одного запроса нужно
    потому, что `title_matches_query` требует в имени раздачи все слова
    запроса: раздача без года в названии отсеялась бы запросом «имя год».

    Args:
        q: Свободная строка от пользователя.
        kp_id: Идентификатор тайтла в Кинопоиске.
        season: Номер сезона для сериала.
        year: Год, если клиент знает его точнее каталога.
        kind: `movie` или `series`; иначе тип берётся из карточки.

    Returns:
        До MAX_SEARCH_QUERIES непустых запросов без повторов.
    """
    queries: list[str] = [q.strip()] if q.strip() else []
    card = catalog_title_card(kp_id) if kp_id else None
    if card is not None and card.meta.matched:
        meta = card.meta
        name = meta.title or meta.original_title
        original = meta.original_title if meta.original_title != name else ""
        shown_year = str(year or "") or meta.year[:4]
        is_series = (kind or "").casefold() == "series" or bool(card.seasons) or (
            kind is None and any(marker in meta.media_type for marker in ("TV", "SERIAL", "SERIES"))
        )
        if name and is_series:
            if season:
                queries.append(f"{name} сезон {season}")
            queries.append(name)
        elif name:
            queries.append(f"{name} {shown_year}".strip() if shown_year else name)
            if shown_year:
                queries.append(name)
        if original:
            queries.append(original)

    unique: list[str] = []
    for query in queries:
        trimmed = query.strip()[: bot.MAX_QUERY_LENGTH]
        if len(trimmed) >= 2 and trimmed.casefold() not in {item.casefold() for item in unique}:
            unique.append(trimmed)
    return unique[:MAX_SEARCH_QUERIES]


YEAR_RE = re.compile(r"(?<!\d)(19\d{2}|20\d{2})(?!\d)")


def drop_other_years(
    releases: list[bot.Release], target_year: int | None, queries: list[str]
) -> list[bot.Release]:
    """Отсекает раздачи одноимённых фильмов других лет.

    По «Обитель зла» 2026 трекеры отдают и 2002, и 2016 — они занимали две
    трети выдачи и вытесняли нужный фильм из 60 мест. Раздача отбрасывается,
    если в её названии есть годы и ни один не ближе чем на год к нужному.
    Числа из самого названия фильма («2001: Космическая одиссея», «1917»)
    годами не считаются; раздачи без года остаются.

    Args:
        releases: Выдача источников.
        target_year: Год фильма; None — не отсекать.
        queries: Запросы поиска: годы в них — часть названия или нужный год.

    Returns:
        Раздачи без чужих лет, в прежнем порядке.
    """
    if not target_year:
        return releases
    name_years = {int(value) for query in queries for value in YEAR_RE.findall(query)} - {target_year}
    kept: list[bot.Release] = []
    for release in releases:
        years = {int(value) for value in YEAR_RE.findall(release.title)} - name_years
        if not years or any(abs(year - target_year) <= 1 for year in years):
            kept.append(release)
    return kept


def aggregate_releases(
    providers: list[bot.SearchProvider], queries: list[str], deadline: float | None
) -> tuple[list[bot.Release], dict[str, str], bool]:
    """Опрашивает источники и сводит выдачу в один ранжированный список.

    Повторяет SearchAggregator.search бота шаг в шаг — те же отсев, ранг,
    дедупликация и разнообразие — и добавляет две вещи, которых там нет:
    несколько запросов за один проход и общий срок. Без срока клиент стоит
    столько, сколько думает самый медленный трекер.

    Args:
        providers: Настроенные источники.
        queries: Запросы, по одному проходу на каждый источник.
        deadline: Срок в секундах; None — ждать всех, как прежде.

    Returns:
        Кортеж «релизы, статус строками, статус разбором, признак неполноты».
    """
    found: list[tuple[str, bot.Release]] = []
    counts: dict[str, int] = {provider.name: 0 for provider in providers}
    failures: dict[str, int] = {provider.name: 0 for provider in providers}
    pending: dict[str, int] = {provider.name: 0 for provider in providers}

    pool = ThreadPoolExecutor(
        max_workers=min(8, max(1, len(providers) * len(queries))), thread_name_prefix="search"
    )
    tasks: dict[Future[list[bot.Release]], tuple[bot.SearchProvider, str]] = {}
    for provider in providers:
        # Byparr-трекер — 20 секунд на страницу и один браузер на всех:
        # ему только первый, самый точный запрос
        for query in queries[:1] if is_slow_provider(provider) else queries:
            tasks[pool.submit(provider.search, query)] = (provider, query)
            pending[provider.name] += 1
    partial = False
    try:
        for future in as_completed(tasks, timeout=deadline):
            provider, query = tasks[future]
            pending[provider.name] -= 1
            try:
                items = future.result()
            except Exception as exc:
                failures[provider.name] += 1
                LOG.warning("Источник %s не ответил: %s", provider.name, exc, exc_info=True)
                continue
            counts[provider.name] += len(items)
            found.extend((query, item) for item in items)
    except FuturesTimeout:
        partial = True
        LOG.warning("Поиск: срок %s с вышел, отдаю что успели", deadline)
    finally:
        pool.shutdown(wait=False, cancel_futures=True)

    status: dict[str, str] = {}
    sources: list[SourceStatus] = []
    for provider in providers:
        if pending[provider.name] > 0:
            state = "timeout"
            status[provider.name] = "таймаут"
        elif failures[provider.name] and not counts[provider.name]:
            state = "error"
            status[provider.name] = "ошибка"
        else:
            state = "ok"
            status[provider.name] = str(counts[provider.name])
        sources.append(SourceStatus(name=provider.name, state=state, found=counts[provider.name]))
    return rank_releases(found), status, sources, partial


def rank_releases(found: list[tuple[str, bot.Release]]) -> list[bot.Release]:
    """Отсев, ранг, дедупликация и разнообразие — как в SearchAggregator бота."""
    unique: dict[str, bot.Release] = {}
    for query, item in found:
        if not bot.title_matches_query(query, item.title):
            continue
        ranked = dataclasses.replace(
            item,
            score=item.score + bot.source_bonus(item.source_name) + bot.query_relevance(query, item.title),
        )
        key = bot.release_dedupe_key(ranked)
        previous = unique.get(key)
        if previous is None or (ranked.score, ranked.seeders) > (previous.score, previous.seeders):
            unique[key] = ranked
    ranked_items = sorted(unique.values(), key=lambda item: (item.score, item.seeders), reverse=True)
    video_items = [item for item in ranked_items if bot.release_content_kind(item) == "video"]
    # Когда настоящих видеораздач достаточно, одноимённые игры и саундтреки
    # не должны занимать места в коротком списке — правило бота, дословно.
    if len(video_items) >= 4:
        ranked_items = video_items
    return ranked_items


class ReleaseItem(BaseModel):
    """Строка выдачи `/search`.

    Первые тринадцать полей — прежние; их разбирает выпущенный TorBox One,
    и ни имена, ни типы у них не меняются. Остальные добавлены для KinoApp.
    """

    id: str
    title: str
    size_text: str
    size_bytes: int
    seeders: int
    leechers: int
    source_name: str
    category: str
    magnet_url: str
    info_hash: str
    quality_summary: str
    voiceover_summary: str
    source_url: str
    provider: str
    score: int
    content_kind: str
    resolution: int | None = None
    source_type: str | None = None
    video_codec: str | None = None
    hdr: str | None = None
    voiceover_kinds: list[str] = Field(default_factory=list)
    voiceover_studios: list[str] = Field(default_factory=list)
    season_from: int | None = None
    season_to: int | None = None
    episode_from: int | None = None
    episode_to: int | None = None
    episodes_declared: int | None = None
    published_at: str | None = None


class SourceStatus(BaseModel):
    """Состояние одного источника в этом поиске, разбором, а не строкой.

    `status` рядом остаётся словарём строк: его печатает бот и разбирает
    выпущенный TorBox One. Но «ошибка» и «18» там лежат в одном поле, и
    клиент, который хочет отличить «ничего не нашлось» от «все трекеры
    легли», вынужден сравнивать строки. Живая проверка это и показала:
    оба источника отказали, а ответ выглядел как пустая выдача.
    """

    name: str
    # ok — ответил, error — отказал, timeout — не успел к сроку
    state: str
    found: int = 0


class SearchResult(BaseModel):
    """Ответ `/search`: статус по источникам и найденные раздачи."""

    status: dict[str, str]
    partial: bool = False
    sources: list[SourceStatus] = Field(default_factory=list)
    results: list[ReleaseItem] = Field(default_factory=list)


class HashRequest(BaseModel):
    """Запрос ленивого расчёта хэшей."""

    ids: list[str] = Field(default_factory=list)
    limit: int | None = None
    deadline: float | None = None


class BriefRequest(BaseModel):
    """Тайтлы, которым лента «Что смотреть» просит обложку, логотип и «о чём»."""

    ids: list[int] = Field(default_factory=list)


class HashResult(BaseModel):
    """Ответ `/release/hashes`: хэш или None на каждый обработанный релиз."""

    hashes: dict[str, str | None] = Field(default_factory=dict)
    limit: int = DEFAULT_HASH_IDS


class SeasonItem(BaseModel):
    """Сезон и число серий в нём.

    `released: false` — сезон ещё не вышел (`meta_service.unreleased_seasons`).
    У вышедшего сезона и у сезона, о котором судить не по чему, поля нет
    вовсе, как до версии 1.3: `true` сервер не отдаёт.
    """

    season: int
    episodes: int
    released: bool | None = None

    # Возврат без аннотации намеренно: с `-> dict[str, Any]` Pydantic берёт
    # её за схему ответа, и в OpenAPI у сезона пропадают все поля
    @model_serializer(mode="wrap")
    def _drop_unknown_release(self, handler: SerializerFunctionWrapHandler):
        payload = handler(self)
        if payload.get("released") is None:
            payload.pop("released", None)
        return payload


class TitleCardResult(BaseModel):
    """Ответ `/catalog/title/{kp_id}`: карточка плюс внешние id и сезоны.

    `tmdb_id` и `tmdb_media_type` лежат внутри `meta`, а не рядом: их
    отдаёт сам `TitleMeta`, и дублировать их сюда значило бы завести два
    источника правды для одного значения.
    """

    meta: dict[str, Any]
    imdb_id: str = ""
    seasons: list[SeasonItem] = Field(default_factory=list)


class HealthResult(BaseModel):
    """Ответ `/health`: проверка адреса сервера без обращения к внешним API."""

    status: str = "ok"
    version: str = API_VERSION
    token_required: bool = False
    providers: list[str] = Field(default_factory=list)
    catalog: bool = False


META = meta_service.build_meta_service()
PROVIDERS = build_providers()
PROVIDER_BY_KEY = {provider.key: provider for provider in PROVIDERS}
CATALOG = build_catalog()
HASH_CACHE = meta_service.TTLCache(HASH_CACHE_PATH, HASH_TTL_SECONDS)
RELEASE_STORE = meta_service.TTLCache(RELEASE_STORE_PATH, RELEASE_STORE_TTL_SECONDS)
CACHE = ReleaseCache(store=RELEASE_STORE)
PREFETCHER = HashPrefetcher(prefetch_resolve, prefetch_known)

app = FastAPI(title="TorBox One Search API", version=API_VERSION)


def require_token(authorization: str = Header(default="")) -> None:
    """Статический bearer-токен; без SEARCH_API_TOKEN авторизация выключена (dev-режим)."""
    if not API_TOKEN:
        return
    if authorization != f"Bearer {API_TOKEN}":
        raise HTTPException(status_code=401, detail="Неверный токен")


def get_cached_release(release_id: str) -> bot.Release:
    """Релиз по идентификатору: кэш, дисковое хранилище, потом сам трекер."""
    release = CACHE.get(release_id) or restore_release(release_id)
    if release is None:
        raise HTTPException(
            status_code=404, detail="Результат поиска устарел, повтори поиск"
        )
    return release


def catalog_title_card(kp_id: int) -> meta_service.TitleCard | None:
    """Карточка каталога по id или None, если каталог не настроен или молчит."""
    if not META.available:
        return None
    try:
        return META.title_card(kp_id)
    except Exception:
        LOG.warning("Каталог не отдал карточку %s", kp_id, exc_info=True)
        return None


def catalog_error(exc: Exception) -> HTTPException:
    """Ошибку каталога превращает в понятный приложению код.

    Квота — это не «каталог недоступен»: её ждут, а не повторяют запрос.
    """
    if isinstance(exc, meta_service.CatalogLimitError):
        return HTTPException(status_code=429, detail=str(exc))
    return HTTPException(status_code=502, detail=f"Каталог недоступен: {exc}")


@app.get("/health")
def health() -> HealthResult:
    """Проверка адреса сервера: без токена и без обращений к внешним API."""
    return HealthResult(
        status="ok",
        version=API_VERSION,
        token_required=bool(API_TOKEN),
        providers=[provider.name for provider in PROVIDERS],
        catalog=META.available,
    )


@app.get("/health/auth", dependencies=[Depends(require_token)])
def health_auth() -> dict[str, str]:
    """Проверка токена: тот же дешёвый ответ, но за авторизацией."""
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
def tracker_search(
    q: str = "",
    kp_id: int | None = None,
    season: int | None = None,
    year: int | None = None,
    kind: str | None = None,
    limit: int = MAX_APP_RESULTS,
    deadline: float | None = None,
) -> SearchResult:
    """Поиск раздач по трекерам.

    Прежний вызов `?q=` работает как раньше: один запрос, ожидание всех
    источников, восемь результатов, тот же набор полей в каждой строке.

    Args:
        q: Свободная строка запроса.
        kp_id: Идентификатор тайтла в Кинопоиске — сервер строит запросы сам.
        season: Номер сезона для сериала.
        year: Год, если он известен клиенту точнее, чем каталогу.
        kind: `movie` или `series`.
        limit: Сколько раздач вернуть, 1…MAX_SEARCH_LIMIT.
        deadline: Срок в секундах; по истечении отдаётся `partial: true`.

    Returns:
        Статус по источникам, признак неполноты и список раздач.
    """
    if not PROVIDERS:
        raise HTTPException(
            status_code=503, detail="Ни один поисковый источник не настроен"
        )
    queries = build_search_queries(q, kp_id, season, year, kind)
    if not queries:
        raise HTTPException(status_code=400, detail="Запрос слишком короткий")
    wanted = min(max(limit, 1), MAX_SEARCH_LIMIT)

    slow_search = any(is_slow_provider(provider) for provider in PROVIDERS)
    with PREFETCHER.foreground() if slow_search else contextlib.nullcontext():
        releases, status, sources, partial = aggregate_releases(list(PROVIDERS), queries, deadline)
    if slow_search:
        releases = [with_known_hashes(stable_release_id(item), item) for item in releases]
    if (kind or "").casefold() != "series":
        card = catalog_title_card(kp_id) if kp_id and not year else None
        card_year = card.meta.year[:4] if card is not None and card.meta.matched else ""
        target_year = year or (int(card_year) if card_year.isdigit() else None)
        if card is not None and (card.seasons or any(marker in card.meta.media_type for marker in ("TV", "SERIAL", "SERIES"))):
            target_year = None
        releases = drop_other_years(releases, target_year, queries)
    # Два прохода разнообразия — как у бота: сначала широкий срез кандидатов,
    # потом короткий список. Для limit <= 24 результат тот же, что прежде.
    candidates = bot.select_diverse_releases(releases, max(bot.MAX_SEARCH_CANDIDATES, wanted))
    top = bot.select_diverse_releases(candidates, wanted)
    stored = CACHE.put_all(top)
    LOG.info("Поиск %s: %d релизов, статус %s", queries, len(stored), status)
    if slow_search:
        # Хэши RuTracker и Kinozal — заранее, в порядке ранга
        slow_items = [
            (release_id, release) for release_id, release in stored
            if is_slow_provider(PROVIDER_BY_KEY.get(release.provider))
            and not prefetch_known(release_id, release)
        ]
        PREFETCHER.enqueue(slow_items[:PREFETCH_PER_SEARCH])
    return SearchResult(
        status=status,
        partial=partial,
        sources=sources,
        results=[
            ReleaseItem.model_validate(release_payload(release_id, release))
            for release_id, release in stored
        ],
    )


@app.post("/release/hashes", dependencies=[Depends(require_token)])
def release_hashes(request: HashRequest) -> HashResult:
    """Считает `info_hash` для верхних кандидатов выдачи.

    NNM-Club не отдаёт в поиске ни magnet, ни хэш, а без хэша приложение не
    может спросить TorBox, лежит ли раздача в кэше, — и это большинство
    русских раздач. Хэш берётся из `.torrent` по одному запросу на релиз,
    поэтому эндпоинт ленивый и с потолком: считать его на каждом поиске —
    это десятки лишних обращений к трекеру.

    Args:
        request: Идентификаторы релизов и необязательный общий срок.

    Returns:
        Словарь «идентификатор → hex40 или None» и применённый потолок.
    """
    wanted = [release_id.strip() for release_id in request.ids if release_id.strip()]
    if not wanted:
        return HashResult(hashes={}, limit=DEFAULT_HASH_IDS)
    limit = min(max(request.limit or DEFAULT_HASH_IDS, 1), MAX_HASH_IDS)
    deadline = request.deadline or HASH_DEADLINE_SECONDS
    # Лишние идентификаторы не отбрасываются молча: их просто нет в ответе,
    # и клиент видит это по `limit`.
    hashes = collect_info_hashes(list(dict.fromkeys(wanted))[:limit], deadline)
    LOG.info("Хэши: спросили %d, посчитали %d", len(wanted), sum(1 for value in hashes.values() if value))
    return HashResult(hashes=hashes, limit=limit)


@app.get("/catalog/quota", dependencies=[Depends(require_token)])
def catalog_quota() -> dict[str, Any]:
    """Остаток суточной квоты каталога: бесплатный способ узнать её заранее."""
    if not META.available:
        raise HTTPException(status_code=503, detail="KINOPOISK_API_KEY не настроен")
    try:
        return META.quota()
    except Exception as exc:
        raise catalog_error(exc) from exc


@app.get("/catalog/title/{kp_id}", dependencies=[Depends(require_token)])
def catalog_title(kp_id: int) -> TitleCardResult:
    """Карточка тайтла по идентификатору каталога.

    Args:
        kp_id: Идентификатор Кинопоиска.

    Returns:
        TitleMeta, идентификатор IMDb и состав сезонов. `tmdb_id` и
        `tmdb_media_type` приходят внутри `meta`: Кинопоиск их не отдаёт,
        и сервер выясняет их у самого TMDB по IMDb-идентификатору —
        точным редиректом, без ключа API. У невышедшего сезона —
        `released: false`.
    """
    if not META.available:
        raise HTTPException(status_code=503, detail="KINOPOISK_API_KEY не настроен")
    try:
        card = META.title_card(kp_id)
    except Exception as exc:
        raise catalog_error(exc) from exc
    return TitleCardResult(
        meta=dataclasses.asdict(card.meta),
        imdb_id=card.imdb_id,
        seasons=[
            SeasonItem(season=item.season, episodes=item.episodes, released=item.released)
            for item in card.seasons
        ],
    )


@app.get("/catalog/shelf/{kind:path}", dependencies=[Depends(require_token)])
def catalog_shelf(kind: str, page: int = 1) -> list[dict[str, Any]]:
    """Полка Главного экрана.

    Args:
        kind: `popular`, `top_movies`, `top_series`, `popular_series`,
            `premieres`, `closest_releases`, тематические подборки, а также
            `genre/<название>` и `year/<год>`. Полный список — в
            `docs/search-api.md`; всё это подборки самого Кинопоиска.
        page: Страница выдачи, начиная с 1.

    Returns:
        Карточки в той же форме, что и `/catalog/popular`.
    """
    if not META.available:
        raise HTTPException(status_code=503, detail="KINOPOISK_API_KEY не настроен")
    try:
        return [catalog_payload(title) for title in META.shelf(kind, page)]
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except Exception as exc:
        raise catalog_error(exc) from exc


@app.get("/catalog/discover", dependencies=[Depends(require_token)])
def catalog_discover(
    media_type: str = Query("ALL", alias="type"),
    genre: str = "",
    country: str = "",
    year_from: int | None = None,
    year_to: int | None = None,
    rating_from: float | None = None,
    order: str = "NUM_VOTE",
    page: int = 1,
) -> list[dict[str, Any]]:
    """Подбор по фильтру каталога для ленты «Что смотреть».

    Args:
        media_type: Параметр `type`: `ALL`, `FILM`, `TV_SERIES`, `MINI_SERIES`, `TV_SHOW`.
        genre: Жанр по-русски, как в карточке.
        country: Страна по-русски, как в карточке.
        year_from: Нижняя граница года.
        year_to: Верхняя граница года.
        rating_from: Порог оценки Кинопоиска.
        order: `NUM_VOTE` (по умолчанию), `RATING` или `YEAR`.
        page: Страница выдачи, начиная с 1.

    Returns:
        Расширенные карточки: форма полки плюс `rating_imdb`. Полей карточки
        фильма здесь нет (`detailed: false`) — их просят через `/catalog/briefs`.
    """
    if not META.available:
        raise HTTPException(status_code=503, detail="KINOPOISK_API_KEY не настроен")
    try:
        return META.discover(
            media_type=media_type,
            genre=genre,
            country=country,
            year_from=year_from,
            year_to=year_to,
            rating_from=rating_from,
            order=order,
            page=page,
        )
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except Exception as exc:
        raise catalog_error(exc) from exc


@app.get("/catalog/similar/{kp_id}", dependencies=[Depends(require_token)])
def catalog_similar(kp_id: int, limit: int = meta_service.DEFAULT_SIMILAR_LIMIT) -> list[dict[str, Any]]:
    """«Похожие» Кинопоиска, каждый с карточкой фильма: страна, жанры, оценки, обложка.

    Args:
        kp_id: Тайтл, к которому ищем похожие.
        limit: Сколько первых похожих отдать; каждый стоит запроса карточки
            на холодном кэше.

    Returns:
        Расширенные карточки в порядке каталога.
    """
    if not META.available:
        raise HTTPException(status_code=503, detail="KINOPOISK_API_KEY не настроен")
    try:
        return META.similar(kp_id, limit)
    except Exception as exc:
        raise catalog_error(exc) from exc


@app.post("/catalog/briefs", dependencies=[Depends(require_token)])
def catalog_briefs(request: BriefRequest, http_request: Request) -> list[dict[str, Any]]:
    """Обложка, логотип и «о чём» для карточек ленты — пачкой.

    С версии 1.4 — ещё и картинки TMDB: логотип-название, если у Кинопоиска
    его нет, и `art_url` — постер без надписей для слайда hero приложения.

    Args:
        request: Идентификаторы Кинопоиска; читаются первые
            `meta_service.MAX_BRIEF_IDS`, лишние просто не попадают в ответ.

    Returns:
        Расширенные карточки с `detailed: true` в порядке запроса; тайтлов,
        которых каталог не отдал, в ответе нет.
    """
    if not META.available:
        raise HTTPException(status_code=503, detail="KINOPOISK_API_KEY не настроен")
    try:
        cards = META.briefs(request.ids)
    except Exception as exc:
        raise catalog_error(exc) from exc
    return [absolute_art(card, http_request) for card in cards]


def absolute_art(card: dict[str, Any], request: Request) -> dict[str, Any]:
    """Пути картинок, которые сервер раздаёт сам, — полным адресом.

    Адрес берётся из самого запроса: за nginx это `https://<хост>/` — nginx
    передаёт `Host` и `X-Forwarded-Proto`, а uvicorn верит им с 127.0.0.1.
    """
    base = str(request.base_url).rstrip("/")
    for key in ("logo_url", "art_url"):
        value = card.get(key)
        if isinstance(value, str) and value.startswith("/catalog/art/"):
            card[key] = base + value
    return card


@app.get("/catalog/art/{size}/{name}")
def catalog_art(size: str, name: str) -> FileResponse:
    """Логотип или постер TMDB, скачанный сервером для `/catalog/briefs`.

    Без токена: это картинка, как постер на CDN Кинопоиска, и загрузчик
    картинок приложения заголовков не шлёт. Раздаётся только то, что сервер
    скачал сам (`MetaService.art_file`), — по чужому имени ответ 404.
    """
    path = META.art_file(size, name)
    if path is None:
        raise HTTPException(status_code=404, detail="Картинки нет")
    media_type = "image/png" if name.endswith(".png") else "image/jpeg"
    return FileResponse(path, media_type=media_type, headers={"Cache-Control": "public, max-age=2592000"})


@app.get("/catalog/{kp_id}/episodes", dependencies=[Depends(require_token)])
def catalog_episodes(kp_id: int, season: int | None = None) -> list[dict[str, Any]]:
    """Серии тайтла. То же, что `/meta/{film_id}/episodes`; старый путь остаётся."""
    return meta_episodes(kp_id, season)


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
    """Серии тайтла: русские названия, описания и кадры (если настроен TMDB).

    У серий невышедшего сезона — `released: false`; у остальных поля нет.
    """
    if not META.available:
        raise HTTPException(status_code=503, detail="KINOPOISK_API_KEY не настроен")
    try:
        return [episode_payload(item) for item in META.episodes(film_id, season)]
    except Exception as exc:
        raise HTTPException(
            status_code=502, detail=f"Каталог метаданных недоступен: {exc}"
        ) from exc


def episode_payload(episode: meta_service.EpisodeMeta) -> dict[str, Any]:
    """Серия для ответа: `released` только у невышедшего сезона, как у `SeasonItem`.

    Args:
        episode: Серия из `MetaService.episodes`.

    Returns:
        Поля серии; `released` есть лишь со значением False.
    """
    payload = dataclasses.asdict(episode)
    if payload.get("released") is None:
        payload.pop("released", None)
    return payload


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
    except bot.MagnetRedirect:
        raise HTTPException(
            status_code=502, detail="У этой раздачи нет .torrent, только magnet. Обновите поиск и выберите её снова."
        ) from None
    except Exception as exc:
        LOG.warning("Не получилось скачать .torrent %s: %s", release_id, type(exc).__name__)
        raise HTTPException(
            status_code=502, detail="Не получилось скачать .torrent у источника. Выберите другую раздачу."
        ) from exc
    return Response(content=payload, media_type="application/x-bittorrent")
