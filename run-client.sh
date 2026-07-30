#!/bin/bash
#
# Запуск бота отдельного клиента на своём компьютере.
#
#   ./run-client.sh eco-invest-group
#
# Нужен только на маке, где нет служб systemd. На сервере то же самое
# делает systemctl — см. СЕРВЕР.md.
#
# Код общий, у каждого клиента своя папка с настройками и своей базой.
# Так данные клиентов не смешиваются: у каждого свой справочник агентств,
# реестр агентов и журнал фиксаций.

set -e

APP="$(cd "$(dirname "$0")" && pwd)"
SLUG="$1"

if [ -z "$SLUG" ]; then
    echo "Укажите папку клиента. Например:"
    echo "  ./run-client.sh eco-invest-group"
    echo
    echo "Доступные клиенты:"
    CLIENTS="${CLIENTS_DIR:-$HOME/Desktop/clients}"
    ls -1 "$CLIENTS" 2>/dev/null | sed 's/^/  /' || echo "  (папка $CLIENTS пуста)"
    exit 1
fi

# Папку клиентов берём из настроек оператора, иначе — привычное место.
CLIENTS="${CLIENTS_DIR:-$HOME/Desktop/clients}"
[ -f "$APP/.env" ] && CLIENTS=$(grep '^CLIENTS_DIR=' "$APP/.env" 2>/dev/null \
    | tail -1 | cut -d= -f2- | tr -d '"' || echo "$CLIENTS")
CLIENTS="${CLIENTS:-$HOME/Desktop/clients}"

DIR="$CLIENTS/$SLUG"
if [ ! -f "$DIR/.env" ]; then
    echo "Не нашёл настройки: $DIR/.env"
    exit 1
fi

source "$APP/venv/bin/activate"

echo "Клиент: $SLUG"
echo "Настройки: $DIR/.env"
echo "Логи: $DIR/bot.log — остановить Ctrl+C"
echo

trap 'echo; echo "Останавливаю…"; exit 0' INT TERM

while true; do
    # ENV_FILE говорит боту, чей именно .env читать. Без этого он взял бы
    # файл оператора — он лежит рядом с кодом, откуда бот и запускается.
    (
        cd "$APP"
        exec env ENV_FILE="$DIR/.env" python3 bot.py
    ) 2>&1 | tee -a "$DIR/bot.log"

    echo "$(date '+%H:%M:%S') — бот остановился, перезапуск через 5 сек"
    sleep 5
done
