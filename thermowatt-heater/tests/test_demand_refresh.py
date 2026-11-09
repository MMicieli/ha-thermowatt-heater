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


# --- review fixes: multi-device side effects, attempted-vs-fresh, retry -------

def _two_devices(bridge):
    bridge.config["devices"]["SN2"] = {"name": "HWS2", "last_setpoint": 58}


def test_demand_for_one_device_does_not_poll_a_recently_polled_other_device(bridge):
    _two_devices(bridge)
    now = time.time()
    bridge._last_status_attempt["SN2"] = now - 5   # not due for a 60 s background poll
    bridge.poll_status = MagicMock(return_value=(True, 200))
    bridge.on_mqtt_message(None, None, msg("SN"))
    bridge._run_poll_cycle()
    bridge.poll_status.assert_called_once_with("SN")


def test_attempt_that_failed_is_not_reported_as_satisfying_the_request(bridge, capsys):
    bridge._last_status_attempt["SN"] = time.time() - 1
    bridge._last_successful_poll["SN"] = time.time() - 400   # attempt did not produce telemetry
    bridge.poll_status = MagicMock()
    bridge.on_mqtt_message(None, None, msg())
    bridge._run_poll_cycle()
    bridge.poll_status.assert_not_called()                    # still storm-guarded ...
    assert "gave no fresh telemetry" in capsys.readouterr().out    # ... but honestly reported


def test_fresh_success_is_reported_as_satisfying_the_request(bridge, capsys):
    bridge._last_status_attempt["SN"] = time.time() - 1
    bridge._last_successful_poll["SN"] = time.time() - 1
    bridge.poll_status = MagicMock()
    bridge.on_mqtt_message(None, None, msg())
    bridge._run_poll_cycle()
    out = capsys.readouterr().out
    assert "gave fresh telemetry" in out and "no fresh" not in out


def _json_resp(code=200, body=None):
    r = MagicMock(status_code=code)
    r.json.return_value = body if body is not None else {}
    return r


GOOD = {"result": {"WaterHeaterSts": 1, "T_Avg": 55.0, "T_SetPoint": 60.0, "Cmd": 9}}


def _polls(bridge, session_effect):
    """Real request()/poll_status(); only the HTTP session is replaced."""
    bridge.DEMAND_RETRY_DELAY = 0
    bridge.session.request = MagicMock(side_effect=session_effect)
    bridge.on_mqtt_message(None, None, msg())
    with patch.object(bridge, "_save_config"):
        bridge._run_poll_cycle()
    return bridge.session.request.call_count


def test_real_transport_failures_retry_exactly_once(bridge):
    import requests
    assert _polls(bridge, requests.Timeout("read timed out")) == 2
    bridge._last_status_attempt.clear()
    assert _polls(bridge, requests.ConnectionError("reset")) == 2


def test_answered_invalid_200_is_not_retried(bridge):
    assert _polls(bridge, lambda *a, **k: _json_resp(200, {"success": True, "result": {}})) == 1


def test_answered_http_error_is_not_retried(bridge):
    bridge.login = MagicMock()
    assert _polls(bridge, lambda *a, **k: _json_resp(503)) == 1


def test_local_publish_failure_after_a_valid_response_is_not_retried(bridge):
    bridge.mqtt_client.publish.side_effect = RuntimeError("mqtt down")
    assert _polls(bridge, lambda *a, **k: _json_resp(200, GOOD)) == 1


def test_explicit_quota_response_is_not_retried(bridge):
    assert _polls(bridge, lambda *a, **k: _json_resp(
        200, {"success": False, "error": "Daily request limit exceeded"})) == 1


def test_http_error_or_quota_response_is_not_retried(bridge):
    bridge.DEMAND_RETRY_DELAY = 0
    bridge.login = MagicMock()
    bridge.poll_status = MagicMock(return_value=(False, 500))
    bridge.on_mqtt_message(None, None, msg())
    bridge._run_poll_cycle()
    assert bridge.poll_status.call_count == 1

    def quota(serial):
        bridge._rate_limit_until = time.time() + 120
        bridge.rate_limit_backoff = 1
        return (False, None)
    bridge.poll_status = MagicMock(side_effect=quota)
    bridge._rate_limit_until = 0
    bridge.rate_limit_backoff = 0
    bridge._last_status_attempt.clear()
    bridge.on_mqtt_message(None, None, msg())
    bridge._run_poll_cycle()
    assert bridge.poll_status.call_count == 1


def test_background_poll_failure_without_demand_is_not_retried(bridge):
    bridge.poll_status = MagicMock(return_value=(False, None))
    bridge._run_poll_cycle()
    assert bridge.poll_status.call_count == 1


def test_repeated_requests_send_at_most_one_request_per_spacing_window(bridge):
    bridge.poll_status = MagicMock(return_value=(True, 200))
    for _ in range(50):
        bridge.on_mqtt_message(None, None, msg())
    bridge._run_poll_cycle()
    for _ in range(50):                     # a second burst moments later
        bridge.on_mqtt_message(None, None, msg())
    bridge._run_poll_cycle()
    assert bridge.poll_status.call_count == 1
