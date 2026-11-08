"""Configuration contract for the proactive-refresh switch (issue #17), no Supervisor needed."""

import importlib
import os
import re
import sys

import pytest

sys.argv = ["thermowatt_bridge.py"]

ADDON = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _flag(monkeypatch, value):
    if value is None:
        monkeypatch.delenv("THERMOWATT_PROACTIVE_REFRESH", raising=False)
    else:
        monkeypatch.setenv("THERMOWATT_PROACTIVE_REFRESH", value)
    import thermowatt_bridge
    return importlib.reload(thermowatt_bridge).MyThermowattBridge.PROACTIVE_REFRESH


@pytest.mark.parametrize("value,expected", [
    (None, False), ("", False), ("0", False), ("false", False), ("off", False),
    ("garbage", False), ("2", False), ("1", True), ("true", True), (" True ", True),
    ("yes", True), ("ON", True),
])
def test_env_parsing_is_default_off_and_strict(monkeypatch, value, expected):
    assert _flag(monkeypatch, value) is expected
    importlib.reload(sys.modules["thermowatt_bridge"])


def test_addon_option_is_declared_default_false_and_optional():
    text = open(os.path.join(ADDON, "config.yaml")).read()
    options, schema = text.split("schema:")
    assert re.search(r"^\s+proactive_token_refresh:\s*false\s*$", options, re.M)
    assert re.search(r"^\s+proactive_token_refresh:\s*bool\?\s*$", schema, re.M)


def test_run_sh_derives_the_env_only_from_the_option():
    text = open(os.path.join(ADDON, "run.sh")).read()
    assert "bashio::config.true 'proactive_token_refresh'" in text
    assert "THERMOWATT_PROACTIVE_REFRESH=1" in text and "THERMOWATT_PROACTIVE_REFRESH=0" in text
    assert "export THERMOWATT_PROACTIVE_REFRESH" in text
