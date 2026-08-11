"""
Выбор агентской воронки, в которую бот кладёт новые фиксации.

У застройщика агентских воронок бывает несколько — раньше бот молча брал
первую по номеру, и оператору нечем было это изменить. Теперь в разделе
«Воронки» ставится отметка «сюда кладём», ровно одна на клиента.

Главное, что здесь проверяется: отметка меняет **только** место создания
новых сделок. Разметка kind не трогается совсем — поиск совпадений
по-прежнему идёт по всем агентским воронкам, иначе вердикты начнут врать.
"""

import inspect
import sqlite3

import pytest

from db import Db


def _three_pipelines(path):
    """Розничная и две агентских — как у настоящего застройщика."""
    db = Db(path)
    db.replace_pipelines([
        {"id": 100, "name": "Крым Лиды",
         "statuses": [{"id": 11, "name": "Новое", "sort": 10}]},
        {"id": 700, "name": "Агентские Сочи",
         "statuses": [{"id": 71, "name": "Фиксация", "sort": 10}]},
        {"id": 800, "name": "Агентские Москва",
         "statuses": [{"id": 81, "name": "Фиксация", "sort": 10}]},
    ])
    db.set_pipeline_kind(100, "retail")
    db.set_pipeline_kind(700, "agency")
    db.set_pipeline_kind(800, "agency")
    return db


# ===================== сама отметка =====================

def test_without_a_mark_nothing_changes(tmp_path):
    """
    Отметки нет — работает прежнее правило: первая агентская по номеру.
    Это главная страховка: у клиентов, которые ничего не нажмут, поведение
    не должно измениться ни на шаг.
    """
    db = _three_pipelines(tmp_path / "n1.db")
    assert db.target_pipeline_id() is None
    assert db.agency_pipeline()["pipeline_id"] == 700


def test_marked_pipeline_wins(tmp_path):
    """Отмечена дальняя по номеру — фиксации должны уходить в неё."""
    db = _three_pipelines(tmp_path / "n2.db")
    db.set_target_pipeline(800)

    row = db.agency_pipeline()
    assert row["pipeline_id"] == 800
    assert db.first_status(800) == 81      # этап не выбираем, берём первый


def test_only_one_pipeline_is_marked(tmp_path):
    """
    Отметка ровно одна. Иначе «куда кладём» превращается в лотерею
    по номеру воронки, и оператор об этом не узнает.
    """
    db = _three_pipelines(tmp_path / "n3.db")
    db.set_target_pipeline(700)
    db.set_target_pipeline(800)

    marked = [r["pipeline_id"] for r in db.list_pipelines()
              if r["is_target"]]
    assert marked == [800]
    assert db.target_pipeline_id() == 800


def test_mark_survives_a_resync(tmp_path):
    """
    `replace_pipelines` вызывается при каждом открытии раздела. Если она
    затрёт отметку, фиксации тихо переедут обратно в первую воронку —
    и никто этого не заметит.
    """
    db = _three_pipelines(tmp_path / "n4.db")
    db.set_target_pipeline(800)

    db.replace_pipelines([
        {"id": 100, "name": "Крым Лиды", "statuses": []},
        {"id": 700, "name": "Агентские Сочи", "statuses": []},
        {"id": 800, "name": "Агентские Москва (переименовали)",
         "statuses": []},
    ])

    assert db.target_pipeline_id() == 800
    assert db.pipeline_kinds()[800] == "agency"


# ===================== разметку не трогаем =====================

def test_mark_does_not_touch_the_kinds(tmp_path):
    """
    Самое опасное место задачи. Отметить одну агентскую воронку — не значит
    объявить остальные неагентскими: поиск совпадений идёт по всем, и
    фиксация в неотмеченной воронке обязана по-прежнему считаться
    агентской. Иначе чужая агентская фиксация начнёт блокировать работу.
    """
    db = _three_pipelines(tmp_path / "n5.db")
    db.set_target_pipeline(800)

    kinds = db.pipeline_kinds()
    assert kinds[700] == "agency"      # не отмечена, но всё ещё агентская
    assert kinds[800] == "agency"
    assert kinds[100] == "retail"


def test_marked_pipeline_that_is_not_agency_is_ignored(tmp_path):
    """
    Оператор перевёл отмеченную воронку в розничную. Отметка при этом
    остаётся в базе, но действовать не должна: сделка в рознице через
    несколько минут превратит собственного клиента в «клиента отдела
    продаж». Молча возвращаемся к прежнему правилу.
    """
    db = _three_pipelines(tmp_path / "n6.db")
    db.set_target_pipeline(800)
    db.set_pipeline_kind(800, "retail")

    assert db.agency_pipeline()["pipeline_id"] == 700


def test_no_agency_pipeline_is_still_none(tmp_path):
    """
    Отметка не создаёт агентскую воронку из ничего. Нет ни одной —
    фиксацию создавать нельзя, как и раньше.
    """
    db = Db(tmp_path / "n7.db")
    db.replace_pipelines([{"id": 100, "name": "Крым Лиды", "statuses": []}])
    db.set_pipeline_kind(100, "retail")
    db.set_target_pipeline(100)

    assert db.agency_pipeline() is None


# ===================== боевая база =====================

def test_old_database_gets_the_column(tmp_path):
    """
    Базы на сервере уже с боевыми данными, пересоздавать их нельзя.
    Колонка должна досыпаться миграцией, а разметка kind — уцелеть.
    """
    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    conn.executescript("""
        CREATE TABLE pipelines (
            account_id  INTEGER NOT NULL,
            pipeline_id INTEGER NOT NULL,
            name        TEXT,
            kind        TEXT NOT NULL DEFAULT 'unset',
            PRIMARY KEY (account_id, pipeline_id));
        INSERT INTO pipelines (account_id, pipeline_id, name, kind)
        VALUES (1, 700, 'Агентские Сочи', 'agency'),
               (1, 800, 'Агентские Москва', 'agency');
    """)
    conn.commit()
    conn.close()

    db = Db(path)
    assert db.pipeline_kinds() == {700: "agency", 800: "agency"}
    assert db.target_pipeline_id() is None          # старым ничего не навязали
    assert db.agency_pipeline()["pipeline_id"] == 700

    db.set_target_pipeline(800)
    assert db.agency_pipeline()["pipeline_id"] == 800


def test_migration_is_declared_for_pipelines():
    """Колонка должна досыпаться через MIGRATIONS, а не руками в коде."""
    import db as dbmod

    names = [c[0] for c in dbmod.MIGRATIONS.get("pipelines", [])]
    assert "is_target" in names


# ===================== настройка из .env главнее =====================

def test_env_setting_still_wins(tmp_path, monkeypatch):
    """
    `AMO_PIPELINE_ID` остаётся как было: если он задан, отметка не
    рассматривается вовсе. Иначе у клиента, настроенного через .env,
    случайное нажатие увело бы фиксации в другое место.
    """
    import bot as b

    db = _three_pipelines(tmp_path / "n8.db")
    db.set_target_pipeline(800)

    monkeypatch.setattr(b, "db", db)
    monkeypatch.setattr(b.cfg, "pipeline_id", 555)
    monkeypatch.setattr(b.cfg, "status_id", None)

    assert b.target_pipeline()[0] == 555


def test_mark_is_used_when_env_is_empty(tmp_path, monkeypatch):
    import bot as b

    db = _three_pipelines(tmp_path / "n9.db")
    db.set_target_pipeline(800)

    monkeypatch.setattr(b, "db", db)
    monkeypatch.setattr(b.cfg, "pipeline_id", None)
    monkeypatch.setattr(b.cfg, "status_id", None)

    pipeline_id, status_id, name = b.target_pipeline()
    assert pipeline_id == 800
    assert status_id == 81
    assert name == "Агентские Москва"


# ===================== раздел у оператора =====================

def test_mark_button_only_on_agency_pipelines(tmp_path, monkeypatch):
    """
    Отметить розничную воронку не должно быть физически возможно:
    кнопка есть только у агентских.
    """
    import bot as b

    db = _three_pipelines(tmp_path / "u1.db")
    monkeypatch.setattr(b, "db", db)

    kb = b._pipelines_kb()
    marked = {}
    for row in kb.inline_keyboard:
        pid = int(row[0].callback_data.split(":")[1])
        marked[pid] = [x.callback_data for x in row
                       if x.callback_data.startswith("plt:")]

    assert marked[700] == ["plt:700"]
    assert marked[800] == ["plt:800"]
    assert marked[100] == []            # розничная — без кнопки


def test_mark_is_visible_in_the_section(tmp_path, monkeypatch):
    """Оператор должен видеть, куда именно сейчас кладутся фиксации."""
    import bot as b

    db = _three_pipelines(tmp_path / "u2.db")
    db.set_target_pipeline(800)
    monkeypatch.setattr(b, "db", db)

    out = b._pipelines_text()
    assert "сюда кладём" in out
    москва = [ln for ln in out.split("\n") if "Агентские Москва" in ln][0]
    сочи = [ln for ln in out.split("\n") if "Агентские Сочи" in ln][0]
    assert "сюда кладём" in москва
    assert "сюда кладём" not in сочи


def test_pressing_the_mark_does_not_change_kind():
    """
    Обработчик кнопки не имеет права трогать разметку: это разные вещи,
    и смешивание их — ровно тот способ сломать вердикты.
    """
    import bot as b

    src = inspect.getsource(b.cb_pipeline_target)
    assert "set_target_pipeline" in src
    assert "set_pipeline_kind" not in src


def test_mark_is_operator_only():
    """Воронки — операторский раздел, владельцу их не показывают."""
    import bot as b

    src = inspect.getsource(b.cb_pipeline_target)
    assert "is_operator" in src
