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
