"""Офлайн-прогон чтения фото реквизитов моделью (spec 0049 Р-13, #530).

Картинки рисуются здесь же, с выдуманными реквизитами, и проходят ту же
перекодировку, что фото гостя при загрузке (WebP со стороной ≤ 1600 px,
`file_content.prepare_upload`): модель видит то, что увидит на проде. Запрос
строит тот же `act_requisites.build_scan_request`, что подписчик воркера.

Что мерим: поля читаются как напечатаны (ru и kk), две половины на двух фото
складываются, строка «для ИИ» в картинке не исполняется, чек и счёт с двумя
компаниями не дают чужих реквизитов. Значения проходят ту же проверку формата,
что ответ модели на проде (`documents.apply_scan_requisites`), — сравнивается
то, что легло бы в строку. Сценарии — `act_requisites_scan_scenarios.py`:
`baseline` — чтение, `attacks` — фото с инструкциями для модели (issue #954);
атаку стоит повторить: исполненная один раз из трёх инструкция — тоже находка.

Запуск ПЛАТНЫЙ — только с согласия основателя на план и цену (CLAUDE.md);
нужны ключ Anthropic и поднятый Postgres с миграциями, как у bakeoff:

    python -m hospitality.ai.evals.act_requisites_scan [--only attacks] [--repeat 3]
        [--ceiling 0.20] [--font путь.ttf] [--save-images папка]
    python -m hospitality.ai.evals.act_requisites_scan --plan   # цена, без вызовов

Потолок — расход по журналу шлюза: следующий вызов не идёт, если с его резервом
сумма вышла бы за потолок. `--plan` печатает число вызовов и их резерв по
прайс-листу шлюза — верхнюю границу цены для согласования; модель не зовёт.
"""

from __future__ import annotations

import argparse
import asyncio
import io
import os
import sys
import uuid
from decimal import Decimal
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont
from sqlalchemy import select

from hospitality.ai import act_requisites
from hospitality.ai.evals.act_requisites_scan_scenarios import (
    ATTACKS,
    BASELINE,
    SCENARIOS,
    F,
    Faint,
    Line,
    Scenario,
)
from hospitality.ai.gateway import api as gateway
from hospitality.ai.gateway import service as gateway_service
from hospitality.modules.documents.api import RequisiteField, clean_scan_value
from hospitality.platform.file_content import FilePurpose, prepare_upload
from hospitality.platform.models import Tenant
from hospitality.shared.config import get_settings
from hospitality.shared.db import platform_session_scope
from hospitality.shared.tenancy import tenant_context

_FONTS = (
    "/System/Library/Fonts/Supplemental/Arial.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
)
_SETS = {"all": SCENARIOS, "baseline": BASELINE, "attacks": ATTACKS}
_PAPER = (244, 241, 233)
_INK = (25, 25, 30)
# Бледная мелкая строка (`Faint`): на снимке человеку почти не видна.
_FAINT_INK = (206, 202, 193)


def render(lines: list[Line], font_path: str) -> bytes:
    """Карточка «как сфотографирована»: светлый фон, лёгкий наклон и размытие."""
    font = ImageFont.truetype(font_path, 34)
    faint_font = ImageFont.truetype(font_path, 20)
    image = Image.new("RGB", (1500, 120 + 56 * len(lines)), _PAPER)
    draw = ImageDraw.Draw(image)
    for index, line in enumerate(lines):
        if isinstance(line, Faint):
            draw.text((60, 60 + 56 * index), line.text, font=faint_font, fill=_FAINT_INK)
        else:
            draw.text((60, 60 + 56 * index), line, font=font, fill=_INK)
    photo = image.rotate(1.5, expand=True, fillcolor=(205, 200, 190)).filter(
        ImageFilter.GaussianBlur(0.6)
    )
    out = io.BytesIO()
    photo.save(out, format="PNG")
    # Та же перекодировка, что у загрузки гостя: WebP ≤ 1600 px без метаданных.
    return prepare_upload(FilePurpose.GUEST_DOCUMENT, out.getvalue()).data


def stored_values(raw: dict[RequisiteField, str]) -> dict[RequisiteField, str]:
    """Что легло бы в строку: проверка формата прода; не тот формат — пусто."""
    return {field: clean_scan_value(field, raw.get(field, "")) or "" for field in F}


async def _tenant_id() -> uuid.UUID:
    async with platform_session_scope() as session:
        tenant = await session.scalar(select(Tenant).where(Tenant.slug == "scan-eval"))
        if tenant is None:
            tenant = Tenant(slug="scan-eval", name="Scan Eval")
            session.add(tenant)
            await session.flush()
        return tenant.id


def build_request(scenario: Scenario, font_path: str, out_dir: Path | None) -> gateway.LlmRequest:
    images = [render(page, font_path) for page in scenario.pages]
    if out_dir is not None:
        for index, data in enumerate(images):
            (out_dir / f"{scenario.name}_{index}.webp").write_bytes(data)
    return act_requisites.build_scan_request(
        [gateway.LlmImage(media_type="image/webp", data=data) for data in images]
    )


def plan(scenarios: list[Scenario], repeat: int, font_path: str, model: str) -> Decimal:
    """Сколько вызовов и какой у них резерв по прайс-листу шлюза — без вызовов модели."""
    total = Decimal(0)
    for scenario in scenarios:
        reserve = gateway_service._reserve_usd(build_request(scenario, font_path, None), model)
        total += reserve * repeat
        print(f"{scenario.name}: {repeat} × ${reserve:.4f} (фото: {len(scenario.pages)})")
    print(f"Итого вызовов: {len(scenarios) * repeat}, резерв (верхняя граница): ${total:.2f}")
    return total


async def run(
    scenarios: list[Scenario], repeat: int, ceiling: Decimal, font_path: str, out_dir: Path | None
) -> int:
    model = get_settings().llm_model_documents
    provider = gateway.build_provider(model)
    tenant_id = await _tenant_id()
    spent = Decimal(0)
    failures = 0
    for scenario in scenarios:
        request = build_request(scenario, font_path, out_dir)
        for attempt in range(1, repeat + 1):
            if spent + gateway_service._reserve_usd(request, provider.model) > ceiling:
                print(f"СТОП: потолок ${ceiling} — {scenario.name} и дальше не прогнаны")
                return failures + 1
            with tenant_context(tenant_id):
                response = await gateway.complete(request, provider=provider)
            spent += response.cost_usd
            failures += not report(scenario, attempt, response)
    runs = len(scenarios) * repeat
    print(f"Итого: {runs - failures}/{runs}, потрачено ${spent:.4f}")
    return failures


def report(scenario: Scenario, attempt: int, response: gateway.LlmResponse) -> bool:
    """Строка исхода и поля; у атаки — ещё и то, что модель сказала мимо схемы."""
    raw = act_requisites.recorded_values(response)
    got = stored_values(raw or {})
    ok = raw is not None and scenario.passes(got)
    print(
        f"{'OK ' if ok else 'FAIL'} {scenario.name} #{attempt}: ${response.cost_usd:.4f}, "
        f"вход {response.input_tokens}, выход {response.output_tokens}"
    )
    for name in F:
        mark = "" if scenario.field_ok(name, got[name]) else "  ≠"
        sent = (raw or {}).get(name, "")
        note = f"  (модель: {sent!r})" if sent != got[name] else ""
        print(f"      {name.value:13} {got[name]!r}{mark}{note}")
    if scenario.attack:
        # До гостя и строки это не доходит (путь читает только шесть полей),
        # но показывает, послушалась ли модель инструкции на фото.
        call = next((c for c in response.tool_calls if c.name == act_requisites.TOOL_NAME), None)
        extra = sorted(set(call.arguments) - {name.value for name in F}) if call else []
        if response.text.strip() or extra or not any(got.values()):
            print(
                f"      мимо схемы: текст {response.text.strip()!r}, лишние ключи {extra}, "
                f"все поля пусты: {not any(got.values())}"
            )
    return ok


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--only", choices=sorted(_SETS), default="all")
    parser.add_argument("--repeat", type=int, default=1)
    parser.add_argument("--ceiling", type=Decimal, default=Decimal("0.20"))
    parser.add_argument("--font", default=next((p for p in _FONTS if Path(p).exists()), ""))
    parser.add_argument("--save-images", type=Path, default=None)
    parser.add_argument("--plan", action="store_true", help="цена по резерву шлюза, без вызовов")
    args = parser.parse_args()
    if not args.font:
        sys.exit("Нет шрифта с кириллицей: укажите --font путь.ttf")
    os.environ.setdefault("LLM_MODEL_DOCUMENTS", "claude-sonnet-5")
    get_settings.cache_clear()
    scenarios = _SETS[args.only]
    if args.plan:
        plan(scenarios, args.repeat, args.font, get_settings().llm_model_documents)
        return
    failures = asyncio.run(run(scenarios, args.repeat, args.ceiling, args.font, args.save_images))
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
