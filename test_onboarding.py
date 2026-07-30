"""Помощник подключения нового клиента и разворачивание."""

import time

import pytest

import onboarding as onb
import provision as pv
from db import Db

NOW = int(time.time())


# ===================== приглашения =====================

def test_invite_is_single_use(tmp_path):
    """
    Ссылка одноразовая: иначе кто угодно, получив её, начал бы подключать
    своих застройщиков к чужому серверу.
    """
    db = Db(tmp_path / "i.db")
    db.create_invite("abc123", created_by=1)

    assert db.use_invite("abc123", user_id=42) is None
    assert db.use_invite("abc123", user_id=99) == "used"


def test_same_user_can_resume(tmp_path):
    """Тот же человек может открыть ссылку повторно — он её и использовал."""
    db = Db(tmp_path / "i2.db")
    db.create_invite("abc", 1)
    assert db.use_invite("abc", 42) is None
    assert db.use_invite("abc", 42) is None


def test_invite_expires(tmp_path):
    db = Db(tmp_path / "i3.db")
    db.create_invite("old", 1, ttl_hours=1)
    db.conn.execute("UPDATE invites SET expires_at=? WHERE code=?",
                    (NOW - 10, "old"))
    db.conn.commit()
    assert db.use_invite("old", 42) == "expired"


def test_unknown_invite(tmp_path):
    db = Db(tmp_path / "i4.db")
    assert db.use_invite("нету", 42) == "not_found"


def test_code_is_unguessable():
    codes = {onb.new_code() for _ in range(500)}
    assert len(codes) == 500                    # совпадений быть не должно
    assert all(c.isalnum() and len(c) == 8 for c in codes)


def test_code_avoids_lookalike_characters():
    """Код иногда переписывают руками — ноль и «O» путать не надо."""
    all_chars = set("".join(onb.new_code() for _ in range(300)))
    assert not (all_chars & set("0O1lI"))


# ===================== состояние помощника =====================

def test_onboarding_flow_state(tmp_path):
    db = Db(tmp_path / "o.db")
    oid = db.start_onboarding("abc", 42, "ivan", "Иван")

    row = db.active_onboarding(42)
    assert row["id"] == oid
    assert row["step"] == "developer"
    assert row["data"] == {}

    db.update_onboarding(oid, step="bot_token", data={"developer": "Ромашка"})
    row = db.active_onboarding(42)
    assert row["step"] == "bot_token"
    assert row["data"]["developer"] == "Ромашка"


def test_new_attempt_closes_previous(tmp_path):
    """Иначе человек застрянет между двумя незакрытыми диалогами."""
    db = Db(tmp_path / "o2.db")
    first = db.start_onboarding("a", 42, "u", "И")
    second = db.start_onboarding("b", 42, "u", "И")

    assert db.active_onboarding(42)["id"] == second
    assert db.get_onboarding(first)["status"] == "rejected"


def test_ready_appears_in_pending(tmp_path):
    db = Db(tmp_path / "o3.db")
    oid = db.start_onboarding("a", 42, "u", "И")
    assert db.pending_onboardings() == []

    db.update_onboarding(oid, status="ready", slug="romashka")
    pending = db.pending_onboardings()
    assert len(pending) == 1 and pending[0]["slug"] == "romashka"

    db.update_onboarding(oid, status="done")
    assert db.pending_onboardings() == []


# ===================== имя папки =====================

@pytest.mark.parametrize("name,expected", [
    ("Ромашка", "romashka"),
    ("BREIG", "breig"),
    ("Дом+", "dom"),
    ("ЖК «Восход»", "zhk-voshod"),
    ("Century 21", "century-21"),
    ("  ", "client"),
])
def test_slugify(name, expected):
    assert onb.slugify(name) == expected


def test_slugify_avoids_collisions():
    assert onb.slugify("Ромашка", taken={"romashka"}) == "romashka-2"
    assert onb.slugify("Ромашка", taken={"romashka", "romashka-2"}) == "romashka-3"


def test_slugify_is_path_safe():
    """Имя попадёт в путь и в название службы — только латиница и дефис."""
    import re

    for bad in ("../взлом", "ЖК / Восход", "a b\tc", "Ромашка!!!"):
        s = onb.slugify(bad)
        assert re.fullmatch(r"[a-z0-9-]+", s), s
        assert ".." not in s and "/" not in s


# ===================== проверки =====================

@pytest.mark.asyncio
async def test_bot_token_format_checked_before_network():
    ok, msg = await onb.check_bot_token("не токен")
    assert not ok and "не похоже" in msg


@pytest.mark.asyncio
async def test_subdomain_format_checked():
    ok, msg = await onb.check_amo("не такой поддомен!", "token")
    assert not ok and "поддомен" in msg


# ===================== генерация настроек =====================

def test_render_env_contains_everything():
    text = pv.render_env(
        developer="Ромашка", bot_token="123:ABC", subdomain="romashka",
        amo_token="eyJ0", operator_ids={1, 2}, owner_ids={42},
        db_path="/opt/fixbot/clients/romashka/fixbot.db",
        inherited={"ANTHROPIC_API_KEY": "sk-ant-x", "DEFAULT_LANG": "ru"},
        stamp="01.09.2026")

    for part in ('TELEGRAM_TOKEN="123:ABC"', 'AMO_SUBDOMAIN="romashka"',
                 'AMO_LONG_TOKEN="eyJ0"', 'DEVELOPER_NAME="Ромашка"',
                 "OPERATOR_IDS=1,2", "OWNER_IDS=42",
                 'ANTHROPIC_API_KEY="sk-ant-x"', 'DEFAULT_LANG="ru"'):
        assert part in text


def test_render_env_quotes_values_with_spaces():
    """
    «Eco Invest Group» без кавычек ломает загрузку настроек: оболочка
    попытается выполнить «Invest» как команду.
    """
    text = pv.render_env(
        developer="Eco Invest Group", bot_token="1:A", subdomain="eco",
        amo_token="t", operator_ids={1}, owner_ids={2}, db_path="/a b/y.db")
    assert 'DEVELOPER_NAME="Eco Invest Group"' in text
    assert 'DB_PATH="/a b/y.db"' in text


def test_quote_escapes_dangerous_characters():
    assert pv.quote('он сказал "да"') == '"он сказал \\"да\\""'
    assert pv.quote(None) == '""'


def test_render_env_starts_in_observation_mode():
    """
    Новый клиент поднимается в режиме наблюдения: сначала смотрим, где
    бот промахивается на его формулировках, и только потом пишем в CRM.
    """
    text = pv.render_env(
        developer="Р", bot_token="1", subdomain="s", amo_token="t",
        operator_ids={1}, owner_ids={2}, db_path="/x/y.db")
    assert "DRY_RUN=1" in text


def test_collect_inherited_takes_only_shared():
    env = {
        "ANTHROPIC_API_KEY": "sk-ant-x",
        "DEFAULT_LANG": "en",
        "TELEGRAM_TOKEN": "чужой ключ",      # у каждого клиента свой
        "AMO_LONG_TOKEN": "чужой токен",
        "OWNER_IDS": "999",
        "RANDOM_VAR": "мусор",
    }
    got = pv.collect_inherited(env)
    assert got == {"ANTHROPIC_API_KEY": "sk-ant-x", "DEFAULT_LANG": "en"}


def test_collect_inherited_skips_empty():
    assert pv.collect_inherited({"MODEL": "", "DEFAULT_LANG": "ru"}) == \
        {"DEFAULT_LANG": "ru"}


# ===================== разворачивание =====================

@pytest.mark.asyncio
async def test_deploy_creates_folder_and_protects_env(tmp_path):
    report = await pv.deploy(clients_dir=str(tmp_path), slug="romashka",
                             env_text="TELEGRAM_TOKEN=secret\n")
    env = tmp_path / "romashka" / ".env"
    assert env.exists()
    assert env.read_text() == "TELEGRAM_TOKEN=secret\n"
    # В файле токены — читать его должен только владелец процесса.
    assert oct(env.stat().st_mode)[-3:] == "600"
    assert report["folder"].endswith("romashka")


@pytest.mark.asyncio
async def test_deploy_refuses_to_overwrite(tmp_path):
    await pv.deploy(clients_dir=str(tmp_path), slug="dup", env_text="A=1\n")
    with pytest.raises(pv.ProvisionError, match="уже существует"):
        await pv.deploy(clients_dir=str(tmp_path), slug="dup", env_text="A=2\n")


@pytest.mark.asyncio
async def test_deploy_requires_clients_dir():
    with pytest.raises(pv.ProvisionError, match="CLIENTS_DIR"):
        await pv.deploy(clients_dir="", slug="x", env_text="A=1\n")


@pytest.mark.asyncio
async def test_deploy_without_systemd_is_not_a_failure(tmp_path, monkeypatch):
    """
    На маке служб нет. Настройки всё равно создаются, а оператору честно
    сообщается, что запускать нужно вручную.
    """
    monkeypatch.setattr(pv, "has_systemd", lambda: False)
    report = await pv.deploy(clients_dir=str(tmp_path), slug="local",
                             env_text="A=1\n")
    assert not report["started"]
    assert "systemd" in report["log"]
    assert (tmp_path / "local" / ".env").exists()


def test_deploy_report_explains_manual_start():
    out = pv.deploy_report("romashka", {
        "folder": "/opt/fixbot/clients/romashka", "started": False,
        "log": "unit not found"}, "@romashka_bot")
    assert "не запустилась" in out
    assert "systemctl enable --now fixbot@romashka" in out


def test_deploy_report_success():
    out = pv.deploy_report("romashka", {
        "folder": "/opt/fixbot/clients/romashka", "started": True,
        "log": "active"}, "@romashka_bot")
    assert "работает" in out
    assert "@romashka_bot" in out


# ===================== тексты =====================

def test_every_step_has_question():
    for step in onb.STEPS:
        text = onb.ask_text(step)
        assert text and "Шаг" in text
        assert "{" not in text


def test_steps_numbered_consistently():
    assert "Шаг 1 из 5" in onb.ask_text("developer")
    assert "Шаг 5 из 5" in onb.ask_text("amo_token")


def test_token_step_promises_to_delete_message():
    """Секретам незачем лежать в истории — и человек должен это знать."""
    assert "удалю" in onb.ask_text("amo_token")


def test_summary_for_operator(tmp_path):
    db = Db(tmp_path / "s.db")
    oid = db.start_onboarding("a", 42, "ivan", "Иван Петров")
    db.update_onboarding(oid, data={
        "developer": "Ромашка", "subdomain": "romashka",
        "bot_check": "@romashka_bot (Ромашка)",
        "amo_check": "Ромашка: воронок 7, контактов 6539"},
        slug="romashka", status="ready")

    out = onb.summary_for_operator(db.get_onboarding(oid))
    for part in ("Ромашка", "romashka", "@romashka_bot", "Иван Петров",
                 "6539"):
        assert part in out


def test_summary_hides_secrets(tmp_path):
    """В сводке оператору не должно быть самих токенов — только проверки."""
    db = Db(tmp_path / "s2.db")
    oid = db.start_onboarding("a", 42, "u", "И")
    db.update_onboarding(oid, data={
        "developer": "Р", "bot_token": "SECRET-BOT-TOKEN",
        "amo_token": "SECRET-AMO-TOKEN", "subdomain": "s"}, status="ready")

    out = onb.summary_for_operator(db.get_onboarding(oid))
    assert "SECRET-BOT-TOKEN" not in out
    assert "SECRET-AMO-TOKEN" not in out
