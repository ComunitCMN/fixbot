"""
Ядро логики: чей это клиент и что с ним делать.

Главное правило предметной области: **агентская фиксация не даёт эксклюзива.**
Несколько агентств могут вести одного клиента одновременно, и он достаётся
тому, кто первым доведёт до брони. Поэтому совпадение с чужой агентской
фиксацией НЕ блокирует — бот фиксирует клиента и просто предупреждает.

Блокирует только одно: клиент уже принадлежит отделу продаж (пришёл по
рекламе, звонком, с сайта). Определяется по воронке связанной сделки.
Такой клиент освобождается после года без активности.

Модуль намеренно не знает ни про Telegram, ни про amoCRM — только чистые
функции над данными. Это позволяет прогнать все сценарии тестами.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum

RETAIL_TTL_DAYS = 365
DAY = 86400


class Verdict(str, Enum):
    UNIQUE = "unique"                  # 🟢 совпадений нет
    SAME_AGENT = "same_agent"          # ℹ️ он же фиксировал раньше
    SAME_AGENCY = "same_agency"        # ℹ️ коллега из его агентства
    OTHER_AGENCY = "other_agency"      # 🟡 чужая фиксация, но работать можно
    RETAIL_BLOCKED = "retail_blocked"  # 🔴 клиент отдела продаж
    RETAIL_EXPIRED = "retail_expired"  # 🟢 был у отдела продаж, но остыл
    BOOKED_ELSEWHERE = "booked"        # 🔴 уже на брони у другого агентства
    UNKNOWN_ORIGIN = "unknown"         # 🟡 непонятно чей


#: Вердикты, при которых создаём контакт и сделку в amoCRM.
CREATES_FIXATION = {
    Verdict.UNIQUE,
    Verdict.OTHER_AGENCY,
    Verdict.RETAIL_EXPIRED,
}

#: Вердикты, о которых стоит уведомить администратора.
NOTIFIES_ADMIN = {
    Verdict.RETAIL_EXPIRED,
    Verdict.UNKNOWN_ORIGIN,
}


@dataclass
class Decision:
    verdict: Verdict
    #: совпадения, на которых основан вердикт
    reasons: list = field(default_factory=list)
    #: дата первой чужой агентской фиксации
    other_agency_since: int | None = None
    #: сколько ещё агентств ведёт клиента
    other_agency_count: int = 0
    #: последняя активность розничной сделки
    retail_activity: int | None = None
    #: с какого числа клиент числится за отделом продаж
    retail_since: int | None = None
    #: дата предыдущей фиксации этого же агента/агентства
    own_since: int | None = None
    #: имя коллеги, который фиксировал раньше
    colleague: str | None = None

    @property
    def creates_fixation(self) -> bool:
        return self.verdict in CREATES_FIXATION

    @property
    def notifies_admin(self) -> bool:
        return self.verdict in NOTIFIES_ADMIN


def decide(
    matches: list,
    agency_id: int | None,
    agent_telegram_id: int | None,
    now: int | None = None,
    retail_ttl_days: int = RETAIL_TTL_DAYS,
) -> Decision:
    """
    Принимает решение по списку совпадений (db.Match).

    Порядок проверок — от самого блокирующего к самому безобидному.
    Первое сработавшее правило выигрывает.
    """
    now = now or int(time.time())
    ttl = retail_ttl_days * DAY

    if not matches:
        return Decision(Verdict.UNIQUE)

    # --- 1. Розничные сделки: единственное, что реально блокирует ---
    retail = [m for m in matches if getattr(m, "has_retail", False)]
    if retail:
        last = max((m.last_retail_activity or 0) for m in retail)
        since = _earliest(retail) or last or None
        if last and now - last < ttl:
            return Decision(Verdict.RETAIL_BLOCKED, reasons=retail,
                            retail_activity=last, retail_since=since)
        if not last:
            # Розничная сделка есть, но когда шевелилась — неизвестно.
            # Осторожничаем: считаем живой.
            return Decision(Verdict.RETAIL_BLOCKED, reasons=retail,
                            retail_since=since)
        retail_expired_at = last
    else:
        retail_expired_at = None

    # --- 2. Клиент уже на брони: конкуренция закончена ---
    booked = [m for m in matches
              if getattr(m, "booked", False) and not _is_own(m, agency_id)]
    if booked:
        return Decision(Verdict.BOOKED_ELSEWHERE, reasons=booked)

    # --- 3. Происхождение неизвестно: контакт есть, а сделок нет ---
    unknown = [
        m for m in matches
        if getattr(m, "source", "amo") == "amo"
        and not getattr(m, "has_retail", False)
        and not getattr(m, "has_agency", False)
    ]
    if unknown and not _agency_matches(matches):
        return Decision(Verdict.UNKNOWN_ORIGIN, reasons=unknown)

    # --- 4. Свои: тот же агент или коллега из того же агентства ---
    own = [m for m in matches if _is_own(m, agency_id)]
    if own:
        mine = [m for m in own
                if agent_telegram_id is not None
                and getattr(m, "agent_telegram_id", None) == agent_telegram_id]
        if mine:
            return Decision(Verdict.SAME_AGENT, reasons=mine,
                            own_since=_earliest(mine))
        return Decision(Verdict.SAME_AGENCY, reasons=own,
                        own_since=_earliest(own),
                        colleague=next(
                            (m.agent_name for m in own
                             if getattr(m, "agent_name", None)), None))

    # --- 5. Чужие агентства: работать можно, фиксируем ---
    others = [m for m in _agency_matches(matches) if not _is_own(m, agency_id)]
    if others:
        return Decision(
            Verdict.OTHER_AGENCY, reasons=others,
            other_agency_since=_earliest(others),
            other_agency_count=len({
                getattr(m, "agency_id", None) or getattr(m, "agency_company_id", None)
                or id(m) for m in others
            }),
        )

    # --- 6. Розница была, но остыла ---
    if retail_expired_at:
        return Decision(Verdict.RETAIL_EXPIRED, reasons=retail,
                        retail_activity=retail_expired_at)

    return Decision(Verdict.UNIQUE)


# ---------------------------------------------------------------- helpers

def _is_own(m, agency_id: int | None) -> bool:
    """
    Совпадение принадлежит тому же агентству, что и запрашивающий.

    У совпадений из amoCRM агентство хранится как agency_company_id (id компании),
    поэтому вызывающий код обязан заранее проставить в Match.agency_id
    внутренний id из справочника — это делает bot.enrich_matches().
    """
    if agency_id is None:
        return False
    return getattr(m, "agency_id", None) == agency_id


def _agency_matches(matches: list) -> list:
    """Совпадения, которые точно являются агентскими фиксациями."""
    return [
        m for m in matches
        if getattr(m, "has_agency", False) or getattr(m, "source", "") == "chat"
    ]


def _earliest(matches: list) -> int | None:
    dates = [m.created_at for m in matches if getattr(m, "created_at", None)]
    return min(dates) if dates else None
