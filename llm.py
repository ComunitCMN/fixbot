"""
Распознавание фиксации через Claude API.

Правила живут в prompts/fixation.md — обычным текстом. Файл читается на
каждое сообщение (с кэшем по mtime), так что правила можно править на лету,
не перезапуская бота.

Дешёвая предфильтрация: если в сообщении нет ни телефона, ни ключевых слов,
запрос в модель не отправляется вообще.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from pathlib import Path

import httpx

import phones

log = logging.getLogger(__name__)

API_URL = "https://api.anthropic.com/v1/messages"
PROMPT_PATH = Path(__file__).parent / "prompts" / "fixation.md"

# Если ничего из этого нет и телефона нет — точно не фиксация, экономим вызов.
# Слова на обоих языках: агентства бывают и русскоязычные, и англоязычные.
HINTS = re.compile(
    r"фикс|закреп|регистрир|заявк|клиент|фио|тел\.?:|телефон|бронь|брониру"
    r"|regist|assign|reserv|book|client|customer|lead|phone|contact"
    r"|name\s*:|tel\.?:|fix\b|attach",
    re.IGNORECASE,
)

_cache: dict[str, tuple[float, str]] = {}


@dataclass
class Fixation:
    is_fixation: bool
    confidence: float = 0.0
    client_name: str | None = None
    phone: str | None = None
    agency: str | None = None
    object: str | None = None
    comment: str | None = None
    reason: str | None = None

    @classmethod
    def no(cls, reason: str) -> "Fixation":
        return cls(is_fixation=False, confidence=1.0, reason=reason)


def load_prompt() -> str:
    mtime = PROMPT_PATH.stat().st_mtime
    cached = _cache.get("prompt")
    if cached and cached[0] == mtime:
        return cached[1]
    text = PROMPT_PATH.read_text(encoding="utf-8")
    _cache["prompt"] = (mtime, text)
    return text


def prefilter(text: str) -> bool:
    """True — есть смысл спрашивать модель."""
    if not text or len(text) < 8:
        return False
    return bool(HINTS.search(text)) or bool(phones.extract_all(text))


class Classifier:
    def __init__(self, api_key: str, model: str = "claude-haiku-4-5-20251001",
                 min_confidence: float = 0.6):
        self.model = model
        self.min_confidence = min_confidence
        self._client = httpx.AsyncClient(
            headers={
                "x-api-key": api_key,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            timeout=30.0,
        )

    async def close(self) -> None:
        await self._client.aclose()

    async def translate(self, html: str, target: str = "английский",
                        model: str | None = None) -> str | None:
        """
        Переводит текст рассылки, не ломая разметку Telegram.

        Возвращает None, если не получилось: тогда рассылка уйдёт
        в оригинале, а не потеряется.
        """
        if not (html or "").strip():
            return ""
        body = {
            "model": model or self.model,
            "max_tokens": 4000,
            "system": TRANSLATE_PROMPT.format(target=target),
            "messages": [{"role": "user", "content": html}],
        }
        try:
            # Отдельный таймаут: рассылку нельзя подвешивать надолго,
            # без перевода она всё равно уйдёт — в оригинале.
            r = await self._client.post(API_URL, json=body, timeout=45.0)
            r.raise_for_status()
            return r.json()["content"][0]["text"].strip()
        except Exception as e:  # noqa: BLE001
            log.warning("перевод не удался: %s", e)
            return None

    async def classify(self, text: str, chat_title: str = "",
                       author: str = "", context: list[str] | None = None
                       ) -> Fixation:
        """
        context — предыдущие сообщения того же автора за последние минуты.

        Нужен потому, что фиксацию часто разрывают на два сообщения:
        сначала номер, потом «зафиксируйте». По отдельности ни одно из них
        фиксацией не выглядит.
        """
        # Признак мог быть в предыдущем сообщении, поэтому предфильтр
        # смотрит на всё вместе.
        joined = "\n".join([*(context or []), text])
        if not prefilter(joined):
            return Fixation.no("предфильтр: нет признаков фиксации")

        ctx_block = ""
        if context:
            lines = "\n".join(f"- {c}" for c in context)
            ctx_block = (
                "\nПредыдущие сообщения этого же автора (для контекста, "
                "решение принимай по последнему):\n"
                f"<context>\n{lines}\n</context>\n"
            )

        user_block = (
            f"Чат: {chat_title or 'неизвестен'}\n"
            f"Автор: {author or 'неизвестен'}\n"
            f"{ctx_block}"
            f"Сообщение:\n<message>\n{text}\n</message>"
        )

        body = {
            "model": self.model,
            "max_tokens": 500,
            "system": [{
                "type": "text",
                "text": load_prompt(),
                # Промпт длинный и одинаковый — кэшируем, чтобы платить меньше.
                "cache_control": {"type": "ephemeral"},
            }],
            "messages": [{"role": "user", "content": user_block}],
        }

        try:
            r = await self._client.post(API_URL, json=body)
            r.raise_for_status()
            raw = r.json()["content"][0]["text"]
        except Exception as e:  # noqa: BLE001
            log.warning("Claude API недоступен: %s", e)
            return Fixation.no(f"ошибка API: {e}")

        data = _parse_json(raw)
        if data is None:
            log.warning("Не удалось разобрать ответ модели: %r", raw[:200])
            return Fixation.no("нераспознанный ответ модели")

        f = Fixation(
            is_fixation=bool(data.get("is_fixation")),
            confidence=float(data.get("confidence") or 0.0),
            client_name=data.get("client_name"),
            phone=data.get("phone"),
            agency=data.get("agency"),
            object=data.get("object"),
            comment=data.get("comment"),
            reason=data.get("reason"),
        )
        if f.is_fixation and f.confidence < self.min_confidence:
            f.is_fixation = False
            f.reason = f"низкая уверенность {f.confidence:.2f}: {f.reason}"
        return f


TRANSLATE_PROMPT = """Ты переводишь рассылки для агентов по недвижимости.

Тебе дают текст с HTML-разметкой Telegram. Переведи его на {target},
соблюдая правила:

- **Разметку сохраняй как есть.** Теги <b>, <i>, <u>, <s>, <code>, <pre>,
  <a href="…">, <tg-spoiler>, <blockquote> должны остаться на тех же
  смысловых местах. Не добавляй новых тегов и не убирай существующие.
- Эмодзи, переносы строк и структуру абзацев сохраняй один в один.
- Названия компаний, жилых комплексов, имена людей не переводи.
- Ссылки внутри href не трогай.
- Числа, даты и телефоны оставляй как есть.
- Тон деловой, без канцелярита.

## Материалы для русскоязычной аудитории убирай

Письмо получат агенты, не знающие русского. Всё, что предназначено
русскоязычным, им бесполезно и мешает. Поэтому:

- строки и ссылки, помеченные как русские — «Ру», «RU», «рус.»,
  «на русском», «(рус)» — **удаляй целиком**, вместе с подписью;
- у оставшегося англоязычного варианта пометку убирай: «Offer EN: link»
  превращается в «Offer: link», потому что выбирать больше не из чего;
- если помеченных вариантов нет, ничего не удаляй.

**Пример.** Было:

    Оффер на юнит Ру: <a href="http://ru">ссылка</a>
    Оффер на юнит EN: <a href="http://en">ссылка</a>

Стало:

    Unit offer: <a href="http://en">link</a>

В ответ верни ТОЛЬКО переведённый текст, без пояснений и без обёртки
в markdown."""


def _parse_json(raw: str) -> dict | None:
    raw = raw.strip()
    raw = re.sub(r"^```(?:json)?|```$", "", raw, flags=re.MULTILINE).strip()
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", raw, re.DOTALL)
        if not m:
            return None
        try:
            return json.loads(m.group(0))
        except json.JSONDecodeError:
            return None
