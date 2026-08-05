"""
Веб-часть: кнопка «Разрешить» для подключения amoCRM.

Поднимается только у бота-оператора и только когда задан WEB_PORT. Клиентским
ботам она не нужна: у них amoCRM уже подключена.

## Зачем это вообще

Сейчас на последнем шаге подключения клиент идёт в настройки amoCRM, создаёт
интеграцию, выпускает долгосрочный токен и присылает его строкой. Это самое
узкое место: нужен администратор аккаунта, а длинную строку легко обрезать
при копировании. С кнопкой остаётся два клика.

## Как проходит обмен

    /connect/<код>          страница с кнопкой amoCRM
        ↓  клиент жмёт «Разрешить»
    /amo/callback           amoCRM возвращает code, referer, state
        ↓  code меняем на токены, пока он не протух (20 минут)
    заявка оператору        с кнопкой «Развернуть», как и раньше

`state` — это код приглашения: он неугадываемый и одноразовый, поэтому
годится и как защита от подделанного возврата. Чужой `state` не совпадёт
ни с одним незакрытым подключением, и обмен не состоится.

## Чего здесь нет

Публикации интеграции. Кнопка ставит интеграцию в чужой аккаунт только
если она **публичная**, а публичные проходят модерацию amoCRM. Пока
интеграция приватная, вся эта цепочка работает лишь с аккаунтом, где она
создана, — то есть годится для проверки, но не для клиентов.
"""

from __future__ import annotations

import logging
import re
from typing import Any, Awaitable, Callable

from aiohttp import web

log = logging.getLogger(__name__)

#: Поддомен amoCRM: буквы, цифры, дефис. Приходит снаружи — проверяем.
SUBDOMAIN_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,40}$")

PAGE = """<!doctype html>
<html lang="ru"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Подключение amoCRM</title>
<style>
 body {{ font: 16px/1.5 -apple-system, system-ui, sans-serif; color: #1a1a1a;
        max-width: 30rem; margin: 4rem auto; padding: 0 1.25rem; }}
 h1 {{ font-size: 1.4rem; margin-bottom: .5rem; }}
 p {{ color: #444; }}
 .note {{ color: #777; font-size: .875rem; margin-top: 2rem; }}
</style>
</head><body>
<h1>Подключение amoCRM</h1>
<p>Нажмите кнопку и подтвердите доступ. Мы попросим только сделки,
контакты и компании — удалять что-либо бот не умеет.</p>
<div id="btn"></div>
<script class="amocrm_oauth"
        charset="utf-8"
        data-client-id="{client_id}"
        data-title="{title}"
        data-compact="false"
        data-class-name="amo-button"
        data-color="default"
        data-state="{state}"
        data-mode="popup"
        src="https://www.amocrm.ru/auth/button.min.js"></script>
<p class="note">После подтверждения можно закрыть страницу и вернуться
в Telegram.</p>
</body></html>"""

DONE = """<!doctype html>
<html lang="ru"><head><meta charset="utf-8">
<title>Готово</title>
<style>body{{font:16px/1.5 -apple-system,system-ui,sans-serif;max-width:30rem;
margin:4rem auto;padding:0 1.25rem}}</style></head>
<body><h1>{head}</h1><p>{text}</p></body></html>"""


def _page_done(head: str, text: str) -> web.Response:
    return web.Response(text=DONE.format(head=head, text=text),
                        content_type="text/html")


def make_app(
    *,
    client_id: str,
    title: str,
    on_connected: Callable[[str, str, dict], Awaitable[None]],
    exchange: Callable[[str, str], Awaitable[dict]],
    is_valid_state: Callable[[str], bool],
) -> web.Application:
    """
    Собирает веб-приложение.

    Наружу вынесено всё, что зависит от бота, — так эту часть можно
    проверить тестами, не поднимая ни Telegram, ни amoCRM:

    * `exchange(code, subdomain)` — обменять код на токены;
    * `on_connected(state, subdomain, tokens)` — что делать после успеха;
    * `is_valid_state(state)` — знаем ли мы такое подключение.
    """

    async def start(request: web.Request) -> web.Response:
        state = request.match_info["state"]
        if not is_valid_state(state):
            return _page_done(
                "Ссылка не действует",
                "Похоже, подключение уже завершено или ссылка устарела. "
                "Попросите новую у того, кто её прислал.")
        return web.Response(
            text=PAGE.format(client_id=client_id, title=title, state=state),
            content_type="text/html")

    async def callback(request: web.Request) -> web.Response:
        q = request.query
        code = q.get("code", "")
        state = q.get("state", "")
        referer = (q.get("referer") or "").strip().lower()
        subdomain = referer.split(".")[0] if referer else ""

        if q.get("error"):
            log.warning("amoCRM вернула ошибку: %s", q.get("error"))
            return _page_done("Доступ не выдан",
                              "Вы отказались или закрыли окно. "
                              "Можно попробовать ещё раз.")

        if not code or not is_valid_state(state):
            return _page_done("Что-то не сходится",
                              "Ссылка устарела или уже использована.")

        if not SUBDOMAIN_RE.match(subdomain):
            log.warning("Непонятный поддомен в возврате: %r", referer)
            return _page_done("Не удалось определить аккаунт",
                              "Попробуйте ещё раз или напишите нам.")

        try:
            tokens = await exchange(code, subdomain)
        except Exception as e:  # noqa: BLE001
            log.exception("Обмен кода не удался")
            return _page_done(
                "Не получилось подключить",
                f"amoCRM ответила отказом. Попробуйте ещё раз. ({str(e)[:120]})")

        try:
            await on_connected(state, subdomain, tokens)
        except Exception:  # noqa: BLE001
            # Токены уже получены — терять их из-за сбоя на нашей стороне
            # нельзя, иначе клиент пойдёт по кругу.
            log.exception("Не смог сохранить подключение")
            return _page_done("Почти готово",
                              "Доступ выдан, но мы не смогли его записать. "
                              "Напишите нам, разберёмся без вашего участия.")

        return _page_done("Готово",
                          "amoCRM подключена. Возвращайтесь в Telegram.")

    async def health(_: web.Request) -> web.Response:
        return web.Response(text="ok")

    app = web.Application()
    # Пути намеренно разведены: будь страница на /amo/{state}, возврат
    # /amo/callback совпал бы с ней как со «state=callback».
    app.add_routes([
        web.get("/connect/{state}", start),
        web.get("/amo/callback", callback),
        web.get("/healthz", health),
    ])
    return app


async def run(app: web.Application, port: int) -> web.AppRunner:
    """Поднимает сервер на localhost: наружу его выставляет Caddy с HTTPS."""
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "127.0.0.1", port).start()
    log.info("Веб-часть слушает 127.0.0.1:%s", port)
    return runner
