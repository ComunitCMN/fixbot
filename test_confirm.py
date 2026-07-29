"""Тесты подтверждения фиксации и карточки агента."""

import time

import pytest

import texts
from db import Db, Match
from phones import normalize
from verdict import Decision, Verdict, decide

NOW = int(time.time())
DAY = 86400


# ===================== хранение заявок =====================

def test_pending_roundtrip(tmp_path):
    db = Db(tmp_path / "p1.db")
    payload = {"client": "Петров", "digits": "79171475214",
               "agency_id": 1, "agency_name": "Дом+"}
    pid = db.create_pending(chat_id=-100, message_id=5, author_id=42,
                            payload=payload)

    row = db.get_pending(pid)
    assert row["status"] == "waiting"
    assert row["author_id"] == 42
    assert row["payload"]["client"] == "Петров"


def test_pending_closes_once(tmp_path):
    db = Db(tmp_path / "p2.db")
    pid = db.create_pending(-100, 5, 42, {"digits": "79171475214"})
    db.close_pending(pid, "done")
    assert db.get_pending(pid)["status"] == "done"


def test_pending_payload_update(tmp_path):
    """Смена агентства не должна терять остальные поля."""
    db = Db(tmp_path / "p3.db")
    pid = db.create_pending(-100, 5, 42, {"client": "Петров", "agency_id": 1})
    payload = db.get_pending(pid)["payload"]
    payload["agency_id"] = 7
    payload["agency_name"] = "Новосёл"
    db.update_pending_payload(pid, payload)

    got = db.get_pending(pid)["payload"]
    assert got["agency_id"] == 7 and got["client"] == "Петров"


def test_pending_expires(tmp_path):
    db = Db(tmp_path / "p4.db")
    pid = db.create_pending(-100, 5, 42, {"digits": "79171475214"})
    db.conn.execute("UPDATE pending SET created_at=? WHERE id=?",
                    (NOW - 7200, pid))
    db.conn.commit()

    expired = db.expire_pending(3600)
    assert len(expired) == 1 and expired[0]["id"] == pid
    assert db.get_pending(pid)["status"] == "expired"
    # повторный вызов ничего не находит
    assert db.expire_pending(3600) == []


def test_pending_fresh_not_expired(tmp_path):
    db = Db(tmp_path / "p5.db")
    db.create_pending(-100, 5, 42, {"digits": "79171475214"})
    assert db.expire_pending(3600) == []


# ===================== карточка агента =====================

def test_agent_amo_contact_stored(tmp_path):
    db = Db(tmp_path / "a1.db")
    db.upsert_agent(42, "ivan_p", "Иван Петров", agency_id=1)
    db.set_agent_amo_contact(42, 9001)
    assert db.get_agent(42)["amo_contact_id"] == 9001


def test_migration_adds_columns(tmp_path):
    """Старая база без новых колонок должна доехать без потери данных."""
    import sqlite3

    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    conn.executescript("""
        CREATE TABLE agents (
            account_id INTEGER NOT NULL, telegram_id INTEGER NOT NULL,
            username TEXT, display_name TEXT, agency_id INTEGER,
            dm_open INTEGER NOT NULL DEFAULT 0,
            subscribed INTEGER NOT NULL DEFAULT 1,
            created_at INTEGER, last_seen_at INTEGER,
            PRIMARY KEY (account_id, telegram_id));
        INSERT INTO agents (account_id, telegram_id, display_name)
        VALUES (1, 42, 'Старый Агент');
    """)
    conn.commit()
    conn.close()

    db = Db(path)
    agent = db.get_agent(42)
    assert agent["display_name"] == "Старый Агент"
    assert agent["amo_contact_id"] is None      # колонка появилась
    db.set_agent_amo_contact(42, 555)
    assert db.get_agent(42)["amo_contact_id"] == 555


# ===================== тексты =====================

def test_other_agency_is_green_and_permissive():
    """
    Чужая фиксация не должна выглядеть как отказ или предупреждение:
    агент в равной позиции, а не в проигрышной.
    """
    d = decide([Match(digits="79171475214", source="chat", has_agency=True,
                      agency_id=2, created_at=NOW - 12 * DAY)],
               agency_id=1, agent_telegram_id=10, now=NOW)
    out = texts.render(d, client="Петров", p=normalize("+79171475214"),
                       agency="Дом+", contact_url="http://c", lead_url="http://l")

    assert "🟢" in out
    assert "🟡" not in out
    assert "работайте" in out.lower()
    assert "депозит" in out.lower()
    assert "эксклюзива нет" not in out.lower()


def test_agency_pipeline_picked_from_markup(tmp_path):
    """Фиксации должны уходить в размеченную агентскую воронку."""
    db = Db(tmp_path / "pl.db")
    db.replace_pipelines([
        {"id": 100, "name": "Крым Лиды",
         "statuses": [{"id": 1, "name": "Новое", "sort": 10}]},
        {"id": 700, "name": "Агентские Клиенты",
         "statuses": [{"id": 71, "name": "Фиксация", "sort": 10},
                      {"id": 72, "name": "Бронь", "sort": 20}]},
    ])
    db.set_pipeline_kind(100, "retail")
    db.set_pipeline_kind(700, "agency")

    row = db.agency_pipeline()
    assert row["pipeline_id"] == 700
    assert db.first_status(700) == 71          # первый этап по порядку


def test_first_status_skips_system_stages(tmp_path):
    """
    «Неразобранное» и закрывающие этапы через API назначать нельзя —
    amoCRM отвечает NotSupportedChoice.
    """
    db = Db(tmp_path / "st.db")
    db.replace_pipelines([{
        "id": 700, "name": "Агентские",
        "statuses": [
            {"id": 1, "name": "Неразобранное", "sort": 0, "type": 1},
            {"id": 71, "name": "Фиксация", "sort": 10, "type": 0},
            {"id": 142, "name": "Успешно реализовано", "sort": 900},
            {"id": 143, "name": "Закрыто", "sort": 910},
        ],
    }])
    assert db.first_status(700) == 71


def test_first_status_none_when_only_system(tmp_path):
    """Если рабочих этапов нет — лучше не указывать этап вовсе."""
    db = Db(tmp_path / "st2.db")
    db.replace_pipelines([{
        "id": 700, "name": "Пустая",
        "statuses": [{"id": 1, "name": "Неразобранное", "sort": 0, "type": 1}],
    }])
    assert db.first_status(700) is None


def test_status_type_column_migrates(tmp_path):
    """Старая база без колонки type не должна ломаться."""
    import sqlite3

    path = tmp_path / "old_st.db"
    conn = sqlite3.connect(path)
    conn.executescript("""
        CREATE TABLE statuses (
            account_id INTEGER NOT NULL, pipeline_id INTEGER NOT NULL,
            status_id INTEGER NOT NULL, name TEXT, sort INTEGER,
            is_booking INTEGER NOT NULL DEFAULT 0,
            notify INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (account_id, pipeline_id, status_id));
        INSERT INTO statuses (account_id, pipeline_id, status_id, name, sort)
        VALUES (1, 700, 1, 'Неразобранное', 0), (1, 700, 71, 'Фиксация', 10);
    """)
    conn.commit()
    conn.close()

    db = Db(path)
    # старые строки без type всё равно не должны отдавать служебный этап
    assert db.first_status(700) == 71


def test_no_agency_pipeline_returns_none(tmp_path):
    """
    Если агентской воронки нет — сделку создавать нельзя.
    Иначе она уйдёт в розницу и заблокирует собственного же клиента.
    """
    db = Db(tmp_path / "pl2.db")
    db.replace_pipelines([
        {"id": 100, "name": "Крым Лиды", "statuses": [{"id": 1, "sort": 10}]},
    ])
    db.set_pipeline_kind(100, "retail")
    assert db.agency_pipeline() is None


def test_pending_findable_without_reply(tmp_path):
    """
    Название агентства часто присылают обычным сообщением, не реплаем.
    Значит свежий незакрытый вопрос надо находить по автору и чату.
    """
    import time as _t

    db = Db(tmp_path / "nr.db")
    pid = db.create_pending(chat_id=-100, message_id=5, author_id=42,
                            payload={"digits": "79171475214"})

    row = db.conn.execute(
        "SELECT id FROM pending WHERE account_id=? AND chat_id=?"
        " AND author_id=? AND status='waiting' AND created_at > ?"
        " ORDER BY created_at DESC LIMIT 1",
        (db.account_id, -100, 42, int(_t.time()) - 900),
    ).fetchone()
    assert row and row["id"] == pid

    # чужой человек в том же чате не должен подхватить чужую заявку
    other = db.conn.execute(
        "SELECT id FROM pending WHERE account_id=? AND chat_id=?"
        " AND author_id=? AND status='waiting'",
        (db.account_id, -100, 999),
    ).fetchone()
    assert other is None


def test_pending_not_found_after_close(tmp_path):
    """Закрытая заявка не должна ловить последующие сообщения."""
    import time as _t

    db = Db(tmp_path / "nc.db")
    pid = db.create_pending(-100, 5, 42, {"digits": "79171475214"})
    db.close_pending(pid, "done")
    row = db.conn.execute(
        "SELECT id FROM pending WHERE account_id=? AND chat_id=?"
        " AND author_id=? AND status='waiting' AND created_at > ?",
        (db.account_id, -100, 42, int(_t.time()) - 900),
    ).fetchone()
    assert row is None


def test_retail_blocked_is_polite():
    """
    Бот не выносит приговор: он не знает договорённостей между
    агентством и застройщиком. Его дело — сообщить факт.
    """
    d = decide([Match(digits="79171475214", has_retail=True,
                      last_retail_activity=NOW - 30 * DAY,
                      created_at=NOW - 200 * DAY)],
               agency_id=1, agent_telegram_id=10, now=NOW)
    out = texts.render(d, client="Петров", p=normalize("+79171475214"),
                       agency="Дом+")

    assert "не уникальный" in out.lower()
    assert "отделом продаж" in out
    assert "уточните у своего менеджера" in out.lower()
    # никаких запретов и приговоров
    assert "невозможна" not in out.lower()
    assert "нельзя" not in out.lower()
    assert "смысла нет" not in out.lower()


def test_retail_blocked_shows_since_date():
    """Дата — «с какого числа числится», а не «когда последний раз шевелился»."""
    d = decide([Match(digits="79171475214", has_retail=True,
                      last_retail_activity=NOW - 30 * DAY,
                      created_at=NOW - 200 * DAY)],
               agency_id=1, agent_telegram_id=10, now=NOW)
    assert d.retail_since == NOW - 200 * DAY
    assert texts.when(d.retail_since) in texts.render(
        d, client="П", p=normalize("+79171475214"), agency=None)


def test_booked_elsewhere_is_polite():
    d = decide([Match(digits="79171475214", has_agency=True, booked=True,
                      agency_id=2)],
               agency_id=1, agent_telegram_id=10, now=NOW)
    out = texts.render(d, client="Петров", p=normalize("+79171475214"),
                       agency="Дом+")
    assert "уточните у своего менеджера" in out.lower()
    assert "смысла нет" not in out.lower()


def test_confirm_card_hides_crm_details():
    """
    Агент из чужого агентства не должен видеть внутреннюю кухню
    застройщика — ему важно только то, что фиксация идёт по кнопке.
    """
    out = texts.confirm_card(
        client="Петров", p=normalize("+79171475214"), agency="Дом+",
        agent="Иван", object_=None, verdict_note="")
    assert "amoCRM" not in out
    assert "после нажатия кнопки" in out


def test_confirm_card_offers_name_when_missing():
    out = texts.confirm_card(
        client=None, p=normalize("+79171475214"), agency="Дом+",
        agent="Иван", object_=None, verdict_note="")
    assert "не указано" in out
    assert "Имя можно добавить" in out


def test_masked_note_is_plain_language():
    """Без жаргона: «под маской», «префикс» и «неоднозначность» — не нужны."""
    out = texts.masked_note(normalize("+7 917 147-52-6*"))
    assert "не хватает последней цифры" in out
    assert "маск" not in out.lower()
    assert "префикс" not in out.lower()

    out2 = texts.masked_note(normalize("+7 917 147-52-**"))
    assert "не хватает 2 последних цифр" in out2


def test_confirm_card_has_all_fields():
    out = texts.confirm_card(
        client="Петров Сергей", p=normalize("+7 917 147-52-**"),
        agency="Дом+", agent="Иван (@ivan)", object_="ЖК Восход",
        verdict_note=texts.confirm_note_unique())
    for part in ("Петров Сергей", "Дом+", "Иван", "ЖК Восход",
                 "+7 917 147-52-**"):
        assert part in out
    assert "после нажатия кнопки" in out
    assert "Номер неполный" in out


def test_confirm_card_marks_missing_agency():
    out = texts.confirm_card(client="Петров", p=normalize("+79171475214"),
                             agency=None, agent="Иван", object_=None,
                             verdict_note="")
    assert "не определено" in out


def test_confirm_card_without_client_name():
    out = texts.confirm_card(client=None, p=normalize("+79171475214"),
                             agency="Дом+", agent="Иван", object_=None,
                             verdict_note="")
    assert "—" in out          # имя не выдумываем


def test_confirmed_hides_crm_links_by_default():
    """
    Ссылки на CRM застройщика агентам не нужны: доступа к ней у них нет.
    По умолчанию их быть не должно нигде в ответе.
    """
    out = texts.confirmed("Петров", normalize("+79171475214"), "Дом+",
                          "http://c", "http://l", "http://a")
    assert "зафиксирован" in out.lower()
    assert "Петров" in out and "Дом+" in out
    assert "amoCRM" not in out
    assert "href" not in out


def test_confirmed_shows_links_when_enabled():
    """Для внутренних чатов ссылки можно включить через SHOW_CRM_LINKS."""
    texts.SHOW_LINKS = True
    try:
        out = texts.confirmed("Петров", normalize("+79171475214"), "Дом+",
                              "http://c", "http://l", "http://a")
        assert "http://c" in out and "http://l" in out and "http://a" in out
    finally:
        texts.SHOW_LINKS = False


def test_verdict_texts_have_no_links_by_default():
    """Ни один вердикт не должен протаскивать ссылки в обход настройки."""
    for v in Verdict:
        d = Decision(v)
        d.other_agency_since = NOW - DAY
        d.retail_activity = NOW - 400 * DAY
        d.retail_since = NOW - 500 * DAY
        d.own_since = NOW - DAY
        out = texts.render(d, client="Петров", p=normalize("+79171475214"),
                           agency="Дом+", contact_url="http://c",
                           lead_url="http://l")
        assert "amoCRM" not in out, v
        assert "href" not in out, v


@pytest.mark.parametrize("verdict", [
    Verdict.UNIQUE, Verdict.OTHER_AGENCY, Verdict.RETAIL_EXPIRED,
])
def test_creating_verdicts_have_confirm_note(verdict):
    """У каждого вердикта, ведущего к записи, должна быть своя пояснялка."""
    d = Decision(verdict)
    d.other_agency_since = NOW - DAY
    d.retail_activity = NOW - 400 * DAY
    note = {
        Verdict.UNIQUE: texts.confirm_note_unique(),
        Verdict.OTHER_AGENCY: texts.confirm_note_other_agency(d),
        Verdict.RETAIL_EXPIRED: texts.confirm_note_retail_expired(d, 365),
    }[verdict]
    assert note and "🟢" in note
