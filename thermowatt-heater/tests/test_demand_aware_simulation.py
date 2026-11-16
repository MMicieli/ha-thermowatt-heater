"""Stage 3 (issue #17): 48 h virtual-time simulation of the REAL bridge scheduler against a fake
cloud and a model of Package 19's pre-cycle refresh. Budgets here are planning targets measured in the
model, not vendor quotas. See tests/sim_harness.py for the (stated) fidelity limits."""
import functools
import math
import statistics as st

import pytest

from tests.sim_harness import DAY, T0
from tests.sim_scenarios import deadline_fault, detection_delays, hms, native_during_boost, run


@functools.lru_cache(maxsize=None)
def R(name, mode, **kw):
    r = run(name, mode, **dict(kw))
    r.pop("world", None)
    return r


def lognorm(med, sigma):
    return lambda rng: rng.lognormvariate(math.log(med), sigma)


def day(r, d, key="total"):
    return r["per_day"][d][key]


# ---- calibration against the measured system --------------------------------

def test_model_reproduces_the_measured_legacy_budget():
    r = R("normal", "L")
    assert 668 <= day(r, 0) <= 676 and 668 <= day(r, 1) <= 676        # measured ~672/day (28/h)
    assert r["stale"] == 0 and r["flaps"] == 0
    assert 26 <= statistics_age(r) <= 30                              # measured median age at cycle 28 s


def statistics_age(r):
    return st.mean(r["ages"])


# ---- request budget ------------------------------------------------------------

def test_demand_aware_meets_the_450_planning_budget_and_dp_meets_400():
    d = R("normal", "D")
    dp = R("normal", "DP")
    for r in (d, dp):
        assert r["stale"] == 0 and r["flaps"] == 0 and r["gaps_over_300"] == 0
        assert r["max_status_gap"] <= 292.0                       # < the fixed 300 s bound
        assert max(r["ages"]) <= 90                               # <= 90 s at cycle start, gate is 120 s
    assert day(d, 0) <= 425 and day(d, 1) <= 425
    assert day(dp, 0) <= 392 and day(dp, 1) <= 392
    assert day(R("normal", "L"), 0) - day(d, 0) >= 240            # >= 37 % reduction


def test_poll_count_matches_the_sawtooth_arithmetic():
    d = R("normal", "D")
    polls = day(d, 0, "status") - R("normal", "D")["replays"] / 2
    assert 320 <= polls <= 332                                    # 288 * (1 + 1/8) = 324


def test_proactive_net_saving_from_the_real_401_refresh_replay_path():
    legacy = day(R("normal", "L"), 0) - day(R("normal", "LP"), 0)
    aware = day(R("normal", "D"), 0) - day(R("normal", "DP"), 0)
    assert 38 <= legacy <= 46          # ~ 96 reactive - ~53 proactive, NOT 68 (and not 2 per token)
    assert 30 <= aware <= 42           # wider margin (350 s) renews earlier: ~58 refreshes/day
    assert R("normal", "LP")["replays"] == 0 and R("normal", "DP")["replays"] == 0


def test_a_hypothetical_469_threshold_is_reached_by_legacy_but_not_by_demand_aware():
    assert R("cap468", "L")["rejected"], "legacy must hit a 468/day cap"
    for m in ("D", "DP"):
        r = R("cap468", m)
        assert not r["rejected"] and r["stale"] == 0
        assert max(day(r, 0), day(r, 1)) <= 425


# ---- one poll per cycle: why a margin is needed ----------------------------------

MIX = [("reset", 0.3, 0.80), ("connect", 5.0, 0.12), ("read", 15.3, 0.08)]    # transport-failure kinds


def mean_over_seeds(name, mode, seeds, **kw):
    runs = [R(name, mode, seed=sd, post_delay=PD, fail_profile=tuple(MIX), **kw) for sd in seeds]
    return (st.mean(r["flaps"] for r in runs), st.mean(r["offline_s"] for r in runs),
            st.mean(day(r, 0) for r in runs))


PD = lognorm(0.015, 1.0)       # post-stamp work: median 15 ms, p95 ~80 ms (unmeasured in production)


def test_one_request_per_cycle_has_a_50ms_margin():
    naive = R("normal", "L", poll_interval=300)                   # poll_interval=300: ~1 request / cycle
    assert 380 <= day(naive, 0) <= 388 and naive["flaps"] == 0   # healthy + zero post-stamp work: no flap...
    assert R("normal", "L", poll_interval=300, post_delay=lambda rng: 0.06)["flaps"] >= 10   # ...60 ms breaks it
    assert R("normal", "D", post_delay=lambda rng: 0.06)["flaps"] == 0


def test_failure_tolerance_is_better_than_one_request_per_cycle_but_WORSE_than_today():
    """Honest comparison over 8 seeds with a realistic failure mix (80 % resets, 12 % connect and 8 %
    read timeouts). Removing the second poll per cycle costs availability under transport failures."""
    seeds = range(1, 9)
    for name, factor in (("p015", 1), ("p128", 1)):
        l_f, l_o, _ = mean_over_seeds(name, "L", seeds)
        n_f, n_o, _ = mean_over_seeds(name, "L", seeds, poll_interval=300)
        d_f, d_o, _ = mean_over_seeds(name, "D", seeds)
        assert d_o < n_o and d_f < n_f                            # the margin helps against naive 1/cycle
        assert d_o > l_o and d_f >= l_f                           # ...but today's 180 s cadence is more tolerant
    d_f, d_o, d_req = mean_over_seeds("p015", "D", seeds)
    assert d_f <= 4 and d_o <= 120                                 # at 1.5 %: a few brief offline blips per 48 h


# ---- HTTP_TIMEOUT=(5, 15) versus the 10 s margin ---------------------------------------------

@pytest.mark.parametrize("seconds", [2, 6, 10, 14, 19])
def test_slow_successful_deadline_polls_do_not_publish_offline_even_past_300s(seconds):
    r = deadline_fault("D", "slow", seconds)
    assert r["offline_publications"] == 0 and r["stale_cycles"] == 0
    assert r["gap"] == pytest.approx(290 + seconds + 0.7, abs=3)  # the success stamp can land past 300 s


def test_a_reset_is_absorbed_by_the_five_second_retry():
    r = deadline_fault("D", "reset")
    assert r["offline_publications"] == 0 and r["gap"] <= 297.5


@pytest.mark.parametrize("kind", ["connect", "read"])
def test_timeouts_are_not_tolerated_they_fail_closed_briefly(kind):
    r = deadline_fault("D", kind)
    assert r["offline_publications"] == 1                          # offline is published: fail closed
    assert 5 <= r["offline_s"] <= 40 and r["gap"] <= 335          # ...and recovered by the next attempt
    assert r["stale_cycles"] == 0
    legacy = deadline_fault("L", kind, which="legacy")
    assert legacy["offline_publications"] == 0                    # today's 180 s cadence rides it out


def test_recovery_retry_shortens_the_offline_time_after_a_timeout():
    with_retry = deadline_fault("D", "connect", recovery=20)["offline_s"]
    without = deadline_fault("D", "connect", recovery=0)["offline_s"]
    assert with_retry <= without


# ---- Package 19: reuse threshold, phase gate, template eligibility, handback ----------------

def test_gate_timing_at_the_snapshot_and_translation_event():
    r = R("p19idle", "D")
    ages = [g["true_age"] for g in r["gate"] if g["true_age"] is not None]
    assert max(r["ages"]) <= 90                                    # at the :00 render / EMHASS snapshot
    assert max(ages) <= 100 and not any(a > 120 for a in ages)     # at the translation event (T+8..11 s)
    assert all(g["sensor"] for g in r["gate"])                     # eligibility never lost to staleness
    assert R("p19idle", "L")["gate"] and all(g["sensor"] for g in R("p19idle", "L")["gate"])


def test_a_larger_reuse_threshold_would_breach_the_120s_gate_at_translation():
    r = R("p19idle", "D", reuse_age=110)
    assert any(g["true_age"] > 120 for g in r["gate"])             # why 80 s and not 110 s


def test_without_the_phase_gate_the_handback_refresh_wait_starves():
    """PR #25 head: reuse for any arrival. The handback script needs a poll stamped >= request - 10 s."""
    broken = R("p19day10", "D", phase_gate=False, boost_rule=False)
    outcomes = [h["outcome"] for h in broken["handbacks"]]
    assert outcomes.count("halt_refresh_wait") >= 10
    fixed = R("p19day10", "D")
    legacy = R("p19day10", "L")
    assert [h["outcome"] for h in fixed["handbacks"]] == [h["outcome"] for h in legacy["handbacks"]] == ["ok"] * 18
    assert fixed["acquisitions"] == legacy["acquisitions"] == 18
    assert fixed["confirmed"] == legacy["confirmed"] == 54 and not fixed["ownership_end"]


def test_handback_refresh_always_gets_a_real_poll_at_every_phase():
    for mode in ("D", "DP"):
        r = R("p19day10", mode)
        assert all(h["refresh_polled"] for h in r["handbacks"])


def test_native_mode_change_while_owned_releases_without_a_command_in_every_mode():
    for mode in ("L", "D"):
        r = native_during_boost(mode)
        assert r["outcomes"] == ["released_native_mode"] and not r["owned"]
        assert r["commands_after_native"] == 0


def test_ha_restart_during_a_handback_is_completed_after_start():
    for mode in ("L", "D"):
        r = native_during_boost_restart(mode)
        assert not r["owned"] and r["outcomes"][-1] in ("ok", "released_native_mode")


def native_during_boost_restart(mode):
    from tests.sim_scenarios import World, MODES, T0 as _T0
    import pathlib
    import tempfile
    w = World(seed=2, days=1, **MODES[mode])
    w.p19 = True
    w.build(pathlib.Path(tempfile.mkdtemp()))
    for i in range(3):
        w.wants.add(100 + i)
    w.ha_down.append((_T0 + 300 * 103 + 2, _T0 + 300 * 103 + 360))      # HA down across the handback
    try:
        w.run()
    finally:
        w.close()
    return {"outcomes": [h["outcome"] for h in w.handbacks], "owned": w.ownership}


def test_pre_emption_detection_costs_about_22_seconds_on_average():
    legacy, aware = detection_delays("L"), detection_delays("D")
    mean_l, mean_d = st.mean(legacy), st.mean(aware)
    assert 15 <= mean_d - mean_l <= 30
    assert max(aware) <= 390
    assert sum(x <= 300 for x in aware) / len(aware) >= 0.82       # seen by the next cycle: 83.5 % vs 90.7 %


def test_owned_boost_gets_fresh_telemetry_at_every_cycle():
    r = R("p19day10", "D")
    owned = [g["true_age"] for g in r["gate"] if g["ownership"] and g["true_age"] is not None]
    assert owned and max(owned) <= 60                              # 95 s without the boost-setpoint rule


# ---- realistic HWS boost/handback request budgets (real 8/9/10 Nov profiles) --------------------

@pytest.mark.parametrize("profile,boosts,d_limit,dp_limit", [
    ("p19day8", 3, 450, 415), ("p19day9", 5, 490, 455), ("p19day10", 9, 545, 510)])
def test_budget_on_real_boost_days(profile, boosts, d_limit, dp_limit):
    legacy, d, dp = R(profile, "L"), R(profile, "D"), R(profile, "DP")
    assert day(d, 0) <= d_limit and day(dp, 0) <= dp_limit
    assert day(legacy, 0) - day(d, 0) >= 200                        # still >= 200 requests/day lower than today
    per_command = (day(d, 0) - 419) / (d["commands_sent"] / 2)       # 2 simulated days
    assert 3.8 <= per_command <= 5.6                                # ~4.5 requests per command incl. confirmation polls
    assert all(h["outcome"] == "ok" for h in d["handbacks"]) and not d["ownership_end"]


def test_boost_heavy_days_exceed_the_450_planning_budget():
    """Do not read 418/day as a budget for ownership-on operation."""
    assert day(R("p19day10", "DP"), 0) > 450
    assert day(R("p19day9", "D"), 0) > 450


def test_missing_demand_reverts_to_the_legacy_cadence_and_keeps_the_300s_bound():
    r = R("nodemand", "D")
    assert r["flaps"] == 0 and r["stale"] == 0
    assert r["max_status_gap"] <= 292.0
    assert r["gaps_over_300"] == 0


def test_native_preemption_is_seen_within_one_deadline_gap():
    legacy = R("preempt", "L")["latencies"]
    aware = R("preempt", "D")["latencies"]
    assert max(legacy) <= 185
    assert max(aware) <= 295 and st.mean(aware) <= 160           # trade-off: ~70 s later on average
    assert R("preempt", "D")["preempt_unseen"] == 0


def test_failed_token_refresh_recovers_inside_the_bound():
    for m in ("D", "DP"):
        r = R("failed_refresh", m)
        assert r["stale"] == 0 and r["flaps"] == 0
        assert r["max_status_gap"] <= 300.05


@pytest.mark.parametrize("sd", [1.0, 6.0])
def test_insensitive_to_demand_jitter(sd):
    r = R("normal", "D", jitter_sd=sd)
    assert r["stale"] == 0 and r["flaps"] == 0 and max(r["ages"]) <= 100


# ---- reserve for heater commands ----------------------------------------------------

@pytest.mark.parametrize("windows,mode,limit", [
    (3, "D", 450), (6, "DP", 450), (10, "DP", 480), (0, "DP", 400),
])
def test_command_reserve(windows, mode, limit):
    r = R(f"cmds{windows}", mode)
    assert day(r, 0) <= limit and day(r, 1) <= limit
    assert r["confirmed"] == r["commands_sent"] == 4 * windows    # every command confirmed by readback
    assert r["stale"] == 0 and r["flaps"] == 0


def test_cost_per_command_is_about_five_requests():
    base = day(R("cmds0", "D"), 0)
    per = [(day(R(f"cmds{n}", "D"), 0) - base) / (2 * n) for n in (3, 6, 10)]
    assert all(4.0 <= p <= 5.5 for p in per)
