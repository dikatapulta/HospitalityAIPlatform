"""Красный CI на ``main`` — алерт в канал команды (issue #413, ERR-OPS-013).

Окружение job собирается руками: так выглядит ``toJSON(needs)`` и ``GITHUB_*``
раннера на настоящем push в ``main``. Сеть — ``httpx.MockTransport``.
"""

from __future__ import annotations

import json

import httpx
import pytest

from hospitality.shared.config import Settings
from hospitality.shared.telegram_api import TelegramApiError
from hospitality.tools.ci_alert import ERR_CI_RED_MAIN, describe_failure, main

_TOKEN = "123456789:AAHciAlertLeakMarker"

_RED_CHECK = json.dumps(
    {
        "check": {"result": "failure", "outputs": {}},
        "audit": {"result": "success", "outputs": {}},
        "migrations": {"result": "success", "outputs": {}},
        "dev-environment": {"result": "success", "outputs": {}},
        "deploy-staging": {"result": "skipped", "outputs": {}},
    }
)

_ENVIRON = {
    "GITHUB_SERVER_URL": "https://github.com",
    "GITHUB_REPOSITORY": "dikatapulta/HospitalityAIPlatform",
    "GITHUB_RUN_ID": "123",
    "GITHUB_SHA": "4cf46ac0123456789abcdef0123456789abcdef0",
    "GITHUB_WORKFLOW": "CI",
    "CI_NEEDS_JSON": _RED_CHECK,
    "CI_COMMIT_TITLE": "Этажи доской (#660) (#664)\n\nТело коммита не нужно.",
}


def _settings(token: str = _TOKEN, chat_id: str = "-1001") -> Settings:
    return Settings(telegram_alert_bot_token=token, telegram_alert_chat_id=chat_id)


def test_detail_names_failed_jobs_skipped_deploy_commit_and_run() -> None:
    detail = describe_failure(
        workflow="CI",
        needs_json=_RED_CHECK,
        sha=_ENVIRON["GITHUB_SHA"],
        commit_title=_ENVIRON["CI_COMMIT_TITLE"],
        run_url="https://github.com/o/r/actions/runs/123",
    )
    assert detail == (
        "workflow «CI»; упали: check; не запускались: deploy-staging — staging не обновлён"
        " · коммит 4cf46ac «Этажи доской (#660) (#664)»"
        " · прогон: https://github.com/o/r/actions/runs/123"
    )


def test_failed_deploy_is_not_called_skipped_and_long_title_is_trimmed() -> None:
    """Упал сам деплой — «staging не обновлён» было бы неправдой: выкатка могла пройти."""
    needs = json.dumps({"check": {"result": "success"}, "deploy-staging": {"result": "failure"}})
    detail = describe_failure(
        workflow="CI", needs_json=needs, sha="abcdef0", commit_title="x" * 300, run_url="u"
    )
    assert "упали: deploy-staging" in detail
    assert "не обновлён" not in detail and "не запускались" not in detail
    assert "«" + "x" * 99 + "…»" in detail


def test_unreadable_needs_still_alerts_and_dispatch_has_no_title() -> None:
    """Алерт важнее подробностей; у ручного запуска заголовка коммита нет."""
    detail = describe_failure(
        workflow="Smoke", needs_json="", sha="abcdef0", commit_title="", run_url="u"
    )
    assert detail == "workflow «Smoke»; какие job упали — в прогоне · коммит abcdef0 · прогон: u"


def test_main_sends_canonical_alert_to_team_chat(capsys: pytest.CaptureFixture[str]) -> None:
    sent: list[dict[str, str]] = []

    def telegram(request: httpx.Request) -> httpx.Response:
        assert request.url.path == f"/bot{_TOKEN}/sendMessage"
        sent.append(json.loads(request.content))
        return httpx.Response(200, json={"ok": True, "result": {}})

    assert main(_settings(), _ENVIRON, httpx.MockTransport(telegram)) == 0

    (payload,) = sent
    assert payload["chat_id"] == "-1001"
    text = payload["text"]
    assert text.startswith(f"🔴 [ci] {ERR_CI_RED_MAIN}: проверки на main красные\n")
    assert "упали: check" in text
    assert "https://github.com/dikatapulta/HospitalityAIPlatform/actions/runs/123" in text
    assert text.endswith("docs/runbooks/alerts.md#err-ops-013")
    assert "отправлен" in capsys.readouterr().out


def test_main_without_secrets_is_a_notice_not_a_red_job(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Секреты ещё не заведены — job зелёная, причина видна в логе прогона."""

    def no_network(_request: httpx.Request) -> httpx.Response:
        raise AssertionError("без секретов в сеть не ходим")

    assert main(_settings("", ""), _ENVIRON, httpx.MockTransport(no_network)) == 0
    assert capsys.readouterr().out.startswith("::notice::")


def test_half_configured_pair_fails_fast() -> None:
    with pytest.raises(SystemExit):
        main(_settings(chat_id=""), _ENVIRON)


def test_bot_api_refusal_fails_the_job_without_the_token() -> None:
    def refused(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"ok": False, "description": "Bad Request: chat not found"})

    with pytest.raises(TelegramApiError) as error:
        main(_settings(), _ENVIRON, httpx.MockTransport(refused))
    assert "chat not found" in str(error.value)
    assert "AAHciAlertLeakMarker" not in str(error.value)
