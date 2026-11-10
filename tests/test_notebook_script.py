"""Тесты ops/notebook.sh — чтения общих тетрадей «потом» и очереди основателя (#532).

Скрипт работает от своей папки (`ops/..`), поэтому тест копирует его во временное дерево
с поддельными архивом и записями и проверяет главное: печатается архив и все записи
по дате, README папки не печатается, запись с неверным именем не теряется молча.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
NOTEBOOK_SCRIPT = REPO_ROOT / "ops" / "notebook.sh"


def _tree(tmp_path: Path) -> Path:
    (tmp_path / "ops").mkdir()
    shutil.copy(NOTEBOOK_SCRIPT, tmp_path / "ops" / "notebook.sh")
    later = tmp_path / "docs" / "later"
    later.mkdir(parents=True)
    (tmp_path / "docs" / "LATER.md").write_text("- [ ] 13.10 — archive-line\n")
    (later / "README.md").write_text("readme-text\n")
    (later / "2026-11-02-pr540-second.md").write_text("- [ ] 02.11 — second-line\n")
    (later / "2026-10-30-pr535-first.md").write_text("- [ ] 30.10 — first-line\n")
    return tmp_path


def _run(root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(root / "ops" / "notebook.sh"), *args],
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )


def test_prints_archive_then_entries_by_date_without_readme(tmp_path: Path) -> None:
    result = _run(_tree(tmp_path), "later")

    assert result.returncode == 0, result.stderr
    out = result.stdout
    assert out.index("archive-line") < out.index("first-line") < out.index("second-line")
    assert "readme-text" not in out
    assert "записей 2" in out
    assert result.stderr == ""


def test_misnamed_entry_is_printed_with_warning(tmp_path: Path) -> None:
    root = _tree(tmp_path)
    (root / "docs" / "later" / "Находка.md").write_text("- [ ] 31.10 — misnamed-line\n")

    result = _run(root, "later")

    assert result.returncode == 0, result.stderr
    assert "misnamed-line" in result.stdout
    assert "записей 3" in result.stdout
    assert "имя не по формату" in result.stderr


def test_unknown_notebook_is_refused(tmp_path: Path) -> None:
    result = _run(_tree(tmp_path), "elsewhere")

    assert result.returncode == 2
    assert "usage" in result.stderr


def _decisions_tree(tmp_path: Path) -> Path:
    root = _tree(tmp_path)
    review = root / "docs" / "founder-review"
    review.mkdir()
    (root / "docs" / "FOUNDER_REVIEW_QUEUE.md").write_text("- [ ] 20.10 — archive-decision\n")
    (review / "README.md").write_text("- [ ] readme-decision\n")
    (review / "2026-11-08-pr880-old.md").write_text("- [ ] 08.11 — old-open\n")
    (review / "2026-11-09-pr890-seen.md").write_text("- [x] 09.11 — seen-decision\n")
    (review / "2026-11-10-pr900-new.md").write_text(
        "- [x] 10.11 — new-topic\n  - [ ] 10.11 — new-open\n"
    )
    return root


def test_decisions_prints_only_unreviewed_entries_without_archive(tmp_path: Path) -> None:
    result = _run(_decisions_tree(tmp_path), "decisions")

    assert result.returncode == 0, result.stderr
    out = result.stdout
    assert out.index("old-open") < out.index("new-open")
    for hidden in ("archive-decision", "readme-decision", "seen-decision"):
        assert hidden not in out
    assert "непросмотренных записей 2" in out


def test_decisions_since_date_skips_older_entries(tmp_path: Path) -> None:
    result = _run(_decisions_tree(tmp_path), "decisions", "2026-11-09")

    assert result.returncode == 0, result.stderr
    assert "old-open" not in result.stdout
    assert "new-open" in result.stdout
    assert "непросмотренных записей 1 с 2026-11-09" in result.stdout


def test_decisions_refuses_malformed_date(tmp_path: Path) -> None:
    result = _run(_decisions_tree(tmp_path), "decisions", "10.11")

    assert result.returncode == 2
    assert "usage" in result.stderr


def test_real_notebooks_print() -> None:
    for notebook in ("later", "founder-review"):
        result = subprocess.run(
            ["bash", str(NOTEBOOK_SCRIPT), notebook],
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )
        assert result.returncode == 0, result.stderr
        assert "архив до 29.10.2026" in result.stdout
        assert result.stderr == "", result.stderr
