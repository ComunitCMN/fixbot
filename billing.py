"""
Расчёт обслуживания: сколько клиент должен и что делать сегодня.

Здесь только счёт и календарь — ни Telegram, ни базы. Так эту часть можно
проверить целиком, не поднимая бота: деньги и отключения слишком дороги,
чтобы полагаться на «вроде работает».

## Как устроен период

Периоды идут календарными месяцами от даты начала обслуживания. Начали
5 октября — значит сроки 5 ноября, 5 декабря и так далее. Если в месяце
нет такого числа (начали 31-го), берётся последний день месяца.

## Что происходит по дням

    −3 дня   готовим счёт, спрашиваем оператора про реквизиты
     день Х  срок оплаты
    +7 дней  одно напоминание клиенту
    +13      предупреждение оператору: завтра приостановка
    +14      приостановка

Два правила защищают от несправедливого отключения:

* **клиента не наказывают за молчание оператора.** Пока счёт не отправлен,
  приостановки не будет — бот будет напоминать оператору, а не рубить
  клиенту работу за чужую забывчивость;
* **предупреждение нельзя проскочить.** Если сервер лежал неделю и очнулся
  на 20-й день, сначала уйдёт предупреждение и только потом приостановка.
  Оператор всегда получает шанс сказать «оплачено».
"""

from __future__ import annotations

import calendar
from dataclasses import dataclass
from datetime import date
from enum import Enum

#: За сколько дней до срока готовим счёт.
PREPARE_DAYS = 3
#: Через сколько дней после срока — одно напоминание клиенту.
REMIND_AFTER = 7
#: За сколько дней до приостановки предупреждаем оператора.
WARN_BEFORE_PAUSE = 1
#: Через сколько дней после срока приостанавливаем.
PAUSE_AFTER = 14
#: Как часто напоминать оператору, что счёт так и не ушёл.
NUDGE_EVERY = 7


@dataclass(frozen=True)
class Plan:
    """
    Условия обслуживания. У каждого клиента свои — в коде ничего не зашито.

    `threshold` — граница: строго меньше берём `low`, начиная с неё `high`.
    «До ста — сорок, от ста — семьдесят» это threshold=100.
    """
    threshold: int = 100
    low: int = 40
    high: int = 70
    currency: str = "USD"

    def amount(self, fixations: int) -> int:
        return self.high if fixations >= self.threshold else self.low


class Action(Enum):
    NOTHING = "nothing"
    #: Сообщить клиенту, что начался оплачиваемый месяц. Уходит само:
    #: это не требование денег, а предупреждение, о котором договорились
    #: заранее. Счёт с реквизитами — другое дело, он идёт через оператора.
    ANNOUNCE_START = "announce_start"
    #: Показать оператору сумму и спросить про реквизиты.
    PREPARE_INVOICE = "prepare_invoice"
    #: Счёт не отправлен, а срок уже подошёл — толкаем оператора.
    NUDGE_OPERATOR = "nudge_operator"
    #: Одно вежливое напоминание клиенту.
    REMIND_CLIENT = "remind_client"
    #: «Завтра приостановлю» — оператору.
    WARN_OPERATOR = "warn_operator"
    PAUSE = "pause"


@dataclass(frozen=True)
class State:
    """Что уже сделано в текущем периоде."""
    announced: bool = False
    #: Сумму оператору уже показали и про реквизиты спросили.
    prepared: bool = False
    invoice_sent: bool = False
    reminded: bool = False
    warned: bool = False
    paid: bool = False
    paused: bool = False
    #: Когда последний раз толкали оператора. Раз в неделю, а не каждый
    #: день: ежедневное одинаковое напоминание перестают замечать через
    #: три дня, и тогда оно не работает вовсе.
    last_nudge: date | None = None


def add_month(d: date, months: int = 1) -> date:
    """
    Прибавляет месяцы, не выходя за конец месяца.

    31 января + месяц = 28 февраля, а не 3 марта. Иначе срок оплаты
    уползал бы вперёд с каждым месяцем.
    """
    total = d.month - 1 + months
    year = d.year + total // 12
    month = total % 12 + 1
    day = min(d.day, calendar.monthrange(year, month)[1])
    return date(year, month, day)


def open_period(start: date, last_closed_due: date | None = None
                ) -> tuple[date, date]:
    """
    Незакрытый период: (начало, срок оплаты).

    Период двигается **не по календарю, а по оплате**. Пока за ноябрь
    не заплатили, декабрьский не начинается — иначе неоплаченный месяц
    тихо уезжал бы в прошлое вместе со всеми напоминаниями, и клиент,
    который не заплатил ни разу, не получил бы ни одного напоминания.

    Начало следующего — срок предыдущего, а не дата платежа: заплатил
    20 ноября за ноябрь — следующий счёт всё равно 5 декабря, сроки
    не съезжают.
    """
    begin = last_closed_due or start
    return begin, add_month(begin)


def decide(*, due: date, today: date, state: State,
           begin: date | None = None) -> Action:
    """Что делать сегодня. Одно действие за раз — их и не бывает больше."""
    if state.paused:
        return Action.NOTHING

    # Начало периода объявляем даже если он уже оплачен вперёд: человек
    # должен понимать, за что и к какому числу с него спросят.
    if begin is not None and today >= begin and not state.announced:
        return Action.ANNOUNCE_START

    if state.paid:
        return Action.NOTHING

    days = (today - due).days

    if not state.invoice_sent:
        # Спрашиваем про реквизиты один раз: текст один и тот же, и три
        # одинаковых сообщения подряд человек начнёт пролистывать.
        if days >= -PREPARE_DAYS and not state.prepared:
            return Action.PREPARE_INVOICE
        if days >= 0 and (state.last_nudge is None
                          or (today - state.last_nudge).days >= NUDGE_EVERY):
            return Action.NUDGE_OPERATOR
        return Action.NOTHING

    # Дальше — только когда клиент счёт получил.
    if days >= REMIND_AFTER and not state.reminded:
        return Action.REMIND_CLIENT
    if days >= PAUSE_AFTER - WARN_BEFORE_PAUSE and not state.warned:
        return Action.WARN_OPERATOR
    if days >= PAUSE_AFTER:
        return Action.PAUSE
    return Action.NOTHING
