"""Тесты логики маскированных номеров и поиска дублей."""

import phones
from db import Db
from phones import Phone, compare_prefix, normalize


def test_normalize_full():
    assert normalize("+7 (999) 123-45-67").digits == "79991234567"
    assert normalize("89991234567").digits == "79991234567"
    assert normalize("9991234567").digits == "79991234567"
    assert normalize("7 999 123 45 67").digits == "79991234567"


def test_normalize_masked():
    assert normalize("+7 999 123-45-**").digits == "799912345"     # 9 цифр
    assert normalize("+7 999 123-4*-**").digits == "79991234"      # 8
    assert normalize("8 999 123 45...").digits == "799912345"
    assert normalize("+79991234xxx").digits == "79991234"
    assert normalize("+7 999 123 45 6_").digits == "7999123456"    # 10


def test_normalize_junk():
    assert normalize("") is None
    assert normalize("привет") is None
    assert normalize("123") is None


def test_foreign_numbers_are_accepted_now():
    """Раньше всё, кроме российского, отбрасывалось. Фиксации идут из разных стран."""
    uk = normalize("+44 20 7946 0958")
    assert uk is not None and uk.region == "GB"


def test_usable_threshold():
    assert normalize("+79991234567").is_usable          # 11 — ок
    assert normalize("+7 999 123-45-6*").is_usable      # 10 — ок
    assert normalize("+7 999 123-45-**").is_usable      # 9 — ок, граница
    assert not normalize("+7 999 123-4*-**").is_usable  # 8 — мало
    assert not normalize("+7 999 12*-**-**").is_usable  # 7 — мало


def test_missing_count():
    p = normalize("+7 999 123-4*-**")
    assert p.known == 8 and p.missing == 3
    assert p.min_compare - p.known == 1                # просим ещё 1 цифру


def test_compare_prefix_masked_vs_full():
    masked = normalize("+7 999 123-45-**")
    full = normalize("+7 999 123-45-67")
    assert compare_prefix(masked, full)
    assert compare_prefix(full, masked)


def test_compare_prefix_masked_vs_masked():
    a = normalize("+7 999 123-45-**")
    b = normalize("+7 999 123-45-6*")
    assert compare_prefix(a, b)


def test_compare_prefix_different():
    a = normalize("+7 999 123-45-**")
    b = normalize("+7 999 123-46-**")
    assert not compare_prefix(a, b)


def test_compare_prefix_too_short():
    a = normalize("+7 999 123-4*-**")   # 8 цифр
    b = normalize("+7 999 123-45-67")
    assert not compare_prefix(a, b)     # мало данных — не утверждаем дубль


def test_ambiguity():
    assert phones.ambiguity(normalize("+79991234567")) == 1
    assert phones.ambiguity(normalize("+7 999 123-45-6*")) == 10
    assert phones.ambiguity(normalize("+7 999 123-45-**")) == 100


def test_extract_from_text():
    text = "Фиксирую клиента Петров, тел +7 916 445-22-**, объект Восход"
    found = phones.extract_all(text)
    assert found and found[0].digits == "791644522"


def test_db_finds_masked_duplicate(tmp_path):
    db = Db(tmp_path / "t.db")
    db.replace_contacts([
        {"id": 1, "name": "Иванов Иван", "phones": ["+7 999 123-45-67"],
         "created_at": 1700000000},
        {"id": 2, "name": "Сидоров", "phones": ["+7 999 555-11-22"],
         "created_at": 1700000000},
    ])
    # приходит маскированный — должен найти Иванова
    hits = db.find_matches(normalize("+7 999 123-45-**"))
    assert len(hits) == 1 and hits[0].name == "Иванов Иван"

    # чужой номер — пусто
    assert db.find_matches(normalize("+7 999 777-88-**")) == []


def test_db_masked_in_crm_matches_full(tmp_path):
    """В CRM лежит НЕПОЛНЫЙ номер, приходит полный — тоже совпадение."""
    db = Db(tmp_path / "t2.db")
    db.replace_contacts([
        {"id": 7, "name": "Маскированный", "phones": ["+7 916 445-22-**"],
         "created_at": 1700000000},
    ])
    hits = db.find_matches(normalize("+7 916 445-22-31"))
    assert len(hits) == 1 and hits[0].contact_id == 7


def test_db_finds_chat_fixation(tmp_path):
    db = Db(tmp_path / "t3.db")
    db.log_fixation(digits="799912345", client_name="Петров",
                    chat_title="Чат агентства", agent_name="Аня",
                    amo_lead_id=99)
    hits = db.find_matches(normalize("+7 999 123-45-67"))
    assert len(hits) == 1 and hits[0].source == "chat"


def test_pretty():
    assert normalize("+79991234567").pretty() == "+7 999 123-45-67"
    assert normalize("+7 999 123-45-**").pretty() == "+7 999 123-45-**"
