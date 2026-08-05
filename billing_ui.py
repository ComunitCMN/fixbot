"""
Раздел «Оплаты» в боте оператора: тексты и кнопки.

Вынесено из bot.py, потому что тут есть что проверять: суммы, состояния
периодов и то, какие кнопки в каком состоянии показывать. Держать это
среди обработчиков — значит не проверить никогда.

Всё чистое: на вход строки из базы, на выход текст и разметка.
"""

from __future__ import annotations

from datetime import date

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

import billing as bl
import texts

ISO = "%Y-%m-%d"


def _d(s: str | None) -> date | None:
    return date.fromisoformat(s) if s else None


def money(row, amount) -> str:
    cur = "$" if row["currency"] == "USD" else row["currency"] + " "
    return f"{cur}{amount}"


def status_of(row, period) -> str:
    """Короткая метка состояния — её видно в списке клиентов."""
    if row["paused"]:
        return "⏸"
    if period is None or period["paid_at"] is not None:
        return "✅"
    if period["invoice_sent"]:
        return "📨"
    if period["prepared"]:
        return "📝"
    return "🕓"


def overview_text(items: list[tuple]) -> str:
    """
    Список клиентов с состоянием оплаты.

    `items` — [(billing_row, period_row, due)].
    """
    if not items:
        return ("💰 <b>Оплаты</b>\n\nНи одному клиенту обслуживание пока "
                "не настроено.\n\nОткройте клиента в «🗂 Мои клиенты» и "
                "задайте условия.")

    lines = ["💰 <b>Оплаты</b>", ""]
    for row, period, due in items:
        mark = status_of(row, period)
        amount = (period["amount"] if period and period["amount"]
                  else None)
        tail = f" · {money(row, amount)}" if amount else ""
        lines.append(f"{mark} <b>{texts.esc(row['slug'])}</b> — "
                     f"срок {texts.human_date(due)}{tail}")

    lines += ["", "🕓 ждём · 📝 счёт готов · 📨 отправлен · "
                  "✅ оплачено · ⏸ приостановлен"]
    return "\n".join(lines)


def overview_kb(items: list[tuple]) -> list[list[InlineKeyboardButton]]:
    return [[InlineKeyboardButton(
        text=f"{status_of(row, period)} {row['slug']}",
        callback_data=f"bl:open:{row['slug']}")] for row, period, _ in items]


def client_text(row, period, begin: date, due: date, fixations: int) -> str:
    plan = bl.Plan(threshold=row["threshold"], low=row["low"],
                   high=row["high"], currency=row["currency"])
    amount = (period["amount"] if period and period["amount"]
              else plan.amount(fixations))

    wallet = (f"<code>{texts.esc(row['wallet'])}</code>" if row["wallet"]
              else "❗️ не заданы")
    note = f" ({texts.esc(row['wallet_note'])})" if row["wallet_note"] else ""

    state = "приостановлен ⏸" if row["paused"] else {
        "✅": "оплачено", "📨": "счёт отправлен, ждём оплату",
        "📝": "счёт готов, не отправлен", "🕓": "период идёт",
    }[status_of(row, period)]

    return "\n".join([
        f"💰 <b>{texts.esc(row['slug'])}</b>", "",
        f"Период: {texts.human_date(begin)} — {texts.human_date(due)}",
        f"Фиксаций в CRM: <b>{fixations}</b>",
        f"К оплате: <b>{money(row, amount)}</b>",
        f"Состояние: {state}", "",
        f"Условия: до {plan.threshold} — {money(row, plan.low)}, "
        f"от {plan.threshold} — {money(row, plan.high)}",
        f"Кошелёк{note}: {wallet}",
    ])


def client_kb(row, period) -> list[list[InlineKeyboardButton]]:
    """
    Кнопки под карточкой клиента.

    Показываем только то, что сейчас осмысленно: «Отправить счёт» без
    реквизитов или после отправки — приглашение к ошибке.
    """
    slug = row["slug"]
    rows: list[list[InlineKeyboardButton]] = []
    paid = period is not None and period["paid_at"] is not None

    if not paid and not period["invoice_sent"] and row["wallet"]:
        rows.append([InlineKeyboardButton(
            text="📨 Отправить счёт", callback_data=f"bl:send:{slug}")])

    if not paid and period["invoice_sent"]:
        rows.append([InlineKeyboardButton(
            text="✅ Оплачено", callback_data=f"bl:paid:{slug}")])

    rows.append([InlineKeyboardButton(
        text="💳 Реквизиты", callback_data=f"bl:wallet:{slug}")])
    rows.append([InlineKeyboardButton(
        text="📋 Показать фиксации", callback_data=f"bl:list:{slug}")])

    if row["paused"]:
        rows.append([InlineKeyboardButton(
            text="▶️ Снять приостановку", callback_data=f"bl:resume:{slug}")])

    rows.append([InlineKeyboardButton(text="← Оплаты",
                                      callback_data="m:billing")])
    return rows


ASK_WALLET = (
    "💳 Пришлите адрес кошелька <b>ответом на это сообщение</b>.\n\n"
    "Можно двумя строками: первой адрес, второй сеть — например\n"
    "<code>TXxxxxxxxxxxxxxxxx</code>\n<code>USDT TRC-20</code>\n\n"
    "Следом можно прислать QR картинкой, он уйдёт клиенту вместе со счётом.")

ASK_START = (
    "📅 Пришлите дату начала обслуживания <b>ответом на это сообщение</b>, "
    "в виде <code>2026-10-05</code>.\n\n"
    "От неё считаются периоды: первый счёт будет через месяц.")


def parse_wallet(text: str) -> tuple[str, str]:
    """Адрес и сеть из присланного текста. Вторая строка необязательна."""
    lines = [ln.strip() for ln in (text or "").splitlines() if ln.strip()]
    if not lines:
        return "", ""
    return lines[0], (lines[1] if len(lines) > 1 else "")


def fixations_text(rows: list, begin: date, due: date) -> str:
    """
    Список фиксаций за период — чтобы было чем ответить на спор о сумме.
    """
    head = (f"📋 <b>Фиксации {texts.human_date(begin)} — "
            f"{texts.human_date(due)}</b>")
    if not rows:
        return head + "\n\nЗа период ни одной."

    lines = [head, ""]
    for r in rows[:50]:
        when = texts.when(r["created_at"])
        who = texts.esc(r["agency"] or "—")
        name = texts.esc(r["client_name"] or "без имени")
        lines.append(f"{when} · {name} · {who}")
    if len(rows) > 50:
        lines.append(f"\n…и ещё {len(rows) - 50}")
    return "\n".join(lines)
