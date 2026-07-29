"""
Пошаговый помощник подключения нового застройщика.

Оператор нажимает «Новый клиент» и получает одноразовую ссылку. Клиент
открывает её и попадает в диалог, где бот спрашивает по одной вещи за раз
и **сразу проверяет** каждый ответ: ключ бота — запросом к Telegram,
доступ к amoCRM — живым запросом к его аккаунту. Так человек узнаёт
об ошибке через две секунды, а не когда оператор попытается всё запустить.

Двух шагов не избежать, и это ограничения не наши:

  * бота у @BotFather клиент создаёт руками — API для создания ботов
    у Telegram нет;
  * токен amoCRM выпускает только администратор его аккаунта.

Сообщения с ключами бот удаляет из переписки сразу после проверки:
секретам незачем лежать в истории чата.
"""

from __future__ import annotations

import logging
import re
import secrets
import unicodedata

import httpx

log = logging.getLogger(__name__)

#: Порядок шагов. Сначала простое, потом то, что требует походов в другие
#: приложения — так человек успевает втянуться.
STEPS = ["developer", "bot_token", "privacy", "subdomain", "amo_token"]

TOTAL = len(STEPS)

TELEGRAM_API = "https://api.telegram.org"


def new_code() -> str:
    """Короткий, но неугадываемый код приглашения."""
    return secrets.token_urlsafe(6).replace("-", "").replace("_", "")[:8]


def slugify(name: str, taken: set[str] | None = None) -> str:
    """
    Имя папки клиента из названия компании.

    Только латиница, цифры и дефис: имя попадёт в путь на сервере и
    в название службы systemd.
    """
    taken = taken or set()
    translit = {
        "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "e",
        "ж": "zh", "з": "z", "и": "i", "й": "y", "к": "k", "л": "l", "м": "m",
        "н": "n", "о": "o", "п": "p", "р": "r", "с": "s", "т": "t", "у": "u",
        "ф": "f", "х": "h", "ц": "c", "ч": "ch", "ш": "sh", "щ": "sch",
        "ъ": "", "ы": "y", "ь": "", "э": "e", "ю": "yu", "я": "ya",
    }
    s = unicodedata.normalize("NFKC", (name or "").strip().lower())
    s = "".join(translit.get(ch, ch) for ch in s)
    s = re.sub(r"[^a-z0-9]+", "-", s).strip("-")
    s = s[:24] or "client"

    if s not in taken:
        return s
    for i in range(2, 100):
        candidate = f"{s}-{i}"
        if candidate not in taken:
            return candidate
    return f"{s}-{secrets.token_hex(2)}"


# --------------------------------------------------------------------------
# Проверки
# --------------------------------------------------------------------------

async def check_bot_token(token: str) -> tuple[bool, str]:
    """Спрашиваем у Telegram, живой ли ключ. Возвращаем (ок, описание)."""
    token = (token or "").strip()
    if not re.fullmatch(r"\d{6,}:[\w-]{30,}", token):
        return False, "не похоже на ключ бота"
    try:
        async with httpx.AsyncClient(timeout=15) as c:
            r = await c.get(f"{TELEGRAM_API}/bot{token}/getMe")
        data = r.json()
        if not data.get("ok"):
            return False, data.get("description", "Telegram отклонил ключ")
        me = data["result"]
        return True, f"@{me.get('username')} ({me.get('first_name')})"
    except Exception as e:  # noqa: BLE001
        return False, f"не удалось проверить: {str(e)[:80]}"


async def check_amo(subdomain: str, token: str) -> tuple[bool, str]:
    """
    Проверяем доступ к amoCRM и заодно показываем, что бот там увидит.

    Если токен не подошёл, клиент узнает об этом сразу — а не после того,
    как оператор развернёт нерабочего клиента.
    """
    subdomain = (subdomain or "").strip().lower()
    if not re.fullmatch(r"[a-z0-9-]{2,40}", subdomain):
        return False, "поддомен выглядит странно"

    base = f"https://{subdomain}.amocrm.ru/api/v4"
    headers = {"Authorization": f"Bearer {(token or '').strip()}"}
    try:
        async with httpx.AsyncClient(timeout=25, headers=headers) as c:
            acc = await c.get(f"{base}/account")
            if acc.status_code == 401:
                return False, "токен не принят (401) — возможно скопирован не целиком"
            if acc.status_code == 403:
                return False, "у интеграции не хватает прав (403)"
            if acc.status_code >= 400:
                return False, f"amoCRM ответила {acc.status_code}"

            name = (acc.json() or {}).get("name") or subdomain

            pipes = await c.get(f"{base}/leads/pipelines")
            n_pipes = len(
                ((pipes.json() or {}).get("_embedded") or {}).get("pipelines")
                or []) if pipes.status_code < 400 else 0

            cont = await c.get(f"{base}/contacts", params={"limit": 1})
            total = "?"
            if cont.status_code < 400:
                page = (cont.json() or {})
                total = str(page.get("_total_items") or "есть")
            elif cont.status_code == 204:
                total = "0"
    except Exception as e:  # noqa: BLE001
        return False, f"не удалось связаться: {str(e)[:80]}"

    return True, f"{name}: воронок {n_pipes}, контактов {total}"


# --------------------------------------------------------------------------
# Тексты шагов
# --------------------------------------------------------------------------

def _head(step: str) -> str:
    return f"<b>Шаг {STEPS.index(step) + 1} из {TOTAL}</b>"


WELCOME = """👋 <b>Подключение бота фиксаций</b>

Помогу настроить бота для вашего застройщика. Займёт минут десять.

Я буду спрашивать по одному пункту и сразу проверять ответ, так что
ошибиться сложно. Если что-то не получится — просто напишите об этом.

Начнём."""

ASK = {
    "developer": (
        "{head}\n\n<b>Название компании</b>\n\n"
        "Как называется ваш застройщик? Это увидят агенты в чатах.\n\n"
        "Например: <code>Ромашка</code>"
    ),
    "bot_token": (
        "{head}\n\n<b>Создайте бота</b>\n\n"
        "1. Откройте <b>@BotFather</b>, нажмите Start\n"
        "2. Отправьте <code>/newbot</code>\n"
        "3. Придумайте имя — его увидят агенты\n"
        "4. Придумайте адрес, он должен заканчиваться на <b>bot</b>\n\n"
        "В ответ придёт длинная строка вида\n"
        "<code>7123456789:AAF-xxxxxxxxxxxxx</code>\n\n"
        "Пришлите её сюда."
    ),
    "privacy": (
        "{head}\n\n<b>Разрешите боту видеть сообщения</b>\n\n"
        "Без этого он не заметит фиксации в группах — это самая частая "
        "причина, по которой «ничего не работает».\n\n"
        "В <b>@BotFather</b>:\n"
        "<code>/mybots</code> → ваш бот → <b>Bot Settings</b> → "
        "<b>Group Privacy</b> → <b>Turn off</b>\n\n"
        "Должно появиться <i>Privacy mode is disabled</i>."
    ),
    "subdomain": (
        "{head}\n\n<b>Адрес вашей amoCRM</b>\n\n"
        "Посмотрите в адресной строке браузера. Если вы заходите на\n"
        "<code>https://romashka.amocrm.ru</code> — пришлите "
        "<code>romashka</code>"
    ),
    "amo_token": (
        "{head}\n\n<b>Доступ к amoCRM</b>\n\n"
        "Это делает администратор аккаунта.\n\n"
        "1. <b>Настройки</b> → <b>Интеграции</b>\n"
        "2. <b>Создать интеграцию</b> → <b>Внешняя интеграция</b>\n"
        "3. Название любое, ссылку для перенаправления можно указать "
        "<code>https://example.com</code>\n"
        "4. В правах отметьте доступ к <b>сделкам, контактам и компаниям</b>\n"
        "5. Сохраните, откройте вкладку <b>Ключи и доступы</b>\n"
        "6. Выпустите <b>долгосрочный токен</b>, срок побольше\n\n"
        "Пришлите токен сюда — он длинный, начинается на <code>eyJ</code>. "
        "Скопируйте целиком.\n\n"
        "<i>Сообщение с токеном я удалю сразу после проверки.</i>"
    ),
}


def ask_text(step: str) -> str:
    return ASK[step].format(head=_head(step))


def ok_text(step: str, detail: str) -> str:
    return {
        "developer": f"Принято: <b>{detail}</b>",
        "bot_token": f"✅ Бот найден: <b>{detail}</b>",
        "privacy": "✅ Хорошо",
        "subdomain": f"Принято: <b>{detail}</b>",
        "amo_token": f"✅ Доступ есть — {detail}",
    }.get(step, "✅ Принято")


FINISH = """🎉 <b>Всё собрано</b>

Передал заявку оператору. Он развернёт бота и напишет вам, когда всё
будет готово — обычно это несколько минут.

Пока можно подготовиться: создайте в Telegram группы с агентствами,
с которыми работаете. Добавлять туда бота будем после запуска."""

DONE_FOR_CLIENT = """✅ <b>Ваш бот запущен</b>

Напишите ему: {bot}

Там команда <code>/admin</code> откроет меню: рассылки, группы,
агентства, статистика.

<b>Первое, что стоит сделать</b> — подключить группу с агентством:
добавьте бота в чат и сделайте администратором, дальше он подскажет сам."""


def summary_for_operator(onb: dict) -> str:
    d = onb["data"]
    handle = f" (@{onb['username']})" if onb.get("username") else ""
    return "\n".join([
        "🆕 <b>Новый клиент готов к подключению</b>", "",
        f"Застройщик: <b>{d.get('developer', '—')}</b>",
        f"amoCRM: {d.get('subdomain', '—')}",
        f"<i>{d.get('amo_check', '')}</i>",
        f"Бот: {d.get('bot_check', '—')}",
        f"Владелец: {onb.get('user_name') or '—'}{handle}",
        f"Папка: <code>{onb.get('slug') or '—'}</code>",
    ])


INVITE_TEXT = """➕ <b>Новый клиент</b>

Отправьте эту ссылку тому, кто будет подключать бота:

{link}

Ссылка одноразовая и действует сутки. Клиент откроет её, и я проведу
его по шагам: создать бота, выдать доступ к amoCRM. Каждый ответ
проверю сразу.

Когда всё соберётся — пришлю вам заявку с кнопкой «Развернуть»."""


INVALID_INVITE = {
    "not_found": "Ссылка не найдена. Попросите оператора выдать новую.",
    "used": "Эта ссылка уже использована. Попросите новую.",
    "expired": "Срок ссылки истёк. Попросите оператора выдать новую.",
}
