"""Task 0001: проверка, что каркас проекта собирается и импортируется."""

import hospitality
import hospitality.ai
import hospitality.ai.gateway
import hospitality.channels
import hospitality.integrations
import hospitality.modules
import hospitality.platform
import hospitality.shared


def test_root_package_imports() -> None:
    assert hospitality.__name__ == "hospitality"


def test_layer_packages_exist() -> None:
    layers = [
        hospitality.platform,
        hospitality.shared,
        hospitality.modules,
        hospitality.ai,
        hospitality.channels,
        hospitality.integrations,
    ]
    assert all(pkg.__name__.startswith("hospitality.") for pkg in layers)
