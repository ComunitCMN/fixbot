"""
Язык общения: русский или английский.

Правило простое — бот отвечает на языке того сообщения, на которое
реагирует. В смешанной группе русскому агенту ответит по-русски,
англоязычному по-английски, и настраивать ничего не нужно.

Определяем по алфавиту, а не по словарю: для пары «кириллица / латиница»
этого достаточно и работает мгновенно. Цифры, знаки и телефоны в счёт
не идут — иначе сообщение вида «+7 999 123-45-67» ушло бы в английский.

Если букв нет вовсе (только номер), язык берём из привязки чата, а её
нет — из настроек.
"""

from __future__ import annotations

import re

RU = "ru"
EN = "en"
SUPPORTED = (RU, EN)

_CYRILLIC = re.compile(r"[а-яёА-ЯЁ]")
_LATIN = re.compile(r"[a-zA-Z]")

#: Ниже этой доли кириллицы считаем сообщение английским.
#: Порог низкий: русские часто вставляют латиницу — названия ЖК,
#: имена клиентов, «ok». Одного русского слова достаточно.
_RU_THRESHOLD = 0.15


def detect(text: str, default: str = RU) -> str:
    """Язык текста: ru | en. default — когда букв нет вовсе."""
    if not text:
        return default
    cyr = len(_CYRILLIC.findall(text))
    lat = len(_LATIN.findall(text))
    total = cyr + lat
    if total == 0:
        return default
    return RU if cyr / total >= _RU_THRESHOLD else EN


def detect_many(texts: list[str], default: str = RU) -> str:
    """Язык по нескольким сообщениям — устойчивее, чем по одному короткому."""
    joined = "\n".join(t for t in texts if t)
    return detect(joined, default)


def normalize_lang(value: str | None, fallback: str = RU) -> str:
    v = (value or "").strip().lower()[:2]
    return v if v in SUPPORTED else fallback


#: Столько букв нужно, чтобы менять язык личных уведомлений человека.
#: Одного «ok» мало: русскоязычный агент, ответивший коротко на латинице,
#: не должен из-за этого начать получать уведомления по-английски.
MIN_LETTERS_FOR_PROFILE = 6


def letters(text: str) -> int:
    """Сколько в тексте букв. Цифры и знаки не в счёт."""
    if not text:
        return 0
    return len(_CYRILLIC.findall(text)) + len(_LATIN.findall(text))


def has_letters(text: str) -> bool:
    """Есть ли по чему судить о языке. «+7 999 123-45-67» — нет."""
    return letters(text) > 0


def confident(text: str) -> bool:
    """Достаточно ли текста, чтобы запомнить язык человека надолго."""
    return letters(text) >= MIN_LETTERS_FOR_PROFILE
