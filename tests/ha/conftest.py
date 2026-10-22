from pathlib import Path

import pytest

OURS = str(Path(__file__).parents[2] / "custom_components")


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    """HA's test config ships its own custom_components package; add ours to it."""
    import custom_components

    if OURS not in custom_components.__path__:
        custom_components.__path__.append(OURS)
    yield
