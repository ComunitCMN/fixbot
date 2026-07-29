"""Фиксация, разорванная на несколько сообщений."""

import time

import llm


def test_prefilter_alone_vs_with_context():
    """
    Ни номер сам по себе, ни «зафиксируйте плиз» по отдельности не должны
    считаться достаточными. Вместе — должны дойти до модели.
    """
    assert llm.prefilter("зафиксируйте плиз")          # есть слово-признак
    assert llm.prefilter("+7 987 564 34 88")           # есть телефон
    assert not llm.prefilter("ок")
    assert not llm.prefilter("завтра в 12")

    joined = "\n".join(["+7 987 564 34 88", "зафиксируйте плиз"])
    assert llm.prefilter(joined)


def test_context_block_included_in_prompt(monkeypatch):
    """Контекст должен доезжать до модели отдельным блоком."""
    captured = {}

    class FakeResponse:
        status_code = 200

        def raise_for_status(self):
            return None

        def json(self):
            return {"content": [{"text": '{"is_fixation": false}'}]}

    class FakeClient:
        async def post(self, url, json=None):
            captured["body"] = json
            return FakeResponse()

        async def aclose(self):
            return None

    clf = llm.Classifier("key")
    clf._client = FakeClient()

    import asyncio
    asyncio.run(clf.classify("зафиксируйте плиз", chat_title="Чат",
                             author="Иван",
                             context=["+7 987 564 34 88"]))

    text = captured["body"]["messages"][0]["content"]
    assert "<context>" in text
    assert "+7 987 564 34 88" in text
    assert "зафиксируйте плиз" in text
    # решение принимается по последнему сообщению
    assert "<message>" in text


def test_no_context_no_block(monkeypatch):
    captured = {}

    class FakeResponse:
        status_code = 200

        def raise_for_status(self):
            return None

        def json(self):
            return {"content": [{"text": '{"is_fixation": false}'}]}

    class FakeClient:
        async def post(self, url, json=None):
            captured["body"] = json
            return FakeResponse()

        async def aclose(self):
            return None

    clf = llm.Classifier("key")
    clf._client = FakeClient()

    import asyncio
    asyncio.run(clf.classify("фиксирую клиента +79875643488"))

    assert "<context>" not in captured["body"]["messages"][0]["content"]


def test_recent_buffer_keeps_last_and_expires():
    import bot

    bot._recent.clear()
    for i in range(6):
        bot.remember_message(-100, 42, f"сообщение {i}")

    ctx = bot.recent_context(-100, 42)
    assert len(ctx) == bot.CONTEXT_DEPTH
    assert ctx[-1] == "сообщение 5"          # хранится хвост, а не начало

    # состарившиеся сообщения не попадают в контекст
    bot._recent[(-100, 42)] = [
        (time.time() - bot.CONTEXT_WINDOW_SEC - 10, "старое")]
    assert bot.recent_context(-100, 42) == []


def test_recent_buffer_separates_authors():
    """Контекст одного человека не должен утекать к другому."""
    import bot

    bot._recent.clear()
    bot.remember_message(-100, 42, "+7 987 564 34 88")
    bot.remember_message(-100, 99, "привет")

    assert bot.recent_context(-100, 42) == ["+7 987 564 34 88"]
    assert bot.recent_context(-100, 99) == ["привет"]
    assert bot.recent_context(-777, 42) == []      # и между чатами тоже
