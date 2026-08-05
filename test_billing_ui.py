"""Раздел «Оплаты»: карточки, кнопки и проводка в боте."""

import inspect
from datetime import date

import pytest

import billing_ui as ui
from db import Db

BEGIN, DUE = date(2026, 10, 5), date(2026, 11, 5)


@pytest.fixture
def row(tmp_path):
    db = Db(tmp_path / "op.db")
    db.set_billing("eco", start_date=BEGIN.isoformat())
    db.set_wallet("eco", "TXxx7fA", "USDT TRC-20")
    db.period("eco", DUE.isoformat(), BEGIN.isoformat())
    return db


def labels(kb):
    return [b.text for line in kb for b in line]


# ===================== состояния =====================

def test_status_marks_follow_the_period(row):
    r = row.get_billing("eco")
    p = row.period("eco", DUE.isoformat())
    assert ui.status_of(r, p) == "🕓"

    row.mark_period("eco", DUE.isoformat(), prepared=1)
    assert ui.status_of(r, row.period("eco", DUE.isoformat())) == "📝"

    row.mark_period("eco", DUE.isoformat(), invoice_sent=1)
    assert ui.status_of(r, row.period("eco", DUE.isoformat())) == "📨"

    row.mark_period("eco", DUE.isoformat(), paid_at=1)
    assert ui.status_of(r, row.period("eco", DUE.isoformat())) == "✅"


def test_paused_beats_everything(row):
    row.set_paused("eco", True)
    r, p = row.get_billing("eco"), row.period("eco", DUE.isoformat())
    assert ui.status_of(r, p) == "⏸"


# ===================== кнопки =====================

def test_send_button_hidden_without_wallet(row):
    """Отправить счёт без реквизитов — самая обидная из возможных ошибок."""
    row.conn.execute("UPDATE billing SET wallet=NULL WHERE slug='eco'")
    row.conn.commit()
    kb = ui.client_kb(row.get_billing("eco"), row.period("eco", DUE.isoformat()))
    assert not any("Отправить" in t for t in labels(kb))
    assert any("Реквизиты" in t for t in labels(kb))


def test_send_button_disappears_after_sending(row):
    """Иначе счёт уйдёт клиенту дважды."""
    kb = ui.client_kb(row.get_billing("eco"), row.period("eco", DUE.isoformat()))
    assert any("Отправить" in t for t in labels(kb))

    row.mark_period("eco", DUE.isoformat(), invoice_sent=1)
    kb = ui.client_kb(row.get_billing("eco"), row.period("eco", DUE.isoformat()))
    assert not any("Отправить" in t for t in labels(kb))
    assert any("Оплачено" in t for t in labels(kb))


def test_paid_period_offers_neither(row):
    row.mark_period("eco", DUE.isoformat(), invoice_sent=1, paid_at=1)
    kb = ui.client_kb(row.get_billing("eco"), row.period("eco", DUE.isoformat()))
    text = " ".join(labels(kb))
    assert "Отправить" not in text and "Оплачено" not in text


def test_resume_shown_only_when_paused(row):
    kb = ui.client_kb(row.get_billing("eco"), row.period("eco", DUE.isoformat()))
    assert not any("приостановку" in t for t in labels(kb))

    row.set_paused("eco", True)
    kb = ui.client_kb(row.get_billing("eco"), row.period("eco", DUE.isoformat()))
    assert any("приостановку" in t for t in labels(kb))


# ===================== тексты =====================

def test_client_card_shows_the_essentials(row):
    out = ui.client_text(row.get_billing("eco"),
                         row.period("eco", DUE.isoformat()), BEGIN, DUE, 137)
    for part in ("5 октября", "5 ноября", "137", "$70", "TXxx7fA",
                 "USDT TRC-20"):
        assert part in out
    assert "{" not in out


def test_overview_explains_the_marks(row):
    items = [(row.get_billing("eco"), row.period("eco", DUE.isoformat()), DUE)]
    out = ui.overview_text(items)
    assert "eco" in out and "5 ноября" in out
    assert "🕓" in out and "приостановлен" in out


def test_empty_overview_tells_what_to_do():
    out = ui.overview_text([])
    assert "не настроено" in out


def test_wallet_is_parsed_from_one_or_two_lines():
    assert ui.parse_wallet("TXxx7fA") == ("TXxx7fA", "")
    assert ui.parse_wallet("TXxx7fA\nUSDT TRC-20") == ("TXxx7fA", "USDT TRC-20")
    assert ui.parse_wallet("  TXxx7fA  \n\n TRC-20 \n") == ("TXxx7fA", "TRC-20")
    assert ui.parse_wallet("") == ("", "")


def test_fixation_list_is_readable():
    rows = [{"created_at": 1793000000, "client_name": "Иванов",
             "agency": "ERA B"}]
    out = ui.fixations_text(rows, BEGIN, DUE)
    assert "Иванов" in out and "ERA B" in out


def test_empty_fixation_list_is_not_scary():
    assert "ни одной" in ui.fixations_text([], BEGIN, DUE)


# ===================== проводка в боте =====================

def test_billing_loop_runs_only_in_the_operator_bot():
    """
    Иначе бот каждого застройщика принялся бы считать чужие деньги
    и рассылать чужим владельцам счета.
    """
    import bot as b

    src = inspect.getsource(b.main)
    assert "is_operator_bot()" in src
    assert "billing_loop()" in src

    head = src.split("billing_loop()", 1)[0]
    assert "is_operator_bot()" in head, "проверка должна стоять до запуска"


def test_billing_buttons_are_operator_only():
    """Владелец не должен увидеть ни сумм, ни кнопки «Оплачено»."""
    import bot as b

    src = inspect.getsource(b.cb_billing)
    head = src.split("action", 1)[0]
    assert "mn.OPERATOR" in head


def test_wallet_reply_is_handled_before_broadcast():
    """
    Оператор может одновременно готовить рассылку и вводить реквизиты.
    Если перепутать порядок, адрес кошелька уедет всем агентствам.
    """
    import bot as b

    src = inspect.getsource(b.on_private_any)
    assert src.index("try_wallet_reply") < src.index("try_capture_broadcast")


def test_qr_is_resent_not_forwarded_by_id():
    """
    file_id выдан операторскому боту, чужой бот по нему картинку не возьмёт.
    Поэтому её скачивают и отправляют заново.
    """
    import bot as b

    src = inspect.getsource(b.send_qr_as_client_bot)
    assert "bot.download" in src
    assert "files=" in src


def test_setup_flow_exists_and_comes_before_wallet():
    """
    Без способа завести обслуживание раздел «Оплаты» был бы вечно пустым.
    """
    import bot as b

    assert "bl:setup:" in inspect.getsource(b.cb_menu)

    src = inspect.getsource(b.cb_billing)
    # Заведение обрабатывается до поиска строки в базе — её ещё нет.
    assert src.index('action == "setup"') < src.index("db.get_billing(slug)")


def test_bad_start_date_does_not_pass_silently():
    import bot as b

    src = inspect.getsource(b.try_billing_start_reply)
    assert "ValueError" in src and "Не разобрал дату" in src


def test_nothing_is_defined_after_the_launch_block():
    """
    Всё, что дописано после `if __name__ == "__main__"`, до запуска бота
    не доходит: строки ниже просто не выполняются. Однажды так и вышло —
    раздел «Оплаты» дописали в конец файла, и бот упал на старте
    с NameError, не сказав ничего внятного.
    """
    import ast
    import pathlib

    src = pathlib.Path(__file__).parent / "bot.py"
    tree = ast.parse(src.read_text(encoding="utf-8"))
    last = tree.body[-1]

    assert isinstance(last, ast.If), (
        "последним в bot.py должен быть блок запуска, а не "
        f"{type(last).__name__} на строке {last.lineno}")
    assert "__main__" in ast.dump(last.test)


def test_startup_calls_only_names_that_exist():
    """
    Прямая проверка того, из-за чего бот и упал: всё, что вызывает main(),
    должно существовать в модуле к моменту запуска.
    """
    import ast
    import builtins
    import inspect

    import bot as b

    tree = ast.parse(inspect.getsource(b.main))
    local = {a.arg for f in ast.walk(tree)
             if isinstance(f, ast.arguments) for a in f.args}
    local |= {t.id for n in ast.walk(tree)
              if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Store)
              for t in [n]}

    missing = sorted(
        n.id for n in ast.walk(tree)
        if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)
        and n.id not in local and not hasattr(builtins, n.id)
        and not hasattr(b, n.id))

    assert missing == [], f"main() зовёт несуществующее: {missing}"
