"""Cross-system safety and stubbed end-to-end tests.

Scenario under review: a 70 C HWS boost was confirmed, the Thermowatt API then
rate-limits, and the baseline handback fails. The Home Assistant side is modelled
by rendering Package 19's *actual* template expressions (copied verbatim below;
see test_templates_match_package19_when_available) against the diagnostics this
bridge really publishes, so the proof covers the real contract surface:

  * ownership is released by exactly TWO legitimate paths: (a) a confirmed baseline
    restoration (final wait_template true), or (b) a recognised native non-Manual mode
    (Off/Eco/Auto/Holiday) taking authority at one of the two native-mode guards, which
    issues no temperature command. An unknown/unavailable/unrecognised mode is
    fail-closed: no command, ownership untouched
  * the bridge never emits a queued/replayed physical command
  * the outstanding handback is identifiable from published diagnostics

No alternative actuator is introduced and no handback authority is changed: the
bridge only reports; Package 19 alone decides and clears ownership.
"""

import datetime
import itertools
import json
import os
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
LOGIN_OK = {"result": {"accessToken": "a", "refreshToken": "r"}}

# --- Package 19 template expressions, verbatim (packages/19_hws_thermowatt_translation_v1.yaml) ---
CMD = "state_attr('sensor.outside_hws_hws_command_status', 'TEMP')"
WAIT_TEMP_50 = (
    "{{ is_state('binary_sensor.hws_hws_heating', 'off') "
    f"and {CMD} is not none "
    f"and {CMD}.status == 'confirmed' "
    f"and {CMD}.requested == 50 }}}}"
)
WAIT_BASELINE = (
    "{{ (state_attr('water_heater.hws_boiler_hws', 'temperature') | float(None) == baseline_c) "
    f"and {CMD} is not none "
    f"and {CMD}.status == 'confirmed' "
    f"and ({CMD}.requested | float(None)) == baseline_c }}}}"
)
TELEMETRY_FRESH = """
{% set poll_status = states('sensor.outside_hws_hws_poll_status') %}
{% set consecutive_poll_failures = states('sensor.outside_hws_hws_consecutive_poll_failures') | int(None) %}
{% set last_poll_raw = states('sensor.outside_hws_hws_last_successful_poll') %}
{% set last_poll_age_s = ((as_datetime(now()) - as_datetime(last_poll_raw)).total_seconds())
   if last_poll_raw not in ['unknown', 'unavailable', none, ''] else none %}
{% set telemetry_fresh = poll_status == 'ok'
   and consecutive_poll_failures is not none
   and consecutive_poll_failures == 0
   and last_poll_age_s is not none
   and last_poll_age_s <= 120 %}
{{ telemetry_fresh }}
"""
# Native-mode authority guards (Issue #400): guard 1 before the optional TEMP 50, guard 2
# before the baseline restore. Verbatim from Package 19.
GUARD_KNOWN_NON_MANUAL = "{{ states('water_heater.hws_boiler_hws') in ['Off', 'Eco', 'Auto', 'Holiday'] }}"
GUARD_NOT_MANUAL = "{{ not is_state('water_heater.hws_boiler_hws', 'Manual') }}"
BRIDGE_ENTITY_USABLE = "{{ states('water_heater.hws_boiler_hws') not in ['unknown', 'unavailable'] }}"
COMMAND_STATUS_HEALTHY = "{{ states('sensor.outside_hws_hws_command_status') in ['idle', 'confirmed'] }}"


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


@pytest.fixture
def bridge(tmp_path, monkeypatch, clock):
    monkeypatch.setattr(bridge_module, "CONFIG_FILE", str(tmp_path / "config.json"))
    with patch("thermowatt_bridge.mqtt.Client") as mock_client:
        mqtt_client = MagicMock()
        mock_client.return_value = mqtt_client
        instance = MyThermowattBridge()
        instance.mqtt_client = mqtt_client
    instance.config["devices"]["SN"] = {"name": "HWS", "last_setpoint": 60}
    return instance


def _resp(code=200, body=None, json_error=False):
    r = MagicMock()
    r.status_code = code
    r.headers = {}
    if json_error:
        r.json.side_effect = ValueError("no body")
    else:
        r.json.return_value = body
    return r


def _message(topic, payload):
    msg = MagicMock()
    msg.retain = False
    msg.topic = topic
    msg.payload = payload.encode()
    return msg


class StubAPI:
    """Minimal Thermowatt cloud: quota window, instant command application."""

    def __init__(self, clock):
        self.clock = clock
        self.limit_from = self.limit_until = None
        self.setpoint, self.t_avg, self.mode_cmd = 60, 60.0, 9
        self.requests = []   # (time, method, path)
        self.logins = 0

    @property
    def heating(self):
        return self.setpoint > self.t_avg

    def limited(self):
        lo, hi = self.limit_from, self.limit_until
        return lo is not None and lo <= self.clock.now < hi

    def post(self, url, **kwargs):  # login / refresh
        self.logins += 1
        self.requests.append((self.clock.now, "POST", url.rsplit("/", 1)[1]))
        return _resp(200, DAILY) if self.limited() else _resp(200, LOGIN_OK)

    def request(self, method, url, **kwargs):
        path = url.split("/api/v1", 1)[1]
        self.requests.append((self.clock.now, method, path))
        if self.limited():
            return _resp(200, DAILY)
        if method == "GET" and path == "/status":
            return _resp(200, {"result": {
                "WaterHeaterSts": int(self.heating), "T_Avg": self.t_avg,
                "T_SetPoint": self.setpoint, "Cmd": self.mode_cmd}})
        if method == "GET" and path == "/user-info":
            return _resp(200, {"result": {"termostati": [{"seriale": "SN", "nome": "HWS"}]}})
        if method == "POST":
            body = kwargs.get("json") or {}
            if "T_SetPoint" in body:
                self.setpoint = body["T_SetPoint"]
            if path == "/eco":
                self.mode_cmd = 3
            return _resp(200, None, json_error=True)  # body-less success
        raise AssertionError(f"unexpected request {method} {path}")

    def command_posts(self):
        return [(t, m, path.lstrip("/")) for t, m, path in self.requests
                if m == "POST" and path.lstrip("/") in ("manual", "eco", "auto", "holiday", "off")]


class HA:
    """Home Assistant view of the bridge + the durable ownership flag."""

    def __init__(self, bridge, clock):
        self.bridge, self.clock = bridge, clock
        self.ownership = True
        self.mode_override = None   # force the water_heater state (e.g. 'unavailable')
        self.env = jinja2.Environment()
        self.env.globals.update(
            states=self.states, state_attr=self.state_attr, is_state=self.is_state,
            as_datetime=lambda v: datetime.datetime.fromisoformat(v) if isinstance(v, str) else v,
            now=lambda: datetime.datetime.fromtimestamp(clock.now, tz=datetime.timezone.utc),
        )

    def _last(self, topic):
        calls = [c for c in self.bridge.mqtt_client.publish.call_args_list if c.args and c.args[0] == topic]
        return json.loads(calls[-1].args[1]) if calls else None

    def _result(self):
        status = self._last("P/SN/STATUS")
        return (status or {}).get("result", {})

    def states(self, entity):
        diag, res = self._last("P/SN/diagnostics") or {}, self._result()
        return {
            "water_heater.hws_boiler_hws": self.mode_override or res.get("ha_mode") or "unknown",
            "binary_sensor.hws_hws_heating": {True: "on", False: "off"}.get(res.get("heating"), "unknown"),
            "sensor.outside_hws_hws_command_status": diag.get("command_status", "unknown"),
            "sensor.outside_hws_hws_poll_status": diag.get("poll_status", "unknown"),
            "sensor.outside_hws_hws_consecutive_poll_failures": str(diag.get("consecutive_failures", "unknown")),
            "sensor.outside_hws_hws_last_successful_poll": diag.get("last_successful_poll") or "unknown",
        }[entity]

    def state_attr(self, entity, attr):
        if entity == "sensor.outside_hws_hws_command_status":
            return ((self._last("P/SN/diagnostics") or {}).get("commands") or {}).get(attr)
        res = self._result()
        return {"temperature": res.get("T_SetPoint"), "T_Avg": res.get("T_Avg")}.get(attr)

    def is_state(self, entity, value):
        return self.states(entity) == value

    def render(self, template, **variables):
        return self.env.from_string(template).render(**variables).strip() == "True"

    def telemetry_fresh(self):
        return self.render(TELEMETRY_FRESH)

    # --- the handback script, as a decision model over the real templates ----------------
    def _wait(self, template, **variables):
        """wait_template with a 60 s timeout, continue_on_timeout; bridge keeps cycling."""
        for _ in range(4):  # 0, 20, 40, 60 s
            if self.render(template, **variables):
                return True
            self.clock.now += 20
            self.bridge._run_poll_cycle()
        return self.render(template, **variables)

    def _set_temperature(self, value):
        self.bridge.on_mqtt_message(None, None, _message("P/SN/CMD/TEMP", str(value)))

    def _native_guard(self):
        """Package 19 native-mode authority guard. Returns 'proceed' | 'released' | 'stop'."""
        if self.render(GUARD_KNOWN_NON_MANUAL):
            self.ownership = False          # path (b): native choice is authoritative
            return "released"
        if self.render(GUARD_NOT_MANUAL):
            return "stop"                   # unknown/unrecognised: fail closed
        return "proceed"

    def run_handback(self, baseline_c, between_stages=None):
        if not self.ownership:
            return "no_ownership"
        # Bounded wait for the bridge entity to be usable, then a halting condition.
        usable = self.render(BRIDGE_ENTITY_USABLE)
        for _ in range(3):
            if usable:
                break
            self.clock.now += 20
            self.bridge._run_poll_cycle()
            usable = self.render(BRIDGE_ENTITY_USABLE)
        if not usable:
            return "bridge_unavailable_stop"
        heating = self.states("binary_sensor.hws_hws_heating")
        t_avg = self.state_attr("water_heater.hws_boiler_hws", "T_Avg")
        if heating == "on" and t_avg is not None and float(t_avg) > 50:
            guard = self._native_guard()
            if guard == "released":
                return "native_authority_released"
            if guard == "stop":
                return "native_mode_unknown_stop"
            self._set_temperature(50)
            if not self._wait(WAIT_TEMP_50):
                return "soft_stop_not_confirmed"   # ownership untouched
        if between_stages:
            between_stages()
        guard = self._native_guard()
        if guard == "released":
            return "native_authority_released"
        if guard == "stop":
            return "native_mode_unknown_stop"
        self._set_temperature(int(baseline_c))
        if not self._wait(WAIT_BASELINE, baseline_c=float(baseline_c)):
            return "baseline_not_confirmed"        # ownership untouched
        self.ownership = False                      # path (a): confirmed baseline restoration
        return "released"


@pytest.fixture
def world(bridge, clock):
    api = StubAPI(clock)
    bridge.session.request = MagicMock(side_effect=api.request)
    bridge.session.post = MagicMock(side_effect=api.post)
    ha = HA(bridge, clock)
    bridge._run_poll_cycle()  # initial telemetry
    return api, ha, bridge, clock


def _confirmed_boost(world):
    """HA owns the element: TEMP 70 submitted and confirmed by a fresh readback."""
    api, ha, bridge, clock = world
    api.t_avg = 60.0
    bridge.on_mqtt_message(None, None, _message("P/SN/CMD/TEMP", "70"))
    clock.now += 20
    bridge._run_poll_cycle()
    rec = bridge._command_state["SN"]["TEMP"]
    assert (rec["status"], rec["requested"]) == ("confirmed", 70)
    assert ha.states("binary_sensor.hws_hws_heating") == "on"
    assert ha.ownership is True


def _start_quota(api, clock, seconds=1500):
    api.limit_from, api.limit_until = clock.now, clock.now + seconds


def _recover(world, extra=120):
    """Advance until the API is healthy again and the bridge has polled successfully."""
    api, ha, bridge, clock = world
    clock.now = max(clock.now, api.limit_until) + 1
    for _ in range(80):
        bridge._run_poll_cycle()
        if bridge.rate_limit_backoff == 0:
            return
        clock.now += max(bridge._rate_limit_remaining(), 60) + 1
    raise AssertionError("bridge never recovered")


class TestBoostRateLimitHandbackFailure:
    def test_quota_before_soft_stop_retains_ownership_and_emits_nothing(self, world):
        api, ha, bridge, clock = world
        _confirmed_boost(world)
        posts_before = list(api.command_posts())
        _start_quota(api, clock)
        clock.now += 60
        bridge._run_poll_cycle()                         # poll hits the quota
        assert bridge._rate_limit_remaining() > 0

        assert ha.run_handback(baseline_c=55) == "soft_stop_not_confirmed"
        assert ha.ownership is True                     # retained, nothing released
        assert api.command_posts() == posts_before      # refused locally: nothing reached the API
        rec = bridge._command_state["SN"]["TEMP"]
        assert (rec["status"], rec["requested"]) == ("submit_failed", 50)
        assert rec["error"].startswith("rate_limited")

        # EMHASS republish / HA start retries while the quota is still exhausted. Once a
        # bridge window has elapsed a caller-initiated command may probe the API, but the
        # API rejects it: the device is never changed, ownership is retained, and the
        # spacing between any two API requests honours the backoff.
        detection_time = T0 + 80
        for _ in range(3):
            clock.now += 120
            assert ha.run_handback(baseline_c=55) == "soft_stop_not_confirmed"
        assert ha.ownership is True
        assert api.setpoint == 70 and api.t_avg == 60.0   # no accepted physical write
        after = [t for t, _, _ in api.requests if t > detection_time]
        gaps = [b - a for a, b in itertools.pairwise(after)]
        assert all(g >= bridge.RATE_LIMIT_BASE_BACKOFF for g in gaps), gaps

    def test_command_hits_quota_during_baseline_stage_fails_closed_with_single_attempt(self, world):
        api, ha, bridge, clock = world
        _confirmed_boost(world)

        def quota_starts_after_soft_stop():
            _start_quota(api, clock)

        result = ha.run_handback(baseline_c=55, between_stages=quota_starts_after_soft_stop)
        assert result == "baseline_not_confirmed"
        assert ha.ownership is True
        rec = bridge._command_state["SN"]["TEMP"]
        assert (rec["status"], rec["requested"]) == ("submit_failed", 55)
        assert "Daily request limit exceeded" in rec["error"]
        baseline_attempts = [r for r in api.requests if r[1] == "POST" and r[0] >= T0 + 20 and r[2] == "/manual"]
        assert len(baseline_attempts) == 2  # TEMP 50 (accepted) + exactly one baseline attempt
        assert api.setpoint == 50                        # device untouched by the rejected write

    @pytest.mark.parametrize("scenario", ["before_soft_stop", "during_baseline"])
    def test_no_replay_after_recovery_and_ownership_only_cleared_by_confirmed_handback(self, world, scenario):
        api, ha, bridge, clock = world
        _confirmed_boost(world)
        if scenario == "before_soft_stop":
            _start_quota(api, clock)
            clock.now += 60
            bridge._run_poll_cycle()
            ha.run_handback(baseline_c=55)
        else:
            ha.run_handback(baseline_c=55, between_stages=lambda: _start_quota(api, clock))
        assert ha.ownership is True
        posts_at_failure = list(api.command_posts())

        _recover(world)
        # Recovered and polling again, yet the bridge itself emitted nothing.
        assert api.command_posts() == posts_at_failure
        assert ha.ownership is True
        assert not any(r["status"] == "pending" for r in bridge._command_state["SN"].values())
        # Outstanding handback remains visible: the failed command is still reported.
        assert ha.states("sensor.outside_hws_hws_command_status") == "submit_failed"

        # Next trigger: a fresh, single, caller-initiated attempt completes the handback.
        clock.now += 30
        assert ha.run_handback(baseline_c=55) == "released"
        assert ha.ownership is False
        new_posts = api.command_posts()[len(posts_at_failure):]
        assert [p[2] for p in new_posts] and len(new_posts) <= 2
        assert api.setpoint == 55
        assert ha.states("sensor.outside_hws_hws_command_status") == "confirmed"

    def test_operator_can_identify_outstanding_handback_promptly(self, world):
        api, ha, bridge, clock = world
        _confirmed_boost(world)
        _start_quota(api, clock)
        clock.now += 60
        bridge._run_poll_cycle()
        ha.run_handback(baseline_c=55)
        diag = ha._last("P/SN/diagnostics")
        # Durable HA evidence (ownership on) + bridge evidence, all published immediately.
        assert ha.ownership is True
        assert diag["poll_status"] == "rate_limited"
        assert diag["telemetry"]["state"] == "rate_limited"
        assert diag["rate_limit"]["active"] is True and diag["rate_limit"]["retry_at"]
        assert diag["command_status"] == "submit_failed"
        assert diag["commands"]["TEMP"]["requested"] == 50
        assert diag["commands"]["TEMP"]["error"].startswith("rate_limited")

    def test_package19_acquisition_stays_fail_closed_through_outage_and_recovery(self, world):
        api, ha, bridge, clock = world
        assert ha.telemetry_fresh() is True
        _start_quota(api, clock)
        clock.now += 60
        bridge._run_poll_cycle()
        assert ha.telemetry_fresh() is False             # immediately, not after 5 failures
        clock.now += 600
        bridge._publish_diagnostics("SN")
        assert ha.telemetry_fresh() is False
        _recover(world)
        assert ha.telemetry_fresh() is True              # genuine recovery restores freshness

    def test_failed_command_blocks_new_acquisition_until_a_command_confirms(self, world):
        api, ha, bridge, clock = world
        _start_quota(api, clock)
        clock.now += 60
        bridge._run_poll_cycle()
        bridge.on_mqtt_message(None, None, _message("P/SN/CMD/TEMP", "50"))
        assert ha.render(COMMAND_STATUS_HEALTHY) is False


NATIVE_CMD = {"Off": 8, "Eco": 3, "Auto": 17, "Holiday": 65}


def _native_mode(world, mode_cmd):
    api, ha, bridge, clock = world
    api.mode_cmd = mode_cmd
    clock.now += 60
    bridge._run_poll_cycle()


class TestOwnershipReleasePaths:
    """Package 19 releases ownership via (a) confirmed baseline or (b) native non-Manual authority."""

    def test_path_a_confirmed_baseline_releases(self, world):
        api, ha, bridge, clock = world
        _confirmed_boost(world)
        assert ha.run_handback(baseline_c=55) == "released"
        assert ha.ownership is False and api.setpoint == 55

    @pytest.mark.parametrize("mode", list(NATIVE_CMD))
    @pytest.mark.parametrize("element_heating", [True, False], ids=["guard1", "guard2"])
    def test_path_b_native_non_manual_mode_releases_without_any_command(self, world, mode, element_heating):
        api, ha, bridge, clock = world
        _confirmed_boost(world)
        if not element_heating:
            api.t_avg = 80.0                     # setpoint 70 < T_Avg: not heating -> guard 2 only
        _native_mode(world, NATIVE_CMD[mode])
        assert ha.states("water_heater.hws_boiler_hws") == mode
        assert ha.states("binary_sensor.hws_hws_heating") == ("on" if element_heating else "off")
        posts = list(api.command_posts())
        assert ha.run_handback(baseline_c=55) == "native_authority_released"
        assert ha.ownership is False
        assert api.command_posts() == posts       # no temperature command issued
        assert api.setpoint == 70

    @pytest.mark.parametrize("mode", list(NATIVE_CMD))
    def test_native_mode_taking_over_mid_handback_skips_baseline(self, world, mode):
        api, ha, bridge, clock = world
        _confirmed_boost(world)
        posts = len(api.command_posts())
        result = ha.run_handback(baseline_c=55, between_stages=lambda: _native_mode(world, NATIVE_CMD[mode]))
        assert result == "native_authority_released"
        assert ha.ownership is False
        assert [p[2] for p in api.command_posts()[posts:]] == ["manual"]   # soft stop only; no baseline
        assert api.setpoint == 50

    def test_unknown_mode_at_entry_fails_closed(self, world):
        api, ha, bridge, clock = world
        _confirmed_boost(world)
        _native_mode(world, 4)                    # unrecognised Cmd family -> ha_mode None -> 'unknown'
        assert ha.states("water_heater.hws_boiler_hws") == "unknown"
        posts = list(api.command_posts())
        assert ha.run_handback(baseline_c=55) == "bridge_unavailable_stop"
        assert ha.ownership is True and api.command_posts() == posts

    def test_unavailable_entity_at_entry_fails_closed(self, world):
        api, ha, bridge, clock = world
        _confirmed_boost(world)
        ha.mode_override = "unavailable"
        posts = list(api.command_posts())
        assert ha.run_handback(baseline_c=55) == "bridge_unavailable_stop"
        assert ha.ownership is True and api.command_posts() == posts

    @pytest.mark.parametrize("mode", ["Boost", "manual", "Heat"])
    @pytest.mark.parametrize("element_heating", [True, False], ids=["guard1", "guard2"])
    def test_unrecognised_mode_fails_closed_with_no_command(self, world, mode, element_heating):
        api, ha, bridge, clock = world
        _confirmed_boost(world)
        if not element_heating:
            api.t_avg = 80.0
            clock.now += 60
            bridge._run_poll_cycle()
        ha.mode_override = mode
        posts = list(api.command_posts())
        assert ha.run_handback(baseline_c=55) == "native_mode_unknown_stop"
        assert ha.ownership is True and api.command_posts() == posts

    def test_unknown_mode_mid_run_blocks_baseline_and_retains_ownership(self, world):
        api, ha, bridge, clock = world
        _confirmed_boost(world)
        posts = len(api.command_posts())
        result = ha.run_handback(baseline_c=55, between_stages=lambda: _native_mode(world, 4))
        assert result == "native_mode_unknown_stop"
        assert ha.ownership is True
        assert [p[2] for p in api.command_posts()[posts:]] == ["manual"]   # soft stop only
        assert api.setpoint == 50

    def test_rate_limit_plus_native_authority_still_releases_without_commands(self, world):
        """If the user takes the heater native while the API is limited, Package 19 still
        releases ownership (path b) and the bridge sends nothing."""
        api, ha, bridge, clock = world
        _confirmed_boost(world)
        _native_mode(world, NATIVE_CMD["Off"])    # last good telemetry shows native Off
        _start_quota(api, clock)
        clock.now += 60
        bridge._run_poll_cycle()
        assert bridge._rate_limit_remaining() > 0
        posts = list(api.command_posts())
        assert ha.run_handback(baseline_c=55) == "native_authority_released"
        assert ha.ownership is False and api.command_posts() == posts

    def test_rate_limit_with_unknown_native_mode_retains_ownership(self, world):
        api, ha, bridge, clock = world
        _confirmed_boost(world)
        ha.mode_override = "Boost"
        _start_quota(api, clock)
        clock.now += 60
        bridge._run_poll_cycle()
        posts = list(api.command_posts())
        assert ha.run_handback(baseline_c=55) == "native_mode_unknown_stop"
        assert ha.ownership is True and api.command_posts() == posts


class TestStubbedEndToEnd:
    """Drives MyThermowattBridge.run() against a stub API over a scripted timeline."""

    class LoopEvent:
        def __init__(self, clock, hooks, stop_at):
            self.clock, self.hooks, self.stop_at, self.flag = clock, sorted(hooks, key=lambda h: h[0]), stop_at, False

        def set(self):
            self.flag = True

        def clear(self):
            self.flag = False

        def is_set(self):
            return self.flag

        def wait(self, timeout=None):
            if self.clock.now >= self.stop_at:
                raise KeyboardInterrupt
            if self.flag:
                return True
            end = self.clock.now + timeout
            while self.hooks and self.hooks[0][0] <= end:
                when, fn = self.hooks.pop(0)
                self.clock.now = max(self.clock.now, when)
                fn()
                if self.flag:
                    return True
            self.clock.now = end
            return False

    def test_normal_polling_quota_backoff_refused_commands_and_recovery(self, bridge, clock, monkeypatch):
        monkeypatch.setattr(bridge_module, "EMAIL", "e")
        monkeypatch.setattr(bridge_module, "PASSWORD", "p")
        monkeypatch.setattr(bridge_module.time, "sleep", lambda s: setattr(clock, "now", clock.now + s))
        api = StubAPI(clock)
        api.limit_from, api.limit_until = T0 + 150, T0 + 540
        bridge.session.request = MagicMock(side_effect=api.request)
        bridge.session.post = MagicMock(side_effect=api.post)

        def cmd(topic, payload):
            return lambda: bridge.on_mqtt_message(None, None, _message(topic, payload))

        hooks = [
            (T0 + 200, cmd("P/SN/CMD/MODE", "Off")),   # inside window 1: refused locally
            (T0 + 400, cmd("P/SN/CMD/TEMP", "50")),    # inside window 2: refused locally
            (T0 + 610, cmd("P/SN/CMD/MODE", "Eco")),   # after recovery: normal accepted command
        ]
        bridge._poll_wakeup = self.LoopEvent(clock, hooks, stop_at=T0 + 700)
        bridge.run()

        status_times = [t - T0 for t, m, p in api.requests if m == "GET" and p == "/status"]
        # 60 s cadence, then a 120 s window (180->300), a 240 s window (300->540), recovery.
        assert status_times[:6] == [0, 60, 120, 180, 300, 540]
        assert not [t for t in status_times if 180 < t < 300 or 300 < t < 540]
        assert bridge.rate_limit_backoff == 0 and bridge._rate_limit_remaining() == 0

        # Only startup login; no re-authentication was triggered by quota errors.
        assert api.logins == 1
        # Exactly one physical command ever reached the API: the post-recovery Eco.
        posts = api.command_posts()
        assert [(round(t - T0), p) for t, _, p in posts] == [(610, "eco")]
        # Commands refused during backoff were reported, not replayed.
        assert bridge._command_state["SN"]["TEMP"]["status"] == "submit_failed"
        assert bridge._command_state["SN"]["MODE"]["requested"] == 3  # Eco replaced the refused Off
        counters = bridge._requests_diagnostics()
        assert counters["rate_limited_responses"] == 2 and counters["blocked"] >= 2
        assert bridge._rate_limit_events_total == 2
        # Final published diagnostics: recovered.
        diags = [json.loads(c.args[1]) for c in bridge.mqtt_client.publish.call_args_list
                 if c.args and c.args[0] == "P/SN/diagnostics"]
        assert any(d["rate_limit"]["active"] for d in diags)
        assert diags[-1]["rate_limit"]["active"] is False and diags[-1]["poll_status"] == "ok"
        saved = json.loads(pathlib.Path(bridge_module.CONFIG_FILE).read_text())
        assert saved["rate_limit"]["step"] == 0


def test_templates_match_package19_when_available():
    """Set THERMOWATT_P19_YAML to the Package 19 file to prove the copied templates are verbatim."""
    path = os.environ.get("THERMOWATT_P19_YAML")
    if not path or not os.path.exists(path):
        pytest.skip("THERMOWATT_P19_YAML not set")
    text = " ".join(pathlib.Path(path).read_text(encoding="utf-8").split())
    normalize = lambda t: " ".join(t.split())  # noqa: E731
    for template in (WAIT_TEMP_50, WAIT_BASELINE):
        inner = normalize(template).removeprefix("{{ ").removesuffix(" }}")
        assert inner in text, inner[:80]
    for fragment in (
        ("{% set telemetry_fresh = poll_status == 'ok' and consecutive_poll_failures is not none "
         "and consecutive_poll_failures == 0 and last_poll_age_s is not none and last_poll_age_s <= 120 %}"),
        "{% set command_status_healthy = command_status in ['idle', 'confirmed'] %}",
        GUARD_KNOWN_NON_MANUAL.removeprefix("{{ ").removesuffix(" }}"),
        GUARD_NOT_MANUAL.removeprefix("{{ ").removesuffix(" }}"),
        BRIDGE_ENTITY_USABLE.removeprefix("{{ ").removesuffix(" }}"),
    ):
        assert fragment in text
