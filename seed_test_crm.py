"""
Наполняет ПУСТОЙ пробный amoCRM тестовыми данными под все семь сценариев.

Зачем: чтобы проверить отказ по клиенту отдела продаж и освобождение
остывшего клиента, нужны сделки с разными датами. Руками это заводить нудно
и легко ошибиться.

    python seed_test_crm.py            # показать, что будет создано
    python seed_test_crm.py --apply    # создать

⚠️ ЗАПУСКАТЬ ТОЛЬКО НА ПРОБНОМ АККАУНТЕ. Скрипт создаёт мусорные контакты
и сделки. В боевой CRM это не нужно, поэтому есть защита: он откажется
работать, если в аккаунте больше MAX_EXISTING контактов.

Что создаёт:

    Воронки            «Розница (тест)» и «Агентские (тест)»
    Агентства          Дом+, АН Новосёл  (компании)

    Свежий Розничный      +7 999 100-00-01  розница, активность сегодня
                          → 🔴 отказ: клиент отдела продаж
    Остывший Розничный    +7 999 100-00-02  розница, активность 2 года назад
                          → 🟢 освободился, можно фиксировать
    Чужой Агентский       +7 999 100-00-03  агентская сделка от Дом+
                          → 🟡 работать можно, эксклюзива нет
    Забронированный       +7 999 100-00-04  агентская сделка в статусе брони
                          → 🔴 конкуренция закрыта
    Ничей Контакт         +7 999 100-00-05  контакт без единой сделки
                          → 🟡 непонятно чей, на разбор

    Уникальным будет любой номер, которого здесь нет,
    например +7 999 100-00-99.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import time

from amo import AmoClient
from amoauth import build_auth
from config import cfg
from db import Db

DAY = 86400
NOW = int(time.time())

#: Если контактов больше — почти наверняка это боевая CRM, работать откажемся.
MAX_EXISTING = 50

PIPELINES = [
    ("Розница (тест)", ["Новое обращение", "Переговоры", "Договор"]),
    ("Агентские (тест)", ["Фиксация", "Показ", "Бронь", "Договор"]),
]

AGENCIES = ["Дом+", "АН Новосёл"]

CONTACTS = [
    # (имя, телефон, вид сделки, сдвиг активности, агентство, статус брони)
    ("Свежий Розничный", "+79991000001", "retail", 2 * DAY, None, False),
    ("Остывший Розничный", "+79991000002", "retail", 730 * DAY, None, False),
    ("Чужой Агентский", "+79991000003", "agency", 20 * DAY, "Дом+", False),
    ("Забронированный", "+79991000004", "agency", 10 * DAY, "АН Новосёл", True),
    ("Ничей Контакт", "+79991000005", None, 0, None, False),
]


def plan() -> str:
    lines = ["Будет создано в amoCRM:", ""]
    lines.append("Воронки:")
    for name, statuses in PIPELINES:
        lines.append(f"  • {name}: {' → '.join(statuses)}")
    lines.append("")
    lines.append("Компании-агентства:")
    for a in AGENCIES:
        lines.append(f"  • {a}")
    lines.append("")
    lines.append("Контакты и сделки:")
    for name, phone, kind, ago, agency, booked in CONTACTS:
        if kind is None:
            lines.append(f"  • {name:22} {phone}  без сделок")
            continue
        when = f"активность {ago // DAY} дн. назад"
        extra = f", агентство {agency}" if agency else ""
        extra += ", в статусе БРОНЬ" if booked else ""
        lines.append(f"  • {name:22} {phone}  {kind}, {when}{extra}")
    lines.append("")
    lines.append("Ожидаемые вердикты бота:")
    lines.append("  +7 999 100-00-01 → 🔴 клиент отдела продаж")
    lines.append("  +7 999 100-00-02 → 🟢 освободился (год без активности)")
    lines.append("  +7 999 100-00-03 → 🟡 чужая фиксация, работать можно")
    lines.append("  +7 999 100-00-04 → 🔴 уже на брони")
    lines.append("  +7 999 100-00-05 → 🟡 непонятно чей")
    lines.append("  +7 999 100-00-99 → 🟢 уникальный")
    return "\n".join(lines)


async def guard(amo: AmoClient) -> None:
    """Не даём запустить это на боевой CRM."""
    existing = await amo.dump_contacts()
    if len(existing) > MAX_EXISTING:
        print(f"\n⛔️ В аккаунте {len(existing)} контактов — это не похоже "
              f"на пустой пробный.\nСкрипт создаёт мусорные данные и на боевой "
              f"CRM запускаться не должен.\n"
              f"Если уверены — поднимите MAX_EXISTING в начале файла.")
        sys.exit(1)


async def apply() -> None:
    db = Db(cfg.db_path)
    db.upsert_account(cfg.amo_subdomain, cfg.amo_auth,
                      access_token=cfg.amo_token or None)
    amo = AmoClient(build_auth(cfg, db))

    try:
        await guard(amo)

        # --- воронки ---
        existing = {p["name"]: p for p in await amo.pipelines()}
        pipeline_ids: dict[str, int] = {}
        for name, statuses in PIPELINES:
            if name in existing:
                print(f"Воронка «{name}» уже есть, пропускаю")
                pipeline_ids[name] = existing[name]["id"]
                continue
            data = await amo._request("POST", "/api/v4/leads/pipelines", json=[{
                "name": name,
                "_embedded": {"statuses": [
                    {"name": s, "sort": (i + 1) * 10}
                    for i, s in enumerate(statuses)
                ]},
            }])
            pid = data["_embedded"]["pipelines"][0]["id"]
            pipeline_ids[name] = pid
            print(f"✓ воронка «{name}» (id {pid})")

        fresh = {p["id"]: p for p in await amo.pipelines()}

        def status_id(pipeline_name: str, status_name: str) -> int | None:
            pid = pipeline_ids[pipeline_name]
            for s in fresh[pid]["statuses"]:
                if s["name"] == status_name:
                    return s["id"]
            return None

        # --- агентства ---
        company_ids: dict[str, int] = {}
        for a in AGENCIES:
            cid = await amo.find_or_create_company(a)
            company_ids[a] = cid
            print(f"✓ компания «{a}» (id {cid})")

        # --- контакты и сделки ---
        for name, phone, kind, ago, agency, booked in CONTACTS:
            contact_id = await amo.create_contact(name=name, phone=phone)
            print(f"✓ контакт {name} (id {contact_id})")
            if kind is None:
                continue

            pname = "Розница (тест)" if kind == "retail" else "Агентские (тест)"
            sname = "Переговоры" if kind == "retail" else (
                "Бронь" if booked else "Фиксация")
            lead_id = await amo.create_lead(
                name=f"{name} — тест",
                contact_id=contact_id,
                company_id=company_ids.get(agency) if agency else None,
                pipeline_id=pipeline_ids[pname],
                status_id=status_id(pname, sname),
                tags=["seed-тест"],
            )
            print(f"  └ сделка в «{pname}» / «{sname}» (id {lead_id})")

            if ago:
                await amo.add_note(
                    "leads", lead_id,
                    f"Тестовые данные. Ожидаемая давность активности: "
                    f"{ago // DAY} дней назад.",
                )

        print("\n" + "=" * 62)
        print("Готово. Дальше:")
        print("  1. В боте: /pipelines → отметь «Розница (тест)» как 🏢, "
              "«Агентские (тест)» как 🤝")
        print("  2. В боте: /sync")
        print("  3. Прогони проверки — номера в шапке этого файла")
        print("\n⚠️ Важно про «Остывшего Розничного»: amoCRM ставит дату")
        print("   изменения сделки автоматически, задним числом её через API")
        print("   не выставить. Чтобы проверить сценарий с освобождением,")
        print("   временно поставь в .env RETAIL_TTL_DAYS=0 — тогда остывшими")
        print("   станут все розничные, и ты увидишь зелёный ответ.")
        print("   После проверки верни 365.")
    finally:
        await amo.close()


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--apply", action="store_true",
                    help="действительно создать данные")
    args = ap.parse_args()

    print(plan())
    if not args.apply:
        print("\nЭто предпросмотр. Чтобы создать: python seed_test_crm.py --apply")
        return

    print("\nСоздаю…\n")
    asyncio.run(apply())


if __name__ == "__main__":
    main()
