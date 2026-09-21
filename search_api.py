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
import dataclasses
import hashlib
import html
import logging
import os
import re
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from typing import Any
from urllib.parse import urljoin

# Логгер настраивается до импорта бота: torbox_tg_bot вызывает
# logging.basicConfig на импорте и иначе перехватил бы конфигурацию uvicorn.
logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)

from fastapi import Depends, FastAPI, Header, HTTPException, Response
from pydantic import BaseModel, Field

import meta_service
import torbox_tg_bot as bot

LOG = logging.getLogger("search_api")

API_VERSION = "1.1"
API_TOKEN = os.getenv("SEARCH_API_TOKEN", "").strip()
RESULTS_TTL_SECONDS = 3600
MAX_APP_RESULTS = bot.MAX_RESULTS

# Восемь результатов хватает короткому списку в Telegram, но мало клиенту,
# который ранжирует сам: ему нужны кандидаты, а не уже выбранная тройка.
MAX_SEARCH_LIMIT = 40
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


def stable_release_id(release: bot.Release) -> str:
    """Постоянный идентификатор релиза вида «провайдер:тема».

    uuid4 из прежней версии жил час в памяти процесса, и сохранённая в
    телефоне привязка «тайтл → раздача» превращалась в 404.

    Args:
        release: Релиз от любого источника.

    Returns:
        Строку без пробелов, пригодную как сегмент пути URL.
    """
    if release.provider == bot.NnmClient.key:
        topic = re.sub(r"\D", "", release.topic_id)
        if topic:
            return f"{bot.NnmClient.key}:{topic}"
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
        "magnet_url": release.magnet_url,
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
    if provider is None or provider_key != bot.NnmClient.key or not rest.isdigit():
        # У Prowlarr нет запроса «отдай релиз по guid»: его релизы
        # восстанавливаются только из дискового хранилища ReleaseCache
        return None
    try:
        return restore_nnm_topic(provider, rest)
    except Exception:
        LOG.warning("Не получилось восстановить тему NNM %s", rest, exc_info=True)
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


def release_info_hash(release_id: str, release: bot.Release) -> str | None:
    """Считает info_hash релиза, скачивая `.torrent`, и кэширует результат.

    NNM-Club не отдаёт в выдаче ни magnet, ни хэш, а без хэша приложение не
    может спросить TorBox, лежит ли раздача в кэше. Считать хэш на поиске
    нельзя — это десятки лишних запросов к трекеру, поэтому он считается
    лениво и только для тех релизов, о которых спросил клиент.

    Args:
        release_id: Стабильный идентификатор релиза.
        release: Релиз, для которого нужен хэш.

    Returns:
        hex40 в нижнем регистре или None, если источник хэш не отдал.
    """
    cache_key = f"hash:{release_id}"
    cached = HASH_CACHE.get(cache_key)
    if isinstance(cached, str) and cached:
        return cached

    known = normalized_info_hash(release.info_hash) or normalized_info_hash(
        bot.magnet_info_hash(release.magnet_url)
    )
    if known:
        HASH_CACHE.put(cache_key, known)
        return known

    provider = PROVIDER_BY_KEY.get(release.provider)
    if provider is None:
        return None
    payload = provider.download_torrent(release)
    value = bot.torrent_fingerprint(payload).casefold()
    if not INFO_HASH_RE.match(value):
        return None
    HASH_CACHE.put(cache_key, value)
    return value


def collect_info_hashes(ids: list[str], deadline: float) -> dict[str, str | None]:
    """Считает хэши пачки релизов параллельно, с общим сроком.

    Отказ одного релиза не мешает остальным: у него в ответе будет None.
    Неудачи не кэшируются — иначе разовая ошибка трекера держалась бы сутки.

    Args:
        ids: Стабильные идентификаторы релизов.
        deadline: Общий срок на всю пачку, в секундах.

    Returns:
        Словарь «идентификатор → hex40 или None».
    """
    result: dict[str, str | None] = {release_id: None for release_id in ids}
    pool = ThreadPoolExecutor(max_workers=max(1, HASH_WORKERS), thread_name_prefix="hash")
    futures: dict[Future[str | None], str] = {}
    for release_id in ids:
        release = CACHE.get(release_id) or restore_release(release_id)
        if release is None:
            LOG.info("Релиз %s не найден, хэш не считаю", release_id)
            continue
        futures[pool.submit(release_info_hash, release_id, release)] = release_id
    try:
        for future in as_completed(futures, timeout=max(1.0, deadline)):
            release_id = futures[future]
            try:
                result[release_id] = future.result()
            except Exception as exc:
                LOG.warning("Хэш релиза %s не посчитан: %s", release_id, exc)
    except TimeoutError:
        LOG.warning("Хэши: не успели к сроку %s с, отдаю что посчитали", deadline)
    finally:
        # Ждать отставших нельзя: срок общий, и ответ уходит сейчас
        pool.shutdown(wait=False, cancel_futures=True)
    return result


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
        for query in queries:
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
    except TimeoutError:
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


class HashResult(BaseModel):
    """Ответ `/release/hashes`: хэш или None на каждый обработанный релиз."""

    hashes: dict[str, str | None] = Field(default_factory=dict)
    limit: int = DEFAULT_HASH_IDS


class SeasonItem(BaseModel):
    """Сезон и число серий в нём."""

    season: int
    episodes: int


class TitleCardResult(BaseModel):
    """Ответ `/catalog/title/{kp_id}`: карточка плюс внешние id и сезоны."""

    meta: dict[str, Any]
    tmdb_id: int = 0
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

    releases, status, sources, partial = aggregate_releases(list(PROVIDERS), queries, deadline)
    # Два прохода разнообразия — как у бота: сначала широкий срез кандидатов,
    # потом короткий список. Для limit <= 24 результат тот же, что прежде.
    candidates = bot.select_diverse_releases(releases, max(bot.MAX_SEARCH_CANDIDATES, wanted))
    top = bot.select_diverse_releases(candidates, wanted)
    stored = CACHE.put_all(top)
    LOG.info("Поиск %s: %d релизов, статус %s", queries, len(stored), status)
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
        TitleMeta, идентификаторы TMDB и IMDb и состав сезонов. tmdb_id
        Кинопоиск не отдаёт ни одним эндпоинтом — он ищется в самом TMDB
        по оригинальному названию и году, и только для сериалов.
    """
    if not META.available:
        raise HTTPException(status_code=503, detail="KINOPOISK_API_KEY не настроен")
    try:
        card = META.title_card(kp_id)
    except Exception as exc:
        raise catalog_error(exc) from exc
    return TitleCardResult(
        meta=dataclasses.asdict(card.meta),
        tmdb_id=card.tmdb_id,
        imdb_id=card.imdb_id,
        seasons=[SeasonItem(season=item.season, episodes=item.episodes) for item in card.seasons],
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
