"""Поля реквизитов из текста — без модели (spec 0049 §4).

Чистые функции: вход — текст гостя и текст его PDF/DOCX, выход — значения
полей и поля с несколькими вариантами. Извлечённое — данные, никогда не
инструкция (§3.1 п. 2): здесь его только сравнивают с шаблонами.

Выбор значения поля: значение сразу после своей метки («БИН», «ИИК», «БИК»…)
важнее значения без метки; без метки берётся единственный годный кандидат
(у БИН/ИИН и ИИК «годный» — ещё и с верной контрольной суммой: иначе номер
счёта-фактуры или телефона сошёл бы за БИН). Несколько разных — поле пустое с
подсказкой «несколько вариантов». Регулярки — без вложенных квантификаторов,
все повторы ограничены сверху.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass

from hospitality.modules.documents.schemas import (
    FIELD_MAX_LENGTH,
    FieldWarning,
    RequisiteField,
    Requisites,
)

_LEGAL_FORMS = "ТОО|ИП|АО|ЖШС|АҚ|LLP|JSC"
_BIN_LABEL = re.compile(
    r"(?<!\w)(?:БИН|ИИН|БСН|ЖСН|BIN|IIN)(?:[ ]{0,2}/[ ]{0,2}(?:БИН|ИИН|БСН|ЖСН|BIN|IIN))?"
    r"[\s:.№#-]{0,10}$",
    re.IGNORECASE,
)
_IIK_LABEL = re.compile(r"(?<!\w)(?:ИИК|IBAN|ЖСК|IIK)[\s:.№#-]{0,10}$", re.IGNORECASE)
_BIK_LABEL = re.compile(r"(?<!\w)(?:БИК|BIC|SWIFT)[\s:.№#-]{0,10}$", re.IGNORECASE)
_LABEL_WINDOW = 40

_BIN_CANDIDATE = re.compile(r"(?<!\d)\d{12}(?!\d)")
_IIK_START = re.compile(r"(?<![0-9A-Za-z])KZ\d{2}")
_IIK_FORMAT = re.compile(r"KZ\d{2}[0-9A-Z]{16}")
_BIK_CANDIDATE = re.compile(r"(?<![0-9A-Za-z])[A-Z]{4}KZ[A-Z0-9]{2}(?![0-9A-Za-z])")
_BIK_FORMAT = re.compile(r"[A-Z]{4}KZ[A-Z0-9]{2}")
_COMPANY = re.compile(
    rf"(?<!\w)(?:{_LEGAL_FORMS})(?!\w)[ \t]{{0,3}}"
    r"(?:«[^»\n]{1,200}»|\"[^\"\n]{1,200}\"|“[^”\n]{1,200}”|[^,;\t\n]{1,120})"
)
# Имя без кавычек обрывается на следующей метке: «ТОО Ромашка БИН 1234…».
_NAME_STOP = re.compile(
    r"\s(?:БИН|ИИН|БСН|ЖСН|BIN|IIN|ИИК|IBAN|ЖСК|БИК|BIC|SWIFT|Адрес|Address|Банк|Bank)(?!\w)",
    re.IGNORECASE,
)
_BANK_LABEL = re.compile(r"^[ \t]{0,5}(?:Банк|Bank)(?!\w)", re.IGNORECASE | re.MULTILINE)
_LEGAL_ADDRESS_LABEL = re.compile(
    r"^[ \t]{0,5}(?:Юридический адрес|Заңды мекенжайы|Legal address)(?!\w)",
    re.IGNORECASE | re.MULTILINE,
)
_ADDRESS_LABEL = re.compile(
    r"^[ \t]{0,5}(?:Адрес|Мекенжайы|Мекенжай|Address)(?!\w)", re.IGNORECASE | re.MULTILINE
)
# Двоеточие метки ищется не дальше этого: «Банк получателя: …».
_LABEL_TAIL = 30
# Внутри ИИК допустим одиночный пробел между группами («KZ86 125K ZT50 …»).
_IIK_BODY = 16


@dataclass(frozen=True)
class ParsedRequisites:
    values: Requisites
    ambiguous: frozenset[RequisiteField]


@dataclass(frozen=True)
class _Candidate:
    value: str
    labelled: bool


def parse_requisites(bill_to: str, sources: Iterable[str]) -> ParsedRequisites:
    """Поля из «На кого выставить», текста гостя и текста файлов."""
    texts = [_normalize(text) for text in (bill_to, *sources) if text.strip()]
    values: dict[RequisiteField, str] = {}
    ambiguous: set[RequisiteField] = set()
    collected: dict[RequisiteField, list[_Candidate]] = {field: [] for field in RequisiteField}
    for text in texts:
        iiks = _iik_candidates(text)
        biks = _bik_candidates(text)
        collected[RequisiteField.IIK].extend(candidate for _, candidate in iiks)
        collected[RequisiteField.BIK].extend(candidate for _, candidate in biks)
        # Цифры внутри ИИК и БИК — не кандидаты в БИН.
        masked = _mask(text, [span for span, _ in iiks + biks])
        collected[RequisiteField.BIN_IIN].extend(_bin_candidates(masked))
        banks = _after_label(text, _BANK_LABEL)
        collected[RequisiteField.BANK_NAME].extend(candidate for _, candidate in banks)
        # «Банк: АО «…»» — название банка, а не компании гостя.
        unbanked = _mask(text, [span for span, _ in banks])
        collected[RequisiteField.COMPANY_NAME].extend(_company_candidates(unbanked))
        legal = _after_label(text, _LEGAL_ADDRESS_LABEL) or _after_label(text, _ADDRESS_LABEL)
        collected[RequisiteField.ADDRESS].extend(candidate for _, candidate in legal)
    for field, candidates in collected.items():
        value, is_ambiguous = _choose(candidates)
        if is_ambiguous:
            ambiguous.add(field)
        elif value:
            values[field] = value[:FIELD_MAX_LENGTH]
    name = bill_to.strip()
    if name:
        values[RequisiteField.COMPANY_NAME] = name[:FIELD_MAX_LENGTH]
        ambiguous.discard(RequisiteField.COMPANY_NAME)
    return ParsedRequisites(
        values=Requisites(**{field.value: value for field, value in values.items()}),
        ambiguous=frozenset(ambiguous),
    )


def normalize_field(field: RequisiteField, raw: str) -> str | None:
    """Значение правки бухгалтера к виду хранения; None — не тот формат (ERR-DOCUMENTS-006)."""
    value = raw.strip()
    if not value:
        return ""
    if field is RequisiteField.BIN_IIN:
        compact = re.sub(r"\s", "", value)
        return compact if re.fullmatch(r"\d{12}", compact) else None
    if field is RequisiteField.IIK:
        compact = re.sub(r"\s", "", value).upper()
        return compact if _IIK_FORMAT.fullmatch(compact) else None
    if field is RequisiteField.BIK:
        compact = value.upper()
        return compact if _BIK_FORMAT.fullmatch(compact) else None
    return value


def field_warnings(values: Requisites) -> dict[RequisiteField, FieldWarning]:
    """Контрольные суммы БИН/ИИН и ИИК — подсказка, а не отказ (Р-5)."""
    warnings: dict[RequisiteField, FieldWarning] = {}
    if values.bin_iin and not bin_checksum_ok(values.bin_iin):
        warnings[RequisiteField.BIN_IIN] = FieldWarning.CHECKSUM
    if values.iik and not iban_checksum_ok(values.iik):
        warnings[RequisiteField.IIK] = FieldWarning.CHECKSUM
    return warnings


def bin_checksum_ok(value: str) -> bool:
    """Контрольная (12-я) цифра БИН/ИИН РК: веса 1…11, при остатке 10 — 3…11, 1, 2."""
    if not re.fullmatch(r"\d{12}", value):
        return False
    digits = [int(ch) for ch in value]
    for weights in (range(1, 12), (3, 4, 5, 6, 7, 8, 9, 10, 11, 1, 2)):
        control = sum(d * w for d, w in zip(digits[:11], weights, strict=True)) % 11
        if control != 10:
            return control == digits[11]
    return False


def iban_checksum_ok(value: str) -> bool:
    """IBAN mod 97 (ISO 13616): первые 4 знака в конец, буквы — числами, остаток 1."""
    if not _IIK_FORMAT.fullmatch(value):
        return False
    rearranged = value[4:] + value[:4]
    return int("".join(str(int(ch, 36)) for ch in rearranged)) % 97 == 1


def _normalize(text: str) -> str:
    # Textarea формы шлёт CRLF: «\r\n» — один перевод строки, иначе значение
    # после метки на следующей строке отрезала бы пустая (ревью PR #510, Н-21).
    return text.replace(" ", " ").replace("\r\n", "\n").replace("\r", "\n")


def _labelled(text: str, start: int, label: re.Pattern[str]) -> bool:
    return label.search(text[max(0, start - _LABEL_WINDOW) : start]) is not None


def _iik_candidates(text: str) -> list[tuple[tuple[int, int], _Candidate]]:
    found: list[tuple[tuple[int, int], _Candidate]] = []
    for match in _IIK_START.finditer(text):
        body: list[str] = []
        index = match.end()
        while len(body) < _IIK_BODY and index < len(text):
            char = text[index]
            if char.isascii() and (char.isdigit() or char.isupper()):
                body.append(char)
            elif char != " " or text[index - 1] == " ":
                break
            index += 1
        if len(body) < _IIK_BODY or (index < len(text) and text[index].isalnum()):
            continue
        value = match.group() + "".join(body)
        found.append(
            ((match.start(), index), _Candidate(value, _labelled(text, match.start(), _IIK_LABEL)))
        )
    return found


def _bik_candidates(text: str) -> list[tuple[tuple[int, int], _Candidate]]:
    return [
        (match.span(), _Candidate(match.group(), _labelled(text, match.start(), _BIK_LABEL)))
        for match in _BIK_CANDIDATE.finditer(text)
    ]


def _bin_candidates(text: str) -> list[_Candidate]:
    candidates = []
    for match in _BIN_CANDIDATE.finditer(text):
        labelled = _labelled(text, match.start(), _BIN_LABEL)
        if labelled or bin_checksum_ok(match.group()):
            candidates.append(_Candidate(match.group(), labelled))
    return candidates


def _company_candidates(text: str) -> list[_Candidate]:
    candidates = []
    for match in _COMPANY.finditer(text):
        name = _NAME_STOP.split(match.group(), maxsplit=1)[0]
        name = re.sub(r"[ \t]{1,3}", " ", name).strip(" \t.:-")
        if name:
            candidates.append(_Candidate(name, labelled=False))
    return candidates


def _after_label(text: str, label: re.Pattern[str]) -> list[tuple[tuple[int, int], _Candidate]]:
    """Значение после метки в начале строки или ячейки: до табуляции или конца строки.

    Пустое значение на строке метки — берётся следующая строка (метка и
    значение в PDF-таблице часто ложатся на две строки).
    """
    found = []
    for match in label.finditer(text):
        end = _line_end(text, match.end())
        rest = text[match.end() : end]
        colon = rest.find(":", 0, _LABEL_TAIL)
        rest = rest[colon + 1 :] if colon != -1 else rest
        value = rest.strip(" \t-").split("\t", 1)[0].strip()
        if not value and end < len(text):
            next_end = _line_end(text, end + 1)
            value = text[end + 1 : next_end].split("\t", 1)[0].strip()
            end = next_end
        if value:
            found.append(((match.start(), end), _Candidate(value[:FIELD_MAX_LENGTH], True)))
    return found


def _line_end(text: str, start: int) -> int:
    end = text.find("\n", start)
    return end if end != -1 else len(text)


def _mask(text: str, spans: list[tuple[int, int]]) -> str:
    chars = list(text)
    for start, end in spans:
        chars[start:end] = " " * (end - start)
    return "".join(chars)


def _choose(candidates: list[_Candidate]) -> tuple[str, bool]:
    """Значение поля и признак «несколько вариантов»."""
    labelled = {candidate.value for candidate in candidates if candidate.labelled}
    pool = labelled or {candidate.value for candidate in candidates}
    if len(pool) > 1:
        return "", True
    return (next(iter(pool)), False) if pool else ("", False)
