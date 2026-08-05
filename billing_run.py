"""
Ежедневная проверка обслуживания у всех клиентов.

Запускается раз в сутки в боте оператора. Ходит по клиентам, спрашивает
`billing.decide`, что сегодня положено, и делает ровно это.

## Почему отправка вынесена наружу

`run_once` не знает ни про Telegram, ни про httpx: кому и что слать,
передаётся функциями. Так весь календарь — объявление, счёт, напоминание,
предупреждение, приостановка — прогоняется тестами за доли секунды и без
единого живого сообщения. Ошибиться здесь дорого: на том конце деньги
и отключение работающего клиента.

## Кто кому пишет

Оператору — его собственный бот. Владельцу-застройщику — **его** бот,
тот, который он знает: сообщение о деньгах, пришедшее от незнакомого
бота, выглядит как мошенничество.
"""

from __future__ import annotations

import logging
from datetime import date
from pathlib import Path

import billing as bl
import clients as cl
import texts

log = logging.getLogger(__name__)

ISO = "%Y-%m-%d"


def _d(s: str | None) -> date | None:
    return date.fromisoformat(s) if s else None


def _plan(row) -> bl.Plan:
    return bl.Plan(threshold=row["threshold"], low=row["low"],
                   high=row["high"], currency=row["currency"])


def _state(period, paused: bool) -> bl.State:
    return bl.State(
        announced=bool(period["announced"]),
        prepared=bool(period["prepared"]),
        invoice_sent=bool(period["invoice_sent"]),
        reminded=bool(period["reminded"]),
        warned=bool(period["warned"]),
        paid=period["paid_at"] is not None,
        paused=paused,
        last_nudge=_d(period["last_nudge"]),
    )


def _midnight(d: date) -> int:
    import time as _t
    return int(_t.mktime(d.timetuple()))


async def run_once(*, db, clients_dir: str, today: date,
                   to_operator, to_owner) -> list[tuple[str, str]]:
    """
    Один проход по всем клиентам. Возвращает [(клиент, действие)] — для
    журнала и для тестов.

    `to_operator(slug, text, kind, extra)` — сообщение оператору.
    `to_owner(slug, folder, text)` — сообщение владельцу его же ботом.
    """
    done: list[tuple[str, str]] = []
    root = Path(clients_dir).expanduser()

    for row in db.all_billing():
        slug = row["slug"]
        folder = root / slug
        start = _d(row["start_date"])
        if start is None:
            continue

        begin, due = bl.open_period(start, _d(row["closed_due"]))
        period = db.period(slug, due.strftime(ISO), begin.strftime(ISO))
        state = _state(period, bool(row["paused"]))
        action = bl.decide(begin=begin, due=due, today=today, state=state)

        if action is bl.Action.NOTHING:
            continue

        plan = _plan(row)
        try:
            await _do(action, db=db, row=row, folder=folder, begin=begin,
                      due=due, plan=plan, period=period, today=today,
                      to_operator=to_operator, to_owner=to_owner)
        except Exception:  # noqa: BLE001
            # Один упавший клиент не должен останавливать остальных:
            # иначе сломанный .env у одного лишит счетов всех.
            log.exception("Обслуживание %s: не удалось выполнить %s",
                          slug, action.value)
            continue
        done.append((slug, action.value))

    return done


async def _do(action, *, db, row, folder: Path, begin: date, due: date,
              plan: bl.Plan, period, today: date, to_operator, to_owner):
    slug, iso_due = row["slug"], due.strftime(ISO)
    first = row["closed_due"] is None

    if action is bl.Action.ANNOUNCE_START:
        await to_owner(slug, folder, texts.period_started(
            begin=begin, due=due, plan=plan, first=first))
        db.mark_period(slug, iso_due, announced=1)
        return

    if action is bl.Action.PREPARE_INVOICE:
        n = cl.billable_fixations(folder, _midnight(begin), _midnight(due))
        amount = plan.amount(n)
        db.mark_period(slug, iso_due, prepared=1, fixations=n, amount=amount)
        await to_operator(slug, _prepare_text(row, begin, due, n, amount),
                          "prepare", {"due": iso_due})
        return

    if action is bl.Action.NUDGE_OPERATOR:
        await to_operator(slug, _nudge_text(row, due, period), "nudge",
                          {"due": iso_due})
        db.mark_period(slug, iso_due, last_nudge=today.strftime(ISO))
        return

    if action is bl.Action.REMIND_CLIENT:
        await to_owner(slug, folder, texts.invoice_reminder(
            due=due, amount=period["amount"] or plan.amount(0),
            currency=plan.currency, wallet=row["wallet"] or "—"))
        db.mark_period(slug, iso_due, reminded=1)
        return

    if action is bl.Action.WARN_OPERATOR:
        await to_operator(slug, _warn_text(row, due), "warn", {"due": iso_due})
        db.mark_period(slug, iso_due, warned=1)
        return

    if action is bl.Action.PAUSE:
        cl.set_paused(folder, True, f"не оплачен период до {iso_due}\n")
        db.set_paused(slug, True)
        await to_operator(slug, _paused_text(row, due), "paused",
                          {"due": iso_due})
        return


# --------------------------------------------------------------------------
# Тексты оператору. Отдельно от клиентских: тут можно прямо, без реверансов.
# --------------------------------------------------------------------------

def _name(row) -> str:
    return texts.esc(row["slug"])


def _money(row, amount) -> str:
    cur = "$" if row["currency"] == "USD" else row["currency"] + " "
    return f"{cur}{amount}"


def _prepare_text(row, begin, due, n, amount) -> str:
    w = row["wallet"]
    wallet = (f"<code>{texts.esc(w)}</code>"
              if w else "❗️ <b>реквизиты не заданы</b>")
    note = f" ({texts.esc(row['wallet_note'])})" if row["wallet_note"] else ""
    return (
        f"💰 <b>Счёт готов — {_name(row)}</b>\n\n"
        f"Период: {texts.human_date(begin)} — {texts.human_date(due)}\n"
        f"Фиксаций в CRM: <b>{n}</b>\n"
        f"К оплате: <b>{_money(row, amount)}</b>\n\n"
        f"Кошелёк{note}:\n{wallet}\n\n"
        f"Отправить клиенту?")


def _nudge_text(row, due, period) -> str:
    return (f"⏳ <b>{_name(row)}</b> — счёт так и не отправлен.\n"
            f"Срок был {texts.human_date(due)}, "
            f"к оплате {_money(row, period['amount'] or 0)}.\n\n"
            f"Пока вы не отправите, клиент ничего не знает — "
            f"и приостановки не будет.")


def _warn_text(row, due) -> str:
    return (f"⚠️ <b>{_name(row)}</b> — завтра приостановлю.\n"
            f"Срок {texts.human_date(due)} прошёл две недели назад, "
            f"оплата не отмечена.\n\n"
            f"Если деньги пришли — нажмите «Оплачено», и ничего не случится.")


def _paused_text(row, due) -> str:
    return (f"⏸ <b>{_name(row)}</b> приостановлен.\n"
            f"Не оплачен период до {texts.human_date(due)}.\n\n"
            f"Агентам бот отвечает нейтрально, про оплату им не сообщается. "
            f"Снимается кнопкой «Оплачено».")
