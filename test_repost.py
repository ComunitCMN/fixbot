"""
Рассылка репостом из канала.

Владелец пересылает боту пост из агентского канала. Обычная рассылка
делает копию — у агентов пост выглядит написанным ботом. Репост
сохраняет плашку «Переслано из …», по которой агент переходит в канал.

Что здесь стережём:
  * про способ спрашиваем только у поста из канала; обычное сообщение
    идёт как раньше, без нового вопроса;
  * репост перевести нельзя — перед отправкой предупреждаем, что
    англоязычные получат его на русском, и даём выбор: репост всем
    или англоязычным переведённую копию;
  * выбранный способ доезжает до отправки;
  * старые черновики, где способа нет, уходят копией, как раньше.
"""

import time
from types import SimpleNamespace

import pytest

import broadcast as bc
from db import Db
from test_chat_lists import OWNER, Screen, _press, _setup


# ===================== доставка =====================

class ForwardingBot:
    def __init__(self, fail=False):
        self.forwarded: list[int] = []
        self.forwarded_albums: list[int] = []
        self.copies: list[int] = []
        self.translated: list[tuple[int, str]] = []
        self.fail = fail

    async def forward_message(self, chat_id, from_chat_id, message_id, **kw):
        if self.fail:
            raise RuntimeError("message can't be forwarded")
        self.forwarded.append(chat_id)

    async def forward_messages(self, chat_id, from_chat_id, message_ids, **kw):
        self.forwarded_albums.append(chat_id)

    async def copy_message(self, chat_id, from_chat_id, message_id, **kw):
        self.copies.append(chat_id)

    async def copy_messages(self, chat_id, from_chat_id, message_ids, **kw):
        self.copies.append(chat_id)

    async def send_message(self, chat_id, text, **kw):
        self.translated.append((chat_id, text))


def _repost_draft(mode, ids=(1,)):
    return bc.Draft(src_chat_id=-1, message_ids=list(ids), html="Старт продаж",
                    mode=mode)


async def test_repost_goes_to_everyone_whatever_the_language():
    """Репост перевести нельзя — англоязычные получают тот же пост."""
    bot = ForwardingBot()
    sent, failed = await bc.deliver(
        bot, [(10, "ru"), (20, "en")], _repost_draft(bc.REPOST), "")

    assert (sent, failed) == (2, 0)
    assert bot.forwarded == [10, 20]
    assert bot.copies == [] and bot.translated == []


async def test_repost_ignores_a_stray_translation():
    """Выбран «репост всем» — даже случайный перевод в черновике не уходит."""
    bot = ForwardingBot()
    await bc.deliver(bot, [(20, "en")], _repost_draft(bc.REPOST), "Hello")
    assert bot.forwarded == [20] and bot.translated == []


async def test_repost_with_translation_for_english_groups():
    """Русским — репост, англоязычным — переведённая копия, как раньше."""
    bot = ForwardingBot()
    await bc.deliver(bot, [(10, "ru"), (20, "en")],
                     _repost_draft(bc.REPOST_EN), "Sales start")

    assert bot.forwarded == [10]
    assert bot.translated == [(20, "Sales start")]


async def test_failed_translation_falls_back_to_the_repost():
    """Перевода нет — англоязычным уходит репост, а не пустота и не копия."""
    bot = ForwardingBot()
    await bc.deliver(bot, [(20, "en")], _repost_draft(bc.REPOST_EN), None)
    assert bot.forwarded == [20] and bot.copies == []


async def test_album_is_reposted_as_an_album():
    bot = ForwardingBot()
    await bc.deliver(bot, [(10, "ru")], _repost_draft(bc.REPOST, ids=(1, 2)), "")
    assert bot.forwarded_albums == [10] and bot.forwarded == []


async def test_draft_without_a_mode_is_a_copy():
    """Обычная рассылка не меняется: копия и перевод, как до доработки."""
    assert bc.Draft(src_chat_id=-1).mode == bc.COPY
    bot = ForwardingBot()
    draft = bc.Draft(src_chat_id=-1, message_ids=[1], html="Привет")
    await bc.deliver(bot, [(10, "ru"), (20, "en")], draft, "Hello")
    assert bot.copies == [10] and bot.translated == [(20, "Hello")]
    assert bot.forwarded == []


# ===================== обвязка =====================

class Chat:
    """Личка владельца с ботом: всё, что бот туда написал."""

    def __init__(self):
        self.screen = Screen()
        self.said: list[str] = []

    async def answer(self, text, reply_markup=None, **kw):
        self.said.append(text)
        self.screen.take(text, reply_markup)
        return SimpleNamespace(edit_text=self.answer)


def _incoming(chat, from_channel=True, html="Старт продаж в ЖК Восход"):
    origin = (SimpleNamespace(type="channel", chat=SimpleNamespace(id=-500))
              if from_channel else None)
    return SimpleNamespace(
        message_id=7, chat=SimpleNamespace(id=OWNER, type="private"),
        from_user=SimpleNamespace(id=OWNER, username="u", full_name="U"),
        html_text=html or None, text=html or None, caption=None,
        photo=None, video=None, animation=None, document=None, audio=None,
        media_group_id=None, forward_origin=origin,
        answer=chat.answer, reply=chat.answer)


def _env(b, monkeypatch, tmp_path, fail_forward=False):
    db = Db(tmp_path / "rp.db")
    _setup(b, monkeypatch, db)
    fake = ForwardingBot(fail=fail_forward)
    for name in ("forward_message", "forward_messages", "copy_message",
                 "copy_messages", "send_message"):
        monkeypatch.setattr(b.bot, name, getattr(fake, name))

    asked: list[str] = []

    async def translate(html, *a, **kw):
        asked.append(html)
        return "Sales start"

    monkeypatch.setattr(b.clf, "translate", translate)
    return db, fake, asked


async def _capture(b, msg):
    b._awaiting_broadcast[OWNER] = time.time()
    return await b.try_capture_broadcast(msg)


async def _press_mode(b, data, chat):
    c, screen = _press(data)
    c.message.answer = chat.answer
    await b.cb_broadcast_mode(c)
    return screen


# ===================== вопрос о способе =====================

async def test_post_from_a_channel_asks_how_to_send(tmp_path, monkeypatch):
    import bot as b

    db, _, asked = _env(b, monkeypatch, tmp_path)
    chat = Chat()
    assert await _capture(b, _incoming(chat)) is True

    assert "bcm:1:repost" in chat.screen.kb
    assert "bcm:1:copy" in chat.screen.kb
    assert asked == []                 # переводить пока нечего
    assert not any(k.endswith(":chats") for k in chat.screen.kb)


async def test_ordinary_message_is_not_asked(tmp_path, monkeypatch):
    """Не пересылка из канала — всё как раньше: перевод и выбор адресатов."""
    import bot as b

    db, _, asked = _env(b, monkeypatch, tmp_path)
    chat = Chat()
    await _capture(b, _incoming(chat, from_channel=False))

    assert not any(k.startswith("bcm:") for k in chat.screen.kb)
    assert "bc:1:chats" in chat.screen.kb
    assert asked == ["Старт продаж в ЖК Восход"]
    assert db.get_broadcast(1)["mode"] in (None, bc.COPY)


async def test_choosing_copy_is_the_old_broadcast(tmp_path, monkeypatch):
    import bot as b

    db, _, asked = _env(b, monkeypatch, tmp_path)
    chat = Chat()
    await _capture(b, _incoming(chat))
    await _press_mode(b, "bcm:1:copy", chat)

    assert asked == ["Старт продаж в ЖК Восход"]
    assert db.get_broadcast(1)["mode"] == bc.COPY
    assert db.get_broadcast(1)["html_en"] == "Sales start"
    assert "bc:1:chats" in chat.screen.kb


# ===================== репост =====================

async def test_repost_preview_warns_that_english_get_russian(tmp_path,
                                                            monkeypatch):
    import bot as b

    db, fake, asked = _env(b, monkeypatch, tmp_path)
    chat = Chat()
    await _capture(b, _incoming(chat))
    await _press_mode(b, "bcm:1:repost", chat)

    assert fake.forwarded == [OWNER]          # превью — настоящий репост
    assert "на русском" in chat.screen.text
    assert "bcm:1:all" in chat.screen.kb and "bcm:1:en" in chat.screen.kb
    assert asked == []


async def test_repost_to_everyone(tmp_path, monkeypatch):
    import bot as b

    db, _, asked = _env(b, monkeypatch, tmp_path)
    chat = Chat()
    await _capture(b, _incoming(chat))
    await _press_mode(b, "bcm:1:repost", chat)
    await _press_mode(b, "bcm:1:all", chat)

    assert db.get_broadcast(1)["mode"] == bc.REPOST
    assert asked == []
    assert "bc:1:chats" in chat.screen.kb


async def test_repost_with_english_translation(tmp_path, monkeypatch):
    import bot as b

    db, fake, asked = _env(b, monkeypatch, tmp_path)
    chat = Chat()
    await _capture(b, _incoming(chat))
    await _press_mode(b, "bcm:1:repost", chat)
    await _press_mode(b, "bcm:1:en", chat)

    row = db.get_broadcast(1)
    assert row["mode"] == bc.REPOST_EN and row["html_en"] == "Sales start"
    assert (OWNER, "Sales start") in fake.translated    # превью перевода
    assert "bc:1:chats" in chat.screen.kb


async def test_post_without_text_is_not_warned(tmp_path, monkeypatch):
    """Фото без подписи: переводить нечего, и предупреждать не о чем."""
    import bot as b

    db, _, _ = _env(b, monkeypatch, tmp_path)
    chat = Chat()
    await _capture(b, _incoming(chat, html=""))
    await _press_mode(b, "bcm:1:repost", chat)

    assert db.get_broadcast(1)["mode"] == bc.REPOST
    assert not any("на русском" in s for s in chat.said)
    assert "bc:1:chats" in chat.screen.kb


async def test_forbidden_forward_is_told_before_sending(tmp_path, monkeypatch):
    """Telegram не дал переслать — говорим на превью и предлагаем копию."""
    import bot as b

    db, _, _ = _env(b, monkeypatch, tmp_path, fail_forward=True)
    chat = Chat()
    await _capture(b, _incoming(chat))
    await _press_mode(b, "bcm:1:repost", chat)

    assert "переслать" in chat.screen.text.lower()
    assert chat.screen.kb == ["bcm:1:copy", "bc:1:cancel"]
    assert db.get_broadcast(1)["mode"] in (None, bc.COPY)


async def test_stranger_cannot_choose_the_mode(tmp_path, monkeypatch):
    import bot as b

    db, fake, _ = _env(b, monkeypatch, tmp_path)
    chat = Chat()
    await _capture(b, _incoming(chat))
    c, screen = _press("bcm:1:repost", user=99)
    await b.cb_broadcast_mode(c)

    assert screen.alerts == ["Недоступно"]
    assert fake.forwarded == []


# ===================== отправка =====================

async def test_chosen_mode_reaches_the_delivery(tmp_path, monkeypatch):
    import bot as b

    db, _, _ = _env(b, monkeypatch, tmp_path)
    bid = db.create_broadcast(OWNER, OWNER, [1], [], "Старт продаж")
    db.update_broadcast(bid, mode=bc.REPOST, target={"chats": True})
    db.register_chat(-1, "Дом+ × Eco")
    db.set_meta("chat_agency:-1", str(db.create_agency("Дом+", "дом+")))

    seen = []

    async def deliver(bot, targets, draft, html_en, on_fail=None):
        seen.append(draft.mode)
        return len(targets), 0

    monkeypatch.setattr(b.bc, "deliver", deliver)
    c, _ = _press(f"bcgo:{bid}")
    await b.cb_broadcast_go(c)
    assert seen == [bc.REPOST]


async def test_old_draft_without_a_mode_goes_as_a_copy(tmp_path, monkeypatch):
    """Черновик, начатый до обновления, способа не знает — уходит копией."""
    import bot as b

    db, _, _ = _env(b, monkeypatch, tmp_path)
    bid = db.create_broadcast(OWNER, OWNER, [1], [], "Старт продаж")
    db.update_broadcast(bid, target={"chats": True})
    db.register_chat(-1, "Дом+ × Eco")
    db.set_meta("chat_agency:-1", str(db.create_agency("Дом+", "дом+")))
    assert db.get_broadcast(bid)["mode"] is None

    seen = []

    async def deliver(bot, targets, draft, html_en, on_fail=None):
        seen.append(draft.mode)
        return len(targets), 0

    monkeypatch.setattr(b.bc, "deliver", deliver)
    c, _ = _press(f"bcgo:{bid}")
    await b.cb_broadcast_go(c)
    assert seen == [bc.COPY]


def test_mode_column_reaches_a_live_database(tmp_path):
    """Базы на сервере боевые: колонка добавляется миграцией."""
    import sqlite3

    path = tmp_path / "old.db"
    db = Db(path)
    db.conn.close()
    con = sqlite3.connect(path)
    cols = {r[1] for r in con.execute("PRAGMA table_info(broadcasts)")}
    con.close()
    assert "mode" in cols


async def test_confirmation_says_it_is_a_repost(tmp_path, monkeypatch):
    """На «Отправляем?» видно, что уйдёт репост, а не копия."""
    import bot as b

    db = Db(tmp_path / "cf.db")
    _setup(b, monkeypatch, db)
    db.register_chat(-1, "Дом+ × Eco")
    db.set_meta("chat_agency:-1", str(db.create_agency("Дом+", "дом+")))
    bid = db.create_broadcast(OWNER, OWNER, [1], [], "Старт продаж")
    db.update_broadcast(bid, mode=bc.REPOST)

    c, screen = _press(f"bc:{bid}:chats")
    await b.cb_broadcast(c)
    assert "репост" in screen.text.lower()
