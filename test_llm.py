"""Контракт OpenAI: прежние данные и решения, новый HTTP-протокол.

Все сообщения выдуманы, запросы перехватывает MockTransport.
"""

import json
import subprocess
import sys
from pathlib import Path

import httpx
import pytest

import llm
import provision


MODEL = "gpt-5.4-mini-2026-03-17"
FIXATION = {
    "is_fixation": True, "confidence": 0.65,
    "client_name": "Тестовый Клиент", "phone": "+7 999 000 00 **",
    "agency": "Тестовое агентство", "object": "Тестовый ЖК",
    "comment": "передача клиента", "reason": "имя и телефон клиента",
}


def response(text, *, status="completed"):
    return {"status": status, "output": [
        {"type": "reasoning", "summary": []},
        {"type": "message", "role": "assistant", "status": status,
         "content": [{"type": "output_text", "text": text}]},
    ]}


async def intercept(clf, handler):
    headers = clf._client.headers.copy()
    timeout = clf._client.timeout
    await clf.close()
    clf._client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler), headers=headers, timeout=timeout)


@pytest.mark.asyncio
async def test_openai_request_preserves_rules_context_limits_and_fields():
    captured = {}

    def handler(request):
        captured["request"] = request
        return httpx.Response(200, json=response(json.dumps(FIXATION)))

    clf = llm.Classifier("test-openai-key", min_confidence=0.3)
    await intercept(clf, handler)
    try:
        fx = await clf.classify("зафиксируйте плиз", chat_title="Тестовый чат",
                                author="Тестовый агент",
                                context=["Тестовый Клиент +7 999 000 00 **"])
    finally:
        await clf.close()

    assert vars(fx) == FIXATION
    req = captured["request"]
    assert str(req.url) == "https://api.openai.com/v1/responses"
    assert req.headers["authorization"] == "Bearer test-openai-key"
    assert "x-api-key" not in req.headers and "anthropic-version" not in req.headers
    assert req.extensions["timeout"]["read"] == 30.0
    body = json.loads(req.content)
    assert body["model"] == MODEL
    assert body["max_output_tokens"] == 500
    assert body["reasoning"] == {"effort": "none"}
    assert body["store"] is False
    assert body["instructions"] == llm.load_prompt()
    user = body["input"][0]["content"]
    for fragment in ("Тестовый чат", "Тестовый агент", "<context>",
                     "+7 999 000 00 **", "<message>\nзафиксируйте плиз"):
        assert fragment in user
    fmt = body["text"]["format"]
    assert fmt["type"] == "json_schema" and fmt["strict"] is True
    schema = fmt["schema"]
    assert set(schema["required"]) == set(FIXATION)
    assert set(schema["properties"]) == set(FIXATION)
    assert schema["additionalProperties"] is False


@pytest.mark.asyncio
async def test_prefilter_still_skips_api():
    def unexpected(request):
        pytest.fail("предфильтр не должен отправлять запрос")

    clf = llm.Classifier("test-key")
    await intercept(clf, unexpected)
    try:
        assert not (await clf.classify("спасибо за встречу")).is_fixation
    finally:
        await clf.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("confidence, accepted", [(0.29, False), (0.3, True), (0.9, True)])
async def test_configured_confidence_threshold_is_unchanged(confidence, accepted):
    payload = {**FIXATION, "confidence": confidence}
    clf = llm.Classifier("test-key", min_confidence=0.3)
    await intercept(clf, lambda req: httpx.Response(200, json=response(json.dumps(payload))))
    try:
        fx = await clf.classify("Тестовый Клиент +7 999 000 00 **")
        assert fx.is_fixation is accepted
        assert fx.phone == FIXATION["phone"]
    finally:
        await clf.close()


@pytest.mark.asyncio
async def test_negative_result_with_null_fields_is_preserved():
    payload = {name: None for name in FIXATION}
    payload["is_fixation"] = False
    clf = llm.Classifier("test-key", min_confidence=0.3)
    await intercept(clf, lambda req: httpx.Response(200, json=response(json.dumps(payload))))
    try:
        fx = await clf.classify("Объявление для клиентов, звоните в отдел продаж")
        assert not fx.is_fixation and fx.confidence == 0.0
        assert all(getattr(fx, name) is None for name in
                   ("client_name", "phone", "agency", "object", "comment", "reason"))
    finally:
        await clf.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("payload", [
    response(json.dumps(FIXATION), status="incomplete"),
    {"status": "completed", "output": [{"type": "message", "content": [
        {"type": "refusal", "refusal": "test refusal"}]}]},
    {"status": "failed", "output": []},
    {"status": "completed", "output": []},
    response("not JSON"), response("[]"), response("null"),
    response(json.dumps({**FIXATION, "is_fixation": "false"})),
    response(json.dumps({**FIXATION, "confidence": "bad"})),
    response(json.dumps({**FIXATION, "phone": ["unexpected"]})),
])
async def test_unusable_response_cannot_create_a_fixation(payload, caplog):
    clf = llm.Classifier("test-key")
    await intercept(clf, lambda req: httpx.Response(200, json=payload))
    try:
        assert not (await clf.classify("Тестовый Клиент +7 999 000 00 **")).is_fixation
        assert FIXATION["phone"] not in caplog.text
    finally:
        await clf.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("code", [401, 429, 500])
async def test_http_errors_keep_existing_fallbacks(code):
    clf = llm.Classifier("test-key")
    await intercept(clf, lambda req: httpx.Response(code, json={"error": "test"}))
    try:
        fx = await clf.classify("зафиксируйте клиента")
        assert not fx.is_fixation and "ошибка API" in fx.reason
        assert await clf.translate("Тестовое объявление") is None
    finally:
        await clf.close()


@pytest.mark.asyncio
async def test_timeout_keeps_existing_fallbacks():
    def timeout(request):
        raise httpx.ReadTimeout("test timeout", request=request)

    clf = llm.Classifier("test-key")
    await intercept(clf, timeout)
    try:
        assert not (await clf.classify("зафиксируйте клиента")).is_fixation
        assert await clf.translate("Тестовое объявление") is None
    finally:
        await clf.close()


@pytest.mark.asyncio
async def test_translation_preserves_prompt_formatting_and_limits():
    html = '<b>Тестовое объявление</b> <a href="https://example.com">ссылка</a>'
    translated = '<b>Test announcement</b> <a href="https://example.com">link</a>'

    def handler(request):
        body = json.loads(request.content)
        assert str(request.url) == "https://api.openai.com/v1/responses"
        assert body["instructions"] == llm.TRANSLATE_PROMPT.format(target="английский")
        assert body["input"] == [{"role": "user", "content": html}]
        assert body["max_output_tokens"] == 4000
        assert body["store"] is False
        assert request.extensions["timeout"]["read"] == 45.0
        assert "text" not in body  # перевод — HTML, а не JSON
        return httpx.Response(200, json=response(translated))

    clf = llm.Classifier("test-key")
    await intercept(clf, handler)
    try:
        assert await clf.translate(html) == translated
    finally:
        await clf.close()


@pytest.mark.parametrize("settings, expected", [
    ({}, MODEL),
    ({"MODEL": "claude-haiku-4-5-20251001"}, MODEL),
    ({"MODEL": "gpt-5.4-mini"}, "gpt-5.4-mini"),
    ({"MODEL": "claude-old", "OPENAI_MODEL": MODEL}, MODEL),
])
def test_config_uses_openai_key_and_handles_existing_model_setting(settings, expected):
    env = {"ENV_FILE": "/dev/null", "TELEGRAM_TOKEN": "1:test",
           "AMO_SUBDOMAIN": "test", "OPENAI_API_KEY": "test-openai-key", **settings}
    proc = subprocess.run(
        [sys.executable, "-c", "from config import cfg; print(cfg.openai_key, cfg.model, cfg.min_confidence)"],
        cwd=Path(__file__).parent, env=env, capture_output=True, text=True, timeout=30)
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == f"test-openai-key {expected} 0.3"


def test_new_clients_inherit_openai_settings_only():
    settings = {"OPENAI_API_KEY": "test-openai-key", "OPENAI_MODEL": MODEL,
                "ANTHROPIC_API_KEY": "unused", "MODEL": "claude-old",
                "MIN_CONFIDENCE": "0.3", "TELEGRAM_TOKEN": "private"}
    inherited = provision.collect_inherited(settings)
    assert inherited["OPENAI_API_KEY"] == "test-openai-key"
    assert inherited["OPENAI_MODEL"] == MODEL
    assert inherited["MIN_CONFIDENCE"] == "0.3"
    assert "ANTHROPIC_API_KEY" not in inherited and "TELEGRAM_TOKEN" not in inherited


@pytest.mark.parametrize("key", [None, "", "   "])
def test_missing_openai_key_is_explained_without_using_anthropic_key(key):
    env = {"ENV_FILE": "/dev/null", "TELEGRAM_TOKEN": "1:test",
           "AMO_SUBDOMAIN": "test", "ANTHROPIC_API_KEY": "unused-secret"}
    if key is not None:
        env["OPENAI_API_KEY"] = key
    proc = subprocess.run([sys.executable, "-c", "import config"],
                          cwd=Path(__file__).parent, env=env,
                          capture_output=True, text=True, timeout=30)
    assert proc.returncode != 0
    assert "Укажите OPENAI_API_KEY" in proc.stderr
    assert "unused-secret" not in proc.stderr
