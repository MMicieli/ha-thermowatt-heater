"""Regression tests for Stage 1 rate-limit reliability.

Covers detection (HTTP 429 / HTTP 200 'Daily request limit exceeded'), bounded
exponential backoff, priority over polling/wake-ups/fast-poll, no re-auth on quota
errors, command response validation, no queue/replay, pending-command expiry,
concurrency, restart persistence and request-counter observability.
"""

import json
import sys
import threading
from unittest.mock import MagicMock, patch

import pytest

sys.argv = ["thermowatt_bridge.py"]

import thermowatt_bridge as bridge_module  # noqa: E402
from thermowatt_bridge import MyThermowattBridge, RateLimitedError  # noqa: E402

DAILY = {"success": False, "error": "Daily request limit exceeded"}
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


def _good_status(cmd=9):
    return _resp(200, {"result": {"WaterHeaterSts": 1, "T_Avg": 55.0, "T_SetPoint": 60, "Cmd": cmd}})


def _message(topic, payload):
    msg = MagicMock()
    msg.retain = False
    msg.topic = topic
    msg.payload = payload.encode()
    return msg


def _diag(bridge):
    calls = [c for c in bridge.mqtt_client.publish.call_args_list
             if c.args and c.args[0] == "P/SN/diagnostics"]
    assert calls
    return json.loads(calls[-1].args[1])


def _published(bridge, topic):
    return [c for c in bridge.mqtt_client.publish.call_args_list if c.args and c.args[0] == topic]


# ───────────────────────────── detection ─────────────────────────────

class TestDetection:
    def test_http_429_detected(self, bridge):
        assert bridge._detect_rate_limit(_resp(429, None, json_error=True)) == ("HTTP 429", None)

    @pytest.mark.parametrize("error", [
        "Daily request limit exceeded",
        "  daily REQUEST limit exceeded  ",
        "Error: Daily request limit exceeded.",
    ])
    def test_http_200_daily_limit_detected(self, bridge, error):
        r = _resp(200, {"success": False, "error": error})
        assert bridge._detect_rate_limit(r) == ("Daily request limit exceeded", None)

    @pytest.mark.parametrize("success", [None, True, False, "false", 0])
    def test_quota_message_detected_regardless_of_success_field(self, bridge, success):
        body = {"error": "Daily request limit exceeded"}
        if success is not None:
            body["success"] = success
        assert bridge._detect_rate_limit(_resp(200, body)) is not None

    @pytest.mark.parametrize("body", [
        {"success": False, "error": "Water heater not found, check the Wi-Fi connection"},
        {"success": False, "error": "Invalid token"},
        {"success": False},
        {"success": False, "error": None},
        {"success": False, "error": 429},
        {"success": False, "error": ["Daily request limit exceeded"]},
        {"result": {}},
        {},
        [],
        ["Daily request limit exceeded"],
        "Daily request limit exceeded",
        None,
    ])
    def test_unrelated_payloads_are_not_rate_limits(self, bridge, body):
        assert bridge._detect_rate_limit(_resp(200, body)) is None

    def test_non_json_body_is_not_a_rate_limit(self, bridge):
        assert bridge._detect_rate_limit(_resp(200, json_error=True)) is None

    def test_retry_after_parsed_and_garbage_ignored(self, bridge):
        assert bridge._detect_rate_limit(_resp(429, None, {"Retry-After": "90"}, True)) == ("HTTP 429", 90.0)
        assert bridge._detect_rate_limit(
            _resp(429, None, {"Retry-After": "Wed, 21 Oct 2026 07:28:00 GMT"}, True)
        ) == ("HTTP 429", None)
        assert bridge._detect_rate_limit(_resp(429, None, {"Retry-After": "-5"}, True)) == ("HTTP 429", 0.0)

    def test_unrelated_invalid_payload_poll_is_failure_not_rate_limit(self, bridge):
        bridge.request = MagicMock(return_value=_resp(200, {"success": False, "error": "Water heater not found"}))
        assert bridge.poll_status("SN") == (False, None)
        assert bridge._rate_limit_remaining() == 0
        assert bridge.rate_limit_backoff == 0
        assert bridge._consecutive_failures["SN"] == 1
        assert "Water heater not found" in bridge._last_poll_error["SN"]


# ───────────────────────────── backoff ─────────────────────────────

class TestBackoff:
    def test_http_200_limit_poll_enters_backoff_without_publishing_state(self, bridge, clock):
        bridge.request = MagicMock(return_value=_resp(200, DAILY))
        assert bridge.poll_status("SN") == (False, None)
        assert bridge._rate_limit_remaining() == 120
        assert bridge.rate_limit_backoff == 1
        assert bridge.current_poll_interval == 120
        assert _published(bridge, "P/SN/STATUS") == []
        assert "rate limited" in bridge._last_poll_error["SN"]
        d = _diag(bridge)
        assert d["rate_limit"]["active"] is True and d["rate_limit"]["backoff_step"] == 1
        assert d["rate_limit"]["last_reason"] == "Daily request limit exceeded"

    def test_http_429_poll_enters_backoff(self, bridge):
        bridge.request = MagicMock(return_value=_resp(429, None, json_error=True))
        assert bridge.poll_status("SN") == (False, 429)
        assert bridge._rate_limit_remaining() == 120
        assert bridge._last_poll_error["SN"] == "HTTP 429"

    def test_exponential_schedule_is_bounded(self, bridge, clock):
        delays = []
        for _ in range(8):
            bridge._enter_rate_limit("HTTP 429")
            delays.append(bridge._rate_limit_remaining())
            clock.now += delays[-1] + 1  # window expires, next detection steps again
        assert delays == [120, 240, 480, 960, 1920, 3600, 3600, 3600]

    def test_retry_after_raises_but_never_exceeds_ceiling(self, bridge, clock):
        bridge._enter_rate_limit("HTTP 429", retry_after=500)
        assert bridge._rate_limit_remaining() == 500
        clock.now += 600
        bridge._enter_rate_limit("HTTP 429", retry_after=10**9)
        assert bridge._rate_limit_remaining() == bridge.RATE_LIMIT_MAX_BACKOFF
        clock.now += 4000
        bridge._enter_rate_limit("HTTP 429", retry_after=1)
        assert bridge._rate_limit_remaining() == 480  # step 3 exponential floor wins over small Retry-After

    def test_detection_inside_window_does_not_advance_step(self, bridge):
        bridge._enter_rate_limit("HTTP 429")
        bridge._enter_rate_limit("Daily request limit exceeded")
        assert bridge.rate_limit_backoff == 1
        assert bridge._rate_limit_events_total == 1
        assert bridge._request_counters["rate_limited_responses"] == 2

    def test_normal_polling_interval_unchanged(self):
        assert MyThermowattBridge.POLL_INTERVAL == 60
        assert MyThermowattBridge.POLL_INTERVAL_CONFIRM == 20
        assert MyThermowattBridge.CONFIRM_WINDOW == 60
        assert MyThermowattBridge.CMD_COOLDOWN == 15

    def test_normal_recovery_clears_state_and_resumes_60s(self, bridge, clock):
        bridge.request = MagicMock(return_value=_resp(200, DAILY))
        bridge._run_poll_cycle()
        assert bridge.current_poll_interval == 120
        clock.now += 121
        bridge.request = MagicMock(return_value=_good_status())
        bridge._run_poll_cycle()
        assert bridge.rate_limit_backoff == 0
        assert bridge._rate_limit_remaining() == 0
        assert bridge.current_poll_interval == bridge.POLL_INTERVAL
        assert _diag(bridge)["rate_limit"]["active"] is False
        assert len(_published(bridge, "P/SN/STATUS")) == 1

    def test_interval_resets_after_window_even_if_next_poll_fails_non_rate_limit(self, bridge, clock):
        bridge._enter_rate_limit("HTTP 429")
        clock.now += 121
        bridge.request = MagicMock(return_value=_resp(503, None, json_error=True))
        bridge.login = MagicMock()
        bridge._run_poll_cycle()
        assert bridge.current_poll_interval == bridge.POLL_INTERVAL

    def test_cycle_skips_all_io_while_limited(self, bridge):
        bridge._enter_rate_limit("HTTP 429")
        bridge.request = MagicMock()
        bridge.login = MagicMock()
        bridge._run_poll_cycle()
        bridge.request.assert_not_called()
        bridge.login.assert_not_called()

    def test_repeated_limit_after_window_steps_up(self, bridge, clock):
        bridge.request = MagicMock(side_effect=lambda *a, **k: _resp(200, DAILY))
        bridge._run_poll_cycle()
        clock.now += 121
        bridge._run_poll_cycle()
        assert bridge.rate_limit_backoff == 2
        assert bridge._rate_limit_remaining() == 240


# ───────────────────── no re-authentication on quota errors ─────────────────────

class TestNoReauth:
    def test_http_200_limit_cycle_does_not_login(self, bridge):
        bridge.request = MagicMock(return_value=_resp(200, DAILY))
        bridge.login = MagicMock()
        bridge._run_poll_cycle()
        bridge.login.assert_not_called()

    def test_http_429_cycle_does_not_login(self, bridge):
        bridge.request = MagicMock(return_value=_resp(429, None, json_error=True))
        bridge.login = MagicMock()
        bridge._run_poll_cycle()
        bridge.login.assert_not_called()

    def test_other_http_error_still_relogs_in(self, bridge):
        bridge.request = MagicMock(return_value=_resp(500, None, json_error=True))
        bridge.login = MagicMock()
        bridge._run_poll_cycle()
        bridge.login.assert_called_once()

    def test_request_does_not_refresh_on_quota_response(self, bridge):
        # Even if the quota body arrives on a 401, no token refresh is attempted.
        bridge.session.request = MagicMock(return_value=_resp(401, DAILY))
        bridge.refresh_session = MagicMock()
        r = bridge.request("GET", "/status", serial="SN")
        assert r.status_code == 401
        bridge.refresh_session.assert_not_called()
        assert bridge._rate_limit_remaining() > 0

    def test_refresh_hitting_limit_does_not_fall_back_to_login(self, bridge):
        bridge.session.request = MagicMock(return_value=_resp(401, {"success": False, "error": "expired"}))
        bridge.session.post = MagicMock(return_value=_resp(429, None, json_error=True))
        bridge.login = MagicMock()
        with pytest.raises(RateLimitedError):
            bridge.request("GET", "/status", serial="SN")
        bridge.login.assert_not_called()
        assert bridge.session.request.call_count == 1

    def test_http_gate_blocks_request_login_refresh(self, bridge):
        bridge._enter_rate_limit("HTTP 429")
        bridge.session.request = MagicMock()
        bridge.session.post = MagicMock()
        for call in (lambda: bridge.request("GET", "/status", serial="SN"), bridge.login, bridge.refresh_session):
            with pytest.raises(RateLimitedError):
                call()
        bridge.session.request.assert_not_called()
        bridge.session.post.assert_not_called()

    def test_login_detects_api_level_limit(self, bridge):
        bridge.session.post = MagicMock(return_value=_resp(200, DAILY))
        with pytest.raises(RateLimitedError):
            bridge.login()
        assert bridge._rate_limit_remaining() > 0


# ───────────────────── wake-ups and concurrency ─────────────────────

class FakeEvent:
    """Event whose wait() advances a fake clock; scripted wake results."""

    def __init__(self, clock, wakes):
        self.clock, self.wakes, self.waits, self._set = clock, list(wakes), [], False

    def wait(self, timeout=None):
        self.waits.append(timeout)
        woke = self.wakes.pop(0) if self.wakes else False
        if not woke:
            self.clock.now += timeout
        self._set = woke
        return woke

    def clear(self):
        self._set = False

    def set(self):
        self._set = True

    def is_set(self):
        return self._set


class TestWaitPriority:
    def test_wake_during_limit_is_discarded_until_window_ends(self, bridge, clock):
        bridge._enter_rate_limit("HTTP 429")  # 120 s
        ev = FakeEvent(clock, wakes=[True, True])
        bridge._poll_wakeup = ev
        start = clock.now
        bridge._wait_for_next_poll(60)
        # Two spurious wakes (command fast-poll) did not end the wait early.
        assert clock.now - start >= 120 - 1e-6 or bridge._rate_limit_remaining() == 0
        assert bridge._rate_limit_remaining() == 0
        assert len(ev.waits) >= 3

    def test_wake_without_limit_returns_immediately(self, bridge, clock):
        ev = FakeEvent(clock, wakes=[True])
        bridge._poll_wakeup = ev
        bridge._wait_for_next_poll(60)
        assert clock.now == T0

    def test_limit_beginning_during_wait_overrides_wake(self, bridge, clock):
        class Racing(FakeEvent):
            def wait(self, timeout=None):
                if not self.waits:
                    bridge._enter_rate_limit("HTTP 429")
                return super().wait(timeout)

        ev = Racing(clock, wakes=[True])
        bridge._poll_wakeup = ev
        bridge._wait_for_next_poll(60)
        assert bridge._rate_limit_remaining() == 0
        assert clock.now >= T0 + 120

    def test_fast_poll_does_not_override_limit_in_cycle(self, bridge, clock):
        bridge._enter_rate_limit("HTTP 429")
        bridge._record_command("SN", "MODE", "Cmd", 3)  # sets wake + fast-poll interval
        bridge.request = MagicMock()
        bridge._run_poll_cycle()
        bridge.request.assert_not_called()

    def test_concurrent_detections_step_once(self, bridge):
        barrier = threading.Barrier(2)
        bridge.session.request = MagicMock(side_effect=lambda *a, **k: (barrier.wait(), _resp(200, DAILY))[1])

        errors = []

        def poll():
            try:
                bridge.poll_status("SN")
            except Exception as e:  # noqa: BLE001  # pragma: no cover
                errors.append(e)

        def command():
            try:
                bridge.on_mqtt_message(None, None, _message("P/SN/CMD/MODE", "Off"))
            except Exception as e:  # noqa: BLE001  # pragma: no cover
                errors.append(e)

        threads = [threading.Thread(target=poll), threading.Thread(target=command)]
        # Requests serialise on _http_lock, so the barrier would deadlock; release it
        # by letting the first request through alone.
        bridge.session.request.side_effect = lambda *a, **k: _resp(200, DAILY)
        for t in threads:
            t.start()
        for t in threads:
            t.join(5)
        assert not errors
        assert bridge.rate_limit_backoff == 1
        assert bridge._rate_limit_events_total == 1
        assert bridge._rate_limit_remaining() == 120
        # Whichever thread lost the race was refused locally or saw the same window.
        assert bridge.session.request.call_count <= 2

    def test_pending_command_expires_during_backoff_without_polling(self, bridge, clock):
        bridge.request = MagicMock(return_value=_resp(200, {}, json_error=True))
        bridge.on_mqtt_message(None, None, _message("P/SN/CMD/MODE", "Eco"))
        assert bridge._command_state["SN"]["MODE"]["status"] == "pending"
        bridge._enter_rate_limit("HTTP 429")
        ev = FakeEvent(clock, wakes=[])
        bridge._poll_wakeup = ev
        bridge.request = MagicMock()
        bridge._wait_for_next_poll(60)
        bridge.request.assert_not_called()
        record = bridge._command_state["SN"]["MODE"]
        assert record["status"] == "timed_out"
        assert _diag(bridge)["commands"]["MODE"]["status"] == "timed_out"
        assert _diag(bridge)["command_status"] == "timed_out"


# ───────────────────── command safety ─────────────────────

class TestCommandSafety:
    def test_command_refused_locally_while_limited_and_never_queued(self, bridge, clock):
        bridge._enter_rate_limit("HTTP 429")
        bridge.session.request = MagicMock()
        bridge.on_mqtt_message(None, None, _message("P/SN/CMD/MODE", "Off"))
        bridge.session.request.assert_not_called()
        record = bridge._command_state["SN"]["MODE"]
        assert record["status"] == "submit_failed"
        assert record["error"].startswith("rate_limited")
        assert bridge._poll_wakeup.is_set() is False
        assert bridge._last_cmd_time.get("SN", {}).get("MODE") is None  # no cooldown consumed
        assert bridge._confirm_until == 0.0

    def test_no_replay_after_recovery(self, bridge, clock):
        bridge._enter_rate_limit("HTTP 429")
        bridge.session.request = MagicMock()
        bridge.on_mqtt_message(None, None, _message("P/SN/CMD/MODE", "Manual"))
        bridge.on_mqtt_message(None, None, _message("P/SN/CMD/TEMP", "65"))
        clock.now += 200
        bridge.session.request = MagicMock(return_value=_good_status())
        bridge._run_poll_cycle()
        posts = [c for c in bridge.session.request.call_args_list if c.args[0] == "POST"]
        assert posts == []  # only the status GET was sent
        assert bridge.session.request.call_count == 1
        assert all(r["status"] == "submit_failed" for r in bridge._command_state["SN"].values())

    def test_command_hitting_http_200_limit_fails_and_starts_backoff(self, bridge):
        bridge.session.request = MagicMock(return_value=_resp(200, DAILY))
        bridge.on_mqtt_message(None, None, _message("P/SN/CMD/MODE", "Off"))
        record = bridge._command_state["SN"]["MODE"]
        assert record["status"] == "submit_failed"
        assert "Daily request limit exceeded" in record["error"]
        assert bridge._rate_limit_remaining() > 0
        assert bridge._poll_wakeup.is_set() is False
        assert bridge.session.request.call_count == 1  # no retry

    def test_command_hitting_429_fails_and_starts_backoff(self, bridge):
        bridge.session.request = MagicMock(return_value=_resp(429, None, json_error=True))
        bridge.on_mqtt_message(None, None, _message("P/SN/CMD/TEMP", "55"))
        assert bridge._command_state["SN"]["TEMP"]["status"] == "submit_failed"
        assert bridge._command_state["SN"]["TEMP"]["error"] == "429"
        assert bridge._rate_limit_remaining() > 0

    @pytest.mark.parametrize("body", [
        {"success": False, "error": "Device offline"},
        {"success": False},
    ])
    def test_2xx_api_rejection_is_not_pending(self, bridge, body):
        bridge.session.request = MagicMock(return_value=_resp(200, body))
        bridge.on_mqtt_message(None, None, _message("P/SN/CMD/MODE", "Eco"))
        record = bridge._command_state["SN"]["MODE"]
        assert record["status"] == "submit_failed"
        assert record["error"].startswith("API rejected")
        assert bridge._poll_wakeup.is_set() is False
        assert bridge._confirm_until == 0.0
        assert bridge._last_cmd_time.get("SN", {}).get("MODE") is None
        assert _diag(bridge)["command_status"] == "submit_failed"

    def test_rejection_text_is_truncated(self, bridge):
        bridge.session.request = MagicMock(return_value=_resp(200, {"success": False, "error": "x" * 5000}))
        bridge.on_mqtt_message(None, None, _message("P/SN/CMD/MODE", "Eco"))
        assert len(bridge._command_state["SN"]["MODE"]["error"]) < 200

    @pytest.mark.parametrize("kwargs", [
        {"body": None, "json_error": True},            # body-less / non-JSON
        {"body": {"success": True}},
        {"body": {"result": {"ok": 1}}},
        {"body": []},
        {"body": "ok"},
        {"body": {}},
    ])
    def test_legitimate_2xx_success_remains_pending(self, bridge, kwargs):
        resp = _resp(200, kwargs["body"], json_error=kwargs.get("json_error", False))
        bridge.session.request = MagicMock(return_value=resp)
        bridge.on_mqtt_message(None, None, _message("P/SN/CMD/MODE", "Off"))
        assert bridge._command_state["SN"]["MODE"]["status"] == "pending"
        assert bridge._poll_wakeup.is_set() is True

    def test_204_no_content_remains_pending(self, bridge):
        bridge.session.request = MagicMock(return_value=_resp(204, None, json_error=True))
        bridge.on_mqtt_message(None, None, _message("P/SN/CMD/MODE", "Auto"))
        assert bridge._command_state["SN"]["MODE"]["status"] == "pending"

    def test_rejected_command_can_be_retried_by_user_immediately(self, bridge):
        bridge.session.request = MagicMock(return_value=_resp(200, {"success": False, "error": "busy"}))
        bridge.on_mqtt_message(None, None, _message("P/SN/CMD/MODE", "Eco"))
        bridge.session.request = MagicMock(return_value=_resp(200, None, json_error=True))
        bridge.on_mqtt_message(None, None, _message("P/SN/CMD/MODE", "Eco"))
        assert bridge._command_state["SN"]["MODE"]["status"] == "pending"

    def test_confirmation_contract_unchanged_and_latency_recorded(self, bridge, clock):
        bridge.session.request = MagicMock(return_value=_resp(200, None, json_error=True))
        bridge.on_mqtt_message(None, None, _message("P/SN/CMD/MODE", "Eco"))
        record = bridge._command_state["SN"]["MODE"]
        assert record["deadline"] - record["submitted_at"] == 60
        clock.now += 22
        bridge._reconcile_pending_commands("SN", {"result": {"Cmd": 3}}, clock.now - 1, clock.now)
        assert record["status"] == "confirmed"
        d = bridge._command_diagnostics("SN", clock.now)[1]["MODE"]
        assert d["confirm_latency_s"] == 22.0
        assert d["confirm_poll_number"] == 1

    def test_cooldown_still_enforced_after_accepted_command(self, bridge):
        bridge.session.request = MagicMock(return_value=_resp(200, None, json_error=True))
        bridge.on_mqtt_message(None, None, _message("P/SN/CMD/MODE", "Eco"))
        bridge.on_mqtt_message(None, None, _message("P/SN/CMD/MODE", "Auto"))
        assert bridge.session.request.call_count == 1


# ───────────────────── restart persistence ─────────────────────

class TestPersistence:
    def test_state_survives_restart_without_commands(self, tmp_path, monkeypatch, clock):
        b1 = _make(tmp_path, monkeypatch)
        b1.session.request = MagicMock(return_value=_resp(200, {}, json_error=True))
        b1.on_mqtt_message(None, None, _message("P/SN/CMD/MODE", "Eco"))
        b1._enter_rate_limit("Daily request limit exceeded")
        clock.now += 130
        b1._enter_rate_limit("Daily request limit exceeded")  # step 2, 240 s
        saved = json.loads((tmp_path / "config.json").read_text())
        assert saved["rate_limit"]["step"] == 2

        clock.now += 10
        b2 = _make(tmp_path, monkeypatch)
        assert b2.rate_limit_backoff == 2
        assert b2._rate_limit_remaining() == pytest.approx(230)
        assert b2._command_state == {}
        assert b2._last_cmd_time == {}
        assert b2._rate_limit_events_total == 2

    def test_boot_waits_out_persisted_window_before_any_request(self, tmp_path, monkeypatch, clock):
        b1 = _make(tmp_path, monkeypatch)
        b1._enter_rate_limit("HTTP 429")
        b2 = _make(tmp_path, monkeypatch)
        slept = []

        def fake_sleep(s):
            slept.append(s)
            clock.now += s

        b2.session.request = MagicMock()
        b2.session.post = MagicMock()
        with patch("thermowatt_bridge.time.sleep", fake_sleep):
            b2._wait_out_persisted_rate_limit()
        assert sum(slept) == pytest.approx(120)
        assert max(slept) <= 30
        b2.session.request.assert_not_called()
        b2.session.post.assert_not_called()
        assert _diag(b2)["rate_limit"]["active"] is True  # stale retained state replaced

    def test_expired_persisted_window_does_not_wait_and_clears_on_success(self, tmp_path, monkeypatch, clock):
        b1 = _make(tmp_path, monkeypatch)
        b1._enter_rate_limit("HTTP 429")
        clock.now += 500
        b2 = _make(tmp_path, monkeypatch)
        with patch("thermowatt_bridge.time.sleep") as sleep:
            b2._wait_out_persisted_rate_limit()
        sleep.assert_not_called()
        assert b2.rate_limit_backoff == 1
        b2.request = MagicMock(return_value=_good_status())
        b2._run_poll_cycle()
        assert b2.rate_limit_backoff == 0
        assert json.loads((tmp_path / "config.json").read_text())["rate_limit"]["step"] == 0

    def test_absurd_persisted_until_is_clamped(self, tmp_path, monkeypatch, clock):
        (tmp_path / "config.json").write_text(json.dumps({
            "device_uuid": "u", "access_token": None, "refresh_token": None, "devices": {},
            "rate_limit": {"until": T0 + 10**9, "step": 3},
        }))
        b = _make(tmp_path, monkeypatch)
        assert b._rate_limit_remaining() == b.RATE_LIMIT_MAX_BACKOFF

    @pytest.mark.parametrize("bad", [{"until": "x"}, {"step": "y"}, "garbage", [1], {"until": None, "step": None}])
    def test_corrupt_persisted_state_is_ignored(self, tmp_path, monkeypatch, clock, bad):
        (tmp_path / "config.json").write_text(json.dumps({
            "device_uuid": "u", "access_token": None, "refresh_token": None, "devices": {},
            "rate_limit": bad,
        }))
        try:
            b = _make(tmp_path, monkeypatch)
        except AttributeError:
            pytest.fail("corrupt rate_limit config must not crash startup")
        assert b._rate_limit_remaining() == 0

    def test_writes_only_on_transitions_not_per_detection(self, bridge):
        with patch.object(bridge, "_save_config") as save:
            bridge._enter_rate_limit("HTTP 429")
            writes_after_first = save.call_count
            for _ in range(20):
                bridge._enter_rate_limit("HTTP 429")  # inside window
            assert save.call_count == writes_after_first == 1


# ───────────────────── observability ─────────────────────

class TestObservability:
    def test_requests_counted_by_category(self, bridge):
        bridge.session.request = MagicMock(return_value=_good_status())
        bridge.request("GET", "/status", serial="SN")
        bridge.request("GET", "/user-info")
        bridge.request("POST", "/off", serial="SN")
        bridge.session.post = MagicMock(return_value=_resp(200, {"result": {"accessToken": "a", "refreshToken": "b"}}))
        bridge.login()
        c = bridge._requests_diagnostics()
        assert (c["status"], c["other"], c["command"], c["auth"], c["total"]) == (1, 1, 1, 1, 4)

    def test_401_retry_counts_both_attempts(self, bridge):
        bridge.session.request = MagicMock(side_effect=[_resp(401, {"success": False}), _good_status()])
        bridge.refresh_session = MagicMock(return_value=True)
        bridge.request("GET", "/status", serial="SN")
        assert bridge._requests_diagnostics()["status"] == 2

    def test_counters_reset_at_utc_day_rollover(self, bridge):
        bridge._count_request("status")
        bridge._request_counters["day"] = "1999-01-01"
        bridge._count_request("status")
        assert bridge._request_counters["total"] == 1
        assert bridge._request_counters["day"] == bridge._utc_day()

    def test_counter_flush_is_throttled(self, bridge, clock):
        bridge._count_request("status")
        with patch.object(bridge, "_save_config") as save:
            bridge._maybe_flush_counters()      # first flush (never flushed)
            bridge._count_request("status")
            bridge._maybe_flush_counters()      # throttled
            clock.now += bridge.COUNTER_FLUSH_INTERVAL + 1
            bridge._maybe_flush_counters()
            assert save.call_count == 2
            bridge._maybe_flush_counters(force=True)  # clean -> nothing to write
            assert save.call_count == 2

    def test_counters_restored_same_day_only(self, tmp_path, monkeypatch, clock):
        b1 = _make(tmp_path, monkeypatch)
        b1._count_request("status")
        b1._count_request("command")
        b1._maybe_flush_counters(force=True)
        b2 = _make(tmp_path, monkeypatch)
        assert b2._request_counters["total"] == 2
        saved = json.loads((tmp_path / "config.json").read_text())
        saved["request_counters"]["day"] = "1999-01-01"
        (tmp_path / "config.json").write_text(json.dumps(saved))
        b3 = _make(tmp_path, monkeypatch)
        assert b3._request_counters["total"] == 0

    def test_diagnostics_expose_rate_limit_and_no_secrets(self, bridge):
        bridge.config["access_token"] = "SECRET-ACCESS"
        bridge.config["refresh_token"] = "SECRET-REFRESH"
        bridge.request = MagicMock(return_value=_resp(200, DAILY))
        bridge.poll_status("SN")
        raw = _published(bridge, "P/SN/diagnostics")[-1].args[1]
        d = json.loads(raw)
        assert set(d["rate_limit"]) == {"active", "retry_at", "backoff_step", "last_reason", "last_at", "events_total"}
        assert {"status", "command", "auth", "other", "total", "rate_limited_responses", "blocked", "day"} <= set(d["requests"])
        for secret in ("SECRET-ACCESS", "SECRET-REFRESH", bridge.API_KEY):
            assert secret not in raw

    def test_discovery_includes_new_diagnostic_sensors_with_bridge_availability(self, bridge):
        bridge.publish_discovery("SN", "HWS")
        for slug in ("rate_limit_status", "api_requests"):
            payload = json.loads(_published(bridge, f"homeassistant/sensor/SN/{slug}/config")[-1].args[1])
            assert payload["entity_category"] == "diagnostic"
            assert payload["availability_topic"] == bridge_module.AVAILABILITY_TOPIC

    def test_discovery_templates_render(self, bridge):
        jinja2 = pytest.importorskip("jinja2")
        bridge.publish_discovery("SN", "HWS")
        diag = {"rate_limit": {"active": True}, "requests": {"total": 7}}
        env = jinja2.Environment()
        status = json.loads(_published(bridge, "homeassistant/sensor/SN/rate_limit_status/config")[-1].args[1])
        reqs = json.loads(_published(bridge, "homeassistant/sensor/SN/api_requests/config")[-1].args[1])
        assert env.from_string(status["value_template"]).render(value_json=diag) == "limited"
        assert env.from_string(reqs["value_template"]).render(value_json=diag) == "7"
