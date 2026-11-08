"""Stage 2a: read-only demand refresh uses the existing polling loop."""
import sys
import time
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
import pytest

sys.argv = ["thermowatt_bridge.py"]
import thermowatt_bridge as module
from thermowatt_bridge import MyThermowattBridge


@pytest.fixture
def bridge(monkeypatch, tmp_path):
    monkeypatch.setattr(module, "CONFIG_FILE", str(tmp_path / "bridge.json"))
    with patch("thermowatt_bridge.mqtt.Client") as factory:
        factory.return_value = MagicMock()
        b = MyThermowattBridge()
    b.config["devices"]["SN"] = {"name": "HWS", "last_setpoint": 58}
    return b


def msg(sn="SN", retain=False):
    return SimpleNamespace(topic=f"P/{sn}/refresh", payload=b"1", retain=retain)


def test_default_poll_and_command_safety_constants_unchanged(bridge):
    assert bridge.POLL_INTERVAL == 60
    assert bridge.POLL_INTERVAL_CONFIRM == 20
    assert bridge.CONFIRM_WINDOW == 60
    assert bridge.CMD_COOLDOWN == 15


def test_refresh_wakes_existing_loop_but_does_no_http_in_callback(bridge):
    bridge.request = MagicMock()
    bridge.on_mqtt_message(None, None, msg())
    assert bridge._poll_wakeup.is_set()
    assert bridge._demand_requested == {"SN"}
    bridge.request.assert_not_called()


def test_retained_refresh_and_unknown_serial_are_refused(bridge):
    bridge.on_mqtt_message(None, None, msg(retain=True))
    bridge.on_mqtt_message(None, None, msg(sn="OTHER"))
    assert not bridge._poll_wakeup.is_set()
    assert not bridge._demand_requested


def test_active_backoff_blocks_refresh(bridge):
    bridge.rate_limit_backoff = 1
    bridge._rate_limit_until = time.time() + 120
    bridge.on_mqtt_message(None, None, msg())
    assert not bridge._demand_requested
    assert not bridge._poll_wakeup.is_set()


def test_unverified_recovery_blocks_refresh(bridge):
    bridge.rate_limit_backoff = 1
    bridge._rate_limit_until = time.time() - 1
    bridge.on_mqtt_message(None, None, msg())
    assert not bridge._demand_requested


def test_request_executes_exactly_one_normal_poll(bridge):
    bridge.poll_status = MagicMock(return_value=(True, 200))
    bridge.on_mqtt_message(None, None, msg())
    bridge._run_poll_cycle()
    bridge.poll_status.assert_called_once_with("SN")
    assert bridge._demand_requested == set()


def test_in_flight_poll_coalesces_rapid_refresh(bridge):
    bridge._last_status_attempt["SN"] = time.time() - 1
    bridge.poll_status = MagicMock()
    bridge.on_mqtt_message(None, None, msg())
    bridge._run_poll_cycle()
    bridge.poll_status.assert_not_called()


def test_background_poll_unchanged_without_demand(bridge):
    bridge._last_status_attempt["SN"] = time.time()
    bridge.poll_status = MagicMock(return_value=(True, 200))
    bridge._run_poll_cycle()
    bridge.poll_status.assert_called_once_with("SN")


def test_confirming_command_is_not_suppressed_by_demand_debounce(bridge):
    bridge._last_status_attempt["SN"] = time.time()
    bridge._confirm_until = time.time() + 60
    bridge.current_poll_interval = bridge.POLL_INTERVAL_CONFIRM
    bridge.poll_status = MagicMock(return_value=(True, 200))
    bridge.on_mqtt_message(None, None, msg())
    bridge._run_poll_cycle()
    bridge.poll_status.assert_called_once_with("SN")


def test_connect_subscribes_existing_commands_and_read_only_refresh(bridge):
    client = MagicMock()
    bridge.on_connect(client, None, None, 0, None)
    topics = [c.args[0] for c in client.subscribe.call_args_list]
    assert "P/SN/CMD/#" in topics
    assert "P/SN/refresh" in topics
