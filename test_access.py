"""Кому бот отвечает по существу."""

import access as ac


def agent(**kw):
    base = {"agency_id": 1, "status": "active"}
    base.update(kw)
    return base


# ===================== правило =====================

def test_operator_and_owner_are_unrestricted():
    assert ac.decide(has_menu=True, agent=None) is ac.Access.ALLOWED
    assert ac.decide(has_menu=True, agent=None,
                     lookups_last_hour=999) is ac.Access.ALLOWED


def test_known_agent_is_allowed():
    assert ac.decide(has_menu=False, agent=agent()) is ac.Access.ALLOWED


def test_stranger_gets_nothing():
    """
    Главное правило. Вердикт «клиент у отдела продаж» — это сведения
    о базе застройщика, и посторонний их получать не должен.
    """
    assert ac.decide(has_menu=False, agent=None) is ac.Access.STRANGER


def test_agent_without_agency_must_introduce_himself():
    """Бот его видел, но не знает, от кого он, — вердикта пока нет."""
    assert ac.decide(has_menu=False,
                     agent=agent(agency_id=None)) is ac.Access.STRANGER


def test_pending_application_waits():
    assert ac.decide(has_menu=False,
                     agent=agent(status="pending")) is ac.Access.PENDING


def test_rejected_stays_rejected():
    """Отказ владельца — навсегда, пока он сам не передумает."""
    assert ac.decide(has_menu=False,
                     agent=agent(status="rejected")) is ac.Access.REJECTED


def test_rejection_beats_everything_else():
    assert ac.decide(has_menu=False, agent=agent(status="rejected"),
                     lookups_last_hour=0) is ac.Access.REJECTED


def test_missing_status_means_ordinary_agent():
    """У всех, кто был в базе до этой правки, поля просто нет."""
    assert ac.decide(has_menu=False,
                     agent={"agency_id": 1}) is ac.Access.ALLOWED
    assert ac.decide(has_menu=False,
                     agent={"agency_id": 1, "status": None}) is ac.Access.ALLOWED


# ===================== частота =====================

def test_too_many_lookups_stops_the_probing():
    """Двадцать проверок подряд — это перебор номеров, а не работа."""
    assert ac.decide(has_menu=False, agent=agent(),
                     lookups_last_hour=19) is ac.Access.ALLOWED
    assert ac.decide(has_menu=False, agent=agent(),
                     lookups_last_hour=20) is ac.Access.TOO_MANY


def test_counter_forgets_after_an_hour():
    c = ac.LookupCounter()
    for i in range(5):
        c.add(42, now=1000 + i)
    assert c.count(42, now=1004) == 5                   # только что
    assert c.count(42, now=1004 + ac.HOUR // 2) == 5    # полчаса спустя
    assert c.count(42, now=1004 + ac.HOUR + 1) == 0     # час прошёл у всех


def test_counter_keeps_people_apart():
    c = ac.LookupCounter()
    c.add(1, now=0)
    c.add(1, now=1)
    c.add(2, now=2)
    assert c.count(1, now=3) == 2
    assert c.count(2, now=3) == 1


# ===================== подключение к боту =====================

def test_check_is_closed_to_strangers():
    """
    Главная дыра: `/check` отвечал кому угодно, а вердикт — это сведения
    о базе застройщика. Проверка доступа должна стоять первой строкой,
    до разбора номера.
    """
    import inspect

    import bot as b

    src = inspect.getsource(b.cmd_check)
    assert "allowed_to_look_up" in src
    head = src.split("allowed_to_look_up", 1)[0]
    assert "normalize" not in head and "split" not in head


def test_refusal_says_nothing_about_the_base():
    """
    Текст одинаков для любого номера: посторонний не должен узнать даже
    того, есть клиент в базе или нет.
    """
    import texts

    for lang in ("ru", "en"):
        out = texts.no_access("stranger", lang)
        assert out and "{" not in out
        low = out.lower()
        for word in ("отдел продаж", "уникал", "sales department", "unique"):
            assert word not in low


def test_every_refusal_kind_has_a_text():
    import texts

    for state in ac.Access:
        if state is ac.Access.ALLOWED:
            continue
        for lang in ("ru", "en"):
            assert texts.no_access(state.value, lang)


def test_allowed_lookups_are_counted():
    """Иначе ограничение в двадцать проверок никогда не сработает."""
    import inspect

    import bot as b

    src = inspect.getsource(b.allowed_to_look_up)
    assert "_lookups.add" in src
    # Считаем только состоявшиеся проверки, а не отказы.
    assert src.index("_lookups.add") < src.index("no_access")


def test_bound_chat_is_a_pass_by_itself():
    """
    В рабочем чате агентства пропуск — сам чат. Иначе `/check` перестал бы
    работать у агента, чьё агентство ещё не попало в профиль.
    """
    assert ac.decide(has_menu=False, agent=None,
                     in_bound_chat=True) is ac.Access.ALLOWED
    assert ac.decide(has_menu=False, agent=agent(agency_id=None),
                     in_bound_chat=True) is ac.Access.ALLOWED


def test_rejection_holds_even_in_a_bound_chat():
    """Кого владелец отклонил, тот отклонён и в группе."""
    assert ac.decide(has_menu=False, agent=agent(status="rejected"),
                     in_bound_chat=True) is ac.Access.REJECTED


def test_limit_applies_in_groups_too():
    assert ac.decide(has_menu=False, agent=None, in_bound_chat=True,
                     lookups_last_hour=20) is ac.Access.TOO_MANY


def test_bot_passes_the_chat_along():
    import inspect

    import bot as b

    assert "in_bound_chat" in inspect.getsource(b.access_of)
    assert "access_of(m.from_user.id, m.chat.id)" in \
        inspect.getsource(b.allowed_to_look_up)


# ===================== база: статус и частники =====================

def test_migration_adds_columns_to_an_existing_base(tmp_path):
    """
    Базы на сервере боевые: колонки должны добавляться к живой таблице,
    а не появляться только у новых баз.
    """
    import sqlite3

    from db import Db

    path = tmp_path / "old.db"
    Db(path).conn.close()                       # база «старого» образца
    conn = sqlite3.connect(path)
    conn.execute("ALTER TABLE agents DROP COLUMN status")
    conn.execute("ALTER TABLE agents DROP COLUMN intro_name")
    conn.execute("ALTER TABLE agencies DROP COLUMN private")
    conn.commit()
    conn.close()

    db = Db(path)                               # открываем новым кодом
    cols = {r["name"] for r in db.conn.execute("PRAGMA table_info(agents)")}
    assert {"status", "intro_name"} <= cols
    cols = {r["name"] for r in db.conn.execute("PRAGMA table_info(agencies)")}
    assert "private" in cols


def test_private_agent_becomes_an_agency(tmp_path):
    from db import Db

    db = Db(tmp_path / "p.db")
    aid = db.create_private_agency("  иван   петров ")

    assert db.is_private_agency(aid)
    assert db.get_agency(aid)["name"] == "иван петров"
    # Обычное агентство пометки не получает.
    other = db.create_agency("TEUS", "teus")
    assert not db.is_private_agency(other)


def test_application_waits_and_then_resolves(tmp_path):
    from db import Db

    db = Db(tmp_path / "a.db")
    db.upsert_agent(42, "ivan", "Иван")
    db.set_agent_status(42, "pending", intro_name="Иван Петров")

    assert [r["telegram_id"] for r in db.pending_agents()] == [42]
    assert db.get_agent(42)["intro_name"] == "Иван Петров"

    db.set_agent_status(42, "active")
    assert db.pending_agents() == []
    # Имя, которым представился, не теряется после решения.
    assert db.get_agent(42)["intro_name"] == "Иван Петров"
