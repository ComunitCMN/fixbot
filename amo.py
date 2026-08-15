"""
Клиент amoCRM API v4.

Авторизация приходит извне через AuthProvider (долгосрочный токен или OAuth) —
самому клиенту всё равно, какой именно, он просто спрашивает токен перед
каждым запросом.
"""

from __future__ import annotations

import asyncio
import logging

import httpx

log = logging.getLogger(__name__)


class AmoError(RuntimeError):
    """
    Ошибка amoCRM. `status` — код ответа, если он был.

    Код нужен, чтобы отличить «не понравилось поле» (4xx, повторить можно)
    от сбоя на той стороне (5xx, повторять нельзя: сделка могла создаться,
    и повтор положил бы в CRM вторую такую же).
    """

    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


class AmoClient:
    def __init__(self, auth, timeout: float = 30.0):
        self.auth = auth
        self.base = f"https://{auth.subdomain}.amocrm.ru"
        self._client = httpx.AsyncClient(base_url=self.base, timeout=timeout)

    async def close(self) -> None:
        await self._client.aclose()
        await self.auth.close()

    async def _request(self, method: str, url: str, **kw) -> dict:
        for attempt in range(4):
            token = await self.auth.token()
            headers = {
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            }
            r = await self._client.request(method, url, headers=headers, **kw)

            if r.status_code == 429:           # лимит 7 запросов/сек
                await asyncio.sleep(1.5 * (attempt + 1))
                continue
            if r.status_code in (204, 202):
                return {}
            if r.status_code == 401 and attempt == 0:
                # Токен мог протухнуть между проверкой и запросом.
                continue
            if r.status_code >= 400:
                raise AmoError(
                    f"{method} {url} → {r.status_code}: {r.text[:400]}",
                    status=r.status_code,
                )
            if not r.content:
                return {}
            return r.json()
        raise AmoError(f"{method} {url}: не удалось выполнить запрос")

    # ==================== чтение ====================

    async def _paginate(self, url: str, key: str, params: dict | None = None,
                        limit: int = 250):
        page = 1
        params = dict(params or {})
        while True:
            params.update({"page": page, "limit": limit})
            data = await self._request("GET", url, params=params)
            items = (data.get("_embedded") or {}).get(key) or []
            if not items:
                return
            for it in items:
                yield it
            if not (data.get("_links") or {}).get("next"):
                return
            page += 1
            await asyncio.sleep(0.15)

    async def pipelines(self) -> list[dict]:
        """Воронки со статусами — для команды /pipelines."""
        data = await self._request("GET", "/api/v4/leads/pipelines")
        out = []
        for p in (data.get("_embedded") or {}).get("pipelines") or []:
            statuses = [
                {"id": s["id"], "name": s.get("name"), "sort": s.get("sort"),
                 # type=1 — служебный этап «Неразобранное»: назначить
                 # его через API нельзя, amoCRM ответит NotSupportedChoice
                 "type": s.get("type")}
                for s in ((p.get("_embedded") or {}).get("statuses") or [])
            ]
            out.append({"id": p["id"], "name": p.get("name"),
                        "sort": p.get("sort"), "statuses": statuses})
        return out

    async def users(self) -> list[dict]:
        """
        Сотрудники застройщика — из них оператор выбирает ответственного
        за новые фиксации.

        Отключённых не показываем: сделку на них amoCRM не примет.
        Но если признака в ответе нет вовсе, человека оставляем —
        пустой список хуже лишней строки, настроить будет нечего.
        """
        out = []
        async for u in self._paginate("/api/v4/users", "users"):
            rights = u.get("rights") or {}
            if rights.get("is_active") is False:
                continue
            out.append({"id": u["id"],
                        "name": u.get("name") or u.get("email") or str(u["id"])})
        return out

    async def dump_contacts(self) -> list[dict]:
        """Плоский список контактов с телефонами — для зеркала."""
        out: list[dict] = []
        async for c in self._paginate("/api/v4/contacts", "contacts"):
            phones = [
                v.get("value")
                for f in (c.get("custom_fields_values") or [])
                if f.get("field_code") == "PHONE"
                for v in (f.get("values") or [])
                if v.get("value")
            ]
            if not phones:
                continue
            out.append({
                "id": c["id"], "name": c.get("name"), "phones": phones,
                "created_at": c.get("created_at"),
            })
        return out

    async def dump_companies(self) -> list[dict]:
        out = []
        async for c in self._paginate("/api/v4/companies", "companies"):
            out.append({"id": c["id"], "name": c.get("name")})
        return out

    async def dump_leads(self) -> list[dict]:
        """
        Все сделки со связанными контактами и компанией.

        Именно отсюда берётся происхождение клиента: по pipeline_id сделки
        понятно, розничная она или агентская.
        """
        out = []
        async for l in self._paginate(  # noqa: E741
            "/api/v4/leads", "leads", params={"with": "contacts,companies"}
        ):
            emb = l.get("_embedded") or {}
            out.append({
                "id": l["id"],
                "pipeline_id": l.get("pipeline_id"),
                "status_id": l.get("status_id"),
                "updated_at": l.get("updated_at") or l.get("created_at"),
                "created_at": l.get("created_at"),
                "contact_ids": [c["id"] for c in (emb.get("contacts") or [])],
                "company_id": next(
                    (c["id"] for c in (emb.get("companies") or [])), None
                ),
            })
        return out

    async def dump_leads_full(self) -> list[dict]:
        """
        Сделки для выгрузки базы: этап, контакты и причина отказа.

        Отдельно от `dump_leads`: тот кормит зеркало и намеренно берёт
        минимум — зеркалу нужно только происхождение контакта. Причины
        отказа в зеркале нет и не предполагается, поэтому здесь она
        запрашивается прямо у amoCRM параметром `with`.

        Только чтение.
        """
        out = []
        async for l in self._paginate(  # noqa: E741
            "/api/v4/leads", "leads",
            params={"with": "contacts,loss_reason"},
        ):
            emb = l.get("_embedded") or {}
            raw = emb.get("loss_reason")
            if isinstance(raw, dict):
                raw = [raw]
            reason = next((r.get("name") for r in (raw or [])
                           if isinstance(r, dict) and r.get("name")), None)
            out.append({
                "id": l["id"],
                "pipeline_id": l.get("pipeline_id"),
                "status_id": l.get("status_id"),
                "name": l.get("name"),
                "created_at": l.get("created_at"),
                "updated_at": l.get("updated_at") or l.get("created_at"),
                "contact_ids": [c["id"] for c in (emb.get("contacts") or [])],
                "loss_reason": reason,
            })
        return out

    async def dump_contacts_full(self) -> list[dict]:
        """
        Контакты для выгрузки: имя и телефоны, включая тех, у кого
        телефона нет вовсе.

        `dump_contacts` таких пропускает — зеркалу они не нужны, оно
        ищет по номеру. В файле сделка без номера всё равно нужна: иначе
        строка исчезнет молча.

        Только чтение.
        """
        out = []
        async for c in self._paginate("/api/v4/contacts", "contacts"):
            phones = [
                v.get("value")
                for f in (c.get("custom_fields_values") or [])
                if f.get("field_code") == "PHONE"
                for v in (f.get("values") or [])
                if v.get("value")
            ]
            out.append({"id": c["id"], "name": c.get("name"),
                        "phones": phones})
        return out

    async def search_contacts(self, query: str, with_leads: bool = False) -> list[dict]:
        params: dict = {"query": query, "limit": 50}
        if with_leads:
            params["with"] = "leads"
        data = await self._request("GET", "/api/v4/contacts", params=params)
        return (data.get("_embedded") or {}).get("contacts") or []

    async def leads_by_ids(self, ids: list[int]) -> dict[int, dict]:
        """Пачкой забирает сделки по id — одним запросом вместо десятка."""
        if not ids:
            return {}
        out: dict[int, dict] = {}
        for chunk in (ids[i:i + 50] for i in range(0, len(ids), 50)):
            data = await self._request(
                "GET", "/api/v4/leads",
                params=[("filter[id][]", i) for i in chunk] + [("limit", 250)],
            )
            for l in (data.get("_embedded") or {}).get("leads") or []:  # noqa: E741
                out[l["id"]] = l
        return out

    async def live_lookup(self, digits: str, kinds: dict[int, str],
                          booking_status_ids: set[int] | None = None) -> list[dict]:
        """
        Живой поиск номера прямо в amoCRM, минуя зеркало.

        Нужен, чтобы бот видел контакты, созданные после последней
        синхронизации. Зеркало при этом никуда не девается: оно надёжнее
        для маскированных номеров, потому что ищет по префиксу из цифр,
        а поиск amoCRM работает по строке и от формата записи зависит.

        Возвращает записи в том же виде, что и зеркало, — чтобы их можно
        было слить в один список совпадений.
        """
        from phones import compare_prefix, normalize

        booking_status_ids = booking_status_ids or set()
        target = normalize(digits)
        if target is None:
            return []

        # Один и тот же номер amoCRM может хранить по-разному, поэтому
        # пробуем несколько написаний, пока что-то не найдётся.
        d = target.digits
        queries = [d, f"+{d}", d[1:]]
        if target.is_full:
            queries.append(f"+{d[0]} {d[1:4]} {d[4:7]}-{d[7:9]}-{d[9:11]}")

        found: dict[int, dict] = {}
        for q in queries:
            try:
                for c in await self.search_contacts(q, with_leads=True):
                    found.setdefault(c["id"], c)
            except AmoError as e:
                log.warning("живой поиск «%s»: %s", q, e)
            if found:
                break

        if not found:
            return []

        # Подтягиваем сделки всех найденных контактов одним махом.
        lead_ids: list[int] = []
        for c in found.values():
            lead_ids += [l["id"] for l in
                         ((c.get("_embedded") or {}).get("leads") or [])]
        leads = await self.leads_by_ids(lead_ids)

        out: list[dict] = []
        for c in found.values():
            phones_raw = [
                v.get("value")
                for f in (c.get("custom_fields_values") or [])
                if f.get("field_code") == "PHONE"
                for v in (f.get("values") or [])
                if v.get("value")
            ]
            # Поиск amoCRM нестрогий и может вернуть лишнее — проверяем сами.
            matched = None
            for raw in phones_raw:
                p = normalize(raw)
                if p and compare_prefix(target, p):
                    matched = p
                    break
            if matched is None:
                continue

            row = {
                "contact_id": c["id"], "name": c.get("name"),
                "digits": matched.digits, "created_at": c.get("created_at"),
                "has_retail": False, "last_retail_activity": None,
                "has_agency": False, "agency_company_id": None, "booked": False,
            }
            for ref in ((c.get("_embedded") or {}).get("leads") or []):
                lead = leads.get(ref["id"])
                if not lead:
                    continue
                kind = kinds.get(lead.get("pipeline_id"), "unset")
                ts = lead.get("updated_at") or lead.get("created_at") or 0
                if kind == "retail":
                    row["has_retail"] = True
                    row["last_retail_activity"] = max(
                        row["last_retail_activity"] or 0, ts)
                elif kind == "agency":
                    row["has_agency"] = True
                    if lead.get("status_id") in booking_status_ids:
                        row["booked"] = True
            out.append(row)
        return out

    # ==================== запись ====================

    async def create_contact(self, name: str, phone: str | None = None,
                             phone_field_id: int | None = None,
                             company_id: int | None = None) -> int:
        fields: list[dict] = []
        if phone:
            f: dict = {"values": [{"value": phone, "enum_code": "MOB"}]}
            if phone_field_id:
                f["field_id"] = phone_field_id
            else:
                f["field_code"] = "PHONE"
            fields.append(f)

        body: dict = {"name": name}
        if fields:
            body["custom_fields_values"] = fields
        if company_id:
            body["_embedded"] = {"companies": [{"id": company_id}]}

        data = await self._request("POST", "/api/v4/contacts", json=[body])
        return data["_embedded"]["contacts"][0]["id"]

    async def find_or_create_agent(self, name: str, company_id: int | None,
                                   username: str | None = None,
                                   phone: str | None = None,
                                   tag: str = "агент") -> int:
        """
        Карточка агента — контакт-сотрудник агентства.

        Отдельная сущность, привязанная к компании-агентству и помеченная
        тегом. Так в карточке агентства видно всех его людей, и можно
        считать, кто сколько клиентов привёл.

        Тег обязателен: он отделяет агентов от клиентов в общем списке
        контактов, которых в аккаунте могут быть тысячи.
        """
        query = username or name
        for c in await self.search_contacts(query):
            same_name = (c.get("name") or "").strip().lower() == name.strip().lower()
            if same_name:
                return c["id"]

        fields: list[dict] = []
        if phone:
            fields.append({"field_code": "PHONE",
                           "values": [{"value": phone, "enum_code": "MOB"}]})

        embedded: dict = {"tags": [{"name": tag}]}
        if company_id:
            embedded["companies"] = [{"id": company_id}]

        body: dict = {"name": name, "_embedded": embedded}
        if fields:
            body["custom_fields_values"] = fields

        data = await self._request("POST", "/api/v4/contacts", json=[body])
        contact_id = data["_embedded"]["contacts"][0]["id"]

        # Компанию привязываем отдельно: через _embedded при создании
        # amoCRM связывает не всегда, и карточка остаётся без агентства.
        if company_id:
            try:
                await self.link_entity("contacts", contact_id,
                                       "companies", company_id)
            except AmoError as e:
                log.info("привязка агента к компании: %s", str(e)[:200])

        if username:
            await self.add_note(
                "contacts", contact_id,
                f"Агент из Telegram: @{username.lstrip('@')}",
            )
        return contact_id

    async def find_or_create_company(self, name: str) -> int:
        """Ищет компанию по точному названию, иначе создаёт."""
        data = await self._request(
            "GET", "/api/v4/companies", params={"query": name, "limit": 10}
        )
        for c in (data.get("_embedded") or {}).get("companies") or []:
            if (c.get("name") or "").strip().lower() == name.strip().lower():
                return c["id"]
        created = await self._request(
            "POST", "/api/v4/companies", json=[{"name": name}]
        )
        return created["_embedded"]["companies"][0]["id"]

    async def create_lead(self, name: str, contact_id: int,
                          company_id: int | None = None,
                          pipeline_id: int | None = None,
                          status_id: int | None = None,
                          tags: list[str] | None = None,
                          custom_fields: list[dict] | None = None,
                          agent_contact_id: int | None = None,
                          responsible_user_id: int | None = None) -> int:
        # Клиент идёт первым — amoCRM считает первый контакт основным.
        # Агент вторым, чтобы из сделки можно было перейти в его карточку.
        embedded: dict = {"contacts": [{"id": contact_id}]}
        if agent_contact_id and agent_contact_id != contact_id:
            embedded["contacts"].append({"id": agent_contact_id})
        if company_id:
            embedded["companies"] = [{"id": company_id}]
        if tags:
            embedded["tags"] = [{"name": t} for t in tags]

        lead: dict = {"name": name, "_embedded": embedded}
        if pipeline_id:
            lead["pipeline_id"] = pipeline_id
        if status_id:
            lead["status_id"] = status_id
        if custom_fields:
            lead["custom_fields_values"] = custom_fields
        if responsible_user_id:
            lead["responsible_user_id"] = responsible_user_id

        # Два поля здесь необязательные, и на каждом amoCRM умеет
        # заупрямиться: этап бывает служебным («Неразобранное» она через
        # API не принимает), ответственный — уволенным или отключённым.
        # Ни то, ни другое не стоит потерянной фиксации, поэтому спорное
        # поле убираем и повторяем без него.
        #
        # Порядок важен: первым уходит ответственный. Без него сделка
        # просто достанется владельцу токена — ровно как до этой
        # доработки. Этап трогаем только следом: убрав его, мы меняем
        # то, где сделка окажется в воронке, а это заметно оператору.
        optional = ["responsible_user_id", "status_id"]
        while True:
            try:
                data = await self._request("POST", "/api/v4/leads", json=[lead])
                break
            except AmoError as e:
                drop = next((f for f in optional
                             if f in lead and f in str(e)), None)
                if (drop is None and "responsible_user_id" in lead
                        and 400 <= (e.status or 0) < 500):
                    # amoCRM не всегда называет поле, которое ей не
                    # понравилось. Отказ разбирать по буквам нельзя,
                    # а пожертвовать можно только ответственным.
                    drop = "responsible_user_id"
                if drop is None:
                    raise
                log.warning("amoCRM отклонила сделку (%s=%s), повторяю без "
                            "этого поля: %s", drop, lead.get(drop), str(e)[:200])
                lead.pop(drop, None)
                optional.remove(drop)
        return data["_embedded"]["leads"][0]["id"]

    async def set_contact_phone(self, contact_id: int, phone: str,
                                field_id: int | None = None) -> None:
        """Дописывает телефон в существующую карточку — например агенту."""
        field: dict = {"values": [{"value": phone, "enum_code": "MOB"}]}
        if field_id:
            field["field_id"] = field_id
        else:
            field["field_code"] = "PHONE"
        await self._request("PATCH", f"/api/v4/contacts/{contact_id}",
                            json={"custom_fields_values": [field]})

    async def link_entity(self, entity: str, entity_id: int,
                          to_entity_type: str, to_entity_id: int) -> None:
        """
        Связывает две сущности отдельным запросом.

        Передача `_embedded.companies` при создании контакта срабатывает
        не всегда — карточка агента оставалась без компании. Явная связка
        через /link надёжнее и работает и для новых, и для существующих
        контактов.
        """
        await self._request(
            "POST", f"/api/v4/{entity}/{entity_id}/link",
            json=[{"to_entity_id": to_entity_id,
                   "to_entity_type": to_entity_type}],
        )

    async def add_note(self, entity: str, entity_id: int, text: str) -> None:
        await self._request(
            "POST", f"/api/v4/{entity}/{entity_id}/notes",
            json=[{"note_type": "common", "params": {"text": text}}],
        )

    # ==================== ссылки ====================

    def contact_url(self, contact_id: int) -> str:
        return f"{self.base}/contacts/detail/{contact_id}"

    def lead_url(self, lead_id: int) -> str:
        return f"{self.base}/leads/detail/{lead_id}"

    def company_url(self, company_id: int) -> str:
        return f"{self.base}/companies/detail/{company_id}"


# ======================== расчёт происхождения ========================

def compute_origins(leads: list[dict], kinds: dict[int, str],
                    booking_status_ids: set[int] | None = None) -> list[dict]:
    """
    Из списка сделок собирает происхождение по каждому контакту.

    kinds: {pipeline_id: 'retail' | 'agency' | 'ignore' | 'unset'}

    Контакт считается розничным, если у него есть хоть одна сделка
    в розничной воронке. Дата активности — максимальная updated_at
    среди таких сделок.
    """
    booking_status_ids = booking_status_ids or set()
    acc: dict[int, dict] = {}

    for l in leads:  # noqa: E741
        kind = kinds.get(l.get("pipeline_id"), "unset")
        if kind == "ignore":
            continue
        ts = l.get("updated_at") or l.get("created_at") or 0

        for cid in l.get("contact_ids") or []:
            row = acc.setdefault(cid, {
                "contact_id": cid, "has_retail": 0, "last_retail_activity": None,
                "has_agency": 0, "agency_company_id": None,
                "last_agency_activity": None, "booked": 0,
            })
            if kind == "retail":
                row["has_retail"] = 1
                row["last_retail_activity"] = max(
                    row["last_retail_activity"] or 0, ts
                )
            elif kind == "agency":
                row["has_agency"] = 1
                row["last_agency_activity"] = max(
                    row["last_agency_activity"] or 0, ts
                )
                if l.get("company_id") and not row["agency_company_id"]:
                    row["agency_company_id"] = l["company_id"]
                if l.get("status_id") in booking_status_ids:
                    row["booked"] = 1

    return list(acc.values())
