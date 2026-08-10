#!/bin/bash
#
# Заморозить рабочее состояние.  ./сохранить.sh  [подпись]
#
# Проверяет тесты, кладёт всё в git, ставит именную метку и отправляет
# на GitHub. Метка — это точка, куда сервер сможет вернуться одной
# командой, даже через полгода и в другом разговоре.
#
# Если тесты красные — не делает ничего.

cd "$(dirname "$0")" || exit 1

PY=./venv/bin/python
[ -x "$PY" ] || PY=python3

# Библиотеки доставляем молча — см. пояснение в проверить.sh.
$PY -m pip install -q -r requirements.txt 2>/dev/null

echo "Проверяю перед сохранением…"
if ! FIXBOT_TESTING=1 $PY -m pytest -q --tb=line > /tmp/fixbot-tests.txt 2>&1; then
    grep -E "^(FAILED|ERROR)" /tmp/fixbot-tests.txt | head -20
    tail -3 /tmp/fixbot-tests.txt
    echo
    echo "❌  Красное. Ничего не сохраняю — иначе метка будет врать."
    if grep -q "fixture .* not found" /tmp/fixbot-tests.txt; then
        echo
        echo "Похоже, дело не в коде: тестам не хватает библиотеки."
        echo "Выполните и попробуйте снова:"
        echo "    $PY -m pip install -r requirements.txt"
    fi
    exit 1
fi
tail -1 /tmp/fixbot-tests.txt

NOTE="${1:-рабочее состояние}"
TAG="ok-$(date '+%Y-%m-%d-%H%M')"

git add -A
if git diff --cached --quiet; then
    echo "Новых правок нет — ставлю метку на текущее состояние."
else
    git commit -q -m "$NOTE" || exit 1
    echo "Записал: $NOTE"
fi

git tag -a "$TAG" -m "$NOTE (тесты зелёные)" || exit 1
git push -q origin main --follow-tags || {
    echo "⚠️  Локально сохранено, но на GitHub не ушло. Проверьте интернет и ключ."
    exit 1
}

echo
echo "✅  Заморожено под меткой:  $TAG"
echo "    Вернуться к ней на сервере:  ./откатить.sh $TAG"
