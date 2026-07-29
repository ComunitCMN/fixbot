"""
Меню управления ботом.

Две роли, потому что бота держит один человек, а пользуется другой:

  **оператор** — тот, у кого бот стоит на сервере. Ему нужно техническое:
  воронки amoCRM, синхронизация, диагностика.

  **владелец** — застройщик, для которого бот работает. Ему нужны
  рассылки, группы, агентства и статистика. Токены и воронки он видеть
  не должен: это не его слой, и ошибиться там легко.

Владелец может выдать доступ своим сотрудникам — маркетологу, руководителю
отдела продаж. Оператору при этом приходит уведомление: он отвечает за
сервер и должен знать, кто получил доступ.

Всё меню — кнопки. Команды остаются, но помнить их не требуется.
"""

from __future__ import annotations

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

OPERATOR = "operator"
OWNER = "owner"
NOBODY = "nobody"


def role_of(user_id: int, cfg, db) -> str:
    if user_id in cfg.operator_ids:
        return OPERATOR
    if user_id in cfg.owner_ids or db.is_staff(user_id):
        return OWNER
    return NOBODY


def main_menu(role: str) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text="📣 Рассылка", callback_data="m:bcast")],
        [InlineKeyboardButton(text="💬 Группы", callback_data="m:chats")],
        [InlineKeyboardButton(text="🏢 Агентства", callback_data="m:agencies")],
        [InlineKeyboardButton(text="📊 Статистика", callback_data="m:stats")],
        [InlineKeyboardButton(text="👥 Сотрудники", callback_data="m:staff")],
        [InlineKeyboardButton(text="❓ Как это работает",
                              callback_data="m:help")],
    ]
    if role == OPERATOR:
        rows.append([InlineKeyboardButton(text="🗂 Мои клиенты",
                                          callback_data="m:clients")])
        rows.append([InlineKeyboardButton(text="🔧 Техническое",
                                          callback_data="m:tech")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def back_kb(extra: list[list[InlineKeyboardButton]] | None = None
            ) -> InlineKeyboardMarkup:
    rows = list(extra or [])
    rows.append([InlineKeyboardButton(text="← Меню", callback_data="m:root")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def tech_menu() -> InlineKeyboardMarkup:
    return back_kb([
        [InlineKeyboardButton(text="🔀 Разметка воронок",
                              callback_data="m:pipelines")],
        [InlineKeyboardButton(text="🔄 Синхронизировать amoCRM",
                              callback_data="m:sync")],
        [InlineKeyboardButton(text="🩺 Состояние", callback_data="m:health")],
    ])


def stats_menu() -> InlineKeyboardMarkup:
    return back_kb([
        [InlineKeyboardButton(text="За 7 дней", callback_data="m:stats:7"),
         InlineKeyboardButton(text="30 дней", callback_data="m:stats:30")],
        [InlineKeyboardButton(text="За всё время", callback_data="m:stats:0")],
    ])


def staff_menu(rows: list) -> InlineKeyboardMarkup:
    kb = [[InlineKeyboardButton(
        text=f"➖ {r['name'] or r['telegram_id']}",
        callback_data=f"m:staffdel:{r['telegram_id']}")] for r in rows[:20]]
    kb.append([InlineKeyboardButton(text="➕ Добавить сотрудника",
                                    callback_data="m:staffadd")])
    return back_kb(kb)


# --------------------------------------------------------------------------
# Тексты разделов
# --------------------------------------------------------------------------

def root_text(role: str, developer: str | None) -> str:
    who = "оператор" if role == OPERATOR else "владелец"
    title = developer or "FixBot"
    return (f"⚙️ <b>{title} — управление</b>\n"
            f"<i>Вы вошли как {who}.</i>\n\n"
            "Выберите раздел.")


def chats_text(chats: list[tuple[int, str | None]]) -> str:
    if not chats:
        return ("💬 <b>Группы</b>\n\n"
                "Пока ни одна группа не подключена.\n\n"
                "<b>Как подключить:</b>\n"
                "1. Создайте группу с агентством\n"
                "2. Добавьте туда бота\n"
                "3. Сделайте бота администратором\n"
                "4. Бот сам предложит закрепить группу за агентством\n\n"
                "После этого агентам не придётся указывать агентство "
                "в каждом сообщении.")

    lines = ["💬 <b>Подключённые группы</b>", ""]
    for chat_id, agency in chats:
        lines.append(f"• {agency or '— агентство не задано'}")
    lines += ["", f"Всего: {len(chats)}", "",
              "<i>Чтобы подключить новую — добавьте бота в группу "
              "и сделайте администратором.</i>"]
    return "\n".join(lines)


def agencies_text(rows: list) -> str:
    if not rows:
        return ("🏢 <b>Агентства</b>\n\n"
                "Справочник пуст. Агентства появляются сами: при первой "
                "фиксации бот спрашивает агентство и запоминает его.")
    lines = ["🏢 <b>Агентства</b>", ""]
    for r in rows[:30]:
        lines.append(f"• {r['name']}")
    lines += ["", f"Всего: {len(rows)}"]
    return "\n".join(lines)


def _bar(n: int, total: int, width: int = 12) -> str:
    if not total:
        return ""
    filled = max(1, round(width * n / total)) if n else 0
    return "▉" * filled


def stats_text(*, days: int, total: int, period: int,
               by_agency: list[tuple[str, int]],
               by_agent: list[tuple[str, int]],
               agents: dict, rejected: list[tuple[str, int]]) -> str:
    label = {7: "за 7 дней", 30: "за 30 дней"}.get(days, "за всё время")
    lines = [f"📊 <b>Статистика {label}</b>", "",
             f"Фиксаций: <b>{period}</b>"]
    if days:
        lines.append(f"Всего за всё время: {total}")

    if by_agency:
        top = max(c for _, c in by_agency)
        lines += ["", "<b>По агентствам</b>"]
        for name, c in by_agency[:10]:
            lines.append(f"{c:>4}  {_bar(c, top)}  {name}")

    if by_agent:
        top = max(c for _, c in by_agent)
        lines += ["", "<b>Активность агентов</b>"]
        for name, c in by_agent[:8]:
            lines.append(f"{c:>4}  {_bar(c, top)}  {name}")

    lines += ["", "<b>Агенты</b>",
              f"Всего в базе: {agents['total']}",
              f"Подписаны на уведомления: {agents['subscribed']}",
              f"Оставили телефон: {agents['with_phone']}"]

    if rejected:
        names = {
            "retail_blocked": "клиент отдела продаж",
            "booked": "уже на брони",
            "unknown": "нужна проверка",
            "same_agent": "повтор от того же агента",
            "same_agency": "повтор от агентства",
        }
        lines += ["", "<b>Отклонено</b>"]
        for verdict, c in rejected[:6]:
            lines.append(f"{c:>4}  {names.get(verdict, verdict)}")

    return "\n".join(lines)


def staff_text(rows: list, owner_ids: set[int]) -> str:
    lines = ["👥 <b>Сотрудники</b>", "",
             "Кто ещё может пользоваться этим меню: делать рассылки, "
             "смотреть статистику, подключать группы.", ""]
    if owner_ids:
        lines.append(f"Основной доступ: {len(owner_ids)} чел.")
    if rows:
        lines.append("")
        for r in rows:
            handle = f" (@{r['username']})" if r["username"] else ""
            lines.append(f"• {r['name'] or r['telegram_id']}{handle}")
    else:
        lines += ["", "<i>Добавленных сотрудников пока нет.</i>"]
    lines += ["", "<i>Чтобы добавить — перешлите сюда любое сообщение "
                  "этого человека.</i>"]
    return "\n".join(lines)


HELP_TEXT = """❓ <b>Как это работает</b>

<b>Что делает бот.</b> Сидит в рабочих чатах с агентствами, замечает
сообщения о фиксации клиентов и заводит их в CRM. Перед записью
показывает карточку и ждёт подтверждения кнопкой — сам ничего не пишет.

<b>Проверка клиента.</b> Бот сравнивает номер с базой и отвечает:
• 🟢 клиент новый — фиксируем;
• 🟢 клиента уже фиксировало другое агентство — работать можно, кто
  первым доведёт до депозита, того и клиент;
• 🔴 клиент уже у отдела продаж — фиксация не проходит.

Номер можно присылать с закрытыми последними двумя цифрами.

<b>Уведомления агентам.</b> Тем, кто нажал «Отслеживать статус», бот
пишет в личку: срок фиксации заканчивается, появился конкурент, клиент
постучался в отдел продаж, сделка состоялась.

<b>Рассылки.</b> Раздел «Рассылка»: присылаете сообщение — бот показывает,
как его увидят агенты, и отправляет. Англоязычным переводит сам.

<b>Подключить новую группу.</b> Добавьте бота в чат с агентством и
сделайте администратором — дальше он спросит, за каким агентством
закрепить чат."""
