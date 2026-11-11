"""Что изменил прогон CI: только документы или код (#1057, `hospitality.tools.ci_scope`).

Git — настоящий, во временном репозитории: проверочный merge-коммит PR собирается так же,
как его собирает GitHub (`main` первым родителем), push — одним коммитом squash-слияния.
"""

from __future__ import annotations

import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import pytest
import yaml

from hospitality.tools import ci_scope
from hospitality.tools.ci_scope import changed_paths, docs_only, is_doc_path

_SCRIPT = Path(ci_scope.__file__)
_CI = Path(__file__).resolve().parents[1] / ".github" / "workflows" / "ci.yml"


@pytest.mark.parametrize(
    "path",
    [
        "docs/QUEUE.md",
        "docs/later/2026-11-11-x.md",
        "docs/specs/0086-act.md",
        "docs/legal-notes.md",
        "docs/audits/data.json",
        "README.md",
        "CLAUDE.md",
        "PROJECT_EXECUTION_PLAN.md",
    ],
)
def test_doc_paths(path: str) -> None:
    assert is_doc_path(path)


@pytest.mark.parametrize(
    "path",
    [
        # Текст политики приложение отдаёт гостю — он едет в образ.
        "docs/legal/privacy-policy.md",
        "docs/legal",
        # Промпты — исполняемые .md: отбор по путям, а не по расширению.
        "src/hospitality/ai/prompts/guest_system.md",
        "src/hospitality/modules/requests/README.md",
        "src/hospitality/tools/demo_codex.md",
        ".github/workflows/ci.yml",
        "site/index.html",
        "pyproject.toml",
        "docs",
        "../docs/QUEUE.md",
        "",
    ],
)
def test_not_doc_paths(path: str) -> None:
    assert not is_doc_path(path)


def test_docs_only_needs_every_path_and_at_least_one() -> None:
    assert docs_only(["docs/QUEUE.md", "README.md"])
    assert not docs_only(["docs/QUEUE.md", "src/hospitality/app.py"])
    # Пустой дифф — изменения не видно, полный путь.
    assert not docs_only([])


@dataclass
class Repo:
    path: Path

    def git(self, *args: str) -> str:
        done = subprocess.run(
            ["git", *args], cwd=self.path, capture_output=True, text=True, check=False
        )
        assert done.returncode == 0, done.stderr
        return done.stdout.strip()

    def commit(self, files: dict[str, str], message: str = "правка") -> str:
        for name, text in files.items():
            target = self.path / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(text, encoding="utf-8")
        self.git("add", "-A")
        self.git("commit", "-q", "-m", message)
        return self.git("rev-parse", "HEAD")

    def pr_merge(self, branch_files: dict[str, str]) -> None:
        """Ветка PR от main и проверочный merge-коммит, как `refs/pull/N/merge` у GitHub."""
        self.git("checkout", "-q", "-b", "feature")
        self.commit(branch_files)
        self.git("checkout", "-q", "main")
        self.commit({"src/other.py": "x = 2\n"}, "main ушёл вперёд")
        self.git("merge", "-q", "--no-ff", "-m", "merge", "feature")


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Repo:
    for name, value in {
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@example.com",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@example.com",
    }.items():
        monkeypatch.setenv(name, value)
    subprocess.run(["git", "init", "-q", "-b", "main", str(tmp_path)], check=True)
    monkeypatch.chdir(tmp_path)
    result = Repo(tmp_path)
    result.commit({"README.md": "начало\n", "src/other.py": "x = 1\n"}, "начало")
    return result


def test_pr_sees_its_own_change_not_main(repo: Repo) -> None:
    # main ушёл вперёд кодом, PR — только документ: дифф с первым родителем — только PR.
    repo.pr_merge({"docs/QUEUE.md": "очередь\n"})
    assert changed_paths("pull_request", "") == ["docs/QUEUE.md"]


def test_pr_without_merge_commit_is_unknown(repo: Repo) -> None:
    repo.commit({"docs/QUEUE.md": "очередь\n"})
    assert changed_paths("pull_request", "") is None


def test_move_from_code_to_docs_shows_both_sides(repo: Repo) -> None:
    repo.git("checkout", "-q", "-b", "feature")
    (repo.path / "docs").mkdir()
    repo.git("mv", "src/other.py", "docs/other.py")
    repo.git("commit", "-q", "-m", "перенос")
    repo.git("checkout", "-q", "main")
    repo.git("merge", "-q", "--no-ff", "-m", "merge", "feature")

    paths = changed_paths("pull_request", "")
    assert paths is not None and sorted(paths) == ["docs/other.py", "src/other.py"]
    assert not docs_only(paths)


def test_cyrillic_file_name_is_plain(repo: Repo) -> None:
    repo.pr_merge({"docs/later/заметка.md": "потом\n"})
    assert changed_paths("pull_request", "") == ["docs/later/заметка.md"]


def test_push_compares_with_previous_head(repo: Repo) -> None:
    before = repo.git("rev-parse", "HEAD")
    repo.commit({"docs/QUEUE.md": "очередь\n", "README.md": "иначе\n"})
    assert changed_paths("push", before) == ["README.md", "docs/QUEUE.md"]


def test_push_of_several_commits_is_unknown(repo: Repo) -> None:
    before = repo.git("rev-parse", "HEAD")
    repo.commit({"docs/a.md": "a\n"})
    repo.commit({"docs/b.md": "b\n"})
    assert changed_paths("push", before) is None
    assert changed_paths("push", "") is None


def test_root_commit_and_other_events_are_unknown(repo: Repo) -> None:
    assert changed_paths("push", "0" * 40) is None
    repo.commit({"docs/a.md": "a\n"})
    assert changed_paths("workflow_dispatch", "") is None


def test_main_prints_outputs_for_github(repo: Repo, capsys: pytest.CaptureFixture[str]) -> None:
    repo.pr_merge({"docs/QUEUE.md": "очередь\n"})
    assert ci_scope.main(["--event", "pull_request"]) == 0
    assert capsys.readouterr().out.splitlines() == ["docs_only=true", "light=true"]

    before = repo.git("rev-parse", "HEAD")
    repo.commit({"docs/QUEUE.md": "снова\n"})
    ci_scope.main(["--event", "push", "--before", before])
    # Push из документов: деплоя нет, но проверки полные.
    assert capsys.readouterr().out.splitlines() == ["docs_only=true", "light=false"]


def test_code_change_takes_full_path(repo: Repo, capsys: pytest.CaptureFixture[str]) -> None:
    repo.pr_merge({"docs/QUEUE.md": "очередь\n", "src/hospitality/ai/prompts/guest.md": "p\n"})
    ci_scope.main(["--event", "pull_request"])
    captured = capsys.readouterr()
    assert captured.out.splitlines() == ["docs_only=false", "light=false"]
    assert "src/hospitality/ai/prompts/guest.md" in captured.err


def test_script_runs_without_project_on_path(repo: Repo) -> None:
    # Так её зовёт job changes: системный python3 -I до установки зависимостей.
    repo.pr_merge({"docs/QUEUE.md": "очередь\n"})
    done = subprocess.run(
        [sys.executable, "-I", str(_SCRIPT), "--event", "pull_request"],
        cwd=repo.path,
        capture_output=True,
        text=True,
        check=False,
    )
    assert done.returncode == 0, done.stderr
    assert done.stdout.splitlines() == ["docs_only=true", "light=true"]


def test_required_check_cannot_be_skipped() -> None:
    # Пропущенную job защита ветки считает зелёной: check обязана запускаться всегда
    # и ждать оба пути — иначе красные тесты или отменённый прогон дали бы merge.
    jobs = yaml.safe_load(_CI.read_text(encoding="utf-8"))["jobs"]
    check = jobs["check"]
    assert check["name"] == "check"
    assert check["if"] == "${{ always() }}"
    assert set(check["needs"]) == {"changes", "lint", "tests", "docs-tests"}
    for job in ("lint", "tests", "docs-tests", "audit", "migrations", "dev-environment"):
        assert jobs[job]["needs"] == "changes", job
    assert "docs_only" in jobs["deploy-staging"]["if"]
