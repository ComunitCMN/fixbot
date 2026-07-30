"""Конфигурация из переменных окружения (.env)."""

from __future__ import annotations

import os
from dataclasses import dataclass, field

from dotenv import load_dotenv

#: Какой файл настроек читать.
#:
#: По умолчанию python-dotenv ищет `.env` от текущей папки и вверх. Для
#: одиночного бота это удобно, но у оператора код общий, а настройки
#: клиентов лежат по своим папкам: запуская клиента из папки с кодом, бот
#: находил `.env` оператора и подмешивал оттуда всё, чего не было
#: у клиента. Поэтому путь передаётся явно.
_ENV_FILE = os.getenv("ENV_FILE", "").strip()
load_dotenv(_ENV_FILE or None)


def _int(name: str) -> int | None:
    v = os.getenv(name, "").strip()
    return int(v) if v.lstrip("-").isdigit() else None


def _ids(name: str) -> set[int]:
    return {
        int(x) for x in os.getenv(name, "").replace(" ", "").split(",")
        if x.lstrip("-").isdigit()
    }


@dataclass
class Config:
    telegram_token: str = field(default_factory=lambda: os.environ["TELEGRAM_TOKEN"])
    anthropic_key: str = field(default_factory=lambda: os.environ["ANTHROPIC_API_KEY"])

    # --- amoCRM ---
    amo_subdomain: str = field(default_factory=lambda: os.environ["AMO_SUBDOMAIN"])
    #: long_lived (по умолчанию) | oauth
    amo_auth: str = os.getenv("AMO_AUTH", "long_lived").strip().lower()
    amo_token: str = os.getenv("AMO_LONG_TOKEN", "").strip()
    amo_client_id: str = os.getenv("AMO_CLIENT_ID", "").strip()
    amo_client_secret: str = os.getenv("AMO_CLIENT_SECRET", "").strip()
    amo_redirect_uri: str = os.getenv("AMO_REDIRECT_URI", "").strip()

    # --- Claude ---
    model: str = os.getenv("MODEL", "claude-haiku-4-5-20251001")
    #: Порог низкий намеренно: запись в CRM всё равно требует подтверждения
    #: кнопкой, поэтому лишний вопрос дешевле пропущенной фиксации.
    min_confidence: float = float(os.getenv("MIN_CONFIDENCE", "0.3"))
    #: Через сколько минут снимать карточку, если её никто не подтвердил
    confirm_ttl_min: int = int(os.getenv("CONFIRM_TTL_MIN", "60"))

    #: Как часто проверять, не сдвинулись ли сделки. Вебхуков нет — они
    #: требуют публичного адреса, — поэтому опрашиваем amoCRM сами.
    status_check_min: int = int(os.getenv("STATUS_CHECK_MIN", "10"))

    #: Сколько дней действует агентская фиксация. Срок ничего не блокирует:
    #: по нему бот напоминает агенту и, если тот подтверждает работу,
    #: пишет отметку в сделку.
    fixation_ttl_days: int = int(os.getenv("FIXATION_TTL_DAYS", "45"))
    #: За сколько дней до конца напомнить
    renew_warn_days: int = int(os.getenv("RENEW_WARN_DAYS", "3"))

    # --- поведение ---
    db_path: str = os.getenv("DB_PATH", "fixbot.db")
    sync_interval_min: int = int(os.getenv("SYNC_INTERVAL_MIN", "30"))

    #: Дополнительно к зеркалу спрашивать amoCRM вживую на каждой проверке.
    #: Убирает слепую зону со свежими контактами ценой ~секунды задержки.
    live_lookup: bool = os.getenv("LIVE_LOOKUP", "1") == "1"
    #: сколько дней без активности освобождают клиента отдела продаж
    retail_ttl_days: int = int(os.getenv("RETAIL_TTL_DAYS", "365"))

    #: Страна для номеров, записанных без кода: «8 999…», «0812…».
    #: Номера с «+» разбираются сами, подсказка им не нужна.
    #: Для отдельного чата переопределяется командой /region.
    default_region: str = os.getenv("DEFAULT_REGION", "RU").upper()

    #: Язык по умолчанию, когда в сообщении нет букв (например, только номер).
    #: Обычно бот отвечает на языке того сообщения, на которое реагирует.
    default_lang: str = os.getenv("DEFAULT_LANG", "ru").lower()[:2]

    #: Название застройщика — ваше собственное. Нужно, чтобы вычленить
    #: агентство из имени совместного чата: «TEUS & Squaresell» → TEUS.
    developer_name: str = os.getenv("DEVELOPER_NAME", "").strip()

    #: куда класть новые агентские сделки
    pipeline_id: int | None = field(default_factory=lambda: _int("AMO_PIPELINE_ID"))
    status_id: int | None = field(default_factory=lambda: _int("AMO_STATUS_ID"))
    phone_field_id: int | None = field(default_factory=lambda: _int("AMO_PHONE_FIELD_ID"))

    #: привязывать ли агентство к карточке клиента (по умолчанию — нет,
    #: агентство висит только на сделке)
    link_agency_to_contact: bool = os.getenv("LINK_AGENCY_TO_CONTACT", "0") == "1"

    allowed_chats: set[int] = field(default_factory=lambda: _ids("ALLOWED_CHATS"))

    #: Оператор — тот, кто держит бота на своём сервере. Видит техническое:
    #: воронки, синхронизацию, состояние amoCRM.
    operator_ids: set[int] = field(default_factory=lambda: _ids("OPERATOR_IDS"))
    #: Владелец — застройщик, для которого бот работает. Рассылки, группы,
    #: агентства, статистика. Ни токенов, ни воронок не видит.
    owner_ids: set[int] = field(default_factory=lambda: _ids("OWNER_IDS"))
    #: Старое название. Раньше роль была одна, поэтому продолжаем его
    #: понимать как «оператор» — иначе при обновлении бот потерял бы админа.
    admin_ids: set[int] = field(default_factory=lambda: _ids("ADMIN_IDS"))

    #: Папка с клиентами оператора: clients/<имя>/fixbot.db.
    #: Нужна только для раздела «Мои клиенты» — сводки по всем застройщикам.
    clients_dir: str = os.getenv("CLIENTS_DIR", "").strip()

    quiet: bool = os.getenv("QUIET", "1") == "1"

    #: Показывать ли агентам ссылки на карточки в amoCRM. По умолчанию нет:
    #: доступа к CRM застройщика у сторонних агентств всё равно нет.
    show_crm_links: bool = os.getenv("SHOW_CRM_LINKS", "0") == "1"

    #: Режим наблюдения: бот всё считает и отвечает в чат, но НИЧЕГО
    #: не пишет в amoCRM. Нужен на первой неделе, пока правила распознавания
    #: не обкатаны — иначе в CRM набежит мусор, который потом чистить руками.
    dry_run: bool = os.getenv("DRY_RUN", "0") == "1"

    def __post_init__(self) -> None:
        if self.amo_auth not in ("long_lived", "oauth"):
            raise ValueError("AMO_AUTH должен быть long_lived или oauth")
        if not self.operator_ids:
            self.operator_ids = set(self.admin_ids)
        # Оператор умеет всё, что умеет владелец.
        self.admin_ids = self.operator_ids | self.admin_ids


cfg = Config()
