"""Stage 3 (issue #17): 48 h virtual-time simulation of the REAL bridge scheduler against a fake
cloud and a model of Package 19's pre-cycle refresh. Budgets here are planning targets measured in the
model, not vendor quotas. See tests/sim_harness.py for the (stated) fidelity limits."""
import functools
import math
import statistics as st

import pytest

from tests.sim_harness import DAY, T0
from tests.sim_scenarios import hms, run


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

def test_one_request_per_cycle_has_a_50ms_margin_and_no_failure_tolerance():
    naive = R("normal", "L", poll_interval=300)                   # poll_interval=300: ~1 request / cycle
    assert 380 <= day(naive, 0) <= 388 and naive["flaps"] == 0   # healthy + zero post-stamp work: no flap...
    slow = R("normal", "L", poll_interval=300, post_delay=lambda rng: 0.06)
    assert slow["flaps"] >= 10                                    # ...but 60 ms of post-stamp work breaks it
    d_slow = R("normal", "D", post_delay=lambda rng: 0.06)
    assert d_slow["flaps"] == 0                                   # the 10 s margin is insensitive to it
    pd = lognorm(0.015, 1.0)
    naive_f = R("p015", "L", poll_interval=300, post_delay=pd)
    aware_f = R("p015", "D", post_delay=pd)
    assert naive_f["flaps"] >= 5 and aware_f["flaps"] <= 2        # one failed poll == offline for naive
    naive_h = R("p128", "L", poll_interval=300, post_delay=pd)
    aware_h = R("p128", "D", post_delay=pd)
    legacy_h = R("p128", "L", post_delay=pd)
    assert aware_h["flaps"] * 3 <= naive_h["flaps"]
    assert aware_h["flaps"] <= legacy_h["flaps"] + 6              # no worse than today's 180 s cadence


# ---- the 48 h composite fault run -------------------------------------------------

@pytest.mark.parametrize("mode", ["D", "DP"])
def test_composite_no_regression_against_legacy(mode):
    legacy = R("composite", "L")
    r = R("composite", mode)
    assert r["stale"] <= legacy["stale"] == 0                     # outside the injected fault windows
    assert r["flaps"] <= legacy["flaps"] == 0
    assert r["rejected"] == legacy["rejected"]                    # backoff/probe behaviour identical
    assert r["commands_sent"] == legacy["commands_sent"] == 26
    assert r["confirmed"] == legacy["confirmed"] == 18            # the rest hit the quota window (fail closed)
    assert r["cmd_failed"] == legacy["cmd_failed"] == 8
    assert r["confirmed"] + r["cmd_failed"] == r["commands_sent"]  # incl. the handback pending across an HA restart
    assert day(r, 0) < 0.7 * day(legacy, 0) and day(r, 1) < 0.75 * day(legacy, 1)


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
