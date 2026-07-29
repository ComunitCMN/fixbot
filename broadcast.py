"""
Рассылки агентам и в рабочие чаты.

Задача звучит просто — «разошли вот это сообщение», — но точная копия
и перевод несовместимы: переведённый текст это уже другое сообщение.
Поэтому русскоязычным уходит копия один в один (Telegram умеет копировать
сообщение со всеми фото, видео и оформлением), а англоязычным — те же
медиа с переведённой подписью, где разметка сохранена.

Альбом Telegram присылает не одним сообщением, а несколькими подряд
с общим media_group_id. Поэтому после первого куска ждём остальные
ALBUM_WAIT секунд и только потом собираем рассылку.

Ограничения Telegram: примерно 30 сообщений в секунду. Шлём пачками
с паузами, иначе часть адресатов просто не получит письмо.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field

from aiogram.types import (InputMediaAnimation, InputMediaAudio,
                           InputMediaDocument, InputMediaPhoto,
                           InputMediaVideo, Message)

log = logging.getLogger(__name__)

#: Сколько ждать остальные куски альбома.
ALBUM_WAIT = 1.5

#: Сообщений в секунду. Ниже официального лимита — с запасом.
RATE = 20
#: Пауза между пачками.
BATCH_PAUSE = 1.0

_MEDIA_CLASS = {
    "photo": InputMediaPhoto,
    "video": InputMediaVideo,
    "document": InputMediaDocument,
    "animation": InputMediaAnimation,
    "audio": InputMediaAudio,
}


@dataclass
class Draft:
    """Собранное для рассылки сообщение."""

    src_chat_id: int
    message_ids: list[int] = field(default_factory=list)
    items: list[dict] = field(default_factory=list)
    html: str = ""

    @property
    def has_media(self) -> bool:
        return bool(self.items)


def extract_media(m: Message) -> dict | None:
    """Тип и file_id вложения — чтобы переслать его с другой подписью."""
    if m.photo:
        return {"type": "photo", "file_id": m.photo[-1].file_id}
    if m.video:
        return {"type": "video", "file_id": m.video.file_id}
    if m.animation:
        return {"type": "animation", "file_id": m.animation.file_id}
    if m.document:
        return {"type": "document", "file_id": m.document.file_id}
    if m.audio:
        return {"type": "audio", "file_id": m.audio.file_id}
    return None


def message_html(m: Message) -> str:
    """
    Текст сообщения с разметкой Telegram.

    html_text отдаёт жирный, курсив, ссылки и спойлеры в виде тегов —
    ровно то, что нужно и для перевода, и для повторной отправки.
    """
    if m.html_text:
        return m.html_text
    return m.text or m.caption or ""


def build_draft(messages: list[Message]) -> Draft:
    """Собирает черновик из одного сообщения или из альбома."""
    messages = sorted(messages, key=lambda x: x.message_id)
    draft = Draft(src_chat_id=messages[0].chat.id)

    for m in messages:
        draft.message_ids.append(m.message_id)
        media = extract_media(m)
        if media:
            draft.items.append(media)
        text = message_html(m)
        # Подпись у альбома обычно только на первом элементе.
        if text and not draft.html:
            draft.html = text

    return draft


def to_input_media(items: list[dict], caption: str | None) -> list:
    """Готовит альбом к отправке, подпись вешается на первый элемент."""
    out = []
    for i, it in enumerate(items):
        cls = _MEDIA_CLASS.get(it["type"], InputMediaDocument)
        kwargs = {"media": it["file_id"]}
        if i == 0 and caption:
            kwargs["caption"] = caption[:1024]
            kwargs["parse_mode"] = "HTML"
        out.append(cls(**kwargs))
    return out


async def send_copy(bot, chat_id: int, draft: Draft) -> None:
    """Копия один в один — со всеми медиа и оформлением."""
    if len(draft.message_ids) > 1:
        try:
            await bot.copy_messages(chat_id=chat_id,
                                    from_chat_id=draft.src_chat_id,
                                    message_ids=draft.message_ids)
            return
        except Exception:  # noqa: BLE001
            # Старые версии Bot API не умеют копировать пачкой —
            # тогда шлём по одному, альбом просто рассыплется.
            log.info("copy_messages не поддержан, копирую по одному")

    for mid in draft.message_ids:
        await bot.copy_message(chat_id=chat_id,
                               from_chat_id=draft.src_chat_id,
                               message_id=mid)


async def send_translated(bot, chat_id: int, draft: Draft, html: str) -> None:
    """Те же вложения, но с переведённой подписью."""
    if not draft.items:
        await bot.send_message(chat_id, html, disable_web_page_preview=False)
        return

    if len(draft.items) == 1:
        it = draft.items[0]
        sender = {
            "photo": bot.send_photo, "video": bot.send_video,
            "animation": bot.send_animation, "audio": bot.send_audio,
        }.get(it["type"], bot.send_document)
        await sender(chat_id, it["file_id"], caption=html[:1024] or None)
        return

    await bot.send_media_group(chat_id, to_input_media(draft.items, html))


async def deliver(bot, targets: list[tuple[int, str]], draft: Draft,
                  html_en: str | None,
                  on_fail=None) -> tuple[int, int]:
    """
    Рассылает по списку (chat_id, язык). Возвращает (доставлено, ошибок).

    Пачками с паузой: Telegram режет примерно на 30 сообщениях в секунду,
    и без пауз часть адресатов молча не получит письмо.
    """
    sent = failed = 0
    for i, (chat_id, lang) in enumerate(targets):
        try:
            if lang == "en" and html_en:
                await send_translated(bot, chat_id, draft, html_en)
            else:
                await send_copy(bot, chat_id, draft)
            sent += 1
        except Exception as e:  # noqa: BLE001
            failed += 1
            log.info("рассылка: не доставлено в %s — %s", chat_id, str(e)[:120])
            if on_fail:
                await on_fail(chat_id, e)

        if (i + 1) % RATE == 0:
            await asyncio.sleep(BATCH_PAUSE)

    return sent, failed
