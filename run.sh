#!/bin/bash
#
# Запуск бота с автоперезапуском.
#
#   ./run.sh
#
# Зачем: когда Mac засыпает, соединение с Telegram рвётся, и процесс
# часто не оживает после пробуждения. Этот скрипт следит за ним и
# поднимает заново, если он выпал.
#
# Что скрипт НЕ умеет: пережить закрытую крышку. Пока ноутбук спит,
# спит и бот — фиксации в это время не обрабатываются. Лечится только
# переездом на сервер.
#
# Остановить: Ctrl+C в этом окне, либо  pkill -f "python3 bot.py"

cd "$(dirname "$0")" || exit 1
source venv/bin/activate || { echo "Нет venv. Выполните: python3 -m venv venv"; exit 1; }

# Не даём системе засыпать, пока ноутбук просто стоит открытым.
# Крышку это не переживёт, но от простоя спасает.
if command -v caffeinate >/dev/null; then
    CAFFEINATE="caffeinate -i"
else
    CAFFEINATE=""
fi

echo "Бот запущен. Логи пишутся в bot.log, остановить — Ctrl+C."
echo

trap 'echo; echo "Останавливаю…"; pkill -f "python3 bot.py"; exit 0' INT TERM

while true; do
    echo "$(date '+%Y-%m-%d %H:%M:%S') — старт" | tee -a bot.log
    $CAFFEINATE python3 bot.py 2>&1 | tee -a bot.log

    code=${PIPESTATUS[0]}
    echo "$(date '+%Y-%m-%d %H:%M:%S') — бот остановился (код $code)" | tee -a bot.log

    # Пауза, чтобы при устойчивой ошибке не крутиться в цикле
    # и не засыпать лог одинаковыми строками.
    sleep 5
done
