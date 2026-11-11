#!/usr/bin/env python3
"""Щит публичности (#1023, слово основателя 11.11.2026): закрытое не уходит в публичный
репозиторий и в тексты GitHub. Одна программа, две двери.

  pre-push  хук git: у уходящих коммитов проверяются добавленные строки, сообщения и имена
            файлов. Коммиты называет сам git — shell не разбирается.
  hook      хук PreToolUse Claude Code (.claude/settings.json) на команды gh и git: текст
            команды и файлы, названные в ней; push мимо хука (--no-verify) — отказ.
            Заодно ставит заглушку pre-push, если её нет.
  install   поставить заглушку pre-push в .git/hooks (она общая у всех копий репозитория).
  scan      проверить руками: файлы, stdin («-») или коммиты (--range <ревизии git log>).

Что ищется: общие шаблоны ниже (ключи, пароли в адресах, адреса серверов, ИИН человека,
личные ящики, имена файлов) и закрытый список — файл вне репозитория,
ops/public-guard-list.local в основной папке: строка «класс | образец», образец — буквально,
`tel:` — телефон в любой записи, `re:` — регулярное выражение; плюс значения секретов из
локального .env. Смысл щит не понимает — правило выше щита: CLAUDE.md, «Правила работы».
Не видит он и текст, который команда сама же порождает (`скрипт > body.md && gh … -F body.md`):
хук смотрит команду до её запуска.

Находка — отказ с классом и местом. Ошибка самого щита — пропуск с предупреждением: щит не
должен останавливать работу (как ops/pool-copy-guard.sh). Только стандартная библиотека и
Python 3.9+ — хук запускает системный python3. Тесты — tests/test_public_guard*.py.
"""

from __future__ import annotations

import bisect
import ipaddress
import json
import os
import re
import shlex
import subprocess
import sys
import unicodedata
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Callable, Iterator, Optional

PRIVATE_REPOS = ("dikatapulta/HospitalityAIPlatform-security",)  # туда закрытое писать можно
LIST_NAME = "ops/public-guard-list.local"
STUB_MARK = "public-guard-stub v1"
MAX_FILE_BYTES = 20_000_000  # страница сайта с вшитыми картинками — 3 МБ, и её читать надо
# Ложные срабатывания общих шаблонов: точная строка — и почему она не закрытое.
ALLOWED: tuple[str, ...] = (
    "1.1.1.1", "1.0.0.1", "8.8.8.8", "8.8.4.4", "9.9.9.9", "1.2.3.4",  # общеизвестные адреса-примеры
)  # fmt: skip
FAKE_PASSWORDS = frozenset(
    "pass password passwd pwd secret user test example changeme xxx hospitality postgres redis".split()
)
FAKE_HOST = re.compile(r"^(?:localhost|127\.0\.0\.1|db|redis|postgres|host|[\w-]*fake[\w-]*|.*\.(?:example|invalid|test|local)(?:\.\w+)?)$")

Check = Optional[Callable[["re.Match[str]", str], bool]]
VERSION_BEFORE = re.compile(  # «версия 12.4.118.26», «Opera (у вас 7.3.41.9», «v1.2.3.4»
    r"(?:\bv|(?:version|верси\w*|opera|chrome|chromium|firefox|safari|edg\w*|windows|android"
    r"|ios|macos|python|node|postgres\w*|redis|nginx|ubuntu|debian)[^\n]{0,16})$",
    re.IGNORECASE,
)


def _server_address(match: re.Match[str], text: str) -> bool:
    value = match.group(0)
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        return False
    if not address.is_global or value.endswith(".0.0.0"):  # 130.0.0.0 — версия браузера
        return False
    return not VERSION_BEFORE.search(text[max(0, match.start() - 24) : match.start()])


def _real_credentials(match: re.Match[str], _text: str) -> bool:
    """Пароль в адресе — настоящий, а не заглушка теста: user:pass@127.0.0.1, x:y@fake-host."""
    password, host = match.group("password"), match.group("host").lower()
    fake = password.lower() in FAKE_PASSWORDS or password == match.group("user")
    return not fake and not FAKE_HOST.match(host)


def _person_iin(match: re.Match[str], _text: str) -> bool:
    """ИИН человека: ГГММДД, разряд века и пола 1–6, контрольный разряд. У БИН день — 40+."""
    value = match.group(0)
    if not (1 <= int(value[2:4]) <= 12 and 1 <= int(value[4:6]) <= 31 and "1" <= value[6] <= "6"):
        return False
    digits = [int(char) for char in value]
    check = sum(d * w for d, w in zip(digits, range(1, 12))) % 11
    if check == 10:
        check = sum(d * w for d, w in zip(digits, (3, 4, 5, 6, 7, 8, 9, 10, 11, 1, 2))) % 11
    return check == digits[11]


GENERIC: tuple[tuple[str, re.Pattern[str], Check], ...] = (
    ("секрет: закрытый ключ", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"), None),
    ("секрет: ключ age", re.compile(r"AGE-SECRET-KEY-1[0-9A-Z]{20,}"), None),
    (
        "секрет: токен бота Telegram",
        re.compile(r"(?<![0-9A-Za-z])\d{8,10}:[A-Za-z0-9_-]{35}(?![A-Za-z0-9_-])"),
        None,
    ),
    (
        "секрет: ключ API",
        re.compile(r"(?<![0-9A-Za-z-])sk-(?:ant-|proj-)?[A-Za-z0-9_-]{24,}"),
        None,
    ),
    (
        "секрет: токен GitHub",
        re.compile(r"(?<![0-9A-Za-z])(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{30,})"),
        None,
    ),
    ("секрет: токен туннеля", re.compile(r"(?<![0-9A-Za-z])eyJhIjoi[A-Za-z0-9+/=_-]{40,}"), None),
    ("секрет: DSN Sentry", re.compile(r"https://[0-9a-f]{32}@[a-z0-9.-]*sentry\.io/\d+"), None),
    (
        "секрет: пароль в адресе",
        re.compile(
            r"[a-z][a-z0-9+.-]*://(?P<user>[^/\s:@'\"<>$]*):(?P<password>[^/\s:@'\"<>${}*]{3,})"
            r"@(?P<host>[\w.-]+)"
        ),
        _real_credentials,
    ),
    (
        "адрес сервера",
        re.compile(r"(?<![\d.])(?:\d{1,3}\.){3}\d{1,3}(?![\d.])"),
        _server_address,
    ),
    ("ИИН человека", re.compile(r"(?<![0-9A-Za-z])\d{12}(?![0-9A-Za-z])"), _person_iin),
    (
        "личный почтовый ящик",
        re.compile(
            r"[A-Za-z0-9._%+-]+@(?:gmail|googlemail|mail|bk|inbox|list|internet|yandex|ya"
            r"|icloud|me|outlook|hotmail|live|yahoo|proton|protonmail)\.(?:com|ru|kz|me)\b",
            re.IGNORECASE,
        ),
        None,
    ),
)
FORBIDDEN_NAME = re.compile(
    r"(?:^|/)(?:\.env(?:\.[^/]*)?|[^/]*\.local|id_(?:rsa|dsa|ecdsa|ed25519)[^/]*"
    r"|[^/]*\.(?:pem|p12|pfx|age|dump|pgdump))$"
)
SECRET_KEY_NAME = re.compile(r"KEY|TOKEN|SECRET|PASSWORD|PASSWD|DSN|CREDENTIAL", re.IGNORECASE)
# Команда узнаётся с запасом (и /usr/bin/gh, и gh внутри цепочки): лишняя проверка безвредна,
# пропущенная — утечка. Сужает проверку только PRIVATE_TARGET — узкой канонической формой.
GH_CALL = re.compile(r"(?<![\w.-])gh\s+[a-z]")
GIT_TEXT = re.compile(r"(?<![\w.-])git\b[^\n;&|]*\s(?:commit|tag|notes)\b")
GIT_PUSH = re.compile(r"(?<![\w.-])git\b[^\n;&|]*\spush\b")
PUSH_BYPASS = re.compile(r"--no-verify|hooksPath", re.IGNORECASE)
PRIVATE_TARGET = re.compile(
    r"(?:-R|--repo)[=\s]+[\"']?(?:https://github\.com/)?(?:%s)(?![\w-])"
    % "|".join(re.escape(repo) for repo in PRIVATE_REPOS)
)
# Файл с текстом для GitHub назван переменной: --body-file $F, -F "$F", поле=@$F.
BODY_FROM_VARIABLE = re.compile(
    r"(?:(?:--body-file|--notes-file|--input)[=\s]+[\"']?[^\s\"']*|\s-F\s+[\"']?[^\s\"'=]*|=@[^\s\"']*)[$`]"
)
MESSAGE_FILE = re.compile(r"(?:\s-F|--file)[=\s]+[\"']?([^\s\"']+)")
OUTPUT_TO = re.compile(r"(?:\d|&)?>>?$")  # отдельное слово «>», «>>», «2>»: дальше — куда писать
ADVICE = (
    "Репозиторий и всё, что пишется на GitHub, публичны, и историю не стереть. Убери это "
    "из текста: вместо названия — «пилотный отель», вместо адреса — <IP>. Чему место вне "
    "репозитория и где оно живёт — CLAUDE.md, «Правила работы», пункт «Публичный "
    "репозиторий». Срабатывание общего шаблона ложное — впиши исключение в ALLOWED в "
    "ops/public_guard.py тем же PR; закрытый список ради публикации не правится."
)


@dataclass(frozen=True)
class Finding:
    label: str
    where: str
    excerpt: str

    def line(self) -> str:
        return f"  · {self.where} — {self.label}: «{self.excerpt}»"


@dataclass
class Rules:
    private: list[tuple[str, re.Pattern[str]]] = field(default_factory=list)
    secrets: list[tuple[str, str]] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


def git(args: list[str], cwd: Optional[str] = None) -> Optional[str]:
    try:
        done = subprocess.run(
            ["git", "-c", "core.quotePath=false", *args],
            cwd=cwd,
            capture_output=True,
            timeout=120,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return done.stdout.decode("utf-8", "replace") if done.returncode == 0 else None


def common_dir(cwd: Optional[str]) -> Optional[str]:
    out = git(["rev-parse", "--path-format=absolute", "--git-common-dir"], cwd)
    return out.strip() if out else None


@lru_cache(maxsize=None)
def main_dir() -> Optional[Path]:
    """Основная папка проекта: над общим .git репозитория, в котором лежит сам скрипт."""
    common = common_dir(str(Path(__file__).resolve().parent))
    return Path(common).parent if common and common.endswith("/.git") else None


def normalize(text: str) -> str:
    """Один вид для сравнения: «й» одной буквой (NFC), без регистра, «ё» как «е», один пробел."""
    folded = unicodedata.normalize("NFC", text).casefold().replace("ё", "е")
    return re.sub(r"\s+", " ", folded).strip()


def _env_values(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        name, sep, value = raw.strip().partition("=")
        if sep and not name.startswith("#"):
            values[name.strip()] = value.strip().strip("\"'")
    return values


def load_rules() -> Rules:
    rules = Rules()
    main = main_dir()
    listed = os.environ.get("PUBLIC_GUARD_LIST") or (str(main / LIST_NAME) if main else "")
    if not listed or not Path(listed).is_file():
        rules.notes.append(f"закрытого списка нет ({listed or LIST_NAME}) — только общие шаблоны")
    else:
        for number, raw in enumerate(Path(listed).read_text(encoding="utf-8-sig").splitlines(), 1):
            if not raw.strip() or raw.lstrip().startswith("#"):
                continue
            label, sep, sample = raw.partition("|")
            label, sample = (label.strip() or "закрытое", sample.strip()) if sep else ("закрытое", raw.strip())
            try:
                rules.private.append((label, _private_pattern(sample)))
            except (re.error, ValueError):
                rules.notes.append(f"закрытый список, строка {number}: образец не разобран — пропущена")
    env_file = os.environ.get("PUBLIC_GUARD_ENV") or (str(main / ".env") if main else "")
    if env_file and Path(env_file).is_file():
        example = Path(__file__).resolve().parent.parent / ".env.example"
        public = set(_env_values(example).values()) if example.is_file() else set()
        for name, value in _env_values(Path(env_file)).items():
            is_path = value[:1] in ("~", "/", ".")  # путь к файлу ключа — не сам ключ
            if len(value) >= 16 and not is_path and value not in public and SECRET_KEY_NAME.search(name):
                rules.secrets.append((f"секрет из .env ({name})", value))
    return rules


def _private_pattern(sample: str) -> re.Pattern[str]:
    if sample.startswith("re:"):
        source = unicodedata.normalize("NFC", sample[3:].strip()).replace("ё", "е")
        return re.compile(source, re.IGNORECASE)
    if sample.startswith("tel:"):
        digits = re.sub(r"\D", "", sample)[-10:]
        if len(digits) < 7:
            raise ValueError(sample)
        return re.compile(r"(?<!\d)(?:\+?7|8)?[\s().-]*" + r"[\s().-]*".join(digits) + r"(?!\d)")
    if not normalize(sample):
        raise ValueError(sample)
    return re.compile(re.escape(normalize(sample)))


def scan_text(text: str, where: str, rules: Rules, first_line: int = 1) -> list[Finding]:
    """Находки в тексте. Закрытый список ищется по нормализованному тексту, склеенному через
    пробел: образец из двух слов находится и на переносе строки."""
    found: list[Finding] = []
    seen: set[tuple[str, int]] = set()

    def add(label: str, line: int, value: str) -> None:
        if (label, line) not in seen and value not in ALLOWED:
            seen.add((label, line))
            shown = value[:6] + "…" if label.startswith("секрет") else value[:60]
            found.append(Finding(label, f"{where}:{first_line + line}", shown))

    for label, pattern, check in GENERIC:
        for match in pattern.finditer(text):
            if check is None or check(match, text):
                add(label, text.count("\n", 0, match.start()), match.group(0))
    for label, value in rules.secrets:
        at = text.find(value)
        if at >= 0:
            add(label, text.count("\n", 0, at), value)
    if rules.private:
        lines = [normalize(line) for line in text.split("\n")]
        starts, offset = [], 0
        for line in lines:
            starts.append(offset)
            offset += len(line) + 1
        joined = " ".join(lines)
        for label, pattern in rules.private:
            for match in pattern.finditer(joined):
                add(label, bisect.bisect_right(starts, match.start()) - 1, match.group(0))
    return found


def scan_file(path: Path, rules: Rules) -> list[Finding]:
    try:
        if path.stat().st_size > MAX_FILE_BYTES:
            return []
        return scan_text(path.read_text(encoding="utf-8", errors="replace"), str(path), rules)
    except OSError:
        return []


def added_blocks(patch: str) -> Iterator[tuple[str, str, int, str]]:
    """Из вывода `git log -p --cc --format=%x1e%H`: (коммит, файл, первая строка, текст) для
    каждой пачки подряд добавленных строк. У слияния добавленное — «+» во всех колонках."""
    commit, path, columns, line_no = "", "", 1, 0
    start, block = 0, []
    for raw in [*patch.split("\n"), "\x1e"]:
        in_hunk = bool(path) and line_no > 0  # заголовки файла бывают только до первого @@
        added = in_hunk and raw[:columns] == "+" * columns
        if not added and block:
            yield commit, path, start, "\n".join(block)
            block = []
        if raw.startswith("\x1e"):
            commit, path, line_no = raw[1:].strip(), "", 0
        elif raw.startswith("diff "):
            path, line_no = "", 0
        elif not in_hunk and raw.startswith("+++ "):
            path = raw[4:].strip().removeprefix("b/")
        elif raw.startswith("@@"):
            columns = len(raw) - len(raw.lstrip("@")) - 1
            new_range = re.search(r"\+(\d+)", raw)
            line_no = int(new_range.group(1)) if new_range else 0
        elif added:
            if not block:
                start = line_no
            block.append(raw[columns:])
            line_no += 1
        elif in_hunk and "-" not in raw[:columns] and not raw.startswith("\\"):
            line_no += 1


def scan_revisions(revisions: list[str], rules: Rules) -> list[Finding]:
    found: list[Finding] = []
    for entry in (git(["log", "--format=%H%x1f%B%x1e", *revisions]) or "").split("\x1e"):
        commit, _, message = entry.strip().partition("\x1f")
        if commit:
            found += scan_text(message, f"{commit[:8]} сообщение коммита", rules)
    names = git(["log", "--diff-filter=ACMR", "--name-only", "--format=", "-z", *revisions]) or ""
    for name in {part.strip() for part in names.split("\0")}:
        if name and FORBIDDEN_NAME.search(name) and not name.endswith(".example"):
            found.append(Finding("файл, которому не место в репозитории", name, name))
    patch = git(
        ["log", "-p", "--cc", "--unified=0", "--no-color", "--no-ext-diff", "--no-textconv",
         "--format=%x1e%H", *revisions]
    )  # fmt: skip
    for commit, path, start, text in added_blocks(patch or ""):
        found += scan_text(text, f"{commit[:8]} {path}", rules, first_line=start)
    return found


def cmd_pre_push() -> int:
    rules = load_rules()
    found: list[Finding] = []
    for line in sys.stdin.read().splitlines():
        parts = line.split()
        if len(parts) != 4 or not parts[1].strip("0"):
            continue  # удаление ветки проверять нечего
        revisions = [parts[1], "--not", "--remotes"]
        if parts[3].strip("0") and git(["cat-file", "-e", parts[3] + "^{commit}"]) is not None:
            revisions.append(parts[3])
        found += scan_revisions(revisions, rules)
    for note in rules.notes:
        print(f"щит публичности: {note}", file=sys.stderr)
    if not found:
        return 0
    print(f"Щит публичности: push остановлен — в уходящих коммитах закрытое ({len(found)}):", file=sys.stderr)
    print("\n".join(item.line() for item in found[:20]), file=sys.stderr)
    print(
        ADVICE + " Из коммитов убирается правкой и `git commit --amend`; коммитов несколько — "
        "ветку пересобрать от origin/main.",
        file=sys.stderr,
    )
    return 1


def stub_text(main: Path) -> str:
    return f"""#!/bin/sh
# {STUB_MARK} — щит публичности (ops/public_guard.py, #1023). Ставит и обновляет
# `python3 ops/public_guard.py install` и дверь hook; руками не править.
command -v python3 >/dev/null 2>&1 || {{ echo "щит публичности: нет python3 — push не проверен" >&2; exit 0; }}
top="$(git rev-parse --show-toplevel 2>/dev/null)"
for guard in "$top/ops/public_guard.py" "{main}/ops/public_guard.py"; do
    if [ -f "$guard" ]; then exec python3 "$guard" pre-push "$@"; fi
done
echo "щит публичности: ops/public_guard.py не найден — push не проверен" >&2
exit 0
"""


def install_stub(cwd: Optional[str]) -> str:
    """Ставит заглушку в хуки репозитория самого скрипта; чужой pre-push не трогает."""
    main, common = main_dir(), common_dir(cwd)
    if main is None or common != str(main / ".git"):
        return "не репозиторий проекта — заглушка не ставится"
    hooks = git(["rev-parse", "--path-format=absolute", "--git-path", "hooks"], cwd)
    if not hooks:
        return "папка хуков не найдена"
    target, text = Path(hooks.strip()) / "pre-push", stub_text(main)
    if target.exists():
        current = target.read_text(encoding="utf-8", errors="replace")
        if "public-guard-stub" not in current:
            return f"в {target} чужой хук — заглушка не поставлена"
        if current == text:
            return f"заглушка на месте: {target}"
    target.parent.mkdir(parents=True, exist_ok=True)
    draft = target.with_name(f".pre-push.{os.getpid()}.tmp")  # git не должен застать полфайла
    draft.write_text(text, encoding="utf-8")
    draft.chmod(0o755)
    os.replace(draft, target)
    return f"заглушка поставлена: {target}"


def deny(reason: str) -> int:
    output = {"hookEventName": "PreToolUse", "permissionDecision": "deny", "permissionDecisionReason": reason}
    print(json.dumps({"hookSpecificOutput": output}, ensure_ascii=False))
    return 0


def named_files(command: str, cwd: str) -> Iterator[Path]:
    try:
        tokens = shlex.split(command, posix=True)
    except ValueError:
        tokens = command.split()
    seen: set[Path] = set()
    previous = ""
    for token in tokens:
        # `gh api … > выгрузка.json`: файл, куда команда пишет, — не то, что уходит на GitHub.
        written = bool(OUTPUT_TO.match(previous)) or bool(re.match(r"(?:\d|&)?>>?[^&>]", token))
        previous = token
        if written:
            continue
        for part in {token, token.rpartition("=")[2]}:
            name = part.strip("()\"';,<>").lstrip("@")
            if not name or len(name) > 1024:
                continue
            path = Path(os.path.expanduser(name))
            path = path if path.is_absolute() else Path(cwd) / path
            if path not in seen and path.is_file():
                seen.add(path)
                yield path


def cmd_hook() -> int:
    data = json.load(sys.stdin)
    if data.get("tool_name") != "Bash":
        return 0
    command = str(data.get("tool_input", {}).get("command", ""))
    cwd = str(data.get("cwd") or os.getcwd())
    try:
        install_stub(cwd)
    except OSError:
        pass  # не поставилась заглушка — текст команды всё равно проверяется
    gh_calls, git_text = len(GH_CALL.findall(command)), bool(GIT_TEXT.search(command))
    if GIT_PUSH.search(command) and PUSH_BYPASS.search(command):
        return deny(
            "Щит публичности: push мимо проверки (--no-verify, hooksPath) окну запрещён — так "
            "закрытое уходит в публичный репозиторий. Щит ошибся — покажи основателю его вывод."
        )
    if not gh_calls and not git_text:
        return 0
    if gh_calls == 1 and not git_text and PRIVATE_TARGET.search(command):
        return 0  # приватный репозиторий: закрытому там и место
    if gh_calls and BODY_FROM_VARIABLE.search(command):
        return deny(
            "Щит публичности: файл с текстом для GitHub назван переменной — проверить его нельзя. "
            "Назови путь буквально (--body-file /путь/к/файлу)."
        )
    rules = load_rules()
    found = scan_text(command, "текст команды", rules)
    files = named_files(command, cwd) if gh_calls else (
        Path(os.path.expanduser(m)) if os.path.isabs(os.path.expanduser(m)) else Path(cwd) / m
        for m in MESSAGE_FILE.findall(command)
    )
    for path in files:
        found += scan_file(path, rules)
    if not found:
        return 0
    to_private = (
        f" Пишешь в приватный репозиторий безопасности — назови его флагом: -R {PRIVATE_REPOS[0]}."
        if gh_calls
        else ""
    )
    return deny(
        f"Щит публичности: в том, что уходит на GitHub, закрытое ({len(found)}):\n"
        + "\n".join(item.line() for item in found[:12])
        + "\n"
        + ADVICE
        + to_private
    )


def cmd_scan(args: list[str]) -> int:
    rules = load_rules()
    if args[:1] == ["--range"]:
        found = scan_revisions(args[1:], rules)
    else:
        found = []
        for name in args or ["-"]:
            found += scan_text(sys.stdin.read(), "stdin", rules) if name == "-" else scan_file(Path(name), rules)
    for note in rules.notes:
        print(f"щит публичности: {note}", file=sys.stderr)
    print("\n".join(item.line() for item in found) if found else "щит публичности: чисто")
    return 1 if found else 0


def main(argv: list[str]) -> int:
    mode = argv[1] if len(argv) > 1 else ""
    try:
        if mode == "hook":
            return cmd_hook()
        if mode == "pre-push":
            return cmd_pre_push()
        if mode == "install":
            print(f"щит публичности: {install_stub(None)}")
            return 0
        if mode == "scan":
            return cmd_scan(argv[2:])
    except Exception as error:  # noqa: BLE001 — ошибка щита работу не останавливает
        print(f"щит публичности: ошибка самого щита, проверка пропущена — {error!r}", file=sys.stderr)
        return 0
    print(__doc__, file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
