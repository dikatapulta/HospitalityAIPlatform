"""Что изменил прогон CI: только документы или код (#1057, аудит процесса 11.11, Р-1).

Очередь слияния строго последовательная — одно слияние стоит одного прогона `check`,
а 42 % слияний меняют только документы. PR только с документами идёт лёгким путём:
тесты, читающие документы (`tests/ci_select.py`), под тем же именем job `check`.
Push в `main` из одних документов проходит полный `check`, но staging не деплоит.

Модуль зовёт job `changes` в `.github/workflows/ci.yml` до установки зависимостей —
системным `python3 -I` по пути к файлу: только стандартная библиотека, пакет
`hospitality` не импортируется. Вывод — строки `docs_only=…` и `light=…` для
`$GITHUB_OUTPUT`; пояснение — в stderr, оно же лог job.

Сомнение — всегда полный путь: дифф не определить, он пуст, событие не PR и не push,
путь вне списка документов. Ошибиться в сторону полного прогона стоит минут CI,
в сторону лёгкого — красного `main`.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from collections.abc import Sequence
from pathlib import PurePosixPath

# Документы — каталог docs/ и .md в корне репозитория. Отбор по путям, а не по
# расширению: промпты в src/hospitality/ai/prompts — исполняемые .md.
_DOCS_DIR = "docs"
# docs/legal — исключение: политику конфиденциальности приложение отдаёт гостю
# страницей /legal/privacy (spec 0029), текст едет в образ (COPY docs/legal в ops/Dockerfile).
_RUNTIME_DOCS = ("docs/legal",)
_ROOT_DOC_SUFFIX = ".md"


def is_doc_path(path: str) -> bool:
    """Путь от корня репозитория — документ, который не читает ни сборка, ни приложение."""
    parts = PurePosixPath(path).parts
    if not parts or ".." in parts:
        return False
    if len(parts) == 1:
        return parts[0].endswith(_ROOT_DOC_SUFFIX)
    normalized = "/".join(parts)
    if any(normalized == root or normalized.startswith(root + "/") for root in _RUNTIME_DOCS):
        return False
    return parts[0] == _DOCS_DIR


def docs_only(paths: Sequence[str]) -> bool:
    """Все пути — документы; пустой список — нет: изменения не видно, значит полный путь."""
    return bool(paths) and all(is_doc_path(path) for path in paths)


def _git(*args: str) -> str | None:
    result = subprocess.run(["git", *args], capture_output=True, text=True, check=False)
    return result.stdout if result.returncode == 0 else None


def _parents(revision: str) -> list[str] | None:
    out = _git("rev-list", "--parents", "-n", "1", revision)
    return out.split()[1:] if out else None


def changed_paths(event: str, before: str) -> list[str] | None:
    """Пути, которые меняет прогон; None — изменение не определить.

    PR: GitHub собирает проверочный merge-коммит «main + PR», первый родитель — `main`,
    дифф с ним и есть то, что PR внесёт сейчас, после любых обновлений ветки. Push:
    коммит squash-слияния, первый родитель обязан быть прежней головой (`before`),
    иначе в push больше одного коммита. `--no-renames`: перенос файла из кода в docs/
    виден как удаление в коде, а не как новый документ; `-z` — кириллица в имени файла
    без кавычек и экранирования git.
    """
    parents = _parents("HEAD")
    if parents is None:
        return None
    if event == "pull_request":
        if len(parents) != 2:
            return None
    elif event == "push":
        if len(parents) != 1 or not before or parents[0] != before:
            return None
    else:
        return None
    out = _git("diff", "--name-only", "--no-renames", "-z", parents[0], "HEAD")
    if out is None:
        return None
    return [path for path in out.split("\0") if path]


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    parser.add_argument("--event", required=True, help="github.event_name")
    parser.add_argument("--before", default="", help="github.event.before (push)")
    args = parser.parse_args(argv)

    paths = changed_paths(args.event, args.before)
    only_docs = paths is not None and docs_only(paths)
    light = only_docs and args.event == "pull_request"
    if paths is None:
        why = "изменение не определить — полный путь"
    elif not paths:
        why = "изменённых файлов нет — полный путь"
    else:
        code = [path for path in paths if not is_doc_path(path)]
        why = f"файлов {len(paths)}, не документов {len(code)}" + (
            f", первый — {code[0]}" if code else ""
        )
    print(f"ci_scope: {args.event}: {why}; docs_only={only_docs}, light={light}", file=sys.stderr)
    print(f"docs_only={str(only_docs).lower()}")
    print(f"light={str(light).lower()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
