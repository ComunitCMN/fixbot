"""
Частный агент: как он представляется и как владелец его принимает.

## Зачем отдельно

Агент из агентства опознаётся по рабочему чату. У частника чата нет,
и единственный пропуск — решение владельца. Значит нужен путь: человек
представляется, владелец видит карточку и жмёт кнопку.

Тут только разбор имени и тексты. Кто кому отвечает — в `access.py`,
кнопки и отправка — в боте.

## Почему имя, а не что-то ещё

Ничего надёжнее у нас всё равно нет: телефон человек может назвать
чужой, а имя владелец сверит с тем, что знает о рынке. Задача имени —
не доказать личность, а дать владельцу основание нажать «принять»
или «отказать».
"""

from __future__ import annotations

import re

#: Слишком короткое сойдёт за случайную реплику, слишком длинное —
#: за пересланный текст.
MIN_LEN = 4
MAX_LEN = 60

_JUNK = re.compile(r"[0-9@#$%^&*_+=<>{}\[\]|\\/]")


def looks_like_name(text: str) -> bool:
    """
    Похоже ли на имя, которым человек представляется.

    Нарочно строго: если бот примет за имя случайное «привет» или
    пересланное объявление, владелец получит карточку с мусором
    и перестанет их читать.
    """
    s = (text or "").strip()
    if not (MIN_LEN <= len(s) <= MAX_LEN):
        return False
    if _JUNK.search(s):
        return False
    if "\n" in s:
        return False
    words = s.split()
    if not (1 <= len(words) <= 4):
        return False
    # Хотя бы одно слово длиннее двух букв, и всё — буквы или дефисы.
    return (any(len(w) > 2 for w in words)
            and all(re.fullmatch(r"[^\W\d_]+(-[^\W\d_]+)*", w) for w in words))


def clean_name(text: str) -> str:
    return " ".join((text or "").split())[:MAX_LEN]


ASK_INTRO = {
    "ru": ("👋 Здравствуйте! Я бот застройщика — проверяю и фиксирую "
           "клиентов.\n\n"
           "Если вы работаете сами, без агентства, — пришлите имя "
           "и фамилию. Я передам застройщику, и как только он подтвердит, "
           "вы сможете фиксировать клиентов прямо здесь.\n\n"
           "Если вы из агентства — просто напишите в рабочем чате, "
           "я вас запомню, и подтверждение не понадобится."),
    "en": ("👋 Hello! I'm the developer's bot — I check and register "
           "clients.\n\n"
           "If you work on your own, without an agency, send me your full "
           "name. I'll pass it to the developer, and once they approve "
           "you'll be able to register clients right here.\n\n"
           "If you're with an agency, just write in your work chat — "
           "I'll remember you, no approval needed."),
}

APPLIED = {
    "ru": ("✅ Передал застройщику: <b>{name}</b>.\n\n"
           "Как только он подтвердит, я напишу вам сюда. Обычно это "
           "недолго."),
    "en": ("✅ Sent to the developer: <b>{name}</b>.\n\n"
           "I'll message you here as soon as they approve. It usually "
           "doesn't take long."),
}

APPROVED = {
    "ru": ("✅ Застройщик вас подтвердил.\n\n"
           "Теперь можно фиксировать клиентов прямо здесь: пришлите имя "
           "и телефон одним сообщением. Скрыть можно максимум две "
           "последние цифры."),
    "en": ("✅ The developer has approved you.\n\n"
           "You can now register clients right here: send the client's "
           "name and phone in one message. At most the last two digits "
           "may be hidden."),
}

#: Отказ мягкий и без объяснений: причина — дело застройщика, а не бота.
DECLINED = {
    "ru": ("К сожалению, подтвердить не получилось. "
           "Уточните, пожалуйста, у застройщика напрямую."),
    "en": ("Unfortunately we couldn't confirm you. "
           "Please check with the developer directly."),
}


def application_card(name: str, username: str | None,
                     telegram_id: int) -> str:
    """Карточка заявки владельцу."""
    import texts

    handle = f" (@{texts.esc(username)})" if username else ""
    return "\n".join([
        "🙋 <b>Заявка от частного агента</b>", "",
        f"Имя: <b>{texts.esc(name)}</b>{handle}",
        f"Telegram ID: <code>{telegram_id}</code>", "",
        "Приняв, вы разрешите ему проверять и фиксировать клиентов "
        "в личной переписке с ботом. Он попадёт в статистику отдельной "
        "строкой, как агентство.",
    ])
