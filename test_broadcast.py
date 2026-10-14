"""Рассылки: сборка сообщения, выборка адресатов, отписка."""

import time
from types import SimpleNamespace

import pytest

import broadcast as bc
from db import Db

NOW = int(time.time())
DAY = 86400


def fake_msg(message_id, chat_id=-100, html="", photo=None, video=None,
             media_group_id=None):
    return SimpleNamespace(
        message_id=message_id,
        chat=SimpleNamespace(id=chat_id),
        html_text=html or None, text=html or None, caption=None,
        photo=[SimpleNamespace(file_id=photo)] if photo else None,
        video=SimpleNamespace(file_id=video) if video else None,
        animation=None, document=None, audio=None,
        media_group_id=media_group_id,
    )


# ===================== сборка черновика =====================

def test_draft_from_single_text():
    d = bc.build_draft([fake_msg(1, html="<b>Привет</b> агенты")])
    assert d.message_ids == [1]
    assert d.html == "<b>Привет</b> агенты"
    assert not d.has_media


def test_draft_keeps_formatting():
    """Жирный, ссылки и эмодзи должны дойти до перевода как есть."""
    html = '<b>ЖК Восход</b> — старт продаж 🎉 <a href="http://x">условия</a>'
    d = bc.build_draft([fake_msg(1, html=html)])
    assert d.html == html


def test_draft_from_album_collects_all_media():
    msgs = [
        fake_msg(1, html="<b>Подпись</b>", photo="ph1", media_group_id="g"),
        fake_msg(2, photo="ph2", media_group_id="g"),
        fake_msg(3, video="vid1", media_group_id="g"),
    ]
    d = bc.build_draft(msgs)
    assert d.message_ids == [1, 2, 3]
    assert [i["type"] for i in d.items] == ["photo", "photo", "video"]
    assert d.html == "<b>Подпись</b>"      # подпись только у первого


def test_draft_sorts_album_by_message_id():
    """Telegram может прислать куски не по порядку."""
    msgs = [fake_msg(3, photo="c", media_group_id="g"),
            fake_msg(1, photo="a", html="Текст", media_group_id="g"),
            fake_msg(2, photo="b", media_group_id="g")]
    d = bc.build_draft(msgs)
    assert [i["file_id"] for i in d.items] == ["a", "b", "c"]


def test_input_media_caption_only_on_first():
    items = [{"type": "photo", "file_id": "a"},
             {"type": "photo", "file_id": "b"}]
    media = bc.to_input_media(items, "<b>Подпись</b>")
    assert media[0].caption == "<b>Подпись</b>"
    assert media[0].parse_mode == "HTML"
    assert media[1].caption is None


# ===================== адресаты =====================

def _agent(db, tid, agency_id=1, dm=True, bcast=None):
    db.upsert_agent(tid, f"u{tid}", f"Agent {tid}",
                    agency_id=agency_id, dm_open=dm)
    if bcast is not None:
        db.set_agent_field(tid, bcast=bcast)


def test_recipients_only_subscribed(tmp_path):
    """
    Рассылка уходит только тем, кто нажал Start: писать первым Telegram
    не даёт.
    """
    db = Db(tmp_path / "b.db")
    _agent(db, 1, dm=True)
    _agent(db, 2, dm=False)

    assert [r["telegram_id"] for r in db.broadcast_recipients({})] == [1]


def test_recipients_respect_unsubscribe(tmp_path):
    db = Db(tmp_path / "b2.db")
    _agent(db, 1)
    _agent(db, 2, bcast=0)

    assert [r["telegram_id"] for r in db.broadcast_recipients({})] == [1]


def test_recipients_by_agency(tmp_path):
    db = Db(tmp_path / "b3.db")
    _agent(db, 1, agency_id=10)
    _agent(db, 2, agency_id=20)

    got = db.broadcast_recipients({"agency_id": 20})
    assert [r["telegram_id"] for r in got] == [2]


def test_recipients_active_only(tmp_path):
    db = Db(tmp_path / "b4.db")
    _agent(db, 1)
    _agent(db, 2)
    db.log_fixation(digits="79991234567", agent_telegram_id=1,
                    verdict="unique", amo_lead_id=1, created_at=NOW - 5 * DAY)
    db.log_fixation(digits="79995556677", agent_telegram_id=2,
                    verdict="unique", amo_lead_id=2, created_at=NOW - 90 * DAY)

    got = db.broadcast_recipients({"active_days": 30})
    assert [r["telegram_id"] for r in got] == [1]


def test_broadcast_chats_filtered_by_agency(tmp_path):
    db = Db(tmp_path / "b5.db")
    db.set_meta("chat_agency:-100", "7")
    db.set_meta("chat_agency:-200", "8")

    assert len(db.broadcast_chats({})) == 2
    assert db.broadcast_chats({"agency_id": 8}) == [(-200, 8)]


def test_broadcast_chats_skip_unbound(tmp_path):
    db = Db(tmp_path / "b6.db")
    db.set_meta("chat_agency:-100", "")
    assert db.broadcast_chats({}) == []


# ===================== хранение =====================

def test_broadcast_roundtrip(tmp_path):
    db = Db(tmp_path / "b7.db")
    bid = db.create_broadcast(admin_id=42, src_chat_id=-100,
                              message_ids=[1, 2],
                              items=[{"type": "photo", "file_id": "x"}],
                              html="<b>Текст</b>")
    b = db.get_broadcast(bid)
    assert b["message_ids"] == [1, 2]
    assert b["items"][0]["file_id"] == "x"
    assert b["status"] == "draft"

    db.update_broadcast(bid, html_en="<b>Text</b>", target={"agency_id": 3})
    b = db.get_broadcast(bid)
    assert b["html_en"] == "<b>Text</b>"
    assert b["target"] == {"agency_id": 3}


def test_broadcast_update_ignores_unknown(tmp_path):
    db = Db(tmp_path / "b8.db")
    bid = db.create_broadcast(1, -100, [1], [], "текст")
    db.update_broadcast(bid, hacker="drop")
    assert db.get_broadcast(bid)["html"] == "текст"


# ===================== доставка =====================

class FakeBot:
    def __init__(self, fail_on=()):
        self.copies: list[int] = []
        self.translated: list[tuple[int, str]] = []
        self.fail_on = set(fail_on)

    async def copy_messages(self, chat_id, from_chat_id, message_ids):
        if chat_id in self.fail_on:
            raise RuntimeError("bot was blocked by the user")
        self.copies.append(chat_id)

    async def copy_message(self, chat_id, from_chat_id, message_id):
        if chat_id in self.fail_on:
            raise RuntimeError("bot was blocked by the user")
        self.copies.append(chat_id)

    async def send_message(self, chat_id, text, **kw):
        self.translated.append((chat_id, text))

    async def send_photo(self, chat_id, file_id, caption=None, **kw):
        self.translated.append((chat_id, caption or ""))

    async def send_media_group(self, chat_id, media):
        self.translated.append((chat_id, media[0].caption or ""))


def test_translate_prompt_drops_russian_materials():
    """
    Англоязычным агентствам русские материалы не нужны: ссылки с пометкой
    «Ру» должны выпадать, а у оставшейся английской пометка убираться.
    """
    import llm

    p = llm.TRANSLATE_PROMPT.lower()
    assert "удаляй целиком" in p
    assert "ру" in p and "на русском" in p
    assert "offer: link" in p          # пример желаемого результата


def test_translate_prompt_keeps_formatting_rules():
    import llm

    p = llm.TRANSLATE_PROMPT
    for tag in ("<b>", "<a href", "<tg-spoiler>"):
        assert tag in p
    assert "Эмодзи" in p


def test_long_html_is_never_truncated_mid_tag():
    """
    Превью раньше собиралось вставкой размеченного текста в чужое
    сообщение с обрезкой по длине. Обрезка рвала тег посередине,
    Telegram отказывался принимать такое, и рассылка вставала.
    Теперь превью — настоящие сообщения, обрезки нет вовсе.
    """
    import inspect

    import bot

    src = inspect.getsource(bot.try_capture_broadcast)
    assert "draft.html[:" not in src
    assert "html_en[:" not in src
    assert "send_copy" in src and "send_translated" in src


@pytest.mark.asyncio
async def test_delivery_splits_by_language():
    """Русским — копия один в один, англоязычным — перевод."""
    bot = FakeBot()
    draft = bc.Draft(src_chat_id=-1, message_ids=[1], items=[], html="Привет")
    sent, failed = await bc.deliver(
        bot, [(10, "ru"), (20, "en")], draft, "Hello")

    assert (sent, failed) == (2, 0)
    assert bot.copies == [10]
    assert bot.translated == [(20, "Hello")]


@pytest.mark.asyncio
async def test_delivery_without_translation_falls_back_to_copy():
    """Если перевести не вышло, англоязычным уходит оригинал, а не пустота."""
    bot = FakeBot()
    draft = bc.Draft(src_chat_id=-1, message_ids=[1], html="Привет")
    sent, _ = await bc.deliver(bot, [(20, "en")], draft, None)

    assert sent == 1 and bot.copies == [20]


@pytest.mark.asyncio
async def test_delivery_counts_failures_and_reports_them():
    bot = FakeBot(fail_on={20})
    seen: list[int] = []

    async def on_fail(chat_id, err):
        seen.append(chat_id)

    draft = bc.Draft(src_chat_id=-1, message_ids=[1], html="Привет")
    sent, failed = await bc.deliver(
        bot, [(10, "ru"), (20, "ru"), (30, "ru")], draft, None, on_fail=on_fail)

    assert (sent, failed) == (2, 1)
    assert seen == [20]
    assert bot.copies == [10, 30]      # остальные всё равно получили


# ===================== длинный перевод =====================
#
# 14.10.2026, Eco Invest: англоязычные группы получили перевод рассылки
# обрезанным. Подпись к фото резалась срезом html[:1024] — а 1024 у
# Telegram считается по видимому тексту, без тегов. Ссылка с длинным
# адресом съедала половину лимита, и конец текста пропадал молча.

class RecordingBot:
    """Записывает всё, что ушло адресату, по порядку."""

    def __init__(self):
        self.sent: list[tuple[str, str | None]] = []

    async def send_message(self, chat_id, text, **kw):
        self.sent.append(("text", text))

    async def send_photo(self, chat_id, file_id, caption=None, **kw):
        self.sent.append(("photo", caption))

    async def send_video(self, chat_id, file_id, caption=None, **kw):
        self.sent.append(("video", caption))

    send_animation = send_audio = send_document = send_video

    async def send_media_group(self, chat_id, media):
        self.sent.append(("album", media[0].caption))
        for extra in media[1:]:
            assert extra.caption is None

    def all_text(self) -> str:
        return "".join(t or "" for _, t in self.sent)


LONG_HREF = "https://example.com/offer?" + "x" * 600


def test_visible_length_ignores_tags_and_counts_like_telegram():
    """Лимит Telegram — видимые символы; эмодзи считается за два."""
    assert bc.visible_len(f'<b>Hi</b> <a href="{LONG_HREF}">link</a>') == 7
    assert bc.visible_len("a &amp; b") == 5
    assert bc.visible_len("🎉") == 2


@pytest.mark.asyncio
async def test_caption_with_long_links_is_sent_whole():
    """Подпись в лимите по видимому тексту уходит целиком, под фото."""
    html = (f'<b>Unit offer</b>: <a href="{LONG_HREF}">link</a>\n'
            + "Sea view. " * 60 + "THE END")
    assert len(html) > 1024 and bc.visible_len(html) <= 1024
    bot = RecordingBot()
    draft = bc.Draft(src_chat_id=-1, message_ids=[1],
                     items=[{"type": "photo", "file_id": "ph"}], html="ру")

    await bc.send_translated(bot, 20, draft, html)

    assert bot.sent == [("photo", html)]


@pytest.mark.asyncio
async def test_translation_too_long_for_a_caption_is_not_cut():
    """Не влезает в подпись — фото без подписи, следом текст целиком."""
    html = "<b>Unit offer</b>\n" + "Sea view, quiet area. " * 70 + "THE END"
    assert bc.visible_len(html) > 1024
    bot = RecordingBot()
    draft = bc.Draft(src_chat_id=-1, message_ids=[1],
                     items=[{"type": "photo", "file_id": "ph"}], html="ру")

    await bc.send_translated(bot, 20, draft, html)

    assert bot.sent == [("photo", None), ("text", html)]


@pytest.mark.asyncio
async def test_album_with_a_long_translation_is_not_cut():
    html = "Sea view, quiet area. " * 70 + "THE END"
    bot = RecordingBot()
    draft = bc.Draft(src_chat_id=-1, message_ids=[1, 2],
                     items=[{"type": "photo", "file_id": "a"},
                            {"type": "photo", "file_id": "b"}], html="ру")

    await bc.send_translated(bot, 20, draft, html)

    assert bot.sent == [("album", None), ("text", html)]


def test_no_caption_is_cut_by_raw_length():
    """Срез по длине размеченного текста рвёт и текст, и теги."""
    import inspect

    src = inspect.getsource(bc)
    assert "[:1024]" not in src


class _FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


@pytest.mark.asyncio
async def test_translation_stopped_by_the_length_limit_is_not_used(monkeypatch):
    """
    Модель упёрлась в max_tokens — перевод оборван. Лучше отправить
    оригинал целиком, чем английский без конца.
    """
    import llm

    clf = llm.Classifier("test-key")

    async def fake_post(url, json=None, timeout=None):
        return _FakeResponse({"stop_reason": "max_tokens",
                              "content": [{"type": "text", "text": "Unit off"}]})

    monkeypatch.setattr(clf._client, "post", fake_post)
    try:
        assert await clf.translate("Оффер на юнит, вид на море") is None
    finally:
        await clf.close()


@pytest.mark.asyncio
async def test_complete_translation_is_used(monkeypatch):
    import llm

    clf = llm.Classifier("test-key")

    async def fake_post(url, json=None, timeout=None):
        return _FakeResponse({"stop_reason": "end_turn",
                              "content": [{"type": "text", "text": "Unit offer"}]})

    monkeypatch.setattr(clf._client, "post", fake_post)
    try:
        assert await clf.translate("Оффер на юнит") == "Unit offer"
    finally:
        await clf.close()
