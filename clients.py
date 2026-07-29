"""
Сводка по всем клиентам оператора.

Каждый застройщик — отдельный процесс со своей базой, поэтому его бот
про соседей ничего не знает. Но все базы лежат на одном сервере, так что
сводку можно собрать, просто прочитав их.

Читаем строго **только на чтение**: подключаемся в режиме `mode=ro`,
никаких записей. Чужой бот в это время работает, и лезть в его данные
на запись нельзя.

Папка клиентов задаётся в CLIENTS_DIR и выглядит так:

    /opt/fixbot/clients/
      breig/   .env  fixbot.db
      friend/  .env  fixbot.db
"""

from __future__ import annotations

import logging
import re
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)

DAY = 86400


@dataclass
class ClientInfo:
    """Что удалось узнать об одном клиенте."""

    slug: str
    name: str
    subdomain: str | None = None
    fixations_total: int = 0
    fixations_30: int = 0
    fixations_7: int = 0
    agencies: int = 0
    agents: int = 0
    agents_subscribed: int = 0
    chats: int = 0
    last_sync: int | None = None
    error: str | None = None

    @property
    def alive_hint(self) -> str:
        """
        Живой ли бот. Точно узнать из базы нельзя, но синхронизация
        идёт каждые полчаса — по её давности видно достаточно.
        """
        if self.error:
            return "❓"
        if not self.last_sync:
            return "⚠️"
        age = time.time() - self.last_sync
        if age < 2 * 3600:
            return "🟢"
        if age < 24 * 3600:
            return "🟡"
        return "🔴"


def _developer_name(env_path: Path) -> str | None:
    """
    Достаёт из .env только название застройщика.

    Читаем одну строку и ничего больше: в файле лежат токены, и тащить
    их в память без нужды незачем.
    """
    try:
        for line in env_path.read_text(encoding="utf-8").splitlines():
            m = re.match(r"\s*DEVELOPER_NAME\s*=\s*(.*)", line)
            if m:
                return m.group(1).strip().strip('"\'') or None
    except OSError:
        pass
    return None


def _open_ro(path: Path) -> sqlite3.Connection:
    """Открывает базу только на чтение — её пишет чужой процесс."""
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5)
    conn.row_factory = sqlite3.Row
    return conn


def _count(conn: sqlite3.Connection, sql: str, args: tuple = ()) -> int:
    try:
        return conn.execute(sql, args).fetchone()[0] or 0
    except sqlite3.Error:
        return 0


def read_client(folder: Path) -> ClientInfo:
    db_path = folder / "fixbot.db"
    info = ClientInfo(slug=folder.name,
                      name=_developer_name(folder / ".env") or folder.name)

    if not db_path.exists():
        info.error = "база не найдена"
        return info

    try:
        conn = _open_ro(db_path)
    except sqlite3.Error as e:
        info.error = str(e)[:80]
        return info

    try:
        now = int(time.time())
        info.fixations_total = _count(
            conn, "SELECT COUNT(*) FROM fixations WHERE amo_lead_id IS NOT NULL")
        info.fixations_30 = _count(
            conn, "SELECT COUNT(*) FROM fixations"
                  " WHERE amo_lead_id IS NOT NULL AND created_at > ?",
            (now - 30 * DAY,))
        info.fixations_7 = _count(
            conn, "SELECT COUNT(*) FROM fixations"
                  " WHERE amo_lead_id IS NOT NULL AND created_at > ?",
            (now - 7 * DAY,))
        info.agencies = _count(conn, "SELECT COUNT(*) FROM agencies")
        info.agents = _count(conn, "SELECT COUNT(*) FROM agents")
        info.agents_subscribed = _count(
            conn, "SELECT COUNT(*) FROM agents WHERE dm_open=1")
        info.chats = _count(
            conn, "SELECT COUNT(*) FROM meta"
                  " WHERE key LIKE 'chat_agency:%' AND value <> ''")

        row = conn.execute(
            "SELECT value FROM meta WHERE key='contacts_synced_at' LIMIT 1"
        ).fetchone()
        if row and str(row["value"]).isdigit():
            info.last_sync = int(row["value"])

        sub = conn.execute(
            "SELECT subdomain FROM accounts LIMIT 1").fetchone()
        if sub:
            info.subdomain = sub["subdomain"]
    except sqlite3.Error as e:
        info.error = str(e)[:80]
    finally:
        conn.close()

    return info


def scan(clients_dir: str | Path) -> list[ClientInfo]:
    """Все клиенты в папке, по алфавиту."""
    root = Path(clients_dir).expanduser()
    if not root.is_dir():
        return []
    out = [read_client(p) for p in sorted(root.iterdir()) if p.is_dir()]
    return out


# --------------------------------------------------------------------------
# Тексты
# --------------------------------------------------------------------------

def overview_text(clients: list[ClientInfo], clients_dir: str) -> str:
    if not clients:
        return ("🗂 <b>Мои клиенты</b>\n\n"
                f"В папке <code>{clients_dir or '—'}</code> ничего не нашлось.\n\n"
                "Ожидается раскладка вида:\n"
                "<code>clients/breig/fixbot.db\n"
                "clients/friend/fixbot.db</code>\n\n"
                "Путь задаётся переменной <code>CLIENTS_DIR</code>.")

    total_fix = sum(c.fixations_total for c in clients)
    total_30 = sum(c.fixations_30 for c in clients)
    total_ag = sum(c.agents for c in clients)

    lines = ["🗂 <b>Мои клиенты</b>", "",
             f"Клиентов: <b>{len(clients)}</b>",
             f"Фиксаций всего: <b>{total_fix}</b>, за 30 дней: <b>{total_30}</b>",
             f"Агентов в базах: <b>{total_ag}</b>", ""]

    for c in clients:
        lines.append(f"{c.alive_hint} <b>{c.name}</b>")
        if c.error:
            lines.append(f"      ⚠️ {c.error}")
            continue
        lines.append(f"      фиксаций: {c.fixations_total} "
                     f"(30 дн. {c.fixations_30}, 7 дн. {c.fixations_7})")
        lines.append(f"      агентств: {c.agencies} · групп: {c.chats} · "
                     f"агентов: {c.agents} (подписано {c.agents_subscribed})")

    lines += ["", "<i>🟢 синхронизация свежая · 🟡 больше суток назад · "
                  "🔴 давно не было — возможно бот остановлен</i>"]
    return "\n".join(lines)


def client_text(c: ClientInfo) -> str:
    if c.error:
        return f"<b>{c.name}</b>\n\n⚠️ {c.error}"

    import datetime as dt

    def when(ts: int | None) -> str:
        return (dt.datetime.fromtimestamp(ts).strftime("%d.%m.%Y %H:%M")
                if ts else "не было")

    return "\n".join([
        f"{c.alive_hint} <b>{c.name}</b>",
        f"<i>папка: {c.slug}</i>",
        f"<i>amoCRM: {c.subdomain or '—'}</i>",
        "",
        "<b>Фиксации</b>",
        f"за 7 дней: {c.fixations_7}",
        f"за 30 дней: {c.fixations_30}",
        f"всего: {c.fixations_total}",
        "",
        "<b>Подключено</b>",
        f"агентств: {c.agencies}",
        f"групп: {c.chats}",
        f"агентов: {c.agents}, из них подписано: {c.agents_subscribed}",
        "",
        f"Последняя синхронизация: {when(c.last_sync)}",
    ])
