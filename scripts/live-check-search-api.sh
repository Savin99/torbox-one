#!/usr/bin/env bash
# Живая проверка контракта сервера поиска: каждый эндпоинт по разу на
# безобидном примере, печатаются ФОРМЫ ответов (имена полей и типы), а не
# значения. Результат сверяется с docs/search-api.md — файл писался по коду,
# и расхождение означает ошибку в нём.
#
#   ./scripts/live-check-search-api.sh                  # поднять сервер локально
#   SEARCH_API_BASE_URL=https://example.org \
#   SEARCH_API_TOKEN=... ./scripts/live-check-search-api.sh
#
# Скрипт ходит к настоящим трекерам, Кинопоиску и TMDB: это десяток запросов
# к каталогу из суточной квоты и несколько к трекерам. Токен берётся из
# окружения и никогда не печатается.
set -Eeuo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd -- "$SCRIPT_DIR/.." && pwd)"
ENV_FILE="${BOT_ENV_FILE:-$PROJECT_DIR/.env}"

command -v jq >/dev/null || { printf 'Нужен jq: brew install jq\n' >&2; exit 1; }

# Токен только из окружения; если его нет — забираем ровно одну строку из
# .env, не выполняя файл целиком.
if [[ -z "${SEARCH_API_TOKEN:-}" && -f "$ENV_FILE" ]]; then
    SEARCH_API_TOKEN="$(sed -n 's/^[[:space:]]*SEARCH_API_TOKEN=//p' "$ENV_FILE" | tail -1 | tr -d "\"'" || true)"
fi
TOKEN="${SEARCH_API_TOKEN:-}"

BASE_URL="${SEARCH_API_BASE_URL:-}"
SERVER_PID=""
LAST_BODY=""
# Журнал сервера может содержать адреса с ключами и cookies трекера:
# на экран его выводить только через redact, наружу — не выкладывать.
LOG_FILE="${SEARCH_API_CHECK_LOG:-/tmp/live-check-search-api.log}"

cleanup() {
    [[ -n "$SERVER_PID" ]] && kill "$SERVER_PID" 2>/dev/null || true
}
trap cleanup EXIT

if [[ -z "$BASE_URL" ]]; then
    PORT="${SEARCH_API_PORT:-8787}"
    # Чужой процесс на этом порту не трогаем: проверка не вправе его гасить
    if lsof -nP -iTCP:"$PORT" -sTCP:LISTEN >/dev/null 2>&1; then
        printf 'Порт %s занят чужим процессом. Задай свободный: SEARCH_API_PORT=8799\n' "$PORT" >&2
        exit 1
    fi
    BASE_URL="http://127.0.0.1:$PORT"
    printf 'Поднимаю сервер локально на %s\n' "$BASE_URL"
    "$SCRIPT_DIR/run-search-api.sh" >"$LOG_FILE" 2>&1 &
    SERVER_PID=$!
    for _ in $(seq 1 40); do
        curl -fsS --max-time 2 "$BASE_URL/health" >/dev/null 2>&1 && break
        sleep 0.5
    done
    curl -fsS --max-time 2 "$BASE_URL/health" >/dev/null 2>&1 || {
        printf 'Сервер не поднялся, лог: %s\n' "$LOG_FILE" >&2
        exit 1
    }
fi

# Вырезаем всё, что похоже на ключ или токен, ДО любого показа на экран.
redact() {
    sed -E \
        -e 's/(token|api_key|apikey|api-key|key)=[^&"[:space:]]+/\1=<вырезано>/gI' \
        -e 's/(Bearer )[A-Za-z0-9._-]+/\1<вырезано>/g' \
        -e 's/\b[A-Fa-f0-9]{32}\b/<вырезано>/g'
}

# Форма ответа: структура с типами вместо значений. Значения не печатаются
# вовсе — так из ответа не утечёт ни ссылка, ни ключ.
SHAPE_FILTER='def shape:
  if type == "object" then with_entries(.value |= shape)
  elif type == "array" then (if length == 0 then [] else [.[0] | shape] end)
  else type end;
shape'

FAILED=0

call() {
    local method="$1" path="$2" body="${3:-}"
    local out status
    printf '\n=== %s %s\n' "$method" "$path"
    if [[ "$method" == "POST" ]]; then
        out="$(curl -sS -m 90 -w '\n%{http_code}' -X POST \
            -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
            -d "$body" "$BASE_URL$path" 2>&1 || true)"
    else
        out="$(curl -sS -m 90 -w '\n%{http_code}' \
            -H "Authorization: Bearer $TOKEN" "$BASE_URL$path" 2>&1 || true)"
    fi
    status="$(printf '%s' "$out" | tail -1)"
    LAST_BODY="$(printf '%s' "$out" | sed '$d')"
    printf 'HTTP %s\n' "$status"
    if [[ "$status" != 200 ]]; then
        FAILED=$((FAILED + 1))
        printf '%s\n' "$LAST_BODY" | redact | head -5
        return 0
    fi
    printf '%s' "$LAST_BODY" | jq -e . >/dev/null 2>&1 || {
        printf 'ответ не JSON (%s байт)\n' "${#LAST_BODY}"
        return 0
    }
    printf '%s' "$LAST_BODY" | jq "$SHAPE_FILTER" | redact
}

pick() {
    printf '%s' "$LAST_BODY" | jq -r "$1" 2>/dev/null || printf ''
}

printf '# Проверка связи\n'
call GET /health
call GET /health/auth

printf '\n# Каталог\n'
call GET /catalog/quota
printf '\nОстаток квоты: '
printf '%s' "$LAST_BODY" | jq -c '{daily: .dailyQuota, total: .totalQuota, account: .accountType}' 2>/dev/null || printf 'не разобрать\n'

call GET /catalog/popular
call GET '/catalog/search?q=%D0%94%D1%8E%D0%BD%D0%B0'
call GET '/catalog/shelf/popular'
call GET '/catalog/shelf/top_movies'
call GET '/catalog/shelf/top_series'
SERIES_ID="$(pick '.[0].catalog_id')"
call GET '/catalog/shelf/premieres'
call GET '/catalog/shelf/genre/%D0%B4%D1%80%D0%B0%D0%BC%D0%B0'
call GET '/catalog/shelf/year/1999'
call GET '/catalog/shelf/%D0%BD%D0%B5%D1%82-%D1%82%D0%B0%D0%BA%D0%BE%D0%B9'

if [[ -n "$SERIES_ID" && "$SERIES_ID" != "null" ]]; then
    printf '\n# Сериал из топа: %s\n' "$SERIES_ID"
    call GET "/catalog/title/$SERIES_ID"
    TMDB_ID="$(pick '.tmdb_id')"
    SEASONS="$(pick '.seasons | length')"
    printf 'tmdb_id: %s, сезонов: %s\n' "$TMDB_ID" "$SEASONS"
    call GET "/catalog/$SERIES_ID/episodes?season=1"
    call GET "/meta/$SERIES_ID/episodes?season=1"
fi

call GET '/meta/match?name=%D0%98%D0%B3%D1%80%D0%B0%20%D0%9F%D1%80%D0%B5%D1%81%D1%82%D0%BE%D0%BB%D0%BE%D0%B2%20%2F%20Game%20of%20Thrones%20(%D0%A1%D0%B5%D0%B7%D0%BE%D0%BD%201)%20%5B2011%2C%20BDRip%201080p%5D'

printf '\n# Поиск: прежний вызов\n'
call GET '/search?q=%D0%98%D0%BD%D1%82%D0%B5%D1%80%D1%81%D1%82%D0%B5%D0%BB%D0%BB%D0%B0%D1%80%202014'
printf 'Старые поля на месте: '
# Пустая выдача — это не «полей нет»: проверять просто не на чем, и путать
# одно с другим нельзя. Живая проверка 21.09.2026 споткнулась ровно об это.
printf '%s' "$LAST_BODY" | jq -r '
  ["id","title","size_text","size_bytes","seeders","leechers","source_name","category",
   "magnet_url","info_hash","quality_summary","voiceover_summary","source_url"] as $old
  | if (.results | length) == 0
    then "проверять не на чем: выдача пуста, источники — \(.status)"
    else ($old - (.results[0] | keys_unsorted))
         | if length == 0 then "да" else "НЕТ, потерялись: \(.)" end
    end' 2>/dev/null || printf 'не разобрать\n'

printf '\n# Поиск: вызов приложения\n'
if [[ -n "$SERIES_ID" && "$SERIES_ID" != "null" ]]; then
    call GET "/search?kp_id=$SERIES_ID&kind=series&season=1&limit=20&deadline=25"
else
    call GET '/search?q=%D0%98%D0%BD%D1%82%D0%B5%D1%80%D1%81%D1%82%D0%B5%D0%BB%D0%BB%D0%B0%D1%80%202014&limit=20&deadline=25'
fi
printf 'partial: %s, источники: ' "$(pick '.partial')"
printf '%s' "$LAST_BODY" | jq -c '.status' 2>/dev/null || printf '\n'
printf 'Без info_hash в выдаче: %s из %s\n' \
    "$(pick '[.results[] | select(.info_hash == "")] | length')" "$(pick '.results | length')"
IDS="$(printf '%s' "$LAST_BODY" | jq -c '[.results[].id][:4]' 2>/dev/null || printf '[]')"
FIRST_ID="$(pick '.results[0].id')"

printf '\n# Короткий срок на настоящем трекере\n'
call GET '/search?q=%D0%98%D0%BD%D1%82%D0%B5%D1%80%D1%81%D1%82%D0%B5%D0%BB%D0%BB%D0%B0%D1%80%202014&deadline=0.3'
printf 'partial: %s, источники: ' "$(pick '.partial')"
printf '%s' "$LAST_BODY" | jq -c '.status' 2>/dev/null || printf '\n'

printf '\n# Ленивые хэши для первых четырёх\n'
call POST /release/hashes "{\"ids\": $IDS, \"limit\": 4, \"deadline\": 45}"
printf 'Посчитано: %s из %s\n' \
    "$(pick '[.hashes[] | select(. != null)] | length')" "$(pick '.hashes | length')"
printf 'Все сорок символов нижнего регистра: %s\n' \
    "$(pick '[.hashes[] | select(. != null) | test("^[0-9a-f]{40}$")] | all')"

if [[ -n "$FIRST_ID" && "$FIRST_ID" != "null" ]]; then
    printf '\n# Релиз %s\n' "$FIRST_ID"
    call GET "/release/$FIRST_ID/details"

    printf '\n=== GET /release/%s/torrent\n' "$FIRST_ID"
    TORRENT="$(mktemp)"
    CODE="$(curl -sS -m 90 -o "$TORRENT" -w '%{http_code}' \
        -H "Authorization: Bearer $TOKEN" "$BASE_URL/release/$FIRST_ID/torrent" || true)"
    printf 'HTTP %s, байт: %s, начинается с bencode-словаря: %s\n' \
        "$CODE" "$(wc -c <"$TORRENT" | tr -d ' ')" \
        "$([[ "$(head -c 1 "$TORRENT")" == d ]] && echo да || echo НЕТ)"
    rm -f "$TORRENT"

    printf '\n# Тот же релиз после перезапуска сервера\n'
    if [[ -n "$SERVER_PID" ]]; then
        kill "$SERVER_PID" 2>/dev/null || true
        wait "$SERVER_PID" 2>/dev/null || true
        "$SCRIPT_DIR/run-search-api.sh" >>"$LOG_FILE" 2>&1 &
        SERVER_PID=$!
        for _ in $(seq 1 40); do
            curl -fsS --max-time 2 "$BASE_URL/health" >/dev/null 2>&1 && break
            sleep 0.5
        done
        call GET "/release/$FIRST_ID/details"
        printf 'Идентификатор пережил перезапуск: %s\n' \
            "$([[ -n "$(pick '.quality_summary')" ]] && echo да || echo НЕТ)"
    else
        printf 'пропущено: сервер не наш\n'
    fi
fi

printf '\n# Ошибки\n'
call GET '/search?q=a'
call GET '/release/prowlarr:nosuch:0000000000000000/torrent'

printf '\nГотово. Эндпоинтов с кодом не 200 (кроме ожидаемых ошибок в конце): %s\n' "$FAILED"
printf 'Сверь формы выше с docs/search-api.md и почини документ, если он врёт.\n'
