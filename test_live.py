"""Тесты слияния зеркала и живого поиска."""

import time

from db import Db, Match
from phones import normalize
from verdict import Verdict, decide

DAY = 86400
NOW = int(time.time())


def test_live_row_lands_in_mirror(tmp_path):
    """Найденный вживую контакт должен сохраниться с верным происхождением."""
    db = Db(tmp_path / "l1.db")
    db.upsert_from_live({
        "contact_id": 500, "name": "Свежий", "digits": "79171475214",
        "created_at": NOW, "has_retail": True, "last_retail_activity": NOW,
        "has_agency": False, "agency_company_id": None, "booked": False,
    })
    hits = db.find_matches(normalize("+79171475214"))
    assert len(hits) == 1
    assert hits[0].has_retail and hits[0].origin_known
    assert decide(hits, None, None, now=NOW).verdict is Verdict.RETAIL_BLOCKED


def test_live_row_does_not_invent_agency(tmp_path):
    """
    upsert_from_live не должен помечать контакт агентским «на всякий случай» —
    в отличие от add_contact_row, который для этого и создан.
    """
    db = Db(tmp_path / "l2.db")
    db.upsert_from_live({
        "contact_id": 501, "name": "Ничей", "digits": "79991112233",
        "has_retail": False, "has_agency": False,
        "last_retail_activity": None, "agency_company_id": None, "booked": False,
    })
    hits = db.find_matches(normalize("+79991112233"))
    assert not hits[0].has_agency and not hits[0].has_retail
    assert decide(hits, None, None, now=NOW).verdict is Verdict.UNKNOWN_ORIGIN


def test_live_overwrites_stale_mirror(tmp_path):
    """Живые данные свежее — они должны перебивать старую запись зеркала."""
    db = Db(tmp_path / "l3.db")
    db.replace_contacts([{"id": 502, "name": "Был ничей",
                          "phones": ["+79991112233"]}])
    db.replace_origins([{"contact_id": 502, "has_retail": 0, "has_agency": 0}])
    assert decide(db.find_matches(normalize("+79991112233")), None, None,
                  now=NOW).verdict is Verdict.UNKNOWN_ORIGIN

    # в CRM у него появилась розничная сделка
    db.upsert_from_live({
        "contact_id": 502, "name": "Стал розничным", "digits": "79991112233",
        "has_retail": True, "last_retail_activity": NOW - DAY,
        "has_agency": False, "agency_company_id": None, "booked": False,
    })
    assert decide(db.find_matches(normalize("+79991112233")), None, None,
                  now=NOW).verdict is Verdict.RETAIL_BLOCKED


def test_live_masked_number_still_matches(tmp_path):
    """Контакт, добавленный живым поиском, ловится по маскированному номеру."""
    db = Db(tmp_path / "l4.db")
    db.upsert_from_live({
        "contact_id": 503, "name": "Полный", "digits": "79171475214",
        "has_retail": True, "last_retail_activity": NOW,
        "has_agency": False, "agency_company_id": None, "booked": False,
    })
    hits = db.find_matches(normalize("+7 917 147-52-**"))
    assert len(hits) == 1 and hits[0].contact_id == 503


def test_merge_dedupes_by_contact_id():
    """Один контакт из зеркала и из живого поиска не должен задваиваться."""
    mirror = [Match(digits="79171475214", contact_id=600, source="amo")]
    live_row = {"contact_id": 600, "name": "Он же", "digits": "79171475214",
                "has_retail": True, "last_retail_activity": NOW,
                "has_agency": False, "agency_company_id": None,
                "booked": False, "created_at": NOW}

    by_contact = {m.contact_id: m for m in mirror
                  if m.source == "amo" and m.contact_id}
    by_contact[live_row["contact_id"]] = Match(
        digits=live_row["digits"], name=live_row["name"], source="amo",
        contact_id=live_row["contact_id"], has_retail=live_row["has_retail"],
        last_retail_activity=live_row["last_retail_activity"],
        origin_known=True,
    )
    merged = [m for m in mirror if m.source != "amo" or not m.contact_id]
    merged += list(by_contact.values())

    assert len(merged) == 1
    assert merged[0].has_retail          # победила живая версия
