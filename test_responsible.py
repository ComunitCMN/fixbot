"""
Ответственный менеджер на новых фиксациях.

Раньше бот ответственного не ставил вовсе, и amoCRM вешала сделку на
владельца токена — то есть на техническую учётку. Теперь оператор
выбирает менеджера кнопкой, и сделка создаётся сразу на нём.

Главное, что здесь проверяется:

* не настроено — поведение ровно как было, ни один действующий клиент
  не должен заметить правки;
* менеджер уволен или отключён — фиксация всё равно создаётся. Кадровые
  перемены у застройщика не стоят живому агенту потерянной фиксации.

Решение целиком — в РЕШЕНИЯ.md, раздел «Ответственный за новые фиксации».
"""

import inspect

import pytest

from amo import AmoClient, AmoError
from db import Db


# ===================== заглушка amoCRM =====================

class FakeAuth:
    subdomain = "test"

    async def token(self):
        return "t"

    async def close(self):
        pass


class FakeAmo(AmoClient):
    """
    Настоящий AmoClient с подменённым транспортом: логика create_lead
    проверяется как есть, наружу уходит не запрос, а запись в списке.

    `answers` — что отвечать по очереди: словарь отдаётся как ответ,
    AmoError бросается.
    """

    def __init__(self, answers=None):
        super().__init__(FakeAuth())
        self.calls: list[dict] = []
        self.answers = list(answers or [])

    async def _request(self, method, url, **kw):
        body = (kw.get("json") or [{}])[0]
        self.calls.append({"method": method, "url": url, "body": body})
        answer = (self.answers.pop(0) if self.answers
                  else {"_embedded": {"leads": [{"id": 555}]}})
        if isinstance(answer, Exception):
            raise answer
        return answer


def _lead_bodies(amo: FakeAmo) -> list[dict]:
    return [c["body"] for c in amo.calls if c["url"] == "/api/v4/leads"]


# ===================== сама настройка =====================

def test_nobody_is_responsible_by_default(tmp_path):
    """
    Пустая настройка — это не ошибка, а сегодняшнее поведение. У клиента,
    который ничего не нажмёт, ответственного как не было, так и нет.
    """
    db = Db(tmp_path / "r1.db")
    assert db.responsible_user_id() is None
    assert db.responsible_user_name() is None


def test_the_choice_is_remembered(tmp_path):
    db = Db(tmp_path / "r2.db")
    db.set_responsible_user(3141592, "Ольга Смирнова")

    assert db.responsible_user_id() == 3141592
    assert db.responsible_user_name() == "Ольга Смирнова"


def test_only_one_manager_at_a_time(tmp_path):
    """Настройка одна на клиента: новый выбор заменяет прежний."""
    db = Db(tmp_path / "r3.db")
    db.set_responsible_user(1, "Первый")
    db.set_responsible_user(2, "Второй")

    assert db.responsible_user_id() == 2
    assert db.responsible_user_name() == "Второй"


def test_the_choice_can_be_taken_back(tmp_path):
    """
    Снять выбор нужно уметь без правки базы руками: иначе единственный
    способ вернуться к прежнему поведению — лезть в файл на сервере.
    """
    db = Db(tmp_path / "r4.db")
    db.set_responsible_user(7, "Пётр")
    db.set_responsible_user(None, None)

    assert db.responsible_user_id() is None
    assert db.responsible_user_name() is None


def test_an_old_database_needs_no_rebuild(tmp_path):
    """
    Базы на сервере с боевыми данными, пересоздавать их нельзя. Настройка
    обязана появиться на старой базе сама и по умолчанию быть пустой.
    """
    import sqlite3

    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    conn.executescript("""
        CREATE TABLE pipelines (
            account_id  INTEGER NOT NULL,
            pipeline_id INTEGER NOT NULL,
            name        TEXT,
            kind        TEXT NOT NULL DEFAULT 'unset',
            PRIMARY KEY (account_id, pipeline_id));
        INSERT INTO pipelines (account_id, pipeline_id, name, kind)
        VALUES (1, 700, 'Агентские Сочи', 'agency');
    """)
    conn.commit()
    conn.close()

    db = Db(path)
    assert db.responsible_user_id() is None
    db.set_responsible_user(42, "Ольга")
    assert db.responsible_user_id() == 42
    assert db.pipeline_kinds() == {700: "agency"}      # разметка цела


# ===================== сделка в amoCRM =====================

@pytest.mark.asyncio
async def test_the_lead_gets_the_responsible():
    amo = FakeAmo()
    await amo.create_lead(name="Петров", contact_id=10,
                          pipeline_id=700, status_id=71,
                          responsible_user_id=3141592)

    assert _lead_bodies(amo)[0]["responsible_user_id"] == 3141592


@pytest.mark.asyncio
async def test_without_the_setting_the_lead_is_exactly_as_before():
    """
    Самая важная страховка задачи. Не настроено — в запросе поля быть
    не должно вообще: пустое значение amoCRM поняла бы по-своему.
    """
    amo = FakeAmo()
    await amo.create_lead(name="Петров", contact_id=10,
                          pipeline_id=700, status_id=71)

    body = _lead_bodies(amo)[0]
    assert "responsible_user_id" not in body
    assert body["pipeline_id"] == 700 and body["status_id"] == 71


@pytest.mark.asyncio
async def test_a_fired_manager_does_not_cost_a_fixation():
    """
    Менеджера уволили, amoCRM отказывается вешать на него сделку.
    Единственное место задачи, где экономить нельзя: откатываемся
    на прежнее поведение и создаём сделку всё равно.
    """
    amo = FakeAmo([
        AmoError("POST /api/v4/leads → 400: responsible_user_id is invalid",
                 status=400),
        {"_embedded": {"leads": [{"id": 777}]}},
    ])

    lead_id = await amo.create_lead(name="Петров", contact_id=10,
                                    pipeline_id=700, status_id=71,
                                    responsible_user_id=3141592)

    assert lead_id == 777
    bodies = _lead_bodies(amo)
    assert len(bodies) == 2
    assert "responsible_user_id" not in bodies[1]
    assert bodies[1]["pipeline_id"] == 700      # остальное на месте


@pytest.mark.asyncio
async def test_a_refusal_that_names_no_field_also_saves_the_fixation():
    """
    amoCRM не всегда пишет, что именно ей не понравилось. Ответственный —
    самое новое и самое необязательное в запросе, поэтому при отказе
    жертвуем именно им, а не фиксацией.
    """
    amo = FakeAmo([
        AmoError("POST /api/v4/leads → 400: Bad Request", status=400),
        {"_embedded": {"leads": [{"id": 778}]}},
    ])

    lead_id = await amo.create_lead(name="Петров", contact_id=10,
                                    responsible_user_id=3141592)

    assert lead_id == 778
    assert "responsible_user_id" not in _lead_bodies(amo)[1]


@pytest.mark.asyncio
async def test_a_broken_amocrm_is_not_retried():
    """
    Пятисотая — это не «поле не понравилось», а сбой на той стороне;
    сделка могла и создаться. Повтор здесь означал бы вторую такую же
    сделку в CRM, поэтому ошибку отдаём наверх, как и раньше.
    """
    amo = FakeAmo([AmoError("POST /api/v4/leads → 500: oops", status=500)])

    with pytest.raises(AmoError):
        await amo.create_lead(name="Петров", contact_id=10,
                              responsible_user_id=3141592)

    assert len(_lead_bodies(amo)) == 1


@pytest.mark.asyncio
async def test_the_status_fallback_still_works():
    """
    Служебный этап amoCRM отклоняет — сделка создаётся без него. Это
    прежнее поведение, и ответственный не должен его задеть.
    """
    amo = FakeAmo([
        AmoError("POST /api/v4/leads → 400: status_id NotSupportedChoice",
                 status=400),
        {"_embedded": {"leads": [{"id": 779}]}},
    ])

    lead_id = await amo.create_lead(name="Петров", contact_id=10,
                                    pipeline_id=700, status_id=1)

    assert lead_id == 779
    assert "status_id" not in _lead_bodies(amo)[1]


@pytest.mark.asyncio
async def test_both_can_go_and_the_fixation_still_lands():
    """Не подошли оба поля — сделка всё равно должна появиться."""
    amo = FakeAmo([
        AmoError("POST /api/v4/leads → 400: responsible_user_id", status=400),
        AmoError("POST /api/v4/leads → 400: status_id", status=400),
        {"_embedded": {"leads": [{"id": 780}]}},
    ])

    lead_id = await amo.create_lead(name="Петров", contact_id=10,
                                    pipeline_id=700, status_id=1,
                                    responsible_user_id=3141592)

    assert lead_id == 780
    last = _lead_bodies(amo)[-1]
    assert "responsible_user_id" not in last and "status_id" not in last


# ===================== список менеджеров =====================

@pytest.mark.asyncio
async def test_the_list_comes_from_amocrm():
    """
    Оператор выбирает кнопкой, а список тянется из CRM. Заставлять его
    искать числовой идентификатор пользователя в интерфейсе amoCRM —
    верный способ получить молча неверную настройку.
    """
    amo = FakeAmo([{
        "_embedded": {"users": [
            {"id": 11, "name": "Ольга Смирнова",
             "rights": {"is_active": True}},
            {"id": 12, "name": "Уволенный", "rights": {"is_active": False}},
        ]},
    }])

    users = await amo.users()

    assert [u["id"] for u in users] == [11]
    assert users[0]["name"] == "Ольга Смирнова"
    assert amo.calls[0]["url"] == "/api/v4/users"


@pytest.mark.asyncio
async def test_a_user_without_rights_is_not_hidden():
    """
    Если amoCRM не прислала признак активности, прятать человека нельзя:
    иначе список окажется пустым и настроить будет нечего.
    """
    amo = FakeAmo([{"_embedded": {"users": [{"id": 11, "name": "Ольга"}]}}])

    assert [u["id"] for u in await amo.users()] == [11]


# ===================== бот отдаёт выбранного =====================

@pytest.mark.asyncio
async def test_the_bot_puts_the_chosen_manager_on_a_new_fixation(
        tmp_path, monkeypatch):
    """
    Проверка сквозная: настройка в базе должна доехать до запроса
    в amoCRM. Между ними обработчик фиксации, и потеря значения там —
    именно то, чего не увидит ни один модульный тест по отдельности.
    """
    import bot as b
    import phones

    db = Db(tmp_path / "flow.db")
    db.set_responsible_user(3141592, "Ольга Смирнова")
    monkeypatch.setattr(b, "db", db)

    created: dict = {}

    class Amo:
        async def create_contact(self, **kw):
            return 10

        async def find_or_create_agent(self, **kw):
            return 20

        async def create_lead(self, **kw):
            created.update(kw)
            return 30

        async def add_note(self, *a, **kw):
            pass

    monkeypatch.setattr(b, "amo", Amo())

    await b._create_in_amo(
        {"agency_id": None, "client": "Петров", "author": "Иван"},
        phones.normalize("+79171475214"), agent_telegram_id=42,
    )

    assert created["responsible_user_id"] == 3141592


@pytest.mark.asyncio
async def test_the_bot_asks_for_nobody_when_it_is_not_set(
        tmp_path, monkeypatch):
    """Не настроено — в amoCRM уходит None, то есть прежний запрос."""
    import bot as b
    import phones

    db = Db(tmp_path / "flow2.db")
    monkeypatch.setattr(b, "db", db)

    created: dict = {}

    class Amo:
        async def create_contact(self, **kw):
            return 10

        async def find_or_create_agent(self, **kw):
            return 20

        async def create_lead(self, **kw):
            created.update(kw)
            return 30

        async def add_note(self, *a, **kw):
            pass

    monkeypatch.setattr(b, "amo", Amo())

    await b._create_in_amo(
        {"agency_id": None, "client": "Петров", "author": "Иван"},
        phones.normalize("+79171475214"), agent_telegram_id=42,
    )

    assert created.get("responsible_user_id") is None


# ===================== раздел у оператора =====================

def test_the_choice_is_a_button_not_a_number(tmp_path, monkeypatch):
    """Кнопка на каждого менеджера плюс кнопка «никого»."""
    import bot as b

    db = Db(tmp_path / "kb.db")
    db.set_responsible_user(11, "Ольга Смирнова")
    monkeypatch.setattr(b, "db", db)

    users = [{"id": 11, "name": "Ольга Смирнова"},
             {"id": 12, "name": "Игорь Ким"}]
    kb = b._responsible_kb(users)
    data = [x.callback_data for row in kb.inline_keyboard for x in row]

    assert "rsp:11" in data and "rsp:12" in data
    assert "rsp:0" in data          # «не задан» — путь назад к прежнему


def test_the_section_shows_who_it_is_now(tmp_path, monkeypatch):
    import bot as b

    db = Db(tmp_path / "txt.db")
    db.set_responsible_user(11, "Ольга Смирнова")
    monkeypatch.setattr(b, "db", db)

    out = b._responsible_text([{"id": 11, "name": "Ольга Смирнова"}])
    assert "Ольга Смирнова" in out


def test_an_unset_section_explains_what_happens_now(tmp_path, monkeypatch):
    """
    Пустая настройка должна читаться как осмысленное состояние, а не как
    поломка: иначе оператор нажмёт наугад.
    """
    import bot as b

    db = Db(tmp_path / "txt2.db")
    monkeypatch.setattr(b, "db", db)

    out = b._responsible_text([{"id": 11, "name": "Ольга Смирнова"}])
    assert "не задан" in out.lower()


def test_the_section_is_operator_only():
    """Ответственный — операторский раздел, владельцу его не показывают."""
    import bot as b

    src = inspect.getsource(b.cb_responsible)
    assert "is_operator" in src


def test_the_owner_has_no_way_into_the_section():
    """
    Раздел живёт в «Техническом», а туда владелец не ходит: воронки
    и ответственные — не его слой.
    """
    import menu as mn

    data = [x.callback_data for row in mn.tech_menu().inline_keyboard
            for x in row]
    assert "m:resp" in data

    owner = [x.callback_data for row in mn.main_menu(mn.OWNER).inline_keyboard
             for x in row]
    assert "m:tech" not in owner
