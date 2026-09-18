import importlib.util
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "load_test.py"
SPEC = importlib.util.spec_from_file_location("openfotos_load_test", SCRIPT)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


def test_form_parser_selects_requested_form_and_csrf_token() -> None:
    html = """
        <form action="/ignored/"><input name="csrfmiddlewaretoken" value="wrong"></form>
        <form action="/portfolio/events/id/unlock/" method="post">
          <input name="csrfmiddlewaretoken" value="right">
        </form>
    """

    action, token = MODULE._form(
        html,
        base_url="https://studio.example/portfolio/events/id/",
        action_fragment="/unlock/",
    )

    assert action == "https://studio.example/portfolio/events/id/unlock/"
    assert token == "right"


def test_missing_form_fails_without_echoing_page_content() -> None:
    with pytest.raises(MODULE.LoadTestConfigurationError, match="'/search/' form"):
        MODULE._form(
            "<p>private content</p>",
            base_url="https://studio.example/",
            action_fragment="/search/",
        )


def test_percentile_uses_nearest_rank() -> None:
    values = [float(value) for value in range(1, 101)]

    assert MODULE.percentile(values, 50) == 50
    assert MODULE.percentile(values, 95) == 95
