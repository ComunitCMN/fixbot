"""
Тексты ответов бота на русском и английском.

Вынесены отдельно от логики: править формулировки можно, не трогая код,
и они же используются в тестах — чтобы проверить, что для каждого
вердикта есть свой ответ на обоих языках.

Строки лежат в таблице STR, а не размазаны по функциям: так сразу видно,
что перевод не забыт, и тест может это проверить механически.

Тон: бот сообщает факты и не выносит приговоров. Он не знает
договорённостей между агентством и застройщиком, поэтому вместо
«работать нельзя» отправляет к менеджеру. И не упоминает CRM
застройщика — у сторонних агентств доступа туда нет.
"""

from __future__ import annotations

import datetime as dt
import html

from i18n import EN, RU
from phones import Phone
from verdict import Decision, Verdict

#: Показывать ли в чате ссылки на карточки в CRM.
#: По умолчанию нет: у агентов из сторонних агентств доступа к CRM
#: застройщика всё равно нет, для них это мусор в сообщении.
#: Включается переменной SHOW_CRM_LINKS=1.
SHOW_LINKS = False


def esc(s) -> str:
    return html.escape(str(s)) if s else ""


def when(ts: int | None, lang: str = RU) -> str:
    if not ts:
        return "дата неизвестна" if lang == RU else "date unknown"
    return dt.datetime.fromtimestamp(ts).strftime("%d.%m.%Y")


STR: dict[str, dict[str, str]] = {
    RU: {
        "dry_run": ("\n\n👀 <i>Режим наблюдения: в CRM ничего не записано. "
                    "Выключите DRY_RUN, чтобы фиксации сохранялись.</i>"),

        "masked": ("\n\n⚠️ Номер неполный — не хватает {missing}. "
                   "Проверка прошла по тем цифрам, что есть. "
                   "Пришлите номер целиком, если хотите проверить точно."),
        "last_digit": "последней цифры",
        "last_digits": "{n} последних цифр",
        # Просто сообщаем факт: в такой стране длину номера не проверить.
        # Просить дописывать не надо — фиксация всё равно проходит.
        "could_be_longer": ("\n\nℹ️ В этой стране номера бывают разной длины, "
                            "поэтому проверить полноту не получится."),

        "need_phone": ("⚠️ <b>Не вижу номер телефона</b>\n"
                       "Чтобы проверить клиента, пришлите номер. Скрыть можно "
                       "максимум две последние цифры, например "
                       "<code>+7 999 123-45-**</code>."),
        "need_digits": ("⚠️ <b>Номер слишком короткий</b>\n"
                        "Получено: <code>{raw}</code> — известно {known} цифр "
                        "из {expected}{country}.\n"
                        "Не хватает ещё <b>{need}</b>, чтобы искать совпадения."
                        "\n\nПришлите номер минимум с {min} цифрами (скрытыми "
                        "можно оставить только две последние) — или целиком."),

        "agency_q_known": ("❓ <b>От какого вы агентства?</b>\n"
                           "Выберите кнопкой — бот запомнит, и в следующий раз "
                           "спрашивать не будет."),
        "agency_q_empty": ("❓ <b>От какого вы агентства?</b>\n"
                           "Справочник пока пуст. Нажмите «Добавить агентство» "
                           "и пришлите название — бот запомнит его за вами."),
        "agency_q_similar": ("❓ <b>Уточните агентство</b>\n"
                             "Вы написали «{query}». Похожие в базе:\n{names}\n\n"
                             "Выберите нужное кнопкой ниже."),
        "ask_agency": ("✏️ Пришлите название агентства <b>ответом на это "
                       "сообщение</b>.\nНапример: <code>Дом+</code>."),
        "agency_field": "Название агентства:",
        "agency_ph": "Например: Дом+",
        "agency_saved": ("Запомнил: вы из агентства <b>{name}</b>.\n"
                         "Больше указывать не придётся."),
        "btn_add_agency": "➕ Добавить агентство",
        "btn_other_agency": "🏢 Другое агентство",

        "ask_name": ("✏️ Пришлите имя клиента ответом на это сообщение.\n"
                     "Например: <code>Иванов Пётр</code>."),
        "ask_name_cur": ("✏️ Сейчас записано: <b>{current}</b>.\n"
                         "Пришлите новое имя клиента ответом на это сообщение."),
        "name_field": "Имя клиента:",
        "name_ph": "Например: Иванов Пётр",
        "name_saved": "Имя клиента: <b>{name}</b>.",
        "btn_add_name": "✏️ Добавить имя клиента",
        "btn_edit_name": "✏️ Изменить имя",

        "btn_fix": "✅ Зафиксировать",
        "btn_cancel": "❌ Отмена",
        "btn_back": "← Назад",

        "anonymous": ("⚠️ Не вижу, кто отправил сообщение — оно от имени канала "
                      "или анонимного администратора. Напишите от своего "
                      "аккаунта."),
        "not_configured": ("⚙️ Бот ещё не настроен: не размечены воронки.\n"
                           "Администратору нужно выполнить <code>/pipelines</code> "
                           "и указать, какая воронка розничная, а какая "
                           "агентская."),
        "no_pipeline": ("⚙️ <b>Некуда класть фиксацию</b>\n"
                        "Не размечена ни одна агентская воронка. "
                        "Администратору: <code>/sync</code>, затем "
                        "<code>/pipelines</code> → отметьте воронку как "
                        "🤝 агентскую."),

        "card_title": "📝 <b>Похоже на фиксацию клиента</b>",
        "card_client": "Клиент:    <b>{v}</b>",
        "card_phone": "Телефон:   <code>{v}</code>",
        "card_agency": "Агентство: <b>{v}</b>",
        "card_agent": "Агент:     {v}",
        "card_object": "Объект:    {v}",
        "card_empty": "— не указано",
        "card_no_agency": "❗️ не определено",
        "card_tail": ("<i>Проверьте данные. Клиент будет зафиксирован после "
                      "нажатия кнопки.</i>"),
        "card_tail_name": "<i>Имя можно добавить кнопкой ниже.</i>",

        "note_unique": "🟢 Совпадений по базе нет — клиент уникальный.",
        "note_other": ("🟢 Работать можно.\n"
                       "ℹ️ По клиенту уже есть фиксация другого агентства "
                       "от {date}{extra}. Кто первым приведёт под депозит — "
                       "того и клиент."),
        "note_expired": ("🟢 Клиент был в базе отдела продаж, но без активности "
                         "с {date} — срок истёк, можно забирать."),
        "note_agencies": " (агентств: {n})",

        "v_unique": "🟢 <b>Клиент уникальный — зафиксирован</b>",
        "v_other": "🟢 <b>Клиент зафиксирован — работайте</b>",
        "v_other_note": ("ℹ️ По этому клиенту уже есть фиксация другого "
                         "агентства от {date}{extra}, но вы всё равно можете "
                         "с ним работать.\nЗдесь работает принцип: кто первым "
                         "приведёт под депозит."),
        "v_same_agent": ("ℹ️ <b>Вы уже фиксировали этого клиента</b>\n"
                         "<code>{phone}</code> — фиксация от {date}.\n"
                         "Повторно фиксировать не нужно."),
        "v_same_agency": ("ℹ️ <b>Клиента уже зафиксировал ваш коллега</b>{who}\n"
                          "<code>{phone}</code>, фиксация от {date}.\n"
                          "Клиент числится за {agency}."),
        "v_same_agency_fallback": "вашим агентством",
        "v_retail": ("🔴 <b>Клиент не уникальный</b>\n"
                     "<code>{phone}</code> зафиксирован за прямым отделом "
                     "продаж{since}.\n\n"
                     "Уточните у своего менеджера, что делать в этом случае."),
        "v_booked": ("🔴 <b>Клиент не уникальный</b>\n"
                     "<code>{phone}</code> уже доведён до брони другим "
                     "агентством.\n\n"
                     "Уточните у своего менеджера, что делать в этом случае."),
        "v_expired": ("🟢 <b>Клиент освободился — зафиксирован за вами</b>\n"
                      "Он был в базе отдела продаж, но без активности "
                      "с {date}."),
        "v_unknown": ("🟡 <b>Нужна проверка</b>\n"
                      "Клиент <code>{phone}</code> уже есть в базе, но по нему "
                      "нет истории — понять, за кем он закреплён, не "
                      "получилось.\n\nПередал менеджеру, ответ придёт "
                      "в течение дня."),
        "since": " с {date}",

        "confirmed": "✅ <b>Клиент зафиксирован</b>",
        "cancelled": "❌ Отменено. Ничего не записано.",
        "expired": ("⌛️ Подтверждение не получено, заявка снята. "
                    "Если фиксация нужна — отправьте сообщение заново."),
        "not_yours": "Подтвердить может только тот, кто отправил сообщение.",
        "check_only": "\n\n<i>Это только проверка, ничего не создано.</i>",
        "live_failed": ("\n<i>⚠️ Не удалось свериться с базой в реальном "
                        "времени — ответ может быть неактуальным.</i>"),

        "setup_title": "👋 <b>Бот подключён к этой группе</b>",
        "setup_guess": ("Судя по названию чата, это группа агентства "
                        "<b>{name}</b>. Всё верно?"),
        "setup_no_guess": ("Из названия чата понять агентство не получилось.\n"
                           "Выберите из списка или добавьте новое."),
        "setup_tail": ("После привязки бот перестанет спрашивать агентство "
                       "у участников — будет подставлять его сам."),
        "setup_done": ("✅ Чат закреплён за агентством <b>{name}</b>.\n"
                       "Фиксации отсюда будут подписываться им автоматически."),
        "setup_btn_yes": "✅ Да, это {name}",
        "setup_btn_pick": "📋 Выбрать другое",
        "setup_not_admin": "Настроить группу может только администратор.",

        "btn_watch": "🔔 Отслеживать статус",
        "watch_hint": "\n\n<i>Статусы по клиенту придут вам в личные сообщения.</i>",

        "dm_hello": ("👋 Готово, теперь я буду писать вам сюда о движении "
                     "ваших клиентов."),
        # Молчать в ответ на команду нельзя: человек решит, что бот сломался,
        # и будет жать её снова. А ещё это может быть владелец, перепутавший
        # бота, — таких у оператора несколько.
        "no_menu": ("Это служебная команда — у вас к ней доступа нет, "
                    "и ничего страшного: для работы она не нужна.\n\n"
                    "Если вы застройщик и ждёте своё меню — возможно, вы "
                    "написали не тому боту. У каждого застройщика он свой."),
        "dm_fixation": ("Вы отслеживаете:\n"
                        "<b>{client}</b> — <code>{phone}</code>\n"
                        "Агентство: {agency}\nСтатус: {status}"),
        "dm_not_yours": ("Эта фиксация не ваша — отслеживать её может только "
                         "тот, кто её отправил."),
        "dm_ask_phone": ("Оставьте свой номер — он попадёт в вашу карточку, "
                         "и с вами смогут связаться напрямую.\n"
                         "Кнопка ниже пришлёт его в один тап."),
        "btn_share_phone": "📱 Поделиться номером",
        "btn_skip": "Пропустить",
        "dm_phone_saved": "Спасибо, номер записан: <code>{phone}</code>",
        "dm_phone_skipped": "Хорошо, обойдёмся без номера.",

        "dm_use_group": ("Фиксации принимаются в рабочих чатах с застройщиком — "
                         "там я знаю, от какого вы агентства.\n\n"
                         "Здесь можно посмотреть свои: /my"),

        "my_empty": ("У вас пока нет фиксаций.\n"
                     "Отправьте клиента в рабочий чат — он появится здесь."),
        "my_title": "📋 <b>Ваши последние фиксации</b>",
        "my_row": "{n}. <b>{client}</b> — <code>{phone}</code>\n     {status}",
        "my_unknown_status": "статус уточняется",

        "notify_expiring": ("⏳ <b>Фиксация скоро заканчивается</b>\n"
                            "<b>{client}</b> — <code>{phone}</code>\n"
                            "Срок до {date}.\n\n"
                            "Если продолжаете работать с клиентом — нажмите "
                            "кнопку, и срок начнётся заново."),
        "btn_renew": "🔄 Продолжаю работу",
        "renewed": ("✅ Продлил. <b>{client}</b> закреплён за вами "
                    "до {date}."),
        "renew_note": ("Агент {agent} продолжает работу с клиентом. "
                       "Фиксация продлена до {date}."),

        "notify_rival": ("⚠️ <b>Этого клиента зафиксировало ещё одно "
                         "агентство</b>\n"
                         "<b>{client}</b> — <code>{phone}</code>\n\n"
                         "Эксклюзива нет, но теперь вы не одни: клиент "
                         "достанется тому, кто первым доведёт до депозита."),

        "notify_direct": ("🍀 <b>Вам повезло</b>\n"
                          "<b>{client}</b> — <code>{phone}</code>\n\n"
                          "Клиент сам обратился в прямой отдел продаж, но вы "
                          "зафиксировали его раньше. В работу мы его не "
                          "берём — клиент ваш, работайте."),

        "notify_won": ("🎉 <b>Сделка состоялась, поздравляем!</b>\n"
                       "<b>{client}</b> — <code>{phone}</code>\n\n"
                       "Спасибо за работу."),
        "notify_off": ("Уведомления выключены. Включить обратно: "
                       "<code>/notify on</code>"),
        "notify_on": "Уведомления включены.",

        "bcast_off": ("Вы отписались от рассылок.\n"
                      "Уведомления о ваших клиентах продолжат приходить — "
                      "их выключает отдельная команда <code>/notify off</code>."),
        "bcast_on": "Вы снова получаете рассылки.",
        "bcast_footer": "\n\n<i>Отписаться от рассылок: /stop</i>",
    },
    EN: {
        "dry_run": ("\n\n👀 <i>Observation mode: nothing was saved. "
                    "Turn off DRY_RUN to store fixations.</i>"),

        "masked": ("\n\n⚠️ The number is incomplete — {missing} missing. "
                   "We checked against the digits we have. "
                   "Send the full number for an exact check."),
        "last_digit": "the last digit is",
        "last_digits": "the last {n} digits are",
        "could_be_longer": ("\n\nℹ️ Phone numbers in this country vary in "
                            "length, so we can't tell whether this one is "
                            "complete."),

        "need_phone": ("⚠️ <b>No phone number found</b>\n"
                       "Send the client's number so we can check them. "
                       "You may hide at most the last two digits, "
                       "e.g. <code>+62 812 3456 78**</code>."),
        "need_digits": ("⚠️ <b>The number is too short</b>\n"
                        "Received: <code>{raw}</code> — {known} digits "
                        "of {expected}{country}.\n"
                        "<b>{need}</b> more needed to search for matches.\n\n"
                        "Send at least {min} digits (only the last two may be "
                        "hidden) — or the full number."),

        "agency_q_known": ("❓ <b>Which agency are you from?</b>\n"
                           "Pick one — the bot will remember and won't ask "
                           "again."),
        "agency_q_empty": ("❓ <b>Which agency are you from?</b>\n"
                           "The list is empty. Tap “Add agency” and send the "
                           "name — it will be remembered for you."),
        "agency_q_similar": ("❓ <b>Which agency do you mean?</b>\n"
                             "You wrote “{query}”. Similar ones:\n{names}\n\n"
                             "Pick the right one below."),
        "ask_agency": ("✏️ Send the agency name <b>as a reply to this "
                       "message</b>.\nFor example: <code>Century 21</code>."),
        "agency_field": "Agency name:",
        "agency_ph": "e.g. Century 21",
        "agency_saved": ("Got it: you're from <b>{name}</b>.\n"
                         "You won't need to specify it again."),
        "btn_add_agency": "➕ Add agency",
        "btn_other_agency": "🏢 Another agency",

        "ask_name": ("✏️ Send the client's name as a reply to this message.\n"
                     "For example: <code>John Smith</code>."),
        "ask_name_cur": ("✏️ Currently saved: <b>{current}</b>.\n"
                         "Send a new client name as a reply to this message."),
        "name_field": "Client name:",
        "name_ph": "e.g. John Smith",
        "name_saved": "Client name: <b>{name}</b>.",
        "btn_add_name": "✏️ Add client name",
        "btn_edit_name": "✏️ Edit name",

        "btn_fix": "✅ Register client",
        "btn_cancel": "❌ Cancel",
        "btn_back": "← Back",

        "anonymous": ("⚠️ I can't see who sent this — the message is from a "
                      "channel or an anonymous admin. Please write from your "
                      "own account."),
        "not_configured": ("⚙️ The bot isn't set up yet: sales pipelines "
                           "aren't classified.\nAn admin needs to run "
                           "<code>/pipelines</code> and mark which pipeline is "
                           "direct sales and which is for agencies."),
        "no_pipeline": ("⚙️ <b>Nowhere to store the fixation</b>\n"
                        "No agency pipeline is marked. Admin: run "
                        "<code>/sync</code>, then <code>/pipelines</code> → "
                        "mark a pipeline as 🤝 agency."),

        "card_title": "📝 <b>This looks like a client registration</b>",
        "card_client": "Client:  <b>{v}</b>",
        "card_phone": "Phone:   <code>{v}</code>",
        "card_agency": "Agency:  <b>{v}</b>",
        "card_agent": "Agent:   {v}",
        "card_object": "Project: {v}",
        "card_empty": "— not specified",
        "card_no_agency": "❗️ not identified",
        "card_tail": ("<i>Check the details. The client will be registered "
                      "once you tap the button.</i>"),
        "card_tail_name": "<i>You can add the name with the button below.</i>",

        "note_unique": "🟢 No matches found — the client is new.",
        "note_other": ("🟢 You can work with this client.\n"
                       "ℹ️ Another agency registered them on {date}{extra}. "
                       "Whoever brings them to a deposit first gets the "
                       "client."),
        "note_expired": ("🟢 The client was in the direct sales database but "
                         "has had no activity since {date} — the period has "
                         "expired, they're available."),
        "note_agencies": " ({n} agencies in total)",

        "v_unique": "🟢 <b>Client registered</b>",
        "v_other": "🟢 <b>Client registered — go ahead</b>",
        "v_other_note": ("ℹ️ Another agency registered this client on "
                         "{date}{extra}, but you can still work with them.\n"
                         "Whoever brings them to a deposit first gets the "
                         "client."),
        "v_same_agent": ("ℹ️ <b>You already registered this client</b>\n"
                         "<code>{phone}</code> — registered on {date}.\n"
                         "No need to do it again."),
        "v_same_agency": ("ℹ️ <b>A colleague already registered this "
                          "client</b>{who}\n<code>{phone}</code>, "
                          "registered on {date}.\n"
                          "The client is assigned to {agency}."),
        "v_same_agency_fallback": "your agency",
        "v_retail": ("🔴 <b>Client is not new</b>\n"
                     "<code>{phone}</code> is registered with the direct sales "
                     "department{since}.\n\n"
                     "Please check with your manager how to proceed."),
        "v_booked": ("🔴 <b>Client is not new</b>\n"
                     "<code>{phone}</code> has already been brought to a "
                     "booking by another agency.\n\n"
                     "Please check with your manager how to proceed."),
        "v_expired": ("🟢 <b>Client is available — registered to you</b>\n"
                      "They were in the direct sales database but have had no "
                      "activity since {date}."),
        "v_unknown": ("🟡 <b>Needs checking</b>\n"
                      "Client <code>{phone}</code> is already in the database, "
                      "but there's no history — we couldn't tell who they're "
                      "assigned to.\n\nPassed to a manager, you'll get an "
                      "answer within a day."),
        "since": " since {date}",

        "confirmed": "✅ <b>Client registered</b>",
        "cancelled": "❌ Cancelled. Nothing was saved.",
        "expired": ("⌛️ No confirmation received, the request was dropped. "
                    "Send the message again if you still need it."),
        "not_yours": "Only the person who sent the message can confirm it.",
        "check_only": "\n\n<i>This is only a check, nothing was created.</i>",
        "live_failed": ("\n<i>⚠️ Couldn't verify against the live database — "
                        "the answer may be out of date.</i>"),

        "setup_title": "👋 <b>The bot has been added to this group</b>",
        "setup_guess": ("Judging by the chat name, this is the "
                        "<b>{name}</b> agency group. Is that right?"),
        "setup_no_guess": ("I couldn't work out the agency from the chat "
                           "name.\nPick one from the list or add a new one."),
        "setup_tail": ("Once linked, the bot will stop asking members for "
                       "their agency and fill it in automatically."),
        "setup_done": ("✅ This chat is linked to <b>{name}</b>.\n"
                       "Fixations from here will be assigned to it "
                       "automatically."),
        "setup_btn_yes": "✅ Yes, it's {name}",
        "setup_btn_pick": "📋 Pick another",
        "setup_not_admin": "Only an admin can set up the group.",

        "btn_watch": "🔔 Track status",
        "watch_hint": "\n\n<i>Client updates will be sent to you in a direct message.</i>",

        "dm_hello": ("👋 All set — I'll message you here whenever your "
                     "clients move forward."),
        "no_menu": ("That's a service command — you don't have access to it, "
                    "and you don't need it for your work.\n\n"
                    "If you're a developer waiting for your own menu, you may "
                    "have messaged the wrong bot. Each developer has their own."),
        "dm_fixation": ("You're tracking:\n"
                        "<b>{client}</b> — <code>{phone}</code>\n"
                        "Agency: {agency}\nStatus: {status}"),
        "dm_not_yours": ("This isn't your registration — only the person who "
                         "sent it can track it."),
        "dm_ask_phone": ("Share your phone number — it'll go into your card "
                         "so people can reach you directly.\n"
                         "The button below sends it in one tap."),
        "btn_share_phone": "📱 Share my number",
        "btn_skip": "Skip",
        "dm_phone_saved": "Thanks, saved: <code>{phone}</code>",
        "dm_phone_skipped": "No problem, we'll manage without it.",

        "dm_use_group": ("Client registrations go in the work chats with the "
                         "developer — that's where I know which agency you're "
                         "from.\n\nHere you can review yours: /my"),

        "my_empty": ("You have no registrations yet.\n"
                     "Send a client to a work chat and it'll show up here."),
        "my_title": "📋 <b>Your latest registrations</b>",
        "my_row": "{n}. <b>{client}</b> — <code>{phone}</code>\n     {status}",
        "my_unknown_status": "status pending",

        "notify_expiring": ("⏳ <b>Your registration is about to expire</b>\n"
                            "<b>{client}</b> — <code>{phone}</code>\n"
                            "Valid until {date}.\n\n"
                            "If you're still working with this client, tap "
                            "the button and the term starts over."),
        "btn_renew": "🔄 Still working on it",
        "renewed": ("✅ Renewed. <b>{client}</b> stays with you "
                    "until {date}."),
        "renew_note": ("Agent {agent} is still working with this client. "
                       "Registration extended until {date}."),

        "notify_rival": ("⚠️ <b>Another agency registered this client</b>\n"
                         "<b>{client}</b> — <code>{phone}</code>\n\n"
                         "Nobody has exclusivity, but you're no longer alone: "
                         "the client goes to whoever reaches a deposit "
                         "first."),

        "notify_direct": ("🍀 <b>Lucky you</b>\n"
                          "<b>{client}</b> — <code>{phone}</code>\n\n"
                          "The client approached our direct sales team, but "
                          "you registered them first. We're not taking them "
                          "on — the client is yours, go ahead."),

        "notify_won": ("🎉 <b>The deal went through — congratulations!</b>\n"
                       "<b>{client}</b> — <code>{phone}</code>\n\n"
                       "Thanks for your work."),
        "notify_off": ("Notifications are off. Turn them back on: "
                       "<code>/notify on</code>"),
        "notify_on": "Notifications are on.",

        "bcast_off": ("You've unsubscribed from announcements.\n"
                      "Updates about your own clients will keep coming — "
                      "those are turned off separately with "
                      "<code>/notify off</code>."),
        "bcast_on": "You're receiving announcements again.",
        "bcast_footer": "\n\n<i>Unsubscribe from announcements: /stop</i>",
    },
}


def t(lang: str, key: str, **kw) -> str:
    """Строка на нужном языке. Русский — запасной вариант."""
    table = STR.get(lang) or STR[RU]
    template = table.get(key) or STR[RU][key]
    return template.format(**kw) if kw else template


def dry_run_note(lang: str = RU) -> str:
    return t(lang, "dry_run")


# --------------------------------------------------------------------------
# Номер
# --------------------------------------------------------------------------

def masked_note(p: Phone, lang: str = RU) -> str:
    """
    Приписка о том, что номер неполный.

    Написана нарочито просто: агент не обязан разбираться в терминах
    вроде «маски» или «префикса». Ему важно одно — проверка прошла
    не по всему номеру.
    """
    if p.is_full:
        return t(lang, "could_be_longer") if p.could_be_longer else ""
    n = p.missing
    missing = (t(lang, "last_digit") if n == 1
               else t(lang, "last_digits", n=n))
    return t(lang, "masked", missing=missing)


def need_phone(lang: str = RU) -> str:
    return t(lang, "need_phone")


def need_digits(p: Phone, raw: str | None = None, lang: str = RU) -> str:
    from phones import MAX_MISSING

    return t(lang, "need_digits",
             raw=esc(raw) or p.pretty(), known=p.known, expected=p.expected,
             country=f" ({p.region})" if p.region else "",
             need=p.missing - MAX_MISSING, min=p.min_compare)


# --------------------------------------------------------------------------
# Агентство и имя
# --------------------------------------------------------------------------

def need_agency(known: bool, query: str | None = None,
                candidates: list[str] | None = None, lang: str = RU) -> str:
    """Спрашиваем так, чтобы на вопрос можно было ответить кнопкой."""
    if candidates:
        names = "\n".join(f"• {esc(c)}" for c in candidates)
        return t(lang, "agency_q_similar", query=esc(query), names=names)
    return t(lang, "agency_q_known" if known else "agency_q_empty")


def ask_agency_name(lang: str = RU) -> str:
    return t(lang, "ask_agency")


def agency_saved(name: str, lang: str = RU) -> str:
    return t(lang, "agency_saved", name=esc(name))


def ask_client_name(current: str | None, lang: str = RU) -> str:
    if current:
        return t(lang, "ask_name_cur", current=esc(current))
    return t(lang, "ask_name")


def client_name_saved(name: str, lang: str = RU) -> str:
    return t(lang, "name_saved", name=esc(name))


# --------------------------------------------------------------------------
# Служебные
# --------------------------------------------------------------------------

def anonymous_sender(lang: str = RU) -> str:
    return t(lang, "anonymous")


def not_configured(lang: str = RU) -> str:
    return t(lang, "not_configured")


def no_agency_pipeline(lang: str = RU) -> str:
    return t(lang, "no_pipeline")


def cancelled(lang: str = RU) -> str:
    return t(lang, "cancelled")


def expired_prompt(lang: str = RU) -> str:
    return t(lang, "expired")


def not_your_prompt(lang: str = RU) -> str:
    return t(lang, "not_yours")


# --------------------------------------------------------------------------
# Карточка подтверждения
# --------------------------------------------------------------------------

def _links(contact_url: str | None, lead_url: str | None) -> list[str]:
    if not SHOW_LINKS:
        return []
    out = []
    if contact_url:
        out.append(f'<a href="{contact_url}">contact</a>')
    if lead_url:
        out.append(f'<a href="{lead_url}">deal</a>')
    return out


def confirm_card(*, client: str | None, p: Phone, agency: str | None,
                 agent: str, object_: str | None, verdict_note: str,
                 lang: str = RU) -> str:
    """
    Карточка, которую агент должен подтвердить.

    Смысл — снять с распознавания ответственность за точность. Ошибочное
    срабатывание стоит одного проигнорированного сообщения, а не мусора
    в базе. Поэтому лучше переспросить лишний раз, чем промолчать.
    """
    rows = [
        t(lang, "card_client", v=esc(client) or t(lang, "card_empty")),
        t(lang, "card_phone", v=p.pretty()),
        t(lang, "card_agency", v=esc(agency) or t(lang, "card_no_agency")),
        t(lang, "card_agent", v=esc(agent)),
    ]
    if object_:
        rows.append(t(lang, "card_object", v=esc(object_)))

    out = [t(lang, "card_title"), "", "\n".join(rows)]
    if verdict_note:
        out += ["", verdict_note]
    tail = t(lang, "card_tail")
    if not client:
        tail += "\n" + t(lang, "card_tail_name")
    out += ["", tail]
    return "\n".join(out) + masked_note(p, lang)


def confirm_note_unique(lang: str = RU) -> str:
    return t(lang, "note_unique")


def confirm_note_other_agency(d: Decision, lang: str = RU) -> str:
    extra = (t(lang, "note_agencies", n=d.other_agency_count)
             if d.other_agency_count > 1 else "")
    return t(lang, "note_other", date=when(d.other_agency_since, lang),
             extra=extra)


def confirm_note_retail_expired(d: Decision, ttl_days: int = 365,
                                lang: str = RU) -> str:
    return t(lang, "note_expired", date=when(d.retail_activity, lang))


def confirmed(client: str | None, p: Phone, agency: str | None,
              contact_url: str | None = None, lead_url: str | None = None,
              agent_url: str | None = None, lang: str = RU) -> str:
    out = [t(lang, "confirmed"),
           f"{esc(client) or 'Client'} — <code>{p.pretty()}</code>"]
    if agency:
        out.append(t(lang, "card_agency", v=esc(agency)))
    links = _links(contact_url, lead_url)
    if agent_url and SHOW_LINKS:
        links.append(f'<a href="{agent_url}">agent</a>')
    if links:
        out += [""] + links
    return "\n".join(out).rstrip() + masked_note(p, lang)


# --------------------------------------------------------------------------
# Вердикты
# --------------------------------------------------------------------------

def _client_line(client: str, p: Phone, agency: str | None,
                 lang: str) -> str:
    line = f"{esc(client)} — <code>{p.pretty()}</code>"
    if agency:
        line += "\n" + t(lang, "card_agency", v=esc(agency))
    return line


def render(d: Decision, *, client: str, p: Phone, agency: str | None,
           contact_url: str | None = None, lead_url: str | None = None,
           ttl_days: int = 365, lang: str = RU) -> str:
    v = d.verdict

    if v is Verdict.UNIQUE:
        body = [t(lang, "v_unique"), _client_line(client, p, agency, lang)]
    elif v is Verdict.OTHER_AGENCY:
        extra = (t(lang, "note_agencies", n=d.other_agency_count)
                 if d.other_agency_count > 1 else "")
        body = [t(lang, "v_other"), _client_line(client, p, agency, lang), "",
                t(lang, "v_other_note", date=when(d.other_agency_since, lang),
                  extra=extra)]
    elif v is Verdict.RETAIL_EXPIRED:
        body = [t(lang, "v_expired", date=when(d.retail_activity, lang)),
                _client_line(client, p, agency, lang)]
    elif v is Verdict.SAME_AGENT:
        body = [t(lang, "v_same_agent", phone=p.pretty(),
                  date=when(d.own_since, lang))]
    elif v is Verdict.SAME_AGENCY:
        body = [t(lang, "v_same_agency", phone=p.pretty(),
                  who=f" — {esc(d.colleague)}" if d.colleague else "",
                  date=when(d.own_since, lang),
                  agency=esc(agency) or t(lang, "v_same_agency_fallback"))]
    elif v is Verdict.RETAIL_BLOCKED:
        since = d.retail_since or d.retail_activity
        body = [t(lang, "v_retail", phone=p.pretty(),
                  since=t(lang, "since", date=when(since, lang)) if since else "")]
    elif v is Verdict.BOOKED_ELSEWHERE:
        body = [t(lang, "v_booked", phone=p.pretty())]
    elif v is Verdict.UNKNOWN_ORIGIN:
        body = [t(lang, "v_unknown", phone=p.pretty())]
    else:
        raise ValueError(f"нет текста для вердикта {v}")

    links = _links(contact_url, lead_url)
    if links:
        body += [""] + links
    return "\n".join(body).rstrip() + masked_note(p, lang)


# --------------------------------------------------------------------------
# Настройка группы
# --------------------------------------------------------------------------

def setup_group(guess: str | None, lang: str = RU) -> str:
    head = t(lang, "setup_title")
    mid = (t(lang, "setup_guess", name=esc(guess)) if guess
           else t(lang, "setup_no_guess"))
    return f"{head}\n\n{mid}\n\n{t(lang, 'setup_tail')}"


def setup_done(name: str, lang: str = RU) -> str:
    return t(lang, "setup_done", name=esc(name))


# --------------------------------------------------------------------------
# Уведомления администратору (всегда по-русски: это его язык)
# --------------------------------------------------------------------------

def admin_retail_expired(client: str, p: Phone, agency: str | None,
                         chat: str | None, d: Decision) -> str:
    return ("🔔 <b>Агентство забрало остывшего клиента</b>\n"
            f"{esc(client)} — <code>{p.pretty()}</code>\n"
            f"Агентство: {esc(agency) or '—'}\n"
            f"Чат: {esc(chat) or '—'}\n"
            f"Последняя активность в рознице: {when(d.retail_activity)}\n\n"
            "Если менеджер собирался его реанимировать — самое время "
            "вмешаться.")


def admin_unknown_origin(client: str, p: Phone, agency: str | None,
                         chat: str | None, contact_urls: list[str]) -> str:
    links = "\n".join(f'• <a href="{u}">карточка</a>' for u in contact_urls[:5])
    return ("🔔 <b>Спорная фиксация — нужен разбор</b>\n"
            f"{esc(client)} — <code>{p.pretty()}</code>\n"
            f"Агентство: {esc(agency) or '—'}\n"
            f"Чат: {esc(chat) or '—'}\n\n"
            f"Контакт есть в базе, но сделок нет, поэтому непонятно, "
            f"чей это клиент:\n{links}")


# ===================== обслуживание =====================
#
# Тексты про деньги отделены от всего остального намеренно: их видит
# владелец-застройщик, а не агенты. Агентам про оплату не пишется никогда —
# это отношения между оператором и застройщиком.

_MONTHS_RU = ("января", "февраля", "марта", "апреля", "мая", "июня", "июля",
              "августа", "сентября", "октября", "ноября", "декабря")


def human_date(d, lang: str = RU) -> str:
    if lang == RU:
        return f"{d.day} {_MONTHS_RU[d.month - 1]}"
    return d.strftime("%B %-d") if hasattr(d, "strftime") else str(d)


def period_started(*, begin, due, plan, first: bool = True,
                   lang: str = RU) -> str:
    """
    Начался оплачиваемый месяц.

    Первый раз объясняем условия целиком, дальше — одной строкой: человек
    их уже знает, и повторять каждый месяц значит превратить полезное
    сообщение в шум, который перестанут читать.
    """
    b, d = human_date(begin, lang), human_date(due, lang)
    cur = "$" if plan.currency == "USD" else plan.currency + " "

    if not first:
        if lang == RU:
            return (f"📅 Новый период обслуживания: с {b}.\n"
                    f"Счёт пришлю к {d}.")
        return (f"📅 New service period started on {b}.\n"
                f"I'll send the invoice by {d}.")

    if lang == RU:
        return (
            f"📅 <b>Начался период обслуживания</b>\n\n"
            f"С {b} идёт первый оплачиваемый месяц. Оплата — к {d}, "
            f"за прошедший период.\n\n"
            f"<b>Сколько:</b>\n"
            f"• до {plan.threshold} фиксаций — {cur}{plan.low}\n"
            f"• от {plan.threshold} фиксаций — {cur}{plan.high}\n\n"
            f"Считаются только фиксации, которые попали в вашу CRM — "
            f"то есть те, где агент нажал кнопку и сделка создалась. "
            f"Проверки без подтверждения в счёт не идут.\n\n"
            f"Ближе к сроку пришлю сумму, число фиксаций и реквизиты "
            f"криптокошелька. Ничего делать заранее не нужно.")
    return (
        f"📅 <b>Service period started</b>\n\n"
        f"Your first billable month began on {b}. Payment is due by {d}, "
        f"for the period just ended.\n\n"
        f"<b>Pricing:</b>\n"
        f"• under {plan.threshold} registrations — {cur}{plan.low}\n"
        f"• {plan.threshold} or more — {cur}{plan.high}\n\n"
        f"Only registrations that reached your CRM are counted — the ones "
        f"an agent confirmed and a deal was created for. Lookups without "
        f"confirmation don't count.\n\n"
        f"Closer to the date I'll send the amount, the count and the "
        f"crypto wallet details. Nothing to do in advance.")


def invoice(*, begin, due, fixations: int, amount: int, currency: str,
            wallet: str, wallet_note: str = "", lang: str = RU) -> str:
    """Счёт клиенту. Сумма, за что, куда платить — и ничего лишнего."""
    b, d = human_date(begin, lang), human_date(due, lang)
    cur = "$" if currency == "USD" else currency + " "
    note = f" ({esc(wallet_note)})" if wallet_note else ""

    if lang == RU:
        return (
            f"💳 <b>Счёт за обслуживание</b>\n\n"
            f"Период: {b} — {d}\n"
            f"Фиксаций в CRM: <b>{fixations}</b>\n"
            f"К оплате: <b>{cur}{amount}</b>\n\n"
            f"Кошелёк{note}:\n<code>{esc(wallet)}</code>\n\n"
            f"Оплатить до {d}. Как переведёте — напишите, я отмечу.")
    return (
        f"💳 <b>Service invoice</b>\n\n"
        f"Period: {b} — {d}\n"
        f"Registrations in CRM: <b>{fixations}</b>\n"
        f"Amount due: <b>{cur}{amount}</b>\n\n"
        f"Wallet{note}:\n<code>{esc(wallet)}</code>\n\n"
        f"Please pay by {d}. Drop me a line once you have — I'll mark it.")


def invoice_reminder(*, due, amount: int, currency: str, wallet: str,
                     lang: str = RU) -> str:
    """
    Одно напоминание, без нажима. Клиент мог просто забыть, и разговаривать
    с ним как с должником — плохая идея, отношения дороже.
    """
    d = human_date(due, lang)
    cur = "$" if currency == "USD" else currency + " "
    if lang == RU:
        return (f"🔔 Напоминаю про оплату обслуживания за период до {d} — "
                f"<b>{cur}{amount}</b>.\n\n<code>{esc(wallet)}</code>\n\n"
                f"Если уже оплатили, просто напишите — сверю.")
    return (f"🔔 A reminder about the service payment due {d} — "
            f"<b>{cur}{amount}</b>.\n\n<code>{esc(wallet)}</code>\n\n"
            f"If you've already paid, just say so and I'll check.")


def service_paused(lang: str = RU) -> str:
    """
    Что видит агент, когда бот приостановлен.

    Про деньги — ни слова. Отношения оператора с застройщиком агентов
    не касаются, и узнавать о чужих долгах они не должны.
    """
    if lang == RU:
        return ("⏸ Проверка временно недоступна.\n"
                "Уточните у своего менеджера.")
    return ("⏸ Lookups are temporarily unavailable.\n"
            "Please check with your manager.")
