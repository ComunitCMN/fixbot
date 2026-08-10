"""
FixBot — фиксация клиентов из Telegram в amoCRM.

Порядок обработки сообщения из группы:

    предфильтр → Claude решает, фиксация ли это
                 ↓
    номер клиента (может быть маскированным)
                 ↓
    агентство: из сообщения → из привязки чата → из профиля агента
                 ↓
    префиксный поиск совпадений + происхождение каждого
                 ↓
    verdict.decide() — один из семи вердиктов
                 ↓
    ответ в чат, при необходимости — контакт и сделка в amoCRM
"""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
import os
import time
from pathlib import Path

from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import (CallbackQuery, ChatMemberUpdated, ForceReply,
                           InlineKeyboardButton, InlineKeyboardMarkup,
                           KeyboardButton, Message, ReplyKeyboardMarkup,
                           ReplyKeyboardRemove)

import httpx
import phonenumbers as pn

import agencies as ag
import billing as bl
import billing_run as blrun
import billing_ui as blui
import broadcast as bc
import clients as cl
import menu as mn
import onboarding as onb
import provision as pv
import i18n
import phones
import texts
import verdict as vd
from amo import AmoClient, compute_origins
from amoauth import build_auth
from config import cfg
from db import Db, Match
import llm
from llm import Classifier

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("fixbot")

bot = Bot(cfg.telegram_token,
          default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()
db = Db(cfg.db_path)
db.upsert_account(cfg.amo_subdomain, cfg.amo_auth,
                  access_token=cfg.amo_token or None)
amo = AmoClient(build_auth(cfg, db))
clf = Classifier(cfg.anthropic_key, cfg.model, cfg.min_confidence)


# ==========================================================================
# Вспомогательное
# ==========================================================================

def author_name(m: Message) -> str:
    u = m.from_user
    if not u:
        return "неизвестно"
    name = " ".join(filter(None, [u.first_name, u.last_name])) or "без имени"
    return f"{name} (@{u.username})" if u.username else name


def is_admin(m: Message) -> bool:
    """Технические команды — только оператору."""
    return (not cfg.operator_ids
            or (m.from_user is not None
                and m.from_user.id in cfg.operator_ids))


def chat_region(chat_id: int | None) -> str:
    """
    Страна, по которой читать номера без международного кода.

    Фиксации приходят из разных стран, и «0812…» в чате про Бали — это
    индонезийский номер, а «8 999…» в чате про Москву — российский.
    Номера с «+» разбираются сами, привязка на них не влияет.
    """
    if chat_id is not None:
        bound = db.get_meta(f"chat_region:{chat_id}")
        if bound:
            return bound
    return cfg.default_region


def parse_phone(raw: str | None, chat_id: int | None) -> phones.Phone | None:
    return phones.normalize(raw, chat_region(chat_id)) if raw else None


def service_paused() -> bool:
    """
    Приостановлено ли обслуживание.

    Метку ставит бот оператора — файлом `PAUSED` рядом с базой клиента.
    Не записью в базе: базу пишет этот процесс, и второй писатель ей
    не нужен. Файл читается на каждую фиксацию, но это одна проверка
    существования — дешевле, чем запрос к диску за строкой.
    """
    from pathlib import Path
    try:
        return (Path(cfg.db_path).resolve().parent / "PAUSED").exists()
    except OSError:
        return False


def chat_lang(chat_id: int | None, text: str = "") -> str:
    """
    Язык ответа — язык самого сообщения.

    В смешанной группе русскому агенту отвечаем по-русски, англоязычному
    по-английски, даже если группа помечена русской. Отвечаем-то мы
    человеку, а не группе.

    Привязка чата — запас на случай, когда судить не по чему: сообщение
    из одних цифр, «+7 999 123-45-67». Раньше привязка стояла первой
    и перебивала всё, из-за чего англоязычный агент в русской группе
    получал ответы по-русски.
    """
    if i18n.has_letters(text):
        return i18n.detect(text, cfg.default_lang)
    if chat_id is not None:
        pinned = db.get_meta(f"chat_lang:{chat_id}")
        if pinned:
            return i18n.normalize_lang(pinned, cfg.default_lang)
    return cfg.default_lang


#: Последние сообщения каждого автора: (chat_id, user_id) → [(время, текст)].
#: Держим в памяти: контекст нужен на минуты, переживать перезапуск ему незачем.
_recent: dict[tuple[int, int], list[tuple[float, str]]] = {}

CONTEXT_WINDOW_SEC = 10 * 60
CONTEXT_DEPTH = 3


def remember_message(chat_id: int, user_id: int, text: str) -> None:
    key = (chat_id, user_id)
    now = time.time()
    buf = [(t, x) for t, x in _recent.get(key, [])
           if now - t < CONTEXT_WINDOW_SEC]
    buf.append((now, text[:500]))
    _recent[key] = buf[-CONTEXT_DEPTH:]

    # Чтобы словарь не рос бесконечно в больших чатах.
    if len(_recent) > 5000:
        for k, v in list(_recent.items()):
            if not v or now - v[-1][0] > CONTEXT_WINDOW_SEC:
                _recent.pop(k, None)


def recent_context(chat_id: int, user_id: int) -> list[str]:
    """
    Что этот же человек писал в этом чате за последние минуты.

    Фиксацию часто разрывают: «+7 987 564 34 88», следом «зафиксируйте».
    По отдельности ни одно сообщение фиксацией не выглядит, вместе —
    вполне.
    """
    now = time.time()
    return [x for t, x in _recent.get((chat_id, user_id), [])
            if now - t < CONTEXT_WINDOW_SEC]


def target_pipeline() -> tuple[int | None, int | None, str | None]:
    """
    Куда класть новую агентскую сделку: (pipeline_id, status_id, название).

    Приоритет у явной настройки AMO_PIPELINE_ID, иначе берётся воронка,
    помеченная как агентская в /pipelines. Если ни того, ни другого —
    фиксацию создавать нельзя: сделка уйдёт в розничную воронку и через
    несколько минут бот сам же признает этого клиента «клиентом отдела
    продаж».
    """
    if cfg.pipeline_id:
        return cfg.pipeline_id, cfg.status_id, None
    row = db.agency_pipeline()
    if not row:
        return None, None, None
    return (row["pipeline_id"],
            cfg.status_id or db.first_status(row["pipeline_id"]),
            row["name"])


async def find_all_matches(p: phones.Phone) -> tuple[list, bool]:
    """
    Совпадения из зеркала плюс живой запрос в amoCRM.

    Зеркало отвечает мгновенно и надёжно ищет по маскированным номерам.
    Живой запрос закрывает его слабое место — контакты, созданные после
    последней синхронизации. Результаты складываются и схлопываются по
    id контакта: живые данные свежее, поэтому перебивают зеркальные.

    Второе значение — удалось ли достучаться до amoCRM.
    """
    mirror = db.find_matches(p)
    if not cfg.live_lookup:
        return enrich_matches(mirror), False

    try:
        live = await amo.live_lookup(p.digits, db.pipeline_kinds(),
                                     db.booking_status_ids())
    except Exception:  # noqa: BLE001
        log.exception("живой поиск в amoCRM не удался, работаю по зеркалу")
        return enrich_matches(mirror), False

    by_contact = {m.contact_id: m for m in mirror
                  if m.source == "amo" and m.contact_id}
    for row in live:
        by_contact[row["contact_id"]] = Match(
            digits=row["digits"], name=row["name"], source="amo",
            contact_id=row["contact_id"], has_retail=row["has_retail"],
            last_retail_activity=row["last_retail_activity"],
            has_agency=row["has_agency"],
            agency_company_id=row["agency_company_id"],
            booked=row["booked"], origin_known=True,
            created_at=row["created_at"],
        )
        # Свежий контакт заодно кладём в зеркало, чтобы не искать его заново.
        db.upsert_from_live(row)

    merged = [m for m in mirror if m.source != "amo" or not m.contact_id]
    merged += list(by_contact.values())
    return enrich_matches(merged), True


def enrich_matches(matches: list) -> list:
    """
    Проставляет во внешние совпадения внутренний id агентства.

    verdict.decide() сравнивает агентства по Match.agency_id, а у совпадений
    из amoCRM известен только id компании — здесь одно превращается в другое.
    """
    for m in matches:
        if m.agency_id is None and m.agency_company_id:
            row = db.agency_by_company_id(m.agency_company_id)
            if row:
                m.agency_id = row["id"]
    return matches


async def resolve_agency(raw_name: str | None, telegram_id: int | None,
                         chat_id: int | None) -> tuple[int | None, str | None, ag.Resolution | None]:
    """
    Определяет агентство: из сообщения → из профиля агента → из привязки чата.

    Возвращает (agency_id, отображаемое имя, резолюция для уточнения).
    Резолюция не None только тогда, когда нужно переспросить у человека.
    """
    # 1. Явно указано в сообщении
    if raw_name:
        known = [
            {"name": a["name"], "norm": a["norm_name"], "agency_id": a["id"],
             "amo_company_id": a["amo_company_id"]}
            for a in db.list_agencies()
        ] + [
            {"name": c["name"], "norm": c["norm_name"],
             "amo_company_id": c["company_id"]}
            for c in db.list_companies()
        ]
        res = ag.resolve(raw_name, known)
        if res.status == "exact" and res.best:
            if res.best.agency_id:
                db.add_agency_alias(res.best.agency_id, res.norm)
                return res.best.agency_id, res.best.name, None
            aid = db.create_agency(ag.pretty_name(res.best.name), res.best.norm,
                                   res.best.amo_company_id)
            db.add_agency_alias(aid, res.norm)
            return aid, res.best.name, None
        return None, None, res

    # 2. Чат закреплён за агентством. Важнее профиля: закрепление
    # делает администратор осознанно, а профиль мог остаться от прошлого
    # места работы агента.
    if chat_id:
        bound = db.get_meta(f"chat_agency:{chat_id}")
        if bound and bound.isdigit():
            row = db.get_agency(int(bound))
            if row:
                return row["id"], row["name"], None

    # 3. Профиль агента
    if telegram_id:
        agent = db.get_agent(telegram_id)
        if agent and agent["agency_id"]:
            row = db.get_agency(agent["agency_id"])
            if row:
                return row["id"], row["name"], None

    return None, None, None


async def notify_admins(text: str) -> None:
    for admin_id in cfg.admin_ids:
        try:
            await bot.send_message(admin_id, text, disable_web_page_preview=True)
        except Exception as e:  # noqa: BLE001
            log.warning("Не смог уведомить админа %s: %s", admin_id, e)


# ==========================================================================
# Синхронизация
# ==========================================================================

async def sync_all() -> dict:
    log.info("Синхронизация amoCRM…")

    pipelines = await amo.pipelines()
    db.replace_pipelines(pipelines)

    companies = await amo.dump_companies()
    db.replace_companies(companies)

    contacts = await amo.dump_contacts()
    n_phones = db.replace_contacts(contacts)

    leads = await amo.dump_leads()
    origins = compute_origins(leads, db.pipeline_kinds(), db.booking_status_ids())
    db.replace_origins(origins)

    stats = db.stats()
    log.info("Готово: %s телефонов, %s сделок, %s контактов с происхождением",
             n_phones, len(leads), len(origins))
    return stats


async def sync_loop() -> None:
    while True:
        await asyncio.sleep(cfg.sync_interval_min * 60)
        try:
            await sync_all()
        except Exception:  # noqa: BLE001
            log.exception("Ошибка синхронизации")


# ==========================================================================
# Команды
# ==========================================================================

@dp.message(Command("ping"))
async def cmd_ping(m: Message) -> None:
    s = db.stats()
    synced = db.get_meta("contacts_synced_at")
    configured = "да" if db.is_configured() else "❗️ НЕТ, нужен /pipelines"
    _, _, pname = target_pipeline()
    target = (f"«{texts.esc(pname)}»" if pname
              else ("задана в .env" if cfg.pipeline_id
                    else "❗️ НЕ ЗАДАНА, фиксации не сохранятся"))
    mode = "👀 наблюдение, в CRM не пишет" if cfg.dry_run else "боевой"
    await m.reply(
        "✅ <b>Бот работает</b>\n"
        f"Режим: {mode}\n"
        f"Фиксации кладёт в: {target}\n"
        f"Воронки размечены: {configured}\n"
        f"Телефонов в зеркале: <b>{s['phones']}</b>\n"
        f"Контактов с происхождением: <b>{s['origins']}</b>\n"
        f"Агентств в справочнике: <b>{s['agencies']}</b>\n"
        f"Фиксаций через бота: <b>{s['fixations']}</b>\n"
        f"Синхронизация: {texts.when(int(synced)) if synced else 'ещё не было'}"
    )


@dp.message(Command("pipelines"))
async def cmd_pipelines(m: Message) -> None:
    """Разметка воронок: какая розничная, какая агентская."""
    if not is_admin(m):
        return
    try:
        db.replace_pipelines(await amo.pipelines())
    except Exception as e:  # noqa: BLE001
        await m.reply(f"Не смог получить воронки: {texts.esc(str(e))[:300]}")
        return
    await m.reply(_pipelines_text(), reply_markup=_pipelines_kb())


LABELS = {"retail": "🏢 розничная", "agency": "🤝 агентская",
          "ignore": "🚫 не учитывать", "unset": "⬜️ не размечена"}


def _pipelines_text() -> str:
    rows = db.list_pipelines()
    lines = ["<b>Разметка воронок amoCRM</b>", ""]
    for r in rows:
        lines.append(f"{LABELS[r['kind']]} — {texts.esc(r['name'])}")
    lines += [
        "",
        "<b>Розничная</b> — клиенты отдела продаж. Совпадение с такой "
        "воронкой блокирует агентскую фиксацию.",
        "<b>Агентская</b> — фиксации от агентств. Совпадение не блокирует, "
        "только предупреждает.",
        "",
        "Нажмите на воронку, чтобы поменять её тип.",
    ]
    return "\n".join(lines)


def _pipelines_kb() -> InlineKeyboardMarkup:
    buttons = []
    for r in db.list_pipelines():
        mark = {"retail": "🏢", "agency": "🤝", "ignore": "🚫", "unset": "⬜️"}[r["kind"]]
        buttons.append([InlineKeyboardButton(
            text=f"{mark} {r['name']}",
            callback_data=f"pl:{r['pipeline_id']}",
        )])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


@dp.callback_query(F.data.startswith("pl:"))
async def cb_pipeline(c: CallbackQuery) -> None:
    pid = int(c.data.split(":")[1])
    cycle = {"unset": "retail", "retail": "agency", "agency": "ignore",
             "ignore": "unset"}
    current = db.pipeline_kinds().get(pid, "unset")
    db.set_pipeline_kind(pid, cycle[current])
    try:
        await c.message.edit_text(_pipelines_text(), reply_markup=_pipelines_kb())
    except Exception:  # noqa: BLE001
        pass
    await c.answer(f"Теперь: {LABELS[cycle[current]]}")


@dp.message(Command("sync"))
async def cmd_sync(m: Message) -> None:
    if not is_admin(m):
        return
    msg = await m.reply("Синхронизирую amoCRM, это может занять пару минут…")
    try:
        s = await sync_all()
        await msg.edit_text(
            f"Готово.\nТелефонов: {s['phones']}\n"
            f"Контактов с происхождением: {s['origins']}\n"
            f"Компаний: {s['companies']}"
        )
    except Exception as e:  # noqa: BLE001
        log.exception("sync")
        await msg.edit_text(f"Ошибка: {texts.esc(str(e))[:300]}")


@dp.message(Command("bind"))
async def cmd_bind(m: Message) -> None:
    """Привязать текущий чат к агентству: /bind Дом+"""
    if not is_admin(m):
        return
    parts = (m.text or "").split(maxsplit=1)
    if len(parts) < 2:
        await m.reply("Формат: <code>/bind Название агентства</code>")
        return
    aid, name, res = await resolve_agency(parts[1], None, None)
    if aid is None:
        aid = db.create_agency(ag.pretty_name(parts[1]),
                               ag.norm_agency(parts[1]))
        name = ag.pretty_name(parts[1])
    db.set_meta(f"chat_agency:{m.chat.id}", str(aid))
    await m.reply(f"Чат привязан к агентству <b>{texts.esc(name)}</b>.\n"
                  "Теперь фиксации отсюда автоматически подписываются им.")


@dp.message(Command("region"))
async def cmd_region(m: Message) -> None:
    """Привязать чат к стране: /region ID — для чата про Бали."""
    if not is_admin(m):
        return
    parts = (m.text or "").split(maxsplit=1)
    current = chat_region(m.chat.id)
    if len(parts) < 2:
        await m.reply(
            f"Страна этого чата: <b>{current}</b>\n\n"
            "Влияет только на номера, записанные без международного кода: "
            "<code>8 999…</code> прочтётся как российский, "
            "<code>0812…</code> — как индонезийский. "
            "Номера с «+» разбираются сами.\n\n"
            "Сменить: <code>/region ID</code> — двухбуквенный код страны.\n"
            "RU Россия · ID Индонезия · AE ОАЭ · TR Турция · "
            "TH Таиланд · KZ Казахстан · GE Грузия")
        return

    code = parts[1].strip().upper()[:2]
    probe = phones.normalize("+1 555 000 0000", code)
    if len(code) != 2 or not code.isalpha() or probe is None:
        await m.reply("Не похоже на код страны. Нужны две буквы: RU, ID, AE…")
        return

    db.set_meta(f"chat_region:{m.chat.id}", code)
    sample = pn.example_number_for_type(code, pn.PhoneNumberType.MOBILE)
    hint = ""
    if sample:
        national = str(sample.national_number)
        trunk = phones._trunk_prefix(code)
        local = phones.normalize(trunk + national, code)
        if local:
            hint = (f"\nТеперь <code>{trunk + national}</code> здесь читается "
                    f"как <code>{local.pretty()}</code>.")
    await m.reply(f"Страна чата: <b>{code}</b>.{hint}")


@dp.message(Command("lang"))
async def cmd_lang(m: Message) -> None:
    """Прибить язык чата: /lang en. Без аргумента — снять привязку."""
    if not is_admin(m):
        return
    parts = (m.text or "").split(maxsplit=1)
    if len(parts) < 2:
        db.set_meta(f"chat_lang:{m.chat.id}", "")
        await m.reply(
            "Язык чата: <b>по языку сообщения</b>.\n"
            "Пишут по-русски — отвечаю по-русски, по-английски — по-английски.\n\n"
            "Прибить жёстко: <code>/lang ru</code> или <code>/lang en</code>.")
        return

    code = i18n.normalize_lang(parts[1], "")
    if not code:
        await m.reply("Поддерживаются только <code>ru</code> и <code>en</code>.")
        return
    db.set_meta(f"chat_lang:{m.chat.id}", code)
    await m.reply(f"Язык чата: <b>{code}</b>. "
                  f"Снять: <code>/lang</code> без аргумента.")


@dp.message(Command("check"))
async def cmd_check(m: Message) -> None:
    """Проверить номер, ничего не создавая: /check +7 999 123-45-**"""
    parts = (m.text or "").split(maxsplit=1)
    if len(parts) < 2:
        await m.reply("Формат: <code>/check +7 999 123-45-**</code>")
        return
    lang = chat_lang(m.chat.id, m.text or "")
    p = phones.normalize(parts[1], chat_region(m.chat.id))
    if p is None:
        await m.reply(texts.need_phone(lang))
        return
    if not p.is_usable:
        await m.reply(texts.need_digits(p, parts[1], lang))
        return

    agency_id, agency_name, _ = await resolve_agency(
        None, m.from_user.id if m.from_user else None, m.chat.id
    )
    matches, live_ok = await find_all_matches(p)
    d = vd.decide(matches, agency_id,
                  m.from_user.id if m.from_user else None,
                  retail_ttl_days=cfg.retail_ttl_days)

    note = texts.t(lang, "check_only")
    if cfg.live_lookup and not live_ok:
        note += texts.t(lang, "live_failed")
    await m.reply(
        texts.render(d, client="Проверка", p=p, agency=agency_name,
                     ttl_days=cfg.retail_ttl_days, lang=lang) + note,
        disable_web_page_preview=True,
    )


# ==========================================================================
# Основной обработчик
# ==========================================================================

@dp.message(F.chat.type.in_({"group", "supergroup"}), F.text | F.caption)
async def on_message(m: Message) -> None:
    if cfg.allowed_chats and m.chat.id not in cfg.allowed_chats:
        return
    text = m.text or m.caption or ""
    if text.startswith("/"):
        return

    # Ответ на вопрос бота об агентстве — это не новая фиксация.
    if await try_agency_reply(m):
        return

    # Запоминаем чат: список групп у Telegram не спросить, а настраивать
    # язык и агентство надо на чём-то.
    if db.see_chat(m.chat.id, m.chat.title):
        _announce_chat(m.chat.id, m.chat.title)

    author = author_name(m)
    lang = chat_lang(m.chat.id, text)
    uid = m.from_user.id if m.from_user else 0
    context = recent_context(m.chat.id, uid)
    remember_message(m.chat.id, uid, text)

    fx = await clf.classify(text, chat_title=m.chat.title or "",
                            author=author, context=context)
    if not fx.is_fixation:
        log.debug("Пропуск (%s): %s", fx.reason, text[:80])
        if not cfg.quiet:
            await m.reply(f"<i>не фиксация: {texts.esc(fx.reason)}</i>")
        return

    # Приостановка ставится оператором снаружи, файлом в папке клиента.
    # Проверяем здесь, а не раньше: пока сообщение не признано фиксацией,
    # отвечать вообще незачем — иначе бот заговорит на каждую реплику.
    if service_paused():
        await m.reply(texts.service_paused(lang))
        return

    if not db.is_configured():
        await m.reply(texts.not_configured(lang))
        return

    if m.from_user is None:
        await m.reply(texts.anonymous_sender(lang))
        return

    log.info("Фиксация от %s в «%s»: %s", author, m.chat.title, fx.client_name)
    db.upsert_agent(m.from_user.id, m.from_user.username, author)
    # Язык личных уведомлений — язык, на котором пишет сам человек,
    # а не язык группы: в русском чате может работать англоязычный агент.
    # Короткие реплики не в счёт, иначе одно «ok» переключило бы человека
    # на английский навсегда.
    if i18n.confident(text):
        db.set_agent_field(m.from_user.id, lang=i18n.detect(text, lang))

    # ---------- телефон ----------
    region = chat_region(m.chat.id)
    p = phones.normalize(fx.phone or "", region)
    if p is None:
        # В самом сообщении номера может не быть — он мог прийти
        # предыдущим. Ищем сначала здесь, потом в контексте.
        found = phones.extract_all(text, region)
        for older in reversed(context):
            if found:
                break
            found = phones.extract_all(older, region)
        p = found[0] if found else None
    if p is None:
        await m.reply(texts.need_phone(lang))
        return
    if not p.is_usable:
        await m.reply(texts.need_digits(p, fx.phone, lang))
        return

    # ---------- агентство ----------
    agency_id, agency_name, res = await resolve_agency(
        fx.agency, m.from_user.id, m.chat.id
    )

    payload = {
        "client": fx.client_name, "digits": p.digits,
        "agency_id": agency_id, "agency_name": agency_name,
        "object": fx.object, "comment": fx.comment,
        "raw_text": text, "chat_title": m.chat.title,
        "author": author, "username": m.from_user.username, "lang": lang,
    }

    # Агентство неизвестно — спрашиваем кнопками, а не текстом.
    if agency_id is None:
        payload["awaiting"] = "agency"
        pending_id = db.create_pending(m.chat.id, m.message_id,
                                       m.from_user.id, payload)
        payload["lang"] = lang
        cands = [c.name for c in res.candidates] if res else None
        sent = await m.reply(
            texts.need_agency(bool(db.list_agencies()),
                              res.query if res else None, cands, lang),
            reply_markup=_agency_pick_kb(pending_id, res, lang),
        )
        db.set_pending_prompt(pending_id, sent.message_id)
        return

    # ---------- вердикт ----------
    matches, live_ok = await find_all_matches(p)
    d = vd.decide(matches, agency_id, m.from_user.id,
                  retail_ttl_days=cfg.retail_ttl_days)
    log.info("Вердикт: %s (совпадений: %d, живой поиск: %s)",
             d.verdict.value, len(matches), "ок" if live_ok else "нет")

    client = fx.client_name

    # ---------- блокирующие вердикты: отвечаем сразу ----------
    # Спрашивать «хотите зафиксировать?» бессмысленно, если фиксация
    # всё равно невозможна. Экономим человеку лишний шаг.
    if not d.creates_fixation:
        db.log_fixation(
            digits=p.digits, client_name=client, agency_id=agency_id,
            agent_telegram_id=m.from_user.id, agent_name=author,
            chat_id=m.chat.id, chat_title=m.chat.title,
            message_id=m.message_id, raw_text=text, verdict=d.verdict.value,
        )
        await m.reply(
            texts.render(d, client=client or "Клиент", p=p, agency=agency_name,
                         ttl_days=cfg.retail_ttl_days, lang=lang),
            disable_web_page_preview=True,
        )
        if d.notifies_admin:
            urls = [amo.contact_url(x.contact_id)
                    for x in d.reasons if x.contact_id]
            await notify_admins(texts.admin_unknown_origin(
                client or "—", p, agency_name, m.chat.title, urls))
        if not cfg.dry_run:
            await _note_attempt(d, client or "—", p, agency_name, m, author, text)
        return

    # ---------- всё остальное: просим подтвердить ----------
    payload = {
        "client": client, "digits": p.digits,
        "agency_id": agency_id, "agency_name": agency_name,
        "object": fx.object, "comment": fx.comment,
        "raw_text": text, "chat_title": m.chat.title,
        "author": author, "username": m.from_user.username,
        "verdict": d.verdict.value,
        # Язык кладём в заявку, иначе карточка придёт на английском,
        # а подтверждение после нажатия кнопки — на русском.
        "lang": lang,
    }
    pending_id = db.create_pending(m.chat.id, m.message_id,
                                   m.from_user.id, payload)

    note = {
        vd.Verdict.UNIQUE: texts.confirm_note_unique(lang),
        vd.Verdict.OTHER_AGENCY: texts.confirm_note_other_agency(d, lang),
        vd.Verdict.RETAIL_EXPIRED: texts.confirm_note_retail_expired(
            d, cfg.retail_ttl_days, lang),
    }.get(d.verdict, "")

    card = texts.confirm_card(client=client, p=p, agency=agency_name,
                              agent=author, object_=fx.object,
                              verdict_note=note, lang=lang)
    if cfg.dry_run:
        card += texts.dry_run_note(lang)

    sent = await m.reply(card,
                         reply_markup=_confirm_kb(pending_id, bool(client), lang),
                         disable_web_page_preview=True)
    db.set_pending_prompt(pending_id, sent.message_id)


def _confirm_kb(pending_id: int, has_name: bool = True,
                lang: str = i18n.RU) -> InlineKeyboardMarkup:
    name_label = texts.t(lang, "btn_edit_name" if has_name else "btn_add_name")
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=texts.t(lang, "btn_fix"),
                              callback_data=f"fix:{pending_id}")],
        [InlineKeyboardButton(text=name_label,
                              callback_data=f"pname:{pending_id}")],
        [InlineKeyboardButton(text=texts.t(lang, "btn_other_agency"),
                              callback_data=f"pag:{pending_id}")],
        [InlineKeyboardButton(text=texts.t(lang, "btn_cancel"),
                              callback_data=f"nofix:{pending_id}")],
    ])


async def _create_in_amo(payload: dict, p: phones.Phone,
                         agent_telegram_id: int) -> dict:
    """
    Создаёт в amoCRM всё, что нужно для фиксации.

    Порядок: компания-агентство → карточка агента → контакт клиента →
    сделка, связанная с клиентом, агентом и агентством.
    """
    agency_id = payload["agency_id"]
    agency_name = payload.get("agency_name")
    client = payload.get("client") or f"Клиент {p.pretty()}"

    # --- агентство ---
    agency_row = db.get_agency(agency_id) if agency_id else None
    company_id = agency_row["amo_company_id"] if agency_row else None
    if not company_id and agency_name:
        company_id = await amo.find_or_create_company(agency_name)
        db.create_agency(agency_row["name"] if agency_row else agency_name,
                         agency_row["norm_name"] if agency_row
                         else ag.norm_agency(agency_name),
                         company_id)

    # --- агент ---
    agent_contact_id = None
    agent_row = db.get_agent(agent_telegram_id)
    if agent_row and agent_row["amo_contact_id"]:
        agent_contact_id = agent_row["amo_contact_id"]
    else:
        try:
            agent_contact_id = await amo.find_or_create_agent(
                name=payload.get("author") or "Агент",
                company_id=company_id,
                username=payload.get("username"),
                phone=agent_row["phone"] if agent_row else None,
            )
            db.set_agent_amo_contact(agent_telegram_id, agent_contact_id)
        except Exception:  # noqa: BLE001
            # Карточка агента — приятное дополнение, но не повод
            # заваливать саму фиксацию.
            log.exception("не смог создать карточку агента")

    # Привязываем агента к его агентству отдельным запросом. Делаем это
    # каждый раз, а не только при создании: карточка могла появиться
    # раньше — до того, как стало известно агентство, или при другом.
    if agent_contact_id and company_id:
        try:
            await amo.link_entity("contacts", agent_contact_id,
                                  "companies", company_id)
        except Exception as e:  # noqa: BLE001
            # Повторная привязка к той же компании — не ошибка.
            log.info("привязка агента к компании: %s", str(e)[:200])

    # --- клиент ---
    phone_value = f"+{p.digits}" if p.is_full else p.pretty()
    contact_id = await amo.create_contact(
        name=client, phone=phone_value, phone_field_id=cfg.phone_field_id,
        company_id=company_id if cfg.link_agency_to_contact else None,
    )

    # --- сделка ---
    pipeline_id, status_id, _ = target_pipeline()
    lead_id = await amo.create_lead(
        name=f"{client} — {payload.get('object') or payload.get('chat_title') or 'Telegram'}",
        contact_id=contact_id, company_id=company_id,
        pipeline_id=pipeline_id, status_id=status_id,
        tags=["telegram-фиксация"], agent_contact_id=agent_contact_id,
    )
    await amo.add_note(
        "leads", lead_id,
        f"Зафиксирован из Telegram, подтверждено агентом.\n"
        f"Агентство: {agency_name or '—'}\n"
        f"Агент: {payload.get('author')}"
        f"{' @' + payload['username'] if payload.get('username') else ''}\n"
        f"Чат: {payload.get('chat_title')}\n"
        f"Объект: {payload.get('object') or '—'}\n"
        f"Комментарий: {payload.get('comment') or '—'}\n\n"
        f"Исходное сообщение:\n{(payload.get('raw_text') or '')[:1000]}",
    )

    db.add_contact_row(contact_id, client, p.digits, company_id)
    return {"contact_id": contact_id, "lead_id": lead_id,
            "agent_contact_id": agent_contact_id, "company_id": company_id}


# ==========================================================================
# Кнопки подтверждения
# ==========================================================================

def _agency_pick_kb(pending_id: int, res=None,
                    lang: str = i18n.RU) -> InlineKeyboardMarkup:
    """
    Кнопки выбора агентства.

    Сначала похожие по написанию (если агент что-то указал), потом весь
    справочник, и обязательно кнопка добавить новое — иначе при пустом
    справочнике разговор упирается в тупик.
    """
    rows: list[list[InlineKeyboardButton]] = []
    shown: set[int] = set()

    if res:
        for c in res.candidates:
            if c.agency_id:
                shown.add(c.agency_id)
                rows.append([InlineKeyboardButton(
                    text=f"✅ {c.name}",
                    callback_data=f"pset:{pending_id}:{c.agency_id}")])

    for a in db.list_agencies()[:20]:
        if a["id"] in shown:
            continue
        rows.append([InlineKeyboardButton(
            text=a["name"], callback_data=f"pset:{pending_id}:{a['id']}")])

    rows.append([InlineKeyboardButton(
        text=texts.t(lang, "btn_add_agency"),
        callback_data=f"pnew:{pending_id}")])
    rows.append([InlineKeyboardButton(
        text=texts.t(lang, "btn_cancel"),
        callback_data=f"nofix:{pending_id}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def _show_confirm(message, row: dict) -> None:
    """Пересчитывает вердикт и показывает карточку подтверждения."""
    payload = row["payload"]
    lang = payload.get("lang") or cfg.default_lang
    p = phones.from_digits(payload["digits"])
    matches, _ = await find_all_matches(p)
    d = vd.decide(matches, payload.get("agency_id"), row["author_id"],
                  retail_ttl_days=cfg.retail_ttl_days)

    if not d.creates_fixation:
        db.close_pending(row["id"], "cancelled")
        await message.edit_text(
            texts.render(d, client=payload.get("client") or "—", p=p,
                         agency=payload.get("agency_name"),
                         ttl_days=cfg.retail_ttl_days, lang=lang),
            disable_web_page_preview=True)
        return

    note = {
        vd.Verdict.UNIQUE: texts.confirm_note_unique(lang),
        vd.Verdict.OTHER_AGENCY: texts.confirm_note_other_agency(d, lang),
        vd.Verdict.RETAIL_EXPIRED: texts.confirm_note_retail_expired(
            d, cfg.retail_ttl_days, lang),
    }.get(d.verdict, "")

    card = texts.confirm_card(
        client=payload.get("client"), p=p, agency=payload.get("agency_name"),
        agent=payload.get("author") or "—", object_=payload.get("object"),
        verdict_note=note, lang=lang)
    if cfg.dry_run:
        card += texts.dry_run_note(lang)
    await message.edit_text(
        card,
        reply_markup=_confirm_kb(row["id"], bool(payload.get("client")), lang),
        disable_web_page_preview=True)


async def try_agency_reply(m: Message) -> bool:
    """
    Ответ на просьбу назвать агентство.

    Заявку находим по сообщению, на которое отвечают, — это надёжнее,
    чем держать состояние диалога в памяти: бот может перезапуститься,
    а привязка ответа к сообщению никуда не денется.

    Возвращает True, если сообщение было названием агентства и обработано.
    """
    if m.from_user is None:
        return False
    name = (m.text or "").strip()[:100]
    if not name or name.startswith("/"):
        return False

    reply = m.reply_to_message
    row = None

    if reply and reply.from_user and reply.from_user.is_bot:
        row = db.conn.execute(
            "SELECT id FROM pending WHERE account_id=? AND chat_id=?"
            " AND prompt_id=? AND status='waiting'",
            (db.account_id, m.chat.id, reply.message_id),
        ).fetchone()

    if row is None:
        # Ответ обычным сообщением, без реплая. Так делают постоянно,
        # поэтому принимаем и такой: ищем свежий незакрытый вопрос от
        # этого же человека в этом же чате.
        if len(name) > 60 or phones.extract_all(name, chat_region(m.chat.id)):
            return False
        row = db.conn.execute(
            "SELECT id FROM pending WHERE account_id=? AND chat_id=?"
            " AND author_id=? AND status='waiting' AND created_at > ?"
            " ORDER BY created_at DESC LIMIT 1",
            (db.account_id, m.chat.id, m.from_user.id,
             int(time.time()) - 15 * 60),
        ).fetchone()

    if row is None:
        return False

    pending = db.get_pending(row["id"])
    if not pending or pending["author_id"] != m.from_user.id:
        return False

    payload = pending["payload"]
    awaiting = payload.get("awaiting")
    if awaiting is None:
        awaiting = "agency" if not payload.get("agency_id") else None
    if awaiting not in ("agency", "name"):
        return False

    if awaiting == "name":
        payload["client"] = name
        payload.pop("awaiting", None)
        db.update_pending_payload(pending["id"], payload)
        pending["payload"] = payload
        await m.reply(texts.client_name_saved(name, payload.get("lang") or cfg.default_lang))
    else:
        aid, display, _ = await resolve_agency(name, None, None)
        if aid is None:
            norm = ag.norm_agency(name)
            display = ag.pretty_name(name)
            aid = db.create_agency(display, norm)
            db.add_agency_alias(aid, norm)

        payload["agency_id"] = aid
        payload["agency_name"] = display
        payload.pop("awaiting", None)
        db.update_pending_payload(pending["id"], payload)
        db.upsert_agent(m.from_user.id, m.from_user.username,
                        payload.get("author"), agency_id=aid)
        pending["payload"] = payload
        await m.reply(texts.agency_saved(display or name,
                                        payload.get("lang") or cfg.default_lang))

    # Карточку показываем на месте вопроса, если он ещё доступен,
    # иначе новым сообщением.
    target = reply if (reply and reply.message_id == pending.get("prompt_id")) else None
    try:
        if target is not None:
            await _show_confirm(target, pending)
        else:
            sent = await m.answer("…")
            db.set_pending_prompt(pending["id"], sent.message_id)
            await _show_confirm(sent, pending)
    except Exception:  # noqa: BLE001
        log.exception("не смог показать карточку подтверждения")
    return True


async def _load_pending(c: CallbackQuery) -> dict | None:
    """Достаёт заявку и проверяет, что жмёт именно её автор."""
    pending_id = int(c.data.split(":")[1])
    row = db.get_pending(pending_id)
    if not row:
        await c.answer("Заявка не найдена", show_alert=True)
        return None
    if row["status"] != "waiting":
        await c.answer("Заявка уже обработана", show_alert=True)
        return None
    if c.from_user and c.from_user.id != row["author_id"]:
        await c.answer(texts.not_your_prompt(
            row["payload"].get("lang") or cfg.default_lang), show_alert=True)
        return None
    return row


@dp.callback_query(F.data.startswith("nofix:"))
async def cb_cancel(c: CallbackQuery) -> None:
    row = await _load_pending(c)
    if not row:
        return
    db.close_pending(row["id"], "cancelled")
    await c.message.edit_text(texts.cancelled(row["payload"].get("lang") or cfg.default_lang))
    await c.answer()


@dp.callback_query(F.data.startswith("fix:"))
async def cb_confirm(c: CallbackQuery) -> None:
    row = await _load_pending(c)
    if not row:
        return
    payload = row["payload"]
    lang = payload.get("lang") or cfg.default_lang
    p = phones.from_digits(payload["digits"])

    await c.answer("…")

    # Данные могли измениться, пока карточка висела в чате, —
    # пересчитываем вердикт перед записью.
    matches, _ = await find_all_matches(p)
    d = vd.decide(matches, payload.get("agency_id"), row["author_id"],
                  retail_ttl_days=cfg.retail_ttl_days)
    if not d.creates_fixation:
        db.close_pending(row["id"], "cancelled")
        await c.message.edit_text(
            "Пока вы думали, ситуация изменилась:\n\n"
            + texts.render(d, client=payload.get("client") or "—", p=p,
                           agency=payload.get("agency_name"),
                           ttl_days=cfg.retail_ttl_days, lang=lang),
            disable_web_page_preview=True,
        )
        return

    if cfg.dry_run:
        db.close_pending(row["id"], "done")
        _, _, pname = target_pipeline()
        where = f"\nСделка ушла бы в воронку «{texts.esc(pname)}»." if pname else ""
        await c.message.edit_text(
            "✅ Подтверждено." + where + "\n\n"
            "👀 <i>Режим наблюдения: в amoCRM ничего не записано. "
            "Выключите DRY_RUN, чтобы фиксации сохранялись.</i>")
        return

    if target_pipeline()[0] is None:
        await c.message.edit_text(texts.no_agency_pipeline(lang))
        return

    try:
        created = await _create_in_amo(payload, p, row["author_id"])
    except Exception as e:  # noqa: BLE001
        log.exception("amoCRM")
        await c.message.edit_text(
            f"❌ Не смог записать в amoCRM: {texts.esc(str(e))[:300]}")
        return

    db.close_pending(row["id"], "done")
    fixation_id = db.log_fixation(
        digits=p.digits, client_name=payload.get("client"),
        agency_id=payload.get("agency_id"), agent_telegram_id=row["author_id"],
        agent_name=payload.get("author"), chat_id=row["chat_id"],
        chat_title=payload.get("chat_title"), message_id=row["message_id"],
        raw_text=payload.get("raw_text"), verdict=d.verdict.value,
        amo_contact_id=created["contact_id"], amo_lead_id=created["lead_id"],
    )

    agent = db.get_agent(row["author_id"])
    subscribed = bool(agent and agent["dm_open"])
    text = texts.confirmed(
        payload.get("client"), p, payload.get("agency_name"),
        amo.contact_url(created["contact_id"]),
        amo.lead_url(created["lead_id"]),
        amo.contact_url(created["agent_contact_id"])
        if created["agent_contact_id"] else None,
        lang=lang,
    )
    if subscribed:
        text += texts.t(lang, "watch_hint")
    await c.message.edit_text(
        text,
        reply_markup=None if subscribed else watch_button(fixation_id, lang),
        disable_web_page_preview=True,
    )

    # Конкурентам сообщаем сразу: скорость здесь решает, кто доведёт
    # клиента до депозита первым.
    try:
        await notify_rivals_about_new(fixation_id, p.digits,
                                      payload.get("agency_id"),
                                      payload.get("client"))
    except Exception:  # noqa: BLE001
        log.exception("не смог оповестить конкурентов")

    if d.notifies_admin and d.verdict is vd.Verdict.RETAIL_EXPIRED:
        await notify_admins(texts.admin_retail_expired(
            payload.get("client") or "—", p, payload.get("agency_name"),
            payload.get("chat_title"), d))


@dp.callback_query(F.data.startswith("pag:"))
async def cb_change_agency(c: CallbackQuery) -> None:
    """Показывает список известных агентств, чтобы поменять выбранное."""
    row = await _load_pending(c)
    if not row:
        return
    agencies = db.list_agencies()
    if not agencies:
        await c.answer("Справочник агентств пуст — укажите агентство "
                       "в сообщении", show_alert=True)
        return
    buttons = [[InlineKeyboardButton(
        text=a["name"], callback_data=f"pset:{row['id']}:{a['id']}"
    )] for a in agencies[:20]]
    lang = row["payload"].get("lang") or cfg.default_lang
    buttons.append([InlineKeyboardButton(
        text=texts.t(lang, "btn_add_agency"),
        callback_data=f"pnew:{row['id']}")])
    buttons.append([InlineKeyboardButton(
        text=texts.t(lang, "btn_back"), callback_data=f"pback:{row['id']}")])
    await c.message.edit_reply_markup(
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))
    await c.answer()


@dp.callback_query(F.data.startswith("pset:"))
async def cb_set_agency(c: CallbackQuery) -> None:
    _, pid, aid = c.data.split(":")
    c.data = f"pset:{pid}"          # чтобы _load_pending разобрал id
    row = await _load_pending(c)
    if not row:
        return
    agency = db.get_agency(int(aid))
    if not agency:
        await c.answer("Агентство не найдено", show_alert=True)
        return

    payload = row["payload"]
    payload["agency_id"] = agency["id"]
    payload["agency_name"] = agency["name"]
    db.update_pending_payload(row["id"], payload)
    # Запоминаем выбор за агентом — в следующий раз спрашивать не придётся.
    db.upsert_agent(row["author_id"], payload.get("username"),
                    payload.get("author"), agency_id=agency["id"])
    row["payload"] = payload

    await c.answer(f"Агентство: {agency['name']}")
    await _show_confirm(c.message, row)


@dp.callback_query(F.data.startswith("pnew:"))
async def cb_new_agency(c: CallbackQuery) -> None:
    """
    Просит прислать название агентства.

    ForceReply заставляет Telegram сам подставить ответ на нужное
    сообщение — иначе человек пишет обычным сообщением, и связать его
    с вопросом не по чему. Отдельным сообщением, потому что к
    отредактированному ForceReply прицепить нельзя.
    """
    row = await _load_pending(c)
    if not row:
        return
    lang = row["payload"].get("lang") or cfg.default_lang
    await _ask_by_reply(c, row, "agency", texts.ask_agency_name(lang),
                        texts.t(lang, "agency_field"),
                        texts.t(lang, "agency_ph"))


@dp.callback_query(F.data.startswith("pname:"))
async def cb_client_name(c: CallbackQuery) -> None:
    """Даёт дописать или поправить имя клиента до фиксации."""
    row = await _load_pending(c)
    if not row:
        return
    lang = row["payload"].get("lang") or cfg.default_lang
    current = row["payload"].get("client")
    await _ask_by_reply(c, row, "name", texts.ask_client_name(current, lang),
                        texts.t(lang, "name_field"),
                        texts.t(lang, "name_ph"))


async def _ask_by_reply(c: CallbackQuery, row: dict, awaiting: str,
                        prompt: str, field_title: str,
                        placeholder: str) -> None:
    """Общий сценарий «спросить значение и дождаться ответа»."""
    payload = row["payload"]
    payload["awaiting"] = awaiting
    db.update_pending_payload(row["id"], payload)

    await c.message.edit_text(prompt)
    ask = await c.message.answer(
        field_title,
        reply_markup=ForceReply(input_field_placeholder=placeholder,
                                selective=True),
    )
    db.set_pending_prompt(row["id"], ask.message_id)
    await c.answer()


@dp.callback_query(F.data.startswith("pback:"))
async def cb_back(c: CallbackQuery) -> None:
    row = await _load_pending(c)
    if not row:
        return
    await c.message.edit_reply_markup(reply_markup=_confirm_kb(
        row["id"], bool(row["payload"].get("client")),
        row["payload"].get("lang") or cfg.default_lang))
    await c.answer()


async def expire_loop() -> None:
    """Убирает из чата карточки, на которые никто не ответил."""
    while True:
        await asyncio.sleep(60)
        try:
            for row in db.expire_pending(cfg.confirm_ttl_min * 60):
                if not row.get("prompt_id"):
                    continue
                try:
                    await bot.edit_message_text(
                        chat_id=row["chat_id"], message_id=row["prompt_id"],
                        text=texts.expired_prompt(
                            row["payload"].get("lang") or cfg.default_lang))
                except Exception:  # noqa: BLE001
                    pass
        except Exception:  # noqa: BLE001
            log.exception("Ошибка чистки заявок")


async def _note_attempt(d, client: str, p: phones.Phone, agency_name: str | None,
                        m: Message, author: str, text: str) -> None:
    """Пишет примечание в найденную карточку — чтобы попытка не потерялась."""
    target = next((x for x in d.reasons if x.contact_id), None)
    if not target:
        return
    try:
        await amo.add_note(
            "contacts", target.contact_id,
            f"Попытка фиксации из Telegram — отклонена ({d.verdict.value}).\n"
            f"Агентство: {agency_name or '—'}\nАгент: {author}\n"
            f"Чат: {m.chat.title}\nНомер в сообщении: {p.pretty()}\n\n"
            f"{text[:500]}",
        )
    except Exception:  # noqa: BLE001
        log.exception("Не удалось добавить примечание")


# ==========================================================================
# Уточнение агентства кнопками
# ==========================================================================

# ==========================================================================

# ==========================================================================
# Личка агента: подписка, кабинет, уведомления
# ==========================================================================

#: Имя бота нужно для deep link. Узнаём один раз при старте.
BOT_USERNAME = ""


def agent_lang(agent_row, fallback: str | None = None) -> str:
    """
    Язык для личных сообщений агенту.

    Берётся из того, на каком языке он пишет в рабочих чатах, — это
    единственный надёжный признак. По имени профиля судить нельзя:
    «Timur» латиницей у русскоязычного человека сплошь и рядом.
    """
    if agent_row is not None and agent_row["lang"]:
        return i18n.normalize_lang(agent_row["lang"], cfg.default_lang)
    return fallback or cfg.default_lang


def watch_button(fixation_id: int, lang: str) -> InlineKeyboardMarkup | None:
    """
    Кнопка-ссылка «Отслеживать статус».

    Telegram запрещает боту писать первым, пока человек не нажал Start
    в личке. Deep link решает это: агент тапает, попадает в личный чат,
    и вместе со Start бот получает код фиксации.
    """
    if not BOT_USERNAME:
        return None
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(
        text=texts.t(lang, "btn_watch"),
        url=f"https://t.me/{BOT_USERNAME}?start=fix{fixation_id}")]])


@dp.message(CommandStart(deep_link=True), F.chat.type == "private")
async def cmd_start_deeplink(m: Message, command: CommandObject) -> None:
    """Агент пришёл по кнопке «Отслеживать статус»."""
    payload = (command.args or "").strip()

    if payload.startswith("onb_"):
        await start_onboarding_flow(m, payload[4:])
        return

    if not payload.startswith("fix") or not payload[3:].isdigit():
        await cmd_start_plain(m)
        return

    fx = db.get_fixation(int(payload[3:]))
    agent = db.get_agent(m.from_user.id) if m.from_user else None
    lang = agent_lang(agent)

    if not fx:
        await m.answer(texts.t(lang, "dm_hello"))
        return

    # Отслеживать чужую фиксацию нельзя: иначе кто угодно, получив
    # ссылку, узнает контакты чужих клиентов.
    if fx["agent_telegram_id"] != m.from_user.id:
        await m.answer(texts.t(lang, "dm_not_yours"))
        return

    db.upsert_agent(m.from_user.id, m.from_user.username,
                    m.from_user.full_name, dm_open=True)
    db.set_watching(fx["id"], True)

    agency = db.get_agency(fx["agency_id"]) if fx["agency_id"] else None
    status = (db.status_title(fx["last_pipeline_id"], fx["last_status_id"])
              or texts.t(lang, "my_unknown_status"))
    await m.answer(
        texts.t(lang, "dm_hello") + "\n\n"
        + texts.t(lang, "dm_fixation",
                  client=texts.esc(fx["client_name"]) or "—",
                  phone=phones.from_digits(fx["digits"]).pretty(),
                  agency=texts.esc(agency["name"]) if agency else "—",
                  status=texts.esc(status)))

    if not (agent and agent["phone"]):
        await _ask_phone(m, lang)


@dp.message(CommandStart(), F.chat.type == "private")
async def cmd_start_plain(m: Message) -> None:
    agent = db.get_agent(m.from_user.id) if m.from_user else None
    lang = agent_lang(agent)
    if m.from_user:
        db.upsert_agent(m.from_user.id, m.from_user.username,
                        m.from_user.full_name, dm_open=True)
    await m.answer(texts.t(lang, "dm_hello"))
    if not (agent and agent["phone"]):
        await _ask_phone(m, lang)


async def _ask_phone(m: Message, lang: str) -> None:
    """Телефон берём кнопкой Telegram — руками его никто вводить не станет."""
    kb = ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text=texts.t(lang, "btn_share_phone"),
                                  request_contact=True)],
                  [KeyboardButton(text=texts.t(lang, "btn_skip"))]],
        resize_keyboard=True, one_time_keyboard=True)
    await m.answer(texts.t(lang, "dm_ask_phone"), reply_markup=kb)


@dp.message(F.contact, F.chat.type == "private")
async def on_contact(m: Message) -> None:
    if not m.from_user or not m.contact:
        return
    # Чужой контакт не берём: кнопкой можно прислать и не свой номер.
    if m.contact.user_id and m.contact.user_id != m.from_user.id:
        return
    agent = db.get_agent(m.from_user.id)
    lang = agent_lang(agent)
    p = phones.normalize(m.contact.phone_number, cfg.default_region)
    value = p.pretty() if p else m.contact.phone_number

    db.upsert_agent(m.from_user.id, m.from_user.username,
                    m.from_user.full_name, dm_open=True)
    db.set_agent_field(m.from_user.id, phone=value)
    await m.answer(texts.t(lang, "dm_phone_saved", phone=texts.esc(value)),
                   reply_markup=ReplyKeyboardRemove())

    # Телефон нужен не столько боту, сколько карточке агента в CRM —
    # иначе связаться с ним по-прежнему не через что.
    if agent and agent["amo_contact_id"]:
        try:
            await amo.set_contact_phone(agent["amo_contact_id"], value,
                                        cfg.phone_field_id)
        except Exception:  # noqa: BLE001
            log.exception("не смог записать телефон агента в CRM")


@dp.message(F.chat.type == "private", F.text.in_({"Пропустить", "Skip"}))
async def on_skip_phone(m: Message) -> None:
    lang = agent_lang(db.get_agent(m.from_user.id) if m.from_user else None)
    await m.answer(texts.t(lang, "dm_phone_skipped"),
                   reply_markup=ReplyKeyboardRemove())


@dp.message(Command("my"), F.chat.type == "private")
async def cmd_my(m: Message) -> None:
    """Личный кабинет: свои фиксации и их статусы."""
    if not m.from_user:
        return
    agent = db.get_agent(m.from_user.id)
    lang = agent_lang(agent)
    rows = db.agent_fixations(m.from_user.id)
    if not rows:
        await m.answer(texts.t(lang, "my_empty"))
        return

    out = [texts.t(lang, "my_title"), ""]
    for i, r in enumerate(rows, 1):
        status = (db.status_title(r["last_pipeline_id"], r["last_status_id"])
                  or texts.t(lang, "my_unknown_status"))
        out.append(texts.t(lang, "my_row", n=i,
                           client=texts.esc(r["client_name"]) or "—",
                           phone=phones.from_digits(r["digits"]).pretty(),
                           status=texts.esc(status)))
    await m.answer("\n".join(out))


async def on_private_any(m: Message) -> None:
    """
    Всё, что админ прислал в режиме рассылки, плюс подсказка остальным.

    Ловим не только текст: рассылают обычно с фото, видео и альбомами.
    Регистрируется последним (см. конец файла) — иначе перехватил бы
    команды, и они бы просто перестали работать.
    """
    if (m.text or "").startswith("/"):
        return
    # Сначала — ответы на вопрос, заданный только что нажатой кнопкой.
    # Мастер подключения живёт неделями, а незаконченная заявка иначе
    # съедает всё подряд: дата обслуживания уходила ему, и он отвечал
    # «не похоже на ключ бота».
    if await try_wallet_reply(m):
        return
    if await try_billing_start_reply(m):
        return
    if await try_onboarding_step(m):
        return
    if await try_add_staff(m):
        return
    if await try_capture_broadcast(m):
        return
    await on_private_text(m)


async def on_private_text(m: Message) -> None:
    """
    Любое другое сообщение в личке.

    Агенты пробуют фиксировать прямо здесь — и это разумно с их стороны,
    но здесь бот не знает, от какого чата и застройщика речь. Молчать
    в ответ хуже всего: человек решит, что бот сломался.
    """
    if not m.from_user or (m.text or "").startswith("/"):
        return
    lang = agent_lang(db.get_agent(m.from_user.id))
    if llm.prefilter(m.text or ""):
        await m.answer(texts.t(lang, "dm_use_group"))


@dp.message(Command("notify"), F.chat.type == "private")
async def cmd_notify(m: Message) -> None:
    if not m.from_user:
        return
    agent = db.get_agent(m.from_user.id)
    lang = agent_lang(agent)
    arg = (m.text or "").split(maxsplit=1)
    on = not (len(arg) > 1 and arg[1].strip().lower() in ("off", "выкл", "0"))
    db.upsert_agent(m.from_user.id, m.from_user.username, m.from_user.full_name)
    db.set_agent_field(m.from_user.id, subscribed=int(on))
    await m.answer(texts.t(lang, "notify_on" if on else "notify_off"))


# ==========================================================================
# Опрос статусов
# ==========================================================================

#: Успешное закрытие сделки в amoCRM — системный этап.
WON_STATUS_ID = 142


def _can_dm(agent) -> bool:
    return bool(agent and agent["dm_open"] and agent["subscribed"])


async def check_statuses() -> None:
    """
    Опрашивает amoCRM по своим сделкам.

    Уведомляем не о каждом движении: агенту из чужого агентства ни к чему
    видеть внутреннюю кухню воронки. Сообщаем только о том, что успешная
    сделка состоялась.
    """
    watched = db.watched_leads()
    if not watched:
        return

    leads = await amo.leads_by_ids([r["amo_lead_id"] for r in watched])

    for row in watched:
        lead = leads.get(row["amo_lead_id"])
        if not lead:
            continue
        new_status = lead.get("status_id")
        if new_status == row["last_status_id"]:
            continue

        old_status = row["last_status_id"]
        db.update_fixation_status(row["id"], new_status,
                                  lead.get("pipeline_id"))

        # Первый обход только запоминает состояние: иначе агент получил бы
        # поздравление с давно закрытой сделкой.
        if old_status is None or new_status != WON_STATUS_ID:
            continue

        agent = db.get_agent(row["agent_telegram_id"])
        if _can_dm(agent) and row["watching"]:
            await _send_dm(agent["telegram_id"], texts.t(
                agent_lang(agent), "notify_won",
                client=texts.esc(row["client_name"]) or "—",
                phone=phones.from_digits(row["digits"]).pretty()))


async def check_expiring() -> None:
    """Напоминает агентам, что фиксация подходит к концу."""
    rows = db.expiring_fixations(cfg.fixation_ttl_days, cfg.renew_warn_days)
    for row in rows:
        agent = db.get_agent(row["agent_telegram_id"])
        db.mark_reminded(row["id"])
        if not (_can_dm(agent) and row["watching"]):
            continue
        lang = agent_lang(agent)
        await _send_dm(
            agent["telegram_id"],
            texts.t(lang, "notify_expiring",
                    client=texts.esc(row["client_name"]) or "—",
                    phone=phones.from_digits(row["digits"]).pretty(),
                    date=texts.when(row["expires_at"], lang)),
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text=texts.t(lang, "btn_renew"),
                                     callback_data=f"renew:{row['id']}")]]))


async def check_direct_department() -> None:
    """
    Ловит момент, когда клиент сам постучался в прямой отдел продаж
    уже после того, как его зафиксировало агентство.

    Для агента это хорошая новость: клиент остаётся за ним. Сообщить об
    этом важно — иначе он узнает о конкуренции с отделом продаж случайно
    и решит, что его обошли.
    """
    for row in db.fixations_awaiting_retail_check():
        p = phones.from_digits(row["digits"])
        retail = [m for m in db.find_matches(p)
                  if m.has_retail and (m.last_retail_activity or 0) > row["created_at"]]
        if not retail:
            continue

        db.mark_retail_notified(row["id"])
        agent = db.get_agent(row["agent_telegram_id"])
        if not (_can_dm(agent) and row["watching"]):
            continue
        await _send_dm(agent["telegram_id"], texts.t(
            agent_lang(agent), "notify_direct",
            client=texts.esc(row["client_name"]) or "—", phone=p.pretty()))


async def notify_rivals_about_new(fixation_id: int, digits: str,
                                  agency_id: int | None,
                                  client_name: str | None) -> None:
    """
    Сообщает тем, кто фиксировал этого же клиента раньше, что появился
    ещё один претендент. Эксклюзива нет, но знать об этом полезно:
    именно от скорости зависит, кто доведёт клиента до депозита.
    """
    phone = phones.from_digits(digits).pretty()
    seen: set[int] = set()
    for rival in db.rivals_for(digits, agency_id):
        tid = rival["agent_telegram_id"]
        if tid in seen:
            continue
        seen.add(tid)
        agent = db.get_agent(tid)
        if not _can_dm(agent):
            continue
        await _send_dm(tid, texts.t(
            agent_lang(agent), "notify_rival",
            client=texts.esc(rival["client_name"]) or texts.esc(client_name) or "—",
            phone=phone))


@dp.callback_query(F.data.startswith("renew:"))
async def cb_renew(c: CallbackQuery) -> None:
    """Агент подтвердил, что продолжает работу с клиентом."""
    fixation_id = int(c.data.split(":")[1])
    row = db.get_fixation(fixation_id)
    if not row:
        await c.answer("Фиксация не найдена", show_alert=True)
        return
    if not c.from_user or c.from_user.id != row["agent_telegram_id"]:
        await c.answer(texts.not_your_prompt(cfg.default_lang), show_alert=True)
        return

    agent = db.get_agent(c.from_user.id)
    lang = agent_lang(agent)
    ts = db.renew_fixation(fixation_id)
    until = ts + cfg.fixation_ttl_days * 86400

    if row["amo_lead_id"]:
        try:
            await amo.add_note("leads", row["amo_lead_id"], texts.t(
                lang, "renew_note", agent=row["agent_name"] or "—",
                date=texts.when(until, lang)))
        except Exception:  # noqa: BLE001
            log.exception("не смог записать примечание о продлении")

    await c.message.edit_text(texts.t(
        lang, "renewed", client=texts.esc(row["client_name"]) or "—",
        date=texts.when(until, lang)))
    await c.answer()


async def _send_dm(telegram_id: int, text: str, reply_markup=None) -> None:
    try:
        await bot.send_message(telegram_id, text, reply_markup=reply_markup,
                               disable_web_page_preview=True)
    except Exception as e:  # noqa: BLE001
        # Агент мог заблокировать бота — помечаем, чтобы не долбиться.
        log.info("не смог написать агенту %s: %s", telegram_id, str(e)[:120])
        db.set_agent_field(telegram_id, dm_open=0)


async def status_loop() -> None:
    while True:
        await asyncio.sleep(cfg.status_check_min * 60)
        for name, job in (("статусы", check_statuses),
                          ("сроки фиксаций", check_expiring),
                          ("прямой отдел", check_direct_department)):
            try:
                await job()
            except Exception:  # noqa: BLE001
                log.exception("Ошибка проверки: %s", name)


# ==========================================================================
# Подключение нового клиента
# ==========================================================================

async def start_onboarding_flow(m: Message, code: str) -> None:
    """Клиент открыл ссылку-приглашение."""
    reason = db.use_invite(code, m.from_user.id)
    if reason:
        await m.answer(onb.INVALID_INVITE.get(
            reason, "Ссылка недействительна."))
        return

    onb_id = db.start_onboarding(code, m.from_user.id, m.from_user.username,
                                 m.from_user.full_name)
    await m.answer(onb.WELCOME)
    await m.answer(onb.ask_text("developer"))
    log.info("Начато подключение клиента: %s (заявка %s)",
             m.from_user.full_name, onb_id)


async def try_onboarding_step(m: Message) -> bool:
    """
    Очередной ответ клиента в помощнике подключения.

    Каждый ответ проверяется сразу: ключ бота — у Telegram, доступ
    к amoCRM — живым запросом. Человек узнаёт об ошибке через пару
    секунд, а не когда оператор попытается всё запустить.
    """
    if not m.from_user:
        return False
    row = db.active_onboarding(m.from_user.id)
    if not row:
        return False

    text = (m.text or "").strip()
    if not text or text.startswith("/"):
        return False

    step, data = row["step"], row["data"]

    if step == "developer":
        if len(text) < 2 or len(text) > 60:
            await m.answer("Название нужно от двух до шестидесяти символов.")
            return True
        data["developer"] = text
        await m.answer(onb.ok_text(step, text))

    elif step == "bot_token":
        note = await m.answer("Проверяю ключ…")
        ok, detail = await onb.check_bot_token(text)
        await _forget_secret(m)
        if not ok:
            await note.edit_text(f"❌ {detail}\n\nПришлите ключ ещё раз.")
            return True
        data["bot_token"] = text
        data["bot_check"] = detail
        await note.edit_text(onb.ok_text(step, detail))

    elif step == "subdomain":
        sub = text.lower().replace("https://", "").replace("http://", "")
        sub = sub.split(".")[0].strip("/ ")
        data["subdomain"] = sub
        await m.answer(onb.ok_text(step, sub))

    elif step == "amo_token":
        note = await m.answer("Проверяю доступ к amoCRM, это займёт секунд "
                              "десять…")
        ok, detail = await onb.check_amo(data.get("subdomain", ""), text)
        await _forget_secret(m)
        if not ok:
            await note.edit_text(
                f"❌ {detail}\n\nПроверьте и пришлите токен ещё раз.\n"
                "Если ошибка про права — добавьте интеграции доступ "
                "к сделкам, контактам и компаниям.")
            return True
        data["amo_token"] = text
        data["amo_check"] = detail
        await note.edit_text(onb.ok_text(step, detail))

    else:
        return False

    await _advance_onboarding(m, row["id"], step, data)
    return True


async def _forget_secret(m: Message) -> None:
    """Убирает сообщение с ключом из переписки."""
    try:
        await m.delete()
    except Exception:  # noqa: BLE001
        log.info("не смог удалить сообщение с секретом")


async def _advance_onboarding(m: Message, onb_id: int, step: str,
                              data: dict) -> None:
    idx = onb.STEPS.index(step)
    if idx + 1 < len(onb.STEPS):
        nxt = onb.STEPS[idx + 1]
        db.update_onboarding(onb_id, step=nxt, data=data)
        kb = None
        if nxt == "privacy":
            kb = InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="✅ Сделал",
                                     callback_data=f"onbok:{onb_id}")]])
        await m.answer(onb.ask_text(nxt), reply_markup=kb)
        return

    await _finish_onboarding(m, onb_id, data)


async def _finish_onboarding(m: Message, onb_id: int, data: dict) -> None:
    taken: set[str] = set()
    if cfg.clients_dir:
        root = Path(cfg.clients_dir).expanduser()
        if root.is_dir():
            taken = {x.name for x in root.iterdir() if x.is_dir()}
    slug = onb.slugify(data.get("developer", ""), taken)

    db.update_onboarding(onb_id, status="ready", data=data, slug=slug)
    await m.answer(onb.FINISH)

    row = db.get_onboarding(onb_id)
    for op in cfg.operator_ids:
        await _send_dm(op, onb.summary_for_operator(row),
                       reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                           [InlineKeyboardButton(
                               text="🚀 Развернуть",
                               callback_data=f"onbgo:{onb_id}")],
                           [InlineKeyboardButton(
                               text="❌ Отклонить",
                               callback_data=f"onbno:{onb_id}")],
                       ]))


@dp.callback_query(F.data.startswith("onbok:"))
async def cb_onb_privacy(c: CallbackQuery) -> None:
    onb_id = int(c.data.split(":")[1])
    row = db.get_onboarding(onb_id)
    if not row or row["user_id"] != c.from_user.id:
        await c.answer("Недоступно", show_alert=True)
        return
    await c.message.edit_text(onb.ok_text("privacy", ""))
    await _advance_onboarding(c.message, onb_id, "privacy", row["data"])
    await c.answer()


@dp.callback_query(F.data.startswith("onbno:"))
async def cb_onb_reject(c: CallbackQuery) -> None:
    if not is_operator(c.from_user.id):
        await c.answer("Недоступно", show_alert=True)
        return
    onb_id = int(c.data.split(":")[1])
    db.update_onboarding(onb_id, status="rejected")
    await c.message.edit_text("Заявка отклонена.")
    await c.answer()


@dp.callback_query(F.data.startswith("onbgo:"))
async def cb_onb_deploy(c: CallbackQuery) -> None:
    """Оператор нажал «Развернуть»."""
    if not is_operator(c.from_user.id):
        await c.answer("Недоступно", show_alert=True)
        return

    onb_id = int(c.data.split(":")[1])
    row = db.get_onboarding(onb_id)
    if not row or row["status"] != "ready":
        await c.answer("Заявка уже обработана", show_alert=True)
        return

    d = row["data"]
    slug = row["slug"] or onb.slugify(d.get("developer", ""))
    await c.answer("Разворачиваю…")
    await c.message.edit_text(f"Разворачиваю <code>{slug}</code>…")

    env_text = pv.render_env(
        developer=d.get("developer", slug),
        bot_token=d.get("bot_token", ""),
        subdomain=d.get("subdomain", ""),
        amo_token=d.get("amo_token", ""),
        operator_ids=cfg.operator_ids,
        owner_ids={row["user_id"]},
        db_path=str(Path(cfg.clients_dir).expanduser() / slug / "fixbot.db"),
        inherited=pv.collect_inherited(dict(os.environ)),
        stamp=texts.when(int(time.time())),
    )

    try:
        report = await pv.deploy(clients_dir=cfg.clients_dir, slug=slug,
                                 env_text=env_text)
    except pv.ProvisionError as e:
        await c.message.edit_text(f"❌ Не смог развернуть: {texts.esc(str(e))}")
        return

    db.update_onboarding(onb_id, status="done", slug=slug)
    handle = (d.get("bot_check") or "").split()[0] if d.get("bot_check") else None
    await c.message.edit_text(pv.deploy_report(slug, report, handle))

    if report["started"] and handle:
        await _send_dm(row["user_id"], onb.DONE_FOR_CLIENT.format(bot=handle))


# ==========================================================================
# Меню управления
# ==========================================================================

#: Владельцы, которые сейчас добавляют сотрудника.
_awaiting_staff: set[int] = set()


def role_of(user_id: int) -> str:
    return mn.role_of(user_id, cfg, db)


def is_operator(user_id: int) -> bool:
    return role_of(user_id) == mn.OPERATOR


def has_menu(user_id: int) -> bool:
    return role_of(user_id) != mn.NOBODY


@dp.message(Command("admin", "menu"), F.chat.type == "private")
async def cmd_admin(m: Message) -> None:
    if not m.from_user:
        return
    if not has_menu(m.from_user.id):
        # Раньше здесь было молчание, и это путало: владелец другого бота
        # написал «/admin» трижды подряд, решив, что бот завис.
        lang = agent_lang(db.get_agent(m.from_user.id))
        await m.answer(texts.t(lang, "no_menu"))
        return
    role = role_of(m.from_user.id)
    await m.answer(mn.root_text(role, cfg.developer_name),
                   reply_markup=mn.main_menu(role, is_operator_bot()))


@dp.callback_query(F.data.startswith("m:"))
async def cb_menu(c: CallbackQuery) -> None:
    if not c.from_user or not has_menu(c.from_user.id):
        await c.answer("Недоступно", show_alert=True)
        return

    role = role_of(c.from_user.id)
    parts = c.data.split(":")
    section = parts[1]
    arg = parts[2] if len(parts) > 2 else None

    async def show(text: str, kb=None) -> None:
        try:
            await c.message.edit_text(text, reply_markup=kb or mn.back_kb(),
                                      disable_web_page_preview=True)
        except Exception:  # noqa: BLE001
            await c.message.answer(text, reply_markup=kb or mn.back_kb(),
                                   disable_web_page_preview=True)

    if section == "root":
        await show(mn.root_text(role, cfg.developer_name),
                   mn.main_menu(role, is_operator_bot()))

    elif section == "help":
        await show(mn.HELP_TEXT)

    elif section == "chats":
        rows = _chat_rows()
        await show(mn.chats_overview(rows), mn.back_kb(mn.chats_kb(rows)))

    elif section == "agencies":
        await show(mn.agencies_text(db.list_agencies()))

    elif section == "stats":
        days = int(arg) if arg is not None else 30
        await show(mn.stats_text(
            days=days,
            total=db.fixations_count(),
            period=db.fixations_count(days or None),
            by_agency=db.fixations_by_agency(days or None),
            by_agent=db.fixations_by_agent(days or None),
            agents=db.agents_summary(),
            rejected=db.rejected_by_verdict(days or None),
        ), mn.stats_menu())

    elif section == "staff":
        await show(mn.staff_text(db.list_staff(), cfg.owner_ids),
                   mn.staff_menu(db.list_staff()))

    elif section == "staffadd":
        _awaiting_staff.add(c.from_user.id)
        await show("👥 Перешлите сюда любое сообщение человека, которому "
                   "нужен доступ.\n\n"
                   "<i>Обычное сообщение не подойдёт — нужна именно "
                   "пересылка, из неё бот узнает его Telegram.</i>")

    elif section == "staffdel" and arg:
        db.remove_staff(int(arg))
        await show(mn.staff_text(db.list_staff(), cfg.owner_ids),
                   mn.staff_menu(db.list_staff()))

    elif section == "bcast":
        _awaiting_broadcast[c.from_user.id] = time.time()
        await show("📣 <b>Рассылка</b>\n\n"
                   "Пришлите или перешлите сюда сообщение — как оно должно "
                   "выглядеть у агентов. Можно с фото, видео, альбомом и "
                   "любым оформлением.\n\n"
                   "Русскоязычным уйдёт точная копия, англоязычным — те же "
                   "вложения с переводом.\n\n"
                   "Отмена: /cancel")

    # --- только оператор ---
    elif section == "clients" and role == mn.OPERATOR:
        rows = cl.scan(cfg.clients_dir) if cfg.clients_dir else []
        kb = [[InlineKeyboardButton(text=f"{c.alive_hint} {c.name}",
                                    callback_data=f"m:client:{c.slug}")]
              for c in rows[:20]]
        kb.append([InlineKeyboardButton(text="➕ Новый клиент",
                                        callback_data="m:invite")])
        pending = db.pending_onboardings()
        if pending:
            kb.append([InlineKeyboardButton(
                text=f"📥 Заявки: {len(pending)}",
                callback_data="m:pending")])
        await show(cl.overview_text(rows, cfg.clients_dir), mn.back_kb(kb))

    elif section == "invite" and role == mn.OPERATOR:
        code = onb.new_code()
        db.create_invite(code, c.from_user.id)
        link = f"https://t.me/{BOT_USERNAME}?start=onb_{code}"
        await show(onb.INVITE_TEXT.format(link=link), mn.back_kb(
            [[InlineKeyboardButton(text="← Клиенты",
                                   callback_data="m:clients")]]))

    elif section == "pending" and role == mn.OPERATOR:
        rows = db.pending_onboardings()
        if not rows:
            await show("Новых заявок нет.", mn.back_kb())
        else:
            kb = [[InlineKeyboardButton(
                text=f"🚀 {r['data'].get('developer', r['slug'])}",
                callback_data=f"onbgo:{r['id']}")] for r in rows]
            await show("📥 <b>Заявки на подключение</b>\n\n"
                       + "\n\n".join(onb.summary_for_operator(r)
                                      for r in rows[:5]),
                       mn.back_kb(kb))

    elif section == "client" and role == mn.OPERATOR and arg:
        found = [c for c in cl.scan(cfg.clients_dir) if c.slug == arg]
        if not found:
            await show("Клиент не найден.", mn.back_kb())
        else:
            has_billing = db.get_billing(arg) is not None
            extra = [[InlineKeyboardButton(
                text=("💰 Оплаты" if has_billing else "💰 Завести обслуживание"),
                callback_data=(f"bl:open:{arg}" if has_billing
                               else f"bl:setup:{arg}"))]]
            extra.append([InlineKeyboardButton(text="← Все клиенты",
                                               callback_data="m:clients")])
            await show(cl.client_text(found[0]), mn.back_kb(extra))

    elif section == "billing" and role == mn.OPERATOR:
        items = _billing_items()
        await show(blui.overview_text(items),
                   mn.back_kb(blui.overview_kb(items)))

    elif section == "tech" and role == mn.OPERATOR:
        await show("🔧 <b>Техническое</b>\n\n"
                   "Разделы для того, кто держит бота на сервере.",
                   mn.tech_menu())

    elif section == "pipelines" and role == mn.OPERATOR:
        try:
            db.replace_pipelines(await amo.pipelines())
        except Exception as e:  # noqa: BLE001
            await show(f"Не смог получить воронки: {texts.esc(str(e))[:200]}",
                       mn.tech_menu())
            return
        await show(_pipelines_text(), _pipelines_kb())

    elif section == "sync" and role == mn.OPERATOR:
        await show("Синхронизирую amoCRM, это займёт пару минут…")
        try:
            s = await sync_all()
            await show(f"Готово.\nТелефонов: {s['phones']}\n"
                       f"Контактов с происхождением: {s['origins']}\n"
                       f"Компаний: {s['companies']}", mn.tech_menu())
        except Exception as e:  # noqa: BLE001
            await show(f"Ошибка: {texts.esc(str(e))[:300]}", mn.tech_menu())

    elif section == "health" and role == mn.OPERATOR:
        await show(_health_text(), mn.tech_menu())

    else:
        await c.answer("Недоступно", show_alert=True)
        return

    await c.answer()


def _health_text() -> str:
    s = db.stats()
    synced = db.get_meta("contacts_synced_at")
    _, _, pname = target_pipeline()
    return (
        "🩺 <b>Состояние</b>\n\n"
        f"Режим: {'👀 наблюдение' if cfg.dry_run else 'боевой'}\n"
        f"amoCRM: {cfg.amo_subdomain} ({cfg.amo_auth})\n"
        f"Фиксации кладёт в: {texts.esc(pname) or '❗️ не задана'}\n"
        f"Воронки размечены: {'да' if db.is_configured() else '❗️ нет'}\n\n"
        f"Телефонов в зеркале: {s['phones']}\n"
        f"Контактов с происхождением: {s['origins']}\n"
        f"Агентств: {s['agencies']}\n"
        f"Фиксаций: {s['fixations']}\n"
        f"Агентов: {s['agents']}\n\n"
        f"Синхронизация: "
        f"{texts.when(int(synced)) if synced else 'ещё не было'}")


async def try_add_staff(m: Message) -> bool:
    """Владелец переслал сообщение человека, которому нужен доступ."""
    if not m.from_user or m.from_user.id not in _awaiting_staff:
        return False
    fwd = m.forward_from
    if not fwd:
        await m.answer("Нужна именно пересылка сообщения этого человека — "
                       "из неё бот узнаёт его Telegram.\n\n"
                       "Если у него закрыт профиль, пересылка не сработает: "
                       "тогда пусть он сам напишет боту, и попросите "
                       "оператора добавить его вручную.")
        return True

    _awaiting_staff.discard(m.from_user.id)
    name = " ".join(filter(None, [fwd.first_name, fwd.last_name])) or "без имени"
    added = db.add_staff(fwd.id, fwd.username, name, m.from_user.id)

    await m.answer(
        (f"✅ Доступ выдан: <b>{texts.esc(name)}</b>." if added
         else f"У <b>{texts.esc(name)}</b> доступ уже был."),
        reply_markup=mn.back_kb())

    if added:
        # Оператор отвечает за сервер и должен знать, кто получил доступ.
        who = author_name(m)
        for op in cfg.operator_ids:
            if op == m.from_user.id:
                continue
            await _send_dm(op, f"👥 <b>Выдан доступ к меню</b>\n"
                               f"{texts.esc(name)}"
                               f"{' (@' + fwd.username + ')' if fwd.username else ''}\n"
                               f"Выдал: {texts.esc(who)}")
    return True


# ==========================================================================
# Рассылки
# ==========================================================================

#: Админы, которые сейчас составляют рассылку: id → время входа в режим.
_awaiting_broadcast: dict[int, float] = {}
#: Копим куски альбома: media_group_id → [сообщения].
_albums: dict[str, list[Message]] = {}


@dp.message(Command("broadcast"), F.chat.type == "private")
async def cmd_broadcast(m: Message) -> None:
    if not m.from_user or not has_menu(m.from_user.id):
        return
    _awaiting_broadcast[m.from_user.id] = time.time()
    await m.answer(
        "📣 <b>Рассылка</b>\n\n"
        "Пришлите или перешлите сюда сообщение — как оно должно выглядеть "
        "у агентов. Можно с фото, видео, альбомом и любым оформлением.\n\n"
        "Русскоязычным уйдёт точная копия, англоязычным — те же вложения "
        "с переводом подписи.\n\n"
        "Отмена: /cancel")


@dp.message(Command("cancel"), F.chat.type == "private")
async def cmd_cancel_broadcast(m: Message) -> None:
    """
    Общая отмена: рассылка, ввод реквизитов и незаконченное подключение.

    Раньше отменялась только рассылка, и брошенная заявка оставалась
    висеть навсегда, перехватывая любое сообщение в личке.
    """
    if not m.from_user:
        return
    uid = m.from_user.id
    _awaiting_broadcast.pop(uid, None)
    db.set_meta(f"await_wallet:{uid}", "")
    db.set_meta(f"await_start:{uid}", "")

    what = ["Отменено."]
    row = db.active_onboarding(uid)
    if row:
        db.update_onboarding(row["id"], status="rejected")
        what.append("Незаконченное подключение закрыто.")
    await m.answer(" ".join(what))


@dp.message(Command("stop"), F.chat.type == "private")
async def cmd_stop(m: Message) -> None:
    """Отписка от рассылок. Уведомления по своим клиентам не трогаем."""
    if not m.from_user:
        return
    agent = db.get_agent(m.from_user.id)
    lang = agent_lang(agent)
    db.upsert_agent(m.from_user.id, m.from_user.username, m.from_user.full_name)
    db.set_agent_field(m.from_user.id, bcast=0)
    await m.answer(texts.t(lang, "bcast_off"))


@dp.message(Command("resume"), F.chat.type == "private")
async def cmd_resume(m: Message) -> None:
    if not m.from_user:
        return
    lang = agent_lang(db.get_agent(m.from_user.id))
    db.set_agent_field(m.from_user.id, bcast=1)
    await m.answer(texts.t(lang, "bcast_on"))


async def try_capture_broadcast(m: Message) -> bool:
    """
    Ловит сообщение, которое админ прислал для рассылки.

    Альбом приходит несколькими сообщениями подряд с общим media_group_id,
    поэтому после первого куска ждём остальные и только затем собираем.
    """
    if not m.from_user or m.from_user.id not in _awaiting_broadcast:
        return False

    if m.media_group_id:
        group = _albums.setdefault(m.media_group_id, [])
        group.append(m)
        if len(group) > 1:
            return True          # остальные куски обработает первый
        await asyncio.sleep(bc.ALBUM_WAIT)
        messages = _albums.pop(m.media_group_id, [m])
    else:
        messages = [m]

    _awaiting_broadcast.pop(m.from_user.id, None)
    draft = bc.build_draft(messages)
    bid = db.create_broadcast(m.from_user.id, draft.src_chat_id,
                              draft.message_ids, draft.items, draft.html)

    note = await m.answer("Перевожу на английский…")
    html_en = await clf.translate(draft.html) if draft.html else ""
    db.update_broadcast(bid, html_en=html_en or "")

    # Превью показываем настоящими сообщениями, а не пересказом. Раньше
    # я вставлял размеченный текст внутрь своего и обрезал по длине —
    # обрезка рвала тег посередине, и Telegram отказывался это принимать.
    try:
        await note.edit_text("👀 <b>Так увидят русскоязычные агенты:</b>")
        await bc.send_copy(bot, m.chat.id, draft)

        if html_en and html_en != draft.html:
            await m.answer("👀 <b>Так увидят англоязычные:</b>")
            await bc.send_translated(bot, m.chat.id, draft, html_en)
        elif draft.html:
            await m.answer("<i>Перевести не удалось — англоязычным уйдёт "
                           "оригинал.</i>")
    except Exception as e:  # noqa: BLE001
        log.exception("не смог показать превью")
        await m.answer(f"Не смог показать превью: {texts.esc(str(e))[:200]}\n"
                       "Рассылку всё равно можно отправить.")

    await m.answer(
        f"📣 Вложений: {len(draft.items)}\n\nКому отправляем?",
        reply_markup=_bcast_target_kb(bid))
    return True


def _bcast_target_kb(bid: int) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text="👥 Всем агентам",
                              callback_data=f"bc:{bid}:all")],
        [InlineKeyboardButton(text="🔥 Активным за 30 дней",
                              callback_data=f"bc:{bid}:active")],
        [InlineKeyboardButton(text="💬 В рабочие чаты",
                              callback_data=f"bc:{bid}:chats")],
        [InlineKeyboardButton(text="🏢 Отдельному агентству",
                              callback_data=f"bc:{bid}:agency")],
        [InlineKeyboardButton(text="❌ Отмена",
                              callback_data=f"bc:{bid}:cancel")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


@dp.callback_query(F.data.startswith("bc:"))
async def cb_broadcast(c: CallbackQuery) -> None:
    _, bid_s, action = c.data.split(":", 2)
    bid = int(bid_s)
    b = db.get_broadcast(bid)
    if not b or not has_menu(c.from_user.id):
        await c.answer("Недоступно", show_alert=True)
        return

    if action == "cancel":
        db.update_broadcast(bid, status="cancelled")
        await c.message.edit_text("Рассылка отменена.")
        await c.answer()
        return

    if action == "agency":
        rows = [[InlineKeyboardButton(
            text=a["name"], callback_data=f"bc:{bid}:ag{a['id']}")]
            for a in db.list_agencies()[:20]]
        if not rows:
            await c.answer("Справочник агентств пуст", show_alert=True)
            return
        await c.message.edit_reply_markup(
            reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
        await c.answer()
        return

    target: dict = {}
    if action == "active":
        target["active_days"] = 30
    elif action == "chats":
        target["chats"] = True
    elif action.startswith("ag"):
        target["agency_id"] = int(action[2:])

    db.update_broadcast(bid, target=target)
    await _show_bcast_confirm(c.message, bid, target)
    await c.answer()


async def _show_bcast_confirm(message, bid: int, target: dict) -> None:
    if target.get("chats"):
        n = len(db.broadcast_chats(target))
        who = f"рабочих чатов: {n}"
    else:
        n = len(db.broadcast_recipients(target))
        who = f"агентов: {n}"

    if not n:
        await message.edit_text(
            f"Отправлять некому — {who}.\n\n"
            "Напомню: писать в личку бот может только тем, кто нажал "
            "«Отслеживать статус» после своей фиксации.")
        db.update_broadcast(bid, status="cancelled")
        return

    await message.edit_text(
        f"Получателей — {who}.\n\nОтправляем?",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🚀 Отправить",
                                  callback_data=f"bcgo:{bid}")],
            [InlineKeyboardButton(text="❌ Отмена",
                                  callback_data=f"bc:{bid}:cancel")],
        ]))


@dp.callback_query(F.data.startswith("bcgo:"))
async def cb_broadcast_go(c: CallbackQuery) -> None:
    bid = int(c.data.split(":")[1])
    b = db.get_broadcast(bid)
    if not b or b["status"] != "draft":
        await c.answer("Уже отправлено", show_alert=True)
        return
    if not has_menu(c.from_user.id):
        await c.answer("Недоступно", show_alert=True)
        return

    target = b["target"] or {}
    draft = bc.Draft(src_chat_id=b["src_chat_id"],
                     message_ids=b["message_ids"] or [],
                     items=b["items"] or [], html=b["html"] or "")

    if target.get("chats"):
        # В группе язык общий, поэтому берём тот, что закреплён за чатом.
        targets = [(chat_id, chat_lang(chat_id))
                   for chat_id, _ in db.broadcast_chats(target)]
    else:
        targets = [(a["telegram_id"], agent_lang(a))
                   for a in db.broadcast_recipients(target)]

    await c.message.edit_text(f"Отправляю… получателей: {len(targets)}")
    db.update_broadcast(bid, status="sent")

    async def on_fail(chat_id: int, err: Exception) -> None:
        # Заблокировал бота — больше не тревожим.
        if "blocked" in str(err).lower() or "bot was kicked" in str(err).lower():
            db.set_agent_field(chat_id, dm_open=0)

    sent, failed = await bc.deliver(bot, targets, draft, b["html_en"],
                                    on_fail=on_fail)
    db.update_broadcast(bid, sent=sent, failed=failed)
    await c.message.edit_text(
        f"✅ <b>Рассылка завершена</b>\n"
        f"Доставлено: {sent}\nНе доставлено: {failed}")


# ==========================================================================
# Подключение к новой группе
# ==========================================================================

@dp.my_chat_member()
async def on_added_to_chat(update: ChatMemberUpdated) -> None:
    """
    Бота добавили в группу или выдали права администратора.

    Сразу предлагаем закрепить чат за агентством: догадку берём из
    названия группы. После закрепления бот перестаёт спрашивать
    агентство у участников — подставляет сам.
    """
    new = update.new_chat_member
    if update.chat.type not in ("group", "supergroup"):
        return
    if new.status not in ("member", "administrator"):
        return
    if db.get_meta(f"chat_agency:{update.chat.id}"):
        return          # уже настроен

    title = update.chat.title or ""
    # Настройку делает администратор застройщика, а не агент, поэтому
    # язык берём из настроек, а не угадываем по названию чата.
    lang = i18n.normalize_lang(
        db.get_meta(f"chat_lang:{update.chat.id}") or cfg.default_lang)

    known = [{"name": a["name"], "norm": a["norm_name"], "agency_id": a["id"]}
             for a in db.list_agencies()]
    guess = ag.agency_from_chat_title(title, cfg.developer_name, known)

    rows: list[list[InlineKeyboardButton]] = []
    if guess:
        db.set_meta(f"chat_guess:{update.chat.id}", guess)
        rows.append([InlineKeyboardButton(
            text=texts.t(lang, "setup_btn_yes", name=guess[:24]),
            callback_data="setup_yes")])
    rows.append([InlineKeyboardButton(
        text=texts.t(lang, "setup_btn_pick"), callback_data="setup_pick")])

    try:
        await bot.send_message(
            update.chat.id, texts.setup_group(guess, lang),
            reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
    except Exception:  # noqa: BLE001
        log.exception("не смог поздороваться в новой группе")


def _setup_lang(chat_id: int) -> str:
    """Язык настройки — язык администратора, а не названия чата."""
    return i18n.normalize_lang(
        db.get_meta(f"chat_lang:{chat_id}") or cfg.default_lang)


async def _bind_chat(chat_id: int, agency_id: int, name: str,
                     lang: str) -> str:
    db.set_meta(f"chat_agency:{chat_id}", str(agency_id))
    db.set_meta(f"chat_guess:{chat_id}", "")
    return texts.setup_done(name, lang)


@dp.callback_query(F.data == "setup_yes")
async def cb_setup_yes(c: CallbackQuery) -> None:
    if not await _is_chat_admin(c):
        return
    chat_id = c.message.chat.id
    guess = db.get_meta(f"chat_guess:{chat_id}")
    lang = _setup_lang(chat_id)
    if not guess:
        await c.answer("Догадка потерялась, выберите вручную", show_alert=True)
        return

    aid, display, _ = await resolve_agency(guess, None, None)
    if aid is None:
        norm = ag.norm_agency(guess)
        display = ag.pretty_name(guess)
        aid = db.create_agency(display, norm)
        db.add_agency_alias(aid, norm)

    await c.message.edit_text(await _bind_chat(chat_id, aid, display, lang))
    await c.answer()


@dp.callback_query(F.data == "setup_pick")
async def cb_setup_pick(c: CallbackQuery) -> None:
    if not await _is_chat_admin(c):
        return
    chat_id = c.message.chat.id
    lang = _setup_lang(chat_id)
    rows = [[InlineKeyboardButton(text=a["name"],
                                  callback_data=f"setup_set:{a['id']}")]
            for a in db.list_agencies()[:20]]
    if not rows:
        await c.answer(
            "Справочник пуст. Привяжите командой /bind Название",
            show_alert=True)
        return
    await c.message.edit_text(
        texts.need_agency(True, lang=lang),
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
    await c.answer()


@dp.callback_query(F.data.startswith("setup_set:"))
async def cb_setup_set(c: CallbackQuery) -> None:
    if not await _is_chat_admin(c):
        return
    agency = db.get_agency(int(c.data.split(":")[1]))
    if not agency:
        await c.answer("Агентство не найдено", show_alert=True)
        return
    chat_id = c.message.chat.id
    lang = _setup_lang(chat_id)
    await c.message.edit_text(
        await _bind_chat(chat_id, agency["id"], agency["name"], lang))
    await c.answer()


async def _is_chat_admin(c: CallbackQuery) -> bool:
    """Настраивать группу может только её администратор."""
    if not c.from_user or not c.message:
        return False
    if cfg.admin_ids and c.from_user.id in cfg.admin_ids:
        return True
    try:
        member = await bot.get_chat_member(c.message.chat.id, c.from_user.id)
        if member.status in ("creator", "administrator"):
            return True
    except Exception:  # noqa: BLE001
        log.exception("не смог проверить права")
    await c.answer(texts.t(_setup_lang(c.message.chat.id),
                          "setup_not_admin"), show_alert=True)
    return False


# ==========================================================================

# Ловит всё прочее в личке — поэтому регистрируется последним, после
# всех команд. Иначе перехватил бы /my, /notify, /broadcast и остальные.
# ==========================================================================
# Обслуживание клиентов
#
# Живёт только в боте оператора. Клиентские боты этого кода не касаются:
# у них нет ни папки клиентов, ни таблиц биллинга.
# ==========================================================================

BILLING_CHECK_MIN = 60


def is_operator_bot() -> bool:
    """
    Этот бот — операторский? Признак — заданная папка клиентов.

    У клиентских ботов её нет, и ежедневная проверка у них не запускается:
    иначе каждый бот принялся бы считать чужие деньги.
    """
    return bool(cfg.clients_dir)


async def send_as_client_bot(slug: str, folder, text: str,
                             photo: str | None = None) -> None:
    """
    Пишет владельцу от имени **его** бота.

    Не от операторского: застройщик знает своего бота, и сообщение про
    деньги от постороннего выглядит как мошенничество. Токен берём
    из настроек клиента — этих ботов оператор сам и создавал.
    """
    env = cl.client_env(folder, ("TELEGRAM_TOKEN",))
    token = env.get("TELEGRAM_TOKEN", "")
    chat_id = cl.owner_of(folder)
    if not token or not chat_id:
        raise RuntimeError(f"{slug}: не знаю токен бота или владельца")

    api = f"https://api.telegram.org/bot{token}"
    async with httpx.AsyncClient(timeout=30) as http:
        if photo:
            r = await http.post(f"{api}/sendPhoto", data={
                "chat_id": chat_id, "caption": text, "parse_mode": "HTML"})
        else:
            r = await http.post(f"{api}/sendMessage", data={
                "chat_id": chat_id, "text": text, "parse_mode": "HTML",
                "disable_web_page_preview": "true"})
    if r.status_code >= 400:
        raise RuntimeError(f"{slug}: Telegram ответил {r.status_code}")


async def notify_operator_billing(slug: str, text: str, kind: str,
                                  extra: dict) -> None:
    """Сообщение оператору — с кнопкой, если по нему нужно решение."""
    kb = None
    if kind in ("prepare", "nudge", "warn"):
        kb = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="Открыть",
                                 callback_data=f"bl:open:{slug}")]])
    for admin_id in cfg.operator_ids:
        try:
            await bot.send_message(admin_id, text, reply_markup=kb)
        except Exception as e:  # noqa: BLE001
            log.warning("Не смог написать оператору %s: %s", admin_id, e)


async def billing_loop() -> None:
    """
    Раз в час смотрим, не пора ли что-то сделать по обслуживанию.

    Раз в час, а не раз в сутки: сервер могли перезагрузить ровно в тот
    момент, когда должна была пройти суточная проверка. Внутри всё равно
    считается по календарным дням, поэтому лишние заходы ничего не делают.
    """
    await asyncio.sleep(30)
    while True:
        try:
            done = await blrun.run_once(
                db=db, clients_dir=cfg.clients_dir,
                today=dt.date.today(),
                to_operator=notify_operator_billing,
                to_owner=send_as_client_bot)
            if done:
                log.info("Обслуживание: %s", done)
        except Exception:  # noqa: BLE001
            log.exception("Обслуживание: проход не удался")
        await asyncio.sleep(BILLING_CHECK_MIN * 60)


def _billing_period(row):
    """Открытый период клиента: (строка периода, начало, срок)."""
    start = dt.date.fromisoformat(row["start_date"])
    closed = (dt.date.fromisoformat(row["closed_due"])
              if row["closed_due"] else None)
    begin, due = bl.open_period(start, closed)
    period = db.period(row["slug"], due.isoformat(), begin.isoformat())
    return period, begin, due


def _billing_items():
    out = []
    for row in db.all_billing():
        period, _, due = _billing_period(row)
        out.append((row, period, due))
    return out


def _midnight(d: dt.date) -> int:
    return int(time.mktime(d.timetuple()))


@dp.callback_query(F.data.startswith("bl:"))
async def cb_billing(c: CallbackQuery) -> None:
    """Кнопки раздела «Оплаты». Только оператор — это его деньги."""
    if not c.from_user or role_of(c.from_user.id) != mn.OPERATOR:
        await c.answer("Недоступно", show_alert=True)
        return

    _, action, slug = (c.data.split(":", 2) + ["", ""])[:3]

    if action == "setup":
        # Обслуживания ещё нет — заводим, поэтому строки в базе и не должно
        # быть. Всё остальное ниже работает с уже заведённым.
        db.set_meta(f"await_start:{c.from_user.id}", slug)
        await c.message.answer(blui.ASK_START,
                               reply_markup=ForceReply(selective=True))
        await c.answer()
        return

    row = db.get_billing(slug)
    if row is None:
        await c.answer("Обслуживание не заведено", show_alert=True)
        return

    folder = Path(cfg.clients_dir).expanduser() / slug
    period, begin, due = _billing_period(row)

    async def card(note: str = "") -> None:
        fresh = db.get_billing(slug)
        p2, b2, d2 = _billing_period(fresh)
        n = cl.billable_fixations(folder, _midnight(b2), _midnight(d2))
        text = blui.client_text(fresh, p2, b2, d2, n)
        if note:
            text += f"\n\n{note}"
        try:
            await c.message.edit_text(
                text, reply_markup=mn.back_kb(blui.client_kb(fresh, p2)))
        except Exception:  # noqa: BLE001
            await c.message.answer(
                text, reply_markup=mn.back_kb(blui.client_kb(fresh, p2)))

    if action == "open":
        await card()

    elif action == "send":
        if not row["wallet"]:
            await c.answer("Сначала задайте реквизиты", show_alert=True)
            return
        n = cl.billable_fixations(folder, _midnight(begin), _midnight(due))
        amount = bl.Plan(threshold=row["threshold"], low=row["low"],
                         high=row["high"], currency=row["currency"]).amount(n)
        try:
            await send_as_client_bot(slug, folder, texts.invoice(
                begin=begin, due=due, fixations=n, amount=amount,
                currency=row["currency"], wallet=row["wallet"],
                wallet_note=row["wallet_note"] or ""))
            if row["wallet_qr"]:
                await send_qr_as_client_bot(slug, folder, row["wallet_qr"])
        except Exception as e:  # noqa: BLE001
            log.exception("Счёт %s не ушёл", slug)
            await c.answer(f"Не отправилось: {str(e)[:120]}", show_alert=True)
            return
        db.mark_period(slug, due.isoformat(), invoice_sent=1,
                       fixations=n, amount=amount)
        await card("📨 Счёт отправлен.")

    elif action == "paid":
        db.close_period(slug, due.isoformat(), paid_at=int(time.time()))
        cl.set_paused(folder, False)
        await card("✅ Период закрыт, приостановка снята.")

    elif action == "resume":
        db.set_paused(slug, False)
        cl.set_paused(folder, False)
        await card("▶️ Приостановка снята.")

    elif action == "list":
        rows = cl.fixation_rows(folder, _midnight(begin), _midnight(due))
        await c.message.answer(
            blui.fixations_text(rows, begin, due),
            reply_markup=mn.back_kb([[InlineKeyboardButton(
                text="← Клиент", callback_data=f"bl:open:{slug}")]]))

    elif action == "wallet":
        db.set_meta(f"await_wallet:{c.from_user.id}", slug)
        await c.message.answer(blui.ASK_WALLET,
                               reply_markup=ForceReply(selective=True))

    await c.answer()


async def send_qr_as_client_bot(slug: str, folder, file_id: str) -> None:
    """
    Пересылает картинку с QR ботом клиента.

    Картинка лежит в Telegram под file_id, выданным операторскому боту,
    а чужой бот по нему её не заберёт. Поэтому скачиваем и отправляем
    заново — иначе клиент получил бы счёт без QR и молча.
    """
    env = cl.client_env(folder, ("TELEGRAM_TOKEN",))
    token, chat_id = env.get("TELEGRAM_TOKEN", ""), cl.owner_of(folder)
    if not token or not chat_id:
        return
    buf = await bot.download(file_id)
    data = buf.read() if hasattr(buf, "read") else buf
    async with httpx.AsyncClient(timeout=60) as http:
        await http.post(
            f"https://api.telegram.org/bot{token}/sendPhoto",
            data={"chat_id": chat_id, "caption": "QR для оплаты"},
            files={"photo": ("qr.jpg", data, "image/jpeg")})


async def try_wallet_reply(m: Message) -> bool:
    """Оператор прислал реквизиты или QR в ответ на просьбу."""
    if not m.from_user:
        return False
    slug = db.get_meta(f"await_wallet:{m.from_user.id}")
    if not slug or not db.get_billing(slug):
        return False

    if m.photo:
        db.set_wallet(slug, db.get_billing(slug)["wallet"] or "",
                      db.get_billing(slug)["wallet_note"] or "",
                      qr=m.photo[-1].file_id)
        await m.reply("✅ QR сохранён.")
        return True

    wallet, note = blui.parse_wallet(m.text or "")
    if not wallet:
        return False
    db.set_wallet(slug, wallet, note)
    db.set_meta(f"await_wallet:{m.from_user.id}", "")
    await m.reply(
        f"✅ Реквизиты сохранены для <b>{texts.esc(slug)}</b>.\n\n"
        f"<code>{texts.esc(wallet)}</code>"
        + (f"\n{texts.esc(note)}" if note else "")
        + "\n\nМожно прислать QR картинкой — уйдёт вместе со счётом.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="← Клиент",
                                 callback_data=f"bl:open:{slug}")]]))
    return True


async def try_billing_start_reply(m: Message) -> bool:
    """Оператор прислал дату начала обслуживания."""
    if not m.from_user or not (m.text or "").strip():
        return False
    slug = db.get_meta(f"await_start:{m.from_user.id}")
    if not slug:
        return False

    try:
        start = dt.date.fromisoformat((m.text or "").strip())
    except ValueError:
        await m.reply("Не разобрал дату. Нужен вид <code>2026-10-05</code>.")
        return True

    db.set_billing(slug, start_date=start.isoformat())
    db.set_meta(f"await_start:{m.from_user.id}", "")
    _, due = bl.open_period(start)
    await m.reply(
        f"✅ Обслуживание <b>{texts.esc(slug)}</b> с "
        f"{texts.human_date(start)}.\n"
        f"Первый счёт — к {texts.human_date(due)}.\n\n"
        f"Условия по умолчанию: до 100 фиксаций $40, от 100 — $70. "
        f"Осталось задать реквизиты.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="💳 Задать реквизиты",
                                 callback_data=f"bl:wallet:{slug}")]]))
    return True


def _chat_rows() -> list[dict]:
    """Группы для меню: название, агентство, язык, признак админа."""
    names = {a["id"]: a["name"] for a in db.list_agencies()}
    out = []
    for r in db.list_chats():
        aid = db.chat_agency_id(r["chat_id"])
        out.append({
            "chat_id": r["chat_id"],
            "title": r["title"],
            "agency": names.get(aid) if aid else None,
            "lang": db.get_meta(f"chat_lang:{r['chat_id']}") or "",
            "messages": r["messages"],
            "is_admin": None if r["is_admin"] is None else bool(r["is_admin"]),
        })
    return out


@dp.callback_query(F.data.startswith("ch:"))
async def cb_chats(c: CallbackQuery) -> None:
    """Настройка группы: язык и агентство."""
    if not c.from_user or not has_menu(c.from_user.id):
        await c.answer("Недоступно", show_alert=True)
        return

    parts = c.data.split(":")
    action, chat_id = parts[1], int(parts[2])
    row = next((r for r in _chat_rows() if r["chat_id"] == chat_id), None)
    if row is None:
        await c.answer("Группа не найдена", show_alert=True)
        return

    if action == "lang":
        code = parts[3]
        # «auto» — снять привязку, а не записать пустую строку языком:
        # дальше её читает i18n, и пустое значение он понимает как «сам».
        db.set_meta(f"chat_lang:{chat_id}", "" if code == "auto" else code)
        row["lang"] = "" if code == "auto" else code

    if action == "agency":
        agencies = db.list_agencies()
        if not agencies:
            await c.answer("Справочник агентств пуст", show_alert=True)
            return
        kb = [[InlineKeyboardButton(
            text=a["name"], callback_data=f"chset:{chat_id}:{a['id']}")]
            for a in agencies[:30]]
        kb.append([InlineKeyboardButton(text="← Назад",
                                        callback_data=f"ch:open:{chat_id}")])
        await c.message.edit_text(
            f"🏢 За каким агентством закрепить "
            f"<b>{texts.esc(row['title'] or chat_id)}</b>?",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=kb))
        await c.answer()
        return

    await c.message.edit_text(
        mn.chat_card(row),
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=mn.chat_kb(chat_id, row["lang"])))
    await c.answer()


@dp.callback_query(F.data.startswith("chset:"))
async def cb_chat_set_agency(c: CallbackQuery) -> None:
    if not c.from_user or not has_menu(c.from_user.id):
        await c.answer("Недоступно", show_alert=True)
        return
    _, chat_id, agency_id = c.data.split(":")
    db.set_meta(f"chat_agency:{chat_id}", agency_id)

    row = next((r for r in _chat_rows() if r["chat_id"] == int(chat_id)), None)
    if row:
        await c.message.edit_text(
            mn.chat_card(row),
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=mn.chat_kb(int(chat_id), row["lang"])))
    await c.answer("Закреплено")


# --------------------------------------------------------------------------
# Новые группы
#
# Бот не может спросить у Telegram список чатов, где он состоит: он узнаёт
# о группе только когда оттуда придёт сообщение. Поэтому о каждой впервые
# замеченной группе говорим владельцу — один раз и в личку.
#
# В саму группу бот не пишет. Там работают агенты, и объявления о
# собственной настройке им ни к чему.
# --------------------------------------------------------------------------

#: Очередь, а не прямая отправка: при переезде бота, который уже сидит
#: в семидесяти чатах, все они «откроются» почти одновременно, и Telegram
#: оборвёт поток сообщений в одну личку.
_new_chats: "asyncio.Queue[tuple[int, str | None]]" = asyncio.Queue()

CHAT_ALERT_PAUSE = 1.5


def _announce_chat(chat_id: int, title: str | None) -> None:
    if db.chat_agency_id(chat_id):
        return              # уже закреплена — говорить не о чем
    _new_chats.put_nowait((chat_id, title))


def _who_to_tell() -> list[int]:
    """Владельцы и оператор — это их группы и их справочник агентств."""
    return sorted(set(cfg.owner_ids) | set(cfg.operator_ids)
                  | set(cfg.admin_ids))


async def chat_alert_loop() -> None:
    while True:
        chat_id, title = await _new_chats.get()
        try:
            known = [{"name": a["name"], "norm": a["norm_name"],
                      "agency_id": a["id"]} for a in db.list_agencies()]
            guess = ag.agency_from_chat_title(
                title or "", cfg.developer_name, known)
            kb = InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="⚙️ Настроить",
                                     callback_data=f"ch:open:{chat_id}")]])
            for uid in _who_to_tell():
                try:
                    await bot.send_message(
                        uid, mn.new_chat_alert(title, chat_id, guess),
                        reply_markup=kb)
                except Exception as e:  # noqa: BLE001
                    log.debug("Не смог сказать %s о группе %s: %s",
                              uid, chat_id, e)
        except Exception:  # noqa: BLE001
            log.exception("Не смог сообщить о новой группе %s", chat_id)
        await asyncio.sleep(CHAT_ALERT_PAUSE)


dp.message.register(on_private_any, F.chat.type == "private")


async def main() -> None:
    phones.set_default_region(cfg.default_region)
    texts.SHOW_LINKS = cfg.show_crm_links
    if not db.get_meta("contacts_synced_at"):
        try:
            await sync_all()
        except Exception:  # noqa: BLE001
            log.exception("Первая синхронизация не удалась")
    if not db.is_configured():
        log.warning("Воронки не размечены — выполните /pipelines в чате с ботом")
    try:
        me = await bot.get_me()
        global BOT_USERNAME
        BOT_USERNAME = me.username or ""
    except Exception:  # noqa: BLE001
        log.exception("не смог узнать имя бота — кнопка подписки не появится")

    asyncio.create_task(chat_alert_loop())
    asyncio.create_task(sync_loop())
    asyncio.create_task(expire_loop())
    asyncio.create_task(status_loop())
    if is_operator_bot():
        asyncio.create_task(billing_loop())
    # Снимаем вебхук, если он остался от прежней интеграции. Пока он
    # висит, Telegram не отдаёт сообщения опросом — бот молчит и не может
    # объяснить почему. Так и вышло, когда бота BREIG отцепили от прежнего
    # сервиса: вебхук остался, и «/admin» уходил в пустоту.
    #
    # drop_pending_updates обязателен: у бота, который годами сидит
    # в семидесяти чатах, накопленная очередь хлынет разом, и он начнёт
    # отвечать на вчерашние сообщения живым агентам.
    try:
        await bot.delete_webhook(drop_pending_updates=True)
        log.info("Вебхук снят, старые сообщения отброшены")
    except Exception:  # noqa: BLE001
        log.exception("Не смог снять вебхук — если бот молчит, дело в нём")

    try:
        await dp.start_polling(bot)
    finally:
        await amo.close()
        await clf.close()


if __name__ == "__main__":
    asyncio.run(main())

