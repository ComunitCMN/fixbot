"""Роли, меню и статистика."""

import time
from types import SimpleNamespace

import menu as mn
from db import Db

NOW = int(time.time())
DAY = 86400


def _cfg(operators=(), owners=()):
    return SimpleNamespace(operator_ids=set(operators), owner_ids=set(owners),
                           developer_name="BREIG")


# ===================== роли =====================

def test_roles(tmp_path):
    db = Db(tmp_path / "r.db")
    cfg = _cfg(operators={1}, owners={2})

    assert mn.role_of(1, cfg, db) == mn.OPERATOR
    assert mn.role_of(2, cfg, db) == mn.OWNER
    assert mn.role_of(3, cfg, db) == mn.NOBODY


def test_staff_gets_owner_role(tmp_path):
    """Сотрудник, которого добавил владелец, получает тот же доступ."""
    db = Db(tmp_path / "r2.db")
    cfg = _cfg(operators={1}, owners={2})

    assert mn.role_of(99, cfg, db) == mn.NOBODY
    db.add_staff(99, "marketer", "Маркетолог", added_by=2)
    assert mn.role_of(99, cfg, db) == mn.OWNER


def test_staff_added_once(tmp_path):
    db = Db(tmp_path / "r3.db")
    assert db.add_staff(99, "u", "Имя", 2) is True
    assert db.add_staff(99, "u", "Имя", 2) is False
    assert len(db.list_staff()) == 1


def test_staff_removed(tmp_path):
    db = Db(tmp_path / "r4.db")
    db.add_staff(99, "u", "Имя", 2)
    db.remove_staff(99)
    assert db.list_staff() == []
    assert not db.is_staff(99)


# ===================== меню =====================

def test_owner_has_no_technical_section():
    """
    Владелец не должен видеть воронки и синхронизацию: это не его слой,
    и ошибиться там легко.
    """
    owner = mn.main_menu(mn.OWNER)
    labels = [b[0].text for b in owner.inline_keyboard]
    assert not any("Техническое" in x for x in labels)

    op = mn.main_menu(mn.OPERATOR)
    labels_op = [b[0].text for b in op.inline_keyboard]
    assert any("Техническое" in x for x in labels_op)


def test_owner_menu_has_everything_they_need():
    labels = " ".join(b[0].text for b in mn.main_menu(mn.OWNER).inline_keyboard)
    for word in ("Рассылка", "Группы", "Агентства", "Статистика",
                 "Сотрудники", "Как это работает"):
        assert word in labels


def test_every_menu_screen_has_way_back():
    for kb in (mn.back_kb(), mn.tech_menu(), mn.stats_menu(),
               mn.staff_menu([])):
        flat = [b.callback_data for row in kb.inline_keyboard for b in row]
        assert "m:root" in flat


def test_help_text_avoids_internals():
    """Справка для владельца, а не для программиста."""
    assert "amoCRM" not in mn.HELP_TEXT
    assert "воронк" not in mn.HELP_TEXT.lower()
    assert "токен" not in mn.HELP_TEXT.lower()
    assert "фиксац" in mn.HELP_TEXT.lower()


def test_chats_text_explains_how_to_connect():
    """Пустой список должен объяснять, что делать, а не просто пустовать."""
    out = mn.chats_text([])
    assert "администратор" in out.lower()
    assert "добавьте" in out.lower()


def test_chats_text_lists_agencies():
    out = mn.chats_text([(-100, "ERA B"), (-200, None)])
    assert "ERA B" in out
    assert "Всего: 2" in out


# ===================== статистика =====================

def _fix(db, agency_id=1, agent="Иван", tid=42, days_ago=0, lead=900,
         verdict="unique"):
    return db.log_fixation(
        digits="7999123456" + str(tid % 10), client_name="Клиент",
        agency_id=agency_id, agent_telegram_id=tid, agent_name=agent,
        verdict=verdict, amo_lead_id=lead,
        created_at=NOW - days_ago * DAY)


def test_stats_counts_by_period(tmp_path):
    db = Db(tmp_path / "s.db")
    _fix(db, days_ago=1)
    _fix(db, days_ago=10, tid=43)
    _fix(db, days_ago=60, tid=44)

    assert db.fixations_count() == 3
    assert db.fixations_count(7) == 1
    assert db.fixations_count(30) == 2


def test_stats_ignores_rejected(tmp_path):
    """В статистику фиксаций попадает только то, что реально записано."""
    db = Db(tmp_path / "s2.db")
    _fix(db)
    _fix(db, tid=43, lead=None, verdict="retail_blocked")

    assert db.fixations_count() == 1
    assert db.rejected_by_verdict() == [("retail_blocked", 1)]


def test_stats_by_agency(tmp_path):
    db = Db(tmp_path / "s3.db")
    a1 = db.create_agency("ERA B", "era b")
    a2 = db.create_agency("TEUS", "teus")
    _fix(db, agency_id=a1, tid=1)
    _fix(db, agency_id=a1, tid=2)
    _fix(db, agency_id=a2, tid=3)

    got = db.fixations_by_agency()
    assert got[0] == ("ERA B", 2)
    assert ("TEUS", 1) in got


def test_stats_by_agency_handles_missing(tmp_path):
    db = Db(tmp_path / "s4.db")
    _fix(db, agency_id=None)
    assert db.fixations_by_agency()[0][0].startswith("—")


def test_stats_by_agent(tmp_path):
    db = Db(tmp_path / "s5.db")
    _fix(db, agent="Иван", tid=1)
    _fix(db, agent="Иван", tid=1)
    _fix(db, agent="Аня", tid=2)

    got = db.fixations_by_agent()
    assert got[0] == ("Иван", 2)


def test_agents_summary(tmp_path):
    db = Db(tmp_path / "s6.db")
    db.upsert_agent(1, "a", "A", dm_open=True)
    db.set_agent_field(1, phone="+7 999 123-45-67")
    db.upsert_agent(2, "b", "B")

    s = db.agents_summary()
    assert s == {"total": 2, "subscribed": 1, "with_phone": 1}


def test_connected_chats(tmp_path):
    db = Db(tmp_path / "s7.db")
    aid = db.create_agency("ERA B", "era b")
    db.set_meta(f"chat_agency:-100", str(aid))
    db.set_meta("chat_agency:-200", "")        # незавершённая привязка

    assert db.connected_chats() == [(-100, "ERA B")]


def test_stats_text_renders(tmp_path):
    db = Db(tmp_path / "s8.db")
    a1 = db.create_agency("ERA B", "era b")
    _fix(db, agency_id=a1)

    out = mn.stats_text(
        days=30, total=1, period=1,
        by_agency=db.fixations_by_agency(),
        by_agent=db.fixations_by_agent(),
        agents=db.agents_summary(),
        rejected=db.rejected_by_verdict())
    assert "ERA B" in out and "за 30 дней" in out
    assert "{" not in out
