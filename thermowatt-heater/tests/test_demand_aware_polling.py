"""Stage 3 (issue #17): demand-aware polling policy. Unit level; the 48 h behaviour is in
test_demand_aware_simulation.py. Default behaviour (option off) must be byte-for-byte the 1.9.0 policy."""
import os
import re
import subprocess
import sys
import time
from types import SimpleNamespace
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
    b._save_config = lambda: None
    return b


@pytest.fixture
def da(bridge):
    """Demand-aware ON at the production interval, with HA actively requesting."""
    bridge.DEMAND_AWARE_FLAG = True
    bridge.POLL_INTERVAL = 180
    bridge.current_poll_interval = 180
    bridge._last_demand_at = time.time()
    return bridge


def healthy(b, age):
    b._last_successful_poll["SN"] = time.time() - age
    b._consecutive_failures["SN"] = 0


def msg(sn="SN", retain=False, payload=b"1"):
    return SimpleNamespace(topic=f"P/{sn}/refresh", payload=payload, retain=retain)


MARK = b"precycle"


# ---- configuration contract ------------------------------------------------

def _flag(value):
    env = {k: v for k, v in os.environ.items() if k != "THERMOWATT_DEMAND_AWARE"}
    if value is not None:
        env["THERMOWATT_DEMAND_AWARE"] = value
    env["PYTHONPATH"] = os.pathsep.join([ADDON] + sys.path)
    code = ("import sys; sys.argv=['x']; import thermowatt_bridge as m; "
            "print(m.MyThermowattBridge.DEMAND_AWARE_FLAG)")
    out = subprocess.run([sys.executable, "-c", code], env=env, capture_output=True,
                         text=True, timeout=60, cwd=ADDON)
    assert out.returncode == 0, out.stderr
    return out.stdout.strip().splitlines()[-1] == "True"


@pytest.mark.parametrize("value,expected", [
    (None, False), ("", False), ("0", False), ("false", False), ("off", False),
    ("garbage", False), ("2", False), ("1", True), ("true", True), (" True ", True),
    ("yes", True), ("ON", True),
])
def test_env_parsing_is_default_off_and_strict(value, expected):
    assert _flag(value) is expected


def test_addon_option_declared_default_false_optional_and_version_not_bumped():
    text = open(os.path.join(ADDON, "config.yaml")).read()
    options, schema = text.split("schema:")
    assert re.search(r"^\s+demand_aware_polling:\s*false\s*$", options, re.M)
    assert re.search(r"^\s+demand_aware_polling:\s*bool\?\s*$", schema, re.M)
    # Merging this change must not itself ship an update to an auto_update add-on.
    assert re.search(r'^version:\s*"1\.9\.0"\s*$', text, re.M)


def test_run_sh_derives_the_env_only_from_the_option():
    text = open(os.path.join(ADDON, "run.sh")).read()
    assert "bashio::config.true 'demand_aware_polling'" in text
    assert "THERMOWATT_DEMAND_AWARE=1" in text and "THERMOWATT_DEMAND_AWARE=0" in text
    assert "export THERMOWATT_DEMAND_AWARE" in text


# ---- invariants that must never drift --------------------------------------

def test_freshness_and_offline_contracts_unchanged_and_margins_hold(bridge):
    assert bridge.TELEMETRY_STALE_AFTER == 120
    assert bridge.OFFLINE_AFTER_S == 300
    assert bridge.POLL_INTERVAL_CONFIRM == 20 and bridge.CONFIRM_WINDOW == 60
    # reuse age + Package 19 lead (:30 -> :00) + 10 s jitter/start-up margin must fit the 120 s gate
    assert bridge.DEMAND_REUSE_MAX_AGE_S + 30 + 10 <= bridge.TELEMETRY_STALE_AFTER
    # the deadline poll leaves room for one transport-failure retry plus a round trip inside 300 s
    assert bridge.OFFLINE_SAFETY_MARGIN_S >= bridge.DEMAND_RETRY_DELAY + 3
    assert bridge.DEMAND_ACTIVE_WINDOW_S > 2 * 300


def test_default_off_policy_is_the_legacy_one(bridge):
    bridge.POLL_INTERVAL = 180
    bridge._last_demand_at = time.time()
    healthy(bridge, 5)
    assert not bridge._demand_aware_enabled()
    assert bridge._background_wait_s(180) == 180
    assert not bridge._demand_reusable("SN", time.time())


@pytest.mark.parametrize("interval", [60, 90, 120])
def test_ignored_at_or_below_package19_gate(da, interval):
    da.POLL_INTERVAL = interval
    assert not da._demand_aware_enabled()
    healthy(da, 5)
    assert da._background_wait_s(interval) == interval


@pytest.mark.parametrize("interval", [121, 180, 300])
def test_enabled_above_package19_gate(da, interval):
    da.POLL_INTERVAL = interval
    assert da._demand_aware_enabled()


# ---- demand activity (bounded fallback when HA stops asking) ---------------

def test_demand_must_have_been_seen_recently(da):
    da._last_demand_at = None
    assert not da._demand_active()
    da._last_demand_at = time.time() - (da.DEMAND_ACTIVE_WINDOW_S + 1)
    assert not da._demand_active()
    da._last_demand_at = time.time() - (da.DEMAND_ACTIVE_WINDOW_S - 5)
    assert da._demand_active()


def test_no_demand_means_legacy_interval_even_when_healthy(da):
    healthy(da, 10)
    da._last_demand_at = None
    assert da._background_wait_s(180) == 180


def test_refresh_message_records_demand_even_during_backoff_but_not_for_unknown_or_retained(da):
    da._last_demand_at = None
    da.on_mqtt_message(None, None, msg(retain=True))
    da.on_mqtt_message(None, None, msg(sn="OTHER"))
    assert da._last_demand_at is None
    da.rate_limit_backoff = 1
    da._rate_limit_until = time.time() + 600
    da.on_mqtt_message(None, None, msg())
    assert da._last_demand_at is not None          # HA is alive...
    assert not da._demand_requested                 # ...but nothing is queued during backoff
    assert not da._poll_wakeup.is_set()


# ---- reuse of a recent healthy poll ----------------------------------------

def test_reuse_when_recent_and_healthy(da):
    healthy(da, 40)
    assert da._demand_reusable("SN", time.time(), marked=True)
    healthy(da, da.DEMAND_REUSE_MAX_AGE_S - 0.5)
    assert da._demand_reusable("SN", time.time(), marked=True)


def test_no_reuse_when_old_failed_never_polled_or_limited(da):
    healthy(da, da.DEMAND_REUSE_MAX_AGE_S + 2)
    assert not da._demand_reusable("SN", time.time(), marked=True)
    healthy(da, 10)
    da._consecutive_failures["SN"] = 1
    assert not da._demand_reusable("SN", time.time(), marked=True)
    da._last_successful_poll.pop("SN")
    da._consecutive_failures["SN"] = 0
    assert not da._demand_reusable("SN", time.time(), marked=True)
    healthy(da, 10)
    da.rate_limit_backoff = 1
    da._rate_limit_until = time.time() - 1          # recovery unverified
    assert not da._demand_reusable("SN", time.time(), marked=True)


def test_cycle_skips_request_for_reusable_demand_and_counts_it(da):
    healthy(da, 40)
    da.poll_status = MagicMock(return_value=(True, 200))
    da.on_mqtt_message(None, None, msg(payload=MARK))
    da._run_poll_cycle()
    da.poll_status.assert_not_called()
    assert (da._demand_stats["served"], da._demand_stats["reused"]) == (0, 1)
    assert da._demand_requested == set()


def test_cycle_serves_demand_when_telemetry_too_old(da):
    healthy(da, da.DEMAND_REUSE_MAX_AGE_S + 20)
    da.poll_status = MagicMock(return_value=(True, 200))
    da.on_mqtt_message(None, None, msg(payload=MARK))
    da._run_poll_cycle()
    da.poll_status.assert_called_once_with("SN")
    assert (da._demand_stats["served"], da._demand_stats["reused"]) == (1, 0)


def test_confirmation_window_is_never_throttled_by_reuse(da):
    healthy(da, 5)
    da._confirm_until = time.time() + 30
    da.current_poll_interval = da.POLL_INTERVAL_CONFIRM
    da.poll_status = MagicMock(return_value=(True, 200))
    da.on_mqtt_message(None, None, msg())
    da._run_poll_cycle()
    da.poll_status.assert_called_once()            # existing behaviour inside the window


def test_unrequested_background_cycle_always_polls(da):
    healthy(da, 5)
    da.poll_status = MagicMock(return_value=(True, 200))
    da._run_poll_cycle()
    da.poll_status.assert_called_once()


def test_option_off_requested_refresh_is_served_exactly_as_before(bridge):
    healthy(bridge, 5)
    bridge.poll_status = MagicMock(return_value=(True, 200))
    bridge.on_mqtt_message(None, None, msg())
    bridge._run_poll_cycle()
    bridge.poll_status.assert_called_once_with("SN")


# ---- deadline poll ----------------------------------------------------------

def test_wait_runs_to_the_deadline_not_a_fixed_interval(da):
    deadline_age = da.OFFLINE_AFTER_S - da.OFFLINE_SAFETY_MARGIN_S
    for age in (0, 28, 100, 250):
        healthy(da, age)
        assert da._background_wait_s(180) == pytest.approx(deadline_age - age, abs=0.2)


def test_overdue_deadline_polls_after_a_one_second_floor_not_a_spin(da):
    healthy(da, 295)
    assert da._background_wait_s(180) == 1.0


def test_only_the_normal_interval_is_stretched(da):
    healthy(da, 10)
    assert da._background_wait_s(da.POLL_INTERVAL_CONFIRM) == da.POLL_INTERVAL_CONFIRM
    assert da._background_wait_s(3600) == 3600      # backoff stretch


def test_unverified_recovery_uses_legacy_interval(da):
    healthy(da, 10)
    da.rate_limit_backoff = 1
    da._rate_limit_until = time.time() - 1
    assert da._background_wait_s(180) == 180


def test_never_polled_uses_legacy_interval(da):
    assert da._background_wait_s(180) == 180


def test_failed_deadline_poll_gets_exactly_one_quick_retry_inside_the_bound(da):
    da._consecutive_failures["SN"] = 1
    da._last_successful_poll["SN"] = time.time() - 290.4     # deadline poll just failed
    assert da._background_wait_s(180) == da.DEMAND_RETRY_DELAY
    da._last_successful_poll["SN"] = time.time() - 296.0     # the retry also failed: bound is gone...
    assert da._background_wait_s(180) == da.DEMAND_RECOVERY_RETRY_S     # ...one 30 s recovery retry
    assert da._background_wait_s(180) == 180                  # ...then the legacy interval
    da._last_successful_poll["SN"] = time.time() - 60       # a demand-served poll failed early: legacy
    assert da._background_wait_s(180) == 180


def test_multiple_devices_use_the_earliest_deadline_and_any_unpolled_falls_back(da):
    da.config["devices"]["SN2"] = {"name": "B", "last_setpoint": 58}
    da._last_successful_poll["SN"] = time.time() - 10
    da._consecutive_failures["SN"] = 0
    assert da._background_wait_s(180) == 180                 # SN2 never polled
    da._last_successful_poll["SN2"] = time.time() - 200
    da._consecutive_failures["SN2"] = 0
    assert da._background_wait_s(180) == pytest.approx(90.0, abs=0.2)


# ---- side effects that follow the new cadence ------------------------------

def _poll_energy(b, gap_s):
    b._last_poll_ts["SN"] = time.time() - gap_s
    resp = MagicMock(status_code=200)
    resp.json.return_value = {"result": {"WaterHeaterSts": 1, "T_Avg": 55.0, "T_SetPoint": 60.0, "Cmd": 9}}
    b.config["energy_kwh"] = {"SN": 0.0}
    with patch.object(b, "request", return_value=resp), \
         patch.object(b, "_compute_status", side_effect=lambda d: {"result": {"heating": True}}):
        b.poll_status("SN")
    return b.config["energy_kwh"]["SN"]


def test_energy_cap_covers_a_deadline_gap_and_stays_bounded(da):
    kwh_per_s = da.element_wattage / 1000 / 3600
    da.POLL_INTERVAL = 121                      # legacy cap would be 242 s; demand-aware floors it at 300 s
    assert _poll_energy(da, 290) == pytest.approx(kwh_per_s * 290, rel=1e-3)
    assert _poll_energy(da, 5000) == pytest.approx(kwh_per_s * 300, rel=1e-3)
    da.POLL_INTERVAL = 180
    assert _poll_energy(da, 290) == pytest.approx(kwh_per_s * 290, rel=1e-3)
    assert _poll_energy(da, 5000) == pytest.approx(kwh_per_s * 360, rel=1e-3)   # outage jump unchanged


def test_energy_cap_unchanged_when_off(bridge):
    kwh_per_s = bridge.element_wattage / 1000 / 3600
    assert _poll_energy(bridge, 5000) == pytest.approx(kwh_per_s * 120, rel=1e-3)


def test_proactive_margin_follows_the_wake_gap(da):
    """exp - now > margin returns without refreshing; margin must cover the 290 s gap."""
    da.PROACTIVE_REFRESH = True
    da.config["access_token"] = "t"
    da.config["refresh_token"] = "r"
    da.refresh_session = MagicMock(return_value=True)
    da._jwt_exp = lambda tok: time.time() + 340          # inside 290 + 60 = 350 s
    da._maybe_proactive_refresh()
    da.refresh_session.assert_called_once()
    da.refresh_session.reset_mock()
    da._proactive_attempt_exp = None
    da._proactive_last_attempt_at = None
    da._jwt_exp = lambda tok: time.time() + 400          # outside
    da._maybe_proactive_refresh()
    da.refresh_session.assert_not_called()


def test_proactive_margin_unchanged_when_off(bridge):
    bridge.PROACTIVE_REFRESH = True
    bridge.POLL_INTERVAL = 180
    bridge.config["access_token"] = "t"
    bridge.config["refresh_token"] = "r"
    bridge.refresh_session = MagicMock(return_value=True)
    bridge._jwt_exp = lambda tok: time.time() + 300      # > 180 + 60: no refresh as in 1.9.0
    bridge._maybe_proactive_refresh()
    bridge.refresh_session.assert_not_called()


def test_diagnostics_publish_effective_state_and_counters(da):
    da._demand_stats = {"served": 3, "reused": 7, "marked": 8, "forced": 2}
    da._publish_diagnostics("SN")
    payloads = [c.args[1] for c in da.mqtt_client.publish.call_args_list
                if c.args and c.args[0] == "P/SN/diagnostics"]
    import json
    body = json.loads(payloads[-1])
    assert body["demand_aware_polling"] is True
    assert body["demand_refresh"] == {"served": 3, "reused": 7, "marked": 8, "forced": 2}


def test_diagnostics_effective_state_is_false_when_ignored(da):
    da.POLL_INTERVAL = 60
    da._publish_diagnostics("SN")
    import json
    body = json.loads([c.args[1] for c in da.mqtt_client.publish.call_args_list
                       if c.args and c.args[0] == "P/SN/diagnostics"][-1])
    assert body["demand_aware_polling"] is False


# ---- arithmetic behind the design (see docs/STAGE3-DEMAND-AWARE-POLLING.md) --

def test_one_success_per_cycle_has_mean_gap_exactly_300_so_margin_needs_a_phase_sawtooth():
    """Gap_k = 300 + a_k - a_{k+1} for success ages a_k in [lo, hi] before cycle k. Keeping every gap
    <= 300 - m needs a_{k+1} >= a_k + m, which can last at most (hi-lo)/m cycles."""
    lo, hi, m = 0.0, 80.0, 10.0
    max_run = int((hi - lo) // m)
    a = lo
    gaps = []
    for _ in range(max_run):
        nxt = a + m
        gaps.append(300 + a - nxt)
        a = nxt
    assert all(g <= 300 - m for g in gaps)
    assert len(gaps) == 8                         # == cycles between demand-served polls in the simulation
    # one more cycle would need age 90 > 80: a reset costs a gap of 300 + 80, i.e. an extra poll
    assert 300 + a - lo > 300
    # extra polls per cycle for this margin: 1 per (run + 1) cycles
    assert 1 / (max_run + 1) == pytest.approx(1 / 9)


# ---- reuse only on explicit intent (handback freshness wait) -------------------------------------

def _pending(b):
    return b._demand_requested, b._demand_reuse_ok.get("SN")


@pytest.mark.parametrize("payload", [b"1", b"", b"0", b"PRECYCLE", b"precycle ", b" precycle", b"precycle\n",
                                     b"pre-cycle", b"reuse", b"true", b"\xff\xfe", None, 1, "1"])
def test_unknown_or_legacy_payloads_never_gain_reuse(da, payload):
    healthy(da, 40)
    da.on_mqtt_message(None, None, msg(payload=payload))
    assert da._demand_requested == {"SN"} and da._demand_reuse_ok["SN"] is False
    da.poll_status = MagicMock(return_value=(True, 200))
    da._run_poll_cycle()
    da.poll_status.assert_called_once_with("SN")                 # served with a real poll
    assert da._demand_stats["reused"] == 0 and da._demand_stats["forced"] == 1


@pytest.mark.parametrize("payload", [MARK, "precycle"])
def test_exact_marker_str_or_bytes_is_reusable(da, payload):
    healthy(da, 40)
    da.on_mqtt_message(None, None, msg(payload=payload))
    assert da._demand_reuse_ok["SN"] is True
    da.poll_status = MagicMock(return_value=(True, 200))
    da._run_poll_cycle()
    da.poll_status.assert_not_called()
    assert da._demand_stats["reused"] == 1 and da._demand_stats["marked"] == 1


def test_marked_request_still_needs_a_poll_when_telemetry_is_old_or_unhealthy(da):
    healthy(da, da.DEMAND_REUSE_MAX_AGE_S + 5)
    da.poll_status = MagicMock(return_value=(True, 200))
    da.on_mqtt_message(None, None, msg(payload=MARK))
    da._run_poll_cycle()
    da.poll_status.assert_called_once()


@pytest.mark.parametrize("first,second,expect_reuse", [
    (MARK, b"1", False),            # pre-cycle then handback
    (b"1", MARK, False),            # handback then pre-cycle: the marked one must not upgrade it
    (MARK, MARK, True),
    (b"1", b"1", False),
    (MARK, b"garbage", False),
    (b"garbage", MARK, False),
])
def test_overlapping_requests_forced_intent_wins_in_either_order(da, first, second, expect_reuse):
    healthy(da, 40)
    da.on_mqtt_message(None, None, msg(payload=first))
    da.on_mqtt_message(None, None, msg(payload=second))
    assert da._demand_reuse_ok["SN"] is expect_reuse
    da.poll_status = MagicMock(return_value=(True, 200))
    da._run_poll_cycle()
    assert (da.poll_status.call_count == 0) is expect_reuse
    assert da._demand_requested == set() and da._demand_reuse_ok == {}      # consumed together


def test_three_way_overlap_and_state_does_not_leak_into_the_next_cycle(da):
    healthy(da, 40)
    for pl in (MARK, b"1", MARK):
        da.on_mqtt_message(None, None, msg(payload=pl))
    da.poll_status = MagicMock(return_value=(True, 200))
    da._run_poll_cycle()
    assert da.poll_status.call_count == 1
    healthy(da, 40)
    da.on_mqtt_message(None, None, msg(payload=MARK))               # a fresh marked request is reusable again
    da._run_poll_cycle()
    assert da.poll_status.call_count == 1


def test_forced_refresh_is_subject_only_to_the_10s_attempt_coalescing(da):
    healthy(da, 40)
    da._last_status_attempt["SN"] = time.time() - 4                  # an attempt 4 s ago (any outcome)
    da.poll_status = MagicMock()
    da.on_mqtt_message(None, None, msg(payload=b"1"))
    da._run_poll_cycle()
    da.poll_status.assert_not_called()                               # existing coalescing, unchanged
    da._last_status_attempt["SN"] = time.time() - 11
    da.on_mqtt_message(None, None, msg(payload=b"1"))
    da.poll_status.return_value = (True, 200)
    da._run_poll_cycle()
    da.poll_status.assert_called_once()


def test_handback_refresh_is_never_satisfied_by_an_old_sample_at_any_time_of_the_cycle(da):
    """The adversarial case from review: ownership on, setpoint 50 or the restored baseline, last poll
    30-80 s old, plain "1" refresh at any moment of the five-minute grid (also the 266-299 s phase)."""
    da._setpoint_seen["SN"] = 50.0
    for age in (11, 30, 55, 80):
        for offset in (0.0, 10.0, 150.0, 266.0, 272.2, 290.2, 298.9):
            now = time.time()
            da._last_successful_poll["SN"] = now - age
            da._consecutive_failures["SN"] = 0
            assert not da._demand_reusable("SN", now, marked=False), (age, offset)


def test_v190_compatible_payload_is_ignored_by_name_not_by_position(da):
    assert da.DEMAND_PRECYCLE_PAYLOAD == "precycle"
    assert not hasattr(da, "DEMAND_REUSE_PHASE_S") and not hasattr(da, "_demand_phase_ok")


# ---- recovery retry after a failed deadline poll whose quick retry no longer fits --------------

def test_recovery_retry_is_used_once_per_success_stamp(da):
    da._consecutive_failures["SN"] = 1
    ts = time.time() - 296.0                                    # connect-timeout failure ended at age ~296
    da._last_successful_poll["SN"] = ts
    assert da._background_wait_s(180) == da.DEMAND_RECOVERY_RETRY_S
    assert da._background_wait_s(180) == 180                    # same stamp: legacy interval
    da._last_successful_poll["SN"] = time.time() - 295.0        # a later success followed by a new failure
    assert da._background_wait_s(180) == da.DEMAND_RECOVERY_RETRY_S


def test_recovery_retry_can_be_disabled_and_never_exceeds_the_interval(da):
    da._consecutive_failures["SN"] = 1
    da._last_successful_poll["SN"] = time.time() - 296.0
    da.DEMAND_RECOVERY_RETRY_S = 0
    assert da._background_wait_s(180) == 180
    da.DEMAND_RECOVERY_RETRY_S = 500
    da._last_successful_poll["SN"] = time.time() - 297.0
    assert da._background_wait_s(180) == 180


def test_an_early_failure_never_gets_the_recovery_retry(da):
    da._consecutive_failures["SN"] = 1
    da._last_successful_poll["SN"] = time.time() - 60
    assert da._background_wait_s(180) == 180


# ---- owned boost: always a fresh sample ------------------------------------------------

@pytest.mark.parametrize("setpoint,reusable", [(58.0, True), (64.9, True), (65.0, False), (70.0, False), (None, True)])
def test_boost_setpoint_disables_reuse(da, setpoint, reusable):
    healthy(da, 40)
    if setpoint is None:
        da._setpoint_seen.pop("SN", None)
    else:
        da._setpoint_seen["SN"] = setpoint
    assert da._demand_reusable("SN", time.time(), marked=True) is reusable


def test_setpoint_is_recorded_from_each_validated_poll_only(da):
    good = MagicMock(status_code=200)
    good.json.return_value = {"result": {"WaterHeaterSts": 1, "T_Avg": 55.0, "T_SetPoint": 70.0, "Cmd": 9}}
    with patch.object(da, "request", return_value=good), patch.object(da, "_save_config"):
        assert da.poll_status("SN") == (True, 200)
    assert da._setpoint_seen["SN"] == 70.0
    bad = MagicMock(status_code=200)
    bad.json.return_value = {"success": False, "error": "Water heater not found"}
    with patch.object(da, "request", return_value=bad):
        da.poll_status("SN")
    assert da._setpoint_seen["SN"] == 70.0              # an invalid payload never overwrites it
