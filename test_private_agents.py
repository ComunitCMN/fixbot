"""Частные агенты: как представляются и что видит владелец."""

import pytest

import private_agents as pa


# ===================== разбор имени =====================

@pytest.mark.parametrize("text", [
    "Иван Петров", "Иван", "Мария Анна Соколова-Белых",
    "John Smith", "  Пётр   Ильич  ",
])
def test_real_names_pass(text):
    assert pa.looks_like_name(text)


@pytest.mark.parametrize("text", [
    "", "ок", "да", "+7 999 123-45-67", "Иван 89991234567",
    "привет как дела я хочу зафиксировать клиента срочно",
    "@ivan_petrov", "Иван\nПетров", "!!!",
])
def test_junk_does_not_pass(text):
    """
    Строго нарочно: карточка с мусором приучит владельца не читать
    заявки, и он пропустит настоящую.
    """
    assert not pa.looks_like_name(text)


def test_name_is_tidied_up():
    assert pa.clean_name("  Иван   Петров  ") == "Иван Петров"


def test_very_long_input_is_cut():
    assert len(pa.clean_name("Иван " * 50)) <= pa.MAX_LEN


# ===================== тексты =====================

def test_all_texts_exist_in_both_languages():
    for block in (pa.ASK_INTRO, pa.APPLIED, pa.APPROVED, pa.DECLINED):
        for lang in ("ru", "en"):
            assert block[lang]


def test_invitation_explains_both_paths():
    """Из агентства — в чат; сам по себе — представиться."""
    out = pa.ASK_INTRO["ru"]
    assert "агентства" in out and "рабочем чате" in out
    assert "имя" in out


def test_card_shows_what_the_owner_needs():
    out = pa.application_card("Иван Петров", "ivan", 42)
    assert "Иван Петров" in out and "@ivan" in out and "42" in out
    # Владелец должен понимать, что именно он разрешает.
    assert "фиксировать" in out
    assert "{" not in out


def test_card_escapes_the_name():
    """Имя приходит от постороннего — в разметку его пускать нельзя."""
    out = pa.application_card("Иван <b>Петров</b>", None, 1)
    assert "&lt;b&gt;" in out


def test_decline_gives_no_reason():
    """Причина — дело застройщика, бот её не выдумывает и не пересказывает."""
    for lang in ("ru", "en"):
        low = pa.DECLINED[lang].lower()
        for word in ("отказ", "reject", "причин", "reason", "почему"):
            assert word not in low


# ===================== проводка в боте =====================

def test_stranger_is_offered_to_introduce_himself():
    import inspect

    import bot as b

    src = inspect.getsource(b.on_private_text)
    assert "Access.STRANGER" in src
    assert "handle_introduction" in src
    # Проверка доступа стоит раньше распознавания фиксации.
    assert src.index("access_of") < src.index("llm.prefilter")


def test_junk_creates_no_application():
    import inspect

    import bot as b

    src = inspect.getsource(b.handle_introduction)
    assert "looks_like_name" in src
    head = src.split("looks_like_name", 1)[0]
    assert "set_agent_status" not in head


def test_approval_creates_a_private_agency_and_opens_access():
    import inspect

    import bot as b

    src = inspect.getsource(b.cb_private_agent)
    assert "create_private_agency" in src
    assert 'set_agent_status(uid, "active")' in src
    assert "agency_id=aid" in src


def test_only_the_owner_decides():
    """Кнопку приёма не должен нажать сам заявитель."""
    import inspect

    import bot as b

    src = inspect.getsource(b.cb_private_agent)
    head = src.split("action", 1)[0]
    assert "has_menu" in head


def test_applicant_is_told_the_outcome():
    import inspect

    import bot as b

    src = inspect.getsource(b.cb_private_agent)
    assert "bot.send_message(uid" in src
