#!/bin/bash
#
# Собирает всё, что нужно перевезти на сервер, в один архив.
#
#   ./pack-for-server.sh
#
# Код не трогаем — он на GitHub. Переезжают только данные:
#
#   * настройки: .env оператора и .env каждого клиента;
#   * базы: справочник агентств, реестр агентов, журнал фиксаций.
#
# ## Почему нельзя просто скопировать файл базы
#
# SQLite работает в режиме WAL: свежие записи какое-то время лежат
# не в самой базе, а в файле-спутнике `fixbot.db-wal` рядом. Обычный
# `cp` заберёт базу без спутника — и последние фиксации пропадут,
# причём молча. Поэтому здесь `.backup`: он делает согласованный
# снимок, дожидаясь, пока всё дописано.
#
# ## Осторожно с архивом
#
# Внутри — токены Telegram, amoCRM и Claude. Не выкладывайте его никуда,
# не отправляйте в мессенджерах и удалите с обоих компьютеров после
# переезда. Как это сделать, скрипт напомнит в конце.

set -e

APP="$(cd "$(dirname "$0")" && pwd)"
STAMP=$(date '+%Y-%m-%d-%H%M')
OUT="$HOME/Desktop/fixbot-переезд-$STAMP"

command -v sqlite3 >/dev/null || { echo "Нет sqlite3 — он есть в любой macOS, что-то не так с PATH"; exit 1; }

# Папку клиентов берём из настроек оператора.
CLIENTS=$(grep '^CLIENTS_DIR=' "$APP/.env" 2>/dev/null | tail -1 | cut -d= -f2- | tr -d '"')
CLIENTS="${CLIENTS:-$HOME/Desktop/clients}"

mkdir -p "$OUT/operator"

echo "Собираю в $OUT"
echo

# --- снимок базы -----------------------------------------------------------
# Аргументы: путь к базе, куда положить.
snapshot() {
    local src="$1" dst="$2"
    if [ ! -f "$src" ]; then
        echo "   базы нет — пропускаю (это нормально для нового клиента)"
        return
    fi
    # .backup дожидается согласованного состояния и забирает всё,
    # включая то, что ещё лежит в файле-спутнике.
    sqlite3 "$src" ".backup '$dst'"
    local rows
    rows=$(sqlite3 "$dst" "SELECT COUNT(*) FROM fixations" 2>/dev/null || echo "?")
    echo "   база: $(du -h "$dst" | cut -f1), фиксаций $rows"
}

# --- оператор --------------------------------------------------------------
echo "Оператор:"
cp "$APP/.env" "$OUT/operator/.env"
echo "   настройки скопированы"
snapshot "$APP/fixbot.db" "$OUT/operator/fixbot.db"
echo

# --- клиенты ---------------------------------------------------------------
if [ -d "$CLIENTS" ]; then
    for dir in "$CLIENTS"/*/; do
        [ -d "$dir" ] || continue
        slug=$(basename "$dir")
        [ -f "$dir/.env" ] || continue

        name=$(grep '^DEVELOPER_NAME=' "$dir/.env" | tail -1 | cut -d= -f2- | tr -d '"')
        echo "Клиент $slug — ${name:-без названия}:"

        mkdir -p "$OUT/clients/$slug"
        cp "$dir/.env" "$OUT/clients/$slug/.env"
        echo "   настройки скопированы"
        snapshot "$dir/fixbot.db" "$OUT/clients/$slug/fixbot.db"
        echo
    done
else
    echo "Папки клиентов нет ($CLIENTS) — переезжает только оператор."
    echo
fi

# --- архив -----------------------------------------------------------------
cd "$(dirname "$OUT")"
tar -czf "$OUT.tar.gz" "$(basename "$OUT")"
rm -rf "$OUT"
chmod 600 "$OUT.tar.gz"

echo "──────────────────────────────────────────────"
echo "Готово: $OUT.tar.gz  ($(du -h "$OUT.tar.gz" | cut -f1))"
echo
echo "Внутри токены. Никуда не выкладывайте и не пересылайте"
echo "мессенджерами — только напрямую на сервер."
echo
echo "Отправить (подставьте адрес сервера):"
echo "  scp \"$OUT.tar.gz\" root@АДРЕС:/root/"
echo
echo "После переезда удалить с ноутбука:"
echo "  rm \"$OUT.tar.gz\""
