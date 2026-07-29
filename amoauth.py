"""
Авторизация в amoCRM — вынесена за отдельный слой.

Сейчас работает долгосрочный токен: скопировал строку из интеграции, и всё.
Когда понадобится подключать чужие аккаунты (несколько застройщиков, каждый
со своей CRM) — включается OAuth, а вся остальная логика бота не меняется:
она видит только `await auth.token()`.

## Чем OAuth отличается на практике

| | Долгосрочный токен | OAuth |
|---|---|---|
| Подключение | скопировать строку | кнопка «Разрешить» в браузере |
| Живёт | до 5 лет | access 24 часа, refresh 3 месяца |
| Обновление | не нужно | автоматическое, но нужен работающий сервис |
| Публичный HTTPS-адрес | не нужен | **обязателен** (redirect_uri) |
| Чужие аккаунты | нельзя | можно |

## Что нужно для включения OAuth

1. В интеграции amoCRM указать «Ссылку для перенаправления» — публичный
   HTTPS-адрес, например `https://fixbot.example.com/amo/callback`.
2. Взять оттуда client_id (ID интеграции) и client_secret (секретный ключ).
3. Поднять этот адрес: страница с кнопкой авторизации amoCRM и обработчик
   возврата (см. OAuthProvider.exchange_code ниже).
4. В .env: AMO_AUTH=oauth, AMO_CLIENT_ID, AMO_CLIENT_SECRET, AMO_REDIRECT_URI.

Кнопка авторизации на странице — стандартный скрипт amoCRM:

    <script class="amocrm_oauth"
            charset="utf-8"
            data-client-id="ВАШ_CLIENT_ID"
            data-title="Подключить FixBot"
            data-compact="false"
            data-class-name="amo-button"
            data-color="default"
            data-state="ПРОИЗВОЛЬНАЯ_СТРОКА_ДЛЯ_ЗАЩИТЫ"
            data-error-callback="onAmoError"
            data-mode="post_message"
            src="https://www.amocrm.ru/auth/button.min.js"></script>

Пользователь жмёт кнопку → всплывает окно amoCRM → «Разрешить» → на
redirect_uri прилетают `code`, `referer` (поддомен) и `state`.
Код живёт 20 минут, его надо сразу обменять на токены.
"""

from __future__ import annotations

import logging
import time
from typing import Protocol

import httpx

log = logging.getLogger(__name__)

# Обновляем access-токен заранее, чтобы не словить 401 на границе.
REFRESH_MARGIN_SEC = 5 * 60


class AuthProvider(Protocol):
    subdomain: str

    async def token(self) -> str:
        """Действующий access-токен."""
        ...

    async def close(self) -> None:
        ...


class LongLivedAuth:
    """Долгосрочный токен: ничего не обновляем, просто отдаём строку."""

    def __init__(self, subdomain: str, token: str):
        self.subdomain = subdomain
        self._token = token

    async def token(self) -> str:
        return self._token

    async def close(self) -> None:
        return None


class OAuthAuth:
    """
    OAuth 2.0. Токены хранятся в таблице accounts и обновляются сами.

    Готов к работе, но требует публичного redirect_uri — поэтому на локальном
    Mac не заработает. Включается переменной AMO_AUTH=oauth.
    """

    AUTH_PAGE = "https://www.amocrm.ru/oauth"

    def __init__(self, subdomain: str, client_id: str, client_secret: str,
                 redirect_uri: str, db=None):
        self.subdomain = subdomain
        self.client_id = client_id
        self.client_secret = client_secret
        self.redirect_uri = redirect_uri
        self.db = db
        self._http = httpx.AsyncClient(timeout=20.0)

    # ---------- ссылки для страницы авторизации ----------

    def authorize_url(self, state: str) -> str:
        """Прямая ссылка, если не использовать JS-кнопку."""
        return (f"{self.AUTH_PAGE}?client_id={self.client_id}"
                f"&state={state}&mode=post_message")

    # ---------- обмен кода на токены ----------

    async def exchange_code(self, code: str, subdomain: str | None = None) -> dict:
        """
        Вызывается обработчиком redirect_uri сразу после «Разрешить».
        Код действует 20 минут.
        """
        sub = subdomain or self.subdomain
        data = await self._post_token(sub, {
            "client_id": self.client_id,
            "client_secret": self.client_secret,
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": self.redirect_uri,
        })
        self.subdomain = sub
        self._store(sub, data)
        return data

    async def _refresh(self) -> str:
        row = self.db.get_account() if self.db else None
        refresh = row["refresh_token"] if row else None
        if not refresh:
            raise RuntimeError(
                "Нет refresh-токена — нужна повторная авторизация в amoCRM"
            )
        data = await self._post_token(self.subdomain, {
            "client_id": self.client_id,
            "client_secret": self.client_secret,
            "grant_type": "refresh_token",
            "refresh_token": refresh,
            "redirect_uri": self.redirect_uri,
        })
        self._store(self.subdomain, data)
        return data["access_token"]

    async def _post_token(self, subdomain: str, payload: dict) -> dict:
        r = await self._http.post(
            f"https://{subdomain}.amocrm.ru/oauth2/access_token", json=payload
        )
        if r.status_code >= 400:
            raise RuntimeError(f"amoCRM OAuth {r.status_code}: {r.text[:300]}")
        return r.json()

    def _store(self, subdomain: str, data: dict) -> None:
        if not self.db:
            return
        expires_at = int(time.time()) + int(data.get("expires_in", 86400))
        self.db.upsert_account(
            subdomain=subdomain, auth_type="oauth",
            access_token=data["access_token"],
            refresh_token=data.get("refresh_token"),
            expires_at=expires_at,
            client_id=self.client_id, client_secret=self.client_secret,
        )

    # ---------- отдача токена наружу ----------

    async def token(self) -> str:
        row = self.db.get_account() if self.db else None
        if not row or not row["access_token"]:
            raise RuntimeError(
                "amoCRM не подключена. Пройдите авторизацию по ссылке "
                f"{self.authorize_url('setup')}"
            )
        if (row["expires_at"] or 0) - REFRESH_MARGIN_SEC <= time.time():
            log.info("Обновляю access-токен amoCRM")
            return await self._refresh()
        return row["access_token"]

    async def close(self) -> None:
        await self._http.aclose()


def build_auth(cfg, db) -> AuthProvider:
    """Собирает нужный провайдер по конфигу."""
    if cfg.amo_auth == "oauth":
        missing = [n for n, v in (
            ("AMO_CLIENT_ID", cfg.amo_client_id),
            ("AMO_CLIENT_SECRET", cfg.amo_client_secret),
            ("AMO_REDIRECT_URI", cfg.amo_redirect_uri),
        ) if not v]
        if missing:
            raise RuntimeError(
                "Для AMO_AUTH=oauth нужно заполнить: " + ", ".join(missing)
            )
        return OAuthAuth(cfg.amo_subdomain, cfg.amo_client_id,
                         cfg.amo_client_secret, cfg.amo_redirect_uri, db)
    if not cfg.amo_token:
        raise RuntimeError("Заполните AMO_LONG_TOKEN или переключитесь на AMO_AUTH=oauth")
    return LongLivedAuth(cfg.amo_subdomain, cfg.amo_token)
