"""Focused regression tests for HTTP-200-but-invalid Thermowatt status payloads.

Live evidence: the Thermowatt cloud API can return HTTP 200 with
{"success": false, "error": "Water heater not found, check the Wi-Fi
connection"} — no `result` object at all. Before this fix the bridge treated
that as a successful poll: poll_status stayed "ok", consecutive_failures
stayed 0, last_successful_poll kept advancing, the invalid body was retained
to the operational STATUS topic, and a pending MODE/TEMP command was
reconciled against it (observed=None -> false "mismatched").
"""

import json
import sys
from unittest.mock import MagicMock, patch

import pytest

sys.argv = ["thermowatt_bridge.py"]

import thermowatt_bridge as bridge_module  # noqa: E402
from thermowatt_bridge import MyThermowattBridge  # noqa: E402


@pytest.fixture
def bridge(tmp_path, monkeypatch):
    monkeypatch.setattr(bridge_module, "CONFIG_FILE", str(tmp_path / "config.json"))
    with patch("thermowatt_bridge.mqtt.Client") as mock_client:
        mqtt_client = MagicMock()
        mock_client.return_value = mqtt_client
        instance = MyThermowattBridge()
        instance.mqtt_client = mqtt_client
    instance.config["devices"]["SN"] = {"name": "HWS", "last_setpoint": 60}
    return instance


def _invalid_resp():
    """HTTP 200 body observed live: device-not-found, no `result` key at all."""
    r = MagicMock()
    r.status_code = 200
    r.json.return_value = {
        "success": False,
        "error": "Water heater not found, check the Wi-Fi connection",
    }
    return r


def _valid_resp(cmd=9, setpoint=60):
    r = MagicMock()
    r.status_code = 200
    r.json.return_value = {
        "result": {
            "WaterHeaterSts": 1,
            "T_Avg": 55.0,
            "T_SetPoint": setpoint,
            "Cmd": cmd,
        }
    }
    return r


def _status_calls(bridge, sn="SN"):
    return [
        c for c in bridge.mqtt_client.publish.call_args_list
        if c.args and c.args[0] == f"P/{sn}/STATUS"
    ]


def _energy_calls(bridge, sn="SN"):
    return [
        c for c in bridge.mqtt_client.publish.call_args_list
        if c.args and c.args[0] == f"P/{sn}/energy_kwh"
    ]


def _availability_calls(bridge, payload, sn="SN"):
    return [
        c for c in bridge.mqtt_client.publish.call_args_list
        if len(c.args) >= 2 and c.args[0] == f"P/{sn}/availability" and c.args[1] == payload
    ]


def _diagnostics_payload(bridge, sn="SN"):
    calls = [
        c for c in bridge.mqtt_client.publish.call_args_list
        if c.args and c.args[0] == f"P/{sn}/diagnostics"
    ]
    assert calls
    return json.loads(calls[-1].args[1])


class TestInvalidStatusPayloadValidation:
    def test_validator_rejects_success_false(self):
        is_valid, reason = MyThermowattBridge._validate_status_payload(
            {"success": False, "error": "Water heater not found, check the Wi-Fi connection"}
        )
        assert is_valid is False
        assert "not found" in reason

    def test_validator_rejects_missing_result(self):
        is_valid, reason = MyThermowattBridge._validate_status_payload({"success": True})
        assert is_valid is False

    def test_validator_rejects_non_dict_result(self):
        is_valid, reason = MyThermowattBridge._validate_status_payload({"result": "not a dict"})
        assert is_valid is False

    def test_validator_accepts_normal_payload(self):
        is_valid, reason = MyThermowattBridge._validate_status_payload(
            {"result": {"Cmd": 9, "T_SetPoint": 60}}
        )
        assert is_valid is True
        assert reason is None

    # 1 & 2 — HTTP 200 + success=false + missing result is a failure; counter increments
    def test_invalid_200_payload_is_failure_and_increments_counter(self, bridge):
        with patch.object(bridge, "request", return_value=_invalid_resp()):
            success, status_code = bridge.poll_status("SN")

        assert success is False
        assert bridge._consecutive_failures["SN"] == 1

    # 3 — last_successful_poll does not advance
    def test_invalid_200_payload_does_not_advance_last_successful_poll(self, bridge):
        with patch.object(bridge, "request", return_value=_invalid_resp()), \
             patch("thermowatt_bridge.time.time", return_value=500.0):
            bridge.poll_status("SN")

        assert "SN" not in bridge._last_successful_poll

    # 4 — STATUS is not published from an invalid payload
    def test_invalid_200_payload_does_not_publish_status(self, bridge):
        with patch.object(bridge, "request", return_value=_invalid_resp()):
            bridge.poll_status("SN")

        assert _status_calls(bridge) == []

    # 5 — energy integration does not advance
    def test_invalid_200_payload_does_not_accumulate_energy(self, bridge):
        bridge._last_poll_ts["SN"] = 100.0
        with patch.object(bridge, "request", return_value=_invalid_resp()), \
             patch("thermowatt_bridge.time.time", return_value=200.0):
            bridge.poll_status("SN")

        assert "SN" not in bridge.config.get("energy_kwh", {})
        assert _energy_calls(bridge) == []
        # _last_poll_ts itself must not be advanced either — it anchors the
        # energy-elapsed calculation for the next *valid* poll.
        assert bridge._last_poll_ts["SN"] == 100.0

    # 6 — pending command is not reconciled from an invalid payload
    def test_invalid_200_payload_does_not_reconcile_pending_command(self, bridge):
        with patch("thermowatt_bridge.time.time", return_value=100.0):
            bridge._record_command("SN", "MODE", "Cmd", 8)

        with patch.object(bridge, "request", return_value=_invalid_resp()), \
             patch("thermowatt_bridge.time.time", return_value=105.0):
            bridge.poll_status("SN")

        record = bridge._command_state["SN"]["MODE"]
        assert record["status"] == "pending"
        assert record["fresh_poll_seen"] is False
        assert record["observed"] is None

    # 7 — after DEGRADED_THRESHOLD invalid payloads, device availability goes offline
    def test_offline_published_after_threshold_invalid_payloads(self, bridge):
        threshold = bridge.DEGRADED_THRESHOLD
        with patch.object(bridge, "request", return_value=_invalid_resp()):
            for _ in range(threshold):
                bridge.poll_status("SN")

        assert len(_availability_calls(bridge, "offline")) == 1
        assert bridge._consecutive_failures["SN"] == threshold

    def test_no_offline_before_threshold_invalid_payloads(self, bridge):
        threshold = bridge.DEGRADED_THRESHOLD
        with patch.object(bridge, "request", return_value=_invalid_resp()):
            for _ in range(threshold - 1):
                bridge.poll_status("SN")

        assert _availability_calls(bridge, "offline") == []

    # 8 — a subsequent valid payload resets failures, updates last_successful_poll,
    #     publishes STATUS, and restores availability via existing recovery behaviour
    def test_valid_payload_after_invalid_streak_recovers(self, bridge):
        threshold = bridge.DEGRADED_THRESHOLD
        with patch.object(bridge, "request", return_value=_invalid_resp()):
            for _ in range(threshold):
                bridge.poll_status("SN")
        assert bridge._consecutive_failures["SN"] == threshold

        bridge.mqtt_client.publish.reset_mock()
        with patch.object(bridge, "request", return_value=_valid_resp()), \
             patch("thermowatt_bridge.time.time", return_value=1000.0):
            success, status_code = bridge.poll_status("SN")

        assert (success, status_code) == (True, 200)
        assert bridge._consecutive_failures["SN"] == 0
        assert bridge._last_successful_poll["SN"] == 1000.0
        assert len(_status_calls(bridge)) == 1
        assert len(_availability_calls(bridge, "online")) == 1

    def test_diagnostics_reflect_invalid_payload_failure(self, bridge):
        with patch.object(bridge, "request", return_value=_invalid_resp()):
            bridge.poll_status("SN")

        diag = _diagnostics_payload(bridge)
        assert diag["consecutive_failures"] == 1
        assert "not found" in diag["last_poll_error"]

    def test_last_poll_error_clears_on_next_valid_poll(self, bridge):
        with patch.object(bridge, "request", return_value=_invalid_resp()):
            bridge.poll_status("SN")
        assert _diagnostics_payload(bridge)["last_poll_error"] is not None

        with patch.object(bridge, "request", return_value=_valid_resp()):
            bridge.poll_status("SN")
        assert _diagnostics_payload(bridge)["last_poll_error"] is None

    # 9 — ordinary valid status polling remains unchanged
    def test_ordinary_valid_poll_unaffected(self, bridge):
        with patch.object(bridge, "request", return_value=_valid_resp(cmd=3, setpoint=61)), \
             patch("thermowatt_bridge.time.time", return_value=2000.0):
            success, status_code = bridge.poll_status("SN")

        assert (success, status_code) == (True, 200)
        assert len(_status_calls(bridge)) == 1
        payload = json.loads(_status_calls(bridge)[0].args[1])
        assert payload["result"]["Cmd"] == 3
        assert bridge._consecutive_failures["SN"] == 0
        assert bridge._last_successful_poll["SN"] == 2000.0

    # 10 — HTTP non-200 and exception behaviour remain unchanged
    def test_non_200_still_counts_as_failure_with_status_code(self, bridge):
        resp = MagicMock()
        resp.status_code = 503
        with patch.object(bridge, "request", return_value=resp):
            success, status_code = bridge.poll_status("SN")

        assert (success, status_code) == (False, 503)
        assert bridge._consecutive_failures["SN"] == 1
        assert _status_calls(bridge) == []

    def test_exception_still_returns_false_none(self, bridge):
        with patch.object(bridge, "request", side_effect=RuntimeError("network down")):
            success, status_code = bridge.poll_status("SN")

        assert (success, status_code) == (False, None)
        assert bridge._consecutive_failures["SN"] == 1
        assert _status_calls(bridge) == []

    def test_invalid_payload_returns_false_none_like_exception_path(self, bridge):
        """Distinguishes the new failure class from a genuine non-200: no real
        status code is available to act on, matching the exception path's
        existing (False, None) contract rather than triggering re-login/429
        handling in the run() loop for what is not an HTTP-level problem."""
        with patch.object(bridge, "request", return_value=_invalid_resp()):
            success, status_code = bridge.poll_status("SN")

        assert (success, status_code) == (False, None)
