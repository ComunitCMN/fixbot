"""
Справочник агентств: приведение названий к единому виду и поиск похожих.

Задача — не допустить, чтобы `Дом+`, `ООО "Дом Плюс"`, `АН ДОМ +` и
`дом плюс` превратились в четыре разные компании в amoCRM. Через месяц
такой базы аналитика по агентствам становится бесполезной.

Работает в два шага:
  1. norm_agency() — грубое приведение: срезаются правовые формы, кавычки,
     регистр, «плюс» → «+», лишние пробелы.
  2. best_match() — нечёткий поиск среди уже известных названий, чтобы
     поймать опечатки, которые нормализация не ловит.

Ничего не создаётся молча: если точного совпадения нет, бот показывает
кандидатов и просит подтвердить.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from difflib import SequenceMatcher

# Правовые формы и приставки, которые ничего не значат для опознания.
NOISE = [
    "общество с ограниченной ответственностью",
    "индивидуальный предприниматель",
    "агентство недвижимости",
    "агенство недвижимости",      # частая опечатка
    "ооо", "оао", "зао", "пао", "ип", "ан", "ак", "тд",
    "агентство", "агенство", "недвижимость", "недвижимости",
    "компания", "группа", "холдинг", "центр",
]

# Похожие по написанию латинские и кириллические буквы. Названия часто
# набирают вперемешку, и «Дом» латинской D визуально не отличить.
LOOKALIKE = str.maketrans({
    "a": "а", "c": "с", "e": "е", "o": "о", "p": "р", "x": "х", "y": "у",
    "b": "в", "h": "н", "k": "к", "m": "м", "t": "т",
})

SYNONYMS = [
    (r"\bплюс\b", "+"),
    (r"\bplus\b", "+"),
    (r"\bи\s*ко\b", "&"),
    (r"\band\b", "&"),
]

# Порог, выше которого считаем названия одним агентством без вопросов.
STRONG = 0.92
# Порог, ниже которого кандидата даже не показываем.
WEAK = 0.72


def norm_agency(name: str) -> str:
    """Приводит название агентства к каноническому виду для сравнения."""
    if not name:
        return ""

    s = unicodedata.normalize("NFKC", str(name)).lower().strip()

    # ё → е: «Новосёл» и «Новосел» — одно и то же агентство,
    # а пишут их вперемешку.
    s = s.replace("ё", "е")

    # Кавычки всех сортов и типографские тире.
    s = re.sub(r"[«»\"'`’‘“”]", " ", s)
    s = re.sub(r"[–—−]", "-", s)

    # Латиница, похожая на кириллицу.
    s = s.translate(LOOKALIKE)

    for pattern, repl in SYNONYMS:
        s = re.sub(pattern, repl, s)

    # Правовые формы — только как отдельные слова.
    for word in sorted(NOISE, key=len, reverse=True):
        s = re.sub(rf"(?<![а-яa-z0-9]){re.escape(word)}(?![а-яa-z0-9])", " ", s)

    # Оставляем буквы, цифры и значимые знаки.
    s = re.sub(r"[^а-яa-z0-9+&-]+", " ", s)
    s = re.sub(r"\s*([+&-])\s*", r"\1", s)
    s = re.sub(r"\s+", " ", s).strip()

    return s


def similarity(a: str, b: str) -> float:
    """0..1, насколько похожи два уже нормализованных названия."""
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    return SequenceMatcher(None, a, b).ratio()


@dataclass
class Candidate:
    name: str
    norm: str
    score: float
    agency_id: int | None = None
    amo_company_id: int | None = None


@dataclass
class Resolution:
    """Результат опознания агентства."""

    status: str                 # exact | confirm | new
    query: str                  # что пришло в сообщении
    norm: str
    candidates: list[Candidate]

    @property
    def best(self) -> Candidate | None:
        return self.candidates[0] if self.candidates else None


def resolve(raw_name: str, known: list[dict], limit: int = 3) -> Resolution:
    """
    Сопоставляет название из сообщения со справочником.

    known: [{name, norm, agency_id?, amo_company_id?}] — агентства и компании
    из amoCRM.

    status:
      exact   — нашли уверенно, можно использовать молча
      confirm — есть похожие, нужно подтверждение кнопками
      new     — ничего похожего, предложить создать
    """
    norm = norm_agency(raw_name)
    if not norm:
        return Resolution("new", raw_name, norm, [])

    scored: list[Candidate] = []
    for k in known:
        k_norm = k.get("norm") or norm_agency(k.get("name") or "")
        score = similarity(norm, k_norm)
        if score >= WEAK:
            scored.append(Candidate(
                name=k.get("name") or k_norm, norm=k_norm, score=score,
                agency_id=k.get("agency_id"),
                amo_company_id=k.get("amo_company_id"),
            ))

    scored.sort(key=lambda c: -c.score)
    scored = scored[:limit]

    if scored and scored[0].score >= STRONG:
        return Resolution("exact", raw_name, norm, scored)
    if scored:
        return Resolution("confirm", raw_name, norm, scored)
    return Resolution("new", raw_name, norm, [])


#: Слова, которыми обычно называют совместные чаты. К имени агентства
#: отношения не имеют, поэтому при разборе названия чата отбрасываются.
CHAT_NOISE = [
    "чат", "группа", "рабочий", "рабочая", "общий", "общая",
    "фиксации", "фиксация", "заявки", "продажи", "сделки", "клиенты",
    "название", "имя", "агентства", "агентство",
    "chat", "group", "work", "working", "sales", "deals", "clients",
    "leads", "team", "agency", "agencies", "partners", "partner",
    "name", "title",
]

#: Короче этого остаток названия ни о чём не говорит.
MIN_CANDIDATE_LEN = 3

#: Разделители «мы и они»: TEUS & Squaresell, Дом+ х Ромашка.
CHAT_SPLIT = re.compile(r"\s*[&×xх|/•—–-]{1,2}\s+|\s+[&×xх|/•—–-]{1,2}\s*")


def agency_from_chat_title(title: str, developer: str | None = None,
                           known: list[dict] | None = None) -> str | None:
    """
    Достаёт вероятное название агентства из имени группы.

    Совместные чаты почти всегда называют «Агентство × Застройщик»:
    «TEUS & Squaresell», «Дом+ х Ромашка», «BREIG | Партнёры». Отбрасываем
    имя застройщика (оно всегда одно и то же, берётся из настроек) и
    служебные слова — остаётся кандидат.

    Ничего не выдумываем: если после чистки пусто или осталось слишком
    много кусков, возвращаем None и спрашиваем человека.
    """
    if not title:
        return None

    parts = [p.strip() for p in CHAT_SPLIT.split(title) if p and p.strip()]
    if not parts:
        parts = [title.strip()]

    dev_norm = norm_agency(developer or "")

    candidates: list[str] = []
    for part in parts:
        # Застройщика узнаём по исходному куску, до чистки: его название
        # тоже может содержать служебные слова.
        if dev_norm and norm_agency(part) == dev_norm:
            continue

        cleaned = part
        # Служебные слова — только целыми словами, чтобы не съесть
        # «Агентство Мечты» до пустоты по ошибке.
        for word in CHAT_NOISE:
            cleaned = re.sub(rf"(?<!\w){re.escape(word)}(?!\w)", " ",
                             cleaned, flags=re.IGNORECASE)
        cleaned = re.sub(r"\s+", " ", cleaned).strip(" -—–|/&")

        # От «Agency name» после чистки остаётся пустота или огрызок —
        # выдавать такое за название агентства нельзя.
        if len(cleaned) < MIN_CANDIDATE_LEN:
            continue
        if dev_norm and norm_agency(cleaned) == dev_norm:
            continue
        candidates.append(cleaned)

    if not candidates:
        return None

    # Если один из кусков уже есть в справочнике — он и есть агентство.
    if known:
        for cand in candidates:
            res = resolve(cand, known)
            if res.status == "exact" and res.best:
                return res.best.name

    # Иначе берём первый: в названии «Агентство × Застройщик» агентство
    # обычно стоит первым, а застройщика мы уже отбросили.
    return pretty_name(candidates[0])


def pretty_name(raw: str) -> str:
    """Аккуратное отображаемое имя: убираем кавычки, схлопываем пробелы."""
    s = re.sub(r"[«»\"'`]", "", str(raw or "")).strip()
    return re.sub(r"\s+", " ", s)
