"""Тесты семи сценариев, происхождения клиента и справочника агентств."""

import time

import pytest

import agencies as ag
import texts
from amo import compute_origins
from db import Db, Match
from phones import normalize
from verdict import Verdict, decide

DAY = 86400
NOW = 1_770_000_000          # фиксированное «сейчас» для повторяемости


def amo_match(**kw) -> Match:
    kw.setdefault("digits", "79991234567")
    kw.setdefault("source", "amo")
    kw.setdefault("origin_known", True)
    return Match(**kw)


def chat_match(**kw) -> Match:
    kw.setdefault("digits", "79991234567")
    kw.setdefault("source", "chat")
    kw.setdefault("has_agency", True)
    kw.setdefault("origin_known", True)
    return Match(**kw)


# ======================= семь сценариев =======================

def test_unique():
    d = decide([], agency_id=1, agent_telegram_id=10, now=NOW)
    assert d.verdict is Verdict.UNIQUE
    assert d.creates_fixation


def test_retail_blocked():
    """Клиент отдела продаж с недавней активностью — отказ."""
    m = amo_match(has_retail=True, last_retail_activity=NOW - 30 * DAY)
    d = decide([m], agency_id=1, agent_telegram_id=10, now=NOW)
    assert d.verdict is Verdict.RETAIL_BLOCKED
    assert not d.creates_fixation


def test_retail_expired_after_year():
    """Тот же клиент, но без движения больше года — освободился."""
    m = amo_match(has_retail=True, last_retail_activity=NOW - 400 * DAY)
    d = decide([m], agency_id=1, agent_telegram_id=10, now=NOW)
    assert d.verdict is Verdict.RETAIL_EXPIRED
    assert d.creates_fixation
    assert d.notifies_admin


def test_retail_boundary_364_vs_366():
    just_inside = amo_match(has_retail=True, last_retail_activity=NOW - 364 * DAY)
    just_outside = amo_match(has_retail=True, last_retail_activity=NOW - 366 * DAY)
    assert decide([just_inside], 1, 10, now=NOW).verdict is Verdict.RETAIL_BLOCKED
    assert decide([just_outside], 1, 10, now=NOW).verdict is Verdict.RETAIL_EXPIRED


def test_retail_without_date_is_blocked():
    """Розничная сделка есть, даты нет — осторожничаем и блокируем."""
    m = amo_match(has_retail=True, last_retail_activity=None)
    assert decide([m], 1, 10, now=NOW).verdict is Verdict.RETAIL_BLOCKED


def test_other_agency_does_not_block():
    """Главное правило: чужая агентская фиксация НЕ блокирует."""
    m = chat_match(agency_id=2, created_at=NOW - 12 * DAY)
    d = decide([m], agency_id=1, agent_telegram_id=10, now=NOW)
    assert d.verdict is Verdict.OTHER_AGENCY
    assert d.creates_fixation
    assert d.other_agency_since == NOW - 12 * DAY


def test_same_agent():
    m = chat_match(agency_id=1, agent_telegram_id=10, created_at=NOW - 5 * DAY)
    d = decide([m], agency_id=1, agent_telegram_id=10, now=NOW)
    assert d.verdict is Verdict.SAME_AGENT
    assert not d.creates_fixation


def test_same_agency_other_agent():
    m = chat_match(agency_id=1, agent_telegram_id=99, agent_name="Иван",
                   created_at=NOW - 5 * DAY)
    d = decide([m], agency_id=1, agent_telegram_id=10, now=NOW)
    assert d.verdict is Verdict.SAME_AGENCY
    assert d.colleague == "Иван"
    assert not d.creates_fixation


def test_booked_elsewhere():
    m = amo_match(has_agency=True, booked=True, agency_id=2)
    d = decide([m], agency_id=1, agent_telegram_id=10, now=NOW)
    assert d.verdict is Verdict.BOOKED_ELSEWHERE
    assert not d.creates_fixation


def test_own_booking_does_not_block():
    """Своя же бронь не должна выглядеть как чужая."""
    m = amo_match(has_agency=True, booked=True, agency_id=1)
    d = decide([m], agency_id=1, agent_telegram_id=10, now=NOW)
    assert d.verdict is not Verdict.BOOKED_ELSEWHERE


def test_unknown_origin():
    """Контакт в базе есть, сделок нет — непонятно, чей."""
    m = amo_match(has_retail=False, has_agency=False)
    d = decide([m], agency_id=1, agent_telegram_id=10, now=NOW)
    assert d.verdict is Verdict.UNKNOWN_ORIGIN
    assert not d.creates_fixation
    assert d.notifies_admin


# ======================= приоритеты =======================

def test_retail_beats_agency():
    """Розница блокирует, даже если рядом есть агентские фиксации."""
    retail = amo_match(has_retail=True, last_retail_activity=NOW - DAY)
    agency = chat_match(agency_id=2, created_at=NOW - 10 * DAY)
    d = decide([retail, agency], agency_id=1, agent_telegram_id=10, now=NOW)
    assert d.verdict is Verdict.RETAIL_BLOCKED


def test_expired_retail_plus_other_agency():
    """Розница остыла, но есть чужая фиксация — показываем её, фиксируем."""
    retail = amo_match(has_retail=True, last_retail_activity=NOW - 400 * DAY)
    agency = chat_match(agency_id=2, created_at=NOW - 10 * DAY)
    d = decide([retail, agency], agency_id=1, agent_telegram_id=10, now=NOW)
    assert d.verdict is Verdict.OTHER_AGENCY
    assert d.creates_fixation


def test_multiple_other_agencies_counted():
    ms = [chat_match(agency_id=2, created_at=NOW - 20 * DAY),
          chat_match(agency_id=3, created_at=NOW - 10 * DAY)]
    d = decide(ms, agency_id=1, agent_telegram_id=10, now=NOW)
    assert d.verdict is Verdict.OTHER_AGENCY
    assert d.other_agency_count == 2
    assert d.other_agency_since == NOW - 20 * DAY


def test_own_takes_priority_over_other():
    """Если есть и своя, и чужая фиксация — говорим про свою."""
    own = chat_match(agency_id=1, agent_telegram_id=10, created_at=NOW - 3 * DAY)
    other = chat_match(agency_id=2, created_at=NOW - 20 * DAY)
    d = decide([own, other], agency_id=1, agent_telegram_id=10, now=NOW)
    assert d.verdict is Verdict.SAME_AGENT


# ======================= тексты =======================

@pytest.mark.parametrize("v", list(Verdict))
def test_every_verdict_has_text(v):
    """Ни один вердикт не должен остаться без ответа в чат."""
    from verdict import Decision

    d = Decision(v)
    d.retail_activity = NOW - 400 * DAY
    d.other_agency_since = NOW - 10 * DAY
    d.own_since = NOW - 5 * DAY
    out = texts.render(d, client="Тестов Тест", p=normalize("+79991234567"),
                       agency="Дом+", contact_url="http://x", lead_url="http://y")
    assert out and len(out) > 20


def test_masked_note_appears():
    from verdict import Decision

    out = texts.render(Decision(Verdict.RETAIL_BLOCKED),
                       client="Т", p=normalize("+7 999 123-45-**"), agency=None)
    assert "Номер неполный" in out
    assert "не хватает 2 последних цифр" in out


@pytest.mark.parametrize("v", list(Verdict))
def test_texts_survive_without_links(v):
    """В режиме наблюдения ссылок нет — пустых <a href=""> быть не должно."""
    from verdict import Decision

    out = texts.render(Decision(v), client="Тестов", p=normalize("+79991234567"),
                       agency="Дом+", contact_url=None, lead_url=None)
    assert 'href=""' not in out
    assert out.strip() == out


def test_no_masked_note_for_full_number():
    from verdict import Decision

    out = texts.render(Decision(Verdict.RETAIL_BLOCKED),
                       client="Т", p=normalize("+79991234567"), agency=None)
    assert "Номер неполный" not in out


# ======================= происхождение из сделок =======================

def test_compute_origins_splits_by_pipeline():
    leads = [
        {"id": 1, "pipeline_id": 100, "status_id": 1, "updated_at": NOW - DAY,
         "contact_ids": [10], "company_id": None},
        {"id": 2, "pipeline_id": 200, "status_id": 5, "updated_at": NOW - 2 * DAY,
         "contact_ids": [20], "company_id": 777},
    ]
    kinds = {100: "retail", 200: "agency"}
    rows = {r["contact_id"]: r for r in compute_origins(leads, kinds)}

    assert rows[10]["has_retail"] == 1 and rows[10]["has_agency"] == 0
    assert rows[20]["has_agency"] == 1 and rows[20]["has_retail"] == 0
    assert rows[20]["agency_company_id"] == 777


def test_compute_origins_takes_latest_activity():
    leads = [
        {"id": 1, "pipeline_id": 100, "updated_at": NOW - 400 * DAY,
         "contact_ids": [10]},
        {"id": 2, "pipeline_id": 100, "updated_at": NOW - 3 * DAY,
         "contact_ids": [10]},
    ]
    rows = compute_origins(leads, {100: "retail"})
    assert rows[0]["last_retail_activity"] == NOW - 3 * DAY


def test_compute_origins_marks_booking():
    leads = [{"id": 1, "pipeline_id": 200, "status_id": 42,
              "updated_at": NOW, "contact_ids": [10], "company_id": 5}]
    rows = compute_origins(leads, {200: "agency"}, booking_status_ids={42})
    assert rows[0]["booked"] == 1


def test_compute_origins_skips_ignored_pipeline():
    leads = [{"id": 1, "pipeline_id": 300, "updated_at": NOW,
              "contact_ids": [10]}]
    assert compute_origins(leads, {300: "ignore"}) == []


# ======================= справочник агентств =======================

@pytest.mark.parametrize("raw,expected", [
    ("Дом+", "дом+"),
    ('ООО "Дом Плюс"', "дом+"),
    ("АН ДОМ +", "дом+"),
    ("дом плюс", "дом+"),
    ("  Дом   Плюс  ", "дом+"),
    ("Агентство недвижимости Новосёл", "новосел"),
    ("ИП Иванов", "иванов"),
])
def test_agency_normalization(raw, expected):
    assert ag.norm_agency(raw) == expected


def test_agency_resolve_exact():
    known = [{"name": "Дом+", "norm": "дом+", "agency_id": 1}]
    res = ag.resolve('ООО "Дом Плюс"', known)
    assert res.status == "exact"
    assert res.best.agency_id == 1


def test_agency_yo_equals_e():
    """«Новосёл» и «Новосел» должны схлопываться в одно агентство."""
    known = [{"name": "Новосёл", "norm": ag.norm_agency("Новосёл"),
              "agency_id": 1}]
    res = ag.resolve("АН Новосел", known)
    assert res.status == "exact"
    assert res.best.agency_id == 1


def test_agency_resolve_unknown_is_new():
    known = [{"name": "Дом+", "norm": "дом+", "agency_id": 1}]
    res = ag.resolve("Этажи", known)
    assert res.status == "new"
    assert res.candidates == []


def test_agency_latin_lookalike():
    """«Дом» с латинской o и «Дом» кириллицей — одно агентство."""
    assert ag.norm_agency("Дoм+") == ag.norm_agency("Дом+")


# ======================= база =======================

def test_db_match_carries_origin(tmp_path):
    db = Db(tmp_path / "t.db")
    db.replace_contacts([
        {"id": 1, "name": "Иванов", "phones": ["+7 999 123-45-67"],
         "created_at": NOW - 500 * DAY},
    ])
    db.replace_origins([
        {"contact_id": 1, "has_retail": 1,
         "last_retail_activity": NOW - 400 * DAY},
    ])
    hits = db.find_matches(normalize("+7 999 123-45-**"))
    assert len(hits) == 1
    assert hits[0].has_retail and hits[0].origin_known

    d = decide(hits, agency_id=1, agent_telegram_id=10, now=NOW)
    assert d.verdict is Verdict.RETAIL_EXPIRED


def test_db_contact_without_origin_is_unknown(tmp_path):
    db = Db(tmp_path / "t2.db")
    db.replace_contacts([
        {"id": 5, "name": "Безымянный", "phones": ["+7 916 445-22-31"]},
    ])
    hits = db.find_matches(normalize("+7 916 445-22-31"))
    d = decide(hits, agency_id=1, agent_telegram_id=10, now=NOW)
    assert d.verdict is Verdict.UNKNOWN_ORIGIN


def test_db_agency_dictionary(tmp_path):
    db = Db(tmp_path / "t3.db")
    aid = db.create_agency("Дом+", "дом+", amo_company_id=777)
    db.add_agency_alias(aid, "дом плюс")

    assert db.find_agency_by_norm("дом+")["id"] == aid
    assert db.find_agency_by_norm("дом плюс")["id"] == aid
    assert db.agency_by_company_id(777)["id"] == aid
    # повторное создание не плодит дубли
    assert db.create_agency("Дом+", "дом+") == aid


def test_db_pipelines_config(tmp_path):
    db = Db(tmp_path / "t4.db")
    assert not db.is_configured()
    db.replace_pipelines([
        {"id": 100, "name": "Розница", "statuses": [{"id": 1, "name": "Новая"}]},
        {"id": 200, "name": "Агентские", "statuses": [{"id": 5, "name": "Бронь"}]},
    ])
    db.set_pipeline_kind(100, "retail")
    db.set_pipeline_kind(200, "agency")
    assert db.is_configured()
    assert db.pipeline_kinds() == {100: "retail", 200: "agency"}

    db.set_booking_status(200, 5)
    assert db.booking_status_ids() == {5}


def test_db_fixation_participates_in_matching(tmp_path):
    """Фиксация из чата должна находиться следующим агентством."""
    db = Db(tmp_path / "t5.db")
    db.log_fixation(digits="799912345", client_name="Петров", agency_id=2,
                    agent_telegram_id=55, agent_name="Аня",
                    verdict="unique", amo_contact_id=9, amo_lead_id=99)
    hits = db.find_matches(normalize("+7 999 123-45-67"))
    assert len(hits) == 1 and hits[0].source == "chat"

    d = decide(hits, agency_id=1, agent_telegram_id=10, now=NOW)
    assert d.verdict is Verdict.OTHER_AGENCY


def test_db_rejected_fixation_not_matched(tmp_path):
    """Отклонённая попытка не должна считаться существующей фиксацией."""
    db = Db(tmp_path / "t6.db")
    db.log_fixation(digits="799912345", client_name="Петров", agency_id=2,
                    verdict="retail_blocked", amo_lead_id=None)
    assert db.find_matches(normalize("+7 999 123-45-67")) == []
