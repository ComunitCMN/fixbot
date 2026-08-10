#!/bin/bash
#
# Вернуть сервер назад.  /opt/fixbot/app/откатить.sh  [метка]
#
#   ./откатить.sh              — на предыдущее состояние (шаг назад)
#   ./откатить.sh ok-2026-08-10-1744   — на конкретную метку
#   ./откатить.sh --список     — какие метки вообще есть
#
# Это красная кнопка. Жать можно смело: код в git никуда не девается,
# и вернуться вперёд всегда получится тем же способом.
#
# Данные скрипт не трогает вообще. Базы, настройки, фиксации, привязки
# чатов остаются как есть — меняется только код.
#
# Про двух пользователей — пояснение в обновить.sh: git от владельца
# папки, systemctl от root.

set -u
APP=/opt/fixbot/app
cd "$APP" || { echo "Нет $APP"; exit 1; }

SUDO=sudo
[ "$(id -u)" -eq 0 ] && SUDO=""
PY=./venv/bin/python

OWNER=$(stat -c '%U' "$APP/.git" 2>/dev/null)
ME=$(id -un)

as_owner() {
    if [ -z "$OWNER" ] || [ "$OWNER" = "$ME" ]; then
        "$@"
    else
        sudo -u "$OWNER" -H "$@"
    fi
}

if ! as_owner git rev-parse --git-dir > /dev/null 2>&1; then
    echo "❌  Git не может работать с $APP."
    echo
    echo "    Папка принадлежит пользователю ${OWNER:-неизвестно}, запущено от $ME."
    echo "    Если это не ошибка, выполните один раз:"
    echo
    echo "        git config --global --add safe.directory $APP"
    exit 1
fi

as_owner git fetch --all --tags -q 2>/dev/null

if [ "${1:-}" = "--список" ] || [ "${1:-}" = "--list" ]; then
    echo "Метки рабочих состояний, свежие сверху:"
    as_owner git for-each-ref --sort=-creatordate --count=15 \
        --format='  %(refname:short)   %(creatordate:short)   %(contents:subject)' \
        refs/tags
    exit 0
fi

TARGET="${1:-HEAD~1}"

if ! as_owner git rev-parse --verify -q "$TARGET" > /dev/null; then
    echo "❌  Не знаю такого места: $TARGET"
    echo "    Список меток:  ./откатить.sh --список"
    exit 1
fi

echo "Сейчас:  $(as_owner git log -1 --format='%s')"
echo "Вернуть: $(as_owner git log -1 --format='%s' "$TARGET")"
echo
read -rp "Возвращаю? [y/N] " ANSWER
case "$ANSWER" in
    y|Y) ;;
    *) echo "Отменено."; exit 0 ;;
esac

as_owner git reset --hard "$TARGET" -q || exit 1
as_owner $PY -m pip install -q -r requirements.txt 2>/dev/null

UNITS=(fixbot-operator)
for d in /opt/fixbot/clients/*/; do
    [ -d "$d" ] && UNITS+=("fixbot@$(basename "$d")")
done
for s in "${UNITS[@]}"; do
    $SUDO systemctl restart "$s" 2>/dev/null
done

sleep 15
DEAD=()
for s in "${UNITS[@]}"; do
    systemctl is-active --quiet "$s" || DEAD+=("$s")
done

echo
if [ ${#DEAD[@]} -eq 0 ]; then
    echo "✅  Вернул. Боты работают на:  $(as_owner git log -1 --format='%s')"
else
    echo "🚨  Вернул код, но не поднялись: ${DEAD[*]}"
    echo "    Смотреть:  journalctl -u ${DEAD[0]} -n 50"
fi
