#!/usr/bin/env bash
set -Eeuo pipefail

# Never echo secrets, even if this script was started from a shell with xtrace.
set +x
umask 077

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd -- "$SCRIPT_DIR/.." && pwd)"
ENV_FILE="$PROJECT_DIR/.env"

usage() {
    cat <<'EOF'
Usage: ./scripts/configure-credentials.sh [--output PATH]

Interactively creates or updates a protected environment file.
Secrets are read without echoing and are never printed afterward.

Options:
  -o, --output PATH  Write to PATH instead of the project .env file
  -h, --help         Show this help
EOF
}

die() {
    printf 'Error: %s\n' "$*" >&2
    exit 1
}

while (($#)); do
    case "$1" in
        -o|--output)
            (($# >= 2)) || die "$1 requires a path"
            ENV_FILE="$2"
            shift 2
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        *)
            die "unknown option: $1"
            ;;
    esac
done

[[ "$ENV_FILE" != "$PROJECT_DIR/.env.example" ]] || \
    die 'refusing to put real credentials into .env.example'
[[ -d "$(dirname -- "$ENV_FILE")" ]] || \
    die "parent directory does not exist: $(dirname -- "$ENV_FILE")"
[[ ! -d "$ENV_FILE" ]] || die "output path is a directory: $ENV_FILE"

read_existing_value() {
    local key="$1"
    local file="$2"
    local line

    [[ -f "$file" ]] || return 0
    while IFS= read -r line || [[ -n "$line" ]]; do
        case "$line" in
            "$key="*)
                printf '%s' "${line#*=}"
                return 0
                ;;
        esac
    done < "$file"
}

is_placeholder() {
    [[ -z "$1" || "$1" == "replace_me" || "$1" == "CHANGE_ME" ]]
}

prompt_secret() {
    local label="$1"
    local current="$2"
    local validator="$3"
    local value

    while true; do
        if is_placeholder "$current"; then
            printf '%s: ' "$label" >&2
        else
            printf '%s (Enter = keep current): ' "$label" >&2
        fi

        if ! IFS= read -r -s value; then
            printf '\n' >&2
            die 'input ended before configuration was complete'
        fi
        printf '\n' >&2

        if [[ -z "$value" ]] && ! is_placeholder "$current"; then
            value="$current"
        fi
        if "$validator" "$value"; then
            REPLY="$value"
            return 0
        fi
    done
}

valid_telegram_token() {
    local value="$1"
    if [[ "$value" =~ ^[0-9]+:[A-Za-z0-9_-]+$ ]] && ((${#value} >= 20)); then
        return 0
    fi
    printf 'Telegram token must look like 123456:ABC...\n' >&2
    return 1
}

valid_torbox_key() {
    local value="$1"
    if ((${#value} >= 16)) && [[ ! "$value" =~ [[:space:]] ]]; then
        return 0
    fi
    printf 'TorBox API key must contain at least 16 characters and no spaces.\n' >&2
    return 1
}

valid_owner_id() {
    local value="$1"
    if [[ "$value" =~ ^[1-9][0-9]*$ ]]; then
        return 0
    fi
    printf 'Owner Telegram ID must be a positive number.\n' >&2
    return 1
}

existing_telegram="$(read_existing_value TELEGRAM_BOT_TOKEN "$ENV_FILE")"
existing_torbox="$(read_existing_value TORBOX_API_KEY "$ENV_FILE")"
existing_owner="$(read_existing_value OWNER_TELEGRAM_ID "$ENV_FILE")"

printf 'Credentials will be saved to %s with permissions 0600.\n' "$ENV_FILE" >&2
prompt_secret 'Telegram bot token' "$existing_telegram" valid_telegram_token
telegram_token="$REPLY"
prompt_secret 'TorBox API key' "$existing_torbox" valid_torbox_key
torbox_api_key="$REPLY"

while true; do
    if is_placeholder "$existing_owner"; then
        printf 'Owner Telegram ID: ' >&2
    else
        printf 'Owner Telegram ID [%s]: ' "$existing_owner" >&2
    fi
    if ! IFS= read -r owner_id; then
        die 'input ended before configuration was complete'
    fi
    [[ -n "$owner_id" ]] || owner_id="$existing_owner"
    if valid_owner_id "$owner_id"; then
        break
    fi
done

template="$PROJECT_DIR/.env.example"
base_file="$template"
[[ -f "$ENV_FILE" ]] && base_file="$ENV_FILE"
[[ -f "$base_file" ]] || die "configuration template not found: $template"

temporary="$(mktemp "${ENV_FILE}.tmp.XXXXXX")"
cleanup() {
    [[ -z "${temporary:-}" || ! -e "$temporary" ]] || rm -f -- "$temporary"
}
trap cleanup EXIT HUP INT TERM

seen_telegram=false
seen_torbox=false
seen_owner=false
while IFS= read -r line || [[ -n "$line" ]]; do
    case "$line" in
        TELEGRAM_BOT_TOKEN=*)
            printf 'TELEGRAM_BOT_TOKEN=%s\n' "$telegram_token" >> "$temporary"
            seen_telegram=true
            ;;
        TORBOX_API_KEY=*)
            printf 'TORBOX_API_KEY=%s\n' "$torbox_api_key" >> "$temporary"
            seen_torbox=true
            ;;
        OWNER_TELEGRAM_ID=*)
            printf 'OWNER_TELEGRAM_ID=%s\n' "$owner_id" >> "$temporary"
            seen_owner=true
            ;;
        *)
            printf '%s\n' "$line" >> "$temporary"
            ;;
    esac
done < "$base_file"

$seen_telegram || printf 'TELEGRAM_BOT_TOKEN=%s\n' "$telegram_token" >> "$temporary"
$seen_torbox || printf 'TORBOX_API_KEY=%s\n' "$torbox_api_key" >> "$temporary"
$seen_owner || printf 'OWNER_TELEGRAM_ID=%s\n' "$owner_id" >> "$temporary"

chmod 0600 "$temporary"
mv -f -- "$temporary" "$ENV_FILE"
temporary=''
trap - EXIT HUP INT TERM

printf 'Done. Credentials saved to %s (secrets were not displayed).\n' "$ENV_FILE"

