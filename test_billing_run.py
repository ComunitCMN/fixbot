"""Ежедневный проход: весь календарь обслуживания без единого сообщения."""

import sqlite3
import time
from datetime import date, timedelta

import pytest

import billing_run as run
import clients as cl
from db import Db

DAY = 86400
START = date(2026, 10, 5)
DUE = date(2026, 11, 5)


class Post:
    """Куда бы ушли сообщения, если бы это был настоящий Telegram."""

    def __init__(self):
        self.operator: list[tuple[str, str, str]] = []
        self.owner: list[tuple[str, str]] = []
        self.broken = False

    async def to_operator(self, slug, text, kind, extra):
        self.operator.append((slug, kind, text))

    async def to_owner(self, slug, folder, text):
        if self.broken:
            raise RuntimeError("владелец заблокировал бота")
        self.owner.append((slug, text))

    @property
    def kinds(self):
        return [k for _, k, _ in self.operator]


@pytest.fixture
def setup(tmp_path):
    """Оператор с одним клиентом и парой фиксаций в его базе."""
    db = Db(tmp_path / "op.db")
    db.set_billing("eco", start_date=START.isoformat())
    db.set_wallet("eco", "TXxx7fA", "USDT TRC-20")

    clients = tmp_path / "clients"
    folder = clients / "eco"
    folder.mkdir(parents=True)
    (folder / ".env").write_text(
        'TELEGRAM_TOKEN="1:x"\nOWNER_IDS=555\nDEVELOPER_NAME="Eco Invest"\n',
        encoding="utf-8")

    conn = sqlite3.connect(folder / "fixbot.db")
    conn.execute("CREATE TABLE fixations (id INTEGER PRIMARY KEY,"
                 " amo_lead_id INTEGER, created_at INTEGER)")
    conn.commit()
    conn.close()
    return db, str(clients), folder


def add_fixations(folder, n, when: date, in_crm=True):
    conn = sqlite3.connect(folder / "fixbot.db")
    ts = int(time.mktime(when.timetuple()))
    conn.executemany(
        "INSERT INTO fixations (amo_lead_id, created_at) VALUES (?,?)",
        [(1 if in_crm else None, ts) for _ in range(n)])
    conn.commit()
    conn.close()


async def go(db, clients, today, post):
    return await run.run_once(db=db, clients_dir=clients, today=today,
                              to_operator=post.to_operator,
                              to_owner=post.to_owner)


# ===================== по шагам =====================

@pytest.mark.asyncio
async def test_nothing_happens_before_the_start(setup):
    db, clients, _ = setup
    post = Post()
    assert await go(db, clients, date(2026, 10, 4), post) == []
    assert not post.owner and not post.operator


@pytest.mark.asyncio
async def test_start_is_announced_to_the_owner(setup):
    db, clients, _ = setup
    post = Post()
    assert await go(db, clients, START, post) == [("eco", "announce_start")]
    assert "Начался период обслуживания" in post.owner[0][1]
    # Оператора этим не тревожим — он и так знает.
    assert not post.operator


@pytest.mark.asyncio
async def test_announcement_is_sent_once(setup):
    db, clients, _ = setup
    post = Post()
    await go(db, clients, START, post)
    await go(db, clients, START + timedelta(days=1), post)
    assert len(post.owner) == 1


@pytest.mark.asyncio
async def test_invoice_is_prepared_with_a_real_count(setup):
    db, clients, folder = setup
    add_fixations(folder, 137, date(2026, 10, 20))
    add_fixations(folder, 5, date(2026, 10, 20), in_crm=False)   # не в счёт
    post = Post()

    await go(db, clients, START, post)                # объявление
    await go(db, clients, date(2026, 11, 2), post)    # счёт

    assert post.kinds == ["prepare"]
    text = post.operator[0][2]
    assert "137" in text and "$70" in text
    assert "TXxx7fA" in text
    # В базе тоже зафиксировано — чтобы потом было чем ответить на спор.
    period = db.period("eco", DUE.isoformat())
    assert (period["fixations"], period["amount"]) == (137, 70)


@pytest.mark.asyncio
async def test_cheaper_tier_when_under_the_threshold(setup):
    db, clients, folder = setup
    add_fixations(folder, 99, date(2026, 10, 20))
    post = Post()
    await go(db, clients, START, post)
    await go(db, clients, date(2026, 11, 2), post)
    assert "$40" in post.operator[0][2]


@pytest.mark.asyncio
async def test_fixations_outside_the_period_are_not_counted(setup):
    db, clients, folder = setup
    add_fixations(folder, 200, date(2026, 9, 20))     # до начала обслуживания
    add_fixations(folder, 3, date(2026, 10, 20))
    post = Post()
    await go(db, clients, START, post)
    await go(db, clients, date(2026, 11, 2), post)
    assert "Фиксаций в CRM: <b>3</b>" in post.operator[0][2]


@pytest.mark.asyncio
async def test_missing_wallet_is_shouted_about(setup):
    """Отправить счёт без реквизитов — обиднее всего."""
    db, clients, folder = setup
    db.conn.execute("UPDATE billing SET wallet=NULL WHERE slug='eco'")
    db.conn.commit()
    post = Post()
    await go(db, clients, START, post)
    await go(db, clients, date(2026, 11, 2), post)
    assert "реквизиты не заданы" in post.operator[0][2]


@pytest.mark.asyncio
async def test_operator_is_nudged_weekly_after_the_due_date(setup):
    db, clients, _ = setup
    post = Post()
    await go(db, clients, START, post)
    await go(db, clients, date(2026, 11, 2), post)      # prepare

    await go(db, clients, DUE, post)                    # nudge
    await go(db, clients, DUE + timedelta(days=3), post)   # рано
    await go(db, clients, DUE + timedelta(days=7), post)   # снова

    assert post.kinds == ["prepare", "nudge", "nudge"]


@pytest.mark.asyncio
async def test_client_is_not_paused_while_invoice_is_unsent(setup):
    """Забывчивость оператора не должна ломать работу агентам."""
    db, clients, _ = setup
    post = Post()
    for i in range(0, 60, 3):
        await go(db, clients, START + timedelta(days=i), post)
    assert "paused" not in post.kinds
    assert not cl.is_paused(_folder(clients))


def _folder(clients):
    from pathlib import Path
    return Path(clients) / "eco"


# ===================== после отправки счёта =====================

async def send_invoice(db, clients, post):
    """Проходим до счёта и отмечаем, что оператор нажал «Отправить»."""
    await go(db, clients, START, post)
    await go(db, clients, date(2026, 11, 2), post)
    db.mark_period("eco", DUE.isoformat(), invoice_sent=1)


@pytest.mark.asyncio
async def test_reminder_goes_to_the_owner_once(setup):
    db, clients, _ = setup
    post = Post()
    await send_invoice(db, clients, post)

    await go(db, clients, DUE + timedelta(days=7), post)
    await go(db, clients, DUE + timedelta(days=9), post)

    reminders = [t for _, t in post.owner if "Напоминаю" in t]
    assert len(reminders) == 1
    assert "TXxx7fA" in reminders[0]


@pytest.mark.asyncio
async def test_warning_then_pause(setup):
    db, clients, folder = setup
    post = Post()
    await send_invoice(db, clients, post)

    await go(db, clients, DUE + timedelta(days=7), post)    # напоминание
    await go(db, clients, DUE + timedelta(days=13), post)   # предупреждение
    assert post.kinds[-1] == "warn"
    assert not cl.is_paused(folder)

    await go(db, clients, DUE + timedelta(days=14), post)
    assert post.kinds[-1] == "paused"
    assert cl.is_paused(folder)
    assert db.get_billing("eco")["paused"] == 1


@pytest.mark.asyncio
async def test_pause_marker_explains_itself(setup):
    """Человек, увидевший файл на сервере, должен понять, что произошло."""
    db, clients, folder = setup
    post = Post()
    await send_invoice(db, clients, post)
    for d in (7, 13, 14):
        await go(db, clients, DUE + timedelta(days=d), post)
    assert "не оплачен" in (folder / "PAUSED").read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_agents_never_learn_about_money(setup):
    """Ни в одном сообщении наружу не должно быть сумм для агентов."""
    import texts
    for lang in ("ru", "en"):
        low = texts.service_paused(lang).lower()
        for word in ("оплат", "долг", "payment", "unpaid", "$"):
            assert word not in low


@pytest.mark.asyncio
async def test_payment_closes_the_period_and_resumes(setup):
    db, clients, folder = setup
    post = Post()
    await send_invoice(db, clients, post)
    for d in (7, 13, 14):
        await go(db, clients, DUE + timedelta(days=d), post)
    assert cl.is_paused(folder)

    # Оператор нажал «Оплачено».
    db.close_period("eco", DUE.isoformat(), paid_at=int(time.time()))
    cl.set_paused(folder, False)

    post2 = Post()
    await go(db, clients, DUE + timedelta(days=16), post2)
    assert post2.owner and "Новый период" in post2.owner[0][1]
    assert not cl.is_paused(folder)


@pytest.mark.asyncio
async def test_next_period_keeps_the_calendar(setup):
    """Заплатил поздно — следующий срок всё равно 5 декабря."""
    db, clients, _ = setup
    post = Post()
    await send_invoice(db, clients, post)
    db.close_period("eco", DUE.isoformat(), paid_at=int(time.time()))

    # Проверка идёт каждый день, и за раз делается одно дело: сперва
    # объявление нового периода, на следующий день — счёт.
    await go(db, clients, date(2026, 12, 2), post)
    await go(db, clients, date(2026, 12, 3), post)

    assert db.period("eco", "2026-12-05")["prepared"] == 1
    assert db.period("eco", "2026-12-05")["begin"] == "2026-11-05"


# ===================== устойчивость =====================

@pytest.mark.asyncio
async def test_one_broken_client_does_not_stop_the_rest(setup, tmp_path):
    """Иначе сломанный клиент лишил бы счетов всех остальных."""
    db, clients, _ = setup
    second = tmp_path / "clients" / "romashka"
    second.mkdir(parents=True)
    (second / ".env").write_text('OWNER_IDS=777\n', encoding="utf-8")
    db.set_billing("romashka", start_date=START.isoformat())

    post = Post()
    post.broken = True                      # владельцам написать не выйдет
    done = await go(db, clients, START, post)

    assert done == []                       # ни одному не удалось
    # но обход не прервался: обе попытки состоялись и обе записаны в журнал
    post.broken = False
    assert len(await go(db, clients, START, post)) == 2


@pytest.mark.asyncio
async def test_failure_is_not_recorded_as_done(setup):
    """
    Не дошло — значит не отмечаем. Иначе объявление считалось бы
    отправленным и клиент не узнал бы о начале периода никогда.
    """
    db, clients, _ = setup
    post = Post()
    post.broken = True
    await go(db, clients, START, post)
    assert db.period("eco", DUE.isoformat())["announced"] == 0


# ===================== клиентский бот слушается метки =====================

def test_client_bot_checks_the_pause_marker():
    """
    Приостановка бессмысленна, если бот её не замечает. Проверяем, что
    в обработчике сообщений она есть — и стоит до записи в CRM.
    """
    import inspect

    import bot as b

    src = inspect.getsource(b.on_message)
    assert "service_paused()" in src

    head = src.split("service_paused()", 1)[0]
    for later in ("save_fixation", "create_lead", "confirm_card"):
        assert later not in head, f"{later} выполняется до проверки паузы"


def test_pause_check_looks_next_to_the_database(tmp_path, monkeypatch):
    import bot as b

    db_file = tmp_path / "fixbot.db"
    db_file.write_text("", encoding="utf-8")
    monkeypatch.setattr(b.cfg, "db_path", str(db_file), raising=False)

    assert b.service_paused() is False
    (tmp_path / "PAUSED").write_text("не оплачено\n", encoding="utf-8")
    assert b.service_paused() is True
