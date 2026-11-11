"""Разбор JavaScript без браузера (#1056 п. 3, аудит процесса 11.11, П-5).

Ни один шаг CI не разбирал JavaScript кабинета и гостевых страниц: лишняя скобка гасит
экран целиком (экран экзамена «Наставника» управляется JS), а линтеры и тесты Python её
не видят. Модуль разбирает движком Node каждый отслеживаемый git файл `.js` и `.mjs` —
`node --check`, без исполнения — и называет файл и строку каждой синтаксической ошибки.
Зовут его job `lint` в `.github/workflows/ci.yml` и `make check`.

Тип файла задаётся явно (`--input-type`), текст идёт через stdin: `node --check путь.js`
сам угадывает тип по содержимому и на `.js` с import/export и синтаксической ошибкой
отвечает кодом 0 (Node 24.16, проверено 12.11.2026) — на модулях витрины проверка молча
не проверяла бы ничего. Модуль — `.mjs`, файл со строкой import/export верхнего уровня
и файл, который статически импортирует модуль (так в браузер попадает
`three-….module.min.js`); остальное — классический скрипт. Ошибка этого правила
в сторону скрипта громкая: синтаксис модуля в скрипте — тоже синтаксическая ошибка.

Перед файлами — проверка самой проверки: заведомо битые модуль и скрипт обязаны упасть,
целые — пройти. Иначе этот Node не годится, и красный весь шаг.
"""

from __future__ import annotations

import re
import subprocess
import sys
from collections.abc import Callable, Mapping
from pathlib import Path, PurePosixPath
from typing import Literal

Kind = Literal["module", "commonjs"]
# (тип, текст) → (код выхода node, его stderr).
NodeRunner = Callable[[Kind, str], tuple[int, str]]

# Строка import/export верхнего уровня. `import(` и `import.meta` есть и у скрипта.
_MODULE_LINE = re.compile(r"^[ \t]*(?:import\b(?![ \t]*[(.])|export\b)", re.MULTILINE)
# Статический импорт по относительному пути: `from "./x.js"` и `import "./x.js"`.
_STATIC_IMPORT = re.compile(
    r"""(?:\bfrom|^[ \t]*import)[ \t]*(["'])(?P<spec>\.{1,2}/[^"'\n]+)\1""", re.MULTILINE
)
# Проверка проверки: (тип, текст, должен ли node его принять).
_SELF_CHECK: tuple[tuple[Kind, str, bool], ...] = (
    ("module", 'import { a } from "./a.js";\nexport const b = a;\n', True),
    ("module", 'import { a } from "./a.js";\nexport const b = (a;\n', False),
    ("commonjs", "var a = function () { return 1; };\n", True),
    ("commonjs", "var a = function ( { return 1; };\n", False),
)
_STACK_LINE = re.compile(r"^\s+at |^Node\.js v")


def classify(sources: Mapping[PurePosixPath, str]) -> dict[PurePosixPath, Kind]:
    """Путь от корня репозитория → тип, которым его разбирать."""
    modules = {
        path for path, text in sources.items() if path.suffix == ".mjs" or _MODULE_LINE.search(text)
    }
    for path in list(modules):
        for match in _STATIC_IMPORT.finditer(sources[path]):
            target = _normalize(path.parent / match["spec"])
            if target in sources:
                modules.add(target)
    return {path: ("module" if path in modules else "commonjs") for path in sources}


def _normalize(path: PurePosixPath) -> PurePosixPath:
    parts: list[str] = []
    for part in path.parts:
        if part == "..":
            if parts:
                parts.pop()
        elif part != ".":
            parts.append(part)
    return PurePosixPath(*parts)


def self_check(run: NodeRunner) -> list[str]:
    """Расхождения node с заведомым ответом; пустой список — проверке можно верить."""
    problems = []
    for kind, text, valid in _SELF_CHECK:
        code, _ = run(kind, text)
        if (code == 0) != valid:
            verdict = "принял битый" if code == 0 else "отверг целый"
            problems.append(f"node --input-type={kind} --check {verdict} образец: {text!r}")
    return problems


def check_sources(sources: Mapping[PurePosixPath, str], run: NodeRunner) -> list[str]:
    """Синтаксические ошибки: по одной строке-сообщению на файл, путь — первым."""
    errors = []
    for path, kind in sorted(classify(sources).items()):
        code, stderr = run(kind, sources[path])
        if code != 0:
            errors.append(_format_error(path, kind, stderr))
    return errors


def _format_error(path: PurePosixPath, kind: Kind, stderr: str) -> str:
    lines = [line for line in stderr.splitlines() if not _STACK_LINE.match(line)]
    body = "\n".join(lines).strip().replace("[stdin]", str(path))
    return f"{path} (разобран как {kind}):\n{body}"


def run_node(kind: Kind, text: str) -> tuple[int, str]:
    result = subprocess.run(
        ["node", f"--input-type={kind}", "--check"],
        input=text,
        capture_output=True,
        text=True,
        check=False,
    )
    return result.returncode, result.stderr


def tracked_sources(root: Path) -> dict[PurePosixPath, str]:
    listing = subprocess.run(
        ["git", "-C", str(root), "ls-files", "-z", "--", "*.js", "*.mjs"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    paths = [PurePosixPath(name) for name in listing.split("\0") if name]
    return {path: (root / path).read_text(encoding="utf-8") for path in paths}


def main() -> int:
    root = Path(
        subprocess.run(
            ["git", "rev-parse", "--show-toplevel"], capture_output=True, text=True, check=True
        ).stdout.strip()
    )
    try:
        problems = self_check(run_node)
    except FileNotFoundError:
        print("js_check: нет node в PATH — нужен Node.js, CI ставит 24 (#1056)", file=sys.stderr)
        return 2
    if problems:
        print("js_check: этот node не годится для проверки —", *problems, sep="\n", file=sys.stderr)
        return 2
    sources = tracked_sources(root)
    errors = check_sources(sources, run_node)
    for error in errors:
        print(error, end="\n\n", file=sys.stderr)
    modules = sum(1 for kind in classify(sources).values() if kind == "module")
    print(f"JS: разобрано файлов {len(sources)} (модулей {modules}), с ошибкой {len(errors)}")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
