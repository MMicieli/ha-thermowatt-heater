"""
Tests for thermowatt_bridge.py — v1.6.3 bridge-hardening items.

Covers:
  - Configurable element wattage (default, custom, invalid, boundary)
  - Sustained poll-failure degraded/offline handling
  - Per-command-type cooldown independence (MODE vs TEMP)
  - Unknown command/state hardening (mode_state_template, unknown MODE payload)
  - Diagnostics fields published correctly
"""

import json
import sys
import time
from unittest.mock import MagicMock, patch

import pytest

# Keep sys.argv minimal — EMAIL/PASSWORD are not needed for unit tests.
sys.argv = ["thermowatt_bridge.py"]

import thermowatt_bridge as bridge_module  # noqa: E402
from thermowatt_bridge import MyThermowattBridge, _load_element_wattage  # noqa: E402


# ─────────────────────────────────────────────────────────────────────────────
# Fixtures
# ─────────────────────────────────────────────────────────────────────────────

@pytest.fixture
def bridge(tmp_path, monkeypatch):
    """Isolated bridge instance: mocked MQTT, temp config file, no ELEMENT_WATTAGE env."""
    monkeypatch.setattr(bridge_module, "CONFIG_FILE", str(tmp_path / "config.json"))
    monkeypatch.delenv("ELEMENT_WATTAGE", raising=False)

    with patch("thermowatt_bridge.mqtt.Client") as MockClient:
        mock_mqtt = MagicMock()
        MockClient.return_value = mock_mqtt
        b = MyThermowattBridge()
        b.mqtt_client = mock_mqtt
    return b


def _success_resp():
    r = MagicMock()
    r.status_code = 200
    r.json.return_value = {
        "result": {
            "WaterHeaterSts": 1,
            "T_Avg": 55.0,
            "T_SetPoint": 60.0,
            "Cmd": 9,
        }
    }
    return r


def _fail_resp(code=503):
    r = MagicMock()
    r.status_code = code
    r.json.return_value = {}
    return r


def _offline_calls(bridge, sn):
    return [
        c for c in bridge.mqtt_client.publish.call_args_list
        if len(c.args) >= 2
        and c.args[0] == f"P/{sn}/availability"
        and c.args[1] == "offline"
    ]


def _online_calls(bridge, sn):
    return [
        c for c in bridge.mqtt_client.publish.call_args_list
        if len(c.args) >= 2
        and c.args[0] == f"P/{sn}/availability"
        and c.args[1] == "online"
    ]


def _discovery_payload(bridge, topic_fragment):
    """Return the parsed JSON from the first discovery publish matching topic_fragment."""
    bridge.mqtt_client.publish.reset_mock()
    bridge.publish_discovery("SN_TEST", "TestBoiler")
    for c in bridge.mqtt_client.publish.call_args_list:
        if len(c.args) >= 2 and topic_fragment in c.args[0]:
            return json.loads(c.args[1])
    return None


# ─────────────────────────────────────────────────────────────────────────────
# 1. Configurable element wattage
# ─────────────────────────────────────────────────────────────────────────────

class TestElementWattage:
    def test_default_is_3000(self, monkeypatch):
        monkeypatch.delenv("ELEMENT_WATTAGE", raising=False)
        assert _load_element_wattage() == 3000

    def test_custom_wattage(self, monkeypatch):
        monkeypatch.setenv("ELEMENT_WATTAGE", "2400")
        assert _load_element_wattage() == 2400

    def test_invalid_string_defaults_to_3000(self, monkeypatch):
        monkeypatch.setenv("ELEMENT_WATTAGE", "notanumber")
        assert _load_element_wattage() == 3000

    def test_float_string_defaults_to_3000(self, monkeypatch):
        # int() rejects floats — "2400.5" must fall back to 3000
        monkeypatch.setenv("ELEMENT_WATTAGE", "2400.5")
        assert _load_element_wattage() == 3000

    def test_below_min_defaults_to_3000(self, monkeypatch):
        monkeypatch.setenv("ELEMENT_WATTAGE", "50")
        assert _load_element_wattage() == 3000

    def test_above_max_defaults_to_3000(self, monkeypatch):
        monkeypatch.setenv("ELEMENT_WATTAGE", "15000")
        assert _load_element_wattage() == 3000

    def test_boundary_min_accepted(self, monkeypatch):
        monkeypatch.setenv("ELEMENT_WATTAGE", "100")
        assert _load_element_wattage() == 100

    def test_boundary_max_accepted(self, monkeypatch):
        monkeypatch.setenv("ELEMENT_WATTAGE", "10000")
        assert _load_element_wattage() == 10000

    def test_bridge_default_wattage_is_3000(self, bridge):
        assert bridge.element_wattage == 3000

    def test_energy_uses_configured_wattage(self, bridge):
        """Custom wattage changes the kWh energy increment.
        Uses a frozen clock so the energy cap (2×POLL_INTERVAL) is deterministic.
        """
        sn = "SN_WATT"
        bridge.element_wattage = 2400
        bridge.config["devices"][sn] = {"name": "Test", "last_setpoint": 60}

        t_now = 1_000_000.0
        # Elapsed = POLL_INTERVAL (60s), within the 2×POLL_INTERVAL (120s) cap
        bridge._last_poll_ts[sn] = t_now - bridge.POLL_INTERVAL

        with patch.object(bridge, "request", return_value=_success_resp()), \
             patch.object(bridge, "_save_config"), \
             patch("thermowatt_bridge.time.time", return_value=t_now):
            bridge.poll_status(sn)

        kwh = bridge.config.get("energy_kwh", {}).get(sn, 0.0)
        # elapsed_s = min(60, 120) = 60s; 2400 W × (60/3600) h = 0.04 kWh
        expected = (2400 / 1000.0) * (bridge.POLL_INTERVAL / 3600.0)
        assert abs(kwh - expected) < 0.001

    def test_default_3000w_energy(self, bridge):
        """Default 3000 W produces the correct kWh for one poll interval.
        Uses a frozen clock so the energy cap (2×POLL_INTERVAL) is deterministic.
        """
        sn = "SN_DEF"
        bridge.config["devices"][sn] = {"name": "Test", "last_setpoint": 60}

        t_now = 1_000_000.0
        bridge._last_poll_ts[sn] = t_now - bridge.POLL_INTERVAL

        with patch.object(bridge, "request", return_value=_success_resp()), \
             patch.object(bridge, "_save_config"), \
             patch("thermowatt_bridge.time.time", return_value=t_now):
            bridge.poll_status(sn)

        kwh = bridge.config.get("energy_kwh", {}).get(sn, 0.0)
        # elapsed_s = min(60, 120) = 60s; 3000 W × (60/3600) h = 0.05 kWh
        expected = (3000 / 1000.0) * (bridge.POLL_INTERVAL / 3600.0)
        assert abs(kwh - expected) < 0.001

    def test_power_sensor_embeds_configured_wattage(self, bridge):
        """Power sensor value_template must contain the configured wattage."""
        bridge.element_wattage = 2400
        payload = _discovery_payload(bridge, "/power/config")
        assert payload is not None
        assert "2400" in payload["value_template"]

    def test_power_sensor_default_3000(self, bridge):
        bridge.element_wattage = 3000
        payload = _discovery_payload(bridge, "/power/config")
        assert "3000" in payload["value_template"]


# ─────────────────────────────────────────────────────────────────────────────
# 2. Sustained poll-failure degraded/offline handling
# ─────────────────────────────────────────────────────────────────────────────

class TestPollFailureDegraded:
    def _poll_n(self, bridge, sn, n, resp):
        with patch.object(bridge, "request", return_value=resp):
            for _ in range(n):
                bridge.poll_status(sn)

    def test_counter_increments_on_each_failure(self, bridge):
        sn = "SN_CNT"
        bridge.config["devices"][sn] = {"name": "Test", "last_setpoint": 60}
        self._poll_n(bridge, sn, 3, _fail_resp())
        assert bridge._consecutive_failures.get(sn, 0) == 3

    def test_no_offline_before_threshold(self, bridge):
        sn = "SN_NOFF"
        bridge.config["devices"][sn] = {"name": "Test", "last_setpoint": 60}
        threshold = bridge.DEGRADED_THRESHOLD
        self._poll_n(bridge, sn, threshold - 1, _fail_resp())
        assert not _offline_calls(bridge, sn)

    def test_offline_published_at_threshold(self, bridge):
        sn = "SN_OFF"
        bridge.config["devices"][sn] = {"name": "Test", "last_setpoint": 60}
        threshold = bridge.DEGRADED_THRESHOLD
        self._poll_n(bridge, sn, threshold, _fail_resp())
        assert len(_offline_calls(bridge, sn)) == 1

    def test_offline_not_published_again_above_threshold(self, bridge):
        """Once offline is published, subsequent failures must not re-publish it."""
        sn = "SN_ONCE"
        bridge.config["devices"][sn] = {"name": "Test", "last_setpoint": 60}
        threshold = bridge.DEGRADED_THRESHOLD
        self._poll_n(bridge, sn, threshold + 3, _fail_resp())
        assert len(_offline_calls(bridge, sn)) == 1

    def test_success_resets_counter(self, bridge):
        sn = "SN_RST"
        bridge.config["devices"][sn] = {"name": "Test", "last_setpoint": 60}
        bridge._consecutive_failures[sn] = bridge.DEGRADED_THRESHOLD

        with patch.object(bridge, "request", return_value=_success_resp()), \
             patch.object(bridge, "_save_config"):
            bridge.poll_status(sn)

        assert bridge._consecutive_failures.get(sn, 0) == 0

    def test_recovery_publishes_online(self, bridge):
        sn = "SN_RCV"
        bridge.config["devices"][sn] = {"name": "Test", "last_setpoint": 60}
        bridge._consecutive_failures[sn] = bridge.DEGRADED_THRESHOLD

        with patch.object(bridge, "request", return_value=_success_resp()), \
             patch.object(bridge, "_save_config"):
            bridge.poll_status(sn)

        assert len(_online_calls(bridge, sn)) >= 1

    def test_no_online_published_when_not_degraded(self, bridge):
        """Recovery online must only fire when prev_failures >= DEGRADED_THRESHOLD."""
        sn = "SN_NON"
        bridge.config["devices"][sn] = {"name": "Test", "last_setpoint": 60}
        bridge._consecutive_failures[sn] = 2  # below threshold

        with patch.object(bridge, "request", return_value=_success_resp()), \
             patch.object(bridge, "_save_config"):
            bridge.poll_status(sn)

        assert not _online_calls(bridge, sn)

    def test_diagnostics_show_degraded(self, bridge):
        sn = "SN_DGRP"
        bridge.config["devices"][sn] = {"name": "Test", "last_setpoint": 60}
        self._poll_n(bridge, sn, bridge.DEGRADED_THRESHOLD, _fail_resp())

        diag_calls = [
            c for c in bridge.mqtt_client.publish.call_args_list
            if len(c.args) >= 2 and f"P/{sn}/diagnostics" == c.args[0]
        ]
        assert diag_calls
        last = json.loads(diag_calls[-1].args[1])
        assert last["poll_status"] == "degraded"
        assert last["consecutive_failures"] >= bridge.DEGRADED_THRESHOLD

    def test_diagnostics_show_ok_after_success(self, bridge):
        sn = "SN_DOK"
        bridge.config["devices"][sn] = {"name": "Test", "last_setpoint": 60}

        with patch.object(bridge, "request", return_value=_success_resp()), \
             patch.object(bridge, "_save_config"):
            bridge.poll_status(sn)

        diag_calls = [
            c for c in bridge.mqtt_client.publish.call_args_list
            if len(c.args) >= 2 and f"P/{sn}/diagnostics" == c.args[0]
        ]
        assert diag_calls
        last = json.loads(diag_calls[-1].args[1])
        assert last["poll_status"] == "ok"
        assert last["consecutive_failures"] == 0

    def test_diagnostics_include_element_wattage(self, bridge):
        sn = "SN_DW"
        bridge.element_wattage = 2400
        bridge.config["devices"][sn] = {"name": "Test", "last_setpoint": 60}

        with patch.object(bridge, "request", return_value=_success_resp()), \
             patch.object(bridge, "_save_config"):
            bridge.poll_status(sn)

        diag_calls = [
            c for c in bridge.mqtt_client.publish.call_args_list
            if len(c.args) >= 2 and f"P/{sn}/diagnostics" == c.args[0]
        ]
        last = json.loads(diag_calls[-1].args[1])
        assert last["element_wattage"] == 2400

    def test_diagnostics_include_poll_interval(self, bridge):
        sn = "SN_DPI"
        bridge.config["devices"][sn] = {"name": "Test", "last_setpoint": 60}

        with patch.object(bridge, "request", return_value=_success_resp()), \
             patch.object(bridge, "_save_config"):
            bridge.poll_status(sn)

        diag_calls = [
            c for c in bridge.mqtt_client.publish.call_args_list
            if len(c.args) >= 2 and f"P/{sn}/diagnostics" == c.args[0]
        ]
        last = json.loads(diag_calls[-1].args[1])
        assert "poll_interval" in last
        assert isinstance(last["poll_interval"], int)


# ─────────────────────────────────────────────────────────────────────────────
# 3. Per-command-type cooldown
# ─────────────────────────────────────────────────────────────────────────────

class TestPerTypeCooldown:
    def test_no_cooldown_initially(self, bridge):
        sn = "SN_NEW"
        assert bridge._check_cooldown(sn, "MODE") is True
        assert bridge._check_cooldown(sn, "TEMP") is True

    def test_mode_does_not_block_temp(self, bridge):
        """Recording a MODE command must not block a subsequent TEMP command."""
        sn = "SN_MT"
        bridge._record_command(sn, "MODE")
        assert bridge._check_cooldown(sn, "MODE") is False
        assert bridge._check_cooldown(sn, "TEMP") is True

    def test_temp_does_not_block_mode(self, bridge):
        sn = "SN_TM"
        bridge._record_command(sn, "TEMP")
        assert bridge._check_cooldown(sn, "TEMP") is False
        assert bridge._check_cooldown(sn, "MODE") is True

    def test_both_blocked_when_both_recorded(self, bridge):
        sn = "SN_BOTH"
        bridge._record_command(sn, "MODE")
        bridge._record_command(sn, "TEMP")
        assert bridge._check_cooldown(sn, "MODE") is False
        assert bridge._check_cooldown(sn, "TEMP") is False

    def test_cooldown_passes_after_expiry(self, bridge):
        sn = "SN_EXP"
        # Backdate the record so cooldown has already elapsed
        bridge._last_cmd_time[sn] = {"MODE": time.time() - bridge.CMD_COOLDOWN - 1}
        assert bridge._check_cooldown(sn, "MODE") is True

    def test_independent_serials(self, bridge):
        """Cooldown on one serial must not affect a different serial."""
        bridge._record_command("SN_A", "MODE")
        assert bridge._check_cooldown("SN_B", "MODE") is True

    def test_mode_mqtt_command_not_blocked_by_prior_temp(self, bridge):
        """Integration: an on_mqtt_message MODE command proceeds if only TEMP was cooled."""
        sn = "SN_SEQ"
        bridge.config["devices"][sn] = {"name": "Test", "last_setpoint": 60}
        bridge._last_cmd_time[sn] = {"TEMP": time.time()}  # TEMP in cooldown

        msg = MagicMock()
        msg.retain  = False
        msg.topic   = f"P/{sn}/CMD/MODE"
        msg.payload = b"Eco"

        mock_resp = MagicMock()
        mock_resp.status_code = 200

        with patch.object(bridge, "request", return_value=mock_resp) as mock_req:
            bridge.on_mqtt_message(None, None, msg)
            mock_req.assert_called_once()  # MODE proceeds despite TEMP cooldown

    def test_temp_mqtt_command_not_blocked_by_prior_mode(self, bridge):
        """Integration: an on_mqtt_message TEMP command proceeds if only MODE was cooled."""
        sn = "SN_SEQ2"
        bridge.config["devices"][sn] = {"name": "Test", "last_setpoint": 60}
        bridge._last_cmd_time[sn] = {"MODE": time.time()}  # MODE in cooldown

        msg = MagicMock()
        msg.retain  = False
        msg.topic   = f"P/{sn}/CMD/TEMP"
        msg.payload = b"60"

        mock_resp = MagicMock()
        mock_resp.status_code = 200

        with patch.object(bridge, "request", return_value=mock_resp) as mock_req, \
             patch.object(bridge, "_save_config"):
            bridge.on_mqtt_message(None, None, msg)
            mock_req.assert_called_once()  # TEMP proceeds despite MODE cooldown


# ─────────────────────────────────────────────────────────────────────────────
# 4. Unknown command / state hardening
# ─────────────────────────────────────────────────────────────────────────────

class TestUnknownStateHardening:
    def test_mode_state_template_does_not_fallback_to_off(self, bridge):
        """The else clause must NOT map unknown Cmd values to 'Off'."""
        payload = _discovery_payload(bridge, "water_heater")
        assert payload is not None
        template = payload["mode_state_template"]
        # Neither 'else %}Off' nor 'else%}Off' variants
        assert "else %}Off" not in template
        assert "else%}Off" not in template

    def test_mode_state_template_maps_all_known_modes(self, bridge):
        payload = _discovery_payload(bridge, "water_heater")
        template = payload["mode_state_template"]
        for mode in ("Manual", "Eco", "Auto", "Holiday", "Off"):
            assert mode in template, f"Expected '{mode}' in mode_state_template"

    def test_mode_state_template_off_is_cmd_16_only(self, bridge):
        """'Off' must appear in the cmd==16 branch, not as a catch-all fallback."""
        payload = _discovery_payload(bridge, "water_heater")
        template = payload["mode_state_template"]
        # The pattern 'int(-1) == 16 %}Off' (or similar) must be present
        assert "16" in template
        # And the else branch returns empty string (no text between %} and {% endif %})
        assert "{% else %}{% endif %}" in template

    def test_unknown_mode_payload_no_api_call(self, bridge):
        """An unrecognised MODE payload must not trigger any REST API call."""
        sn = "SN_UNK"
        bridge.config["devices"][sn] = {"name": "Test", "last_setpoint": 60}

        msg = MagicMock()
        msg.retain  = False
        msg.topic   = f"P/{sn}/CMD/MODE"
        msg.payload = b"Vacation"

        with patch.object(bridge, "request") as mock_req:
            bridge.on_mqtt_message(None, None, msg)
            mock_req.assert_not_called()

    def test_unknown_cmd_topic_ignored(self, bridge):
        """An unrecognised CMD subtopic (not TEMP or MODE) must be dropped silently."""
        sn = "SN_CTOPIC"
        bridge.config["devices"][sn] = {"name": "Test", "last_setpoint": 60}

        msg = MagicMock()
        msg.retain  = False
        msg.topic   = f"P/{sn}/CMD/SCHEDULE"
        msg.payload = b"on"

        with patch.object(bridge, "request") as mock_req:
            bridge.on_mqtt_message(None, None, msg)
            mock_req.assert_not_called()

    def test_retained_command_ignored(self, bridge):
        """Retained CMD messages must be dropped (anti-replay on reconnect)."""
        sn = "SN_RET"
        bridge.config["devices"][sn] = {"name": "Test", "last_setpoint": 60}

        msg = MagicMock()
        msg.retain  = True
        msg.topic   = f"P/{sn}/CMD/MODE"
        msg.payload = b"Eco"

        with patch.object(bridge, "request") as mock_req:
            bridge.on_mqtt_message(None, None, msg)
            mock_req.assert_not_called()

    def test_diagnostic_sensors_use_bridge_availability_only(self, bridge):
        """Diagnostic sensors must NOT use per-device availability — they need to stay
        readable when poll health is degraded."""
        bridge.mqtt_client.publish.reset_mock()
        bridge.publish_discovery("SN_TEST", "TestBoiler")

        diag_slugs = {
            "poll_interval", "consecutive_failures", "last_successful_poll",
            "element_wattage", "poll_status",
        }
        for call in bridge.mqtt_client.publish.call_args_list:
            if len(call.args) < 2:
                continue
            topic = call.args[0]
            # Only check the diagnostic sensor config topics
            if not any(f"/{slug}/config" in topic for slug in diag_slugs):
                continue
            payload = json.loads(call.args[1])
            # Must NOT have multi-availability array (which would go offline when device fails)
            assert "availability" not in payload or isinstance(payload["availability"], str), (
                f"Diagnostic sensor {topic} must use single availability_topic, not array"
            )
            assert "availability_topic" in payload
