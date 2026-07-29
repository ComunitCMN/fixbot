"""
Диагностика одного номера: почему бот ответил так, а не иначе.

    python3 debug_check.py +79851475214

Смотрит в трёх местах и показывает всё, что нашёл:

  1. Нормализация — во что превратился номер, хватает ли цифр
  2. Локальное зеркало — знает ли бот про этот номер вообще
  3. Живая amoCRM — есть ли он в CRM прямо сейчас, в каких сделках и воронках

Главное, что помогает понять: если номер есть в CRM, но нет в зеркале —
значит зеркало устарело, нужен /sync. Если нет нигде — телефон записан
не в стандартное поле или в другом формате.

Бота останавливать не нужно, скрипт только читает.
"""

from __future__ import annotations

import asyncio
import sys

import phones
from amo import AmoClient
from amoauth import build_auth
from config import cfg
from db import Db
from verdict import decide


def line(char: str = "─", n: int = 62) -> None:
    print(char * n)


# Эти три вспомогательные функции живут здесь, а не в amo.py, чтобы скрипт
# работал с уже установленной версией бота и ничего в ней не требовал менять.

def contact_phones(contact: dict) -> list[str]:
    """Телефоны из стандартного поля PHONE."""
    return [
        v.get("value")
        for f in (contact.get("custom_fields_values") or [])
        if f.get("field_code") == "PHONE"
        for v in (f.get("values") or [])
        if v.get("value")
    ]


def all_field_values(contact: dict) -> list[tuple[str, str]]:
    """Все заполненные поля — чтобы найти телефон в нестандартном месте."""
    out = []
    for f in (contact.get("custom_fields_values") or []):
        for v in (f.get("values") or []):
            val = v.get("value")
            if val:
                out.append((f.get("field_name") or f.get("field_code") or "?",
                            str(val)))
    return out


async def contact_leads(amo: AmoClient, contact_id: int) -> list[dict]:
    """Сделки контакта вместе с воронкой и датой активности."""
    data = await amo._request(
        "GET", f"/api/v4/contacts/{contact_id}", params={"with": "leads"}
    )
    ids = [l["id"] for l in ((data.get("_embedded") or {}).get("leads") or [])]
    out = []
    for lid in ids[:20]:
        lead = await amo._request("GET", f"/api/v4/leads/{lid}")
        out.append({
            "id": lead["id"], "name": lead.get("name"),
            "pipeline_id": lead.get("pipeline_id"),
            "status_id": lead.get("status_id"),
            "updated_at": lead.get("updated_at") or lead.get("created_at"),
        })
    return out


async def main(raw: str) -> None:
    print()
    line("═")
    print(f"ДИАГНОСТИКА НОМЕРА: {raw}")
    line("═")

    # ---------- 1. нормализация ----------
    print("\n[1] НОРМАЛИЗАЦИЯ")
    p = phones.normalize(raw)
    if p is None:
        print("  ✗ не распознан как российский номер")
        print("    Проверь формат: +7..., 8..., 9...")
        return
    print(f"  цифры:        {p.digits}")
    print(f"  известно:     {p.known} из 11")
    print(f"  вид:          {p.pretty()}")
    print(f"  годен к поиску: {'да' if p.is_usable else 'НЕТ — мало цифр'}")
    if not p.is_usable:
        return

    db = Db(cfg.db_path)
    db.upsert_account(cfg.amo_subdomain, cfg.amo_auth,
                      access_token=cfg.amo_token or None)

    # ---------- 2. локальное зеркало ----------
    print("\n[2] ЛОКАЛЬНОЕ ЗЕРКАЛО БОТА")
    stats = db.stats()
    synced = db.get_meta("contacts_synced_at")
    print(f"  всего телефонов в зеркале:      {stats['phones']}")
    print(f"  контактов с происхождением:     {stats['origins']}")
    print(f"  последняя синхронизация:        "
          f"{__import__('datetime').datetime.fromtimestamp(int(synced)) if synced else 'не было'}")

    # Ищем по всё более коротким префиксам — чтобы увидеть «почти совпадения».
    print("\n  поиск по префиксам:")
    for length in (11, 10, 9, 7, 5):
        pref = p.digits[:length]
        if len(pref) < 5:
            continue
        rows = db.conn.execute(
            "SELECT contact_id, name, digits FROM contacts"
            " WHERE account_id=? AND digits LIKE ? LIMIT 5",
            (db.account_id, pref + "%"),
        ).fetchall()
        mark = "✓" if rows else "·"
        print(f"    {mark} {pref:11} → найдено {len(rows)}")
        for r in rows:
            print(f"        id={r['contact_id']:<10} {r['digits']:12} {r['name']}")
        if rows:
            break

    matches = db.find_matches(p)
    print(f"\n  совпадений по правилам бота: {len(matches)}")
    for m in matches:
        print(f"    • id={m.contact_id} {m.name}")
        print(f"      розница={m.has_retail} агентство={m.has_agency} "
              f"бронь={m.booked} происхождение_известно={m.origin_known}")

    d = decide(matches, agency_id=None, agent_telegram_id=None,
               retail_ttl_days=cfg.retail_ttl_days)
    print(f"\n  ВЕРДИКТ: {d.verdict.value}")

    # ---------- 3. живая amoCRM ----------
    print("\n[3] ЖИВОЙ ПОИСК В amoCRM")
    amo = AmoClient(build_auth(cfg, db))
    try:
        queries = [p.digits, raw.strip(), p.pretty(), p.digits[1:]]
        seen: set[int] = set()
        found_any = False

        for q in queries:
            try:
                res = await amo.search_contacts(q)
            except Exception as e:  # noqa: BLE001
                print(f"  запрос «{q}»: ошибка {str(e)[:120]}")
                continue
            if not res:
                print(f"  запрос «{q}»: ничего")
                continue
            print(f"  запрос «{q}»: найдено {len(res)}")
            found_any = True

            for c in res:
                if c["id"] in seen:
                    continue
                seen.add(c["id"])
                print(f"\n    ── контакт id={c['id']}  «{c.get('name')}»")
                print(f"       ссылка: {amo.contact_url(c['id'])}")

                tel = contact_phones(c)
                print(f"       телефоны (поле PHONE): {tel or 'НЕТ'}")
                if not tel:
                    print("       ⚠️ телефона в стандартном поле нет!")
                    print("       все заполненные поля:")
                    for name, val in all_field_values(c)[:12]:
                        print(f"         · {name}: {val}")

                leads = await contact_leads(amo, c["id"])
                if not leads:
                    print("       сделок нет → бот сочтёт «непонятно чей»")
                kinds = db.pipeline_kinds()
                names = {r["pipeline_id"]: r["name"] for r in db.list_pipelines()}
                for l in leads:  # noqa: E741
                    pid = l["pipeline_id"]
                    kind = kinds.get(pid, "unset")
                    label = {"retail": "🏢 РОЗНИЦА", "agency": "🤝 агентская",
                             "ignore": "🚫 игнор", "unset": "⬜️ НЕ РАЗМЕЧЕНА"}[kind]
                    when = __import__("datetime").datetime.fromtimestamp(
                        l["updated_at"] or 0)
                    print(f"       сделка {l['id']}: «{l['name']}»")
                    print(f"         воронка: {names.get(pid, pid)} [{label}]")
                    print(f"         активность: {when:%d.%m.%Y}")

        if not found_any:
            print("\n  ✗ Номер не найден в amoCRM ни в одном формате.")
            print("    Возможные причины:")
            print("      • телефон записан не в стандартное поле «Телефон»")
            print("      • контакт создан в другом аккаунте amoCRM")
            print("      • поиск amoCRM не индексирует этот формат записи")

        # ---------- итог ----------
        print()
        line("═")
        in_mirror = bool(matches)
        in_crm = bool(seen)
        if in_crm and not in_mirror:
            print("ВЫВОД: номер ЕСТЬ в amoCRM, но НЕТ в зеркале бота.")
            print("       Зеркало устарело — выполни /sync в чате с ботом.")
        elif not in_crm and not in_mirror:
            print("ВЫВОД: номера нет ни в CRM, ни в зеркале.")
            print("       Для бота это действительно уникальный клиент.")
        elif in_mirror and not in_crm:
            print("ВЫВОД: номер есть в зеркале, но живой поиск его не находит.")
            print("       Скорее всего контакт удалён из amoCRM после синхронизации.")
        else:
            print("ВЫВОД: номер есть и в CRM, и в зеркале — данные согласованы.")
            print(f"       Вердикт бота: {d.verdict.value}")
        line("═")
        print()
    finally:
        await amo.close()


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)
    asyncio.run(main(" ".join(sys.argv[1:])))
