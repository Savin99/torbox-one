#!/usr/bin/env bash
set -Eeuo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd -- "$SCRIPT_DIR/.." && pwd)"
ENV_FILE="${BOT_ENV_FILE:-$PROJECT_DIR/.env}"

if [[ ! -f "$ENV_FILE" ]]; then
    printf 'Configuration not found: %s\n' "$ENV_FILE" >&2
    printf 'Run ./scripts/configure-credentials.sh first.\n' >&2
    exit 1
fi

# The generated .env is shell-compatible. Export its values only to the bot.
set -a
# shellcheck disable=SC1090
source "$ENV_FILE"
set +a

python="$PROJECT_DIR/.venv/bin/python"
if [[ ! -x "$python" ]]; then
    python="$(command -v python3 || true)"
fi
[[ -n "$python" ]] || {
    printf 'python3 was not found.\n' >&2
    exit 1
}

exec "$python" "$PROJECT_DIR/torbox_tg_bot.py"

