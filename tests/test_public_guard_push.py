"""Щит публичности ops/public_guard.py (#1023): дверь git — настоящий `git push`.

Во временной папке — «удалённый» голый репозиторий и рабочий клон с копией скрипта и
заглушкой pre-push, поставленной самим скриптом. Хук зовёт git, как у настоящего push:
проверяется поведение (ушла ветка или нет), а не текст скрипта.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
GUARD = REPO_ROOT / "ops" / "public_guard.py"
HOTEL = "Ромашка-Плаза"


@dataclass
class Sandbox:
    work: Path
    env: dict[str, str]

    def git(self, *args: str, ok: bool = True) -> subprocess.CompletedProcess[str]:
        done = subprocess.run(
            ["git", *args],
            cwd=self.work,
            env=self.env,
            capture_output=True,
            text=True,
            check=False,
            timeout=60,
        )
        if ok:
            assert done.returncode == 0, done.stderr
        return done

    def commit(self, name: str, text: str, message: str = "правка") -> None:
        path = self.work / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        self.git("add", "-f", name)
        self.git("commit", "-m", message)

    def push(self, branch: str = "feature") -> subprocess.CompletedProcess[str]:
        return self.git("push", "origin", f"HEAD:refs/heads/{branch}", ok=False)

    def on_remote(self, branch: str = "feature") -> bool:
        return bool(self.git("ls-remote", "origin", f"refs/heads/{branch}").stdout.strip())

    def guard(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(self.work / "ops" / "public_guard.py"), *args],
            cwd=self.work,
            env=self.env,
            capture_output=True,
            text=True,
            check=False,
            timeout=60,
        )


@pytest.fixture
def box(tmp_path: Path) -> Sandbox:
    listed = tmp_path / "closed.local"
    listed.write_text(f"отель | {HOTEL}\n", encoding="utf-8")
    env = {
        # Хук зовёт `python3` из PATH — первым ставится интерпретатор самих тестов.
        "PATH": f"{Path(sys.executable).parent}:/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin",
        "HOME": str(tmp_path),
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@example.com",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@example.com",
        "PUBLIC_GUARD_LIST": str(listed),
        "PUBLIC_GUARD_ENV": str(tmp_path / "no-env"),
    }
    work = tmp_path / "work"
    subprocess.run(
        ["git", "init", "-q", "--bare", str(tmp_path / "origin.git")], env=env, check=True
    )
    subprocess.run(["git", "init", "-q", "-b", "main", str(work)], env=env, check=True)
    box = Sandbox(work=work.resolve(), env=env)
    box.git("remote", "add", "origin", str(tmp_path / "origin.git"))
    (work / "ops").mkdir()
    shutil.copy(GUARD, work / "ops" / "public_guard.py")
    box.commit("README.md", "начало\n", "начало")
    box.git("add", "ops/public_guard.py")
    box.git("commit", "-m", "щит")
    box.git("push", "origin", "HEAD:refs/heads/main")
    box.git("fetch", "origin")
    assert "заглушка поставлена" in box.guard("install").stdout
    return box


def test_clean_push_goes_through(box: Sandbox) -> None:
    box.commit("docs/note.md", "В пилотном отеле всё готово\n")
    assert box.push().returncode == 0
    assert box.on_remote()


def test_push_with_listed_sample_in_an_added_line_is_stopped(box: Sandbox) -> None:
    box.commit("docs/note.md", f"первая\nвторая\nЗаселение в {HOTEL}\n")
    result = box.push()
    assert result.returncode != 0
    assert not box.on_remote()
    assert "docs/note.md:3" in result.stderr
    assert "отель" in result.stderr


def test_sample_added_and_removed_in_later_commit_still_stops_push(box: Sandbox) -> None:
    box.commit("docs/note.md", f"{HOTEL}\n")
    box.commit("docs/note.md", "пилотный отель\n")
    assert box.push().returncode != 0  # история хранит первый коммит
    assert not box.on_remote()


def test_commit_message_is_checked(box: Sandbox) -> None:
    box.commit("docs/note.md", "текст\n", message=f"Правка для {HOTEL}")
    result = box.push()
    assert result.returncode != 0
    assert "сообщение коммита" in result.stderr


def test_line_that_looks_like_a_file_header_does_not_blind_the_check(box: Sandbox) -> None:
    box.commit("docs/note.md", f"++ не заголовок\n-- и это тоже\n{HOTEL}\n")
    result = box.push()
    assert result.returncode != 0
    assert "docs/note.md:3" in result.stderr


def test_what_is_already_public_is_not_checked_again(box: Sandbox) -> None:
    # В «публичном» main закрытое уже лежит (ушло до щита); ветка отстала и вливает main.
    box.git("checkout", "-q", "-b", "feature")
    box.commit("docs/mine.md", "моя правка\n")
    box.git("checkout", "-q", "main")
    box.commit("docs/old.md", f"давно опубликовано: {HOTEL}\n")
    assert box.git("push", "--no-verify", "origin", "HEAD:refs/heads/main").returncode == 0
    box.git("fetch", "origin")
    box.git("checkout", "-q", "feature")
    box.git("merge", "-q", "--no-edit", "origin/main")
    assert box.push().returncode == 0
    assert box.on_remote()


def test_closed_text_written_while_resolving_a_merge_is_stopped(box: Sandbox) -> None:
    box.commit("docs/shared.md", "общая строка\n")
    box.git("push", "origin", "HEAD:refs/heads/main")
    box.git("checkout", "-q", "-b", "feature")
    box.commit("docs/shared.md", "строка ветки\n")
    box.git("checkout", "-q", "main")
    box.commit("docs/shared.md", "строка main\n")
    box.git("push", "origin", "HEAD:refs/heads/main")
    box.git("fetch", "origin")
    box.git("checkout", "-q", "feature")
    assert box.git("merge", "--no-edit", "origin/main", ok=False).returncode != 0  # конфликт
    (box.work / "docs" / "shared.md").write_text(f"сведено: {HOTEL}\n", encoding="utf-8")
    box.git("add", "docs/shared.md")
    box.git("commit", "--no-edit")
    result = box.push()
    assert result.returncode != 0
    assert "docs/shared.md:1" in result.stderr


def test_file_that_has_no_place_in_the_repository_is_stopped(box: Sandbox) -> None:
    box.commit("ops/deploy/.env.staging.example", "TOKEN=\n")
    assert box.push("examples").returncode == 0
    for name in (".env", "ops/notes.local", "backups/night.dump", "keys/id_ed25519"):
        box.git("checkout", "-q", "-B", "feature", "origin/main")
        box.commit(name, "x\n")
        result = box.push()
        assert result.returncode != 0, name
        assert name in result.stderr


def test_generic_pattern_stops_push_without_any_list(box: Sandbox) -> None:
    box.env["PUBLIC_GUARD_LIST"] = str(box.work.parent / "absent.local")
    address = ".".join(("93", "184", "216", "34"))
    box.commit("docs/runbook.md", f"ssh deploy@{address}\n")
    result = box.push()
    assert result.returncode != 0
    assert "адрес сервера" in result.stderr
    assert "закрытого списка нет" in result.stderr


def test_deleting_a_remote_branch_is_not_checked(box: Sandbox) -> None:
    assert box.push("gone").returncode == 0
    box.commit("docs/note.md", f"{HOTEL}\n")  # лежит локально, с удалением ветки не уходит
    assert box.git("push", "origin", ":refs/heads/gone", ok=False).returncode == 0
    assert not box.on_remote("gone")


def test_failure_of_the_guard_itself_lets_the_push_through_loudly(box: Sandbox) -> None:
    Path(box.env["PUBLIC_GUARD_LIST"]).write_bytes(b"\xff\xfe\x00 not utf-8 \xff")
    box.commit("docs/note.md", "обычная правка\n")
    result = box.push()
    assert result.returncode == 0
    assert "ошибка самого щита" in result.stderr


def test_push_without_the_script_goes_through_with_a_warning(box: Sandbox) -> None:
    box.git("rm", "-q", "ops/public_guard.py")
    box.git("commit", "-m", "ветка старше щита")
    result = box.push()
    assert result.returncode == 0
    assert "не найден" in result.stderr


def test_install_is_repeatable_and_leaves_a_foreign_hook_alone(box: Sandbox) -> None:
    hook = box.work / ".git" / "hooks" / "pre-push"
    assert "заглушка на месте" in box.guard("install").stdout
    hook.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    assert "чужой хук" in box.guard("install").stdout
    assert hook.read_text(encoding="utf-8") == "#!/bin/sh\nexit 0\n"


def test_scan_of_a_commit_range_reports_commit_file_and_line(box: Sandbox) -> None:
    box.commit("docs/note.md", f"строка\n{HOTEL}\n")
    result = box.guard("scan", "--range", "origin/main..HEAD")
    assert result.returncode == 1
    assert "docs/note.md:2" in result.stdout
    assert "чисто" in box.guard("scan", "--range", "origin/main..origin/main").stdout
