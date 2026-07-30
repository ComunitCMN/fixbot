#!/bin/bash
#
# Ежедневная копия всех данных, зашифрованная и увезённая с сервера.
#
#   /opt/fixbot/app/backup.sh
#
# Запускается по расписанию, руками трогать не нужно.
#
# ## Зачем увозить
#
# Копия на том же сервере спасает от «удалил не тот файл», но не спасает
# от потери сервера: кончились деньги, отключили аккаунт, отказал диск —
# и данные уходят вместе с копиями. Поэтому архив ещё и отправляется
# в Telegram: это бесплатно, отдельно от хостинга, и видно глазами,
# что копии действительно идут.
#
# ## Почему зашифровано
#
# Внутри телефоны клиентов застройщиков и токены доступа к их amoCRM.
# Такое нельзя класть в переписку как есть. Архив шифруется паролем
# из BACKUP_PASSPHRASE; без пароля файл бесполезен.
#
# ПАРОЛЬ ХРАНИТЕ ОТДЕЛЬНО ОТ СЕРВЕРА — в менеджере паролей. Потеряете
# и пароль, и сервер одновременно — восстанавливать будет нечего.
#
# ## Что внутри
#
# Ровно та же раскладка, что делает pack-for-server.sh: operator/ и
# clients/<имя>/. Это не совпадение — так восстановление идёт тем же
# скриптом restore-on-server.sh, который мы уже проверили на переезде.
# Один путь восстановления, а не два похожих.

set -e

APP=${FIXBOT_APP:-/opt/fixbot/app}
CLIENTS=${FIXBOT_CLIENTS:-/opt/fixbot/clients}
STORE=${FIXBOT_BACKUPS:-/opt/fixbot/backups}
KEEP_DAYS=${BACKUP_KEEP_DAYS:-14}
TG=${TELEGRAM_API:-https://api.telegram.org}

STAMP=$(date '+%Y-%m-%d-%H%M')
NAME="fixbot-$STAMP"

# --- настройки оператора ---------------------------------------------------
# shellcheck disable=SC1090
set -a; . "$APP/.env"; set +a

CHAT=$(echo "${OPERATOR_IDS:-}" | cut -d, -f1 | tr -d ' ')

say() {  # сообщение оператору в Telegram, если получится
    [ -n "${TELEGRAM_TOKEN:-}" ] && [ -n "$CHAT" ] || return 0
    curl -sS -m 30 -o /dev/null -X POST "$TG/bot$TELEGRAM_TOKEN/sendMessage" \
        -d chat_id="$CHAT" -d parse_mode=HTML -d text="$1" || true
}

fail() { say "❗️ <b>Копия не сделана</b>%0A$1"; echo "$1" >&2; exit 1; }

[ -n "${BACKUP_PASSPHRASE:-}" ] || fail "не задан BACKUP_PASSPHRASE в $APP/.env"
command -v sqlite3 >/dev/null || fail "нет sqlite3"
command -v openssl >/dev/null || fail "нет openssl"

mkdir -p "$STORE"
TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT
ROOT="$TMP/$NAME"
mkdir -p "$ROOT/operator"

# --- снимок базы -----------------------------------------------------------
# .backup, а не cp: база работает в режиме WAL, и часть свежих записей
# лежит в файле-спутнике рядом. Обычное копирование их потеряет молча.
snapshot() {
    [ -f "$1" ] || return 0
    sqlite3 "$1" ".backup '$2'"
}

total=0
count_fix() { sqlite3 "$1" "SELECT COUNT(*) FROM fixations" 2>/dev/null || echo 0; }

[ -f "$APP/.env" ] && cp "$APP/.env" "$ROOT/operator/.env"
if [ -f "$APP/fixbot.db" ]; then
    snapshot "$APP/fixbot.db" "$ROOT/operator/fixbot.db"
    total=$(( total + $(count_fix "$ROOT/operator/fixbot.db") ))
fi

lines=""
if [ -d "$CLIENTS" ]; then
    for dir in "$CLIENTS"/*/; do
        [ -f "$dir/.env" ] || continue
        slug=$(basename "$dir")
        mkdir -p "$ROOT/clients/$slug"
        cp "$dir/.env" "$ROOT/clients/$slug/.env"
        n=0
        if [ -f "$dir/fixbot.db" ]; then
            snapshot "$dir/fixbot.db" "$ROOT/clients/$slug/fixbot.db"
            n=$(count_fix "$ROOT/clients/$slug/fixbot.db")
            total=$(( total + n ))
        fi
        lines="$lines%0A• $slug — фиксаций $n"
    done
fi

# --- упаковка и шифрование -------------------------------------------------
ARCHIVE="$STORE/$NAME.tar.gz.enc"
tar -czf "$TMP/$NAME.tar.gz" -C "$TMP" "$NAME"
openssl enc -aes-256-cbc -pbkdf2 -iter 200000 -salt \
    -in "$TMP/$NAME.tar.gz" -out "$ARCHIVE" -pass env:BACKUP_PASSPHRASE \
    || fail "не удалось зашифровать"
chmod 600 "$ARCHIVE"

# Проверяем, что зашифрованное вообще читается обратно. Копия, которую
# нельзя открыть, хуже отсутствия копии: на неё рассчитываешь.
openssl enc -d -aes-256-cbc -pbkdf2 -iter 200000 \
    -in "$ARCHIVE" -pass env:BACKUP_PASSPHRASE 2>/dev/null \
    | tar -tzf - > /dev/null || fail "архив не открывается обратно"

SIZE=$(du -h "$ARCHIVE" | cut -f1)

# --- отправка --------------------------------------------------------------
sent="не отправлено"
if [ -n "${TELEGRAM_TOKEN:-}" ] && [ -n "$CHAT" ]; then
    if curl -sS -m 120 -o /dev/null -f -X POST \
        "$TG/bot$TELEGRAM_TOKEN/sendDocument" \
        -F chat_id="$CHAT" \
        -F document=@"$ARCHIVE" \
        -F caption="🗄 Копия $STAMP · фиксаций $total · $SIZE"; then
        sent="отправлено"
    else
        say "⚠️ Копия сделана, но не ушла в Telegram.%0AНа сервере она есть: <code>$ARCHIVE</code>"
    fi
fi

# --- уборка ----------------------------------------------------------------
find "$STORE" -maxdepth 1 -name 'fixbot-*.tar.gz.enc' -mtime +"$KEEP_DAYS" -delete

echo "$NAME  $SIZE  фиксаций $total  $sent"
