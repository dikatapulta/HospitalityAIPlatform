"""Щит публичности ops/public_guard.py (#1023): правила поиска и дверь hook.

Вход хука PreToolUse подаётся руками, как у tests/test_pool_copy_guard.py; настоящий вызов
самим Claude Code проверяет живая проба (тело PR). Дверь git — tests/test_public_guard_push.py.

Образцы, похожие на ключ, адрес или ИИН, собираются на ходу: записанные буквально, они
остановили бы push самого этого файла.
"""

from __future__ import annotations

import json
import subprocess
import sys
import unicodedata
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
GUARD = REPO_ROOT / "ops" / "public_guard.py"
PRIVATE_REPO = "dikatapulta/HospitalityAIPlatform-security"

HOTEL = "Ромашка-Плаза"
LISTED = f"""# закрытый список теста
отель | {HOTEL}
отель | tel:+7 700 111 22 33
основатель | re:должност\\w*\\s+по\\s+ромашкам
сломано | re:(незакрытая
"""


def join(*parts: str, sep: str = "") -> str:
    return sep.join(parts)


GLOBAL_IP = join("93", "184", "216", "34", sep=".")
SK_KEY = join("sk-", "ant-", "a1B2" * 8)
BOT_TOKEN = join("1234567890", ":", "A" * 35)
PEM_HEADER = join("-----BEGIN ", "OPENSSH PRIVATE", " KEY-----")
MAILBOX = join("ivan.petrov", "@", "gmail", ".com")


def person_iin(base: str = "90010135012") -> str:
    """ИИН с верным контрольным разрядом: 11 разрядов основы и вычисленный двенадцатый."""
    digits = [int(char) for char in base]
    check = sum(d * w for d, w in zip(digits, range(1, 12), strict=True)) % 11
    if check == 10:
        check = (
            sum(d * w for d, w in zip(digits, (3, 4, 5, 6, 7, 8, 9, 10, 11, 1, 2), strict=True))
            % 11
        )
    assert check < 10, "основа без контрольного разряда — возьми другую"
    return base + str(check)


@pytest.fixture
def env(tmp_path: Path) -> dict[str, str]:
    listed = tmp_path / "closed.local"
    listed.write_text(LISTED, encoding="utf-8")
    return {
        "PATH": "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin",
        "PUBLIC_GUARD_LIST": str(listed),
        "PUBLIC_GUARD_ENV": str(tmp_path / "no-env"),
    }


def guard(args: list[str], stdin: str, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(GUARD), *args],
        input=stdin,
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )


def scan(text: str, env: dict[str, str]) -> str:
    """Вывод `scan -`: «чисто» или строки находок."""
    return guard(["scan", "-"], text, env).stdout


def hook(command: str, env: dict[str, str], cwd: Path, tool: str = "Bash") -> str:
    """Причина отказа хука или пустая строка, если команда пропущена."""
    payload = {
        "hook_event_name": "PreToolUse",
        "tool_name": tool,
        "cwd": str(cwd),
        "tool_input": {"command": command},
    }
    result = guard(["hook"], json.dumps(payload), env)
    assert result.returncode == 0, result.stderr
    if not result.stdout:
        return ""
    output = json.loads(result.stdout)["hookSpecificOutput"]
    assert output["permissionDecision"] == "deny"
    return str(output["permissionDecisionReason"])


# --- закрытый список -------------------------------------------------------------------


def test_listed_sample_is_found_regardless_of_case_and_spacing(env: dict[str, str]) -> None:
    assert "отель" in scan("Заселение в  РОМАШКА-ПЛАЗА  идёт", env)
    assert "чисто" in scan("Заселение в пилотный отель идёт", env)


def test_two_word_sample_is_found_across_a_line_break(env: dict[str, str]) -> None:
    found = scan("первая строка\nновая должность\nпо ромашкам — с декабря", env)
    assert "основатель" in found
    assert "stdin:2" in found  # строка, где образец начался


def test_phone_is_found_in_any_spelling(env: dict[str, str]) -> None:
    for spelling in ("+7 700 111 22 33", "8 (700) 111-22-33", "87001112233", "7001112233"):
        assert "отель" in scan(f"звонить {spelling}", env), spelling
    assert "чисто" in scan("звонить +7 700 111 22 34", env)


def test_cyrillic_is_matched_in_either_unicode_form(env: dict[str, str]) -> None:
    Path(env["PUBLIC_GUARD_LIST"]).write_text("люди | Зелёный Край\n", encoding="utf-8")
    decomposed = unicodedata.normalize("NFD", "встреча в зеленый край")  # «й» — буква и значок
    assert decomposed != "встреча в зеленый край"
    assert "люди" in scan(decomposed, env)


def test_list_saved_with_a_byte_order_mark_keeps_its_first_line(env: dict[str, str]) -> None:
    Path(env["PUBLIC_GUARD_LIST"]).write_text(f"﻿отель | {HOTEL}\n", encoding="utf-8")
    assert "— отель:" in scan(HOTEL, env)


def test_broken_line_of_the_list_is_skipped_and_named(env: dict[str, str]) -> None:
    result = guard(["scan", "-"], HOTEL, env)
    assert result.returncode == 1  # остальные строки списка работают
    assert "строка 5" in result.stderr


def test_missing_list_leaves_generic_patterns_working(env: dict[str, str], tmp_path: Path) -> None:
    env["PUBLIC_GUARD_LIST"] = str(tmp_path / "absent.local")
    result = guard(["scan", "-"], f"{HOTEL} и ключ {SK_KEY}", env)
    assert "закрытого списка нет" in result.stderr
    assert "секрет: ключ API" in result.stdout
    assert "отель" not in result.stdout


def test_secret_value_from_local_env_file_is_found(env: dict[str, str], tmp_path: Path) -> None:
    secret, plain = "q7" * 12, "z9" * 12
    env_file = tmp_path / "dot-env"
    env_file.write_text(
        f"WEBHOOK_SECRET={secret}\nSTAGING_SSH_KEY=~/.ssh/some-long-key-name\nLLM_MODEL={plain}\n"
    )
    env["PUBLIC_GUARD_ENV"] = str(env_file)
    found = scan(f"секрет {secret}, путь ~/.ssh/some-long-key-name, модель {plain}", env)
    assert "секрет из .env (WEBHOOK_SECRET)" in found
    assert secret not in found  # значение секрета в отчёт не печатается
    assert "STAGING_SSH_KEY" not in found  # путь к ключу — не ключ
    assert "LLM_MODEL" not in found  # имя переменной не про секрет


# --- общие шаблоны ---------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "label"),
    [
        (SK_KEY, "секрет: ключ API"),
        (BOT_TOKEN, "секрет: токен бота Telegram"),
        (PEM_HEADER, "секрет: закрытый ключ"),
        (
            join("postgresql://app:", "Zx81-kappa", "@db.internal.net/app"),
            "секрет: пароль в адресе",
        ),
        (join("ssh deploy@", GLOBAL_IP), "адрес сервера"),
        (join("ИИН ", person_iin()), "ИИН человека"),
        (join("писать на ", MAILBOX), "личный почтовый ящик"),
    ],
)
def test_generic_pattern_is_found(text: str, label: str, env: dict[str, str]) -> None:
    assert label in scan(text, env)


@pytest.mark.parametrize(
    "text",
    [
        "postgresql://user:pass@127.0.0.1:1/db",  # заглушка теста
        "postgresql://app:secret-enough@fake-host:5432/db",
        "redis://:${REDIS_PASSWORD}@redis:6379/0",  # подстановка, а не пароль
        "слушает 10.0.0.5, 192.168.1.10 и 203.0.113.7",  # частные и документационные сети
        join("версия ", "12.4.118.26"),  # четыре числа версии, а не адрес
        join("Chrome/", "130.0.0.0", " Safari"),
        join("Opera (у вас ", "7.3.41.9", ", on-premise)"),
        join("DNS ", "8.8.8.8"),  # общеизвестный адрес-пример
        join("БИН банка ", "940140000385"),  # юрлицо: «день» — 40
        join(
            "номер ", person_iin()[:-1], str((int(person_iin()[-1]) + 1) % 10)
        ),  # разряд не сошёлся
        "manager@hotel.kz и anna@example.com",
    ],
)
def test_lookalike_is_not_a_finding(text: str, env: dict[str, str]) -> None:
    assert "чисто" in scan(text, env), text


# --- дверь hook: команды gh ------------------------------------------------------------


def test_clean_gh_command_passes(env: dict[str, str], tmp_path: Path) -> None:
    assert hook('gh pr comment 12 --body "Ревью: блокеров нет"', env, tmp_path) == ""


def test_gh_command_with_listed_sample_in_text_is_denied(
    env: dict[str, str], tmp_path: Path
) -> None:
    reason = hook(
        f'gh issue create --title "Заселение" --body "Отель {HOTEL} просит"', env, tmp_path
    )
    assert "отель" in reason
    assert "текст команды" in reason


def test_heredoc_body_is_checked(env: dict[str, str], tmp_path: Path) -> None:
    command = "gh pr comment 7 --body-file - <<'EOF'\nПередача\nзвонить 8 700 111 22 33\nEOF"
    assert "текст команды:3" in hook(command, env, tmp_path)


def test_body_file_named_in_command_is_read(env: dict[str, str], tmp_path: Path) -> None:
    body = tmp_path / "body.md"
    body.write_text(f"Итог\n\nВ {HOTEL} всё готово\n", encoding="utf-8")
    for command in (
        f"gh pr create --draft --title x --body-file {body}",
        f"gh pr comment 7 -F {body}",
        f"gh api repos/o/r/issues/1/comments -F body=@{body}",
        f"cat {body} | gh pr comment 7 -F -",
        "gh pr comment 7 --body-file body.md",  # путь от папки сессии
    ):
        assert f"{body}:3" in hook(command, env, tmp_path), command
    body.write_text("Итог\n\nВ пилотном отеле всё готово\n", encoding="utf-8")
    assert hook(f"gh pr comment 7 -F {body}", env, tmp_path) == ""


def test_file_the_command_writes_to_is_not_read(env: dict[str, str], tmp_path: Path) -> None:
    dump = tmp_path / "dump.json"
    dump.write_text(f'{{"body": "{HOTEL}"}}', encoding="utf-8")
    for command in (
        f"gh api repos/o/r/issues > {dump}",
        f"gh api repos/o/r/issues >{dump}",
        f"gh api repos/o/r/issues 2> {dump}",
        f"gh api repos/o/r/issues >> {dump} 2>&1",
    ):
        assert hook(command, env, tmp_path) == "", command
    assert f"{dump}:1" in hook(f"gh pr comment 7 -F {dump} > /dev/null", env, tmp_path)
    assert f"{dump}:1" in hook(f"gh pr comment 7 -F - < {dump}", env, tmp_path)


def test_body_file_behind_a_variable_is_denied_as_uncheckable(
    env: dict[str, str], tmp_path: Path
) -> None:
    for command in (
        'gh pr comment 7 --body-file "$BODY"',
        "gh pr comment 7 -F $BODY",
        "gh api repos/o/r/issues -F body=@$BODY",
    ):
        assert "переменной" in hook(command, env, tmp_path), command
    assert hook("gh api repos/o/r/issues -F per_page=$N", env, tmp_path) == ""  # поле, не файл


def test_private_repository_takes_closed_text(env: dict[str, str], tmp_path: Path) -> None:
    body = f'--body "В {HOTEL} дыра"'
    assert hook(f"gh issue create -R {PRIVATE_REPO} --title sec {body}", env, tmp_path) == ""
    assert hook(f"gh issue create --repo={PRIVATE_REPO} --title sec {body}", env, tmp_path) == ""
    # Ссылка на приватный репозиторий в публичном тексте проверку не снимает.
    assert hook(
        f'gh issue create --title "sec#1" --body "см. {PRIVATE_REPO}#1, {HOTEL}"', env, tmp_path
    )
    # Вторая команда gh в той же строке может писать уже в публичный.
    assert hook(f"gh issue view 1 -R {PRIVATE_REPO} && gh pr comment 7 {body}", env, tmp_path)
    assert hook(f"gh issue create -R {PRIVATE_REPO}-mirror --title sec {body}", env, tmp_path)


def test_gh_is_recognised_behind_a_path_and_an_assignment(
    env: dict[str, str], tmp_path: Path
) -> None:
    body = f'--body "{HOTEL}"'
    for command in (
        f"/opt/homebrew/bin/gh pr comment 7 {body}",
        f"GH_CONFIG_DIR=~/.config/gh-reviewer gh pr review 7 --approve {body}",
        f"cd /tmp && gh pr comment 7 {body}",
    ):
        assert "отель" in hook(command, env, tmp_path), command


# --- дверь hook: команды git и всё прочее ----------------------------------------------


def test_commit_message_is_checked_but_added_files_are_left_to_push(
    env: dict[str, str], tmp_path: Path
) -> None:
    (tmp_path / "notes.md").write_text(HOTEL, encoding="utf-8")
    message = tmp_path / "message.txt"
    message.write_text(f"Правка\n\n{HOTEL}\n", encoding="utf-8")
    assert "отель" in hook(f'git commit -m "Заселение в {HOTEL}"', env, tmp_path)
    assert f"{message}:3" in hook(f"git commit -F {message}", env, tmp_path)
    assert f"{message}:3" in hook("git -C . commit --file=message.txt", env, tmp_path)
    # Содержимое файла из `git add` смотрит pre-push — по добавленным строкам, а не целиком.
    assert hook('git add notes.md && git commit -m "Заметки"', env, tmp_path) == ""


def test_push_past_the_hook_is_denied(env: dict[str, str], tmp_path: Path) -> None:
    assert "мимо проверки" in hook("git push --no-verify origin HEAD", env, tmp_path)
    assert "мимо проверки" in hook("git -c core.hooksPath=/dev/null push", env, tmp_path)
    assert hook("git push -u origin task-1", env, tmp_path) == ""


def test_other_commands_and_tools_are_not_touched(env: dict[str, str], tmp_path: Path) -> None:
    assert hook(f'grep -rn "{HOTEL}" docs/', env, tmp_path) == ""
    assert hook(f'git log -S "{HOTEL}" --oneline', env, tmp_path) == ""
    assert hook(f'gh pr comment 7 --body "{HOTEL}"', env, tmp_path, tool="Edit") == ""


def test_broken_input_never_blocks_a_command(env: dict[str, str]) -> None:
    result = guard(["hook"], "это не JSON", env)
    assert result.returncode == 0
    assert result.stdout == ""
    assert "ошибка самого щита" in result.stderr


def test_script_works_under_the_system_python(env: dict[str, str]) -> None:
    """Хук запускает `python3` из PATH окна — на macOS это может быть системный 3.9."""
    system_python = Path("/usr/bin/python3")
    if not system_python.exists():
        pytest.skip("системного python3 нет")
    result = subprocess.run(
        [str(system_python), str(GUARD), "scan", "-"],
        input=f"Заселение в {HOTEL}, ключ {SK_KEY}",
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )
    assert result.returncode == 1, result.stderr
    assert "отель" in result.stdout
    assert "секрет: ключ API" in result.stdout


# --- подключение в .claude/settings.json ------------------------------------------------


def registered_commands() -> dict[str, str]:
    settings = json.loads((REPO_ROOT / ".claude" / "settings.json").read_text(encoding="utf-8"))
    return {
        handler["if"]: handler["command"]
        for entry in settings["hooks"]["PreToolUse"]
        for handler in entry["hooks"]
        if "public_guard.py" in handler["command"]
    }


def run_registered(command: str, project: Path, payload: str, env: dict[str, str]) -> str:
    result = subprocess.run(
        command,
        shell=True,
        input=payload,
        env={**env, "CLAUDE_PROJECT_DIR": str(project)},
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr  # код 2 остановил бы команду окна
    return result.stdout


def test_project_settings_run_the_guard_on_gh_and_git(env: dict[str, str], tmp_path: Path) -> None:
    commands = registered_commands()
    assert set(commands) == {"Bash(gh *)", "Bash(git *)"}
    assert len(set(commands.values())) == 2  # одинаковые обработчики Claude Code склеил бы в один
    payload = json.dumps(
        {
            "tool_name": "Bash",
            "cwd": str(tmp_path),
            "tool_input": {"command": f'gh pr comment 7 --body "{HOTEL}"'},
        }
    )
    for command in commands.values():
        assert '"permissionDecision": "deny"' in run_registered(command, REPO_ROOT, payload, env)
        # Основная папка ещё без скрипта (не обновлена): хук молчит, а не останавливает команды.
        assert run_registered(command, tmp_path, payload, env) == ""
