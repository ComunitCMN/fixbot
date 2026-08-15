"""
Скрипты выкатки и отката.

Владелец не читает код: для него `обновить.sh` и `откатить.sh` — это
две кнопки. Кнопка со опечаткой в bash молча ничего не сделает или,
хуже, сделает половину — обновит код и не перезапустит ботов. Поэтому
их проверяем как обычный код.
"""

import pathlib
import re
import subprocess

import pytest

ROOT = pathlib.Path(__file__).parent
СКРИПТЫ = sorted(ROOT.glob("*.sh"))


def текст(имя):
    return (ROOT / имя).read_text(encoding="utf-8")


def код(имя):
    """Только исполняемые строки: без комментариев и подсказок на экран."""
    строки = []
    for s in текст(имя).splitlines():
        без = s.split("#", 1)[0] if not s.lstrip().startswith("#") else ""
        строки.append(без)
    return "\n".join(строки)


def test_scripts_are_found():
    """Если glob вдруг пустой, все проверки ниже станут бесполезны."""
    assert len(СКРИПТЫ) >= 6


@pytest.mark.parametrize("script", СКРИПТЫ, ids=lambda p: p.name)
def test_script_has_no_syntax_errors(script):
    r = subprocess.run(["bash", "-n", str(script)],
                       capture_output=True, text=True)
    assert r.returncode == 0, f"{script.name}: {r.stderr}"


@pytest.mark.parametrize("script", СКРИПТЫ, ids=lambda p: p.name)
def test_script_is_executable(script):
    """Иначе владелец получит «permission denied» и решит, что всё сломалось."""
    assert script.stat().st_mode & 0o111, f"нужно: chmod +x {script.name}"


@pytest.mark.parametrize("script", СКРИПТЫ, ids=lambda p: p.name)
def test_no_bash4_only_features(script):
    """
    На сервере bash 5, а на маке владельца — 3.2: Apple не обновляет его
    с 2007 года из-за лицензии. Ассоциативный массив (`declare -A`) там
    не работает вовсе, и скрипт молча начинает считать не то.

    Так и вышло 15.08.2026: проверка логов прошла у меня и на сервере,
    а на маке легла. Пишем на том bash, который есть у обоих.
    """
    плохие = []
    for n, s in enumerate(текст(script.name).splitlines(), 1):
        if s.lstrip().startswith("#"):
            continue
        тело = s.split("#", 1)[0]
        for приём, чем in (
            (r"declare\s+-A", "ассоциативный массив"),
            (r"\blocal\s+-A", "ассоциативный массив"),
            (r"\$\{[A-Za-z_][A-Za-z_0-9]*\^\^", "${имя^^} — верхний регистр"),
            (r"\$\{[A-Za-z_][A-Za-z_0-9]*,,", "${имя,,} — нижний регистр"),
            (r"\breadarray\b|\bmapfile\b", "mapfile/readarray"),
        ):
            if re.search(приём, тело):
                плохие.append(f"{n}: {чем} — {s.strip()}")
    assert плохие == [], (f"{script.name}: bash 3.2 на маке этого не умеет\n"
                          + "\n".join(плохие))


@pytest.mark.parametrize("script", СКРИПТЫ, ids=lambda p: p.name)
def test_no_cyrillic_identifiers(script):
    """
    Соблазн назвать переменную по-русски велик — документация проекта
    вся на русском. Но bash кириллицу в именах не принимает: строку
    `МЕТКА="ok"` он читает как попытку запустить программу с таким
    именем и на ходу говорит «command not found». Скрипт при этом
    не падает, а просто делает не то. Один раз так и вышло.
    """
    плохие = []
    for n, s in enumerate(текст(script.name).splitlines(), 1):
        if s.lstrip().startswith("#"):
            continue
        тело = s.split("#", 1)[0]
        if re.search(r"^\s*[А-Яа-яЁё_]*[А-Яа-яЁё][А-Яа-яЁё_0-9]*\s*=", тело):
            плохие.append(f"{n}: {s.strip()}")
        if re.search(r"^\s*[А-Яа-яЁё_]+\s*\(\)", тело):
            плохие.append(f"{n}: {s.strip()}")
    assert плохие == [], f"{script.name}: имена латиницей\n" + "\n".join(плохие)


# ===================== выкатка =====================

def test_update_tests_before_restarting_bots():
    """
    Смысл скрипта в этом порядке: сначала тесты, и только потом ботов
    трогаем. Наоборот — значит красный код успел доехать до живых чатов.
    """
    src = текст("обновить.sh")
    assert src.index("pytest") < src.index("restart_all\nsleep")


def test_update_rolls_back_on_red_tests():
    src = текст("обновить.sh")
    красное = src.split("Проверяю тестами", 1)[1].split("# --- 4.", 1)[0]
    assert "rollback" in красное


def test_update_checks_bots_are_alive_after_restart():
    """
    Тесты зелёные, а бот падает на старте — так уже было (код дописали
    после блока запуска). Значит проверять надо не тестами, а тем,
    поднялась ли служба.
    """
    src = текст("обновить.sh")
    хвост = src.split("# --- 5.", 1)[1]
    assert "dead_units" in хвост and "rollback" in хвост


def test_update_notices_a_traceback_in_the_log():
    """Служба с Restart=always выглядит живой, даже падая по кругу."""
    src = текст("обновить.sh")
    assert "Traceback" in src and "NameError" in src


# ---------- проверка логов после перезапуска ----------
#
# 15.08.2026 выкатка откатилась впустую: в хвосте лога breig лежала
# трассировка с прошлого запуска — пойманный `httpx.ReadError` из первой
# синхронизации, который `bot.py` намеренно ловит и пишет через
# `log.exception`. Бот при этом поднялся и работал. Проверка смотрела
# последние 40 строк без учёта времени и не различала «упал» и «поймал».
#
# Поэтому ниже она проверяется не чтением текста, а прогоном: блок
# из `обновить.sh` запускается на выдуманных логах.

МАРКЕР_НАЧАЛО = "# <<< проверка логов"
МАРКЕР_КОНЕЦ = "# >>> проверка логов"

СТАРТ = ("2026-08-15 10:54:02,679 INFO fixbot: Вебхук снят\n"
         "2026-08-15 10:54:02,679 INFO aiogram.dispatcher: Start polling\n"
         "2026-08-15 10:54:02,685 INFO aiogram.dispatcher: Run polling "
         "for bot @test id=1 - 'test'\n")

ТРАССИРОВКА = ("Traceback (most recent call last):\n"
               '  File "/opt/fixbot/app/amo.py", line 156, in dump_leads\n'
               "httpx.ReadError\n")


def _блок_проверки_логов() -> str:
    src = текст("обновить.sh")
    assert МАРКЕР_НАЧАЛО in src and МАРКЕР_КОНЕЦ in src, (
        "в обновить.sh нет размеченного блока проверки логов — "
        "его нельзя прогнать, можно только прочитать глазами")
    return src.split(МАРКЕР_НАЧАЛО, 1)[1].split(МАРКЕР_КОНЕЦ, 1)[0]


def _откат_бы_случился(tmp_path, было: str, стало: str) -> bool:
    """
    Гоняет настоящий блок из `обновить.sh` на выдуманном логе.

    `было` — что лежало в логе до перезапуска, `стало` — что дописалось
    после. Возвращает True, если скрипт счёл это падением и откатился бы.
    """
    root = tmp_path / "fixbot"
    (root / "app").mkdir(parents=True)
    (root / "clients" / "breig").mkdir(parents=True)
    лог = root / "clients" / "breig" / "bot.log"
    лог.write_text(было, encoding="utf-8")
    (root / "app" / "bot.log").write_text(СТАРТ, encoding="utf-8")

    дописать = tmp_path / "after.txt"
    дописать.write_text(стало, encoding="utf-8")

    harness = tmp_path / "harness.sh"
    harness.write_text(
        "set -u\n"
        f'export FIXBOT_LOGS_ROOT="{root}"\n'
        + _блок_проверки_логов()
        + "\nsnapshot_logs\n"
        f'cat "{дописать}" >> "{лог}"\n'
        "if crash_in_logs > /dev/null; then echo ОТКАТ; else echo ЖИВОЙ; fi\n",
        encoding="utf-8")

    r = subprocess.run(["bash", str(harness)], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    return "ОТКАТ" in r.stdout


def test_old_traceback_does_not_cancel_the_update(tmp_path):
    """
    Ровно случай 15.08.2026. Трассировка осталась с прошлого запуска,
    после перезапуска бот поднялся — откатывать нечего. Лог у клиента
    редкий, поэтому старый след живёт в хвосте неделями.
    """
    было = ТРАССИРОВКА + СТАРТ
    assert not _откат_бы_случился(tmp_path, было, стало=СТАРТ)


def test_caught_error_at_startup_does_not_cancel_the_update(tmp_path):
    """
    Ошибку связи с amoCRM при первой синхронизации `bot.py` ловит
    намеренно и пишет через `log.exception` — слово `Traceback`
    в логе появляется по замыслу. Бот после неё стартует, и это видно:
    строка про запуск идёт следом.
    """
    assert not _откат_бы_случился(tmp_path, было=СТАРТ,
                                  стало=ТРАССИРОВКА + СТАРТ)


def test_real_crash_still_cancels_the_update(tmp_path):
    """
    Обратная сторона: настоящее падение обязано откатывать. У него
    трассировка последняя — запуска за ней нет, потому что его не было.
    """
    падение = ("Traceback (most recent call last):\n"
               '  File "/opt/fixbot/app/bot.py", line 1, in <module>\n'
               "ImportError: cannot import name 'exp'\n")
    assert _откат_бы_случился(tmp_path, было=СТАРТ, стало=падение * 3)


def test_untouched_log_is_not_read_at_all(tmp_path):
    """
    Смотреть надо только то, что дописалось после перезапуска. Иначе
    любая старая беда в файле отменяет любую будущую выкатку.
    """
    assert not _откат_бы_случился(tmp_path, было=ТРАССИРОВКА, стало="")


@pytest.mark.parametrize("script", ["обновить.sh", "откатить.sh"])
def test_git_runs_as_the_owner_of_the_repository(script):
    """
    На сервере заходят под root, а код и ключ к GitHub принадлежат
    пользователю fixbot. Root получает «Permission denied (publickey)»
    и «dubious ownership»: обновиться он не может физически.
    Поэтому git, pip и тесты идут от владельца папки.
    """
    src = код(script)
    assert "as_owner" in src
    assert "stat -c '%U'" in src
    for cmd in ("git fetch", "git rev-parse HEAD", "git reset --hard",
                "pip install"):
        if cmd in src:
            for line in src.splitlines():
                if cmd in line and "as_owner" not in line:
                    assert False, f"{script}: {line.strip()} — без as_owner"


@pytest.mark.parametrize("script", ["обновить.sh", "откатить.sh"])
def test_owner_keeps_his_own_home(script):
    """
    Без -H у sudo домашняя папка остаётся root-овской, ssh идёт искать
    ключ в /root/.ssh — а он лежит в /home/fixbot/.ssh. Внешне это
    выглядит как «git молчит и ничего не забирает».
    """
    src = код(script)
    assert 'sudo -u "$OWNER" -H' in src


def test_update_restarts_services_as_root_not_as_owner():
    """
    Наоборот тоже нельзя: у fixbot нет права трогать fixbot-operator —
    в sudoers разрешён только шаблон fixbot@*.
    """
    src = код("обновить.sh")
    for line in src.splitlines():
        if "systemctl restart" in line:
            assert "as_owner" not in line, line.strip()


@pytest.mark.parametrize("script", ["обновить.sh", "откатить.sh"])
def test_git_refusal_is_explained(script):
    """
    Папка на сервере принадлежит fixbot, заходят под root — git объявляет
    репозиторий подозрительным и отказывается работать. Скрипт при этом
    молча доходил до конца, ничего не сделав: худший исход из возможных,
    потому что выглядит как успех. Проверяем доступность git до всего
    остального и объясняем, что выполнить.
    """
    src = текст(script)
    assert "rev-parse --git-dir" in src
    assert "safe.directory" in src
    assert src.index("rev-parse --git-dir") < src.index("git fetch")


def test_update_refuses_when_the_server_was_edited_by_hand():
    """
    Правки руками на сервере — прямой запрет в CLAUDE.md. Молча затирать
    их нельзя: это чья-то работа, и о ней надо сказать.
    """
    src = текст("обновить.sh")
    assert "--ff-only" in src


def test_update_covers_the_operator_bot_too():
    """Забыть про пульт легко: он не подходит под шаблон fixbot@*."""
    src = текст("обновить.sh")
    assert "fixbot-operator" in src
    assert "fixbot@$(basename" in src


# ===================== откат =====================

def test_rollback_never_touches_data():
    """
    Самое страшное, что может сделать откат, — снести боевые базы.
    Ни одного разрушительного слова про данные в нём быть не должно.
    """
    src = код("откатить.sh")
    for опасное in ("rm ", "DROP ", "fixbot.db", ".env",
                    "clients/*/fixbot", "git clean", "--prune"):
        assert опасное not in src, f"откат не должен трогать {опасное!r}"


def test_rollback_asks_before_acting():
    src = текст("откатить.sh")
    assert src.index("read -rp") < src.index("git reset")


def test_rollback_can_list_the_saved_states():
    """Без списка меток человек не знает, куда возвращаться."""
    src = текст("откатить.sh")
    assert "--список" in src and "refs/tags" in src


def test_rollback_restarts_after_changing_code():
    src = текст("откатить.sh")
    assert src.index("git reset") < src.index("systemctl restart")


# ===================== заморозка =====================

def test_save_refuses_on_red_tests():
    """
    Метка означает «здесь точно работало». Метка на красном коде хуже
    отсутствия меток: на неё откатятся в трудную минуту.
    """
    src = текст("сохранить.sh")
    красное = src.split("pytest", 1)[1].split("git add", 1)[0]
    assert "exit 1" in красное


def test_save_tags_are_sortable_by_name():
    """Метки должны выстраиваться по времени сами, без разбора дат."""
    src = текст("сохранить.sh")
    m = re.search(r'TAG="([^"]+)"', src)
    assert m and "%Y-%m-%d" in m.group(1)


def test_save_pushes_tags_to_github():
    """Метка только на маке не спасёт, если мак утонет."""
    src = текст("сохранить.sh")
    assert "--follow-tags" in src


# ===================== проверка на маке =====================

def test_check_script_speaks_plainly():
    """Владелец не читает вывод pytest — ему нужно одно слово."""
    src = текст("проверить.sh")
    assert "ВСЁ ЗЕЛЁНОЕ" in src and "ЕСТЬ ПАДЕНИЯ" in src


def test_check_script_shows_what_failed():
    src = текст("проверить.sh")
    assert "FAILED" in src


@pytest.mark.parametrize("script", ["проверить.sh", "сохранить.sh",
                                    "обновить.sh"])
def test_dependencies_are_installed_before_the_tests(script):
    """
    Худший из возможных ответов — «красное», когда код в порядке, а в venv
    просто не хватает библиотеки из requirements.txt. Так и вышло:
    pytest-aiohttp добавили в список уже после того, как venv на маке
    собрали, и 14 тестов перестали запускаться.
    """
    src = код(script)
    assert "pip install" in src
    assert src.index("pip install") < src.index("pytest")


@pytest.mark.parametrize("script", ["проверить.sh", "сохранить.sh"])
def test_missing_library_is_explained_not_just_shown(script):
    """Владельцу нужно не «ERROR», а строка, которую можно выполнить."""
    src = текст(script)
    assert "fixture .* not found" in src
    assert "pip install -r requirements.txt" in src


@pytest.mark.parametrize("script", ["проверить.sh", "сохранить.sh",
                                    "обновить.sh"])
def test_tests_run_in_testing_mode(script):
    """
    Без FIXBOT_TESTING тесты заводят настоящие службы systemd — так уже
    случилось на сервере, остались юниты fixbot@romashka и fixbot@dup.
    """
    src = текст(script)
    assert "FIXBOT_TESTING=1" in src
