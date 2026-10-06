"""
Сохранённые списки групп для рассылок.

Владельцу нужно слать объявление не во все рабочие чаты разом и не
одному агентству, а в выбранный набор групп — например, «Бали» или
«крупные агентства». Набор собирается один раз в «💬 Группы →
📋 Списки групп» и дальше выбирается в рассылке одной кнопкой.

Что здесь стережём:
  * группа в списке — только закреплённая за агентством; открепили —
    рассылку не получает и на подтверждении показана пропущенной;
  * рассылка по списку не может уйти «всем агентам» по ошибке ветки;
  * удаление — только после «точно?»;
  * доступ — у всех, у кого есть меню, и ни у кого больше;
  * старые черновики «Активным за 30 дней» не ломаются.
"""

import sqlite3
from types import SimpleNamespace

import pytest

from db import Db


# ===================== база =====================

def test_new_tables_appear_in_an_old_live_database(tmp_path):
    """
    На сервере базы боевые: пересоздать нельзя. Старая база без новых
    таблиц должна их получить при запуске и не потерять ничего своего.
    """
    path = tmp_path / "old.db"
    db = Db(path)
    aid = db.create_agency("Дом+", "дом+")
    db.set_meta("chat_agency:-100", str(aid))
    db.conn.execute("DROP TABLE chat_list_items")
    db.conn.execute("DROP TABLE chat_lists")
    db.conn.commit()
    db.conn.close()

    db = Db(path)          # как после обновления бота
    tables = {r[0] for r in db.conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"chat_lists", "chat_list_items"} <= tables
    assert db.get_meta("chat_agency:-100") == str(aid)   # данные на месте

    lid = db.create_chat_list("Бали")
    assert db.toggle_chat_in_list(lid, -100) is True


def test_lists_are_created_counted_and_listed(tmp_path):
    db = Db(tmp_path / "l.db")
    bali = db.create_chat_list("Бали")
    big = db.create_chat_list("Крупные")
    db.toggle_chat_in_list(bali, -1)
    db.toggle_chat_in_list(bali, -2)

    got = {r["name"]: r["count"] for r in db.list_chat_lists()}
    assert got == {"Бали": 2, "Крупные": 0}
    assert db.get_chat_list(big)["name"] == "Крупные"


def test_pressing_twice_takes_the_group_back_out(tmp_path):
    db = Db(tmp_path / "t.db")
    lid = db.create_chat_list("Бали")
    assert db.toggle_chat_in_list(lid, -1) is True
    assert db.chat_list_members(lid) == [-1]
    assert db.toggle_chat_in_list(lid, -1) is False
    assert db.chat_list_members(lid) == []


def test_a_group_can_be_in_several_lists(tmp_path):
    db = Db(tmp_path / "s.db")
    a = db.create_chat_list("Бали")
    b = db.create_chat_list("Крупные")
    db.toggle_chat_in_list(a, -1)
    db.toggle_chat_in_list(b, -1)
    assert db.chat_list_members(a) == [-1]
    assert db.chat_list_members(b) == [-1]


def test_rename_keeps_the_groups(tmp_path):
    db = Db(tmp_path / "r.db")
    lid = db.create_chat_list("Бали")
    db.toggle_chat_in_list(lid, -1)
    db.rename_chat_list(lid, "Бали и Ломбок")
    assert db.get_chat_list(lid)["name"] == "Бали и Ломбок"
    assert db.chat_list_members(lid) == [-1]


def test_deleting_a_list_touches_only_that_list(tmp_path):
    db = Db(tmp_path / "d.db")
    a = db.create_chat_list("Бали")
    b = db.create_chat_list("Крупные")
    db.toggle_chat_in_list(a, -1)
    db.toggle_chat_in_list(b, -1)
    db.set_meta("chat_agency:-1", "5")

    db.delete_chat_list(a)

    assert db.get_chat_list(a) is None
    assert db.chat_list_members(a) == []
    assert db.chat_list_members(b) == [-1]
    assert db.get_meta("chat_agency:-1") == "5"     # группа не тронута


# ===================== обвязка для обработчиков =====================

OWNER = 2
STAFF = 3
STRANGER = 99


class Screen:
    """Что бот показал в ответ на нажатие: текст и кнопки."""

    def __init__(self):
        self.text = None
        self.kb: list[str] = []
        self.labels: list[str] = []
        self.alerts: list[str] = []

    def take(self, text, reply_markup=None):
        self.text = text
        if reply_markup is not None:
            rows = reply_markup.inline_keyboard
            self.kb = [x.callback_data for row in rows for x in row]
            self.labels = [x.text for row in rows for x in row]


def _setup(b, monkeypatch, db):
    monkeypatch.setattr(b, "db", db)
    monkeypatch.setattr(b.cfg, "owner_ids", {OWNER})
    db.add_staff(STAFF, "staff", "Маркетолог", OWNER)

    async def get_chat(chat_id):
        raise RuntimeError("chat not found")

    monkeypatch.setattr(b.bot, "get_chat", get_chat)


def _press(data, user=OWNER):
    screen = Screen()

    async def edit_text(text, reply_markup=None, **kw):
        screen.take(text, reply_markup)

    async def answer_msg(text, reply_markup=None, **kw):
        screen.take(text, reply_markup)

    async def edit_reply_markup(reply_markup=None, **kw):
        screen.take(screen.text, reply_markup)

    async def answer(text="", show_alert=False, **kw):
        if text:
            screen.alerts.append(text)

    c = SimpleNamespace(
        from_user=SimpleNamespace(id=user), data=data,
        message=SimpleNamespace(edit_text=edit_text, answer=answer_msg,
                                edit_reply_markup=edit_reply_markup,
                                chat=SimpleNamespace(id=user)),
        answer=answer)
    return c, screen


def _say(text, user=OWNER):
    screen = Screen()

    async def reply(text, reply_markup=None, **kw):
        screen.take(text, reply_markup)

    m = SimpleNamespace(
        from_user=SimpleNamespace(id=user, username="u", full_name="U"),
        chat=SimpleNamespace(id=user, type="private"),
        text=text, html_text=text, caption=None, photo=None,
        media_group_id=None, forward_from=None,
        reply=reply, answer=reply)
    return m, screen


def _groups(db):
    """Три закреплённые группы и одна незакреплённая."""
    aid = db.create_agency("Дом+", "дом+")
    for chat_id, title in [(-1, "Дом+ × Eco"), (-2, "Rich & Eco"),
                           (-3, "TEUS × Eco")]:
        db.register_chat(chat_id, title)
        db.set_meta(f"chat_agency:{chat_id}", str(aid))
    db.register_chat(-9, "Флудилка")        # не закреплена


async def _click(b, data, user=OWNER):
    c, screen = _press(data, user)
    if data.startswith("m:"):
        await b.cb_menu(c)
    elif data.startswith("gl:"):
        await b.cb_chat_lists(c)
    elif data.startswith("bcgo:"):
        await b.cb_broadcast_go(c)
    elif data.startswith("bc:"):
        await b.cb_broadcast(c)
    return screen


# ===================== раздел «Списки групп» =====================

async def test_groups_screen_has_the_lists_button(tmp_path, monkeypatch):
    import bot as b

    db = Db(tmp_path / "g.db")
    _setup(b, monkeypatch, db)
    _groups(db)

    screen = await _click(b, "m:chats")
    assert "gl:home" in screen.kb
    assert "📋 Списки групп" in screen.labels


async def test_lists_screen_shows_each_list_with_its_count(tmp_path,
                                                           monkeypatch):
    import bot as b

    db = Db(tmp_path / "h.db")
    _setup(b, monkeypatch, db)
    lid = db.create_chat_list("Бали")
    db.toggle_chat_in_list(lid, -1)
    db.toggle_chat_in_list(lid, -2)

    screen = await _click(b, "gl:home")
    assert "Бали" in screen.text and "2" in screen.text
    assert f"gl:open:{lid}" in screen.kb
    assert "gl:new" in screen.kb
    assert "m:chats" in screen.kb          # дорога назад


async def test_new_list_asks_for_a_name_and_creates_it(tmp_path, monkeypatch):
    import bot as b

    db = Db(tmp_path / "n.db")
    _setup(b, monkeypatch, db)

    screen = await _click(b, "gl:new")
    assert "назван" in screen.text.lower()
    assert db.list_chat_lists() == []      # пока только вопрос

    m, reply = _say("Бали")
    await b.on_private_any(m)              # через общую цепочку лички

    lists = db.list_chat_lists()
    assert [r["name"] for r in lists] == ["Бали"]
    assert f"gl:open:{lists[0]['id']}" in reply.kb

    # Следующее сообщение — уже не название.
    m, _ = _say("просто текст")
    await b.try_chat_list_name_reply(m)
    assert len(db.list_chat_lists()) == 1


async def test_list_name_does_not_eat_a_broadcast(tmp_path, monkeypatch):
    """
    Нажал «➕ Новый список», передумал и ушёл в «📣 Рассылку» — текст
    рассылки не должен стать названием списка.
    """
    import bot as b

    db = Db(tmp_path / "x.db")
    _setup(b, monkeypatch, db)

    await _click(b, "gl:new")
    await _click(b, "m:bcast")
    m, _ = _say("Старт продаж!")
    assert await b.try_chat_list_name_reply(m) is False
    assert db.list_chat_lists() == []
    b._awaiting_broadcast.pop(OWNER, None)


async def test_cancel_drops_the_name_question(tmp_path, monkeypatch):
    import bot as b

    db = Db(tmp_path / "c.db")
    _setup(b, monkeypatch, db)
    await _click(b, "gl:new")

    m, _ = _say("/cancel")
    await b.cmd_cancel_broadcast(m)

    m, _ = _say("Бали")
    assert await b.try_chat_list_name_reply(m) is False
    assert db.list_chat_lists() == []


async def test_inside_a_list_every_bound_group_is_a_button(tmp_path,
                                                           monkeypatch):
    import bot as b

    db = Db(tmp_path / "i.db")
    _setup(b, monkeypatch, db)
    _groups(db)
    lid = db.create_chat_list("Бали")
    db.toggle_chat_in_list(lid, -2)

    screen = await _click(b, f"gl:open:{lid}")
    for chat_id in (-1, -2, -3):
        assert f"gl:t:{lid}:{chat_id}" in screen.kb
    assert f"gl:t:{lid}:-9" not in screen.kb    # незакреплённой нет
    marked = [x for x in screen.labels if x.startswith("✅")]
    assert marked == ["✅ Rich & Eco"]


async def test_pressing_a_group_puts_a_tick_and_takes_it_off(tmp_path,
                                                             monkeypatch):
    import bot as b

    db = Db(tmp_path / "p.db")
    _setup(b, monkeypatch, db)
    _groups(db)
    lid = db.create_chat_list("Бали")

    screen = await _click(b, f"gl:t:{lid}:-1")
    assert db.chat_list_members(lid) == [-1]
    assert "✅ Дом+ × Eco" in screen.labels

    screen = await _click(b, f"gl:t:{lid}:-1")
    assert db.chat_list_members(lid) == []
    assert "✅ Дом+ × Eco" not in screen.labels


async def test_an_unbound_group_cannot_be_ticked(tmp_path, monkeypatch):
    """Старая кнопка или подделанное нажатие — незакреплённую не берём."""
    import bot as b

    db = Db(tmp_path / "u.db")
    _setup(b, monkeypatch, db)
    _groups(db)
    lid = db.create_chat_list("Бали")

    await _click(b, f"gl:t:{lid}:-9")
    assert db.chat_list_members(lid) == []


async def test_delete_asks_first(tmp_path, monkeypatch):
    import bot as b

    db = Db(tmp_path / "del.db")
    _setup(b, monkeypatch, db)
    lid = db.create_chat_list("Бали")

    screen = await _click(b, f"gl:del:{lid}")
    assert "точно" in screen.text.lower()
    assert db.get_chat_list(lid) is not None      # ещё не удалён
    assert f"gl:delok:{lid}" in screen.kb
    assert f"gl:open:{lid}" in screen.kb          # можно передумать

    await _click(b, f"gl:delok:{lid}")
    assert db.get_chat_list(lid) is None


async def test_rename_asks_for_a_new_name(tmp_path, monkeypatch):
    import bot as b

    db = Db(tmp_path / "ren.db")
    _setup(b, monkeypatch, db)
    lid = db.create_chat_list("Бали")
    db.toggle_chat_in_list(lid, -1)

    await _click(b, f"gl:ren:{lid}")
    m, _ = _say("Бали и Ломбок")
    await b.on_private_any(m)

    assert db.get_chat_list(lid)["name"] == "Бали и Ломбок"
    assert db.chat_list_members(lid) == [-1]


async def test_staff_can_use_lists(tmp_path, monkeypatch):
    import bot as b

    db = Db(tmp_path / "st.db")
    _setup(b, monkeypatch, db)
    _groups(db)
    lid = db.create_chat_list("Бали")

    await _click(b, f"gl:t:{lid}:-1", user=STAFF)
    assert db.chat_list_members(lid) == [-1]


async def test_strangers_cannot_touch_lists(tmp_path, monkeypatch):
    import bot as b

    db = Db(tmp_path / "no.db")
    _setup(b, monkeypatch, db)
    _groups(db)
    lid = db.create_chat_list("Бали")

    for data in ("gl:home", f"gl:open:{lid}", f"gl:t:{lid}:-1",
                 f"gl:delok:{lid}", "gl:new"):
        screen = await _click(b, data, user=STRANGER)
        assert screen.alerts == ["Недоступно"]
    assert db.chat_list_members(lid) == []
    assert db.get_chat_list(lid) is not None

    m, _ = _say("Взлом", user=STRANGER)
    assert await b.try_chat_list_name_reply(m) is False


# ===================== рассылка по списку =====================

def _draft(db):
    return db.create_broadcast(OWNER, OWNER, [1], [], "Старт продаж")


async def test_target_menu_offers_lists_and_drops_active(tmp_path,
                                                         monkeypatch):
    import bot as b

    db = Db(tmp_path / "k.db")
    _setup(b, monkeypatch, db)
    bid = _draft(db)

    kb = b._bcast_target_kb(bid)
    data = [x.callback_data for row in kb.inline_keyboard for x in row]
    labels = [x.text for row in kb.inline_keyboard for x in row]

    assert f"bc:{bid}:lists" in data
    assert "📋 В список групп…" in labels
    assert f"bc:{bid}:active" not in data
    assert not any("Активным" in x for x in labels)
    # Остальное — как было.
    for action in ("all", "chats", "agency", "cancel"):
        assert f"bc:{bid}:{action}" in data


async def test_choosing_lists_shows_the_saved_lists(tmp_path, monkeypatch):
    import bot as b

    db = Db(tmp_path / "ch.db")
    _setup(b, monkeypatch, db)
    lid = db.create_chat_list("Бали")
    bid = _draft(db)

    screen = await _click(b, f"bc:{bid}:lists")
    assert f"bc:{bid}:gl{lid}" in screen.kb
    assert any("Бали" in x for x in screen.labels)
    assert f"bc:{bid}:cancel" in screen.kb


async def test_no_lists_yet_says_where_to_make_one(tmp_path, monkeypatch):
    import bot as b

    db = Db(tmp_path / "e.db")
    _setup(b, monkeypatch, db)
    bid = _draft(db)

    screen = await _click(b, f"bc:{bid}:lists")
    assert screen.alerts and "Списки групп" in screen.alerts[0]
    assert db.get_broadcast(bid)["status"] == "draft"


async def test_confirmation_names_every_group_and_the_skipped_ones(
        tmp_path, monkeypatch):
    import bot as b

    db = Db(tmp_path / "cf.db")
    _setup(b, monkeypatch, db)
    _groups(db)
    lid = db.create_chat_list("Бали")
    for chat_id in (-1, -2, -3):
        db.toggle_chat_in_list(lid, chat_id)
    db.set_meta("chat_agency:-3", "")      # открепили после сборки списка
    bid = _draft(db)

    screen = await _click(b, f"bc:{bid}:gl{lid}")

    assert "Дом+ × Eco" in screen.text
    assert "Rich &amp; Eco" in screen.text          # с экранированием
    assert "TEUS × Eco" in screen.text
    head, skipped = screen.text.split("ропущ", 1)
    assert "TEUS × Eco" in skipped and "TEUS × Eco" not in head
    assert f"bcgo:{bid}" in screen.kb
    assert "🚀 Отправить" in screen.labels
    assert db.get_broadcast(bid)["status"] == "draft"   # ничего не ушло


async def test_send_goes_to_the_list_like_working_chats(tmp_path,
                                                        monkeypatch):
    """
    Тем же путём, что «В рабочие чаты»: копия в группу на её языке.
    Открепленная группа ничего не получает, агентам в личку — тоже ничего.
    """
    import bot as b

    db = Db(tmp_path / "go.db")
    _setup(b, monkeypatch, db)
    _groups(db)
    db.set_meta("chat_lang:-2", "en")
    db.upsert_agent(500, "a", "Agent", agency_id=1, dm_open=True)
    lid = db.create_chat_list("Бали")
    for chat_id in (-1, -2, -3):
        db.toggle_chat_in_list(lid, chat_id)
    db.set_meta("chat_agency:-3", "")
    bid = _draft(db)

    calls = []

    async def deliver(bot, targets, draft, html_en, on_fail=None):
        calls.append(sorted(targets))
        return len(targets), 0

    monkeypatch.setattr(b.bc, "deliver", deliver)

    await _click(b, f"bc:{bid}:gl{lid}")
    screen = await _click(b, f"bcgo:{bid}")

    assert calls == [[(-2, "en"), (-1, b.chat_lang(-1))]]
    assert db.get_broadcast(bid)["status"] == "sent"
    assert "Доставлено: 2" in screen.text


async def test_group_unbound_after_confirmation_is_still_skipped(
        tmp_path, monkeypatch):
    import bot as b

    db = Db(tmp_path / "late.db")
    _setup(b, monkeypatch, db)
    _groups(db)
    lid = db.create_chat_list("Бали")
    db.toggle_chat_in_list(lid, -1)
    db.toggle_chat_in_list(lid, -2)
    bid = _draft(db)

    calls = []

    async def deliver(bot, targets, draft, html_en, on_fail=None):
        calls.append([t[0] for t in targets])
        return len(targets), 0

    monkeypatch.setattr(b.bc, "deliver", deliver)

    await _click(b, f"bc:{bid}:gl{lid}")
    db.set_meta("chat_agency:-2", "")        # открепили, пока думали
    await _click(b, f"bcgo:{bid}")

    assert calls == [[-1]]


async def test_list_with_nobody_left_sends_nothing(tmp_path, monkeypatch):
    import bot as b

    db = Db(tmp_path / "none.db")
    _setup(b, monkeypatch, db)
    _groups(db)
    lid = db.create_chat_list("Бали")
    db.toggle_chat_in_list(lid, -1)
    db.set_meta("chat_agency:-1", "")
    bid = _draft(db)

    screen = await _click(b, f"bc:{bid}:gl{lid}")
    assert f"bcgo:{bid}" not in screen.kb
    assert "Дом+ × Eco" in screen.text        # видно, кто выпал
    assert db.get_broadcast(bid)["status"] == "cancelled"


async def test_stranger_cannot_send_to_a_list(tmp_path, monkeypatch):
    import bot as b

    db = Db(tmp_path / "sx.db")
    _setup(b, monkeypatch, db)
    _groups(db)
    lid = db.create_chat_list("Бали")
    db.toggle_chat_in_list(lid, -1)
    bid = _draft(db)

    screen = await _click(b, f"bc:{bid}:gl{lid}", user=STRANGER)
    assert screen.alerts == ["Недоступно"]

    calls = []

    async def deliver(*a, **kw):
        calls.append(a)
        return 0, 0

    monkeypatch.setattr(b.bc, "deliver", deliver)
    await _click(b, f"bcgo:{bid}", user=STRANGER)
    assert calls == []


async def test_old_active_draft_still_sends(tmp_path, monkeypatch):
    """
    Черновик, где «Активным за 30 дней» уже выбрали до обновления,
    должен уйти по-прежнему — кнопку убрали, поддержку нет.
    """
    import bot as b

    db = Db(tmp_path / "old_active.db")
    _setup(b, monkeypatch, db)
    db.upsert_agent(500, "a", "Agent", agency_id=1, dm_open=True)
    import time
    db.log_fixation(digits="79991234567", agent_telegram_id=500,
                    verdict="unique", amo_lead_id=1,
                    created_at=int(time.time()) - 86400)
    bid = _draft(db)
    db.update_broadcast(bid, target={"active_days": 30})

    calls = []

    async def deliver(bot, targets, draft, html_en, on_fail=None):
        calls.append([t[0] for t in targets])
        return len(targets), 0

    monkeypatch.setattr(b.bc, "deliver", deliver)
    await _click(b, f"bcgo:{bid}")
    assert calls == [[500]]


def test_list_sending_is_only_by_hand():
    """Ни расписания, ни отложенной отправки: только кнопка «🚀»."""
    import inspect

    import bot as b

    for fn in (b.cb_chat_lists, b.try_chat_list_name_reply):
        src = inspect.getsource(fn)
        assert "deliver" not in src
        assert "create_task" not in src and "sleep" not in src
