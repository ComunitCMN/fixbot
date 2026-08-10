"""Язык ответа и разбор названия группы."""

import pytest

import agencies as ag
import inspect

import i18n
import texts
from phones import normalize
from verdict import Decision, Verdict


# ===================== определение языка =====================

@pytest.mark.parametrize("text,expected", [
    ("Фиксирую клиента Иванов", i18n.RU),
    ("зафиксируйте плиз", i18n.RU),
    ("Register client John Smith", i18n.EN),
    ("please fix this client", i18n.EN),
    ("Фиксирую Wayan Sukma, вилла Bali", i18n.RU),   # латиница внутри русского
    ("Client: Иванов", i18n.RU),                     # одного русского слова хватит
])
def test_detect(text, expected):
    assert i18n.detect(text) == expected


def test_detect_without_letters_uses_default():
    """«+62 812 3456 7890» — букв нет, гадать не о чем."""
    assert i18n.detect("+62 812 3456 7890", i18n.EN) == i18n.EN
    assert i18n.detect("+7 999 123-45-67", i18n.RU) == i18n.RU
    assert i18n.detect("", i18n.EN) == i18n.EN


def test_detect_many_more_stable():
    """По одному короткому сообщению легко ошибиться, по нескольким — нет."""
    assert i18n.detect_many(["ok", "спасибо, зафиксируйте"]) == i18n.RU
    assert i18n.detect_many(["ok", "thanks, register him"]) == i18n.EN


def test_normalize_lang():
    assert i18n.normalize_lang("EN") == i18n.EN
    assert i18n.normalize_lang("ru-RU") == i18n.RU
    assert i18n.normalize_lang("de", i18n.RU) == i18n.RU
    assert i18n.normalize_lang(None) == i18n.RU


# ===================== полнота перевода =====================

def test_all_keys_translated():
    """Механическая проверка: ни одна строка не забыта в переводе."""
    ru = set(texts.STR[i18n.RU])
    en = set(texts.STR[i18n.EN])
    assert ru == en, f"расходятся: {ru ^ en}"


def test_no_russian_left_in_english():
    """В английских строках не должно остаться кириллицы."""
    import re

    cyr = re.compile(r"[а-яёА-ЯЁ]")
    bad = [k for k, v in texts.STR[i18n.EN].items() if cyr.search(v)]
    assert not bad, f"кириллица в переводе: {bad}"


@pytest.mark.parametrize("lang", [i18n.RU, i18n.EN])
@pytest.mark.parametrize("verdict", list(Verdict))
def test_every_verdict_renders_in_both_languages(lang, verdict):
    d = Decision(verdict)
    d.other_agency_since = 1_770_000_000
    d.retail_activity = 1_700_000_000
    d.retail_since = 1_690_000_000
    d.own_since = 1_760_000_000
    out = texts.render(d, client="Ivan", p=normalize("+79991234567"),
                       agency="TEUS", lang=lang)
    assert out and len(out) > 20
    assert "amoCRM" not in out


def test_english_card_has_no_cyrillic():
    import re

    out = texts.confirm_card(
        client="John Smith", p=normalize("+62 812 3456 78**"),
        agency="Century 21", agent="Mike", object_="Villa Bali",
        verdict_note=texts.confirm_note_unique(i18n.EN), lang=i18n.EN)
    assert not re.search(r"[а-яёА-ЯЁ]", out), out


def test_masked_note_both_languages():
    p = normalize("+7 999 123-45-**")
    assert "не хватает" in texts.masked_note(p, i18n.RU)
    assert "missing" in texts.masked_note(p, i18n.EN)


def test_variable_length_note_does_not_ask_for_more():
    """
    В странах с плавающей длиной номера просить дописать бессмысленно:
    фиксация всё равно проходит. Просто сообщаем факт.
    """
    p = normalize("+62 812 3456 7890", "ID")
    for lang in (i18n.RU, i18n.EN):
        note = texts.masked_note(p, lang)
        assert note
        for beg in ("поставьте", "пришлите", "replace them", "send the"):
            assert beg not in note.lower()


def test_payload_language_survives_confirmation():
    """
    Карточка приходила на английском, а подтверждение — на русском:
    язык не клали в заявку. Проверяем, что оба текста берут его оттуда.
    """
    import inspect
    import pathlib

    src = pathlib.Path(
        inspect.getfile(texts)).parent.joinpath("bot.py").read_text()
    # обе ветки on_message должны класть язык в заявку
    payloads = src.count('"username": m.from_user.username')
    with_lang = src.count('"username": m.from_user.username, "lang": lang')
    assert payloads == with_lang + 1, "есть заявка без языка"
    assert '"lang": lang,' in src


# ===================== агентство из названия группы =====================

@pytest.mark.parametrize("title,developer,expected", [
    ("TEUS & agency", "Squaresell", "TEUS"),
    ("TEUS & Squaresell", "Squaresell", "TEUS"),
    ("Дом+ х Ромашка", "Ромашка", "Дом+"),
    ("BREIG | Партнёры", "Squaresell", "BREIG"),
    ("Century 21 — Squaresell", "Squaresell", "Century 21"),
    ("АН Новосёл / Ромашка", "Ромашка", "АН Новосёл"),
])
def test_agency_from_title(title, developer, expected):
    assert ag.agency_from_chat_title(title, developer) == expected


def test_agency_from_title_prefers_known():
    """Если один из кусков уже в справочнике — берём именно его."""
    known = [{"name": "Дом+", "norm": ag.norm_agency("Дом+"), "agency_id": 1}]
    got = ag.agency_from_chat_title("Ромашка x дом плюс", "Ромашка", known)
    assert got == "Дом+"


def test_agency_from_title_gives_up_gracefully():
    """Ничего осмысленного — лучше промолчать и спросить человека."""
    assert ag.agency_from_chat_title("", "Ромашка") is None
    assert ag.agency_from_chat_title("Чат", "Ромашка") is None
    assert ag.agency_from_chat_title("Ромашка", "Ромашка") is None


def test_agency_from_title_ignores_placeholders():
    """
    «Agency name & BREIG» — «name» это заготовка, а не агентство.
    Раньше бот честно предлагал зафиксировать агентство «name».
    """
    assert ag.agency_from_chat_title("Agency name & BREIG", "BREIG") is None
    assert ag.agency_from_chat_title("Название агентства х Ромашка",
                                     "Ромашка") is None
    assert ag.agency_from_chat_title("Agency name", "BREIG") is None


def test_agency_from_title_without_developer():
    """Название застройщика может быть не задано — тогда берём первый кусок."""
    assert ag.agency_from_chat_title("TEUS & Squaresell", None) == "TEUS"


def test_setup_texts_both_languages():
    for lang in (i18n.RU, i18n.EN):
        out = texts.setup_group("TEUS", lang)
        assert "TEUS" in out
        assert texts.setup_done("TEUS", lang)
    assert "Is that right" in texts.setup_group("TEUS", i18n.EN)


# ===================== язык человека, а не группы =====================

def test_letters_are_counted_without_digits():
    assert i18n.letters("+7 999 123-45-67") == 0
    assert not i18n.has_letters("+7 999 123-45-**")
    assert i18n.has_letters("Иван +7 999")


def test_short_replies_do_not_rewrite_a_persons_language():
    """Одно «ok» не должно переключить человека на английский навсегда."""
    assert not i18n.confident("ok")
    assert not i18n.confident("да")
    assert i18n.confident("Зафиксируйте клиента")
    assert i18n.confident("please register")


def test_reply_follows_the_message_not_the_group():
    """
    Главное правило: отвечаем человеку, а не группе. Англоязычный агент
    в русской группе должен получить английский ответ.
    """
    import bot as b

    src = inspect.getsource(b.chat_lang)
    # Определение по тексту должно стоять раньше привязки чата.
    assert src.index("has_letters") < src.index("chat_lang:")


def test_pinned_language_is_the_fallback_for_bare_numbers():
    """Судить не по чему — тогда язык берётся из привязки группы."""
    import bot as b

    src = inspect.getsource(b.chat_lang)
    tail = src.split("has_letters", 1)[1]
    assert "chat_lang:" in tail and "normalize_lang" in tail


def test_profile_language_comes_from_the_person():
    """
    В личку человеку пишем на его языке. Раньше туда записывался язык
    группы — и англоязычный агент из русского чата получал уведомления
    по-русски.
    """
    import bot as b

    src = inspect.getsource(b.on_message)
    line = [ln for ln in src.splitlines() if "set_agent_field" in ln][0]
    assert "i18n.detect(text" in line, line

    # Обновляем только на достаточно длинных сообщениях.
    head = src.split("set_agent_field", 1)[0]
    assert "i18n.confident(text)" in head[-400:]
