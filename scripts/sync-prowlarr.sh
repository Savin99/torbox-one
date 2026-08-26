#!/usr/bin/env bash
set -Eeuo pipefail

# Keep the generated Prowlarr API key out of terminal output and shell history.
set +x
umask 077

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd -- "$SCRIPT_DIR/.." && pwd)"
ENV_FILE="${BOT_ENV_FILE:-$PROJECT_DIR/.env}"
PROWLARR_CONFIG="${PROWLARR_CONFIG_FILE:-$PROJECT_DIR/prowlarr-config/config.xml}"
PROWLARR_ADDRESS="${PROWLARR_ADDRESS:-http://127.0.0.1:9696}"

die() {
    printf 'Error: %s\n' "$*" >&2
    exit 1
}

[[ -f "$PROWLARR_CONFIG" ]] || die "Prowlarr configuration not found: $PROWLARR_CONFIG"
[[ "$ENV_FILE" != "$PROJECT_DIR/.env.example" ]] || die 'refusing to modify .env.example'

if ! api_key="$(python3 - "$PROWLARR_CONFIG" 2>/dev/null <<'PY'
import sys
import xml.etree.ElementTree as ET

value = ET.parse(sys.argv[1]).getroot().findtext("ApiKey", "").strip()
if not value:
    raise SystemExit(1)
print(value, end="")
PY
)"; then
    die 'Prowlarr API key could not be read'
fi

[[ ${#api_key} -ge 16 && "$api_key" != *[[:space:]]* ]] || die 'Prowlarr API key is invalid'

base_file="$ENV_FILE"
[[ -f "$base_file" ]] || base_file="$PROJECT_DIR/.env.example"
[[ -f "$base_file" ]] || die 'environment template not found'

temporary="$(mktemp "${ENV_FILE}.tmp.XXXXXX")"
cleanup() {
    [[ -z "${temporary:-}" || ! -e "$temporary" ]] || rm -f -- "$temporary"
}
trap cleanup EXIT HUP INT TERM

seen_url=false
seen_key=false
while IFS= read -r line || [[ -n "$line" ]]; do
    case "$line" in
        PROWLARR_URL=*)
            printf 'PROWLARR_URL=%s\n' "$PROWLARR_ADDRESS" >> "$temporary"
            seen_url=true
            ;;
        PROWLARR_API_KEY=*)
            printf 'PROWLARR_API_KEY=%s\n' "$api_key" >> "$temporary"
            seen_key=true
            ;;
        *)
            printf '%s\n' "$line" >> "$temporary"
            ;;
    esac
done < "$base_file"

$seen_url || printf 'PROWLARR_URL=%s\n' "$PROWLARR_ADDRESS" >> "$temporary"
$seen_key || printf 'PROWLARR_API_KEY=%s\n' "$api_key" >> "$temporary"

chmod 0600 "$temporary"
mv -f -- "$temporary" "$ENV_FILE"
temporary=''
trap - EXIT HUP INT TERM

printf 'Prowlarr connection saved to %s (API key was not displayed).\n' "$ENV_FILE"
