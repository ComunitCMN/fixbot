"""
Что на самом деле лежит в amoCRM: сделки, воронки, контакты без истории.

    python3 crm_stats.py

Скрипт только читает, ничего не меняет.

Зачем: бот видит 107 сделок на 6539 контактов, и непонятно, действительно
ли их так мало или API отдаёт не всё. Догадок несколько — закрытые сделки,
удалённые контакты, урезанные права интеграции, — и все они проверяются
цифрами.

Что покажет:
  • сколько сделок отдаёт API и как они разложены по воронкам и этапам;
  • сколько среди них закрытых («не реализовано») — их видно отдельно;
  • сколько контактов вообще без сделок и когда они заведены;
  • есть ли у таких контактов другие следы: теги, источник, ответственный.

По результату станет ясно, чинить права интеграции или менять способ
определения происхождения клиента.
"""

from __future__ import annotations

import asyncio
import datetime as dt
from collections import Counter, defaultdict

from amo import AmoClient
from amoauth import build_auth
from config import cfg
from db import Db

DAY = 86400


def when(ts) -> str:
    return dt.datetime.fromtimestamp(ts).strftime("%d.%m.%Y") if ts else "—"


def bar(n: int, total: int, width: int = 28) -> str:
    if not total:
        return ""
    filled = round(width * n / total)
    return "█" * filled + "·" * (width - filled)


async def main() -> None:
    db = Db(cfg.db_path)
    db.upsert_account(cfg.amo_subdomain, cfg.amo_auth,
                      access_token=cfg.amo_token or None)
    amo = AmoClient(build_auth(cfg, db))

    try:
        print("\nЧитаю amoCRM, это займёт пару минут…\n")

        pipelines = await amo.pipelines()
        pipe_name = {p["id"]: p["name"] for p in pipelines}
        status_name: dict[tuple[int, int], str] = {}
        for p in pipelines:
            for s in p["statuses"]:
                status_name[(p["id"], s["id"])] = s.get("name") or str(s["id"])

        kinds = db.pipeline_kinds()
        leads = await amo.dump_leads()
        contacts = await amo.dump_contacts()

        print("=" * 66)
        print("ОБЩИЕ ЦИФРЫ")
        print("=" * 66)
        print(f"  Контактов с телефоном:  {len(contacts)}")
        print(f"  Сделок отдаёт API:      {len(leads)}")
        print(f"  Воронок:                {len(pipelines)}")

        # ---------- сделки по воронкам ----------
        by_pipe: Counter = Counter()
        by_status: dict[int, Counter] = defaultdict(Counter)
        closed = 0
        for l in leads:  # noqa: E741
            by_pipe[l["pipeline_id"]] += 1
            by_status[l["pipeline_id"]][l["status_id"]] += 1
            if l["status_id"] in (142, 143):
                closed += 1

        print("\n" + "=" * 66)
        print("СДЕЛКИ ПО ВОРОНКАМ")
        print("=" * 66)
        for pid, n in by_pipe.most_common():
            kind = {"retail": "🏢 розница", "agency": "🤝 агентская",
                    "ignore": "🚫 игнор"}.get(kinds.get(pid, "unset"),
                                              "⬜️ не размечена")
            print(f"\n  {pipe_name.get(pid, pid)}  [{kind}]  — {n}")
            for sid, cnt in by_status[pid].most_common():
                mark = "  (закрыта)" if sid in (142, 143) else ""
                name = status_name.get((pid, sid), sid)
                print(f"      {cnt:>5}  {name}{mark}")

        print(f"\n  Из них закрытых и нереализованных: {closed} "
              f"({closed * 100 // max(len(leads), 1)}%)")

        # ---------- контакты без сделок ----------
        with_leads = set()
        for l in leads:  # noqa: E741
            with_leads.update(l.get("contact_ids") or [])

        orphans = [c for c in contacts if c["id"] not in with_leads]

        print("\n" + "=" * 66)
        print("КОНТАКТЫ БЕЗ СДЕЛОК")
        print("=" * 66)
        print(f"  Всего:        {len(orphans)} из {len(contacts)} "
              f"({len(orphans) * 100 // max(len(contacts), 1)}%)")
        print(f"  Со сделками:  {len(contacts) - len(orphans)}")
        print()
        print("  Для бота контакт без сделок — «непонятно чей», и по таким")
        print("  клиентам он отвечает жёлтым «нужна проверка».")

        # ---------- когда их заводили ----------
        now = int(dt.datetime.now().timestamp())
        buckets = {"за месяц": 0, "1–6 месяцев": 0, "6–12 месяцев": 0,
                   "больше года": 0, "дата неизвестна": 0}
        for c in orphans:
            ts = c.get("created_at")
            if not ts:
                buckets["дата неизвестна"] += 1
            elif now - ts < 30 * DAY:
                buckets["за месяц"] += 1
            elif now - ts < 180 * DAY:
                buckets["1–6 месяцев"] += 1
            elif now - ts < 365 * DAY:
                buckets["6–12 месяцев"] += 1
            else:
                buckets["больше года"] += 1

        print("\n  Когда заведены:")
        for label, n in buckets.items():
            if n:
                print(f"    {label:>16}  {n:>5}  {bar(n, len(orphans))}")

        print("\n  Примеры (первые 5):")
        for c in orphans[:5]:
            print(f"    id={c['id']:<10} {when(c.get('created_at'))}  "
                  f"{(c.get('name') or '—')[:32]}")

        # ---------- вывод ----------
        print("\n" + "=" * 66)
        print("ЧТО ЭТО ЗНАЧИТ")
        print("=" * 66)

        share = len(orphans) * 100 // max(len(contacts), 1)
        if share > 80:
            print(f"""
  {share}% контактов без единой сделки. Это не похоже на нормальную
  работу отдела продаж — скорее всего одно из двух:

  1. Контакты заливались импортом, а сделки по ним не заводились.
     Тогда определять происхождение по воронкам не выйдет: у большинства
     клиентов воронки просто нет. Нужен другой признак — например, поле
     «Источник» в карточке контакта или тег.

  2. Интеграция видит не все сделки. Права интеграции ограничивают
     выдачу API: если доступ дан не ко всем воронкам или не ко всем
     пользователям, часть сделок в ответ не попадает.

  Как отличить: открой amoCRM и посмотри общее число сделок в разделе
  «Сделки» по всем воронкам, сняв фильтры. Если там заметно больше
  {len(leads)} — режут права, лечится в настройках интеграции.
  Если примерно столько же — значит сделок правда мало.""")
        else:
            print(f"\n  {share}% контактов без сделок — в пределах нормы.")

        if closed and closed * 100 // max(len(leads), 1) > 50:
            print(f"""
  Больше половины сделок ({closed}) закрыты и не реализованы.
  Учитывать их как «клиент в работе у отдела продаж» неправильно:
  такой клиент фактически свободен. Стоит перестать считать закрытые
  сделки блокирующими — скажи, и я это добавлю.""")

        print()
    finally:
        await amo.close()


if __name__ == "__main__":
    asyncio.run(main())
