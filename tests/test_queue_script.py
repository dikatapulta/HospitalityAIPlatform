"""Тесты ops/queue.sh — механизма правила исполнения 10 (ревью PR #176, находка А10).

Скрипт обязан ГРОМКО показывать расхождения между docs/QUEUE.md и GitHub:
молчащая сверка хуже отсутствующей — сессия примет «нет расхождений» за истину.
GitHub подменяется фейковым `gh` в PATH (фикстурные JSON), файл очереди —
фикстурой через переменную QUEUE_FILE. Расхождение печатается явной строкой
с «!», но код выхода остаётся 0: расхождение — рабочий сигнал правила 10
(«поправь файл»), а не отказ скрипта; ненулевой код — только «очередь не прочитана».
"""

from __future__ import annotations

import json
import os
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
QUEUE_SCRIPT = REPO_ROOT / "ops" / "queue.sh"

FAKE_GH = """#!/usr/bin/env bash
case "$*" in
*"pr list"*) cat "$GH_FIXTURES/prs.json" ;;
*"issue list"*) cat "$GH_FIXTURES/issues.json" ;;
*"issue view"*) cat "$GH_FIXTURES/batch.json" ;;
*) exit 1 ;;
esac
"""

# gh жив, но копилку не отдаёт (нет прав, issue удалён) — порог считать нечем.
NO_BATCH_GH = FAKE_GH.replace(
    '*"issue view"*) cat "$GH_FIXTURES/batch.json" ;;', '*"issue view"*) exit 1 ;;'
)

BROKEN_GH = """#!/usr/bin/env bash
exit 1
"""

QUEUE_FIXTURE = """# QUEUE.md — фикстура

## 0. Открытые PR — ведёт GitHub, не файл

Раздел печатает make queue живьём из GitHub, в файле он не ведётся.

## 1. Дорожка «Код»

- **#172** — маскирование карт калечит UUID — **Opus 5 `xhigh`**
- **#133** — outbox: dead-letter + алерт — **Opus 5 `xhigh`**
- **#125 + #103** — лимит LLM + алерт на ~80% — **Opus 5 `high`**
  Упоминание в прозе не считается пунктом: #41 закрыт, спека была в #79.

## 2. Дорожка «Инфра / Legal»

- **#49** — гейт продакшена.

## 3. Отложено осознанно

**#55** (защита от ошибочного `done`).
"""

OPEN_ISSUES: list[dict[str, Any]] = [
    {"number": 172, "title": "Маскирование карт калечит UUID"},
    {"number": 133, "title": "Outbox: dead-letter + алерт"},
    {"number": 125, "title": "Поднять дневной лимит LLM"},
    {"number": 103, "title": "Алерт при ~80% бюджета"},
    {"number": 49, "title": "Гейт продакшена"},
    {"number": 55, "title": "done необратим"},
]


def _batch_body(open_lines: int, done_lines: int = 0) -> str:
    """Тело issue-копилки: `- [ ]` — несделанная строка, `- [x]` — уже внесённая."""
    rows = [f"- [ ] правка {i}" for i in range(open_lines)]
    rows += [f"- [x] внесено {i}" for i in range(done_lines)]
    return "\n".join(["Накопительный issue по правилу 11.", *rows])


def _batch_issue(
    *, body_lines: int = 0, comments: list[tuple[str, int]] | None = None
) -> dict[str, Any]:
    """Копилка #275: тело + комментарии, каждый — (дата ISO, число несделанных строк)."""
    return {
        "createdAt": "2026-08-13T10:00:00Z",
        "body": _batch_body(body_lines),
        "comments": [{"createdAt": ts, "body": _batch_body(n)} for ts, n in (comments or [])],
    }


def _days_ago(days: int) -> str:
    return (datetime.now(UTC) - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _make_pr(number: int, title: str, **overrides: Any) -> dict[str, Any]:
    pr: dict[str, Any] = {
        "number": number,
        "title": title,
        "isDraft": False,
        "reviewDecision": None,
        "createdAt": "2026-08-01T10:00:00Z",
        "closingIssuesReferences": [],
    }
    pr.update(overrides)
    return pr


def _run_queue(
    tmp_path: Path,
    *,
    prs: list[dict[str, Any]] | None = None,
    issues: list[dict[str, Any]] | None = None,
    batch: dict[str, Any] | None = None,
    queue_md: str = QUEUE_FIXTURE,
    gh_script: str = FAKE_GH,
) -> subprocess.CompletedProcess[str]:
    bin_dir = tmp_path / "bin"
    fixtures = tmp_path / "fixtures"
    bin_dir.mkdir()
    fixtures.mkdir()
    gh = bin_dir / "gh"
    gh.write_text(gh_script, encoding="utf-8")
    gh.chmod(0o755)
    (fixtures / "prs.json").write_text(json.dumps(prs or []), encoding="utf-8")
    (fixtures / "issues.json").write_text(
        json.dumps(issues if issues is not None else OPEN_ISSUES), encoding="utf-8"
    )
    (fixtures / "batch.json").write_text(
        json.dumps(batch if batch is not None else _batch_issue()), encoding="utf-8"
    )
    queue_file = tmp_path / "QUEUE.md"
    queue_file.write_text(queue_md, encoding="utf-8")
    env = os.environ.copy()
    env["PATH"] = f"{bin_dir}:{env['PATH']}"
    env["QUEUE_FILE"] = str(queue_file)
    env["GH_FIXTURES"] = str(fixtures)
    return subprocess.run(
        ["bash", str(QUEUE_SCRIPT)],
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )


def test_section_zero_is_printed_from_github(tmp_path: Path) -> None:
    """Раздел 0 — живой список PR из GitHub: смерженный PR в нём остаться не может.

    Прежний ручной раздел 0 говорил «ждёт merge» про уже смерженный PR, а сверка
    молчала (находка Б2/А2). Теперь источник один — `gh pr list`.
    """
    result = _run_queue(
        tmp_path,
        prs=[_make_pr(152, "chore(security): промпт аудита, pip-audit в CI")],
    )
    assert result.returncode == 0, result.stderr
    assert "PR #152" in result.stdout
    assert "ждёт ревью" in result.stdout
    # Смерженных/закрытых PR в выводе нет — их нет в ответе gh pr list --state open.
    assert result.stdout.count("PR #") == 1


def test_closed_issue_claimed_anywhere_is_flagged(tmp_path: Path) -> None:
    """Закрытый issue, оставшийся пунктом очереди, — расхождение в любом разделе.

    Раньше ловилась только верхняя пятёрка раздела 1, и то вне блока расхождений
    (находка Б4): здесь #133 стоит вторым в разделе 1, а #55 — в разделе 3.
    """
    issues = [i for i in OPEN_ISSUES if i["number"] not in (133, 55)]
    result = _run_queue(tmp_path, issues=issues)
    assert result.returncode == 0, result.stderr
    assert "! пункт **#133** закрыт" in result.stdout
    assert "! пункт **#55** закрыт" in result.stdout
    assert "нет — файл и GitHub сходятся" not in result.stdout


def test_open_issue_missing_from_queue_is_flagged(tmp_path: Path) -> None:
    """Открытый issue без единого упоминания в файле — расхождение.

    Майлстоун роли не играет: скрипт его вообще не запрашивает (находка Б3 —
    прежний фильтр по milestone прятал 16 issue из 55).
    """
    issues = [*OPEN_ISSUES, {"number": 999, "title": "Потерянный issue без майлстоуна"}]
    result = _run_queue(tmp_path, issues=issues)
    assert result.returncode == 0, result.stderr
    assert "! issue #999 не вписан ни в один раздел" in result.stdout


def test_prose_mention_in_pr_body_does_not_mark_busy(tmp_path: Path) -> None:
    """«Занято» не срабатывает от номера вне заголовка PR (находка Б1).

    Тело PR скрипт не запрашивает вовсе, поэтому проза («заведены #172…»)
    пометить пункт занятым не может; заголовок без номеров тоже не метит.
    """
    result = _run_queue(
        tmp_path,
        prs=[_make_pr(176, "docs(queue): одна очередь работ", reviewDecision="CHANGES_REQUESTED")],
    )
    assert result.returncode == 0, result.stderr
    assert "занято" not in result.stdout


def test_issue_number_in_pr_title_or_closes_marks_busy(tmp_path: Path) -> None:
    """«Занято» срабатывает от «(#N)» в заголовке PR и от Closes-ссылок."""
    result = _run_queue(
        tmp_path,
        prs=[
            _make_pr(190, "fix(telegram): не калечить UUID маскированием (#172)"),
            _make_pr(191, "feat(outbox): dead-letter", closingIssuesReferences=[{"number": 133}]),
        ],
    )
    assert result.returncode == 0, result.stderr
    assert "#172" in result.stdout and "занято: открытый PR с #172" in result.stdout
    assert "занято: открытый PR с #133" in result.stdout


def test_group_of_three_items_is_parsed_and_busy_checked(tmp_path: Path) -> None:
    """Группа правила 2 длиннее двух номеров разбирается как один пункт.

    Регулярка допускала ровно одно «+ #N», и канон `- **#214 + #217 + #218**`
    молча выпадал и из печати раздела 1, и из проверки закрытых пунктов
    (ревью PR #277, Н-3). «Занято» обязано срабатывать от любого номера группы,
    не только от первого.
    """
    queue_md = QUEUE_FIXTURE.replace(
        "- **#172** — маскирование карт калечит UUID — **Opus 5 `xhigh`**",
        "- **#214 + #217 + #218** — мини-фиксы Telegram одной сессией — **Opus 5 `high`**",
    )
    issues = [
        {"number": 214, "title": "ForceReply selective"},
        {"number": 217, "title": "details в уведомлении"},
        {"number": 218, "title": "заглушка лички сотрудника"},
        *[i for i in OPEN_ISSUES if i["number"] != 172],
    ]
    result = _run_queue(
        tmp_path,
        queue_md=queue_md,
        issues=issues,
        prs=[_make_pr(280, "fix(telegram): details в уведомлении (#217)")],
    )
    assert result.returncode == 0, result.stderr
    assert " 1. #214" in result.stdout  # группа стоит первой, а не выпадает из списка
    assert "занято: открытый PR с #217" in result.stdout
    # Все три номера — пункты очереди: закрытый из них дал бы расхождение.
    assert "нет — файл и GitHub сходятся" in result.stdout


def test_process_batch_is_ripe_by_age(tmp_path: Path) -> None:
    """Копилка правила 11 созревает по возрасту старейшей строки, а не по календарю.

    «Раз в неделю» без подписчика — обязанность без исполнителя (ревью PR #277, Н-2):
    порог считает скрипт, а берёт батч та сессия, которая увидела «СОЗРЕЛ».
    """
    result = _run_queue(tmp_path, batch=_batch_issue(comments=[(_days_ago(9), 2)]))
    assert result.returncode == 0, result.stderr
    assert "СОЗРЕЛ: строк 2" in result.stdout
    assert "старейшей 9 дн." in result.stdout


def test_process_batch_is_ripe_by_line_count(tmp_path: Path) -> None:
    """Десять несделанных строк — созрел, даже если все написаны сегодня."""
    result = _run_queue(
        tmp_path,
        batch=_batch_issue(body_lines=4, comments=[(_days_ago(0), 6)]),
    )
    assert result.returncode == 0, result.stderr
    assert "СОЗРЕЛ: строк 10" in result.stdout


def test_process_batch_counts_only_unchecked_lines(tmp_path: Path) -> None:
    """Внесённые строки (`- [x]`) в порог не идут — иначе батч «созревал» бы навсегда."""
    result = _run_queue(
        tmp_path,
        batch={
            "createdAt": _days_ago(1),
            "body": _batch_body(1, done_lines=20),
            "comments": [],
        },
    )
    assert result.returncode == 0, result.stderr
    assert "не созрел: строк 1 из 10" in result.stdout


def test_process_batch_unreadable_is_loud(tmp_path: Path) -> None:
    """Копилка не прочитана (gh без прав, issue удалён) — громко, но очередь работает.

    Молчание здесь означало бы «батч не созрел», то есть тихую отмену правила 11.
    """
    result = _run_queue(tmp_path, gh_script=NO_BATCH_GH)
    assert result.returncode == 0, result.stderr
    assert "не прочитан" in result.stderr
    assert "Следующее в работу" in result.stdout


def test_draft_pr_is_marked_as_taken_not_reviewable(tmp_path: Path) -> None:
    """Draft-PR — заявка «взято», а не «ревью не начато» (находка Р11)."""
    result = _run_queue(
        tmp_path,
        prs=[_make_pr(192, "wip: спека политик обслуживания (#124)", isDraft=True)],
    )
    assert result.returncode == 0, result.stderr
    assert "черновик — взято другой сессией" in result.stdout


def test_gh_failure_still_prints_the_queue_file(tmp_path: Path) -> None:
    """gh установлен, но падает (токен, офлайн, лимит) — файл всё равно печатается.

    Раньше set -e убивал скрипт до единой строки вывода (находка Р3), хотя
    make queue — первая команда сессии по правилу 10.
    """
    result = _run_queue(tmp_path, gh_script=BROKEN_GH)
    assert result.returncode == 0, result.stderr
    assert "GitHub недоступен" in result.stderr
    assert "## 1. Дорожка «Код»" in result.stdout


def test_unparseable_section_one_is_a_loud_error(tmp_path: Path) -> None:
    """Не разобран раздел 1 — ненулевой код и явное сообщение, не тихий пустой вывод.

    Раньше переименованный заголовок давал пустой список и «нет расхождений»
    (находка Р4) — сессия оставалась без верхнего пункта молча.
    """
    broken = QUEUE_FIXTURE.replace("## 1. Дорожка «Код»", "## 1) Дорожка «Код»")
    result = _run_queue(tmp_path, queue_md=broken)
    assert result.returncode != 0
    assert "Не разобрал раздел 1" in result.stderr
