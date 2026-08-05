"""Кнопка «Разрешить»: страница авторизации и возврат из amoCRM."""

import pytest

import web as w


@pytest.fixture
def harness():
    """Приложение с подставными обменом и сохранением — без сети."""
    seen = {"exchanged": [], "connected": [], "fail_exchange": False,
            "fail_store": False, "valid": {"abc12345"}}

    async def exchange(code, subdomain):
        if seen["fail_exchange"]:
            raise RuntimeError("amoCRM OAuth 400: bad code")
        seen["exchanged"].append((code, subdomain))
        return {"access_token": "AT", "refresh_token": "RT", "expires_in": 86400}

    async def on_connected(state, subdomain, tokens):
        if seen["fail_store"]:
            raise RuntimeError("база недоступна")
        seen["connected"].append((state, subdomain, tokens))

    app = w.make_app(
        client_id="cid-1", title="Подключить FixBot",
        exchange=exchange, on_connected=on_connected,
        is_valid_state=lambda s: s in seen["valid"])
    return app, seen


async def get(client, path):
    r = await client.get(path)
    return r.status, await r.text()


# ===================== страница с кнопкой =====================

@pytest.mark.asyncio
async def test_page_shows_amo_button(aiohttp_client, harness):
    app, _ = harness
    client = await aiohttp_client(app)

    status, body = await get(client, "/connect/abc12345")
    assert status == 200
    assert "amocrm.ru/auth/button.min.js" in body
    assert 'data-client-id="cid-1"' in body
    assert 'data-state="abc12345"' in body


@pytest.mark.asyncio
async def test_unknown_link_is_refused(aiohttp_client, harness):
    """Иначе страницу авторизации мог бы открыть кто угодно."""
    app, _ = harness
    client = await aiohttp_client(app)

    status, body = await get(client, "/connect/чужой-код")
    assert status == 200
    assert "не действует" in body
    assert "button.min.js" not in body


@pytest.mark.asyncio
async def test_callback_path_is_not_eaten_by_page(aiohttp_client, harness):
    """
    Страница и возврат живут по разным путям. Будь они на одном,
    amoCRM попала бы на страницу с кнопкой вместо обработчика.
    """
    app, seen = harness
    client = await aiohttp_client(app)

    status, body = await get(
        client, "/amo/callback?code=CODE&state=abc12345&referer=romashka.amocrm.ru")
    assert status == 200
    assert seen["exchanged"] == [("CODE", "romashka")]


# ===================== возврат из amoCRM =====================

@pytest.mark.asyncio
async def test_successful_connect(aiohttp_client, harness):
    app, seen = harness
    client = await aiohttp_client(app)

    _, body = await get(
        client, "/amo/callback?code=CODE&state=abc12345&referer=romashka.amocrm.ru")

    assert "Готово" in body
    state, subdomain, tokens = seen["connected"][0]
    assert (state, subdomain) == ("abc12345", "romashka")
    assert tokens["refresh_token"] == "RT"


@pytest.mark.asyncio
async def test_foreign_state_is_rejected(aiohttp_client, harness):
    """
    Защита от подделанного возврата: без совпадения с незакрытым
    подключением обмена не происходит.
    """
    app, seen = harness
    client = await aiohttp_client(app)

    _, body = await get(
        client, "/amo/callback?code=CODE&state=подделка&referer=zloy.amocrm.ru")
    assert seen["exchanged"] == []
    assert "устарела" in body


@pytest.mark.asyncio
async def test_missing_code_is_rejected(aiohttp_client, harness):
    app, seen = harness
    client = await aiohttp_client(app)

    await get(client, "/amo/callback?state=abc12345&referer=romashka.amocrm.ru")
    assert seen["exchanged"] == []


@pytest.mark.asyncio
@pytest.mark.parametrize("referer", [
    "", "не поддомен.amocrm.ru", "../etc.amocrm.ru", "a.amocrm.ru evil",
])
async def test_bad_subdomain_is_rejected(aiohttp_client, harness, referer):
    """Поддомен приходит снаружи и уходит в адрес запроса к amoCRM."""
    app, seen = harness
    client = await aiohttp_client(app)

    await client.get("/amo/callback", params={
        "code": "CODE", "state": "abc12345", "referer": referer})
    assert seen["exchanged"] == []


@pytest.mark.asyncio
async def test_user_declined(aiohttp_client, harness):
    app, seen = harness
    client = await aiohttp_client(app)

    _, body = await get(client, "/amo/callback?error=access_denied&state=abc12345")
    assert seen["exchanged"] == []
    assert "не выдан" in body


@pytest.mark.asyncio
async def test_exchange_failure_is_explained(aiohttp_client, harness):
    app, seen = harness
    seen["fail_exchange"] = True
    client = await aiohttp_client(app)

    _, body = await get(
        client, "/amo/callback?code=CODE&state=abc12345&referer=romashka.amocrm.ru")
    assert "Не получилось" in body
    assert seen["connected"] == []


@pytest.mark.asyncio
async def test_tokens_are_not_lost_when_saving_breaks(aiohttp_client, harness):
    """
    Код одноразовый: если сорвалось уже после обмена, повторить нельзя.
    Человек должен увидеть не «ошибка», а что делать дальше.
    """
    app, seen = harness
    seen["fail_store"] = True
    client = await aiohttp_client(app)

    _, body = await get(
        client, "/amo/callback?code=CODE&state=abc12345&referer=romashka.amocrm.ru")
    assert "Почти готово" in body
    assert seen["exchanged"] == [("CODE", "romashka")]


# ===================== мелочи =====================

@pytest.mark.asyncio
async def test_health(aiohttp_client, harness):
    """По этому адресу проверяют, жив ли сервис, — он должен быть простым."""
    app, _ = harness
    client = await aiohttp_client(app)
    status, body = await get(client, "/healthz")
    assert (status, body) == (200, "ok")


def test_no_secrets_on_the_page():
    """На странице только публичный client_id, секрет остаётся на сервере."""
    assert "client_secret" not in w.PAGE
