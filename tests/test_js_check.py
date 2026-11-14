"""Разбор JavaScript в CI: тип файла и проверка проверки (#1056 п. 3).

Настоящий node зовёт job `lint` и `make check`; здесь — поддельный, чтобы тесты
не зависели от версии Node на машине.
"""

from __future__ import annotations

from pathlib import Path, PurePosixPath

import pytest

from hospitality.tools import js_check
from hospitality.tools.js_check import Kind

ROOT = Path(__file__).resolve().parents[1]

_STDERR = (
    "[stdin]:2\nexport const b = (a;\n                   ^\n\n"
    "SyntaxError: Unexpected token ';'\n"
    "    at checkSyntax (node:internal/main/check_syntax:72:5)\n\n"
    "Node.js v24.16.0\n"
)


def _honest(kind: Kind, text: str) -> tuple[int, str]:
    """Как node с явным --input-type: битый текст и синтаксис модуля в скрипте — код 1."""
    module_syntax = any(line.startswith(("import ", "export ")) for line in text.splitlines())
    broken = "(a;" in text or "( {" in text or "BROKEN" in text
    return (1, _STDERR) if broken or (kind == "commonjs" and module_syntax) else (0, "")


def _guessing(kind: Kind, text: str) -> tuple[int, str]:
    """Как `node --check путь.js` на Node 24: файл с import/export — код 0 всегда."""
    if any(line.startswith(("import ", "export ")) for line in text.splitlines()):
        return 0, ""
    return _honest(kind, text)


def test_classify_module_by_syntax_import_and_suffix() -> None:
    sources = {
        PurePosixPath("a/viewer.js"): 'import * as T from "./three.module.min.js";\n',
        PurePosixPath("a/three.module.min.js"): "/** x */\nconst t=1;export{t};",
        PurePosixPath("a/core.js"): '  export const x = 1;\nimport "../b/side.js";\n',
        PurePosixPath("b/side.js"): "window.side = 1;\n",
        PurePosixPath("b/lazy.js"): 'import("./side.js").then(run);\nimportScripts("w.js");\n',
        PurePosixPath("b/plain.js"): "var x = 1; // from './side.js'\n",
        PurePosixPath("b/tool.mjs"): "var y = 2;\n",
    }
    assert js_check.classify(sources) == {
        PurePosixPath("a/viewer.js"): "module",
        PurePosixPath("a/three.module.min.js"): "module",
        PurePosixPath("a/core.js"): "module",
        PurePosixPath("b/side.js"): "module",
        PurePosixPath("b/lazy.js"): "commonjs",
        PurePosixPath("b/plain.js"): "commonjs",
        PurePosixPath("b/tool.mjs"): "module",
    }


def test_self_check_rejects_node_that_guesses_the_type() -> None:
    assert js_check.self_check(_honest) == []
    problems = js_check.self_check(_guessing)
    assert len(problems) == 1
    assert "--input-type=module" in problems[0]
    assert "принял битый" in problems[0]


def test_check_sources_names_file_and_line_without_stack() -> None:
    sources = {
        PurePosixPath("src/ok.js"): "var ok = 1;\n",
        PurePosixPath("src/bad.js"): 'export const b = (a;\nimport "./ok.js";\n',
    }
    errors = js_check.check_sources(sources, _honest)
    assert len(errors) == 1
    assert errors[0].startswith("src/bad.js (разобран как module):\nsrc/bad.js:2\n")
    assert "SyntaxError: Unexpected token ';'" in errors[0]
    assert " at " not in errors[0]
    assert "Node.js v" not in errors[0]


def test_main_parses_every_tracked_file(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    seen: list[Kind] = []

    def run(kind: Kind, text: str) -> tuple[int, str]:
        seen.append(kind)
        return _honest(kind, text)

    monkeypatch.setattr(js_check, "run_node", run)
    assert js_check.main() == 0
    out = capsys.readouterr().out
    assert "с ошибкой 0" in out
    # Витрина — модули (#799): без них проверка разбирала бы одни скрипты.
    assert seen.count("module") >= 15
    assert seen.count("commonjs") >= 40


def test_main_fails_on_broken_file(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    broken = (ROOT / "src/hospitality/staff_portal/static/mentor_exam.js").read_text(
        encoding="utf-8"
    )

    def run(kind: Kind, text: str) -> tuple[int, str]:
        return (1, _STDERR) if text == broken else _honest(kind, text)

    monkeypatch.setattr(js_check, "run_node", run)
    assert js_check.main() == 1
    captured = capsys.readouterr()
    assert (
        "src/hospitality/staff_portal/static/mentor_exam.js (разобран как commonjs)" in captured.err
    )
    assert "с ошибкой 1" in captured.out


@pytest.mark.parametrize(
    ("runner", "message"),
    [(_guessing, "этот node не годится"), (None, "нет node в PATH")],
    ids=["guessing-node", "no-node"],
)
def test_main_refuses_unfit_node(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    runner: js_check.NodeRunner | None,
    message: str,
) -> None:
    def missing(kind: Kind, text: str) -> tuple[int, str]:
        raise FileNotFoundError("node")

    monkeypatch.setattr(js_check, "run_node", runner or missing)
    assert js_check.main() == 2
    assert message in capsys.readouterr().err
