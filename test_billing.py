"""Обслуживание: сумма, календарь периодов, что делать сегодня."""

from datetime import date, timedelta

import pytest

import billing as b

PLAN = b.Plan()          # до 100 — $40, от 100 — $70
DUE = date(2026, 11, 5)


# ===================== сумма =====================

@pytest.mark.parametrize("fixations,expected", [
    (0, 40), (1, 40), (99, 40),
    (100, 70), (101, 70), (5000, 70),
])
def test_amount_by_tier(fixations, expected):
    assert PLAN.amount(fixations) == expected


def test_threshold_is_inclusive():
    """«От ста» — это со ста, а не со ста первого."""
    assert PLAN.amount(99) == 40
    assert PLAN.amount(100) == 70


def test_plan_is_per_client():
    """У следующего клиента будут свои цифры — менять код не придётся."""
    other = b.Plan(threshold=50, low=25, high=45, currency="EUR")
    assert other.amount(49) == 25
    assert other.amount(50) == 45
    assert other.currency == "EUR"


# ===================== календарь =====================

def test_month_is_added_without_overshoot():
    """31 января + месяц — конец февраля, иначе срок уползал бы вперёд."""
    assert b.add_month(date(2026, 1, 31)) == date(2026, 2, 28)
    assert b.add_month(date(2028, 1, 31)) == date(2028, 2, 29)   # високосный
    assert b.add_month(date(2026, 3, 31)) == date(2026, 4, 30)


def test_month_rolls_over_the_year():
    assert b.add_month(date(2026, 12, 5)) == date(2027, 1, 5)


def test_day_of_month_does_not_drift():
    """Начали 31-го — в марте срок снова 31-е, а не 28-е."""
    d = date(2026, 1, 31)
    for expected in (date(2026, 2, 28), date(2026, 3, 31), date(2026, 4, 30)):
        d = b.add_month(date(2026, 1, 31), (expected.month - 1))
        assert d == expected


def test_first_period_of_eco_invest():
    """Обслуживание с 5 октября — первый срок 5 ноября."""
    assert b.open_period(date(2026, 10, 5)) == (date(2026, 10, 5),
                                                date(2026, 11, 5))


def test_next_period_starts_where_previous_ended():
    """Заплатил 20 ноября за ноябрь — следующий счёт всё равно 5 декабря."""
    assert b.open_period(date(2026, 10, 5), date(2026, 11, 5)) == (
        date(2026, 11, 5), date(2026, 12, 5))


def test_unpaid_period_does_not_roll_over():
    """
    Периоды двигает оплата, а не календарь. Иначе неоплаченный ноябрь
    уехал бы в прошлое вместе с напоминаниями, и клиент, не заплативший
    ни разу, не услышал бы ни слова.
    """
    start, unpaid = date(2026, 10, 5), None
    for _ in range(5):                       # прошло пять месяцев молчания
        assert b.open_period(start, unpaid)[1] == date(2026, 11, 5)


# ===================== что делать сегодня =====================

def act(today, **state):
    return b.decide(due=DUE, today=today, state=b.State(**state))


def test_quiet_until_three_days_before():
    assert act(date(2026, 11, 1)) is b.Action.NOTHING
    assert act(date(2026, 11, 2)) is b.Action.PREPARE_INVOICE


def test_prepare_invoice_window():
    for day in (2, 3, 4):
        assert act(date(2026, 11, day)) is b.Action.PREPARE_INVOICE


def test_operator_is_nudged_if_invoice_never_went_out():
    """Оператор не нажал — толкаем его, а не наказываем клиента."""
    assert act(date(2026, 11, 5), prepared=True) is b.Action.NUDGE_OPERATOR
    assert act(date(2026, 11, 30), prepared=True) is b.Action.NUDGE_OPERATOR


def test_client_is_never_paused_for_operator_silence():
    """
    Самое важное правило: пока счёт не отправлен, приостановки нет.
    Иначе агенты Романа перестали бы работать из-за забывчивости оператора.
    """
    for day in range(1, 60):
        today = date(2026, 11, 5) + __import__("datetime").timedelta(days=day)
        assert act(today) is not b.Action.PAUSE


def test_reminder_to_client_after_a_week():
    assert act(date(2026, 11, 11), invoice_sent=True) is b.Action.NOTHING
    assert act(date(2026, 11, 12), invoice_sent=True) is b.Action.REMIND_CLIENT


def test_reminder_happens_once():
    assert act(date(2026, 11, 20), invoice_sent=True,
               reminded=True) is not b.Action.REMIND_CLIENT


def test_warning_the_day_before_pause():
    s = dict(invoice_sent=True, reminded=True)
    assert act(date(2026, 11, 17), **s) is b.Action.NOTHING
    assert act(date(2026, 11, 18), **s) is b.Action.WARN_OPERATOR


def test_pause_on_the_fourteenth_day():
    s = dict(invoice_sent=True, reminded=True, warned=True)
    assert act(date(2026, 11, 18), **s) is b.Action.NOTHING
    assert act(date(2026, 11, 19), **s) is b.Action.PAUSE


def test_warning_cannot_be_skipped_after_downtime():
    """
    Сервер лежал неделю и очнулся на 20-й день. Приостановка не должна
    случиться внезапно — сначала предупреждение оператору.
    """
    s = dict(invoice_sent=True, reminded=True)
    assert act(date(2026, 11, 25), **s) is b.Action.WARN_OPERATOR
    assert act(date(2026, 11, 25), warned=True, **s) is b.Action.PAUSE


def test_payment_stops_everything():
    for day in (date(2026, 11, 2), date(2026, 11, 12), date(2026, 12, 1)):
        assert act(day, invoice_sent=True, paid=True) is b.Action.NOTHING


def test_paused_client_is_not_nagged_further():
    assert act(date(2026, 12, 1), invoice_sent=True, reminded=True,
               warned=True, paused=True) is b.Action.NOTHING


# ===================== начало периода =====================

BEGIN = date(2026, 10, 5)


def act2(today, **state):
    return b.decide(begin=BEGIN, due=DUE, today=today, state=b.State(**state))


def test_client_is_told_when_the_paid_month_starts():
    """
    Роман должен узнать про начало платного периода заранее, а не получить
    внезапный счёт: о цене договаривались устно, и подтвердить её письменно
    в интересах обоих.
    """
    assert act2(date(2026, 10, 4)) is b.Action.NOTHING
    assert act2(date(2026, 10, 5)) is b.Action.ANNOUNCE_START


def test_announcement_happens_once():
    assert act2(date(2026, 10, 20), announced=True) is b.Action.NOTHING


def test_announcement_is_not_missed_after_downtime():
    """Сервер лежал — сообщение уходит позже, но уходит."""
    assert act2(date(2026, 10, 12)) is b.Action.ANNOUNCE_START


def test_announcement_goes_before_the_invoice():
    """Иначе счёт пришёл бы раньше, чем объяснение, за что он."""
    assert act2(date(2026, 11, 2)) is b.Action.ANNOUNCE_START
    assert act2(date(2026, 11, 2), announced=True) is b.Action.PREPARE_INVOICE


def test_paused_client_gets_no_announcements():
    assert act2(date(2026, 10, 5), paused=True) is b.Action.NOTHING


# ===================== текст для клиента =====================

def test_period_text_states_price_date_and_what_counts():
    import texts

    out = texts.period_started(begin=BEGIN, due=DUE, plan=PLAN, lang="ru")
    assert "5 октября" in out and "5 ноября" in out
    assert "$40" in out and "$70" in out and "100" in out
    # Главное для спокойствия клиента — что именно считают.
    assert "CRM" in out and "нажал кнопку" in out
    assert "{" not in out


def test_period_text_has_both_languages():
    import texts

    for lang in ("ru", "en"):
        out = texts.period_started(begin=BEGIN, due=DUE, plan=PLAN, lang=lang)
        assert out and "{" not in out
    assert "Service period" in texts.period_started(
        begin=BEGIN, due=DUE, plan=PLAN, lang="en")


def test_repeat_periods_are_short():
    """Каждый месяц пересказывать условия — превратить письмо в шум."""
    import texts

    first = texts.period_started(begin=BEGIN, due=DUE, plan=PLAN)
    later = texts.period_started(begin=BEGIN, due=DUE, plan=PLAN, first=False)
    assert len(later) < len(first) / 3
    assert "$40" not in later


def test_period_text_never_mentions_a_wallet():
    """Реквизиты уходят только со счётом, который подтвердил оператор."""
    import texts

    for lang in ("ru", "en"):
        low = texts.period_started(begin=BEGIN, due=DUE, plan=PLAN,
                                   lang=lang).lower()
        for word in ("usdt", "trc", "0x", "адрес кошель", "wallet address"):
            assert word not in low


def test_operator_is_asked_about_details_only_once():
    """Три одинаковых сообщения подряд человек начнёт пролистывать."""
    assert act(date(2026, 11, 2)) is b.Action.PREPARE_INVOICE
    assert act(date(2026, 11, 3), prepared=True) is b.Action.NOTHING
    assert act(date(2026, 11, 4), prepared=True) is b.Action.NOTHING


def test_nudges_start_only_after_the_due_date():
    assert act(date(2026, 11, 5), prepared=True) is b.Action.NUDGE_OPERATOR


def test_operator_is_nudged_weekly_not_daily():
    """
    Одинаковое сообщение каждый день перестают замечать через три дня —
    и напоминание превращается в фон, который ничего не напоминает.
    """
    first = date(2026, 11, 5)
    assert act(first, prepared=True) is b.Action.NUDGE_OPERATOR
    for day in range(1, 7):
        assert act(first + timedelta(days=day), prepared=True,
                   last_nudge=first) is b.Action.NOTHING
    assert act(first + timedelta(days=7), prepared=True,
               last_nudge=first) is b.Action.NUDGE_OPERATOR


# ===================== хранение у оператора =====================

def _db(tmp_path):
    from db import Db
    d = Db(tmp_path / "op.db")
    d.set_billing("eco", start_date="2026-10-05")
    return d


def test_settings_round_trip(tmp_path):
    d = _db(tmp_path)
    row = d.get_billing("eco")
    assert row["start_date"] == "2026-10-05"
    assert (row["threshold"], row["low"], row["high"]) == (100, 40, 70)
    assert row["paused"] == 0


def test_wallet_is_stored_in_the_database(tmp_path):
    """
    Кошелёк меняется чаще, чем настройки на сервере, и в git ему нельзя.
    """
    d = _db(tmp_path)
    d.set_wallet("eco", "TXxx7fA", "USDT TRC-20", qr="AgACAgIAAx")
    row = d.get_billing("eco")
    assert (row["wallet"], row["wallet_note"]) == ("TXxx7fA", "USDT TRC-20")
    assert row["wallet_qr"] == "AgACAgIAAx"


def test_wallet_qr_survives_address_change(tmp_path):
    """Меняя адрес, картинку не теряем — она обновляется отдельно."""
    d = _db(tmp_path)
    d.set_wallet("eco", "A", "TRC-20", qr="QR1")
    d.set_wallet("eco", "B", "TRC-20")
    assert d.get_billing("eco")["wallet_qr"] == "QR1"


def test_period_is_created_on_first_touch(tmp_path):
    d = _db(tmp_path)
    row = d.period("eco", "2026-11-05", "2026-10-05")
    assert row["begin"] == "2026-10-05"
    assert row["invoice_sent"] == 0 and row["paid_at"] is None


def test_period_flags_are_remembered(tmp_path):
    d = _db(tmp_path)
    d.period("eco", "2026-11-05", "2026-10-05")
    d.mark_period("eco", "2026-11-05", invoice_sent=1, fixations=137, amount=70)
    row = d.period("eco", "2026-11-05")
    assert (row["invoice_sent"], row["fixations"], row["amount"]) == (1, 137, 70)


def test_unknown_period_field_is_refused(tmp_path):
    """Опечатка в имени поля молча ничего не сохранила бы."""
    d = _db(tmp_path)
    d.period("eco", "2026-11-05", "2026-10-05")
    with pytest.raises(ValueError, match="неизвестные поля"):
        d.mark_period("eco", "2026-11-05", invoce_sent=1)


def test_payment_closes_period_and_unpauses(tmp_path):
    """Оплата обязана снимать приостановку — иначе бот останется немым."""
    d = _db(tmp_path)
    d.period("eco", "2026-11-05", "2026-10-05")
    d.set_paused("eco", True)

    d.close_period("eco", "2026-11-05", paid_at=1763000000)

    row = d.get_billing("eco")
    assert row["closed_due"] == "2026-11-05"
    assert row["paused"] == 0
    assert d.period("eco", "2026-11-05")["paid_at"] == 1763000000


def test_history_is_kept_for_disputes(tmp_path):
    """Если клиент заспорит о сумме, показать будет что."""
    d = _db(tmp_path)
    for due in ("2026-11-05", "2026-12-05", "2027-01-05"):
        d.period("eco", due, due)
        d.mark_period("eco", due, fixations=10, amount=40)
    hist = d.period_history("eco")
    assert [h["due"] for h in hist] == ["2027-01-05", "2026-12-05", "2026-11-05"]


def test_clients_are_listed_for_the_daily_check(tmp_path):
    d = _db(tmp_path)
    d.set_billing("romashka", start_date="2026-11-01", low=25, high=45)
    assert [r["slug"] for r in d.all_billing()] == ["eco", "romashka"]
