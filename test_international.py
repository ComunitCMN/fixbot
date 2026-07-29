"""Номера разных стран: полные, маскированные, местные записи."""

import pytest

import phones
from db import Db
from phones import compare_prefix, from_digits, normalize
from verdict import Verdict, decide


# ===================== полные номера =====================

@pytest.mark.parametrize("raw,region,digits,country", [
    ("+7 999 123-45-67", "RU", "79991234567", "RU"),
    ("8 999 123 45 67", "RU", "79991234567", "RU"),
    ("9991234567", "RU", "79991234567", "RU"),
    ("+62 812 3456 7890", "RU", "6281234567890", "ID"),
    ("0812 3456 7890", "ID", "6281234567890", "ID"),
    ("+971 50 123 4567", "RU", "971501234567", "AE"),
    ("+90 532 123 45 67", "RU", "905321234567", "TR"),
    ("+66 81 234 5678", "RU", "66812345678", "TH"),
    ("+7 701 123 45 67", "RU", "77011234567", "KZ"),
    ("+995 555 12 34 56", "RU", "995555123456", "GE"),
])
def test_full_numbers(raw, region, digits, country):
    p = normalize(raw, region)
    assert p is not None, raw
    assert p.digits == digits
    assert p.region == country
    assert p.is_full and p.is_usable


def test_plus_beats_region_hint():
    """Явный код страны важнее подсказки чата."""
    p = normalize("+62 812 3456 7890", "RU")
    assert p.region == "ID"


def test_local_format_depends_on_region():
    """Одни и те же цифры в разных чатах читаются по-разному."""
    ru = normalize("8 999 123 45 67", "RU")
    assert ru.region == "RU" and ru.digits == "79991234567"

    idn = normalize("0812 3456 7890", "ID")
    assert idn.region == "ID" and idn.digits == "6281234567890"


# ===================== маскированные =====================

@pytest.mark.parametrize("raw,region,known", [
    ("+7 999 123-45-**", "RU", 9),
    ("8 999 123 45...", "RU", 9),
    ("+7 999 123-45-6x", "RU", 10),
    ("+62 812 3456 78**", "RU", 11),
    ("+971 50 123 45**", "RU", 10),
    ("+90 532 123 45 6*", "RU", 11),
])
def test_masked_numbers_usable(raw, region, known):
    p = normalize(raw, region)
    assert p is not None, raw
    assert p.known == known
    assert not p.is_full
    assert p.is_usable, f"{raw}: не хватает {p.missing}"


def test_masked_in_variable_length_country_not_taken_as_full():
    """
    В Индонезии номер бывает 11-13 цифр. Одиннадцатизначный сам по себе
    допустим, поэтому без учёта звёздочек его приняли бы за полный.
    """
    p = normalize("+62 812 3456 78**", "RU")
    assert not p.is_full
    assert p.expected > p.known


def test_too_short_rejected():
    p = normalize("+7 999 123-4*-**", "RU")
    assert p is not None and not p.is_usable
    assert p.missing == 3


# ===================== сравнение =====================

def test_masked_finds_full_indonesia():
    masked = normalize("+62 812 3456 78**", "RU")
    full = normalize("+62 812 3456 7890", "RU")
    assert compare_prefix(masked, full)
    assert compare_prefix(full, masked)


def test_different_countries_never_match():
    """Одинаковый хвост в разных странах — разные люди."""
    ru = normalize("+7 999 123 45 67", "RU")
    kz = normalize("+7 701 123 45 67", "RU")
    assert not compare_prefix(ru, kz)


def test_masked_does_not_match_other_number():
    a = normalize("+62 812 3456 78**", "RU")
    b = normalize("+62 812 3456 1234", "RU")
    assert not compare_prefix(a, b)


def test_from_digits_restores_expected():
    """Зеркало хранит только цифры — длину надо восстановить."""
    p = from_digits("79991234567")
    assert p.expected == 11 and p.is_full and p.region == "RU"

    masked = from_digits("799912345")
    assert masked.expected == 11 and masked.missing == 2


# ===================== мусор =====================

@pytest.mark.parametrize("raw", ["", "привет", "123", "0", "не телефон"])
def test_junk_rejected(raw):
    assert normalize(raw, "RU") is None


@pytest.mark.parametrize("raw", [
    "667653892134",      # случайный набор цифр из чата
    "12345678901234",
    "99999999999999",
    "1111111111111",
])
def test_long_garbage_rejected(raw):
    """
    Раньше такое проходило: у России есть служебные номера до 14 цифр,
    и под их длину подходил любой мусор — 667653892134 превращалось
    в «+7 667653892134**». Проверяем только обычные телефоны.
    """
    assert normalize(raw, "RU") is None


def test_landline_still_accepted():
    """Городские номера отбрасывать нельзя — клиенты дают и такие."""
    p = normalize("+7 495 123 45 67", "RU")
    assert p is not None and p.is_full and p.region == "RU"


def test_extract_from_mixed_text():
    text = ("Фиксирую Кузнецову Ольгу, тел +62 812 3456 78**, "
            "вилла на Бали")
    found = phones.extract_all(text, "ID")
    assert any(f.digits.startswith("62812345678") for f in found)


# ===================== сквозной сценарий =====================

def test_indonesian_client_blocked_by_retail(tmp_path):
    """Индонезийский номер должен работать так же, как российский."""
    db = Db(tmp_path / "i.db")
    db.replace_contacts([
        {"id": 1, "name": "Wayan", "phones": ["+62 812 3456 7890"]},
    ])
    db.replace_origins([
        {"contact_id": 1, "has_retail": 1, "last_retail_activity": 1_770_000_000},
    ])
    hits = db.find_matches(normalize("+62 812 3456 78**", "RU"))
    assert len(hits) == 1
    d = decide(hits, agency_id=1, agent_telegram_id=10, now=1_770_000_100)
    assert d.verdict is Verdict.RETAIL_BLOCKED


def test_variable_length_country_flagged():
    """
    В Индонезии 11-значный номер допустим, но может быть и обрезанным
    13-значным. Отличить нельзя — значит надо предупредить.
    """
    import texts

    idn = normalize("+62 812 3456 78", "ID")
    assert idn.is_full                    # по длине придраться не к чему
    assert idn.could_be_longer            # но бывают и длиннее
    assert "бывают" in texts.masked_note(idn)


def test_fixed_length_country_not_flagged():
    """В России длина одна, лишний раз пугать незачем."""
    import texts

    ru = normalize("+7 999 123 45 67", "RU")
    assert ru.is_full and not ru.could_be_longer
    assert texts.masked_note(ru) == ""


def test_short_indonesian_still_matches_long(tmp_path):
    """Предупреждение предупреждением, а искать совпадения это не мешает."""
    short = normalize("+62 812 3456 78", "ID")
    long = normalize("+62 812 3456 7890", "ID")
    assert compare_prefix(short, long)


def test_pretty_shows_country_format():
    assert normalize("+62 812 3456 7890", "RU").pretty() == "+62 812-3456-7890"
    assert normalize("+7 999 123 45 67", "RU").pretty() == "+7 999 123-45-67"
    assert "*" in normalize("+7 999 123-45-**", "RU").pretty()
