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

# <<< проверка логов
#
# Свежие падения пишет сам Python при старте. Служба с Restart=always
# при этом выглядит живой: падает и поднимается по кругу, а systemctl
# показывает «active». Поэтому лог смотреть надо — но правильно.
#
# Раньше здесь было `tail -40 | grep Traceback`, и 15.08.2026 это
# отменило исправную выкатку: в хвосте лога лежал след с прошлого
# запуска. Две ошибки сразу:
#
#   * времени проверка не знала — старая беда отменяла любую будущую
#     выкатку, а у редко пишущего клиента она живёт в хвосте неделями;
#   * «упал» и «поймал» не различались. `bot.py` намеренно ловит сбой
#     первой синхронизации и пишет его через `log.exception` — слово
#     `Traceback` в логе появляется по замыслу, бот при этом работает.
#
# Теперь читаем только то, что дописалось после перезапуска, и считаем
# падением лишь то, за чем не последовал успешный запуск.

LOGS_ROOT=${FIXBOT_LOGS_ROOT:-/opt/fixbot}

#: Строка, которую aiogram пишет, когда бот действительно поднялся.
ALIVE_MARK="Run polling"
BAD_MARKS="Traceback|NameError|ImportError|SyntaxError"

# Снимок длин логов держим в файле, а не в ассоциативном массиве:
# `declare -A` — это bash 4, а на маке владельца bash 3.2, и там такой
# массив молча превращается в обычный. Файл понимают обе версии.
LOG_SIZES=""

log_files() {
    local f
    for f in "$LOGS_ROOT"/app/bot.log "$LOGS_ROOT"/clients/*/bot.log; do
        [ -f "$f" ] && echo "$f"
    done
    return 0
}

#: Строк в файле. `tr` убирает отступ, которым wc сопровождает число на маке.
count_lines() {
    wc -l < "$1" | tr -d ' '
}

snapshot_logs() {
    LOG_SIZES=$(mktemp "${TMPDIR:-/tmp}/fixbot-logs.XXXXXX")
    local f
    while read -r f; do
        printf '%s\t%s\n' "$(count_lines "$f")" "$f" >> "$LOG_SIZES"
    done < <(log_files)
}

#: Сколько строк было в этом логе до перезапуска. Путь сверяем целиком —
#: в нём бывают пробелы.
lines_before() {
    [ -n "$LOG_SIZES" ] && [ -f "$LOG_SIZES" ] || { echo 0; return 0; }
    awk -F '\t' -v p="$1" '$2 == p { n = $1 } END { print n + 0 }' "$LOG_SIZES"
}

crash_in_logs() {
    local f now from fresh last_bad last_ok
    while read -r f; do
        now=$(count_lines "$f")
        from=$(lines_before "$f")
        # Лог могли обрезать или заменить при ротации — тогда он весь новый.
        [ "$now" -lt "$from" ] && from=0

        fresh=$(tail -n "+$((from + 1))" "$f")
        [ -n "$fresh" ] || continue

        last_bad=$(printf '%s\n' "$fresh" | grep -nE "$BAD_MARKS" \
                   | tail -1 | cut -d: -f1)
        [ -n "$last_bad" ] || continue

        # Пойманная ошибка — не падение: за ней идёт успешный запуск.
        # У настоящего падения трассировка последняя, запуска за ней нет.
        last_ok=$(printf '%s\n' "$fresh" | grep -n "$ALIVE_MARK" \
                  | tail -1 | cut -d: -f1)
        if [ -n "$last_ok" ] && [ "$last_ok" -gt "$last_bad" ]; then
            continue
        fi

        echo "$f"
        return 0
    done < <(log_files)
    return 1
}
# >>> проверка логов

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
# Запоминаем длину логов до перезапуска: смотреть будем только то,
# что дописалось после него.
snapshot_logs
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
