"""Focused regression tests for bounded command readback confirmation."""

import json
import sys
from unittest.mock import MagicMock, patch

import pytest
import requests

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


def _response(code=200, *, cmd=9, setpoint=60):
    response = MagicMock()
    response.status_code = code
    response.json.return_value = {
        "result": {
            "WaterHeaterSts": 1,
            "T_Avg": 55.0,
            "T_SetPoint": setpoint,
            "Cmd": cmd,
        }
    }
    return response


def _message(topic, payload):
    msg = MagicMock()
    msg.retain = False
    msg.topic = topic
    msg.payload = payload.encode()
    return msg


def _diagnostics_payload(bridge, serial="SN"):
    calls = [
        call for call in bridge.mqtt_client.publish.call_args_list
        if call.args and call.args[0] == f"P/{serial}/diagnostics"
    ]
    assert calls
    return json.loads(calls[-1].args[1])


class TestCommandConfirmation:
    def test_successful_submission_is_pending_without_synthetic_status(self, bridge):
        msg = _message("P/SN/CMD/MODE", "Eco")

        with patch.object(bridge, "request", return_value=_response()) as request, \
             patch("thermowatt_bridge.time.time", return_value=100.0):
            bridge.on_mqtt_message(None, None, msg)

        request.assert_called_once()
        assert bridge._command_state["SN"]["MODE"]["status"] == "pending"
        status_calls = [
            call for call in bridge.mqtt_client.publish.call_args_list
            if call.args and call.args[0] == "P/SN/STATUS"
        ]
        assert status_calls == []
        assert _diagnostics_payload(bridge)["command_status"] == "pending"

    def test_off_submission_expects_family_off_and_confirms(self, bridge):
        """Off confirmation is family-based (known family, bit0 cleared), not an
        exact Cmd=8 match — see issue #13. The submitted request is unchanged
        (still POST /off, no body)."""
        msg = _message("P/SN/CMD/MODE", "Off")

        with patch.object(bridge, "request", return_value=_response(cmd=8)) as request, \
             patch("thermowatt_bridge.time.time", return_value=100.0):
            bridge.on_mqtt_message(None, None, msg)

        request.assert_called_once_with(
            "POST",
            "/off",
            serial="SN",
            headers={"Content-Type": "text/plain"},
            data="",
        )
        record = bridge._command_state["SN"]["MODE"]
        assert record["requested"] == bridge.CMD_OFF_SENTINEL
        assert record["status"] == "pending"

        bridge._reconcile_pending_commands(
            "SN", {"result": {"Cmd": "8"}}, poll_started_at=101.0, now=102.0
        )
        assert bridge._command_state["SN"]["MODE"]["status"] == "confirmed"

    @pytest.mark.parametrize("fresh_cmd", ["8", "16", "64"])
    def test_off_confirms_for_any_known_disabled_family(self, bridge, fresh_cmd):
        """A fresh Cmd belonging to ANY recognised family with bit0 cleared confirms
        Off — not only the family that happened to be active before the command."""
        with patch("thermowatt_bridge.time.time", return_value=100.0):
            bridge._record_command("SN", "MODE", "Cmd", bridge.CMD_OFF_SENTINEL)

        bridge._reconcile_pending_commands(
            "SN", {"result": {"Cmd": fresh_cmd}}, poll_started_at=101.0, now=102.0
        )
        assert bridge._command_state["SN"]["MODE"]["status"] == "confirmed"

    @pytest.mark.parametrize("fresh_cmd", ["3", "9", "17", "65"])
    def test_off_does_not_confirm_for_enabled_cmd(self, bridge, fresh_cmd):
        with patch("thermowatt_bridge.time.time", return_value=100.0):
            bridge._record_command("SN", "MODE", "Cmd", bridge.CMD_OFF_SENTINEL)

        bridge._reconcile_pending_commands(
            "SN", {"result": {"Cmd": fresh_cmd}}, poll_started_at=101.0, now=120.0
        )
        record = bridge._command_state["SN"]["MODE"]
        assert record["status"] == "pending"
        assert record["fresh_poll_seen"] is True

    def test_off_does_not_confirm_for_unknown_cmd(self, bridge):
        with patch("thermowatt_bridge.time.time", return_value=100.0):
            bridge._record_command("SN", "MODE", "Cmd", bridge.CMD_OFF_SENTINEL)

        bridge._reconcile_pending_commands(
            "SN", {"result": {"Cmd": "99"}}, poll_started_at=101.0, now=120.0
        )
        record = bridge._command_state["SN"]["MODE"]
        assert record["status"] == "pending"
        assert record["fresh_poll_seen"] is True

        summary, commands = bridge._command_diagnostics("SN", now=161.0)
        assert commands["MODE"]["status"] == "mismatched"

    def test_pending_record_exists_before_poll_wakeup(self, bridge):
        def assert_pending_before_wakeup():
            record = bridge._command_state["SN"]["MODE"]
            assert record["status"] == "pending"
            assert record["requested"] == 3

        with patch.object(bridge._poll_wakeup, "set", side_effect=assert_pending_before_wakeup) as wake, \
             patch("thermowatt_bridge.time.time", return_value=100.0):
            bridge._record_command("SN", "MODE", "Cmd", 3)

        wake.assert_called_once_with()

    def test_poll_started_before_submission_cannot_confirm(self, bridge):
        with patch("thermowatt_bridge.time.time", return_value=100.0):
            bridge._record_command("SN", "MODE", "Cmd", 3)

        bridge._reconcile_pending_commands(
            "SN", {"result": {"Cmd": 3}}, poll_started_at=99.0, now=101.0
        )
        record = bridge._command_state["SN"]["MODE"]
        assert record["status"] == "pending"
        assert record["fresh_poll_seen"] is False

    def test_newer_matching_poll_confirms_mode(self, bridge):
        with patch("thermowatt_bridge.time.time", return_value=100.0):
            bridge._record_command("SN", "MODE", "Cmd", 17)

        bridge._reconcile_pending_commands(
            "SN", {"result": {"Cmd": "17"}}, poll_started_at=101.0, now=102.0
        )
        assert bridge._command_state["SN"]["MODE"]["status"] == "confirmed"

    def test_newer_matching_poll_confirms_temp_and_persists_confirmed_value(self, bridge):
        with patch("thermowatt_bridge.time.time", return_value=100.0):
            bridge._record_command("SN", "TEMP", "T_SetPoint", 55)

        with patch.object(bridge, "_save_config") as save:
            bridge._reconcile_pending_commands(
                "SN",
                {"result": {"T_SetPoint": "55.0"}},
                poll_started_at=101.0,
                now=102.0,
            )

        assert bridge._command_state["SN"]["TEMP"]["status"] == "confirmed"
        assert bridge.config["devices"]["SN"]["last_setpoint"] == 55.0
        save.assert_called_once()

    def test_mismatch_before_deadline_remains_pending(self, bridge):
        with patch("thermowatt_bridge.time.time", return_value=100.0):
            bridge._record_command("SN", "TEMP", "T_SetPoint", 55)

        bridge._reconcile_pending_commands(
            "SN",
            {"result": {"T_SetPoint": 50}},
            poll_started_at=101.0,
            now=120.0,
        )
        record = bridge._command_state["SN"]["TEMP"]
        assert record["status"] == "pending"
        assert record["fresh_poll_seen"] is True
        assert record["observed"] == 50

    def test_fresh_mismatch_becomes_mismatched_at_deadline(self, bridge):
        with patch("thermowatt_bridge.time.time", return_value=100.0):
            bridge._record_command("SN", "TEMP", "T_SetPoint", 55)

        bridge._reconcile_pending_commands(
            "SN",
            {"result": {"T_SetPoint": 50}},
            poll_started_at=101.0,
            now=120.0,
        )
        summary, commands = bridge._command_diagnostics("SN", now=161.0)
        assert summary == "mismatched"
        assert commands["TEMP"]["status"] == "mismatched"

    def test_no_fresh_poll_becomes_timed_out(self, bridge):
        with patch("thermowatt_bridge.time.time", return_value=100.0):
            bridge._record_command("SN", "MODE", "Cmd", 16)

        summary, commands = bridge._command_diagnostics("SN", now=161.0)
        assert summary == "timed_out"
        assert commands["MODE"]["status"] == "timed_out"

    def test_late_matching_poll_does_not_override_timeout(self, bridge):
        with patch("thermowatt_bridge.time.time", return_value=100.0):
            bridge._record_command("SN", "MODE", "Cmd", 16)

        bridge._reconcile_pending_commands(
            "SN", {"result": {"Cmd": 16}}, poll_started_at=161.0, now=162.0
        )
        assert bridge._command_state["SN"]["MODE"]["status"] == "timed_out"

    def test_mode_and_temp_confirm_independently_from_same_poll(self, bridge):
        with patch("thermowatt_bridge.time.time", return_value=100.0):
            bridge._record_command("SN", "MODE", "Cmd", 9)
            bridge._record_command("SN", "TEMP", "T_SetPoint", 55)

        with patch.object(bridge, "_save_config"):
            bridge._reconcile_pending_commands(
                "SN",
                {"result": {"Cmd": 9, "T_SetPoint": 55}},
                poll_started_at=101.0,
                now=102.0,
            )

        assert bridge._command_state["SN"]["MODE"]["status"] == "confirmed"
        assert bridge._command_state["SN"]["TEMP"]["status"] == "confirmed"

    def test_non_2xx_submission_is_failed_not_pending(self, bridge):
        msg = _message("P/SN/CMD/MODE", "Eco")

        with patch.object(bridge, "request", return_value=_response(503)) as request, \
             patch("thermowatt_bridge.time.time", return_value=100.0):
            bridge.on_mqtt_message(None, None, msg)

        request.assert_called_once()
        record = bridge._command_state["SN"]["MODE"]
        assert record["status"] == "submit_failed"
        assert record["error"] == "503"
        assert "MODE" not in bridge._last_cmd_time.get("SN", {})

    def test_request_exception_is_submit_failed_without_retry_or_cooldown(self, bridge):
        msg = _message("P/SN/CMD/MODE", "Eco")

        with patch.object(
            bridge, "request", side_effect=requests.Timeout("cloud read timed out")
        ) as request, patch("thermowatt_bridge.time.time", return_value=100.0):
            bridge.on_mqtt_message(None, None, msg)

        request.assert_called_once()
        record = bridge._command_state["SN"]["MODE"]
        assert record["status"] == "submit_failed"
        assert "Timeout" in record["error"]
        assert "MODE" not in bridge._last_cmd_time.get("SN", {})
        status_calls = [
            call for call in bridge.mqtt_client.publish.call_args_list
            if call.args and call.args[0] == "P/SN/STATUS"
        ]
        assert status_calls == []

    def test_timeout_processing_never_republishes_command(self, bridge):
        with patch("thermowatt_bridge.time.time", return_value=100.0):
            bridge._record_command("SN", "MODE", "Cmd", 3)

        bridge.mqtt_client.publish.reset_mock()
        bridge._command_diagnostics("SN", now=161.0)
        published_topics = [call.args[0] for call in bridge.mqtt_client.publish.call_args_list]
        assert all("/CMD/" not in topic for topic in published_topics)

    def test_command_status_discovery_is_diagnostic_and_bridge_available(self, bridge):
        bridge.mqtt_client.publish.reset_mock()
        bridge.publish_discovery("SN", "HWS")

        calls = [
            call for call in bridge.mqtt_client.publish.call_args_list
            if call.args and call.args[0].endswith("/command_status/config")
        ]
        assert len(calls) == 1
        payload = json.loads(calls[0].args[1])
        assert payload["entity_category"] == "diagnostic"
        assert payload["availability_topic"] == bridge_module.AVAILABILITY_TOPIC
        assert "availability" not in payload
        assert payload["json_attributes_topic"] == "P/SN/diagnostics"

    def test_poll_status_publishes_only_real_polled_state(self, bridge):
        with patch("thermowatt_bridge.time.time", return_value=100.0):
            bridge._record_command("SN", "MODE", "Cmd", 3)
        bridge.mqtt_client.publish.reset_mock()

        with patch.object(bridge, "request", return_value=_response(cmd=3)), \
             patch("thermowatt_bridge.time.time", side_effect=[101.0, 102.0, 103.0]):
            success, code = bridge.poll_status("SN")

        assert (success, code) == (True, 200)
        status_calls = [
            call for call in bridge.mqtt_client.publish.call_args_list
            if call.args and call.args[0] == "P/SN/STATUS"
        ]
        assert len(status_calls) == 1
        payload = json.loads(status_calls[0].args[1])
        assert payload["result"]["Cmd"] == 3
        assert "last_polled_at" in payload["result"]
        assert bridge._command_state["SN"]["MODE"]["status"] == "confirmed"

    def test_temp_cache_persistence_failure_does_not_fail_real_status_poll(self, bridge):
        with patch("thermowatt_bridge.time.time", return_value=100.0):
            bridge._record_command("SN", "TEMP", "T_SetPoint", 55)
        bridge.mqtt_client.publish.reset_mock()

        with patch.object(bridge, "request", return_value=_response(setpoint=55)), \
             patch.object(bridge, "_save_config", side_effect=OSError("disk unavailable")), \
             patch("thermowatt_bridge.time.time", side_effect=[101.0, 102.0, 103.0]):
            success, code = bridge.poll_status("SN")

        assert (success, code) == (True, 200)
        assert bridge._command_state["SN"]["TEMP"]["status"] == "confirmed"
        assert bridge.config["devices"]["SN"]["last_setpoint"] == 55.0
        status_calls = [
            call for call in bridge.mqtt_client.publish.call_args_list
            if call.args and call.args[0] == "P/SN/STATUS"
        ]
        assert len(status_calls) == 1

    def test_successful_submission_wakes_poll_loop(self, bridge):
        assert bridge._poll_wakeup.is_set() is False
        with patch("thermowatt_bridge.time.time", return_value=100.0):
            bridge._record_command("SN", "MODE", "Cmd", 3)
        assert bridge._poll_wakeup.is_set() is True
