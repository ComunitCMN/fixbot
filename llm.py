"""
Распознавание фиксации и перевод рассылок через OpenAI API.

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

API_URL = "https://api.openai.com/v1/responses"
DEFAULT_MODEL = "gpt-5.4-mini-2026-03-17"
PROMPT_PATH = Path(__file__).parent / "prompts" / "fixation.md"

# Те же поля, что в Fixation и prompts/fixation.md. Nullable-поля
# сохраняют прежний контракт: отсутствующие данные не додумываются.
FIXATION_SCHEMA = {
    "type": "object",
    "properties": {
        "is_fixation": {"type": "boolean"},
        "confidence": {"type": ["number", "null"], "minimum": 0, "maximum": 1},
        **{name: {"type": ["string", "null"]} for name in (
            "client_name", "phone", "agency", "object", "comment", "reason")},
    },
    "required": ["is_fixation", "confidence", "client_name", "phone",
                 "agency", "object", "comment", "reason"],
    "additionalProperties": False,
}

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
    def __init__(self, api_key: str, model: str = DEFAULT_MODEL,
                 min_confidence: float = 0.6):
        self.model = model
        self.min_confidence = min_confidence
        self._client = httpx.AsyncClient(
            headers={
                "Authorization": f"Bearer {api_key}",
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
            "max_output_tokens": 4000,
            "reasoning": {"effort": "none"},
            "store": False,
            "instructions": TRANSLATE_PROMPT.format(target=target),
            "input": [{"role": "user", "content": html}],
        }
        try:
            # Отдельный таймаут: рассылку нельзя подвешивать надолго,
            # без перевода она всё равно уйдёт — в оригинале.
            r = await self._client.post(API_URL, json=body, timeout=45.0)
            r.raise_for_status()
            translated = _output_text(r.json())
        except Exception as e:  # noqa: BLE001
            log.warning("перевод не удался: %s", e)
            return None
        # В том числе incomplete/max_output_tokens: оборванный перевод
        # не отправляем, рассылка уйдёт в оригинале целиком.
        if translated is None:
            log.warning("перевод не удался: нет полного текстового ответа")
        return translated

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
            "max_output_tokens": 500,
            # Короткая классификация без расхода лимита на reasoning.
            "reasoning": {"effort": "none"},
            "store": False,
            # Правила и их кэш по mtime остаются прежними. Кэширование
            # общего префикса на стороне OpenAI происходит автоматически.
            "instructions": load_prompt(),
            "input": [{"role": "user", "content": user_block}],
            "text": {"format": {
                "type": "json_schema", "name": "fixation",
                "strict": True, "schema": FIXATION_SCHEMA,
            }},
        }

        try:
            r = await self._client.post(API_URL, json=body)
            r.raise_for_status()
            raw = _output_text(r.json())
        except Exception as e:  # noqa: BLE001
            log.warning("OpenAI API недоступен: %s", e)
            return Fixation.no(f"ошибка API: {e}")

        data = _parse_json(raw) if raw is not None else None
        if not _valid_fixation(data):
            # Ответ может содержать телефон и имя клиента — в лог его
            # не пишем даже при ошибке разбора.
            log.warning("Не удалось разобрать полный ответ модели")
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


def _output_text(data: dict) -> str | None:
    """Только полный текст Responses API; refusal и обрыв не используем.

    output[0] может быть reasoning, поэтому ищем текст по типу блока.
    """
    if not isinstance(data, dict) or data.get("status") != "completed":
        return None
    chunks = []
    for item in data.get("output", []):
        if item.get("type") != "message":
            continue
        if item.get("status", "completed") != "completed":
            return None
        for part in item.get("content", []):
            if part.get("type") == "refusal":
                return None
            if part.get("type") == "output_text":
                text = part.get("text")
                if not isinstance(text, str):
                    return None
                chunks.append(text)
    return "".join(chunks).strip() or None


def _valid_fixation(data: dict | None) -> bool:
    if not isinstance(data, dict) or not isinstance(data.get("is_fixation"), bool):
        return False
    confidence = data.get("confidence")
    if confidence is not None and (
        type(confidence) not in (int, float) or not 0 <= confidence <= 1
    ):
        return False
    return all(data.get(name) is None or isinstance(data[name], str)
               for name in ("client_name", "phone", "agency", "object", "comment", "reason"))


def _parse_json(raw: str) -> dict | None:
    raw = raw.strip()
    raw = re.sub(r"^```(?:json)?|```$", "", raw, flags=re.MULTILINE).strip()
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", raw, re.DOTALL)
        if not m:
            return None
        try:
            data = json.loads(m.group(0))
        except json.JSONDecodeError:
            return None
    return data if isinstance(data, dict) else None
