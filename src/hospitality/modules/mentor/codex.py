"""Разбор кодекса отеля: «часть → раздел → глава → статья» (spec 0048 §3).

Перенос `hotelbot/kodeks.py` «Наставника» под mypy strict. Разметка — контракт данных
тенанта, а не код под отель (ADR-023): отель с другим документом приводит его к ней.
Файлы читаются по имени как один текст, по строке на элемент:

  Название: …              до первого заголовка — шапка (Название обязательно)
  # Общая часть            часть; главы второй и дальше частей — по должностям
  ## Раздел 1. …           раздел, номера подряд с 1
  ### Глава 1. …           глава, сквозная нумерация
  > …                      примечание к главе, сразу под её заголовком
  #### Статья 1. …         статья, сквозная нумерация; вставка — «Статья 14-1»
  1. …                     пункт статьи
  1) …                     подпункт последнего пункта
  Источник: …              документ-источник, обязателен, последней строкой статьи
  прочий текст             абзац статьи без номера

Ошибка разметки — `CodexMarkupError` с файлом и строкой: менеджер видит, что
исправить («Файл 20_front_office.md, строка 41: статья 15 вместо 14»).
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Final, NoReturn

_HEADING = re.compile(r"^(#{1,4})\s+(.+)$")
_SECTION = re.compile(r"^Раздел (\d+)\.\s+(.+)$")
_CHAPTER = re.compile(r"^Глава (\d+)\.\s+(.+)$")
_ARTICLE = re.compile(r"^Статья (\d+)(?:-(\d+))?\.\s+(.+)$")
_POINT = re.compile(r"^(\d+)\.\s+(.+)$")
_SUBPOINT = re.compile(r"^(\d+)\)\s+(.+)$")
_SOURCE = re.compile(r"^Источник:\s*(.+)$")
_META = re.compile(r"^([^:#>]{1,40}):\s+(.+)$")

# Предел длины статьи. Статья — один текст эмбеддинга, а у `text-embedding-3-small`
# предел одного текста — 8192 токена (ревью PR #509, Н-2): статья длиннее роняла бы
# всю пересборку ошибкой провайдера. 12 000 знаков — 8000 токенов даже при 1,5 знака
# на токен; у русского текста их больше, так что запас двойной.
MAX_ARTICLE_CHARS: Final = 12_000


class CodexMarkupError(ValueError):
    """Ошибка разметки: файл, строка и что не так — текст для менеджера."""

    def __init__(self, file_name: str, line: int, problem: str) -> None:
        self.file_name = file_name
        self.line = line
        self.problem = problem
        super().__init__(f"Файл {file_name}, строка {line}: {problem}")


@dataclass
class CodexPoint:
    number: str  # "" — абзац без номера
    text: str
    subpoints: list[tuple[str, str]] = field(default_factory=list)


@dataclass
class CodexArticle:
    number: str  # «14» или «14-1»
    title: str
    file_name: str
    line: int
    points: list[CodexPoint] = field(default_factory=list)
    source: str = ""

    @property
    def anchor(self) -> str:
        return f"st-{self.number}"

    @property
    def body(self) -> str:
        """Текст статьи без заголовка и источника — как он стоит в кодексе."""
        lines: list[str] = []
        for point in self.points:
            lines.append(f"{point.number}. {point.text}" if point.number else point.text)
            lines.extend(f"{number}) {text}" for number, text in point.subpoints)
        return "\n".join(lines)


@dataclass
class CodexChapter:
    number: int
    title: str
    note: str = ""
    articles: list[CodexArticle] = field(default_factory=list)

    @property
    def anchor(self) -> str:
        return f"glava-{self.number}"


@dataclass
class CodexSection:
    number: int
    title: str
    chapters: list[CodexChapter] = field(default_factory=list)

    @property
    def anchor(self) -> str:
        return f"razdel-{self.number}"


@dataclass
class CodexPart:
    number: int
    title: str
    sections: list[CodexSection] = field(default_factory=list)

    @property
    def anchor(self) -> str:
        return f"chast-{self.number}"


@dataclass
class Codex:
    meta: dict[str, str]
    parts: list[CodexPart]

    def walk(self) -> Iterator[tuple[CodexPart, CodexChapter, CodexArticle]]:
        """Статьи по порядку кодекса вместе с их частью и главой."""
        for part in self.parts:
            for section in part.sections:
                for chapter in section.chapters:
                    for article in chapter.articles:
                        yield part, chapter, article

    @property
    def articles(self) -> list[CodexArticle]:
        return [article for _, _, article in self.walk()]

    @property
    def positions(self) -> list[CodexChapter]:
        """Главы для фильтра «Для кого»: всё, кроме первой (общей) части."""
        return [c for p in self.parts[1:] for s in p.sections for c in s.chapters]


def parse_codex(files: list[tuple[str, str]]) -> Codex:
    """Разобрать файлы `(имя, текст)` по порядку имён; ошибка — `CodexMarkupError`."""
    names = [name for name, _ in files]
    if not files:
        raise CodexMarkupError("—", 0, "нет ни одного файла кодекса")
    if len(set(names)) != len(names):
        duplicate = next(name for name in names if names.count(name) > 1)
        raise CodexMarkupError(duplicate, 0, "два файла с одним именем")
    parser = _Parser()
    for name, text in sorted(files):
        for line_number, raw in enumerate(text.splitlines(), 1):
            parser.file_name, parser.line = name, line_number
            line = raw.strip()
            if line:
                parser.feed(line)
    return parser.finish()


class _Parser:
    def __init__(self) -> None:
        self.file_name = ""
        self.line = 0
        self.meta: dict[str, str] = {}
        self.parts: list[CodexPart] = []
        self.chapter_number = 0
        self.article_number = (0, 0)  # (номер, вставка): 14-1 → (14, 1)
        self.article: CodexArticle | None = None

    def fail(self, problem: str) -> NoReturn:
        raise CodexMarkupError(self.file_name, self.line, problem)

    def _part(self) -> CodexPart:
        if not self.parts:
            self.fail("нет заголовка части («# Общая часть»)")
        return self.parts[-1]

    def _section(self) -> CodexSection:
        part = self._part()
        if not part.sections:
            self.fail("нет заголовка раздела («## Раздел 1. …»)")
        return part.sections[-1]

    def _chapter(self) -> CodexChapter:
        section = self._section()
        if not section.chapters:
            self.fail("нет заголовка главы («### Глава 1. …»)")
        return section.chapters[-1]

    def feed(self, line: str) -> None:
        heading = _HEADING.match(line)
        if heading:
            self._close_article()
            level, title = len(heading.group(1)), heading.group(2).strip()
            adders = (self._add_part, self._add_section, self._add_chapter, self._add_article)
            adders[level - 1](title)
            return
        if not self.parts:
            meta = _META.match(line)
            if not meta:
                self.fail("до первого заголовка ждём строки «Ключ: значение»")
            self.meta[meta.group(1).strip()] = meta.group(2).strip()
            return
        if line.startswith(">"):
            chapter = self._chapter()
            if chapter.articles:
                self.fail("примечание «>» пишется сразу под заголовком главы")
            chapter.note = f"{chapter.note} {line.lstrip('> ').strip()}".strip()
            return
        if self.article is None:
            self.fail("текст вне статьи")
        self._add_text(self.article, line)

    def _add_text(self, article: CodexArticle, line: str) -> None:
        if article.source:
            self.fail("«Источник:» — последняя строка статьи")
        if source := _SOURCE.match(line):
            article.source = source.group(1).strip()
            return
        if sub := _SUBPOINT.match(line):
            if not article.points or not article.points[-1].number:
                self.fail("подпункт «1)» без пункта «1.» перед ним")
            subpoints = article.points[-1].subpoints
            if int(sub.group(1)) != len(subpoints) + 1:
                self.fail(f"подпункт {sub.group(1)}) вместо {len(subpoints) + 1})")
            subpoints.append((sub.group(1), sub.group(2).strip()))
            return
        if point := _POINT.match(line):
            expected = sum(1 for p in article.points if p.number) + 1
            if int(point.group(1)) != expected:
                self.fail(f"пункт {point.group(1)}. вместо {expected}.")
            article.points.append(CodexPoint(point.group(1), point.group(2).strip()))
            return
        article.points.append(CodexPoint("", line))

    def _add_part(self, title: str) -> None:
        self.parts.append(CodexPart(len(self.parts) + 1, title))

    def _add_section(self, title: str) -> None:
        match = _SECTION.match(title)
        if not match:
            self.fail("заголовок раздела: «## Раздел 1. Название»")
        number = int(match.group(1))
        expected = sum(len(p.sections) for p in self.parts) + 1
        if number != expected:
            self.fail(f"раздел {number} вместо {expected}")
        self._part().sections.append(CodexSection(number, match.group(2).strip()))

    def _add_chapter(self, title: str) -> None:
        match = _CHAPTER.match(title)
        if not match:
            self.fail("заголовок главы: «### Глава 1. Название»")
        number = int(match.group(1))
        if number != self.chapter_number + 1:
            self.fail(f"глава {number} вместо {self.chapter_number + 1}")
        self.chapter_number = number
        self._section().chapters.append(CodexChapter(number, match.group(2).strip()))

    def _add_article(self, title: str) -> None:
        match = _ARTICLE.match(title)
        if not match:
            self.fail("заголовок статьи: «#### Статья 1. Название»")
        number, insert = int(match.group(1)), int(match.group(2) or 0)
        last, last_insert = self.article_number
        if insert == 0 and number != last + 1:
            self.fail(f"статья {number} вместо {last + 1}")
        if insert and (number != last or insert != last_insert + 1):
            self.fail(
                f"статья {number}-{insert}: вставка идёт сразу за статьёй {number} по порядку"
            )
        self.article_number = (number, insert)
        self.article = CodexArticle(
            number=f"{number}-{insert}" if insert else str(number),
            title=match.group(3).strip(),
            file_name=self.file_name,
            line=self.line,
        )
        self._chapter().articles.append(self.article)

    def _close_article(self) -> None:
        article = self.article
        if article is None:
            return
        if not article.points:
            self.fail(f"статья {article.number} без текста")
        if not article.source:
            self.fail(f"у статьи {article.number} нет строки «Источник: …»")
        if len(article.body) > MAX_ARTICLE_CHARS:
            raise CodexMarkupError(
                article.file_name,
                article.line,
                f"статья {article.number} длиннее {MAX_ARTICLE_CHARS} знаков — разделите её на две",
            )
        self.article = None

    def finish(self) -> Codex:
        self._close_article()
        if "Название" not in self.meta:
            self.fail("в шапке нет строки «Название: …»")
        if not self.parts:
            self.fail("нет ни одной части («# Общая часть»)")
        for part in self.parts:
            if not part.sections:
                self.fail(f"в части «{part.title}» нет разделов")
            for section in part.sections:
                if not section.chapters:
                    self.fail(f"в разделе {section.number} нет глав")
                for chapter in section.chapters:
                    if not chapter.articles:
                        self.fail(f"в главе {chapter.number} нет статей")
        return Codex(self.meta, self.parts)
