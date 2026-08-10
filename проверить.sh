#!/bin/bash
#
# Проверка кода на маке.  ./проверить.sh
#
# Один вопрос — один ответ: можно ли это отдавать на сервер.
# Никаких кодов возврата и простыней вывода: зелено или красно.

cd "$(dirname "$0")" || exit 1

PY=./venv/bin/python
[ -x "$PY" ] || PY=python3

# Библиотеки доставляем молча. Иначе выходит худший из возможных
# результатов: код в порядке, а проверка красная — потому что в venv
# не хватает библиотеки, добавленной в requirements.txt позже.
# Один раз так и было: pytest-aiohttp приехал в список после того,
# как venv на маке уже собрали.
$PY -m pip install -q -r requirements.txt 2>/dev/null

echo "Проверяю… (полминуты)"
echo

OUT=$(FIXBOT_TESTING=1 $PY -m pytest -q --tb=line 2>&1)
CODE=$?

if [ $CODE -eq 0 ]; then
    echo "$OUT" | tail -3
    echo
    echo "✅  ВСЁ ЗЕЛЁНОЕ. Это состояние можно отдавать на сервер."
    echo "    Заморозить его:  ./сохранить.sh \"что сделали\""
    exit 0
fi

echo "$OUT" | grep -E "^(FAILED|ERROR)" | head -20
echo
echo "$OUT" | tail -3
echo
echo "❌  ЕСТЬ ПАДЕНИЯ. На сервер это не отдавать."

# Отдельный разбор для случая, когда дело не в коде, а в окружении:
# ошибка выглядит страшно, а лечится одной строкой.
if echo "$OUT" | grep -q "fixture .* not found"; then
    echo
    echo "Похоже, дело не в коде: тестам не хватает библиотеки."
    echo "Выполните и попробуйте снова:"
    echo "    $PY -m pip install -r requirements.txt"
else
    echo "    Скопируйте строки выше и покажите Claude."
fi
