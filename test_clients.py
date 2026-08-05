"""Сводка оператора по всем клиентам."""

import time

import clients as cl
from db import Db

NOW = int(time.time())
DAY = 86400


def _make_client(root, slug, developer, fixations=0, agents=0, chats=0,
                 synced_ago=None):
    folder = root / slug
    folder.mkdir(parents=True)
    (folder / ".env").write_text(
        f"TELEGRAM_TOKEN=secret-token-do-not-read\n"
        f"AMO_LONG_TOKEN=secret\n"
        f"DEVELOPER_NAME={developer}\n", encoding="utf-8")

    db = Db(folder / "fixbot.db")
    db.upsert_account(f"{slug}crm")
    aid = db.create_agency("ERA B", "era b")
    for i in range(fixations):
        db.log_fixation(digits=f"799912345{i % 10}", client_name="Клиент",
                        agency_id=aid, agent_telegram_id=100 + i,
                        verdict="unique", amo_lead_id=900 + i,
                        created_at=NOW - i * DAY)
    for i in range(agents):
        db.upsert_agent(200 + i, f"u{i}", f"Agent {i}", dm_open=(i == 0))
    for i in range(chats):
        db.set_meta(f"chat_agency:-{100 + i}", str(aid))
    if synced_ago is not None:
        db.set_meta("contacts_synced_at", str(NOW - synced_ago))
    db.conn.close()
    return folder


# ===================== чтение =====================

def test_scan_finds_clients(tmp_path):
    _make_client(tmp_path, "breig", "BREIG", fixations=3, agents=2, chats=1,
                 synced_ago=600)
    _make_client(tmp_path, "friend", "Ромашка", fixations=1)

    got = cl.scan(tmp_path)
    assert [c.slug for c in got] == ["breig", "friend"]
    assert [c.name for c in got] == ["BREIG", "Ромашка"]


def test_client_metrics(tmp_path):
    _make_client(tmp_path, "breig", "BREIG", fixations=5, agents=3, chats=2,
                 synced_ago=60)
    c = cl.scan(tmp_path)[0]

    assert c.fixations_total == 5
    assert c.fixations_7 == 5          # созданы в последние дни
    assert c.agents == 3
    assert c.agents_subscribed == 1
    assert c.chats == 2
    assert c.agencies == 1
    assert c.subdomain == "breigcrm"


def test_fixations_by_period(tmp_path):
    """Старые фиксации не должны попадать в недельный срез."""
    folder = _make_client(tmp_path, "breig", "BREIG")
    db = Db(folder / "fixbot.db")
    db.log_fixation(digits="79991234567", verdict="unique", amo_lead_id=1,
                    created_at=NOW - 2 * DAY)
    db.log_fixation(digits="79991234568", verdict="unique", amo_lead_id=2,
                    created_at=NOW - 40 * DAY)
    db.conn.close()

    c = cl.scan(tmp_path)[0]
    assert c.fixations_total == 2
    assert c.fixations_30 == 1
    assert c.fixations_7 == 1


def test_rejected_not_counted(tmp_path):
    folder = _make_client(tmp_path, "breig", "BREIG")
    db = Db(folder / "fixbot.db")
    db.log_fixation(digits="79991234567", verdict="retail_blocked",
                    amo_lead_id=None)
    db.conn.close()
    assert cl.scan(tmp_path)[0].fixations_total == 0


def test_missing_db_reported(tmp_path):
    (tmp_path / "broken").mkdir()
    c = cl.scan(tmp_path)[0]
    assert c.error == "база не найдена"
    assert c.alive_hint == "❓"


def test_no_developer_name_falls_back_to_folder(tmp_path):
    folder = tmp_path / "noname"
    folder.mkdir()
    Db(folder / "fixbot.db").conn.close()
    assert cl.scan(tmp_path)[0].name == "noname"


def test_scan_missing_dir(tmp_path):
    assert cl.scan(tmp_path / "нет-такой") == []


def test_env_read_takes_only_developer_name(tmp_path):
    """
    Из .env читаем одно поле. Там лежат токены, и тащить их в память
    без нужды незачем.
    """
    folder = _make_client(tmp_path, "breig", "BREIG")
    c = cl.scan(tmp_path)[0]
    dumped = repr(c)
    assert "secret" not in dumped
    assert "BREIG" in dumped


# ===================== признак живости =====================

def test_alive_hint_by_sync_age(tmp_path):
    _make_client(tmp_path, "fresh", "Свежий", synced_ago=600)
    _make_client(tmp_path, "stale", "Вчерашний", synced_ago=10 * 3600)
    _make_client(tmp_path, "dead", "Мёртвый", synced_ago=5 * DAY)
    _make_client(tmp_path, "never", "Без синка")

    by_slug = {c.slug: c.alive_hint for c in cl.scan(tmp_path)}
    assert by_slug["fresh"] == "🟢"
    assert by_slug["stale"] == "🟡"
    assert by_slug["dead"] == "🔴"
    assert by_slug["never"] == "⚠️"


# ===================== тексты =====================

def test_overview_sums_across_clients(tmp_path):
    _make_client(tmp_path, "a", "Первый", fixations=3, agents=2, synced_ago=60)
    _make_client(tmp_path, "b", "Второй", fixations=2, agents=1, synced_ago=60)

    out = cl.overview_text(cl.scan(tmp_path), str(tmp_path))
    assert "Клиентов: <b>2</b>" in out
    assert "Первый" in out and "Второй" in out
    assert "5" in out          # суммарные фиксации


def test_overview_explains_empty_setup():
    out = cl.overview_text([], "")
    assert "CLIENTS_DIR" in out
    assert "fixbot.db" in out


def test_client_text_renders(tmp_path):
    _make_client(tmp_path, "breig", "BREIG", fixations=2, chats=1,
                 synced_ago=60)
    out = cl.client_text(cl.scan(tmp_path)[0])
    assert "BREIG" in out
    assert "breigcrm" in out
    assert "{" not in out


def test_client_text_shows_error(tmp_path):
    (tmp_path / "broken").mkdir()
    out = cl.client_text(cl.scan(tmp_path)[0])
    assert "⚠️" in out


def test_read_only_does_not_write(tmp_path):
    """Базу пишет чужой процесс — открывать её можно только на чтение."""
    folder = _make_client(tmp_path, "breig", "BREIG", fixations=1)
    db_file = folder / "fixbot.db"
    before = db_file.stat().st_mtime

    cl.scan(tmp_path)
    assert db_file.stat().st_mtime == before


import sqlite3, time
from pathlib import Path

import clients as cl

DAY = 86400


def _make(tmp_path, rows):
    folder = tmp_path / "eco"; folder.mkdir()
    c = sqlite3.connect(folder / "fixbot.db")
    c.execute("CREATE TABLE fixations (id INTEGER PRIMARY KEY,"
              " amo_lead_id INTEGER, created_at INTEGER)")
    c.executemany("INSERT INTO fixations (amo_lead_id, created_at) VALUES (?,?)", rows)
    c.commit(); c.close()
    return folder


def test_only_fixations_written_to_crm_are_billed(tmp_path):
    """Проверки без подтверждения агентом клиент оплачивать не должен."""
    now = int(time.time())
    folder = _make(tmp_path, [
        (555, now - DAY), (556, now - 2 * DAY),   # попали в CRM
        (None, now - DAY), (None, now - DAY),     # только проверка
    ])
    assert cl.billable_fixations(folder, now - 30 * DAY, now + 1) == 2


def test_period_bounds_are_respected(tmp_path):
    now = int(time.time())
    folder = _make(tmp_path, [(1, now - 40 * DAY), (2, now - 5 * DAY)])
    assert cl.billable_fixations(folder, now - 30 * DAY, now + 1) == 1


def test_upper_bound_is_exclusive(tmp_path):
    """Иначе фиксация на границе попадёт в оба месяца и её оплатят дважды."""
    now = int(time.time())
    folder = _make(tmp_path, [(1, now)])
    assert cl.billable_fixations(folder, now - DAY, now) == 0
    assert cl.billable_fixations(folder, now - DAY, now + 1) == 1


def test_missing_database_is_not_a_crash(tmp_path):
    empty = tmp_path / "new"; empty.mkdir()
    assert cl.billable_fixations(empty, 0, 2 ** 31) == 0


def test_pause_marker_round_trip(tmp_path):
    folder = tmp_path / "eco"; folder.mkdir()
    assert not cl.is_paused(folder)
    cl.set_paused(folder, True, "не оплачено")
    assert cl.is_paused(folder)
    assert "не оплачено" in (folder / "PAUSED").read_text(encoding="utf-8")
    cl.set_paused(folder, False)
    assert not cl.is_paused(folder)


def test_unpausing_twice_is_harmless(tmp_path):
    folder = tmp_path / "eco"; folder.mkdir()
    cl.set_paused(folder, False)
    cl.set_paused(folder, False)
    assert not cl.is_paused(folder)
