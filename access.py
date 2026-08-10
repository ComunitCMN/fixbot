"""
Кому бот отвечает по существу, а кому — вежливо ничего.

## Зачем это нужно

Вердикт «клиент у отдела продаж» — это сведения о клиентской базе
застройщика. В группе пропуском служит сама принадлежность к рабочему
чату агентства: посторонний туда не попадёт. В личке пропуска нет
никакого, и `/check` какое-то время отвечал кому угодно — достаточно
было узнать адрес бота, а его знают агенты из десятков чатов.

## Правило

Отвечаем по существу:

* оператору и владельцу — это их бот;
* агенту, которого бот видел в подключённой группе;
* частному агенту, которого владелец принял кнопкой.

Всем остальным — мягкий отказ без единого намёка на содержимое базы.

## Про ограничение частоты

Считаем **проверки**, а не фиксации. Двадцать фиксаций за час живой
агент и не сделает, а вот двадцать проверок подряд — это уже перебор
номеров, то есть разведка базы.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum

#: Сколько проверок в час позволяем одному человеку.
LOOKUPS_PER_HOUR = 20
HOUR = 3600


class Access(Enum):
    #: Отвечаем полностью.
    ALLOWED = "allowed"
    #: Заявка подана, ждёт владельца.
    PENDING = "pending"
    #: Незнакомый человек — предлагаем представиться.
    STRANGER = "stranger"
    #: Владелец отказал.
    REJECTED = "rejected"
    #: Слишком часто проверяет.
    TOO_MANY = "too_many"


def decide(*, has_menu: bool, agent: dict | None,
           lookups_last_hour: int = 0) -> Access:
    """
    Что позволено этому человеку.

    `agent` — строка из реестра агентов или None. Значение `status`:
    пусто или «active» — обычный агент, «pending» — ждёт подтверждения,
    «rejected» — отказано.
    """
    if has_menu:
        return Access.ALLOWED          # оператор и владелец без ограничений

    if agent is None:
        return Access.STRANGER

    status = (agent.get("status") or "active").strip()
    if status == "rejected":
        return Access.REJECTED
    if status == "pending":
        return Access.PENDING
    if not agent.get("agency_id"):
        # Бот видел человека, но не знает, от кого он. Пусть представится.
        return Access.STRANGER

    if lookups_last_hour >= LOOKUPS_PER_HOUR:
        return Access.TOO_MANY
    return Access.ALLOWED


@dataclass
class LookupCounter:
    """
    Счётчик проверок за последний час.

    В памяти, а не в базе: ограничение нужно на часы, переживать
    перезапуск ему незачем, а лишняя запись на каждый чих — нет.
    """
    hits: dict[int, list[float]] = field(default_factory=dict)

    def count(self, user_id: int, now: float | None = None) -> int:
        now = now if now is not None else time.time()
        recent = [t for t in self.hits.get(user_id, []) if now - t < HOUR]
        self.hits[user_id] = recent
        return len(recent)

    def add(self, user_id: int, now: float | None = None) -> None:
        now = now if now is not None else time.time()
        self.hits.setdefault(user_id, []).append(now)
