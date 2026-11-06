"""Regression tests for the independent-review corrections to Stage 1 (PR #16).

1. A pre-event successful poll must not clear a newer rate-limit event.
2. A recognised quota rejection can never create a pending command.
3. Startup login / device discovery rate limits are waited out in-process.
4. One HTTP response is classified, counted and registered exactly once.
5. Active quota exhaustion and stale telemetry are visible promptly.
Plus: documented energy under-count after a prolonged telemetry outage.
"""

import json
import pathlib
import sys
import threading
from unittest.mock import MagicMock, patch

import pytest

sys.argv = ["thermowatt_bridge.py"]

import thermowatt_bridge as bridge_module  # noqa: E402
from thermowatt_bridge import MyThermowattBridge  # noqa: E402

DAILY = {"success": False, "error": "Daily request limit exceeded"}
QUOTA_NO_FLAG = {"error": "Daily request limit exceeded"}
T0 = 1_000_000.0


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


def _resp(code=200, body=None, headers=None, json_error=False):
    r = MagicMock()
    r.status_code = code
    r.headers = headers or {}
    if json_error:
        r.json.side_effect = ValueError("no body")
    else:
        r.json.return_value = body
    return r


def _good_status(heating=1):
    return _resp(200, {"result": {"WaterHeaterSts": heating, "T_Avg": 55.0, "T_SetPoint": 60, "Cmd": 9}})


def _message(topic, payload):
    msg = MagicMock()
    msg.retain = False
    msg.topic = topic
    msg.payload = payload.encode()
    return msg


def _diags(bridge):
    return [json.loads(c.args[1]) for c in bridge.mqtt_client.publish.call_args_list
            if c.args and c.args[0] == "P/SN/diagnostics"]


def _api(by_method):
    """session.request stub: fresh response object per call, chosen by HTTP method."""
    def side_effect(method, url, **kwargs):
        return by_method[method]()
    return side_effect


# ───────── 1. older successful poll must not clear a newer rate-limit event ─────────

class TestStalePollCannotClearNewerEvent:
    def test_command_rate_limit_between_poll_and_clear_is_preserved(self, bridge, clock):
        bridge.session.request = MagicMock(side_effect=_api({
            "GET": lambda: _good_status(),
            "POST": lambda: _resp(200, DAILY),
        }))
        original = bridge.poll_status

        def poll_then_command_hits_limit(serial):
            result = original(serial)  # good poll, request already completed
            bridge.on_mqtt_message(None, None, _message("P/SN/CMD/MODE", "Off"))
            return result

        bridge.poll_status = poll_then_command_hits_limit
        bridge._run_poll_cycle()
        assert bridge.rate_limit_backoff == 1
        assert bridge._rate_limit_remaining() == 120
        assert bridge.current_poll_interval == 120
        assert _diags(bridge)[-1]["rate_limit"]["active"] is True
        assert _diags(bridge)[-1]["poll_status"] == "rate_limited"

    def test_generation_alone_protects_when_window_elapsed_before_clear(self, bridge, clock):
        """Poll thread stalls past the whole backoff window after a command's event:
        the elapsed-window check no longer helps, only the generation does."""
        bridge.session.request = MagicMock(side_effect=_api({
            "GET": lambda: _good_status(),
            "POST": lambda: _resp(200, QUOTA_NO_FLAG),
        }))
        original = bridge.poll_status

        def poll_then_event_then_long_stall(serial):
            result = original(serial)
            bridge.on_mqtt_message(None, None, _message("P/SN/CMD/MODE", "Off"))
            clock.now += 500
            return result

        bridge.poll_status = poll_then_event_then_long_stall
        bridge._run_poll_cycle()
        assert bridge._rate_limit_remaining() == 0     # window itself has elapsed
        assert bridge.rate_limit_backoff == 1          # but the event was not cleared
        bridge.poll_status = original
        bridge.session.request = MagicMock(side_effect=_api({"GET": lambda: _good_status()}))
        bridge._run_poll_cycle()                       # a poll that started afterwards clears it
        assert bridge.rate_limit_backoff == 0

    def test_threaded_interleaving_is_deterministic(self, bridge, clock):
        """Poll thread finishes its request, MQTT thread's command hits the limit,
        and only then does the poll thread reach the clear step."""
        poll_request_done = threading.Event()
        command_done = threading.Event()
        bridge.session.request = MagicMock(side_effect=_api({
            "GET": lambda: _good_status(),
            "POST": lambda: _resp(200, QUOTA_NO_FLAG),
        }))
        original = bridge.poll_status

        def gated_poll(serial):
            result = original(serial)
            poll_request_done.set()
            assert command_done.wait(5)
            return result

        bridge.poll_status = gated_poll

        def command_thread():
            assert poll_request_done.wait(5)
            bridge.on_mqtt_message(None, None, _message("P/SN/CMD/TEMP", "55"))
            command_done.set()

        t = threading.Thread(target=command_thread)
        t.start()
        bridge._run_poll_cycle()
        t.join(5)
        assert not t.is_alive()
        assert bridge.rate_limit_backoff == 1 and bridge._rate_limit_remaining() > 0
        assert bridge._command_state["SN"]["TEMP"]["status"] == "submit_failed"

    def test_poll_started_after_event_and_window_can_clear(self, bridge, clock):
        bridge._enter_rate_limit("HTTP 429")
        clock.now += 121
        generation = bridge._rate_limit_gen()  # read before the (later) poll
        assert bridge._clear_rate_limit(generation) is True
        assert bridge.rate_limit_backoff == 0

    def test_poll_older_than_event_cannot_clear_even_after_expiry(self, bridge, clock):
        generation = bridge._rate_limit_gen()      # poll starts here
        bridge._enter_rate_limit("HTTP 429")       # newer event
        clock.now += 500                           # window long over
        assert bridge._clear_rate_limit(generation) is False
        assert bridge.rate_limit_backoff == 1
        assert bridge._clear_rate_limit(bridge._rate_limit_gen()) is True

    def test_unexpired_window_is_never_cleared(self, bridge):
        bridge._enter_rate_limit("HTTP 429")
        assert bridge._clear_rate_limit(bridge._rate_limit_gen()) is False
        assert bridge._rate_limit_remaining() > 0

    def test_in_window_detection_still_bumps_generation(self, bridge):
        bridge._enter_rate_limit("HTTP 429")
        before = bridge._rate_limit_gen()
        bridge._enter_rate_limit("HTTP 429")
        assert bridge._rate_limit_gen() == before + 1
        assert bridge.rate_limit_backoff == 1  # but no extra backoff step


# ───────── 2. quota rejection must never create a pending command ─────────

COMMANDS = [
    ("MODE", "Off", "/off"), ("MODE", "Eco", "/eco"), ("MODE", "Auto", "/auto"),
    ("MODE", "Manual", "/manual"), ("MODE", "Holiday", "/holiday"), ("TEMP", "55", "/manual"),
]


class TestQuotaRejectionNeverPending:
    @pytest.mark.parametrize("body", [
        QUOTA_NO_FLAG,
        {"success": True, "error": "Daily request limit exceeded"},
        {"error": "  DAILY REQUEST LIMIT EXCEEDED "},
        DAILY,
    ])
    @pytest.mark.parametrize("cmd_type,payload,endpoint", COMMANDS)
    def test_every_command_type_rejects_quota_body(self, bridge, body, cmd_type, payload, endpoint):
        bridge.session.request = MagicMock(return_value=_resp(200, body))
        bridge.on_mqtt_message(None, None, _message(f"P/SN/CMD/{cmd_type}", payload))
        record = bridge._command_state["SN"][cmd_type]
        assert record["status"] == "submit_failed"
        assert "Daily request limit exceeded" in record["error"]
        assert bridge._poll_wakeup.is_set() is False
        assert bridge._confirm_until == 0.0
        assert bridge._last_cmd_time.get("SN", {}).get(cmd_type) is None
        assert bridge._rate_limit_remaining() > 0
        assert bridge.session.request.call_count == 1  # no retry
        assert not any(r["status"] == "pending" for r in bridge._command_state["SN"].values())

    def test_detection_and_rejection_cannot_disagree(self, bridge):
        for body in (QUOTA_NO_FLAG, DAILY, {"success": True, "error": "Daily request limit exceeded"}):
            resp = _resp(200, body)
            assert bridge._detect_rate_limit(resp) is not None
            assert bridge._command_rejection(resp) is not None

    def test_quota_rejection_via_wrapped_request_still_starts_backoff(self, bridge):
        """request() mocked (never classified) – _submit_command must classify itself."""
        bridge.request = MagicMock(return_value=_resp(200, QUOTA_NO_FLAG))
        bridge.on_mqtt_message(None, None, _message("P/SN/CMD/MODE", "Off"))
        assert bridge._command_state["SN"]["MODE"]["status"] == "submit_failed"
        assert bridge.rate_limit_backoff == 1

    def test_non_quota_success_false_still_rejected_and_legit_success_still_pending(self, bridge):
        bridge.session.request = MagicMock(return_value=_resp(200, {"success": False, "error": "busy"}))
        bridge.on_mqtt_message(None, None, _message("P/SN/CMD/MODE", "Eco"))
        assert bridge._command_state["SN"]["MODE"]["status"] == "submit_failed"
        assert bridge._rate_limit_remaining() == 0  # not a rate limit
        bridge.session.request = MagicMock(return_value=_resp(200, None, json_error=True))
        bridge.on_mqtt_message(None, None, _message("P/SN/CMD/MODE", "Eco"))
        assert bridge._command_state["SN"]["MODE"]["status"] == "pending"


# ───────── 3. startup authentication / discovery ─────────

LOGIN_OK = {"result": {"accessToken": "a", "refreshToken": "r"}}
DEVICES = {"result": {"termostati": [{"seriale": "SN", "nome": "HWS"}]}}


class TestStartupRateLimit:
    @pytest.fixture
    def sleeps(self, clock, monkeypatch):
        log = []

        def fake_sleep(seconds):
            log.append(seconds)
            clock.now += seconds

        monkeypatch.setattr(bridge_module.time, "sleep", fake_sleep)
        return log

    def test_login_quota_is_waited_out_then_discovery_proceeds(self, bridge, sleeps):
        bridge.session.post = MagicMock(side_effect=[_resp(200, DAILY), _resp(200, LOGIN_OK)])
        bridge.session.request = MagicMock(return_value=_resp(200, DEVICES))
        devices = bridge._boot_authenticate_and_discover()
        assert [d["seriale"] for d in devices] == ["SN"]
        assert bridge.session.post.call_count == 2
        assert sum(sleeps) == pytest.approx(120)
        assert bridge.config["access_token"] == "a"

    def test_login_http_429_is_waited_out(self, bridge, sleeps):
        bridge.session.post = MagicMock(side_effect=[_resp(429, None, json_error=True), _resp(200, LOGIN_OK)])
        bridge.session.request = MagicMock(return_value=_resp(200, DEVICES))
        assert bridge._boot_authenticate_and_discover()
        assert sum(sleeps) == pytest.approx(120)

    @pytest.mark.parametrize("limited", [
        lambda: _resp(200, DAILY),
        lambda: _resp(200, QUOTA_NO_FLAG),
        lambda: _resp(429, None, json_error=True),
    ])
    def test_user_info_quota_does_not_exit_or_report_zero_devices(self, bridge, sleeps, limited):
        bridge.session.post = MagicMock(side_effect=lambda *a, **k: _resp(200, LOGIN_OK))
        bridge.session.request = MagicMock(side_effect=[limited(), _resp(200, DEVICES)])
        devices = bridge._boot_authenticate_and_discover()  # would SystemExit(1) before
        assert len(devices) == 1
        assert bridge.session.request.call_count == 2
        assert sum(sleeps) == pytest.approx(120)

    def test_repeated_startup_limits_back_off_exponentially_with_bounded_requests(self, bridge, sleeps):
        bridge.session.post = MagicMock(side_effect=[
            _resp(200, DAILY), _resp(200, DAILY), _resp(200, DAILY), _resp(200, LOGIN_OK)])
        bridge.session.request = MagicMock(return_value=_resp(200, DEVICES))
        bridge._boot_authenticate_and_discover()
        assert bridge.session.post.call_count == 4
        assert sum(sleeps) == pytest.approx(120 + 240 + 480)

    def test_persisted_window_is_honoured_before_first_request(self, tmp_path, monkeypatch, clock, sleeps):
        first = _make(tmp_path, monkeypatch)
        first._enter_rate_limit("Daily request limit exceeded")
        restarted = _make(tmp_path, monkeypatch)
        restarted.session.post = MagicMock(return_value=_resp(200, LOGIN_OK))
        restarted.session.request = MagicMock(return_value=_resp(200, DEVICES))
        times = []
        restarted.session.post.side_effect = lambda *a, **k: (times.append(clock.now), _resp(200, LOGIN_OK))[1]
        restarted._boot_authenticate_and_discover()
        assert times == [T0 + 120]  # first request only after the persisted window

    def test_genuine_recovery_then_normal_polling_clears_state(self, bridge, clock, sleeps):
        bridge.session.post = MagicMock(side_effect=[_resp(200, DAILY), _resp(200, LOGIN_OK)])
        bridge.session.request = MagicMock(return_value=_resp(200, DEVICES))
        bridge._boot_authenticate_and_discover()
        assert bridge.rate_limit_backoff == 1
        bridge.session.request = MagicMock(side_effect=lambda *a, **k: _good_status())
        bridge._run_poll_cycle()
        assert bridge.rate_limit_backoff == 0
        assert json.loads(pathlib.Path(bridge_module.CONFIG_FILE).read_text())["rate_limit"]["step"] == 0

    def test_non_rate_limit_login_failure_still_exits(self, bridge, sleeps):
        import requests
        bad = _resp(401, {"success": False, "error": "bad credentials"})
        bad.raise_for_status.side_effect = requests.HTTPError("401")
        bridge.session.post = MagicMock(return_value=bad)
        with pytest.raises(SystemExit) as exc:
            bridge._boot_authenticate_and_discover()
        assert exc.value.code == 1
        assert sleeps == []

    def test_non_rate_limit_empty_discovery_still_exits(self, bridge, sleeps):
        bridge.session.post = MagicMock(return_value=_resp(200, LOGIN_OK))
        bridge.session.request = MagicMock(return_value=_resp(200, {"result": {"termostati": []}}))
        with pytest.raises(SystemExit) as exc:
            bridge._boot_authenticate_and_discover()
        assert exc.value.code == 1

    def test_sigterm_during_startup_wait_exits_cleanly(self, bridge, monkeypatch):
        monkeypatch.setattr(bridge_module, "EMAIL", "e")
        monkeypatch.setattr(bridge_module, "PASSWORD", "p")
        bridge._boot_authenticate_and_discover = MagicMock(side_effect=KeyboardInterrupt)
        with pytest.raises(SystemExit) as exc:
            bridge.run()
        assert exc.value.code == 0


# ───────── 4. single classification / counter semantics ─────────

class TestSingleClassification:
    @pytest.mark.parametrize("make", [
        lambda: _resp(200, DAILY), lambda: _resp(200, QUOTA_NO_FLAG),
        lambda: _resp(429, None, json_error=True),
    ])
    def test_poll_response_counted_once_end_to_end(self, bridge, make):
        bridge.session.request = MagicMock(side_effect=lambda *a, **k: make())
        bridge.poll_status("SN")  # request() classifies, poll_status() sees same response
        c = bridge._requests_diagnostics()
        assert c["rate_limited_responses"] == 1
        assert bridge._rate_limit_events_total == 1
        assert bridge._rate_limit_gen() == 1
        assert (c["status"], c["total"], c["blocked"]) == (1, 1, 0)

    def test_command_response_counted_once(self, bridge):
        bridge.session.request = MagicMock(return_value=_resp(200, QUOTA_NO_FLAG))
        bridge.on_mqtt_message(None, None, _message("P/SN/CMD/MODE", "Off"))
        c = bridge._requests_diagnostics()
        assert c["rate_limited_responses"] == 1 and bridge._rate_limit_gen() == 1

    def test_note_response_is_idempotent_per_response_object(self, bridge):
        resp = _resp(200, DAILY)
        assert [bridge._note_response(resp) for _ in range(5)] == [True] * 5
        assert bridge._request_counters["rate_limited_responses"] == 1
        assert bridge._rate_limit_gen() == 1
        other = _resp(200, DAILY)  # a distinct response is a distinct observation
        bridge._note_response(other)
        assert bridge._request_counters["rate_limited_responses"] == 2
        assert bridge.rate_limit_backoff == 1  # same window: still one backoff step

    def test_non_limit_responses_are_memoised_negative(self, bridge):
        resp = _resp(200, {"result": {"x": 1}})
        assert bridge._note_response(resp) is False
        resp.json.return_value = DAILY  # mutation after classification is ignored
        assert bridge._note_response(resp) is False

    def test_counter_semantics_sent_vs_blocked_vs_responses(self, bridge, clock):
        bridge.session.request = MagicMock(side_effect=lambda *a, **k: _resp(200, DAILY))
        bridge.poll_status("SN")                       # 1 sent, 1 limited response
        for _ in range(3):                             # refused locally: not sent
            bridge.on_mqtt_message(None, None, _message("P/SN/CMD/MODE", "Off"))
            clock.now += 20
        c = bridge._requests_diagnostics()
        assert c["total"] == 1 == c["status"] + c["command"] + c["auth"] + c["other"]
        assert c["rate_limited_responses"] == 1
        assert c["blocked"] == 3
        assert bridge.session.request.call_count == 1
        assert bridge._rate_limit_events_total == 1  # windows, not responses

    def test_events_total_counts_windows_not_responses(self, bridge, clock):
        for _ in range(3):
            bridge._note_response(_resp(200, DAILY))
        assert bridge._request_counters["rate_limited_responses"] == 3
        assert bridge._rate_limit_events_total == 1


# ───────── 5. prompt diagnostics ─────────

class TestPromptDiagnostics:
    def test_poll_status_rate_limited_immediately_and_until_recovery(self, bridge, clock):
        bridge.session.request = MagicMock(side_effect=lambda *a, **k: _resp(200, DAILY))
        bridge.poll_status("SN")
        d = _diags(bridge)[-1]
        assert d["poll_status"] == "rate_limited" and d["consecutive_failures"] == 1
        assert d["telemetry"]["state"] == "rate_limited"
        clock.now += 500                               # window over, no good poll yet
        bridge._publish_diagnostics("SN")
        assert _diags(bridge)[-1]["poll_status"] == "rate_limited"
        bridge.session.request = MagicMock(side_effect=lambda *a, **k: _good_status())
        bridge._run_poll_cycle()
        d = _diags(bridge)[-1]
        assert d["poll_status"] == "ok" and d["telemetry"]["state"] == "fresh"

    def test_telemetry_states(self, bridge, clock):
        bridge._publish_diagnostics("SN")
        assert _diags(bridge)[-1]["telemetry"] == {"state": "unknown", "age_s": None, "stale_after_s": 120}
        bridge.session.request = MagicMock(side_effect=lambda *a, **k: _good_status())
        bridge.poll_status("SN")
        assert _diags(bridge)[-1]["telemetry"]["state"] == "fresh"
        clock.now += 121
        bridge._publish_diagnostics("SN")
        t = _diags(bridge)[-1]["telemetry"]
        assert t["state"] == "stale" and t["age_s"] == 121.0
        assert _diags(bridge)[-1]["poll_status"] == "ok"  # non-limit failures keep prior semantics

    def test_diagnostics_republished_periodically_during_backoff(self, bridge, clock):
        bridge.session.request = MagicMock(side_effect=lambda *a, **k: _good_status())
        bridge.poll_status("SN")
        bridge._enter_rate_limit("HTTP 429", retry_after=600)
        n0 = len(_diags(bridge))

        waits = []

        class Ev:
            def wait(self, timeout=None):
                waits.append(timeout)
                clock.now += timeout
                return False

            def clear(self):
                pass

            def set(self):
                pass

        bridge._poll_wakeup = Ev()
        bridge.session.request = MagicMock()
        bridge._wait_for_next_poll(60)
        assert max(waits) <= bridge.DIAG_REFRESH_INTERVAL
        new = _diags(bridge)[n0:]
        assert len(new) >= 9                      # ~one per minute across a 600 s window
        ages = [d["telemetry"]["age_s"] for d in new]
        assert ages == sorted(ages) and ages[-1] > ages[0]
        assert all(d["poll_status"] == "rate_limited" for d in new)
        bridge.session.request.assert_not_called()

    def test_package19_freshness_inputs_remain_fail_closed(self, bridge, clock):
        """P19 requires poll_status == 'ok', failures == 0 and age <= 120 s (unchanged)."""
        bridge.session.request = MagicMock(side_effect=lambda *a, **k: _good_status())
        bridge.poll_status("SN")
        d = _diags(bridge)[-1]
        assert (d["poll_status"], d["consecutive_failures"]) == ("ok", 0)
        bridge.session.request = MagicMock(side_effect=lambda *a, **k: _resp(200, DAILY))
        clock.now += 60
        bridge.poll_status("SN")
        d = _diags(bridge)[-1]
        assert d["poll_status"] != "ok" and d["consecutive_failures"] >= 1

    def test_normal_poll_and_confirmation_contract_unchanged(self):
        assert (MyThermowattBridge.POLL_INTERVAL, MyThermowattBridge.POLL_INTERVAL_CONFIRM,
                MyThermowattBridge.CONFIRM_WINDOW, MyThermowattBridge.CMD_COOLDOWN) == (60, 20, 60, 15)

    def test_new_diagnostic_sensor_for_telemetry_discovery(self, bridge):
        bridge.publish_discovery("SN", "HWS")
        topics = [c.args[0] for c in bridge.mqtt_client.publish.call_args_list]
        assert "homeassistant/sensor/SN/poll_status/config" in topics


# ───────── energy under-count during telemetry outage (documented limitation) ─────────

class TestEnergyDuringOutage:
    def test_gap_is_capped_so_prolonged_outage_undercounts(self, bridge, clock):
        bridge.session.request = MagicMock(side_effect=lambda *a, **k: _good_status(heating=1))
        bridge.poll_status("SN")
        clock.now += 3600  # one hour of telemetry silence while (really) heating
        bridge.poll_status("SN")
        kwh = bridge.config["energy_kwh"]["SN"]
        assert kwh == pytest.approx(3.0 * 120 / 3600, abs=1e-4)   # 0.1 kWh
        assert kwh < 3.0 * 3600 / 3600 / 10                       # far below 3 kWh actually possible
