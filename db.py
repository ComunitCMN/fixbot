"""
Хранилище FixBot.

Схема сразу рассчитана на несколько аккаунтов amoCRM: во всех таблицах есть
account_id. Пока аккаунт один (id=1), но когда появится OAuth и подключатся
чужие CRM — менять структуру не придётся, только перестать хардкодить
единицу.

Что здесь лежит:
  accounts        — подключённые аккаунты amoCRM и их токены
  pipelines       — воронки, размеченные на розничные / агентские
  contacts        — зеркало телефонов (для префиксного поиска по маске)
  contact_origin  — вычисленное происхождение каждого контакта
  companies       — зеркало компаний amoCRM
  agencies        — справочник агентств + синонимы написания
  agents          — реестр Telegram-агентов (заполняется на Этапе 2)
  fixations       — журнал всех попыток фиксации с вердиктом
"""

from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path

from phones import Phone, compare_prefix, from_digits, normalize, search_prefix

DEFAULT_ACCOUNT = 1

SCHEMA = """
PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS accounts (
    id            INTEGER PRIMARY KEY,
    subdomain     TEXT NOT NULL,
    auth_type     TEXT NOT NULL DEFAULT 'long_lived',  -- long_lived | oauth
    access_token  TEXT,
    refresh_token TEXT,
    expires_at    INTEGER,
    client_id     TEXT,
    client_secret TEXT,
    title         TEXT,
    created_at    INTEGER,
    UNIQUE (subdomain)
);

CREATE TABLE IF NOT EXISTS pipelines (
    account_id  INTEGER NOT NULL,
    pipeline_id INTEGER NOT NULL,
    name        TEXT,
    kind        TEXT NOT NULL DEFAULT 'unset',   -- retail | agency | ignore | unset
    PRIMARY KEY (account_id, pipeline_id)
);

CREATE TABLE IF NOT EXISTS statuses (
    account_id  INTEGER NOT NULL,
    pipeline_id INTEGER NOT NULL,
    status_id   INTEGER NOT NULL,
    name        TEXT,
    sort        INTEGER,
    is_booking  INTEGER NOT NULL DEFAULT 0,
    notify      INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (account_id, pipeline_id, status_id)
);

CREATE TABLE IF NOT EXISTS contacts (
    account_id  INTEGER NOT NULL,
    contact_id  INTEGER NOT NULL,
    name        TEXT,
    digits      TEXT NOT NULL,
    created_at  INTEGER,
    PRIMARY KEY (account_id, contact_id, digits)
);
CREATE INDEX IF NOT EXISTS idx_contacts_digits ON contacts(digits);

CREATE TABLE IF NOT EXISTS contact_origin (
    account_id            INTEGER NOT NULL,
    contact_id            INTEGER NOT NULL,
    has_retail            INTEGER NOT NULL DEFAULT 0,
    last_retail_activity  INTEGER,
    has_agency            INTEGER NOT NULL DEFAULT 0,
    agency_company_id     INTEGER,
    last_agency_activity  INTEGER,
    booked                INTEGER NOT NULL DEFAULT 0,
    computed_at           INTEGER,
    PRIMARY KEY (account_id, contact_id)
);

CREATE TABLE IF NOT EXISTS companies (
    account_id INTEGER NOT NULL,
    company_id INTEGER NOT NULL,
    name       TEXT,
    norm_name  TEXT,
    PRIMARY KEY (account_id, company_id)
);
CREATE INDEX IF NOT EXISTS idx_companies_norm ON companies(norm_name);

CREATE TABLE IF NOT EXISTS agencies (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id    INTEGER NOT NULL,
    name          TEXT NOT NULL,
    norm_name     TEXT NOT NULL,
    amo_company_id INTEGER,
    created_at    INTEGER,
    UNIQUE (account_id, norm_name)
);

CREATE TABLE IF NOT EXISTS agency_aliases (
    account_id INTEGER NOT NULL,
    agency_id  INTEGER NOT NULL,
    norm_alias TEXT NOT NULL,
    PRIMARY KEY (account_id, norm_alias)
);

CREATE TABLE IF NOT EXISTS agents (
    account_id   INTEGER NOT NULL,
    telegram_id  INTEGER NOT NULL,
    username     TEXT,
    display_name TEXT,
    agency_id    INTEGER,
    dm_open      INTEGER NOT NULL DEFAULT 0,
    subscribed   INTEGER NOT NULL DEFAULT 1,
    created_at   INTEGER,
    last_seen_at INTEGER,
    PRIMARY KEY (account_id, telegram_id)
);

CREATE TABLE IF NOT EXISTS fixations (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id     INTEGER NOT NULL,
    digits         TEXT NOT NULL,
    client_name    TEXT,
    agency_id      INTEGER,
    agent_telegram_id INTEGER,
    agent_name     TEXT,
    chat_id        INTEGER,
    chat_title     TEXT,
    message_id     INTEGER,
    raw_text       TEXT,
    verdict        TEXT,
    amo_contact_id INTEGER,
    amo_lead_id    INTEGER,
    created_at     INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_fixations_digits ON fixations(digits);

CREATE TABLE IF NOT EXISTS pending (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id  INTEGER NOT NULL,
    chat_id     INTEGER NOT NULL,
    message_id  INTEGER,          -- исходное сообщение агента
    prompt_id   INTEGER,          -- сообщение бота с кнопками
    author_id   INTEGER NOT NULL, -- кто может подтвердить
    payload     TEXT NOT NULL,    -- JSON с разобранными данными
    status      TEXT NOT NULL DEFAULT 'waiting',  -- waiting|done|cancelled|expired
    created_at  INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_pending_status ON pending(status, created_at);

CREATE TABLE IF NOT EXISTS invites (
    code        TEXT PRIMARY KEY,
    created_by  INTEGER NOT NULL,
    expires_at  INTEGER NOT NULL,
    used_by     INTEGER,
    used_at     INTEGER,
    created_at  INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS onboardings (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    code        TEXT,
    user_id     INTEGER NOT NULL,
    username    TEXT,
    user_name   TEXT,
    step        TEXT NOT NULL DEFAULT 'developer',
    data        TEXT NOT NULL DEFAULT '{}',
    status      TEXT NOT NULL DEFAULT 'active',  -- active|ready|done|rejected
    slug        TEXT,
    created_at  INTEGER NOT NULL,
    updated_at  INTEGER
);
CREATE INDEX IF NOT EXISTS idx_onb_user ON onboardings(user_id, status);

CREATE TABLE IF NOT EXISTS staff (
    account_id  INTEGER NOT NULL,
    telegram_id INTEGER NOT NULL,
    username    TEXT,
    name        TEXT,
    added_by    INTEGER,
    created_at  INTEGER,
    PRIMARY KEY (account_id, telegram_id)
);

CREATE TABLE IF NOT EXISTS broadcasts (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id  INTEGER NOT NULL,
    admin_id    INTEGER NOT NULL,
    src_chat_id INTEGER,
    message_ids TEXT,        -- JSON: что копировать один в один
    items       TEXT,        -- JSON: медиа (тип + file_id) для перевода
    html        TEXT,        -- исходный текст с разметкой
    html_en     TEXT,        -- перевод, тоже с разметкой
    target      TEXT,        -- JSON: кому шлём
    status      TEXT NOT NULL DEFAULT 'draft',  -- draft|sent|cancelled
    sent        INTEGER NOT NULL DEFAULT 0,
    failed      INTEGER NOT NULL DEFAULT 0,
    created_at  INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS meta (
    account_id INTEGER NOT NULL,
    key        TEXT NOT NULL,
    value      TEXT,
    PRIMARY KEY (account_id, key)
);

-- Чаты, где бота видели. Список групп у Telegram не спросить: бот узнаёт
-- о чате только когда оттуда приходит сообщение. Поэтому запоминаем сами —
-- иначе настроить язык и агентство было бы не на чем.
CREATE TABLE IF NOT EXISTS chats (
    account_id INTEGER NOT NULL,
    chat_id    INTEGER NOT NULL,
    title      TEXT,
    is_admin   INTEGER,
    messages   INTEGER NOT NULL DEFAULT 0,
    first_seen INTEGER,
    last_seen  INTEGER,
    PRIMARY KEY (account_id, chat_id)
);

-- Обслуживание клиентов. Живёт ТОЛЬКО в базе оператора: биллинг ведётся
-- из его бота, а базы клиентов трогаются лишь на чтение — иначе у файла
-- окажется два хозяина.
--
-- Даты хранятся строками YYYY-MM-DD, а не отметками времени: тут важен
-- календарный день, а не момент, и часовые пояса только мешали бы.
CREATE TABLE IF NOT EXISTS billing (
    slug           TEXT PRIMARY KEY,   -- папка клиента
    start_date     TEXT NOT NULL,      -- когда началось обслуживание
    threshold      INTEGER NOT NULL DEFAULT 100,
    low            INTEGER NOT NULL DEFAULT 40,
    high           INTEGER NOT NULL DEFAULT 70,
    currency       TEXT NOT NULL DEFAULT 'USD',
    wallet         TEXT,               -- адрес кошелька
    wallet_note    TEXT,               -- сеть, например USDT TRC-20
    wallet_qr      TEXT,               -- file_id картинки в Telegram
    closed_due     TEXT,               -- срок последнего оплаченного периода
    paused         INTEGER NOT NULL DEFAULT 0,
    enabled        INTEGER NOT NULL DEFAULT 1
);

-- По строке на период. Хранить историю нужно не для красоты: если клиент
-- заспорит о сумме, только здесь видно, когда и за что выставляли.
CREATE TABLE IF NOT EXISTS billing_periods (
    slug         TEXT NOT NULL,
    due          TEXT NOT NULL,         -- срок оплаты, он же ключ периода
    begin        TEXT NOT NULL,
    fixations    INTEGER,
    amount       INTEGER,
    announced    INTEGER NOT NULL DEFAULT 0,
    prepared     INTEGER NOT NULL DEFAULT 0,
    invoice_sent INTEGER NOT NULL DEFAULT 0,
    reminded     INTEGER NOT NULL DEFAULT 0,
    warned       INTEGER NOT NULL DEFAULT 0,
    paid_at      INTEGER,
    last_nudge   TEXT,
    PRIMARY KEY (slug, due)
);
"""

#: Колонки, добавленные после первого релиза. Ключ — таблица.
MIGRATIONS = {
    "agents": [
        ("amo_contact_id", "INTEGER"),
        ("phone", "TEXT"),
        ("lang", "TEXT"),
        # Согласие на рассылки. Отдельно от subscribed: человек, которому
        # надоела реклама, не должен заодно потерять уведомления о своих
        # клиентах — ради них он и подписывался.
        ("bcast", "INTEGER"),
        # Пусто или «active» — обычный агент. «pending» — частник подал
        # заявку и ждёт владельца, «rejected» — владелец отказал.
        # У всех, кто был в базе до этой правки, поле пустое, и правило
        # доступа понимает это как обычного агента.
        ("status", "TEXT"),
        # Имя, которым частник представился сам.
        ("intro_name", "TEXT"),
    ],
    "agencies": [
        # Частный агент заводится обычным агентством с этой пометкой:
        # тогда ниже по течению — компания в CRM, статистика, разрез
        # по агентствам — ничего менять не надо.
        ("private", "INTEGER"),
    ],
    "statuses": [
        ("type", "INTEGER"),
    ],
    "fixations": [
        # Последний известный этап сделки — по нему ловим изменения.
        ("last_status_id", "INTEGER"),
        ("last_pipeline_id", "INTEGER"),
        # Агент нажал «Отслеживать статус».
        ("watching", "INTEGER"),
        # Когда фиксацию последний раз продлевали. Срок считается отсюда.
        ("renewed_at", "INTEGER"),
        # О каком именно сроке уже напоминали — чтобы не долбить каждый час.
        ("reminded_for", "INTEGER"),
        # Уже сообщили агенту, что клиент постучался в прямой отдел.
        ("retail_notified", "INTEGER"),
    ],
}

#: Служебные этапы amoCRM: «Неразобранное», «Успешно реализовано»,
#: «Закрыто и не реализовано». Ставить их при создании сделки нельзя.
SYSTEM_STATUS_IDS = (1, 142, 143)


@dataclass
class Match:
    """Найденное совпадение по номеру + всё, что известно о его происхождении."""

    digits: str
    name: str | None = None
    source: str = "amo"                  # amo | chat
    contact_id: int | None = None

    # происхождение (только для source="amo")
    has_retail: bool = False
    last_retail_activity: int | None = None
    has_agency: bool = False
    agency_company_id: int | None = None
    booked: bool = False
    origin_known: bool = False

    # для совпадений из журнала чата
    agency_id: int | None = None
    agent_telegram_id: int | None = None
    agent_name: str | None = None
    chat_title: str | None = None
    created_at: int | None = None

    @property
    def phone(self) -> Phone:
        return from_digits(self.digits)


class Db:
    def __init__(self, path: str | Path = "fixbot.db", account_id: int = DEFAULT_ACCOUNT):
        self.path = str(path)
        self.account_id = account_id
        self.conn = sqlite3.connect(self.path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self._migrate()
        self.conn.commit()

    def _migrate(self) -> None:
        """
        Досыпает колонки, которых нет в уже существующей базе.

        CREATE TABLE IF NOT EXISTS не меняет структуру старой таблицы,
        поэтому при обновлении бота новые поля надо добавлять руками.
        """
        for table, columns in MIGRATIONS.items():
            have = {
                r["name"] for r in
                self.conn.execute(f"PRAGMA table_info({table})")
            }
            for name, decl in columns:
                if name not in have:
                    self.conn.execute(
                        f"ALTER TABLE {table} ADD COLUMN {name} {decl}")

    # ================= аккаунты =================

    def upsert_account(self, subdomain: str, auth_type: str = "long_lived",
                       access_token: str | None = None,
                       refresh_token: str | None = None,
                       expires_at: int | None = None,
                       client_id: str | None = None,
                       client_secret: str | None = None) -> int:
        self.conn.execute(
            "INSERT INTO accounts (id, subdomain, auth_type, access_token,"
            " refresh_token, expires_at, client_id, client_secret, created_at)"
            " VALUES (?,?,?,?,?,?,?,?,?)"
            " ON CONFLICT(id) DO UPDATE SET"
            "  subdomain=excluded.subdomain, auth_type=excluded.auth_type,"
            "  access_token=excluded.access_token,"
            "  refresh_token=COALESCE(excluded.refresh_token, accounts.refresh_token),"
            "  expires_at=excluded.expires_at,"
            "  client_id=COALESCE(excluded.client_id, accounts.client_id),"
            "  client_secret=COALESCE(excluded.client_secret, accounts.client_secret)",
            (self.account_id, subdomain, auth_type, access_token, refresh_token,
             expires_at, client_id, client_secret, int(time.time())),
        )
        self.conn.commit()
        return self.account_id

    def get_account(self) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM accounts WHERE id=?", (self.account_id,)
        ).fetchone()

    def save_tokens(self, access_token: str, refresh_token: str | None,
                    expires_at: int) -> None:
        self.conn.execute(
            "UPDATE accounts SET access_token=?,"
            " refresh_token=COALESCE(?, refresh_token), expires_at=? WHERE id=?",
            (access_token, refresh_token, expires_at, self.account_id),
        )
        self.conn.commit()

    # ================= воронки =================

    def replace_pipelines(self, items: list[dict]) -> None:
        """items: [{id, name, statuses:[{id,name,sort}]}]. Разметка kind сохраняется."""
        cur = self.conn.cursor()
        for p in items:
            cur.execute(
                "INSERT INTO pipelines (account_id, pipeline_id, name) VALUES (?,?,?)"
                " ON CONFLICT(account_id, pipeline_id) DO UPDATE SET name=excluded.name",
                (self.account_id, p["id"], p.get("name")),
            )
            for s in p.get("statuses", []):
                cur.execute(
                    "INSERT INTO statuses"
                    " (account_id, pipeline_id, status_id, name, sort, type)"
                    " VALUES (?,?,?,?,?,?)"
                    " ON CONFLICT(account_id, pipeline_id, status_id)"
                    " DO UPDATE SET name=excluded.name, sort=excluded.sort,"
                    "               type=excluded.type",
                    (self.account_id, p["id"], s["id"], s.get("name"),
                     s.get("sort"), s.get("type")),
                )
        self.conn.commit()

    def set_pipeline_kind(self, pipeline_id: int, kind: str) -> None:
        assert kind in ("retail", "agency", "ignore", "unset")
        self.conn.execute(
            "UPDATE pipelines SET kind=? WHERE account_id=? AND pipeline_id=?",
            (kind, self.account_id, pipeline_id),
        )
        self.conn.commit()

    def list_pipelines(self) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM pipelines WHERE account_id=? ORDER BY pipeline_id",
            (self.account_id,),
        ).fetchall()

    def pipeline_kinds(self) -> dict[int, str]:
        return {
            r["pipeline_id"]: r["kind"]
            for r in self.list_pipelines()
        }

    def is_configured(self) -> bool:
        """Размечена ли хотя бы одна розничная воронка."""
        row = self.conn.execute(
            "SELECT COUNT(*) c FROM pipelines WHERE account_id=? AND kind='retail'",
            (self.account_id,),
        ).fetchone()
        return row["c"] > 0

    def set_booking_status(self, pipeline_id: int, status_id: int) -> None:
        self.conn.execute(
            "UPDATE statuses SET is_booking=0 WHERE account_id=? AND pipeline_id=?",
            (self.account_id, pipeline_id),
        )
        self.conn.execute(
            "UPDATE statuses SET is_booking=1"
            " WHERE account_id=? AND pipeline_id=? AND status_id=?",
            (self.account_id, pipeline_id, status_id),
        )
        self.conn.commit()

    def agency_pipeline(self) -> sqlite3.Row | None:
        """
        Воронка, куда бот кладёт свои фиксации.

        Берётся из разметки, а не из настроек: иначе агентские сделки
        попадут в розничную воронку и бот начнёт блокировать собственные
        же фиксации как «клиентов отдела продаж».
        """
        return self.conn.execute(
            "SELECT * FROM pipelines WHERE account_id=? AND kind='agency'"
            " ORDER BY pipeline_id LIMIT 1",
            (self.account_id,),
        ).fetchone()

    def first_status(self, pipeline_id: int) -> int | None:
        """
        Первый рабочий этап воронки.

        Служебные этапы пропускаем: «Неразобранное» и закрывающие статусы
        назначить при создании сделки нельзя — amoCRM отвечает
        NotSupportedChoice. Если рабочих этапов не нашлось, возвращаем
        None: тогда сделка создаётся без указания этапа и amoCRM сама
        поставит её в начало воронки.
        """
        marks = ",".join("?" * len(SYSTEM_STATUS_IDS))
        row = self.conn.execute(
            f"SELECT status_id FROM statuses"
            f" WHERE account_id=? AND pipeline_id=?"
            f"   AND status_id NOT IN ({marks})"
            f"   AND COALESCE(type, 0) = 0"
            f" ORDER BY COALESCE(sort, 0), status_id LIMIT 1",
            (self.account_id, pipeline_id, *SYSTEM_STATUS_IDS),
        ).fetchone()
        return row["status_id"] if row else None

    def booking_status_ids(self) -> set[int]:
        return {
            r["status_id"] for r in self.conn.execute(
                "SELECT status_id FROM statuses WHERE account_id=? AND is_booking=1",
                (self.account_id,),
            )
        }

    # ================= зеркало контактов =================

    def replace_contacts(self, rows: list[dict]) -> int:
        cur = self.conn.cursor()
        cur.execute("DELETE FROM contacts WHERE account_id=?", (self.account_id,))
        n = 0
        for r in rows:
            for raw in r.get("phones", []):
                p = normalize(raw)
                if not p:
                    continue
                cur.execute(
                    "INSERT OR IGNORE INTO contacts"
                    " (account_id, contact_id, name, digits, created_at)"
                    " VALUES (?,?,?,?,?)",
                    (self.account_id, r["id"], r.get("name"), p.digits,
                     r.get("created_at")),
                )
                n += 1
        self.conn.commit()
        self.set_meta("contacts_synced_at", str(int(time.time())))
        return n

    def replace_origins(self, rows: list[dict]) -> int:
        """rows: {contact_id, has_retail, last_retail_activity, has_agency,
        agency_company_id, last_agency_activity, booked}"""
        cur = self.conn.cursor()
        cur.execute("DELETE FROM contact_origin WHERE account_id=?", (self.account_id,))
        now = int(time.time())
        for r in rows:
            cur.execute(
                "INSERT INTO contact_origin (account_id, contact_id, has_retail,"
                " last_retail_activity, has_agency, agency_company_id,"
                " last_agency_activity, booked, computed_at)"
                " VALUES (?,?,?,?,?,?,?,?,?)",
                (self.account_id, r["contact_id"], int(r.get("has_retail", 0)),
                 r.get("last_retail_activity"), int(r.get("has_agency", 0)),
                 r.get("agency_company_id"), r.get("last_agency_activity"),
                 int(r.get("booked", 0)), now),
            )
        self.conn.commit()
        self.set_meta("origins_synced_at", str(now))
        return len(rows)

    def find_matches(self, p: Phone) -> list[Match]:
        """Префиксный поиск совпадений с подтянутым происхождением."""
        if not p.is_usable:
            return []

        short = search_prefix(p)
        out: list[Match] = []

        cur = self.conn.execute(
            "SELECT c.contact_id, c.name, c.digits, c.created_at,"
            "       o.has_retail, o.last_retail_activity, o.has_agency,"
            "       o.agency_company_id, o.booked, o.computed_at"
            " FROM contacts c"
            " LEFT JOIN contact_origin o"
            "   ON o.account_id=c.account_id AND o.contact_id=c.contact_id"
            " WHERE c.account_id=? AND c.digits LIKE ?",
            (self.account_id, short + "%"),
        )
        for row in cur:
            if not compare_prefix(p, from_digits(row["digits"])):
                continue
            out.append(Match(
                digits=row["digits"], name=row["name"], source="amo",
                contact_id=row["contact_id"],
                has_retail=bool(row["has_retail"]),
                last_retail_activity=row["last_retail_activity"],
                has_agency=bool(row["has_agency"]),
                agency_company_id=row["agency_company_id"],
                booked=bool(row["booked"]),
                origin_known=row["computed_at"] is not None,
                created_at=row["created_at"],
            ))

        cur = self.conn.execute(
            "SELECT digits, client_name, agency_id, agent_telegram_id, agent_name,"
            "       chat_title, created_at, amo_contact_id, verdict"
            " FROM fixations"
            " WHERE account_id=? AND digits LIKE ? AND amo_lead_id IS NOT NULL"
            " ORDER BY created_at",
            (self.account_id, short + "%"),
        )
        for row in cur:
            if not compare_prefix(p, from_digits(row["digits"])):
                continue
            out.append(Match(
                digits=row["digits"], name=row["client_name"], source="chat",
                contact_id=row["amo_contact_id"], has_agency=True, origin_known=True,
                agency_id=row["agency_id"],
                agent_telegram_id=row["agent_telegram_id"],
                agent_name=row["agent_name"], chat_title=row["chat_title"],
                created_at=row["created_at"],
            ))

        seen: set[tuple] = set()
        uniq: list[Match] = []
        for m in out:
            key = (m.source, m.contact_id, m.digits, m.agency_id)
            if key not in seen:
                seen.add(key)
                uniq.append(m)
        return uniq

    def upsert_from_live(self, row: dict) -> None:
        """
        Кладёт в зеркало то, что нашёл живой запрос в amoCRM.

        Нужно, чтобы свежий контакт не приходилось искать заново при
        следующей проверке. В отличие от add_contact_row не выдумывает
        происхождение, а записывает ровно то, что вернула CRM.
        """
        now = int(time.time())
        self.conn.execute(
            "INSERT OR IGNORE INTO contacts"
            " (account_id, contact_id, name, digits, created_at) VALUES (?,?,?,?,?)",
            (self.account_id, row["contact_id"], row.get("name"),
             row["digits"], row.get("created_at") or now),
        )
        self.conn.execute(
            "INSERT INTO contact_origin (account_id, contact_id, has_retail,"
            " last_retail_activity, has_agency, agency_company_id, booked,"
            " computed_at) VALUES (?,?,?,?,?,?,?,?)"
            " ON CONFLICT(account_id, contact_id) DO UPDATE SET"
            "  has_retail=excluded.has_retail,"
            "  last_retail_activity=excluded.last_retail_activity,"
            "  has_agency=excluded.has_agency,"
            "  agency_company_id=excluded.agency_company_id,"
            "  booked=excluded.booked, computed_at=excluded.computed_at",
            (self.account_id, row["contact_id"], int(row.get("has_retail", 0)),
             row.get("last_retail_activity"), int(row.get("has_agency", 0)),
             row.get("agency_company_id"), int(row.get("booked", 0)), now),
        )
        self.conn.commit()

    def add_contact_row(self, contact_id: int, name: str | None, digits: str,
                        agency_company_id: int | None = None) -> None:
        now = int(time.time())
        self.conn.execute(
            "INSERT OR IGNORE INTO contacts"
            " (account_id, contact_id, name, digits, created_at) VALUES (?,?,?,?,?)",
            (self.account_id, contact_id, name, digits, now),
        )
        self.conn.execute(
            "INSERT INTO contact_origin (account_id, contact_id, has_retail,"
            " has_agency, agency_company_id, last_agency_activity, computed_at)"
            " VALUES (?,?,0,1,?,?,?)"
            " ON CONFLICT(account_id, contact_id) DO UPDATE SET"
            "  has_agency=1, agency_company_id=COALESCE(excluded.agency_company_id,"
            "  contact_origin.agency_company_id), last_agency_activity=excluded.last_agency_activity",
            (self.account_id, contact_id, agency_company_id, now, now),
        )
        self.conn.commit()

    # ================= компании и агентства =================

    def replace_companies(self, rows: list[dict]) -> int:
        from agencies import norm_agency

        cur = self.conn.cursor()
        cur.execute("DELETE FROM companies WHERE account_id=?", (self.account_id,))
        for r in rows:
            cur.execute(
                "INSERT OR IGNORE INTO companies"
                " (account_id, company_id, name, norm_name) VALUES (?,?,?,?)",
                (self.account_id, r["id"], r.get("name"),
                 norm_agency(r.get("name") or "")),
            )
        self.conn.commit()
        return len(rows)

    def list_companies(self) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT company_id, name, norm_name FROM companies WHERE account_id=?",
            (self.account_id,),
        ).fetchall()

    def find_agency_by_norm(self, norm: str) -> sqlite3.Row | None:
        row = self.conn.execute(
            "SELECT * FROM agencies WHERE account_id=? AND norm_name=?",
            (self.account_id, norm),
        ).fetchone()
        if row:
            return row
        alias = self.conn.execute(
            "SELECT agency_id FROM agency_aliases WHERE account_id=? AND norm_alias=?",
            (self.account_id, norm),
        ).fetchone()
        if not alias:
            return None
        return self.conn.execute(
            "SELECT * FROM agencies WHERE id=?", (alias["agency_id"],)
        ).fetchone()

    def create_agency(self, name: str, norm: str,
                      amo_company_id: int | None = None) -> int:
        cur = self.conn.execute(
            "INSERT INTO agencies (account_id, name, norm_name, amo_company_id, created_at)"
            " VALUES (?,?,?,?,?)"
            " ON CONFLICT(account_id, norm_name) DO UPDATE SET"
            "  amo_company_id=COALESCE(excluded.amo_company_id, agencies.amo_company_id)",
            (self.account_id, name, norm, amo_company_id, int(time.time())),
        )
        self.conn.commit()
        if cur.lastrowid:
            return cur.lastrowid
        row = self.conn.execute(
            "SELECT id FROM agencies WHERE account_id=? AND norm_name=?",
            (self.account_id, norm),
        ).fetchone()
        return row["id"]

    def add_agency_alias(self, agency_id: int, norm_alias: str) -> None:
        self.conn.execute(
            "INSERT OR IGNORE INTO agency_aliases (account_id, agency_id, norm_alias)"
            " VALUES (?,?,?)",
            (self.account_id, agency_id, norm_alias),
        )
        self.conn.commit()

    def get_agency(self, agency_id: int) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM agencies WHERE id=?", (agency_id,)
        ).fetchone()

    def list_agencies(self) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM agencies WHERE account_id=? ORDER BY name",
            (self.account_id,),
        ).fetchall()

    def agency_by_company_id(self, company_id: int) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM agencies WHERE account_id=? AND amo_company_id=?",
            (self.account_id, company_id),
        ).fetchone()

    # ================= агенты =================

    def upsert_agent(self, telegram_id: int, username: str | None,
                     display_name: str | None, agency_id: int | None = None,
                     dm_open: bool | None = None) -> None:
        now = int(time.time())
        flag = int(dm_open) if dm_open is not None else None
        # dm_open объявлен NOT NULL, поэтому при вставке подставляем 0,
        # а при обновлении передаём исходное значение отдельным параметром:
        # None должен означать «не трогать», а не «сбросить в ноль».
        self.conn.execute(
            "INSERT INTO agents (account_id, telegram_id, username, display_name,"
            " agency_id, dm_open, created_at, last_seen_at)"
            " VALUES (?,?,?,?,?,COALESCE(?,0),?,?)"
            " ON CONFLICT(account_id, telegram_id) DO UPDATE SET"
            "  username=COALESCE(excluded.username, agents.username),"
            "  display_name=COALESCE(excluded.display_name, agents.display_name),"
            "  agency_id=COALESCE(excluded.agency_id, agents.agency_id),"
            "  dm_open=COALESCE(?, agents.dm_open),"
            "  last_seen_at=excluded.last_seen_at",
            (self.account_id, telegram_id, username, display_name, agency_id,
             flag, now, now, flag),
        )
        self.conn.commit()

    def get_agent(self, telegram_id: int) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM agents WHERE account_id=? AND telegram_id=?",
            (self.account_id, telegram_id),
        ).fetchone()

    def set_agent_field(self, telegram_id: int, **fields) -> None:
        """Точечно правит карточку агента: язык, телефон, флаг личи."""
        allowed = {"lang", "phone", "dm_open", "subscribed", "agency_id",
                   "bcast", "amo_contact_id"}
        fields = {k: v for k, v in fields.items() if k in allowed}
        if not fields:
            return
        sets = ", ".join(f"{k}=?" for k in fields)
        self.conn.execute(
            f"UPDATE agents SET {sets} WHERE account_id=? AND telegram_id=?",
            (*fields.values(), self.account_id, telegram_id),
        )
        self.conn.commit()

    def agents_with_dm(self) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM agents WHERE account_id=? AND dm_open=1",
            (self.account_id,),
        ).fetchall()

    def set_agent_amo_contact(self, telegram_id: int, contact_id: int) -> None:
        self.conn.execute(
            "UPDATE agents SET amo_contact_id=?"
            " WHERE account_id=? AND telegram_id=?",
            (contact_id, self.account_id, telegram_id),
        )
        self.conn.commit()

    # ================= заявки на подтверждение =================

    def create_pending(self, chat_id: int, message_id: int | None,
                       author_id: int, payload: dict) -> int:
        cur = self.conn.execute(
            "INSERT INTO pending (account_id, chat_id, message_id, author_id,"
            " payload, created_at) VALUES (?,?,?,?,?,?)",
            (self.account_id, chat_id, message_id, author_id,
             json.dumps(payload, ensure_ascii=False), int(time.time())),
        )
        self.conn.commit()
        return cur.lastrowid

    def get_pending(self, pending_id: int) -> dict | None:
        row = self.conn.execute(
            "SELECT * FROM pending WHERE id=? AND account_id=?",
            (pending_id, self.account_id),
        ).fetchone()
        if not row:
            return None
        out = dict(row)
        out["payload"] = json.loads(out["payload"])
        return out

    def set_pending_prompt(self, pending_id: int, prompt_id: int) -> None:
        self.conn.execute(
            "UPDATE pending SET prompt_id=? WHERE id=?", (prompt_id, pending_id))
        self.conn.commit()

    def update_pending_payload(self, pending_id: int, payload: dict) -> None:
        self.conn.execute(
            "UPDATE pending SET payload=? WHERE id=?",
            (json.dumps(payload, ensure_ascii=False), pending_id),
        )
        self.conn.commit()

    def close_pending(self, pending_id: int, status: str) -> None:
        self.conn.execute(
            "UPDATE pending SET status=? WHERE id=?", (status, pending_id))
        self.conn.commit()

    def expire_pending(self, older_than_sec: int) -> list[dict]:
        """Заявки, на которые никто не ответил, — их пора убрать из чата."""
        cutoff = int(time.time()) - older_than_sec
        rows = self.conn.execute(
            "SELECT * FROM pending WHERE account_id=? AND status='waiting'"
            " AND created_at < ?",
            (self.account_id, cutoff),
        ).fetchall()
        out = []
        for r in rows:
            self.conn.execute(
                "UPDATE pending SET status='expired' WHERE id=?", (r["id"],))
            d = dict(r)
            d["payload"] = json.loads(d["payload"])
            out.append(d)
        self.conn.commit()
        return out

    # ================= фиксации =================

    def log_fixation(self, **kw) -> int:
        kw.setdefault("created_at", int(time.time()))
        kw["account_id"] = self.account_id
        cols = ",".join(kw)
        marks = ",".join("?" * len(kw))
        cur = self.conn.execute(
            f"INSERT INTO fixations ({cols}) VALUES ({marks})", tuple(kw.values())
        )
        self.conn.commit()
        return cur.lastrowid

    def get_fixation(self, fixation_id: int) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM fixations WHERE id=? AND account_id=?",
            (fixation_id, self.account_id),
        ).fetchone()

    def set_watching(self, fixation_id: int, on: bool = True) -> None:
        self.conn.execute(
            "UPDATE fixations SET watching=? WHERE id=? AND account_id=?",
            (int(on), fixation_id, self.account_id),
        )
        self.conn.commit()

    def agent_fixations(self, telegram_id: int, limit: int = 20) -> list[sqlite3.Row]:
        """Фиксации конкретного агента — для личного кабинета."""
        return self.conn.execute(
            "SELECT * FROM fixations"
            " WHERE account_id=? AND agent_telegram_id=? AND amo_lead_id IS NOT NULL"
            " ORDER BY created_at DESC LIMIT ?",
            (self.account_id, telegram_id, limit),
        ).fetchall()

    def watched_leads(self) -> list[sqlite3.Row]:
        """
        Сделки, за которыми следим.

        Берём все созданные ботом: даже если агент не подписался, движение
        по сделке нужно заметить — чтобы оповестить конкурентов о брони.
        """
        return self.conn.execute(
            "SELECT id, amo_lead_id, agent_telegram_id, agency_id, digits,"
            "       client_name, last_status_id, last_pipeline_id, watching"
            " FROM fixations"
            " WHERE account_id=? AND amo_lead_id IS NOT NULL",
            (self.account_id,),
        ).fetchall()

    def expiring_fixations(self, ttl_days: int, warn_days: int,
                           now: int | None = None) -> list[sqlite3.Row]:
        """
        Фиксации, у которых скоро истекает срок и о которых ещё не напоминали.

        Срок считается от последнего продления, а если продлений не было —
        от создания. `reminded_for` хранит момент, о котором уже сообщали:
        после продления он перестаёт совпадать, и напоминание придёт снова.
        """
        now = now or int(time.time())
        deadline_cut = now + warn_days * 86400
        return self.conn.execute(
            "SELECT *, COALESCE(renewed_at, created_at) + ? AS expires_at"
            " FROM fixations"
            " WHERE account_id=? AND amo_lead_id IS NOT NULL"
            "   AND agent_telegram_id IS NOT NULL"
            "   AND COALESCE(renewed_at, created_at) + ? <= ?"
            "   AND (reminded_for IS NULL"
            "        OR reminded_for <> COALESCE(renewed_at, created_at))",
            (ttl_days * 86400, self.account_id, ttl_days * 86400, deadline_cut),
        ).fetchall()

    def mark_reminded(self, fixation_id: int) -> None:
        self.conn.execute(
            "UPDATE fixations SET reminded_for=COALESCE(renewed_at, created_at)"
            " WHERE id=? AND account_id=?",
            (fixation_id, self.account_id),
        )
        self.conn.commit()

    def renew_fixation(self, fixation_id: int, when_ts: int | None = None) -> int:
        """Продлевает фиксацию: срок начинает течь заново."""
        ts = when_ts or int(time.time())
        self.conn.execute(
            "UPDATE fixations SET renewed_at=?, reminded_for=NULL"
            " WHERE id=? AND account_id=?",
            (ts, fixation_id, self.account_id),
        )
        self.conn.commit()
        return ts

    def mark_retail_notified(self, fixation_id: int) -> None:
        self.conn.execute(
            "UPDATE fixations SET retail_notified=1 WHERE id=? AND account_id=?",
            (fixation_id, self.account_id),
        )
        self.conn.commit()

    def fixations_awaiting_retail_check(self) -> list[sqlite3.Row]:
        """Живые фиксации, по которым ещё не сообщали о прямом отделе."""
        return self.conn.execute(
            "SELECT * FROM fixations"
            " WHERE account_id=? AND amo_lead_id IS NOT NULL"
            "   AND agent_telegram_id IS NOT NULL"
            "   AND COALESCE(retail_notified, 0) = 0",
            (self.account_id,),
        ).fetchall()

    def update_fixation_status(self, fixation_id: int, status_id: int | None,
                               pipeline_id: int | None) -> None:
        self.conn.execute(
            "UPDATE fixations SET last_status_id=?, last_pipeline_id=?"
            " WHERE id=? AND account_id=?",
            (status_id, pipeline_id, fixation_id, self.account_id),
        )
        self.conn.commit()

    def rivals_for(self, digits: str, exclude_agency_id: int | None,
                   ) -> list[sqlite3.Row]:
        """
        Агенты других агентств, фиксировавшие этого же клиента.

        Нужны, чтобы сообщить им о выходе клиента на бронь: иначе они
        продолжат вкладываться в заведомо проигранного клиента.
        """
        from phones import MAX_MISSING, compare_prefix, from_digits

        p = from_digits(digits)
        short = p.digits[:max(p.expected - MAX_MISSING, 1)]
        rows = self.conn.execute(
            "SELECT DISTINCT agent_telegram_id, agency_id, digits, client_name"
            " FROM fixations"
            " WHERE account_id=? AND digits LIKE ? AND amo_lead_id IS NOT NULL"
            "   AND agent_telegram_id IS NOT NULL",
            (self.account_id, short + "%"),
        ).fetchall()
        return [r for r in rows
                if r["agency_id"] != exclude_agency_id
                and compare_prefix(p, from_digits(r["digits"]))]

    def status_title(self, pipeline_id: int | None,
                     status_id: int | None) -> str | None:
        if not status_id:
            return None
        row = self.conn.execute(
            "SELECT name FROM statuses WHERE account_id=? AND status_id=?"
            + (" AND pipeline_id=?" if pipeline_id else ""),
            ((self.account_id, status_id, pipeline_id) if pipeline_id
             else (self.account_id, status_id)),
        ).fetchone()
        return row["name"] if row else None

    # ================= подключение новых клиентов =================
    #
    # Эти таблицы живут в базе оператора и к конкретному застройщику
    # не относятся, поэтому account_id тут нет.

    def create_invite(self, code: str, created_by: int,
                      ttl_hours: int = 24) -> None:
        now = int(time.time())
        self.conn.execute(
            "INSERT OR REPLACE INTO invites (code, created_by, expires_at,"
            " created_at) VALUES (?,?,?,?)",
            (code, created_by, now + ttl_hours * 3600, now),
        )
        self.conn.commit()

    def use_invite(self, code: str, user_id: int) -> str | None:
        """
        Помечает приглашение использованным.

        Возвращает причину отказа или None, если всё в порядке. Ссылка
        одноразовая: иначе кто угодно, получив её, начал бы подключать
        своих застройщиков.
        """
        row = self.conn.execute(
            "SELECT * FROM invites WHERE code=?", (code,)).fetchone()
        if not row:
            return "not_found"
        if row["used_by"] and row["used_by"] != user_id:
            return "used"
        if row["expires_at"] < int(time.time()):
            return "expired"
        self.conn.execute(
            "UPDATE invites SET used_by=?, used_at=? WHERE code=?",
            (user_id, int(time.time()), code))
        self.conn.commit()
        return None

    def start_onboarding(self, code: str | None, user_id: int,
                         username: str | None, user_name: str | None) -> int:
        # Незавершённые попытки этого же человека закрываем: иначе
        # он застрянет между двумя диалогами.
        self.conn.execute(
            "UPDATE onboardings SET status='rejected'"
            " WHERE user_id=? AND status='active'", (user_id,))
        now = int(time.time())
        cur = self.conn.execute(
            "INSERT INTO onboardings (code, user_id, username, user_name,"
            " created_at, updated_at) VALUES (?,?,?,?,?,?)",
            (code, user_id, username, user_name, now, now),
        )
        self.conn.commit()
        return cur.lastrowid

    def active_onboarding(self, user_id: int) -> dict | None:
        row = self.conn.execute(
            "SELECT * FROM onboardings WHERE user_id=? AND status='active'"
            " ORDER BY id DESC LIMIT 1", (user_id,)).fetchone()
        return self._onb(row)

    def get_onboarding(self, onb_id: int) -> dict | None:
        return self._onb(self.conn.execute(
            "SELECT * FROM onboardings WHERE id=?", (onb_id,)).fetchone())

    @staticmethod
    def _onb(row) -> dict | None:
        if not row:
            return None
        out = dict(row)
        out["data"] = json.loads(out["data"] or "{}")
        return out

    def update_onboarding(self, onb_id: int, step: str | None = None,
                          data: dict | None = None, status: str | None = None,
                          slug: str | None = None) -> None:
        sets, args = ["updated_at=?"], [int(time.time())]
        if step is not None:
            sets.append("step=?")
            args.append(step)
        if data is not None:
            sets.append("data=?")
            args.append(json.dumps(data, ensure_ascii=False))
        if status is not None:
            sets.append("status=?")
            args.append(status)
        if slug is not None:
            sets.append("slug=?")
            args.append(slug)
        args.append(onb_id)
        self.conn.execute(
            f"UPDATE onboardings SET {', '.join(sets)} WHERE id=?", args)
        self.conn.commit()

    def pending_onboardings(self) -> list[dict]:
        """Заявки, ждущие разворачивания оператором."""
        return [self._onb(r) for r in self.conn.execute(
            "SELECT * FROM onboardings WHERE status='ready' ORDER BY id")]

    # ================= сотрудники застройщика =================

    def add_staff(self, telegram_id: int, username: str | None,
                  name: str | None, added_by: int) -> bool:
        """True, если человек добавлен впервые."""
        cur = self.conn.execute(
            "INSERT OR IGNORE INTO staff (account_id, telegram_id, username,"
            " name, added_by, created_at) VALUES (?,?,?,?,?,?)",
            (self.account_id, telegram_id, username, name, added_by,
             int(time.time())),
        )
        self.conn.commit()
        return cur.rowcount > 0

    def remove_staff(self, telegram_id: int) -> None:
        self.conn.execute(
            "DELETE FROM staff WHERE account_id=? AND telegram_id=?",
            (self.account_id, telegram_id),
        )
        self.conn.commit()

    def list_staff(self) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM staff WHERE account_id=? ORDER BY created_at",
            (self.account_id,),
        ).fetchall()

    def is_staff(self, telegram_id: int) -> bool:
        return self.conn.execute(
            "SELECT 1 FROM staff WHERE account_id=? AND telegram_id=?",
            (self.account_id, telegram_id),
        ).fetchone() is not None

    # ================= статистика для владельца =================

    def fixations_count(self, days: int | None = None,
                        only_created: bool = True) -> int:
        sql = "SELECT COUNT(*) c FROM fixations WHERE account_id=?"
        args: list = [self.account_id]
        if only_created:
            sql += " AND amo_lead_id IS NOT NULL"
        if days:
            sql += " AND created_at > ?"
            args.append(int(time.time()) - days * 86400)
        return self.conn.execute(sql, args).fetchone()["c"]

    def fixations_by_agency(self, days: int | None = None,
                            limit: int = 15) -> list[tuple[str, int]]:
        sql = ("SELECT COALESCE(a.name, '— без агентства') AS name,"
               "       COUNT(*) AS c"
               " FROM fixations f LEFT JOIN agencies a ON a.id = f.agency_id"
               " WHERE f.account_id=? AND f.amo_lead_id IS NOT NULL")
        args: list = [self.account_id]
        if days:
            sql += " AND f.created_at > ?"
            args.append(int(time.time()) - days * 86400)
        sql += " GROUP BY name ORDER BY c DESC LIMIT ?"
        args.append(limit)
        return [(r["name"], r["c"]) for r in self.conn.execute(sql, args)]

    def fixations_by_agent(self, days: int | None = None,
                           limit: int = 10) -> list[tuple[str, int]]:
        sql = ("SELECT COALESCE(agent_name, '—') AS name, COUNT(*) AS c"
               " FROM fixations WHERE account_id=? AND amo_lead_id IS NOT NULL")
        args: list = [self.account_id]
        if days:
            sql += " AND created_at > ?"
            args.append(int(time.time()) - days * 86400)
        sql += " GROUP BY agent_telegram_id ORDER BY c DESC LIMIT ?"
        args.append(limit)
        return [(r["name"], r["c"]) for r in self.conn.execute(sql, args)]

    def rejected_by_verdict(self, days: int | None = None) -> list[tuple[str, int]]:
        sql = ("SELECT verdict, COUNT(*) c FROM fixations"
               " WHERE account_id=? AND amo_lead_id IS NULL")
        args: list = [self.account_id]
        if days:
            sql += " AND created_at > ?"
            args.append(int(time.time()) - days * 86400)
        sql += " GROUP BY verdict ORDER BY c DESC"
        return [(r["verdict"] or "—", r["c"]) for r in self.conn.execute(sql, args)]

    def agents_summary(self) -> dict:
        q = lambda sql: self.conn.execute(  # noqa: E731
            sql, (self.account_id,)).fetchone()["c"]
        return {
            "total": q("SELECT COUNT(*) c FROM agents WHERE account_id=?"),
            "subscribed": q("SELECT COUNT(*) c FROM agents"
                            " WHERE account_id=? AND dm_open=1"),
            "with_phone": q("SELECT COUNT(*) c FROM agents"
                            " WHERE account_id=? AND phone IS NOT NULL"),
        }

    def connected_chats(self) -> list[tuple[int, str | None]]:
        """Подключённые группы: (chat_id, название агентства)."""
        rows = self.conn.execute(
            "SELECT key, value FROM meta"
            " WHERE account_id=? AND key LIKE 'chat_agency:%'",
            (self.account_id,),
        ).fetchall()
        out = []
        for r in rows:
            if not r["value"] or not r["value"].isdigit():
                continue
            agency = self.get_agency(int(r["value"]))
            out.append((int(r["key"].split(":", 1)[1]),
                        agency["name"] if agency else None))
        return out

    # ================= рассылки =================

    def create_broadcast(self, admin_id: int, src_chat_id: int,
                         message_ids: list[int], items: list[dict],
                         html: str) -> int:
        cur = self.conn.execute(
            "INSERT INTO broadcasts (account_id, admin_id, src_chat_id,"
            " message_ids, items, html, created_at) VALUES (?,?,?,?,?,?,?)",
            (self.account_id, admin_id, src_chat_id,
             json.dumps(message_ids), json.dumps(items, ensure_ascii=False),
             html, int(time.time())),
        )
        self.conn.commit()
        return cur.lastrowid

    def get_broadcast(self, bid: int) -> dict | None:
        row = self.conn.execute(
            "SELECT * FROM broadcasts WHERE id=? AND account_id=?",
            (bid, self.account_id),
        ).fetchone()
        if not row:
            return None
        out = dict(row)
        for key in ("message_ids", "items", "target"):
            out[key] = json.loads(out[key]) if out[key] else None
        return out

    def update_broadcast(self, bid: int, **fields) -> None:
        allowed = {"html_en", "target", "status", "sent", "failed"}
        fields = {k: v for k, v in fields.items() if k in allowed}
        if not fields:
            return
        if isinstance(fields.get("target"), (dict, list)):
            fields["target"] = json.dumps(fields["target"], ensure_ascii=False)
        sets = ", ".join(f"{k}=?" for k in fields)
        self.conn.execute(
            f"UPDATE broadcasts SET {sets} WHERE id=? AND account_id=?",
            (*fields.values(), bid, self.account_id),
        )
        self.conn.commit()

    def broadcast_recipients(self, target: dict) -> list[sqlite3.Row]:
        """
        Кому уйдёт рассылка.

        Только тем, кто нажал Start в личке и не отписался: писать первым
        Telegram не даёт, а насильно возвращать отписавшихся нельзя.
        """
        sql = ("SELECT * FROM agents WHERE account_id=? AND dm_open=1"
               " AND COALESCE(bcast, 1) = 1")
        args: list = [self.account_id]

        if target.get("agency_id"):
            sql += " AND agency_id=?"
            args.append(target["agency_id"])

        if target.get("active_days"):
            sql += (" AND telegram_id IN (SELECT DISTINCT agent_telegram_id"
                    " FROM fixations WHERE account_id=? AND created_at > ?)")
            args += [self.account_id,
                     int(time.time()) - target["active_days"] * 86400]

        return self.conn.execute(sql, args).fetchall()

    def broadcast_chats(self, target: dict) -> list[tuple[int, int | None]]:
        """Рабочие чаты для рассылки: (chat_id, agency_id)."""
        rows = self.conn.execute(
            "SELECT key, value FROM meta WHERE account_id=? AND key LIKE 'chat_agency:%'",
            (self.account_id,),
        ).fetchall()
        out = []
        for r in rows:
            if not r["value"]:
                continue
            chat_id = int(r["key"].split(":", 1)[1])
            agency_id = int(r["value"]) if r["value"].isdigit() else None
            if target.get("agency_id") and agency_id != target["agency_id"]:
                continue
            out.append((chat_id, agency_id))
        return out

    def stats(self) -> dict:
        q = lambda sql: self.conn.execute(  # noqa: E731
            sql, (self.account_id,)
        ).fetchone()["c"]
        return {
            "phones": q("SELECT COUNT(*) c FROM contacts WHERE account_id=?"),
            "origins": q("SELECT COUNT(*) c FROM contact_origin WHERE account_id=?"),
            "companies": q("SELECT COUNT(*) c FROM companies WHERE account_id=?"),
            "agencies": q("SELECT COUNT(*) c FROM agencies WHERE account_id=?"),
            "fixations": q("SELECT COUNT(*) c FROM fixations WHERE account_id=?"),
            "agents": q("SELECT COUNT(*) c FROM agents WHERE account_id=?"),
        }

    # ================= meta =================

    def get_meta(self, key: str) -> str | None:
        row = self.conn.execute(
            "SELECT value FROM meta WHERE account_id=? AND key=?",
            (self.account_id, key),
        ).fetchone()
        return row["value"] if row else None

    def set_meta(self, key: str, value: str) -> None:
        self.conn.execute(
            "INSERT INTO meta (account_id, key, value) VALUES (?,?,?)"
            " ON CONFLICT(account_id, key) DO UPDATE SET value=excluded.value",
            (self.account_id, key, value),
        )
        self.conn.commit()

    # ================= обслуживание =================
    #
    # Только у оператора. Клиентские базы отсюда не трогаются вовсе:
    # у каждого файла должен быть один хозяин.

    def set_billing(self, slug: str, *, start_date: str,
                    threshold: int = 100, low: int = 40, high: int = 70,
                    currency: str = "USD") -> None:
        self.conn.execute(
            "INSERT INTO billing (slug, start_date, threshold, low, high,"
            " currency) VALUES (?,?,?,?,?,?)"
            " ON CONFLICT(slug) DO UPDATE SET start_date=excluded.start_date,"
            "  threshold=excluded.threshold, low=excluded.low,"
            "  high=excluded.high, currency=excluded.currency",
            (slug, start_date, threshold, low, high, currency))
        self.conn.commit()

    def get_billing(self, slug: str):
        return self.conn.execute(
            "SELECT * FROM billing WHERE slug=?", (slug,)).fetchone()

    def all_billing(self) -> list:
        return list(self.conn.execute(
            "SELECT * FROM billing WHERE enabled=1 ORDER BY slug"))

    def set_wallet(self, slug: str, wallet: str, note: str = "",
                   qr: str | None = None) -> None:
        """
        Реквизиты живут в базе, а не в настройках: кошелёк меняется,
        а `.env` лежит рядом с кодом и правится только руками на сервере.
        """
        self.conn.execute(
            "UPDATE billing SET wallet=?, wallet_note=?,"
            " wallet_qr=COALESCE(?, wallet_qr) WHERE slug=?",
            (wallet, note, qr, slug))
        self.conn.commit()

    def set_paused(self, slug: str, paused: bool) -> None:
        self.conn.execute("UPDATE billing SET paused=? WHERE slug=?",
                          (int(paused), slug))
        self.conn.commit()

    # ---------- периоды ----------

    def period(self, slug: str, due: str, begin: str = ""):
        """Строка периода, создавая её при первом обращении."""
        row = self.conn.execute(
            "SELECT * FROM billing_periods WHERE slug=? AND due=?",
            (slug, due)).fetchone()
        if row is None:
            self.conn.execute(
                "INSERT INTO billing_periods (slug, due, begin)"
                " VALUES (?,?,?)", (slug, due, begin or due))
            self.conn.commit()
            row = self.conn.execute(
                "SELECT * FROM billing_periods WHERE slug=? AND due=?",
                (slug, due)).fetchone()
        return row

    def mark_period(self, slug: str, due: str, **fields) -> None:
        allowed = {"fixations", "amount", "announced", "prepared",
                   "invoice_sent", "reminded", "warned", "paid_at",
                   "last_nudge"}
        bad = set(fields) - allowed
        if bad:
            raise ValueError(f"неизвестные поля периода: {sorted(bad)}")
        sets = ", ".join(f"{k}=?" for k in fields)
        self.conn.execute(
            f"UPDATE billing_periods SET {sets} WHERE slug=? AND due=?",
            (*fields.values(), slug, due))
        self.conn.commit()

    def close_period(self, slug: str, due: str, paid_at: int) -> None:
        """Отмечает период оплаченным и двигает клиента к следующему."""
        self.conn.execute(
            "UPDATE billing_periods SET paid_at=? WHERE slug=? AND due=?",
            (paid_at, slug, due))
        self.conn.execute(
            "UPDATE billing SET closed_due=?, paused=0 WHERE slug=?",
            (due, slug))
        self.conn.commit()

    def period_history(self, slug: str, limit: int = 12) -> list:
        return list(self.conn.execute(
            "SELECT * FROM billing_periods WHERE slug=?"
            " ORDER BY due DESC LIMIT ?", (slug, limit)))

    # ================= чаты =================

    def see_chat(self, chat_id: int, title: str | None = None,
                 is_admin: bool | None = None) -> bool:
        """
        Отметить, что бот видел этот чат. Возвращает True, если впервые.

        Названия групп меняются, поэтому обновляем каждый раз. Счётчик
        сообщений нужен, чтобы в списке живые чаты были отличимы от
        случайно задетых. Признак «впервые» — чтобы сказать владельцу
        про новую группу ровно один раз, а не при каждом сообщении.
        """
        first = self.get_chat(chat_id) is None
        now = int(time.time())
        flag = None if is_admin is None else int(is_admin)
        self.conn.execute(
            "INSERT INTO chats (account_id, chat_id, title, is_admin,"
            " messages, first_seen, last_seen) VALUES (?,?,?,?,1,?,?)"
            " ON CONFLICT(account_id, chat_id) DO UPDATE SET"
            "  title=COALESCE(excluded.title, chats.title),"
            "  is_admin=COALESCE(?, chats.is_admin),"
            "  messages=chats.messages + 1,"
            "  last_seen=excluded.last_seen",
            (self.account_id, chat_id, title, flag, now, now, flag),
        )
        self.conn.commit()
        return first

    def get_chat(self, chat_id: int):
        return self.conn.execute(
            "SELECT * FROM chats WHERE account_id=? AND chat_id=?",
            (self.account_id, chat_id)).fetchone()

    def list_chats(self, limit: int = 200) -> list:
        return list(self.conn.execute(
            "SELECT * FROM chats WHERE account_id=?"
            " ORDER BY last_seen DESC LIMIT ?", (self.account_id, limit)))

    def chat_agency_id(self, chat_id: int) -> int | None:
        raw = self.get_meta(f"chat_agency:{chat_id}")
        return int(raw) if raw and raw.isdigit() else None

    # ================= частные агенты =================

    def set_agent_status(self, telegram_id: int, status: str,
                         intro_name: str | None = None) -> None:
        self.conn.execute(
            "UPDATE agents SET status=?,"
            " intro_name=COALESCE(?, intro_name)"
            " WHERE account_id=? AND telegram_id=?",
            (status, intro_name, self.account_id, telegram_id))
        self.conn.commit()

    def pending_agents(self) -> list:
        """Заявки частников, ждущие владельца."""
        return list(self.conn.execute(
            "SELECT * FROM agents WHERE account_id=? AND status='pending'"
            " ORDER BY created_at", (self.account_id,)))

    def create_private_agency(self, full_name: str) -> int:
        """
        Заводит частника обычным агентством с пометкой «частный».

        Название — его ФИО: так он попадёт в CRM отдельной компанией
        и будет виден в статистике наравне с агентствами.
        """
        from agencies import norm_agency, pretty_name

        name = pretty_name(full_name)
        aid = self.create_agency(name, norm_agency(name))
        self.conn.execute(
            "UPDATE agencies SET private=1 WHERE id=?", (aid,))
        self.conn.commit()
        return aid

    def is_private_agency(self, agency_id: int) -> bool:
        row = self.conn.execute(
            "SELECT private FROM agencies WHERE id=?", (agency_id,)).fetchone()
        return bool(row and row["private"])
