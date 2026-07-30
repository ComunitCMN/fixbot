#!/bin/bash
#
# Раскладывает архив с ноутбука по местам на сервере.
#
#   sudo /opt/fixbot/app/restore-on-server.sh ~/fixbot-переезд-*.tar.gz
#
# Архив собирается на маке скриптом pack-for-server.sh.
#
# ## Что здесь происходит
#
# Настройки и базы просто копируются, но пути внутри `.env` переписываются:
# на маке они вида `/Users/имя/Desktop/clients/...`, а на сервере такой
# папки нет. Если этого не сделать, бот при запуске создаст пустую базу
# по несуществующему пути — и молча начнёт жизнь с чистого листа, считая
# всех знакомых клиентов новыми.
#
# Скрипт ничего не перезаписывает без спроса: если настройки на месте
# уже есть, он остановится и скажет об этом.

set -e

ARCHIVE="$1"
APP=/opt/fixbot/app
CLIENTS=/opt/fixbot/clients
USER_NAME=fixbot

if [ -z "$ARCHIVE" ] || [ ! -f "$ARCHIVE" ]; then
    echo "Укажите архив. Например:"
    echo "  sudo $APP/restore-on-server.sh /root/fixbot-переезд-2026-07-30-1633.tar.gz"
    exit 1
fi

[ "$(id -u)" = "0" ] || { echo "Запускать от root: sudo $0 $ARCHIVE"; exit 1; }

TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT

case "$ARCHIVE" in
    *.enc)
        # Ежедневные копии зашифрованы: внутри телефоны клиентов и токены
        # к чужим CRM. Пароль лежит в менеджере паролей, не на сервере.
        if [ -z "${BACKUP_PASSPHRASE:-}" ]; then
            read -r -s -p "Пароль от копии: " BACKUP_PASSPHRASE; echo
            export BACKUP_PASSPHRASE
        fi
        openssl enc -d -aes-256-cbc -pbkdf2 -iter 200000 \
            -in "$ARCHIVE" -pass env:BACKUP_PASSPHRASE 2>/dev/null \
            | tar -xzf - -C "$TMP" \
            || { echo "Не открылось. Неверный пароль или файл повреждён."; exit 1; }
        ;;
    *)
        tar -xzf "$ARCHIVE" -C "$TMP"
        ;;
esac

ROOT=$(find "$TMP" -maxdepth 1 -mindepth 1 -type d | head -1)

# Переписывает путь в настройках: ключ, новое значение, файл.
set_env() {
    local key="$1" value="$2" file="$3"
    if grep -q "^$key=" "$file"; then
        sed -i "s|^$key=.*|$key=\"$value\"|" "$file"
    else
        printf '%s="%s"\n' "$key" "$value" >> "$file"
    fi
}

# --- оператор --------------------------------------------------------------
if [ -f "$ROOT/operator/.env" ]; then
    [ -f "$APP/.env" ] && { echo "На сервере уже есть $APP/.env — уберите его и повторите"; exit 1; }

    cp "$ROOT/operator/.env" "$APP/.env"
    set_env DB_PATH "$APP/fixbot.db" "$APP/.env"
    set_env CLIENTS_DIR "$CLIENTS" "$APP/.env"
    chown $USER_NAME:$USER_NAME "$APP/.env"
    chmod 600 "$APP/.env"
    echo "Оператор: настройки на месте, пути поправлены"

    if [ -f "$ROOT/operator/fixbot.db" ]; then
        cp "$ROOT/operator/fixbot.db" "$APP/fixbot.db"
        chown $USER_NAME:$USER_NAME "$APP/fixbot.db"
        echo "   база: фиксаций $(sqlite3 "$APP/fixbot.db" 'SELECT COUNT(*) FROM fixations' 2>/dev/null || echo '?')"
    fi
fi

# --- клиенты ---------------------------------------------------------------
if [ -d "$ROOT/clients" ]; then
    for dir in "$ROOT"/clients/*/; do
        [ -d "$dir" ] || continue
        slug=$(basename "$dir")
        dest="$CLIENTS/$slug"

        [ -f "$dest/.env" ] && { echo "Клиент $slug уже есть — пропускаю"; continue; }

        mkdir -p "$dest"
        cp "$dir/.env" "$dest/.env"
        set_env DB_PATH "$dest/fixbot.db" "$dest/.env"
        [ -f "$dir/fixbot.db" ] && cp "$dir/fixbot.db" "$dest/fixbot.db"

        chown -R $USER_NAME:$USER_NAME "$dest"
        chmod 600 "$dest/.env"
        name=$(grep '^DEVELOPER_NAME=' "$dest/.env" | tail -1 | cut -d= -f2- | tr -d '"')
        echo "Клиент $slug — ${name:-без названия}: настройки и база на месте"
    done
fi

echo
echo "──────────────────────────────────────────────"
echo "Готово. Дальше:"
echo "  1. Погасите ботов на ноутбуке — иначе они будут"
echo "     перехватывать сообщения у сервера."
echo "  2. Поднимите службы (см. СЕРВЕР.md, раздел «Служба systemd»)."
echo
echo "Архив с токенами удалите: rm \"$ARCHIVE\""
