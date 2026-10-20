"""
Неуникальный клиент — фиксируем с пометкой.

20.10.2026, решение владельца: клиент, который уже есть в прямом отделе
продаж, больше не отклоняется. Агент видит карточку с кнопкой; по
нажатию сделка ложится в агентскую воронку с тегом «не уникальный»
и примечанием. Раньше бот отвечал красным и сам писал в карточку
клиента «Попытка фиксации — отклонена».

Что здесь стережём:
  * меняется только «клиент прямого отдела»: бронь у другого агентства
    и непонятное происхождение по-прежнему отклоняются;
  * в CRM пишем только по нажатию — как всегда;
  * такая фиксация идёт в счёт, как любая записанная в CRM;
  * у неё нет уведомлений «появился конкурент» и «клиент постучался
    в прямой отдел» и нет срока 45 дней — по факту клиент не уникален;
  * вердикты для всех остальных не меняются: розница проверяется
    первой, и сделка с пометкой никого не разблокирует.
"""

import asyncio
import time
from types import SimpleNamespace

import pytest

from db import Db, Match
from test_double_click import AGENT, Card, _env, _pending, _press
from verdict import Verdict, decide

DAY = 86400
NOW = int(time.time())


def _retail(**kw):
    kw.setdefault("digits", "79991234567")
    kw.setdefault("source", "amo")
    kw.setdefault("origin_known", True)
    kw.setdefault("contact_id", 77)
    return Match(has_retail=True, last_retail_activity=NOW - 30 * DAY,
                 created_at=NOW - 60 * DAY, **kw)


# ===================== вердикт =====================

def test_retail_client_can_be_fixed_with_a_mark():
    d = decide([_retail()], agency_id=1, agent_telegram_id=10, now=NOW)
    assert d.verdict is Verdict.RETAIL_BLOCKED
    assert d.not_unique and d.can_fix
    assert not d.creates_fixation          # обычной «уникальной» она не стала


def test_booked_and_unknown_are_still_refused():
    booked = Match(digits="79991234567", source="amo", origin_known=True,
                   has_agency=True, booked=True, agency_id=5)
    unknown = Match(digits="79991234567", source="amo", origin_known=True)
    for m in (booked, unknown):
        d = decide([m], agency_id=1, agent_telegram_id=10, now=NOW)
        assert not d.not_unique and not d.can_fix


def test_marked_fixation_does_not_unblock_anyone():
    """Розница живая — и после сделки с пометкой всем «не уникальный»."""
    ours = Match(digits="79991234567", source="chat", has_agency=True,
                 origin_known=True, agency_id=1, agent_telegram_id=10,
                 created_at=NOW - DAY)
    for agency in (1, 2):
        d = decide([_retail(), ours], agency_id=agency,
                   agent_telegram_id=99, now=NOW)
        assert d.verdict is Verdict.RETAIL_BLOCKED


def test_ordinary_verdicts_are_not_marked():
    d = decide([], agency_id=1, agent_telegram_id=10, now=NOW)
    assert d.can_fix and not d.not_unique


# ===================== база: счёт, уведомления, срок =====================

def _log(db, verdict, created_at=None, agency_id=1, tid=AGENT):
    return db.log_fixation(
        digits="79991234567", client_name="Иван", agency_id=agency_id,
        agent_telegram_id=tid, verdict=verdict, amo_lead_id=1,
        created_at=created_at or NOW)


def test_marked_fixation_is_billed(tmp_path):
    import clients

    db = Db(tmp_path / "fixbot.db")
    _log(db, "retail_blocked")
    _log(db, "unique")
    db.conn.close()
    assert clients.billable_fixations(tmp_path, NOW - DAY, NOW + DAY) == 2


def test_marked_fixation_has_no_45_day_term(tmp_path):
    db = Db(tmp_path / "t.db")
    old = NOW - 44 * DAY
    marked = _log(db, "retail_blocked", created_at=old)
    plain = _log(db, "unique", created_at=old)
    ids = {r["id"] for r in db.expiring_fixations(45, 3, now=NOW)}
    assert plain in ids and marked not in ids


def test_marked_fixation_gets_no_retail_knock(tmp_path):
    """Клиент и так в прямом отделе — сообщать об этом агенту нечего."""
    db = Db(tmp_path / "k.db")
    marked = _log(db, "retail_blocked")
    plain = _log(db, "unique")
    ids = {r["id"] for r in db.fixations_awaiting_retail_check()}
    assert plain in ids and marked not in ids


def test_marked_fixation_holder_is_not_told_about_rivals(tmp_path):
    db = Db(tmp_path / "r.db")
    _log(db, "retail_blocked", agency_id=2, tid=600)
    _log(db, "unique", agency_id=3, tid=700)
    who = {r["agent_telegram_id"] for r in db.rivals_for("79991234567", 1)}
    assert who == {700}


# ===================== карточка и кнопка =====================

def _retail_env(b, monkeypatch, tmp_path):
    db, card, seen = _env(b, monkeypatch, tmp_path)

    async def find_all_matches(p):
        seen.card_during_crm.append((card.html_text, list(card.kb)))
        return [_retail()], None

    seen.rivals = []

    async def rivals(*a, **kw):
        seen.rivals.append(a)

    seen.payload_flags = []

    async def create_in_amo(payload, p, author_id, d=None):
        seen.payload_flags.append(bool(d and d.not_unique))
        seen.created += 1
        return {"contact_id": 11, "lead_id": 21, "agent_contact_id": None}

    monkeypatch.setattr(b, "find_all_matches", find_all_matches)
    monkeypatch.setattr(b, "notify_rivals_about_new", rivals)
    monkeypatch.setattr(b, "_create_in_amo", create_in_amo)
    return db, card, seen


async def test_card_offers_to_fix_a_retail_client(tmp_path, monkeypatch):
    import bot as b

    db, card, _ = _retail_env(b, monkeypatch, tmp_path)
    pid = _pending(db)
    await b._show_confirm(card, db.get_pending(pid))

    assert f"fix:{pid}" in card.kb
    assert "не уникальный" in card.html_text.lower()
    assert "прямом отделе продаж" in card.html_text
    assert db.get_pending(pid)["status"] == "waiting"


async def test_pressing_writes_a_marked_fixation(tmp_path, monkeypatch):
    import bot as b

    db, card, seen = _retail_env(b, monkeypatch, tmp_path)
    pid = _pending(db)
    await b.cb_confirm(_press(card, pid))

    assert seen.created == 1 and seen.payload_flags == [True]
    row = db.conn.execute("SELECT verdict, amo_lead_id FROM fixations"
                          ).fetchone()
    assert (row["verdict"], row["amo_lead_id"]) == ("retail_blocked", 21)
    assert "не уникальный" in card.html_text.lower()
    assert seen.rivals == []                 # конкурентам не сообщаем


async def test_unique_fixation_still_tells_rivals(tmp_path, monkeypatch):
    """Обычная фиксация — как раньше: конкуренты узнают."""
    import bot as b

    db, card, seen = _retail_env(b, monkeypatch, tmp_path)

    async def nothing_found(p):
        return [], None

    monkeypatch.setattr(b, "find_all_matches", nothing_found)
    pid = _pending(db)
    await b.cb_confirm(_press(card, pid))

    assert seen.payload_flags == [False]
    assert len(seen.rivals) == 1
    assert "не уникальный" not in card.html_text.lower()


def test_group_message_no_longer_refuses_a_retail_client():
    """
    В группе клиент прямого отдела идёт на карточку с кнопкой, а не в
    немедленный отказ с примечанием «отклонена» в CRM.
    """
    import inspect

    import bot as b

    src = inspect.getsource(b.on_message)
    assert "if not d.can_fix:" in src
    assert "if not d.creates_fixation:" not in src


# ===================== запись в CRM =====================

class FakeAmo:
    def __init__(self):
        self.leads: list[dict] = []
        self.notes: list[tuple] = []

    async def find_or_create_company(self, name):
        return 300

    async def find_or_create_agent(self, **kw):
        return 400

    async def link_entity(self, *a, **kw):
        pass

    async def create_contact(self, **kw):
        return 500

    async def create_lead(self, **kw):
        self.leads.append(kw)
        return 600

    async def add_note(self, entity, entity_id, text):
        self.notes.append((entity, entity_id, text))


async def _write(b, monkeypatch, tmp_path, matches):
    db = Db(tmp_path / "w.db")
    monkeypatch.setattr(b, "db", db)
    monkeypatch.setattr(b, "target_pipeline", lambda: (1, 2, "Агентская"))
    fake = FakeAmo()
    monkeypatch.setattr(b, "amo", fake)
    pid = _pending(db)
    payload = db.get_pending(pid)["payload"]
    d = decide(matches, agency_id=payload["agency_id"],
               agent_telegram_id=AGENT, now=NOW)
    import phones
    await b._create_in_amo(payload, phones.from_digits(payload["digits"]),
                           AGENT, d)
    return fake


async def test_marked_lead_has_the_tag_and_the_note(tmp_path, monkeypatch):
    import bot as b

    fake = await _write(b, monkeypatch, tmp_path, [_retail()])
    lead = fake.leads[0]
    assert "не уникальный" in lead["tags"]
    assert "telegram-фиксация" in lead["tags"]
    assert lead["pipeline_id"] == 1                  # агентская воронка
    note = next(t for e, _, t in fake.notes if e == "leads")
    assert note.startswith("⚠️ Не уникальный")
    assert "прямом отделе продаж" in note
    assert "отклонена" not in note


async def test_ordinary_lead_is_exactly_as_before(tmp_path, monkeypatch):
    import bot as b

    fake = await _write(b, monkeypatch, tmp_path, [])
    lead = fake.leads[0]
    assert lead["tags"] == ["telegram-фиксация"]
    note = next(t for e, _, t in fake.notes if e == "leads")
    assert note.startswith("Зафиксирован из Telegram")
