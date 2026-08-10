"""Роли, меню и статистика."""

import time
from types import SimpleNamespace

import menu as mn
import texts
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


# ===================== чужой человек и /admin =====================

def test_admin_does_not_stay_silent_for_strangers():
    """
    Молчание в ответ на команду читается как «бот сломался». Владелец
    Eco Invest написал «/admin» три раза подряд в чужого бота и не получил
    ни слова — потому что доступа к меню у него там нет.
    """
    import inspect

    import bot as b

    src = inspect.getsource(b.cmd_admin)
    body = src.split("if not has_menu", 1)[1]
    # До выхода из обработчика человеку должны хоть что-то ответить.
    before_return = body.split("return", 1)[0]
    assert "m.answer" in before_return, (
        "cmd_admin снова молча выходит для тех, у кого нет меню")


def test_no_menu_text_hints_about_wrong_bot():
    """У оператора несколько ботов, и перепутать их — обычное дело."""
    for lang in ("ru", "en"):
        out = texts.t(lang, "no_menu")
        assert out and "{" not in out
    assert "не тому боту" in texts.t("ru", "no_menu")
    assert "wrong bot" in texts.t("en", "no_menu")


def test_no_menu_does_not_scold():
    """Тон мягкий и здесь: человек не виноват, что ошибся ботом."""
    harsh = ("запрещ", "нельзя", "ошибка", "неверн", "denied", "forbidden")
    for lang in ("ru", "en"):
        low = texts.t(lang, "no_menu").lower()
        assert not any(w in low for w in harsh), low


# ===================== панель групп =====================

def test_chats_are_remembered_when_seen(tmp_path):
    """
    Список чатов у Telegram не спросить: бот узнаёт о группе только когда
    оттуда приходит сообщение. Значит запоминать должен сам.
    """
    db = Db(tmp_path / "c1.db")
    db.see_chat(-100, "TEUS & BREIG", is_admin=True)
    db.see_chat(-100, "TEUS и BREIG")          # группу переименовали

    row = db.get_chat(-100)
    assert row["title"] == "TEUS и BREIG"      # новое название победило
    assert row["is_admin"] == 1                # признак не потерялся
    assert row["messages"] == 2


def test_chat_list_is_sorted_by_recency(tmp_path):
    db = Db(tmp_path / "c2.db")
    db.see_chat(-1, "старый")
    db.conn.execute("UPDATE chats SET last_seen=1 WHERE chat_id=-1")
    db.conn.commit()
    db.see_chat(-2, "свежий")
    assert [r["chat_id"] for r in db.list_chats()] == [-2, -1]


def test_chat_card_warns_when_bot_is_not_admin():
    """
    Без прав администратора и с включённой приватностью бот видит не всё —
    человек должен понимать, почему фиксации проходят через раз.
    """
    r = {"chat_id": -1, "title": "Bali", "agency": None, "lang": "",
         "messages": 7, "is_admin": False}
    assert "не администратор" in mn.chat_card(r)

    r["is_admin"] = True
    assert "не администратор" not in mn.chat_card(r)


def test_language_buttons_mark_the_current_choice():
    kb = mn.chat_kb(-1, "en")
    labels = [b.text for line in kb for b in line]
    assert any(t.startswith("● ") and "English" in t for t in labels)
    assert not any(t.startswith("● ") and "Русский" in t for t in labels)


def test_auto_language_is_the_empty_value():
    """
    «По сообщениям» — это снятая привязка, а не язык с пустым кодом:
    дальше значение читает определитель языка.
    """
    import inspect

    import bot as b

    src = inspect.getsource(b.cb_chats)
    assert '"" if code == "auto" else code' in src


def test_empty_list_explains_why(tmp_path):
    out = mn.chats_overview([])
    assert "первое сообщение" in out
    assert "не администратор" in out


def test_group_messages_are_recorded():
    import inspect

    import bot as b

    assert "db.see_chat(" in inspect.getsource(b.on_message)


def test_first_sighting_is_reported_once(tmp_path):
    """Про новую группу говорим один раз, а не на каждое сообщение."""
    db = Db(tmp_path / "c3.db")
    assert db.see_chat(-100, "TEUS") is True
    assert db.see_chat(-100, "TEUS") is False
    assert db.see_chat(-200, "ERA") is True


def test_bound_chats_are_not_announced():
    """Если агентство уже закреплено, сообщать не о чем."""
    import inspect

    import bot as b

    src = inspect.getsource(b._announce_chat)
    assert "chat_agency_id" in src and "return" in src


def test_alerts_go_through_a_queue():
    """
    У бота, который уже сидит в семидесяти чатах, все они «откроются»
    почти одновременно — Telegram оборвёт поток сообщений в одну личку.
    """
    import inspect

    import bot as b

    src = inspect.getsource(b.chat_alert_loop)
    assert "_new_chats.get()" in src
    assert "asyncio.sleep(CHAT_ALERT_PAUSE)" in src
    assert b.CHAT_ALERT_PAUSE >= 1


def test_alert_does_not_go_into_the_group():
    """В рабочие чаты агентств бот со своей настройкой не лезет."""
    import inspect

    import bot as b

    src = inspect.getsource(b.chat_alert_loop)
    assert "chat_id," not in src.split("send_message(", 1)[1][:40]


def test_alert_text_names_the_group():
    out = mn.new_chat_alert("TEUS & BREIG", -100, "TEUS")
    assert "TEUS & BREIG" in out and "TEUS" in out
    assert "{" not in out
