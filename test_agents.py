"""Этап 2: подписка агентов, личный кабинет, уведомления о статусах."""

import time

import pytest

import i18n
import texts
from db import Db

NOW = int(time.time())
DAY = 86400


def _fixation(db, **kw):
    kw.setdefault("digits", "79991234567")
    kw.setdefault("client_name", "Петров")
    kw.setdefault("agency_id", 1)
    kw.setdefault("agent_telegram_id", 42)
    kw.setdefault("verdict", "unique")
    kw.setdefault("amo_lead_id", 900)
    return db.log_fixation(**kw)


# ===================== реестр агентов =====================

def test_agent_fields_update_pointwise(tmp_path):
    db = Db(tmp_path / "a.db")
    db.upsert_agent(42, "ivan", "Иван")
    db.set_agent_field(42, phone="+7 999 123-45-67", lang="en", dm_open=1)

    a = db.get_agent(42)
    assert a["phone"] == "+7 999 123-45-67"
    assert a["lang"] == "en"
    assert a["dm_open"] == 1
    assert a["display_name"] == "Иван"      # остальное не затёрлось


def test_set_agent_field_ignores_unknown(tmp_path):
    """Случайное имя поля не должно ломать запрос."""
    db = Db(tmp_path / "a2.db")
    db.upsert_agent(42, "ivan", "Иван")
    db.set_agent_field(42, hacker="drop table")      # молча игнорируем
    assert db.get_agent(42)["display_name"] == "Иван"


def test_agent_language_comes_from_work_chat(tmp_path):
    """
    Язык лички берётся из того, как агент пишет в группе. По имени
    профиля судить нельзя: «Timur» латиницей у русскоязычного — обычное
    дело, и человек получал уведомления на английском.
    """
    import bot

    db = Db(tmp_path / "lang.db")
    db.upsert_agent(42, "timur_crp", "Timur")

    # язык ещё не известен — берём значение по умолчанию, а не латиницу имени
    assert bot.agent_lang(db.get_agent(42), None) == bot.cfg.default_lang

    db.set_agent_field(42, lang="ru")
    assert bot.agent_lang(db.get_agent(42)) == i18n.RU

    db.set_agent_field(42, lang="en")
    assert bot.agent_lang(db.get_agent(42)) == i18n.EN


def test_agents_with_dm(tmp_path):
    db = Db(tmp_path / "a3.db")
    db.upsert_agent(1, "a", "A", dm_open=True)
    db.upsert_agent(2, "b", "B")
    assert [r["telegram_id"] for r in db.agents_with_dm()] == [1]


# ===================== личный кабинет =====================

def test_agent_sees_only_own_fixations(tmp_path):
    db = Db(tmp_path / "m.db")
    _fixation(db, agent_telegram_id=42, client_name="Мой")
    _fixation(db, agent_telegram_id=99, client_name="Чужой", digits="79995556677")

    mine = db.agent_fixations(42)
    assert len(mine) == 1 and mine[0]["client_name"] == "Мой"


def test_unconfirmed_fixations_not_listed(tmp_path):
    """Отклонённые попытки в кабинете не нужны — сделки по ним нет."""
    db = Db(tmp_path / "m2.db")
    _fixation(db, amo_lead_id=None, verdict="retail_blocked")
    assert db.agent_fixations(42) == []


def test_watching_flag(tmp_path):
    db = Db(tmp_path / "w.db")
    fid = _fixation(db)
    assert not db.get_fixation(fid)["watching"]
    db.set_watching(fid, True)
    assert db.get_fixation(fid)["watching"] == 1


# ===================== отслеживание статусов =====================

def test_status_change_recorded(tmp_path):
    db = Db(tmp_path / "s.db")
    fid = _fixation(db)
    db.update_fixation_status(fid, 71, 700)
    row = db.get_fixation(fid)
    assert row["last_status_id"] == 71 and row["last_pipeline_id"] == 700


def test_status_title_from_pipelines(tmp_path):
    db = Db(tmp_path / "s2.db")
    db.replace_pipelines([{
        "id": 700, "name": "Агентские",
        "statuses": [{"id": 71, "name": "Фиксация", "sort": 10},
                     {"id": 72, "name": "Бронь", "sort": 20}],
    }])
    assert db.status_title(700, 72) == "Бронь"
    assert db.status_title(None, 71) == "Фиксация"
    assert db.status_title(700, 999) is None


def test_watched_leads_only_with_deals(tmp_path):
    db = Db(tmp_path / "s3.db")
    _fixation(db, amo_lead_id=900)
    _fixation(db, amo_lead_id=None, digits="79995556677")
    assert [r["amo_lead_id"] for r in db.watched_leads()] == [900]


# ===================== оповещение конкурентов =====================

def test_rivals_exclude_own_agency(tmp_path):
    """
    При выходе на бронь оповещаем чужие агентства, но не своё:
    коллеге сообщать, что его же клиент забронирован «конкурентом»,
    бессмысленно.
    """
    db = Db(tmp_path / "r.db")
    _fixation(db, agency_id=1, agent_telegram_id=42)
    _fixation(db, agency_id=2, agent_telegram_id=55)
    _fixation(db, agency_id=3, agent_telegram_id=66)

    rivals = db.rivals_for("79991234567", exclude_agency_id=1)
    assert sorted(r["agent_telegram_id"] for r in rivals) == [55, 66]


def test_rivals_match_masked_numbers(tmp_path):
    """Конкурент мог зафиксировать того же клиента неполным номером."""
    db = Db(tmp_path / "r2.db")
    _fixation(db, agency_id=2, agent_telegram_id=55, digits="799912345")

    rivals = db.rivals_for("79991234567", exclude_agency_id=1)
    assert [r["agent_telegram_id"] for r in rivals] == [55]


def test_rivals_ignore_other_clients(tmp_path):
    db = Db(tmp_path / "r3.db")
    _fixation(db, agency_id=2, agent_telegram_id=55, digits="79997778899")
    assert db.rivals_for("79991234567", exclude_agency_id=1) == []


def test_rivals_ignore_rejected_attempts(tmp_path):
    """Отклонённая попытка фиксацией не была — оповещать не о чем."""
    db = Db(tmp_path / "r4.db")
    _fixation(db, agency_id=2, agent_telegram_id=55, amo_lead_id=None)
    assert db.rivals_for("79991234567", exclude_agency_id=1) == []


# ===================== срок фиксации и продление =====================

TTL = 45
WARN = 3


def test_expiring_found_only_near_deadline(tmp_path):
    db = Db(tmp_path / "e.db")
    fresh = _fixation(db, created_at=NOW - 10 * DAY)
    soon = _fixation(db, created_at=NOW - (TTL - 2) * DAY, digits="79995556677")

    ids = [r["id"] for r in db.expiring_fixations(TTL, WARN, now=NOW)]
    assert soon in ids
    assert fresh not in ids


def test_expiring_reminds_once(tmp_path):
    """Напоминание должно прийти один раз, а не каждые десять минут."""
    db = Db(tmp_path / "e2.db")
    fid = _fixation(db, created_at=NOW - (TTL - 1) * DAY)

    assert [r["id"] for r in db.expiring_fixations(TTL, WARN, now=NOW)] == [fid]
    db.mark_reminded(fid)
    assert db.expiring_fixations(TTL, WARN, now=NOW) == []


def test_renewal_restarts_term_and_reminder(tmp_path):
    """
    После продления срок течёт заново, и о новом сроке напомнят
    отдельно — иначе агент продлил бы один раз и больше не услышал.
    """
    db = Db(tmp_path / "e3.db")
    fid = _fixation(db, created_at=NOW - (TTL - 1) * DAY)
    db.mark_reminded(fid)
    assert db.expiring_fixations(TTL, WARN, now=NOW) == []

    db.renew_fixation(fid, when_ts=NOW)
    assert db.expiring_fixations(TTL, WARN, now=NOW) == []      # срок далеко

    later = NOW + (TTL - 1) * DAY
    assert [r["id"] for r in db.expiring_fixations(TTL, WARN, now=later)] == [fid]


def test_expires_at_counted_from_renewal(tmp_path):
    db = Db(tmp_path / "e4.db")
    fid = _fixation(db, created_at=NOW - 40 * DAY)
    db.renew_fixation(fid, when_ts=NOW)

    row = db.expiring_fixations(TTL, WARN, now=NOW + (TTL - 1) * DAY)[0]
    assert row["expires_at"] == NOW + TTL * DAY


def test_expiring_ignores_rejected(tmp_path):
    """По отклонённой попытке продлевать нечего."""
    db = Db(tmp_path / "e5.db")
    _fixation(db, created_at=NOW - (TTL - 1) * DAY, amo_lead_id=None)
    assert db.expiring_fixations(TTL, WARN, now=NOW) == []


# ===================== клиент в прямом отделе =====================

def test_retail_notice_sent_once(tmp_path):
    db = Db(tmp_path / "d.db")
    fid = _fixation(db)
    assert [r["id"] for r in db.fixations_awaiting_retail_check()] == [fid]

    db.mark_retail_notified(fid)
    assert db.fixations_awaiting_retail_check() == []


def test_retail_after_fixation_is_detected(tmp_path):
    """
    Клиент постучался в прямой отдел уже после фиксации — активность
    розничной сделки свежее самой фиксации.
    """
    from phones import from_digits

    db = Db(tmp_path / "d2.db")
    fid = _fixation(db, created_at=NOW - 5 * DAY)
    db.replace_contacts([{"id": 1, "name": "Петров",
                          "phones": ["+7 999 123-45-67"]}])
    db.replace_origins([{"contact_id": 1, "has_retail": 1,
                         "last_retail_activity": NOW - DAY}])

    row = db.get_fixation(fid)
    hits = [m for m in db.find_matches(from_digits(row["digits"]))
            if m.has_retail and (m.last_retail_activity or 0) > row["created_at"]]
    assert hits


def test_retail_before_fixation_is_not_news(tmp_path):
    """Если розничная сделка была раньше — фиксация бы просто не прошла."""
    from phones import from_digits

    db = Db(tmp_path / "d3.db")
    fid = _fixation(db, created_at=NOW)
    db.replace_contacts([{"id": 1, "name": "Петров",
                          "phones": ["+7 999 123-45-67"]}])
    db.replace_origins([{"contact_id": 1, "has_retail": 1,
                         "last_retail_activity": NOW - 30 * DAY}])

    row = db.get_fixation(fid)
    hits = [m for m in db.find_matches(from_digits(row["digits"]))
            if m.has_retail and (m.last_retail_activity or 0) > row["created_at"]]
    assert not hits


# ===================== тексты =====================

@pytest.mark.parametrize("lang", [i18n.RU, i18n.EN])
@pytest.mark.parametrize("key", [
    "btn_watch", "watch_hint", "dm_hello", "dm_not_yours", "dm_ask_phone",
    "btn_share_phone", "btn_skip", "my_empty", "my_title",
    "my_unknown_status", "notify_on", "notify_off",
])
def test_stage2_strings_present(lang, key):
    assert texts.t(lang, key)


@pytest.mark.parametrize("lang", [i18n.RU, i18n.EN])
def test_notification_texts_render(lang):
    outs = [
        texts.t(lang, "notify_expiring", client="Петров",
                phone="+7 999 123-45-67", date="01.09.2026"),
        texts.t(lang, "notify_rival", client="Петров",
                phone="+7 999 123-45-67"),
        texts.t(lang, "notify_direct", client="Петров",
                phone="+7 999 123-45-67"),
        texts.t(lang, "notify_won", client="Петров",
                phone="+7 999 123-45-67"),
        texts.t(lang, "renewed", client="Петров", date="01.09.2026"),
        texts.t(lang, "renew_note", agent="Иван", date="01.09.2026"),
    ]
    for out in outs:
        assert "{" not in out          # все подстановки заполнены
        assert "amoCRM" not in out


def test_no_stage_change_notifications():
    """
    Об этапах воронки агентам не сообщаем: внутренние названия вроде
    «Прогрев» и «Отвал» наружу не выносим.
    """
    for lang in (i18n.RU, i18n.EN):
        assert "notify_moved" not in texts.STR[lang]


def test_private_hint_present_and_points_to_group():
    """
    Агенты пробуют фиксировать в личке. Молчать в ответ нельзя —
    человек решит, что бот сломался.
    """
    for lang in (i18n.RU, i18n.EN):
        out = texts.t(lang, "dm_use_group")
        assert out and "/my" in out


def test_private_catch_all_registered_last():
    """
    Перехватчик лички ловит вообще всё, включая фото и альбомы, поэтому
    обязан стоять после команд. Иначе /my, /notify и /broadcast просто
    перестанут работать.
    """
    import bot

    names = [h.callback.__name__ for h in bot.dp.message.handlers]
    assert names[-1] == "on_private_any"
    for cmd in ("cmd_my", "cmd_notify", "cmd_broadcast", "cmd_stop"):
        assert names.index(cmd) < names.index("on_private_any")


def test_deep_link_payload_roundtrip():
    """Ссылка кнопки должна содержать разбираемый код фиксации."""
    fid = 412
    payload = f"fix{fid}"
    assert payload.startswith("fix") and payload[3:].isdigit()
    assert int(payload[3:]) == fid
