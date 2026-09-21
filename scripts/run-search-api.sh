#!/usr/bin/env bash
set -Eeuo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd -- "$SCRIPT_DIR/.." && pwd)"
ENV_FILE="${BOT_ENV_FILE:-$PROJECT_DIR/.env}"

# Пути кэшей, заданные снаружи, переживают загрузку .env: иначе проверочный
# запуск незаметно пишет в рабочие файлы пользователя, а их там трогать нечем.
OUTER_STATE_DIR="${SEARCH_API_STATE_DIR:-}"
OUTER_META_CACHE="${META_CACHE_PATH:-}"
OUTER_SHELF_CACHE="${SHELF_CACHE_PATH:-}"

if [[ -f "$ENV_FILE" ]]; then
    # .env совместим с shell; экспортируем значения только этому процессу
    set -a
    # shellcheck disable=SC1090
    source "$ENV_FILE"
    set +a
fi

[[ -n "$OUTER_STATE_DIR" ]] && export SEARCH_API_STATE_DIR="$OUTER_STATE_DIR"
[[ -n "$OUTER_META_CACHE" ]] && export META_CACHE_PATH="$OUTER_META_CACHE"
[[ -n "$OUTER_SHELF_CACHE" ]] && export SHELF_CACHE_PATH="$OUTER_SHELF_CACHE"

HOST="${SEARCH_API_HOST:-127.0.0.1}"
PORT="${SEARCH_API_PORT:-8787}"

if [[ -z "${SEARCH_API_TOKEN:-}" ]]; then
    printf 'ВНИМАНИЕ: SEARCH_API_TOKEN не задан — авторизация выключена (только для разработки).\n' >&2
fi

exec "$PROJECT_DIR/.venv/bin/uvicorn" search_api:app --host "$HOST" --port "$PORT" --app-dir "$PROJECT_DIR"
