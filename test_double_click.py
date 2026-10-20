"""
Повторное нажатие «Зафиксировать».

20.10.2026: агент нажала кнопку несколько раз, пока бот ходил в CRM,
и в CRM легли три сделки на одного клиента. Пометка «обработана»
ставилась только после записи, а запись занимает несколько секунд:
каждое нажатие проходило проверку и записывало заново.

Что здесь стережём:
  * карточку берёт в работу только первое нажатие — одним шагом;
  * агент сразу видит, что идёт запись, и кнопки пропадают;
  * сбой записи возвращает кнопки: нажать ещё раз можно, фиксация
    не теряется;
  * карточка, застрявшая в записи (бот перезапустился посреди),
    убирается по сроку, как брошенная, а не возвращает кнопку —
    первая запись могла успеть дойти, повтор дал бы дубль.
"""

import asyncio
from types import SimpleNamespace

import pytest

from db import Db

AGENT = 500
STRANGER = 99


def _pending(db, client="Иван"):
    aid = db.create_agency("Дом+", "дом+")
    return db.create_pending(-100, 1, AGENT, {
        "digits": "79991234567", "agency_id": aid, "agency_name": "Дом+",
        "client": client, "lang": "ru", "author": "Агент",
        "chat_title": "Дом+ × Eco", "raw_text": "Иван +7 999 123-45-67",
    })


class Card:
    """Сообщение бота с кнопками: текст, кнопки и всплывашки."""

    def __init__(self, text="<b>Карточка клиента</b>"):
        self.html_text = text
        self.kb: list[str] = []
        self.alerts: list[str] = []

    async def edit_text(self, text, reply_markup=None, **kw):
        self.html_text = text
        self.kb = ([b.callback_data for row in reply_markup.inline_keyboard
                    for b in row] if reply_markup else [])


def _press(card, pid, user=AGENT):
    async def answer(text="", show_alert=False, **kw):
        if text:
            card.alerts.append(text)

    return SimpleNamespace(
        from_user=SimpleNamespace(id=user), data=f"fix:{pid}",
        message=SimpleNamespace(edit_text=card.edit_text,
                                html_text=card.html_text,
                                chat=SimpleNamespace(id=-100)),
        answer=answer)


def _env(b, monkeypatch, tmp_path, fail=False):
    db = Db(tmp_path / "dc.db")
    monkeypatch.setattr(b, "db", db)
    monkeypatch.setattr(b.cfg, "dry_run", False)
    monkeypatch.setattr(b, "target_pipeline", lambda: (1, 2, "Агентская"))

    seen = SimpleNamespace(created=0, card_during_crm=[])
    card = Card()

    async def find_all_matches(p):
        seen.card_during_crm.append((card.html_text, list(card.kb)))
        await asyncio.sleep(0.05)          # CRM думает
        return [], None

    async def create_in_amo(payload, p, author_id, d=None):
        await asyncio.sleep(0.05)
        if fail:
            raise RuntimeError("amoCRM: 502 Bad Gateway")
        seen.created += 1
        return {"contact_id": 10 + seen.created, "lead_id": 20 + seen.created,
                "agent_contact_id": None}

    async def nobody(*a, **kw):
        pass

    monkeypatch.setattr(b, "find_all_matches", find_all_matches)
    monkeypatch.setattr(b, "_create_in_amo", create_in_amo)
    monkeypatch.setattr(b, "notify_rivals_about_new", nobody)
    return db, card, seen


def _fixations(db):
    return db.conn.execute("SELECT COUNT(*) FROM fixations").fetchone()[0]


# ===================== кнопка =====================

async def test_three_quick_presses_make_one_fixation(tmp_path, monkeypatch):
    import bot as b

    db, card, seen = _env(b, monkeypatch, tmp_path)
    pid = _pending(db)

    await asyncio.gather(*(b.cb_confirm(_press(card, pid)) for _ in range(3)))

    assert seen.created == 1
    assert _fixations(db) == 1
    assert db.get_pending(pid)["status"] == "done"
    assert sum("записываю" in a.lower() for a in card.alerts) == 2


async def test_agent_sees_progress_before_the_crm_is_asked(tmp_path,
                                                           monkeypatch):
    """Кнопки пропадают сразу, и видно, что бот работает."""
    import bot as b

    db, card, seen = _env(b, monkeypatch, tmp_path)
    pid = _pending(db)
    await b.cb_confirm(_press(card, pid))

    text, kb = seen.card_during_crm[0]
    assert kb == []
    assert "⏳" in text
    assert "Карточка клиента" in text        # карточка видна и во время записи
    assert "crm" not in text.lower()


async def test_failure_brings_the_buttons_back(tmp_path, monkeypatch):
    """Сбой связи не стоит агенту фиксации: кнопка снова на месте."""
    import bot as b

    db, card, seen = _env(b, monkeypatch, tmp_path, fail=True)
    pid = _pending(db)
    await b.cb_confirm(_press(card, pid))

    assert f"fix:{pid}" in card.kb
    assert db.get_pending(pid)["status"] == "waiting"
    assert _fixations(db) == 0


async def test_press_after_a_failure_writes_once(tmp_path, monkeypatch):
    import bot as b

    db, card, _ = _env(b, monkeypatch, tmp_path, fail=True)
    pid = _pending(db)
    await b.cb_confirm(_press(card, pid))

    _, card2, seen = _env(b, monkeypatch, tmp_path)   # связь вернулась
    monkeypatch.setattr(b, "db", db)
    await b.cb_confirm(_press(card2, pid))

    assert seen.created == 1
    assert db.get_pending(pid)["status"] == "done"


async def test_stranger_press_does_not_take_the_card(tmp_path, monkeypatch):
    import bot as b

    db, card, seen = _env(b, monkeypatch, tmp_path)
    pid = _pending(db)
    await b.cb_confirm(_press(card, pid, user=STRANGER))

    assert seen.created == 0
    assert db.get_pending(pid)["status"] == "waiting"


async def test_press_on_a_finished_card_is_refused(tmp_path, monkeypatch):
    import bot as b

    db, card, seen = _env(b, monkeypatch, tmp_path)
    pid = _pending(db)
    await b.cb_confirm(_press(card, pid))
    await b.cb_confirm(_press(card, pid))

    assert seen.created == 1
    assert card.alerts[-1] == "Заявка уже обработана"


# ===================== база =====================

def test_the_card_is_taken_only_once(tmp_path):
    db = Db(tmp_path / "cl.db")
    pid = _pending(db)
    assert db.claim_pending(pid) is True
    assert db.claim_pending(pid) is False
    assert db.get_pending(pid)["status"] == "processing"


def test_released_card_can_be_taken_again(tmp_path):
    db = Db(tmp_path / "rl.db")
    pid = _pending(db)
    db.claim_pending(pid)
    db.release_pending(pid)
    assert db.get_pending(pid)["status"] == "waiting"
    assert db.claim_pending(pid) is True


def test_release_does_not_reopen_a_finished_card(tmp_path):
    """Освобождаем только то, что сами взяли, — готовое не трогаем."""
    db = Db(tmp_path / "rd.db")
    pid = _pending(db)
    db.close_pending(pid, "done")
    db.release_pending(pid)
    assert db.get_pending(pid)["status"] == "done"


def test_card_stuck_in_writing_expires_like_an_abandoned_one(tmp_path):
    """
    Вариант «а»: бот перезапустился посреди записи — карточка уходит
    по сроку, как брошенная. Кнопку не возвращаем: запись могла успеть.
    """
    db = Db(tmp_path / "ex.db")
    stuck = _pending(db)
    db.claim_pending(stuck)
    left = db.create_pending(-100, 2, AGENT, {"digits": "79990000000"})

    gone = {r["id"] for r in db.expire_pending(-1)}

    assert gone == {stuck, left}
    assert db.get_pending(stuck)["status"] == "expired"


async def test_crm_down_during_the_recheck_brings_the_buttons_back(
        tmp_path, monkeypatch):
    """
    Связь упала ещё на повторной проверке дублей, до записи. Кнопки
    к этому моменту уже убраны — их надо вернуть, иначе карточка
    повиснет на «⏳» и фиксацию не сделать.
    """
    import bot as b

    db, card, _ = _env(b, monkeypatch, tmp_path)

    async def broken(p):
        raise RuntimeError("amoCRM: timeout")

    monkeypatch.setattr(b, "find_all_matches", broken)
    pid = _pending(db)
    with pytest.raises(RuntimeError):
        await b.cb_confirm(_press(card, pid))

    assert f"fix:{pid}" in card.kb
    assert "⏳" not in card.html_text
    assert db.get_pending(pid)["status"] == "waiting"
