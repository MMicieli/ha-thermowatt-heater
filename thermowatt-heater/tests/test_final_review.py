"""Final review amendment tests.

* Rate-limit lifecycle is reported as active -> recovery_unverified -> recovered.
* Latched submit_failed behaviour that the README documents.
* The pre-existing HTTP 401 token-refresh replay of a request (including command POSTs)
  is bounded, separate from rate-limit recovery, and verified here.
"""

import json
import pathlib
import sys
from unittest.mock import MagicMock, patch

import jinja2
import pytest

sys.argv = ["thermowatt_bridge.py"]

import thermowatt_bridge as bridge_module  # noqa: E402
from thermowatt_bridge import MyThermowattBridge  # noqa: E402

DAILY = {"success": False, "error": "Daily request limit exceeded"}
T0 = 1_000_000.0
REFRESH_OK = {"result": {"accessToken": "new-a", "refreshToken": "new-r"}}


class FakeClock:
    def __init__(self, now=T0):
        self.now = now

    def __call__(self):
        return self.now


@pytest.fixture
def clock(monkeypatch):
    c = FakeClock()
    monkeypatch.setattr(bridge_module.time, "time", c)
    return c


def _make(tmp_path, monkeypatch):
    monkeypatch.setattr(bridge_module, "CONFIG_FILE", str(tmp_path / "config.json"))
    with patch("thermowatt_bridge.mqtt.Client") as mock_client:
        mqtt_client = MagicMock()
        mock_client.return_value = mqtt_client
        instance = MyThermowattBridge()
        instance.mqtt_client = mqtt_client
    instance.config["devices"]["SN"] = {"name": "HWS", "last_setpoint": 60}
    return instance


@pytest.fixture
def bridge(tmp_path, monkeypatch, clock):
    return _make(tmp_path, monkeypatch)


def _resp(code=200, body=None, json_error=False):
    r = MagicMock()
    r.status_code = code
    r.headers = {}
    if json_error:
        r.json.side_effect = ValueError("no body")
    else:
        r.json.return_value = body
    return r


def _good_status():
    return _resp(200, {"result": {"WaterHeaterSts": 1, "T_Avg": 55.0, "T_SetPoint": 60, "Cmd": 9}})


def _message(topic, payload):
    msg = MagicMock()
    msg.retain = False
    msg.topic = topic
    msg.payload = payload.encode()
    return msg


def _last_diag(bridge):
    calls = [c for c in bridge.mqtt_client.publish.call_args_list if c.args and c.args[0] == "P/SN/diagnostics"]
    return json.loads(calls[-1].args[1])


def _sensor_value(bridge, rate_limit):
    bridge.publish_discovery("SN", "HWS")
    cfg = [c for c in bridge.mqtt_client.publish.call_args_list
           if c.args and c.args[0] == "homeassistant/sensor/SN/rate_limit_status/config"]
    template = json.loads(cfg[-1].args[1])["value_template"]
    return jinja2.Environment().from_string(template).render(value_json={"rate_limit": rate_limit}).strip()


# ───────── 2. active -> recovery_unverified -> recovered ─────────

class TestRateLimitLifecycle:
    def test_never_limited_is_none_and_sensor_ok(self, bridge):
        bridge._publish_diagnostics("SN")
        rl = _last_diag(bridge)["rate_limit"]
        assert rl["state"] == "none" and rl["active"] is False
        assert _sensor_value(bridge, rl) == "ok"

    def test_window_expiry_reports_recovery_unverified_not_ok(self, bridge, clock):
        bridge._enter_rate_limit("HTTP 429")
        bridge._publish_diagnostics("SN")
        rl = _last_diag(bridge)["rate_limit"]
        assert (rl["state"], rl["active"]) == ("active", True)
        assert _sensor_value(bridge, rl) == "limited"

        clock.now += 121                       # window over, no successful poll yet
        bridge._publish_diagnostics("SN")
        d = _last_diag(bridge)
        rl = d["rate_limit"]
        assert (rl["state"], rl["active"], rl["retry_at"]) == ("recovery_unverified", False, None)
        assert rl["recovered_at"] is None
        assert _sensor_value(bridge, rl) == "recovery_unverified"   # previously showed 'ok'
        # Package 19 inputs stay fail-closed in this state.
        assert d["poll_status"] == "rate_limited" and d["telemetry"]["state"] == "rate_limited"

    def test_good_poll_marks_recovered(self, bridge, clock):
        bridge._enter_rate_limit("HTTP 429")
        clock.now += 121
        bridge.request = MagicMock(return_value=_good_status())
        bridge._run_poll_cycle()
        d = _last_diag(bridge)
        rl = d["rate_limit"]
        assert rl["state"] == "recovered" and rl["recovered_at"] is not None and rl["events_total"] == 1
        assert _sensor_value(bridge, rl) == "ok"
        assert d["poll_status"] == "ok" and d["telemetry"]["state"] == "fresh"

    def test_failed_probe_after_window_stays_unverified_then_steps_up(self, bridge, clock):
        bridge._enter_rate_limit("HTTP 429")
        clock.now += 121
        bridge.request = MagicMock(return_value=_resp(503, None, json_error=True))
        bridge.login = MagicMock()
        bridge._run_poll_cycle()
        assert _last_diag(bridge)["rate_limit"]["state"] == "recovery_unverified"
        bridge.session.request = MagicMock(side_effect=lambda *a, **k: _resp(200, DAILY))
        bridge.request = MyThermowattBridge.request.__get__(bridge)
        bridge._run_poll_cycle()
        assert _last_diag(bridge)["rate_limit"]["state"] == "active"
        assert bridge.rate_limit_backoff == 2

    def test_publish_when_window_ends_before_next_poll(self, bridge, clock):
        bridge._enter_rate_limit("HTTP 429")           # 120 s
        states = []

        class Ev:
            """Every wait is cut short by a (discarded) wake-up, so the periodic
            post-wait publish is skipped and only the window-end publish can fire."""

            def wait(self, timeout=None):
                clock.now += timeout
                return True

            def clear(self):
                pass

            def set(self):
                pass

        bridge._poll_wakeup = Ev()
        orig = bridge._publish_all_diagnostics

        def spy():
            orig()
            states.append(_last_diag(bridge)["rate_limit"]["state"])

        bridge._publish_all_diagnostics = spy
        bridge.session.request = MagicMock()
        bridge._wait_for_next_poll(60)
        assert states[-1] == "recovery_unverified"     # visible before any poll is attempted
        bridge.session.request.assert_not_called()

    def test_restart_after_expired_window_is_unverified_until_first_good_poll(self, tmp_path, monkeypatch, clock):
        first = _make(tmp_path, monkeypatch)
        first._enter_rate_limit("HTTP 429")
        clock.now += 500
        restarted = _make(tmp_path, monkeypatch)
        restarted._publish_diagnostics("SN")
        assert _last_diag(restarted)["rate_limit"]["state"] == "recovery_unverified"
        restarted.request = MagicMock(return_value=_good_status())
        restarted._run_poll_cycle()
        assert _last_diag(restarted)["rate_limit"]["state"] == "recovered"
        assert json.loads(pathlib.Path(bridge_module.CONFIG_FILE).read_text())["rate_limit"]["recovered_at"]

    def test_stale_poll_cannot_produce_recovered(self, bridge, clock):
        original = bridge.poll_status
        bridge.session.request = MagicMock(side_effect=lambda m, u, **k: _good_status() if m == "GET" else _resp(200, DAILY))

        def poll_then_command_limited(serial):
            result = original(serial)
            bridge.on_mqtt_message(None, None, _message("P/SN/CMD/MODE", "Off"))
            return result

        bridge._enter_rate_limit("HTTP 429")
        clock.now += 121                                 # unverified; poll starts now
        bridge.poll_status = poll_then_command_limited
        bridge._run_poll_cycle()
        assert _last_diag(bridge)["rate_limit"]["state"] in {"active", "recovery_unverified"}
        assert bridge.rate_limit_backoff >= 1


# ───────── 3. latched submit_failed (documented behaviour) ─────────

class TestLatchedSubmitFailed:
    def _fail_a_temp_command(self, bridge):
        bridge.session.request = MagicMock(side_effect=lambda *a, **k: _resp(200, DAILY))
        bridge.on_mqtt_message(None, None, _message("P/SN/CMD/TEMP", "50"))
        assert bridge._command_diagnostics("SN", bridge_module.time.time())[0] == "submit_failed"

    def test_stays_latched_through_recovery_until_a_later_command_completes(self, bridge, clock):
        self._fail_a_temp_command(bridge)
        clock.now += 200
        bridge.session.request = MagicMock(side_effect=lambda *a, **k: _good_status())
        bridge._run_poll_cycle()                      # recovered, polling normally
        assert bridge.rate_limit_backoff == 0
        assert _last_diag(bridge)["command_status"] == "submit_failed"   # still latched
        clock.now += 200
        bridge._run_poll_cycle()
        assert _last_diag(bridge)["command_status"] == "submit_failed"   # polls never clear it

        # A later, caller-initiated command that is confirmed becomes the latest result.
        bridge.session.request = MagicMock(side_effect=lambda m, u, **k: (
            _resp(200, None, json_error=True) if m == "POST" else _good_status()))
        bridge.on_mqtt_message(None, None, _message("P/SN/CMD/MODE", "Manual"))
        clock.now += 20
        bridge._run_poll_cycle()
        assert _last_diag(bridge)["command_status"] == "confirmed"
        # ...but the TEMP record itself (what Package 19's handback reads) is still the failed one.
        assert _last_diag(bridge)["commands"]["TEMP"]["status"] == "submit_failed"

    def test_bridge_restart_clears_in_memory_record(self, tmp_path, monkeypatch, clock):
        first = _make(tmp_path, monkeypatch)
        self._fail_a_temp_command(first)
        restarted = _make(tmp_path, monkeypatch)
        assert restarted._command_diagnostics("SN", clock.now)[0] == "idle"


# ───────── 4. pre-existing HTTP 401 token-refresh replay ─────────

class TestAuthRefreshReplay:
    @pytest.fixture
    def seq(self, bridge):
        """session.request replies from a list; session.post (refresh) from another."""
        def configure(request_replies, refresh_replies):
            bridge.session.request = MagicMock(side_effect=list(request_replies))
            bridge.session.post = MagicMock(side_effect=list(refresh_replies))
            bridge.login = MagicMock()
        return configure

    def _posts(self, bridge):
        return [c for c in bridge.session.request.call_args_list if c.args[0] == "POST"]

    def test_command_post_is_replayed_once_after_successful_refresh(self, bridge, seq):
        seq([_resp(401, {"success": False, "error": "token expired"}), _resp(200, None, json_error=True)],
            [_resp(200, REFRESH_OK)])
        bridge.on_mqtt_message(None, None, _message("P/SN/CMD/TEMP", "55"))
        calls = self._posts(bridge)
        assert len(calls) == 2                                           # original + one replay
        assert calls[0].args == calls[1].args                            # same method + URL
        assert calls[0].kwargs["json"] == calls[1].kwargs["json"] == {"T_SetPoint": 55}
        assert bridge.session.post.call_count == 1                       # one refresh
        bridge.login.assert_not_called()
        assert bridge._command_state["SN"]["TEMP"]["status"] == "pending"   # a single pending record
        c = bridge._requests_diagnostics()
        assert (c["command"], c["auth"], c["total"]) == (2, 1, 3)       # both attempts counted
        assert bridge.config["access_token"] == "new-a"

    @pytest.mark.parametrize("mode,payload,path", [("Off", "Off", "/off"), ("Eco", "Eco", "/eco"),
                                                   ("Auto", "Auto", "/auto"), ("Holiday", "Holiday", "/holiday")])
    def test_bodyless_mode_commands_replay_identically(self, bridge, seq, mode, payload, path):
        seq([_resp(401, {"success": False}), _resp(200, None, json_error=True)], [_resp(200, REFRESH_OK)])
        bridge.on_mqtt_message(None, None, _message("P/SN/CMD/MODE", payload))
        calls = self._posts(bridge)
        assert len(calls) == 2 and calls[0].args == calls[1].args
        assert calls[0].args[1].endswith(path)
        assert calls[0].kwargs.get("data") == calls[1].kwargs.get("data")

    def test_second_401_after_refresh_is_not_replayed_again(self, bridge, seq):
        seq([_resp(401, {"success": False}), _resp(401, {"success": False})], [_resp(200, REFRESH_OK)])
        bridge.on_mqtt_message(None, None, _message("P/SN/CMD/TEMP", "55"))
        assert len(self._posts(bridge)) == 2                             # never a third
        assert bridge._command_state["SN"]["TEMP"]["status"] == "submit_failed"
        assert bridge._command_state["SN"]["TEMP"]["error"] == "401"
        assert bridge.session.post.call_count == 1
        bridge.login.assert_not_called()

    def test_failed_refresh_means_no_replay(self, bridge, seq):
        seq([_resp(401, {"success": False})], [_resp(401, {"success": False})])
        bridge.on_mqtt_message(None, None, _message("P/SN/CMD/TEMP", "55"))
        assert len(self._posts(bridge)) == 1
        assert bridge._command_state["SN"]["TEMP"]["status"] == "submit_failed"

    def test_rate_limited_refresh_means_no_replay_and_starts_backoff(self, bridge, seq):
        seq([_resp(401, {"success": False})], [_resp(200, DAILY)])
        bridge.on_mqtt_message(None, None, _message("P/SN/CMD/TEMP", "55"))
        assert len(self._posts(bridge)) == 1
        record = bridge._command_state["SN"]["TEMP"]
        assert record["status"] == "submit_failed" and record["error"].startswith("rate_limited")
        assert bridge._rate_limit_remaining() > 0

    def test_replayed_request_hitting_quota_is_not_resent(self, bridge, seq):
        seq([_resp(401, {"success": False}), _resp(200, DAILY)], [_resp(200, REFRESH_OK)])
        bridge.on_mqtt_message(None, None, _message("P/SN/CMD/TEMP", "55"))
        assert len(self._posts(bridge)) == 2
        assert bridge._command_state["SN"]["TEMP"]["status"] == "submit_failed"
        assert bridge._rate_limit_remaining() > 0
        bridge.on_mqtt_message(None, None, _message("P/SN/CMD/MODE", "Off"))
        assert len(self._posts(bridge)) == 2                             # window blocks everything else

    def test_request_itself_classifies_a_quota_response_to_the_replay(self, bridge, seq):
        """Direct request() callers (no caller-side classification) still start the backoff."""
        seq([_resp(401, {"success": False}), _resp(200, DAILY)], [_resp(200, REFRESH_OK)])
        resp = bridge.request("GET", "/status", serial="SN")
        assert resp.status_code == 200 and bridge.session.request.call_count == 2
        assert bridge._rate_limit_remaining() > 0 and bridge.rate_limit_backoff == 1

    def test_no_auth_replay_while_rate_limited(self, bridge, seq):
        bridge._enter_rate_limit("HTTP 429")
        seq([], [])
        bridge.on_mqtt_message(None, None, _message("P/SN/CMD/TEMP", "55"))
        bridge.session.request.assert_not_called()
        bridge.session.post.assert_not_called()

    def test_status_get_also_replays_once(self, bridge, seq):
        seq([_resp(401, {"success": False}), _good_status()], [_resp(200, REFRESH_OK)])
        assert bridge.poll_status("SN") == (True, 200)
        assert bridge.session.request.call_count == 2

    def test_replay_is_within_one_call_not_a_later_resend(self, bridge, seq, clock):
        """After a refresh-replay that fails, a later poll/recovery never re-sends the command."""
        seq([_resp(401, {"success": False}), _resp(401, {"success": False})], [_resp(200, REFRESH_OK)])
        bridge.on_mqtt_message(None, None, _message("P/SN/CMD/TEMP", "55"))
        sent = len(self._posts(bridge))
        bridge.session.request = MagicMock(side_effect=lambda *a, **k: _good_status())
        for _ in range(3):
            clock.now += 60
            bridge._run_poll_cycle()
        assert all(c.args[0] == "GET" for c in bridge.session.request.call_args_list)
        assert sent == 2
