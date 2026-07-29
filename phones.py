"""
Телефоны любой страны, в том числе НЕПОЛНЫЕ (маскированные).

Агентства часто пишут номер без последних цифр:
    +7 999 123-45-**        Россия, известно 9 из 11
    +62 812 3456-78**       Индонезия
    +971 50 123-45-**       ОАЭ
    8 999 123 45...         Россия, местная запись

Разбором занимается libphonenumber (библиотека Google): она знает форматы
и допустимые длины номеров всех стран. Своих таблиц кодов мы не держим.

Ключевое правило проекта: **скрытыми можно оставить не больше двух
последних цифр**. Сколько это в абсолютных цифрах — зависит от страны:
для России полный номер 11 цифр, значит достаточно 9; для ОАЭ полный
12, значит нужно 10.

Отдельная сложность — страны с плавающей длиной номера. В Индонезии
мобильный бывает и 11, и 12 цифр. Если пришло 11, понять, полный это
номер или обрезанный, нельзя. Мы считаем такой номер полным: сравнение
всё равно идёт по общему префиксу, поэтому 11-значный найдёт 12-значного.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import lru_cache

import phonenumbers as pn

#: Сколько последних цифр разрешено не знать.
MAX_MISSING = 2

#: Символы, которыми обычно маскируют хвост номера.
MASK_CHARS = "*xхXХ_?#"

#: Регион по умолчанию для номеров, записанных без кода страны.
#: Переопределяется в config и может быть задан отдельно для каждого чата.
DEFAULT_REGION = "RU"

#: На сколько цифр вперёд ищем допустимую длину номера.
_MAX_LOOKAHEAD = 6

_IS_POSSIBLE = 0


def set_default_region(region: str | None) -> None:
    """Меняет регион по умолчанию (вызывается при старте из config)."""
    global DEFAULT_REGION
    if region:
        DEFAULT_REGION = region.upper()


#: Типы номеров, по которым реально звонят клиентам. Служебные,
#: премиум и прочую экзотику намеренно не берём: под их длины
#: подходит любой случайный набор цифр.
_CLIENT_TYPES = (
    pn.PhoneNumberType.MOBILE,
    pn.PhoneNumberType.FIXED_LINE,
    pn.PhoneNumberType.FIXED_LINE_OR_MOBILE,
)


@lru_cache(maxsize=4096)
def _shortest_valid_length(digits: str, strictly_longer: bool = False) -> int | None:
    """
    Ожидаемая полная длина номера, начинающегося с этих цифр.

    Работает перебором: дополняем номер нулями и спрашиваем у
    libphonenumber, бывает ли такая длина у обычного телефона в этой
    стране. Проверка зависит только от длины, поэтому чем дополнять —
    неважно.

    Важно спрашивать именно про мобильные и городские номера, а не
    «бывает ли такая длина вообще». У России, например, есть служебные
    номера до 14 цифр, и под них подходит любой случайный набор цифр:
    строка 667653892134 превращалась в «+7 667653892134**».

    strictly_longer=True нужен, когда мы точно знаем, что номер обрезан
    (в записи были звёздочки). Без этого в странах с плавающей длиной —
    Индонезия, ОАЭ — маскированный номер приняли бы за полный: скажем,
    +62 812 3456 78** имеет 11 цифр, а 11 цифр для Индонезии допустимая
    длина, хотя настоящий номер тут 13.

    Возвращает None, если номер не похож ни на что осмысленное.
    """
    start = 1 if strictly_longer else 0
    for extra in range(start, _MAX_LOOKAHEAD + 1):
        candidate = digits + "0" * extra
        try:
            parsed = pn.parse("+" + candidate, None)
        except pn.NumberParseException:
            return None
        if any(pn.is_possible_number_for_type_with_reason(parsed, t)
               == _IS_POSSIBLE for t in _CLIENT_TYPES):
            return len(candidate)
    return None


@dataclass(frozen=True)
class Phone:
    """Нормализованный (возможно неполный) номер в формате E.164 без плюса."""

    digits: str
    expected: int = 0
    region: str | None = None

    @property
    def known(self) -> int:
        return len(self.digits)

    @property
    def missing(self) -> int:
        return max(0, self.expected - self.known)

    @property
    def is_full(self) -> bool:
        return self.missing == 0

    @property
    def is_usable(self) -> bool:
        """Достаточно ли цифр, чтобы искать совпадения."""
        return self.missing <= MAX_MISSING

    @property
    def min_compare(self) -> int:
        """Минимум цифр, по которым мы готовы утверждать совпадение."""
        return max(self.expected - MAX_MISSING, 1)

    @property
    def could_be_longer(self) -> bool:
        """
        В этой стране мобильные бывают длиннее — значит, номер мог потерять хвост.

        У России мобильный ровно одной длины, и недобор цифр виден сразу.
        А в Индонезии их четыре (9, 10, 11 и 12 цифр без кода страны):
        короткий вариант сам по себе выглядит полным, хотя мог быть
        обрезан. Отличить невозможно, но предупредить стоит.
        """
        if not self.is_full or not self.region:
            return False
        lengths = _mobile_lengths(self.region)
        if not lengths:
            return False
        national = self.known - _cc_digits(self.region)
        return any(L > national for L in lengths)

    def pretty(self) -> str:
        """Читаемый вид. У неполного номера хвост показан звёздочками."""
        try:
            padded = pn.parse("+" + self.digits + "0" * self.missing, None)
            shown = pn.format_number(padded, pn.PhoneNumberFormat.INTERNATIONAL)
        except pn.NumberParseException:
            return "+" + self.digits + "*" * self.missing
        if self.is_full:
            return shown
        # Затираем ровно столько последних цифр, сколько неизвестно.
        out, left = [], self.missing
        for ch in reversed(shown):
            if ch.isdigit() and left:
                out.append("*")
                left -= 1
            else:
                out.append(ch)
        return "".join(reversed(out))

    def __str__(self) -> str:  # pragma: no cover - косметика
        return self.pretty()


def _strip_mask(raw: str) -> tuple[str, bool]:
    """
    Оставляет только ту часть записи, которой можно верить.

    Второе значение — было ли в записи явное маскирование. Это важно:
    если звёздочки были, номер точно неполный, даже когда его длина
    сама по себе допустима для страны.
    """
    s = str(raw).strip()

    masked = False
    # Многоточие тоже маскирует хвост: «8 999 123 45...».
    if ".." in s:
        s = s[: s.index("..")]
        masked = True

    s = re.sub(rf"[^\d+{re.escape(MASK_CHARS)}]", "", s)
    if not s:
        return "", masked

    # Всё начиная с первого маскировочного символа отбрасываем.
    cut = min((s.find(c) for c in MASK_CHARS if c in s), default=len(s))
    if cut < len(s):
        masked = True
    return s[:cut], masked


def normalize(raw: str, region: str | None = None) -> Phone | None:
    """
    Приводит произвольную запись номера к Phone.

    region — двухбуквенный код страны (RU, ID, AE…), подсказка для номеров,
    записанных без международного кода. Если номер начинается с «+», код
    страны уже указан явно и подсказка не нужна.
    """
    if not raw:
        return None

    kept, masked = _strip_mask(raw)
    if not kept:
        return None

    has_plus = kept.startswith("+")
    digits = re.sub(r"\D", "", kept)
    if len(digits) < 5:
        return None

    hint = (region or DEFAULT_REGION or "").upper() or None

    # Без плюса запись двусмысленна: «7 999 123 45 67» — это российский
    # номер целиком, а «8 999 123 45 67» — он же в местной записи.
    # Собираем оба прочтения и выбираем лучшее.
    if has_plus:
        candidates = [digits]
    else:
        candidates = []
        local = _to_e164_local(digits, hint)
        if local and local != digits:
            candidates.append(local)
        candidates.append(digits)

    best: Phone | None = None
    best_score = -1
    for cand in candidates:
        expected = _shortest_valid_length(cand, masked)
        if expected is None:
            continue
        region_code = _region_of(cand, expected)
        # Настоящий существующий номер важнее просто подходящей длины,
        # а совпадение со страной чата — важнее порядка перебора.
        score = 2 * int(_is_valid(cand)) + int(bool(hint) and region_code == hint)
        if score > best_score:
            best_score = score
            best = Phone(digits=cand, expected=expected, region=region_code)

    return best


@lru_cache(maxsize=4096)
def _is_valid(digits: str) -> bool:
    """Существует ли такой номер на самом деле, а не просто подходит по длине."""
    try:
        return pn.is_valid_number(pn.parse("+" + digits, None))
    except pn.NumberParseException:
        return False


def from_digits(digits: str) -> Phone:
    """
    Восстанавливает Phone из сохранённых цифр.

    Зеркало и заявки хранят только строку цифр, без информации об
    ожидаемой длине, — здесь она вычисляется заново.
    """
    expected = _shortest_valid_length(digits) or len(digits)
    return Phone(digits=digits, expected=expected,
                 region=_region_of(digits, expected))


def _region_of(digits: str, expected: int) -> str | None:
    try:
        parsed = pn.parse("+" + digits + "0" * (expected - len(digits)), None)
        return pn.region_code_for_number(parsed)
    except pn.NumberParseException:
        return None


def _to_e164_local(digits: str, region: str | None) -> str | None:
    """Пробует прочесть цифры как местную запись указанной страны."""
    if not region:
        return None
    try:
        cc = pn.country_code_for_region(region)
    except Exception:  # noqa: BLE001
        return None
    if not cc:
        return None

    national = digits
    # Многие страны пишут междугородний префикс в начале: 8 в России,
    # 0 в Индонезии и Турции.
    trunk = _trunk_prefix(region)
    if trunk and national.startswith(trunk) and len(national) > len(trunk):
        national = national[len(trunk):]

    return f"{cc}{national}"


@lru_cache(maxsize=256)
def _mobile_lengths(region: str) -> tuple[int, ...]:
    """Допустимые длины мобильного номера страны, без кода страны."""
    try:
        meta = pn.PhoneMetadata.metadata_for_region(region.upper())
    except Exception:  # noqa: BLE001
        return ()
    for desc in (getattr(meta, "mobile", None),
                 getattr(meta, "general_desc", None)):
        lengths = getattr(desc, "possible_length", None) if desc else None
        if lengths:
            return tuple(lengths)
    return ()


@lru_cache(maxsize=256)
def _cc_digits(region: str) -> int:
    """Сколько цифр в коде страны: 1 у России, 2 у Индонезии, 3 у ОАЭ."""
    try:
        return len(str(pn.country_code_for_region(region.upper())))
    except Exception:  # noqa: BLE001
        return 0


@lru_cache(maxsize=256)
def _trunk_prefix(region: str) -> str:
    try:
        meta = pn.PhoneMetadata.metadata_for_region(region.upper())
    except Exception:  # noqa: BLE001
        return ""
    return getattr(meta, "national_prefix", "") or ""


def compare_prefix(a: Phone, b: Phone) -> bool:
    """
    True, если номера могут принадлежать одному человеку.

    Сравнение идёт по общему известному префиксу, но не короче, чем
    позволяет более строгий из двух номеров. Так маскированный номер
    находит полный и наоборот.
    """
    n = min(a.known, b.known)
    if n < max(a.min_compare, b.min_compare):
        return False
    return a.digits[:n] == b.digits[:n]


def search_prefix(p: Phone) -> str:
    """Префикс для поиска кандидатов в локальном зеркале."""
    return p.digits[:p.min_compare]


def ambiguity(p: Phone) -> int:
    """Сколько реальных номеров скрывается за маской."""
    return 10 ** p.missing


def extract_all(text: str, region: str | None = None) -> list[Phone]:
    """Вытаскивает все похожие на телефон куски из свободного текста."""
    pattern = (rf"(?:\+\d{{1,3}}[\s\-()]*)?\d[\d\s\-()]{{5,}}"
               rf"[{re.escape(MASK_CHARS)}\s\-.]*")
    found: list[Phone] = []
    seen: set[str] = set()
    for m in re.finditer(pattern, text):
        p = normalize(m.group(0), region)
        if p and p.digits not in seen:
            seen.add(p.digits)
            found.append(p)
    return found
