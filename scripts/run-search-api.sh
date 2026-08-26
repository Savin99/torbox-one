#!/usr/bin/env bash
set -Eeuo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd -- "$SCRIPT_DIR/.." && pwd)"
ENV_FILE="${BOT_ENV_FILE:-$PROJECT_DIR/.env}"

if [[ -f "$ENV_FILE" ]]; then
    # .env совместим с shell; экспортируем значения только этому процессу
    set -a
    # shellcheck disable=SC1090
    source "$ENV_FILE"
    set +a
fi

HOST="${SEARCH_API_HOST:-127.0.0.1}"
PORT="${SEARCH_API_PORT:-8787}"

if [[ -z "${SEARCH_API_TOKEN:-}" ]]; then
    printf 'ВНИМАНИЕ: SEARCH_API_TOKEN не задан — авторизация выключена (только для разработки).\n' >&2
fi

exec "$PROJECT_DIR/.venv/bin/uvicorn" search_api:app --host "$HOST" --port "$PORT" --app-dir "$PROJECT_DIR"
