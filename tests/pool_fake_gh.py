"""Фейковый `gh` для tests/test_pool_merge.py — запускается как программа, не тест.

Состояние PR — $FAKE_GH_DIR/pr-<N>.json, каждый вызов — строкой в calls.jsonl вместе
с GH_CONFIG_DIR (чьим аккаунтом вызван). Отвечает ровно на запросы ops/pool-merge.sh:
поля `--json` выбирают ответ, `--jq` не исполняется — ответ уже в его формате.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

STATE_DIR = Path(os.environ["FAKE_GH_DIR"])


def opt(args: list[str], name: str) -> str | None:
    return args[args.index(name) + 1] if name in args else None


def load(number: str) -> dict[str, Any]:
    state: dict[str, Any] = json.loads((STATE_DIR / f"pr-{number}.json").read_text())
    return state


def save(number: str, state: dict[str, Any]) -> None:
    (STATE_DIR / f"pr-{number}.json").write_text(json.dumps(state))


def pr_view(number: str, fields: str | None) -> None:
    s = load(number)
    if fields == "state,isDraft,reviewDecision,headRefOid":
        print(s["state"], str(s["draft"]).lower(), s["decision"], s["head"])
    elif fields == "state,headRefOid":
        print(s["state"], s["head"])
    elif fields == "headRefOid,statusCheckRollup":
        checks: list[str] = s["checks"]
        status = checks.pop(0) if len(checks) > 1 else checks[0]
        save(number, s)
        print(s["head"], status)
    else:
        sys.exit(f"fake gh: нет ответа на поля {fields}")


def update_branch(number: str) -> None:
    """Как GitHub: голова PR — заранее приготовленный тестом merge-коммит, approve снят."""
    s = load(number)
    if not s.get("update_to"):
        sys.exit("fake gh: update-branch failed")
    origin = (STATE_DIR / "origin").read_text()
    ref = f"refs/pull/{number}/head"
    subprocess.run(["git", "--git-dir", origin, "update-ref", ref, s["update_to"]], check=True)
    s.update(head=s["update_to"], update_to=None, decision="REVIEW_REQUIRED")
    save(number, s)


def merge(number: str, match_head: str | None) -> None:
    s = load(number)
    if match_head != s["head"]:
        sys.exit("fake gh: head mismatch")
    s["state"] = "MERGED"
    save(number, s)


def main(args: list[str]) -> None:
    with (STATE_DIR / "calls.jsonl").open("a") as log:
        call = {"args": args, "config": os.environ.get("GH_CONFIG_DIR", "")}
        log.write(json.dumps(call) + "\n")
    if args[:2] == ["repo", "view"]:
        print("owner/repo")
    elif args[:2] == ["pr", "view"]:
        pr_view(args[2], opt(args, "--json"))
    elif args[:2] == ["pr", "update-branch"]:
        update_branch(args[2])
    elif args[:2] == ["pr", "merge"]:
        merge(args[2], opt(args, "--match-head-commit"))
    elif args[:2] == ["pr", "comment"]:
        pass
    elif args[:1] == ["api"]:
        if (STATE_DIR / "api_fails").exists():
            sys.exit("fake gh: HTTP 401")
    else:
        sys.exit(f"fake gh: неизвестный вызов {args}")


if __name__ == "__main__":
    main(sys.argv[1:])
