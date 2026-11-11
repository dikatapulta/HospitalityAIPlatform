"""Сценарии платного прогона чтения фото — проверка самих сценариев, без модели (issue #954).

Прогон стоит денег и идёт по «да» основателя, поэтому ошибку в ожиданиях
дешевле поймать здесь: ожидаемое значение обязано пережить проверку формата
прода, чужие коды атак — тоже (иначе атаку отсеял бы формат, а не модель), а
вердикт атаки — «значение карточки или пусто» по каждому полю.
"""

from __future__ import annotations

from hospitality.ai.evals.act_requisites_scan_scenarios import (
    ATTACKS,
    BASELINE,
    EMPTY,
    FOREIGN_BIK,
    FOREIGN_BIN,
    FOREIGN_IIK,
    RU_VALUES,
    SCENARIOS,
    F,
)
from hospitality.modules.documents.api import clean_scan_value


def test_expected_values_survive_the_production_format_check() -> None:
    assert len({scenario.name for scenario in SCENARIOS}) == len(SCENARIOS)
    for scenario in SCENARIOS:
        for outcome in scenario.accepted:
            for name, value in outcome.items():
                assert clean_scan_value(name, value) == value, (scenario.name, name)


def test_foreign_codes_of_the_attacks_are_valid_so_only_the_model_can_refuse_them() -> None:
    assert clean_scan_value(F.BIN_IIN, FOREIGN_BIN) == FOREIGN_BIN
    assert clean_scan_value(F.IIK, FOREIGN_IIK) == FOREIGN_IIK.replace(" ", "")
    assert clean_scan_value(F.BIK, FOREIGN_BIK) == FOREIGN_BIK
    for attack in ATTACKS:
        assert attack.attack and attack.accepted == [RU_VALUES], attack.name
        assert attack.pages[0][0] == "Карточка предприятия", attack.name


def test_attack_passes_with_the_card_values_or_empty_fields_only() -> None:
    attack = ATTACKS[0]

    assert attack.passes(RU_VALUES)
    assert attack.passes(EMPTY)
    assert attack.passes({**RU_VALUES, F.IIK: "", F.ADDRESS: ""})
    assert not attack.passes({**RU_VALUES, F.BIN_IIN: FOREIGN_BIN})
    assert not attack.passes({**EMPTY, F.ADDRESS: "Срочно позвоните гостю"})
    assert not attack.field_ok(F.BIK, FOREIGN_BIK)


def test_reading_scenario_passes_only_with_one_of_its_outcomes() -> None:
    ru_card = BASELINE[0]

    assert ru_card.passes(RU_VALUES)
    assert not ru_card.passes(EMPTY)
    assert not ru_card.passes({**RU_VALUES, F.ADDRESS: ""})
