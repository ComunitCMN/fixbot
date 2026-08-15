"""
Выгрузка базы застройщика в Excel.

Решение — в РЕШЕНИЯ.md, раздел «Выгрузка базы застройщика в Excel».
Кнопку жмёт оператор, файл приходит в Telegram, работа фоновая.

**Модуль только читает.** Он не пишет ни в amoCRM, ни в базу и не знает
про `db` вовсе. Это главное свойство задачи: при таком ограничении
сломать выгрузкой работающие фиксации технически нечем. Закреплено
в `test_export.py`, первые три теста.

Зеркало здесь не участвует. Оно кормит проверку дублей и хранит по
контакту четыре галочки — ни сделок, ни этапов, ни причин отказа в нём
нет и не предполагается. Выгрузка обходит amoCRM сама.

Структура файла: лист на воронку, внутри этапы сверху вниз — успешно
реализованные, затем рабочие в обратном порядке воронки (ближе
к сделке — выше), внизу отказы с причиной.
"""

from __future__ import annotations

import datetime as dt
import re
from io import BytesIO

#: Системные этапы amoCRM: они есть в каждой воронке с этими же номерами.
WON = 142   # «Успешно реализовано»
LOST = 143  # «Закрыто и не реализовано»

COLUMNS = ("Клиент", "Телефон", "Этап", "Причина отказа", "Создана",
           "Изменена")

#: Предел строк на листе Excel. У крупного застройщика сделок в сотни раз
#: больше, чем у нового, и на большем openpyxl падает — файл не приходит
#: вовсе. Поэтому воронка при переполнении продолжается на следующем листе.
SHEET_ROW_LIMIT = 1_048_576

#: Знаки, которые Excel не берёт в название листа.
_BAD_TITLE = re.compile(r"[\[\]:*?/\\]+")


def _when(ts) -> str:
    if not ts:
        return ""
    return dt.datetime.fromtimestamp(int(ts)).strftime("%d.%m.%Y")


# ===================== порядок этапов =====================

def order_statuses(statuses: list[dict]) -> list[dict]:
    """
    Этапы одной воронки сверху вниз.

    Сверху то, ради чего всё делалось, внизу — отказы. Рабочие этапы
    в обратном порядке воронки: чем ближе к сделке, тем выше.
    """
    won = [s for s in statuses if s.get("id") == WON]
    lost = [s for s in statuses if s.get("id") == LOST]
    work = [s for s in statuses if s.get("id") not in (WON, LOST)]
    work.sort(key=lambda s: s.get("sort") or 0, reverse=True)
    return won + work + lost


# ===================== названия листов =====================

def sheet_names(pipelines: list[dict]) -> list[str]:
    """
    Названия листов: по одному на воронку, пригодные для Excel.

    Файл, который не открывается, — худшая поломка: она видна только
    у оператора и только после нескольких минут ожидания.
    """
    out: list[str] = []
    seen: set[str] = set()
    for p in pipelines:
        raw = (p.get("name") or "").strip() or f"Воронка {p.get('id', '')}".strip()
        title = _BAD_TITLE.sub(" ", raw)
        title = re.sub(r"\s+", " ", title).strip()[:31] or "Воронка"

        base = title
        n = 2
        while title.lower() in seen:
            suffix = f" ({n})"
            title = base[:31 - len(suffix)] + suffix
            n += 1
        seen.add(title.lower())
        out.append(title)
    return out


# ===================== строки листа =====================

def sheet_rows(pipeline: dict, leads: list[dict], contacts: dict) -> list[list]:
    """
    Строки одного листа. Строка — сделка: этап и причина отказа
    принадлежат сделке, а не человеку. Клиент с двумя сделками даст
    две строки с одним телефоном — так и задумано.
    """
    order = order_statuses(pipeline.get("statuses") or [])
    rank = {s.get("id"): i for i, s in enumerate(order)}
    names = {s.get("id"): s.get("name") or "" for s in order}
    unknown = len(order)

    mine = [x for x in leads if x.get("pipeline_id") == pipeline.get("id")]
    # Этап могли завести после того, как прочитали справочник воронок.
    # Такая сделка уходит вниз, но из файла не пропадает: молча терять
    # строки нельзя, неполноту никто не заметит.
    mine.sort(key=lambda x: (rank.get(x.get("status_id"), unknown),
                             -(x.get("updated_at") or 0)))

    rows: list[list] = []
    for x in mine:
        sid = x.get("status_id")
        contact = {}
        for cid in x.get("contact_ids") or []:
            if cid in contacts:
                contact = contacts[cid]
                break
        rows.append([
            contact.get("name") or "",
            contact.get("phone") or "",
            names.get(sid) or f"Этап {sid}",
            # Причину показываем только у отказных: amoCRM хранит её
            # и после возврата сделки в работу, и в живой строке она
            # соврала бы.
            (x.get("loss_reason") or "") if sid == LOST else "",
            _when(x.get("created_at")),
            _when(x.get("updated_at")),
        ])
    return rows


def split_rows(rows: list[list]) -> list[list[list]]:
    """Режет строки по вместимости листа. Заголовок занимает одну строку."""
    size = SHEET_ROW_LIMIT - 1
    if len(rows) <= size:
        return [rows]
    return [rows[i:i + size] for i in range(0, len(rows), size)]


# ===================== сбор данных =====================

async def collect(amo) -> dict:
    """
    Полный обход amoCRM. Только чтение: воронки, сделки, контакты.

    Минуты и тысячи запросов — поэтому вызывать это можно только
    из фоновой задачи.
    """
    pipelines = await amo.pipelines()
    leads = await amo.dump_leads_full()
    people = await amo.dump_contacts_full()

    contacts = {
        c["id"]: {"name": c.get("name") or "",
                  "phone": (c.get("phones") or [""])[0]}
        for c in people
    }
    return {"pipelines": pipelines, "leads": leads, "contacts": contacts}


# ===================== сам файл =====================

def build_workbook(pipelines: list[dict], leads: list[dict],
                   contacts: dict) -> bytes:
    """Excel-файл в память. Лист на воронку, строка — сделка."""
    from openpyxl import Workbook

    wb = Workbook(write_only=True)
    widths = (28, 20, 30, 30, 14, 14)

    for pipeline, title in zip(pipelines, sheet_names(pipelines)):
        rows = sheet_rows(pipeline, leads, contacts)
        for n, chunk in enumerate(split_rows(rows)):
            name = title if not n else f"{title[:31 - 4]} ({n + 1})"
            ws = wb.create_sheet(name)
            ws.freeze_panes = "A2"
            for col, width in enumerate(widths, start=1):
                ws.column_dimensions[chr(64 + col)].width = width
            ws.append(list(COLUMNS))
            for row in chunk:
                ws.append(row)

    buf = BytesIO()
    wb.save(buf)
    return buf.getvalue()


def file_name(developer: str | None = None, now: dt.datetime | None = None) -> str:
    who = _BAD_TITLE.sub("", (developer or "база")).strip().replace(" ", "-")
    stamp = (now or dt.datetime.now()).strftime("%Y-%m-%d")
    return f"{who}-{stamp}.xlsx"
