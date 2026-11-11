"""Пути прогона по эффектам (#1051 п. 1): сотрудник и гость.

Шаги — те же, что у сквозного теста `tests/test_launch_path.py`, но на базе и настройках
staging: каждый шаг идёт по HTTP приложения, как идут люди, а эффект проверяется там, куда
он обязан прийти, — в базе (заявка, назначение) или на странице другого человека. Модель —
только Fake-адаптеры шлюза (ADR-007): ход гостя и экзамен оплачиваемых вызовов не делают.

Путь — последовательный код: шаг удался — `ok`, первое исключение — шаг ✗ с причиной,
остальные шаги пути — «не дошли». Созданное путь записывает в `Created` сразу, как оно
появилось: уборка (`effects_run.cleanup`) гасит его, даже если путь упал на середине.
"""

from __future__ import annotations

import json
import re
import secrets
import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any, Final
from urllib.parse import urlsplit

import httpx
from fastapi import FastAPI
from httpx import AsyncClient

from hospitality.ai import urgency
from hospitality.ai.gateway.api import MockEmbeddingProvider, MockLlmProvider
from hospitality.ai.mentor.common import MentorProviders
from hospitality.channels.common.consent import CONSENT_VERSION
from hospitality.channels.web.router import get_web_llm_provider
from hospitality.modules.guests import api as guests_api
from hospitality.modules.requests import api as requests_api
from hospitality.platform.config import TenantConfig
from hospitality.platform.staff_services import HOUSEKEEPING_SERVICE_KEY
from hospitality.platform.staff_team import list_tenant_members
from hospitality.shared.db import utc_now
from hospitality.shared.tenancy import tenant_context
from hospitality.staff_portal.mentor_api import get_mentor_providers

# Номер тестового гостя: в демо-картине его нет (`tools/demo_showcase_board.py`).
TEST_ROOM: Final = "9901"
TEST_GUEST: Final = "Проверка запуска"

# Вопрос гостя на каждом языке копии и ответ Fake-модели: ход обязан дойти до модели
# и вернуть гостю её текст, на каком бы языке гость ни спросил.
GUEST_QUESTIONS: Final = (
    ("ru", "Во сколько завтрак?", "Проверка запуска: ответ на вопрос о завтраке."),
    ("kk", "Таңғы ас қашан?", "Іске қосуды тексеру: таңғы ас туралы жауап."),
    ("en", "What time is breakfast?", "Launch check: the breakfast answer."),
)
EMERGENCY: Final = "Пожар в номере, дым!"
EXAM_ANSWER: Final = "Закончу звонок и поздороваюсь с гостем."
# Ходов экзамена не больше, чем вопросов вдвое и с запасом: ответ без разобранной
# оценки не засчитывается и даёт новый вопрос (spec 0072 §2), но не бесконечно.
EXAM_TURN_SLACK: Final = 3

STAFF_STEPS: Final = (
    "«Наставник» включён у отеля",
    "Менеджер приглашает сотрудника службы «Хаускипинг»",
    "Сотрудник принимает приглашение и попадает в «Очередь заявок»",
    "Выход и вход логином — как на следующей смене",
    "Без отметки согласия «Наставник» закрыт",
    "Отметка менеджера открывает «Наставник»",
    "Менеджер назначает экзамен службе",
    "Сотрудник видит назначение и сдаёт экзамен",
    "Сдача видна менеджеру в «Итогах экзаменов»",
)
GUEST_STEPS: Final = (
    "Служба эскалаций и телефон ресепшена заданы",
    f"Тестовый гость заселён в номер {TEST_ROOM}",
    "Экран входа гостя — согласие действующей версии",
    "Код заселения привязывает чат",
    *(
        f"Вопрос ({language}) доходит до модели, ответ — гостю"
        for language, _, _ in GUEST_QUESTIONS
    ),
    "Слово о ЧП — телефон ресепшена, модель не зовётся",
    "ЧП — срочная карточка службы эскалаций с номером и проживанием",
    "Менеджер видит карточку в очереди с пометкой «Срочно»",
)


class StepFailedError(Exception):
    """Эффект шага не пришёл: текст — что ожидалось и что увидели."""


def expect(condition: bool, failure: str) -> None:
    if not condition:
        raise StepFailedError(failure)


@dataclass(frozen=True)
class StepResult:
    path: str
    title: str
    ok: bool | None  # None — путь до шага не дошёл
    detail: str = ""


@dataclass
class Recorder:
    """Итоги шагов одного пути по порядку объявленных названий."""

    path: str
    titles: Sequence[str]
    results: list[StepResult] = field(default_factory=list)

    def ok(self, detail: str = "") -> None:
        self.results.append(StepResult(self.path, self.titles[len(self.results)], True, detail))

    def fail(self, error: BaseException) -> None:
        reason = str(error) or type(error).__name__
        self.results.append(StepResult(self.path, self.titles[len(self.results)], False, reason))
        for title in self.titles[len(self.results) :]:
            self.results.append(StepResult(self.path, title, None, "не дошли"))


@dataclass
class Created:
    """Что прогон завёл в отеле — для уборки."""

    invite_logins: list[str] = field(default_factory=list)
    manager_id: uuid.UUID | None = None
    employee_id: uuid.UUID | None = None
    assignment_id: uuid.UUID | None = None
    stay_id: uuid.UUID | None = None
    card_id: uuid.UUID | None = None


@dataclass
class Run:
    """Приложение в процессе, три браузера со своими cookie и созданное прогоном."""

    tenant_id: uuid.UUID
    slug: str
    config: TenantConfig
    app: FastAPI
    manager: AsyncClient
    employee: AsyncClient
    guest: AsyncClient
    origin: str
    tag: str  # метка прогона в логинах и именах: свои строки видно в кабинете
    created: Created = field(default_factory=Created)

    @property
    def home(self) -> str:
        return f"/staff/{self.slug}"

    async def post(self, client: AsyncClient, path: str, body: dict[str, Any]) -> httpx.Response:
        return await client.post(path, json=body, headers={"origin": self.origin})

    def mentor_answers(self, text: str) -> None:
        """Следующий ход «Наставника» — Fake-модель с этим текстом."""
        fake = MentorProviders(
            chat=MockLlmProvider(text=text),
            streaming=MockLlmProvider(text=text),
            embeddings=MockEmbeddingProvider(),
        )
        self.app.dependency_overrides[get_mentor_providers] = lambda: fake

    def guest_model_answers(self, text: str) -> MockLlmProvider:
        """Следующий ход гостя — Fake-модель с этим текстом; её вызовы видны в `calls`."""
        fake = MockLlmProvider(text=text)
        self.app.dependency_overrides[get_web_llm_provider] = lambda: fake
        return fake


def new_password() -> str:
    return secrets.token_urlsafe(18)


def _passed_row(name: str) -> re.Pattern[str]:
    """Строка сотрудника «сдан» в таблице службы (`mentor_results.html`, `_member_row`)."""
    return re.compile(
        rf"<td>{re.escape(name)}</td>\s*<td><span class=\"mentor-pass\">сдан — \d+ %</span>"
    )


def _last_event(text: str) -> tuple[str, dict[str, Any]]:
    """Последнее событие потока «Наставника» (`event: …` / `data: {…}`)."""
    frame = text.strip().split("\n\n")[-1]
    lines = dict(line.split(": ", 1) for line in frame.split("\n"))
    return lines["event"], json.loads(lines["data"])


async def member_id(run: Run, login: str) -> uuid.UUID:
    for member in await list_tenant_members(run.tenant_id):
        if member.login == login:
            return member.user_id
    raise StepFailedError(f"учётки {login} нет среди сотрудников отеля")


async def accept_invite(run: Run, client: AsyncClient, invite_path: str, password: str) -> None:
    page = await client.get(invite_path)
    expect(page.status_code == 200, f"страница приглашения — {page.status_code}")
    accepted = await client.post(
        invite_path, data={"password": password}, headers={"origin": run.origin}
    )
    expect(
        accepted.status_code == 303 and accepted.headers.get("location") == f"{run.home}/requests",
        f"принятие — {accepted.status_code} {accepted.headers.get('location')}",
    )


async def staff_path(run: Run) -> list[StepResult]:
    rec = Recorder("Сотрудник", STAFF_STEPS)
    api, mentor = f"{run.home}/api", f"{run.home}/mentor"
    login, name, password = f"EFS{run.tag}", f"Проверка запуска {run.tag}", new_password()
    try:
        expect(run.config.mentor_enabled, "у отеля выключен mentor_enabled")
        rec.ok()

        invited = await run.post(
            run.manager,
            f"{api}/team/invites",
            {
                "invited_name": name,
                "login": login,
                "role_key": "staff",
                "service_keys": [HOUSEKEEPING_SERVICE_KEY],
            },
        )
        expect(invited.status_code == 200, f"приглашение — {invited.status_code} {invited.text}")
        run.created.invite_logins.append(login)
        rec.ok(f"логин {login}")

        await accept_invite(
            run, run.employee, urlsplit(invited.json()["invite_url"]).path, password
        )
        run.created.employee_id = await member_id(run, login)
        expect((await run.employee.get(f"{run.home}/requests")).status_code == 200, "очередь")
        rec.ok()

        await run.employee.post("/staff/logout", headers={"origin": run.origin})
        expect((await run.employee.get(f"{run.home}/requests")).status_code == 303, "выход")
        signed = await run.employee.post(
            f"{run.home}/login", data={"login": login, "password": password}
        )
        expect(signed.status_code == 303, f"вход — {signed.status_code}")
        rec.ok()

        closed = await run.employee.get(mentor)
        expect(closed.status_code == 403 and "Раздел закрыт" in closed.text, "раздел не закрыт")
        rec.ok()

        employee_id = run.created.employee_id
        marked = await run.post(
            run.manager, f"{api}/team/members/{employee_id}/ai-consent", {"recipients": ["openai"]}
        )
        expect(marked.status_code == 200, f"отметка — {marked.status_code} {marked.text}")
        expect((await run.employee.get(mentor)).status_code == 200, "раздел не открылся")
        rec.ok()

        assigned = await run.post(
            run.manager,
            f"{api}/mentor/exam/assignments",
            {"chapters": None, "service_keys": [HOUSEKEEPING_SERVICE_KEY]},
        )
        expect(assigned.status_code == 200, f"назначение — {assigned.status_code} {assigned.text}")
        assignment_id = uuid.UUID(assigned.json()["id"])
        run.created.assignment_id = assignment_id
        rec.ok("весь кодекс")

        rec.ok(await _pass_exam(run, api, mentor, assignment_id))

        results = await run.manager.get(f"{mentor}/results")
        expect(results.status_code == 200, f"итоги — {results.status_code}")
        expect(_passed_row(name).search(results.text) is not None, f"строки «{name} — сдан» нет")
        rec.ok()
    except Exception as error:  # отчёт, а не трассировка: следующий путь всё равно идёт
        rec.fail(error)
    return rec.results


async def _pass_exam(run: Run, api: str, mentor: str, assignment_id: uuid.UUID) -> str:
    page = (await run.employee.get(f"{mentor}/exam")).text
    expect(f'data-assignment="{assignment_id}"' in page, "назначения нет на странице экзамена")
    started = await run.post(
        run.employee, f"{api}/mentor/exam/start", {"assignment_id": str(assignment_id)}
    )
    expect(started.status_code == 200, f"старт — {started.status_code} {started.text}")
    progress = started.json()
    for _ in range(progress["total"] * 2 + EXAM_TURN_SLACK):
        attempt = progress["attempt_id"]
        run.mentor_answers("Гость подходит, пока вы говорите по телефону. Что вы сделаете?")
        asked = await _mentor_turn(run, f"{api}/mentor/exam/next", {"attempt_id": attempt})
        run.mentor_answers("Оценка: верно\nВсё так.")
        body = {"message": EXAM_ANSWER, "attempt_id": attempt, "turn": asked["exam"]["turn"]}
        progress = (await _mentor_turn(run, f"{api}/mentor/exam/answer", body))["exam"]
        if progress["finished"]:
            expect(progress["passed"] is True, f"не сдан — {progress['percent']} %")
            return f"{progress['answered']} вопросов, {progress['percent']} %"
    raise StepFailedError("экзамен не закончился")


async def _mentor_turn(run: Run, path: str, body: dict[str, Any]) -> dict[str, Any]:
    response = await run.post(run.employee, path, body)
    expect(response.status_code == 200, f"{path} — {response.status_code}")
    kind, data = _last_event(response.text)
    expect(kind == "done", f"{path} — событие {kind}: {data}")
    return data


async def guest_path(run: Run) -> list[StepResult]:
    rec = Recorder("Гость", GUEST_STEPS)
    chat = f"/g/{run.slug}/{TEST_ROOM}"
    try:
        escalation = await _escalation_service(run)
        expect(bool(run.config.reception_phone), "у отеля нет телефона ресепшена")
        rec.ok(run.config.escalation_category_key or "")

        with tenant_context(run.tenant_id):
            stay = await guests_api.check_in(
                guests_api.StayCheckIn(
                    room_number=TEST_ROOM,
                    check_out_at=utc_now() + timedelta(days=1),
                    guest_display_name=TEST_GUEST,
                )
            )
        run.created.stay_id = stay.stay.id
        rec.ok()

        entry = await run.guest.get(chat)
        expect(entry.status_code == 200 and CONSENT_VERSION in entry.text, "нет экрана согласия")
        rec.ok(CONSENT_VERSION)

        bound = await run.post(run.guest, f"{chat}/session", {"code": stay.access_code})
        expect(bound.status_code == 200, f"код — {bound.status_code} {bound.text}")
        rec.ok()

        for _, question, answer in GUEST_QUESTIONS:
            model = run.guest_model_answers(answer)
            replied = await run.post(run.guest, f"{chat}/messages", _message(question))
            expect(replied.status_code == 200, f"ход — {replied.status_code} {replied.text}")
            expect(bool(model.calls), "ход до модели не дошёл")
            expect(replied.json()["replies"][:1] == [answer], f"гостю ушло {replied.json()}")
            rec.ok()

        model = run.guest_model_answers("ЧП-перехват не должен звать модель.")
        alarm = await run.post(run.guest, f"{chat}/messages", _message(EMERGENCY))
        expect(alarm.status_code == 200, f"ход — {alarm.status_code} {alarm.text}")
        expected = urgency.emergency_reply(
            "ru", run.config.reception_phone, reception_room_dial=run.config.reception_room_dial
        )
        expect(alarm.json()["replies"] == [expected], f"гостю ушло {alarm.json()}")
        expect(not model.calls, "ЧП позвал модель")
        rec.ok()

        card = await _emergency_card(run, stay.stay.id, escalation)
        run.created.card_id = card.id
        rec.ok(f"заявка №{card.daily_number}")

        queue = await run.manager.get(f"{run.home}/requests")
        expect(queue.status_code == 200 and card.summary in queue.text, "карточки нет в очереди")
        expect('class="badge badge-urgent">Срочно<' in queue.text, "нет пометки «Срочно»")
        rec.ok()
    except Exception as error:  # отчёт, а не трассировка: следующий путь всё равно идёт
        rec.fail(error)
    return rec.results


async def _escalation_service(run: Run) -> uuid.UUID:
    """Служба эскалаций отеля: без неё ЧП гостя карточкой в кабинет не приходит (#785)."""
    key = run.config.escalation_category_key
    if key is None:
        raise StepFailedError("у отеля нет службы эскалаций (escalation_category_key)")
    with tenant_context(run.tenant_id):
        categories = await requests_api.list_categories()
    service = next((category.id for category in categories if category.key == key), None)
    if service is None:
        raise StepFailedError(f"службы эскалаций {key} нет среди служб отеля")
    return service


async def _emergency_card(
    run: Run, stay_id: uuid.UUID, service: uuid.UUID
) -> requests_api.ServiceRequestRead:
    with tenant_context(run.tenant_id):
        cards = await requests_api.list_open_requests(limit=100, category_ids=[service])
    ours = [card for card in cards if card.stay_id == stay_id]
    expect(len(ours) == 1, f"карточек проживания прогона: {len(ours)}")
    card = ours[0]
    expect(card.origin is requests_api.ServiceRequestOrigin.ESCALATION, f"origin {card.origin}")
    expect(card.is_urgent and card.room_number == TEST_ROOM, "не срочная или не тот номер")
    expect(EMERGENCY in card.summary, f"суть — {card.summary}")
    return card


def _message(text: str) -> dict[str, str]:
    return {"text": text, "client_message_id": str(uuid.uuid4())}
