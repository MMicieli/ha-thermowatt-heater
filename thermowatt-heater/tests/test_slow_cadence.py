"""Stage 2b: configurable background interval, cadence-independent availability,
interval-aligned energy cap. Defaults must stay identical to 1.8.0 / Stage 1."""
import os
import re
import subprocess
import sys
import time
from unittest.mock import MagicMock, patch

import pytest

sys.argv = ["thermowatt_bridge.py"]
import thermowatt_bridge as module
from thermowatt_bridge import MyThermowattBridge

ADDON = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


@pytest.fixture
def bridge(monkeypatch, tmp_path):
    monkeypatch.setattr(module, "CONFIG_FILE", str(tmp_path / "bridge.json"))
    with patch("thermowatt_bridge.mqtt.Client") as factory:
        factory.return_value = MagicMock()
        b = MyThermowattBridge()
    b.config["devices"]["SN"] = {"name": "HWS", "last_setpoint": 58}
    return b


def _availability(b):
    return [c.args[1] for c in b.mqtt_client.publish.call_args_list
            if c.args and c.args[0] == "P/SN/availability"]


# ---- configuration ---------------------------------------------------------

def _interval(value):
    env = {k: v for k, v in os.environ.items() if k != "THERMOWATT_POLL_INTERVAL"}
    if value is not None:
        env["THERMOWATT_POLL_INTERVAL"] = value
    env["PYTHONPATH"] = os.pathsep.join([ADDON] + sys.path)
    code = ("import sys; sys.argv=['x']; import thermowatt_bridge as m; "
            "print(m.MyThermowattBridge.POLL_INTERVAL)")
    out = subprocess.run([sys.executable, "-c", code], env=env, capture_output=True,
                         text=True, timeout=60, cwd=ADDON)
    assert out.returncode == 0, out.stderr
    return int(out.stdout.strip().splitlines()[-1])


@pytest.mark.parametrize("value,expected", [
    (None, 60), ("", 60), ("null", 60), ("abc", 60), ("300.5", 60), ("59", 60),
    ("601", 60), ("0", 60), ("-300", 60), ("60", 60), ("300", 300), ("600", 600),
])
def test_background_interval_default_60_and_strict_range(value, expected):
    assert _interval(value) == expected


def test_option_declared_default_60_and_run_sh_exports_it():
    text = open(os.path.join(ADDON, "config.yaml")).read()
    options, schema = text.split("schema:")
    assert re.search(r"^\s+poll_interval:\s*60\s*$", options, re.M)
    assert re.search(r"^\s+poll_interval:\s*int\(60,600\)\?\s*$", schema, re.M)
    assert "export THERMOWATT_POLL_INTERVAL" in open(os.path.join(ADDON, "run.sh")).read()


def test_default_instance_matches_1_8_0(bridge):
    assert bridge.POLL_INTERVAL == 60 and bridge.current_poll_interval == 60
    assert bridge.TELEMETRY_STALE_AFTER == 120
    assert bridge._offline_after_s() == 300


def test_slower_cadence_scales_labels_not_safety_constants(bridge):
    bridge.POLL_INTERVAL = 300
    assert bridge._offline_after_s() == 360          # never flaps before a healthy poll
    assert bridge.POLL_INTERVAL_CONFIRM == 20 and bridge.CMD_COOLDOWN == 15


# ---- energy integration cap -----------------------------------------------

def _heating_poll(bridge, gap_s):
    bridge._last_poll_ts["SN"] = time.time() - gap_s
    resp = MagicMock(status_code=200)
    resp.json.return_value = {"result": {"WaterHeaterSts": 1, "T_Avg": 55.0,
                                         "T_SetPoint": 60.0, "Cmd": 9}}
    with patch.object(bridge, "request", return_value=resp), \
         patch.object(bridge, "_compute_status", side_effect=lambda d: {"result": {"heating": True}}), \
         patch.object(bridge, "_save_config"):
        bridge.poll_status("SN")
    return bridge.config["energy_kwh"]["SN"]


def test_energy_cap_covers_one_slow_interval_and_stays_bounded(bridge):
    bridge.POLL_INTERVAL = 300
    kwh = _heating_poll(bridge, 300)
    assert kwh == pytest.approx(bridge.element_wattage / 1000 * 300 / 3600, rel=1e-3)
    bridge.config["energy_kwh"]["SN"] = 0.0
    kwh = _heating_poll(bridge, 5000)                # outage: capped at 2 intervals
    assert kwh == pytest.approx(bridge.element_wattage / 1000 * 600 / 3600, rel=1e-3)


def test_energy_cap_at_default_is_unchanged_120s(bridge):
    kwh = _heating_poll(bridge, 5000)
    assert kwh == pytest.approx(bridge.element_wattage / 1000 * 120 / 3600, rel=1e-3)


# ---- age-based availability ---------------------------------------------------

def test_offline_after_age_even_with_few_failures_and_online_on_next_valid_poll(bridge):
    bridge._last_successful_poll["SN"] = time.time() - 301
    bridge._consecutive_failures["SN"] = 1           # far below DEGRADED_THRESHOLD
    bridge._publish_diagnostics("SN")
    assert _availability(bridge) == ["offline"]
    bridge._publish_diagnostics("SN")
    assert _availability(bridge) == ["offline"]      # published on change only

    resp = MagicMock(status_code=200)
    resp.json.return_value = {"result": {"WaterHeaterSts": 1, "T_Avg": 55.0,
                                         "T_SetPoint": 60.0, "Cmd": 9}}
    with patch.object(bridge, "request", return_value=resp), patch.object(bridge, "_save_config"):
        bridge.poll_status("SN")
    assert _availability(bridge) == ["offline", "online"]


def test_not_offline_inside_the_window(bridge):
    bridge._last_successful_poll["SN"] = time.time() - 290
    bridge._publish_diagnostics("SN")
    assert _availability(bridge) == []


def test_no_successful_poll_since_start_goes_offline_from_process_start(bridge):
    bridge._boot_time = time.time() - 400            # nothing persisted can mark it online
    bridge._publish_diagnostics("SN")
    assert _availability(bridge) == ["offline"]


def test_reconnect_does_not_republish_online_for_aged_out_device(bridge):
    bridge._last_successful_poll["SN"] = time.time() - 1000
    client = MagicMock()
    bridge.on_connect(client, None, None, 0, None)
    pubs = [c.args for c in client.publish.call_args_list if c.args[0] == "P/SN/availability"]
    assert pubs == [("P/SN/availability", "offline")] or pubs[0][1] == "offline"


def test_existing_loop_wakes_to_mark_offline_without_a_poll_or_request(bridge):
    bridge.request = MagicMock()
    bridge._last_successful_poll["SN"] = time.time() - 299.8
    t0 = time.time()
    bridge._wait_for_next_poll(1.5)
    assert _availability(bridge) == ["offline"]
    assert 0.1 < time.time() - t0 < 2.5
    bridge.request.assert_not_called()


def test_offline_is_evaluated_during_quota_backoff_with_no_request(bridge):
    bridge.request = MagicMock()
    bridge.DIAG_REFRESH_INTERVAL = 0.1
    bridge.rate_limit_backoff = 1
    bridge._rate_limit_until = time.time() + 0.6
    bridge._last_successful_poll["SN"] = time.time() - 400
    bridge._wait_for_next_poll(0.2)
    assert _availability(bridge) == ["offline"]
    bridge.request.assert_not_called()


def test_slow_cadence_healthy_device_does_not_flap(bridge):
    bridge.POLL_INTERVAL = 300
    bridge._last_successful_poll["SN"] = time.time() - 320   # just before the next poll
    bridge._publish_diagnostics("SN")
    assert _availability(bridge) == []


# ---- proactive refresh margin follows the cadence ----------------------------

def test_proactive_refresh_margin_covers_a_slow_cycle(bridge, monkeypatch):
    import base64
    import json as _json
    monkeypatch.setattr(MyThermowattBridge, "PROACTIVE_REFRESH", True)
    enc = lambda o: base64.urlsafe_b64encode(_json.dumps(o).encode()).decode().rstrip("=")
    exp = time.time() + 250                           # inside 300 s + 60 s, outside 120 s
    bridge.config["access_token"] = f"{enc({'alg': 'none'})}.{enc({'exp': exp})}.sig"
    bridge.config["refresh_token"] = "r"
    bridge.refresh_session = MagicMock(return_value=True)
    bridge._maybe_proactive_refresh()
    bridge.refresh_session.assert_not_called()   # default 60 s cadence: too early
    bridge.POLL_INTERVAL = 300
    bridge._maybe_proactive_refresh()
    assert bridge.refresh_session.call_count == 1
