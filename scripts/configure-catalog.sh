#!/usr/bin/env bash
set -Eeuo pipefail

set +x
umask 077

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd -- "$SCRIPT_DIR/.." && pwd)"
ENV_FILE="${BOT_ENV_FILE:-$PROJECT_DIR/.env}"

if (($# == 2)) && [[ "$1" == "--output" || "$1" == "-o" ]]; then
    ENV_FILE="$2"
elif (($#)); then
    printf 'Usage: %s [--output PATH]\n' "$0" >&2
    exit 2
fi

[[ "$ENV_FILE" != "$PROJECT_DIR/.env.example" ]] || {
    printf 'Refusing to modify .env.example\n' >&2
    exit 1
}

printf 'Kinopoisk API key (input hidden): ' >&2
IFS= read -r -s api_key
printf '\n' >&2
[[ ${#api_key} -ge 16 && "$api_key" != *[[:space:]]* ]] || {
    printf 'API key must contain at least 16 characters and no spaces.\n' >&2
    exit 1
}

base_file="$ENV_FILE"
[[ -f "$base_file" ]] || base_file="$PROJECT_DIR/.env.example"
[[ -f "$base_file" ]] || {
    printf 'Environment template was not found.\n' >&2
    exit 1
}

temporary="$(mktemp "${ENV_FILE}.tmp.XXXXXX")"
cleanup() {
    [[ -z "${temporary:-}" || ! -e "$temporary" ]] || rm -f -- "$temporary"
}
trap cleanup EXIT HUP INT TERM

seen=false
while IFS= read -r line || [[ -n "$line" ]]; do
    case "$line" in
        KINOPOISK_API_KEY=*)
            printf 'KINOPOISK_API_KEY=%s\n' "$api_key" >> "$temporary"
            seen=true
            ;;
        *)
            printf '%s\n' "$line" >> "$temporary"
            ;;
    esac
done < "$base_file"
$seen || printf 'KINOPOISK_API_KEY=%s\n' "$api_key" >> "$temporary"

chmod 0600 "$temporary"
mv -f -- "$temporary" "$ENV_FILE"
temporary=''
trap - EXIT HUP INT TERM

printf 'Catalog key saved to %s (the key was not displayed).\n' "$ENV_FILE"
