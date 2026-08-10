#!/bin/bash
#
# Безопасное обновление сервера.  /opt/fixbot/app/обновить.sh
#
# Заменяет собой связку «git pull + restart». Разница в том, что этот
# скрипт умеет передумать: если после обновления тесты покраснели или
# хоть один бот не поднялся — он сам возвращает всё как было и говорит
# об этом словами.
#
# Порядок:
#   1. запоминает, на чём сервер стоит сейчас;
#   2. забирает новый код;
#   3. ставит зависимости;
#   4. гоняет тесты — красные, значит откат и боты даже не трогаются;
#   5. перезапускает ботов;
#   6. через 20 секунд смотрит, живы ли они и нет ли в логах падений;
#   7. если что-то мертво — откат и перезапуск на старом коде.
#
# Худший исход этого скрипта — «ничего не изменилось». Не «боты лежат».
#
# ## Два разных пользователя
#
# Заходят на сервер под root, а код, venv и ключ к GitHub принадлежат
# пользователю fixbot. Поэтому работа делится:
#   git, pip, тесты  — от имени владельца папки (иначе «Permission
#                      denied (publickey)» и root-овские файлы в venv);
#   systemctl        — от root.
# Флаг -H у sudo обязателен: без него ssh пойдёт искать ключ в /root/.ssh,
# где его нет.
#
# Имена переменных латиницей намеренно: кириллические bash не принимает
# вовсе — `ИМЯ=значение` он читает как попытку запустить программу.

set -u
APP=/opt/fixbot/app
cd "$APP" || { echo "Нет $APP"; exit 1; }

SUDO=sudo
[ "$(id -u)" -eq 0 ] && SUDO=""

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

PY=./venv/bin/python
UNITS=(fixbot-operator)
for d in /opt/fixbot/clients/*/; do
    [ -d "$d" ] && UNITS+=("fixbot@$(basename "$d")")
done

restart_all() {
    for s in "${UNITS[@]}"; do
        $SUDO systemctl restart "$s" 2>/dev/null
    done
}

dead_units() {
    local dead=()
    for s in "${UNITS[@]}"; do
        systemctl is-active --quiet "$s" || dead+=("$s")
    done
    [ ${#dead[@]} -eq 0 ] && return 1
    echo "${dead[*]}"
    return 0
}

crash_in_logs() {
    # Свежие падения — их пишет сам Python при старте. Служба с
    # Restart=always при этом выглядит живой: падает и поднимается
    # по кругу, а systemctl показывает «active».
    for f in /opt/fixbot/app/bot.log /opt/fixbot/clients/*/bot.log; do
        [ -f "$f" ] || continue
        if tail -40 "$f" | grep -qE "Traceback|NameError|ImportError|SyntaxError"; then
            echo "$f"
            return 0
        fi
    done
    return 1
}

WAS=$(as_owner git rev-parse HEAD)
WAS_TEXT=$(as_owner git log -1 --format='%s')

rollback() {
    echo
    echo "↩️  ВОЗВРАЩАЮ КАК БЫЛО: $WAS_TEXT"
    as_owner git reset --hard "$WAS" -q
    as_owner $PY -m pip install -q -r requirements.txt 2>/dev/null
    restart_all
    sleep 15
    if dead_units > /dev/null; then
        echo "🚨  ОТКАТИЛСЯ, НО БОТЫ НЕ ПОДНЯЛИСЬ. Это уже не про обновление."
        echo "    Смотреть:  journalctl -u fixbot-operator -n 50"
    else
        echo "✅  Откатился, боты работают на прежнем коде."
    fi
    exit 1
}

echo "Папка принадлежит:  $OWNER   (запущено от $ME)"
echo "Сейчас на сервере:  $WAS_TEXT"
echo "Службы:             ${UNITS[*]}"
echo

# --- 1. новый код -----------------------------------------------------
echo "Забираю код…"
if ! as_owner git fetch --all --tags -q; then
    echo "❌  Не смог достучаться до GitHub от имени $OWNER."
    echo "    Проверить ключ:  sudo -u $OWNER -H ssh -T git@github.com"
    exit 1
fi
if ! as_owner git pull --ff-only -q; then
    echo "❌  Не смог обновиться начисто. Похоже, на сервере правили руками."
    echo "    Посмотреть что:  sudo -u $OWNER -H git status"
    exit 1
fi
NOW=$(as_owner git rev-parse HEAD)

if [ "$WAS" = "$NOW" ]; then
    echo "Нового кода нет — сервер и так свежий. Ничего не делаю."
    exit 0
fi
echo "Приехало:  $(as_owner git log -1 --format='%s')"

# --- 2. зависимости ---------------------------------------------------
echo "Ставлю зависимости…"
as_owner $PY -m pip install -q -r requirements.txt || rollback

# --- 3. тесты ДО перезапуска -----------------------------------------
echo "Проверяю тестами…"
if ! as_owner env FIXBOT_TESTING=1 $PY -m pytest -q --tb=line > /tmp/fixbot-deploy.txt 2>&1; then
    grep -E "^(FAILED|ERROR)" /tmp/fixbot-deploy.txt | head -15
    tail -3 /tmp/fixbot-deploy.txt
    echo
    echo "❌  ТЕСТЫ КРАСНЫЕ. Ботов не трогал — они всё это время работали."
    rollback
fi
tail -1 /tmp/fixbot-deploy.txt

# --- 4. перезапуск ----------------------------------------------------
echo "Перезапускаю ботов…"
restart_all
sleep 20

# --- 5. живы ли -------------------------------------------------------
if DEAD=$(dead_units); then
    echo "❌  Не поднялись: $DEAD"
    rollback
fi
if CRASH=$(crash_in_logs); then
    echo "❌  В логе $CRASH — падение при старте."
    rollback
fi

echo
echo "✅  ОБНОВЛЕНО И РАБОТАЕТ."
echo
echo "Теперь проверьте руками, две минуты:"
echo "  1. напишите боту /start — открылось меню;"
echo "  2. /check и знакомый номер — пришёл вердикт;"
echo "  3. в рабочем чате имя и телефон — появились кнопки."
echo
echo "Что-то не так — вернуть прежнее:  ./откатить.sh"
