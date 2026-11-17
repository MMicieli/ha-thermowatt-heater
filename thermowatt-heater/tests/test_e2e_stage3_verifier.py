"""Logic tests for the Stage 3 container verifier (verify_e2e.check_scenario_s) on SYNTHETIC logs.

These do NOT qualify the container: they only prove that the verifier passes a correct run and fails on
each injected violation. The real scenario needs Docker: tests/e2e/run_e2e.sh <name> S|S0.
"""
import copy
import importlib.util
import json
import pathlib

_spec = importlib.util.spec_from_file_location("verify_e2e", pathlib.Path(__file__).parent / "e2e" / "verify_e2e.py")
v = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(v)

SUP = [{"p": "/addons/self/options/config", "authed": True, "known": True},
       {"p": "/services/mqtt", "authed": True, "known": True}]


def e(t, p="/status", **kw):
    return {"abs": 1000.0 + t, "t": t, "m": "GET", "p": p, "limited": kw.pop("limited", False), "body": "", **kw}


def diag(rl="recovered", flag=True, stats=None, poll="ok", fails=0):
    return ("P/STUBSN1/diagnostics", json.dumps({"rate_limit": {"state": rl}, "poll_status": poll, "consecutive_failures": fails,
                                                  "demand_aware_polling": flag, "demand_refresh": stats or {}}))


EVENTS = {"start": 1000, "reuse_precycle": 1230, "forced_1": 1380, "overlap_precycle_then_1": 1440,
          "overlap_1_then_precycle": 1510, "slow_start": 1511, "slow_forced": 1541, "slow_end": 1616,
          "recovered_check": 1706, "quiet_start": 1707, "quiet_end": 2757, "quota_start": 2758, "quota_limited": 2760,
          "quota_pub2": 2780, "limit_off": 2810, "end": 3010}


SLOW_END_T = 615
RECOVERY_T = 790      # slow_end + 175 s: a legitimate 180 s-cadence recovery (observed in Docker: slow_end + 140 s)


def good(option_on=True):
    api = [e(0, "/login"), e(0, "/user-info"), e(0), e(190)]
    if not option_on:
        api.append(e(231))                        # S0 polls for the marked refresh as well
    api += [e(381), e(441), e(442.5)]             # forced '1'; overlap A (1 poll + coalesced); 
    api += [e(512), e(513.5)]                     # overlap B
    api += [e(545 + 20 * i, slow=True, abs=1000 + 545 + 20 * i - 16) for i in range(3)]
    # recovery at the bridge's 180 s fallback cadence: last failed attempt ended ~t=605 (slow_end=615), next poll ~605+180
    api += [e(RECOVERY_T)]
    api += [e(RECOVERY_T + 180 * i) for i in range(1, 6)]   # quiet period at the legacy 180 s cadence
    api += [e(1790, limited=True)]                # quota response, then silence for the 120 s backoff
    api += [e(1912), e(2092)]
    for x in api:
        x["abs"] = x.get("abs", 1000.0 + x["t"])
    for x in api:
        if x["p"] == "/status" and not x.get("slow") and 1000 + 1790 - 0.5 <= x["abs"] < 1000 + 1790 + 0.5:
            pass
    ev = dict(EVENTS)
    # absolute times for the quota block: place it where the synthetic limited response is
    ev.update({"quiet_start": 1000 + 705, "quiet_end": 1000 + 1780, "quota_start": 1000 + 1781, "quota_limited": 1000 + 1791,
               "quota_pub2": 1000 + 1811, "limit_off": 1000 + 1841, "end": 1000 + 2100,
               "slow_start": 1000 + 520, "slow_forced": 1000 + 540, "slow_end": 1000 + SLOW_END_T,
               "recovered_check": 1000 + SLOW_END_T + 210,   # harness: slow_end + 90 + 120
               "reuse_precycle": 1000 + 230, "forced_1": 1000 + 380, "overlap_precycle_then_1": 1000 + 440,
               "overlap_1_then_precycle": 1000 + 511})
    stats = {"marked": 4, "forced": 4}
    mqtt = [("P/STUBSN1/STATUS", "{}"), diag("none", option_on), diag("recovered", option_on, stats if option_on else {})]
    return api, ev, "Starting Thermowatt Bridge for u\n", mqtt, copy.deepcopy(SUP), {"exit_code": 0}


def run(api, ev, log, mqtt, sup, meta, option_on=True):
    return v.check_scenario_s(api, ev, log, mqtt, sup, meta, option_on=option_on)


def test_correct_runs_pass():
    for on in (True, False):
        assert run(*good(on), option_on=on) == [], on


def test_reuse_violation_and_missing_legacy_poll_are_caught():
    api, *rest = good(True)
    api.append(e(231))                      # S polled for a marked refresh that should have been reused
    api.sort(key=lambda x: x["abs"])
    assert any("expected reuse" in m for m in run(api, *rest))
    api, *rest = good(False)
    api = [x for x in api if x["t"] != 231]
    assert any("legacy" in m for m in run(api, *rest, option_on=False))


def test_forced_refresh_without_poll_is_caught():
    api, *rest = good(True)
    api = [x for x in api if x["t"] != 381]
    assert any("plain '1'" in m for m in run(api, *rest))


def test_overlap_lost_or_uncoalesced_is_caught():
    api, *rest = good(True)
    assert any("forced intent lost" in m for m in run([x for x in api if x["t"] not in (441, 442.5)], *rest))
    api2 = api + [e(443), e(444)]
    assert any("coalescing" in m for m in run(sorted(api2, key=lambda x: x["abs"]), *rest))


def test_request_storm_during_timeouts_and_missing_recovery_are_caught():
    api, *rest = good(True)
    storm = api + [e(545 + i, slow=True, abs=1000 + 545 + i - 16) for i in range(40)]
    assert any("storm" in m for m in run(sorted(storm, key=lambda x: x["abs"]), *rest))
    assert any("no successful /status" in m for m in run([x for x in api if x["t"] != RECOVERY_T], *rest))


def _with_recovery_at(api, t_new):
    """Move the single recovery poll (and drop the cadence that followed it) to slow_end + t_new - SLOW_END_T."""
    out = [x for x in api if x["t"] != RECOVERY_T]
    out.append(e(t_new))
    return sorted(out, key=lambda x: x["abs"])


def test_recovery_deadline_matches_the_documented_arithmetic():
    assert v.RECOVERY_DEADLINE_S == 180 + 5 + 15 + v.TOL == 206.0


def test_legitimate_180s_recovery_is_accepted_up_to_the_deadline():
    for on in (True, False):
        for dt in (20, 140, 175, 205):            # observed Docker recovery was 140 s after slow_end
            api, *rest = good(on)
            msgs = run(_with_recovery_at(api, SLOW_END_T + dt), *rest, option_on=on)
            assert not any("no successful /status" in m for m in msgs), (on, dt, msgs)


def test_missing_or_excessively_delayed_recovery_is_rejected():
    for on in (True, False):
        api, *rest = good(on)
        assert any("no successful /status" in m for m in run([x for x in api if x["t"] != RECOVERY_T], *rest, option_on=on))
        late = run(_with_recovery_at(api, SLOW_END_T + 207), *rest, option_on=on)    # just past 206 s
        assert any("no successful /status" in m for m in late), on
        far = run(_with_recovery_at(api, SLOW_END_T + 400), *rest, option_on=on)
        assert any("no successful /status" in m for m in far), on


def test_a_slow_response_does_not_count_as_recovery():
    api, *rest = good(True)
    only_slow = _with_recovery_at(api, SLOW_END_T + 60)
    only_slow[[x["t"] for x in only_slow].index(SLOW_END_T + 60)]["slow"] = True
    assert any("no successful /status" in m for m in run(only_slow, *rest))


def test_harness_cannot_silently_shorten_the_observation_window():
    api, ev, *rest = good(True)
    ev["recovered_check"] = ev["slow_end"] + 90      # the old, too-short harness deadline
    assert any("shorter than the 206 s deadline" in m for m in run(api, ev, *rest))


def test_gap_over_the_offline_threshold_and_missing_legacy_cadence_are_caught():
    api, *rest = good(True)
    api = [x for x in api if x["t"] not in (RECOVERY_T + 180, RECOVERY_T + 360)]            # a 540 s hole
    assert any("offline threshold" in m for m in run(api, *rest))


def test_requests_during_quota_backoff_are_caught():
    api, *rest = good(True)
    api.append(e(1830))
    assert any("during quota backoff" in m for m in run(sorted(api, key=lambda x: x["abs"]), *rest))


def test_missing_recovery_and_wrong_option_reporting_are_caught():
    api, ev, log, mqtt, sup, meta = good(True)
    bad = [m for m in mqtt if not m[0].endswith("diagnostics")] + [diag("recovery_unverified", True)]
    assert any("expected 'recovered'" in m for m in run(api, ev, log, bad, sup, meta))
    assert any("demand_aware_polling" in m for m in run(*good(False), option_on=True))
    api, ev, log, mqtt, sup, meta = good(True)
    mqtt[-1] = diag("recovered", True, {"marked": 1, "forced": 4})
    assert any("counters" in m for m in run(api, ev, log, mqtt, sup, meta))


def test_incomplete_timeline_and_unexpected_paths_are_reported():
    api, ev, log, mqtt, sup, meta = good(True)
    ev.pop("slow_end")
    assert any("timeline incomplete" in m for m in run(api, ev, log, mqtt, sup, meta))
    api, ev, log, mqtt, sup, meta = good(True)
    api.append(e(5, "/manual"))
    assert run(api, ev, log, mqtt, sup, meta)
