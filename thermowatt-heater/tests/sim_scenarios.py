"""Scenario definitions + reporting for the Stage 3 virtual-time simulation (issue #17).

    python -m tests.sim_scenarios            # prints the comparison tables (needs the test deps)

Modes: L = legacy (poll_interval 180, reactive tokens), D = demand-aware polling,
DP = demand-aware + proactive token refresh, LP = legacy + proactive token refresh.
"""
import math
import pathlib
import re
import statistics as st
import sys
import tempfile

sys.argv = sys.argv[:1]
from tests.sim_harness import DAY, SN, T0, World  # noqa: E402

MODES = {
    "L": dict(demand_aware=False, proactive=False),
    "LP": dict(demand_aware=False, proactive=True),
    "D": dict(demand_aware=True, proactive=False),
    "DP": dict(demand_aware=True, proactive=True),
}


def hms(day, h, m=0, s=0):
    return T0 + day * DAY + h * 3600 + m * 60 + s


def _setup_composite(w):
    """48 h with every fault class requested in the task, identical for every mode."""
    c = w.cloud
    rng = w.rng
    # day 0
    w.at(hms(0, 2, 0), lambda: setattr(c, "fail_next_refresh", 1))                 # one failed token refresh
    c.p_fail_windows.append((hms(0, 5), hms(0, 8), 0.128))                          # repeated transport failure
    c.outages.append((hms(0, 9, 30), hms(0, 9, 42)))                                # 12 min hard outage
    w.ha_down.append((hms(0, 11), hms(0, 11, 6)))                                   # HA restart
    # native heater pre-emption (thermostat/app changes mode on its own), 8 per day
    for d in (0, 1):
        for k in range(8):
            t = hms(d, 1 + 2.7 * k, rng.randint(0, 59), rng.randint(0, 59))
            w.native_change(t, 3)
            w.native_change(t + 1500, 9)
    # ownership ON: six boost windows/day = TEMP 50 then TEMP 58 (baseline restore) ; EMHASS cycle +5 s
    for d in (0, 1):
        for h in (7, 10, 13, 15, 17, 19):
            w.command(hms(d, h, 5, 5), "TEMP", "50")
            w.command(hms(d, h, 25, 5), "TEMP", "58")
    # pending handback: restore baseline (MODE Manual + TEMP 58) and HA restarts 20 s later
    w.command(hms(0, 20, 0, 10), "MODE", "Manual")
    w.command(hms(0, 20, 0, 12), "TEMP", "58")
    w.ha_down.append((hms(0, 20, 0, 20), hms(0, 20, 6)))
    # day 1
    c.quota_windows.append((hms(1, 12), hms(1, 19, 30)))                            # quota rejection window
    w.ha_automation_off.append((hms(1, 20, 30), hms(1, 23, 50)))                    # refresh demand missing
    return [(hms(0, 9, 30), hms(0, 9, 42) + 330), (hms(1, 12), hms(1, 19, 30) + 3700),
            (hms(0, 5), hms(0, 8) + 400), (hms(0, 2), hms(0, 2, 20)),
            (hms(0, 11), hms(0, 11, 12)), (hms(0, 20), hms(0, 20, 12)), (hms(1, 20, 30), hms(1, 23, 50) + 400)]


# Real ownership-ON intervals from the recorder (UTC, minutes after midnight, duration in minutes),
# 8, 9 and 10 Nov 2026 (EMHASS + Package 19 live; 3, 5 and 11 boosts; 10, 15 and 26 bridge commands).
PROFILES = {
    "day8": [(5 * 60, 5), (5 * 60 + 45, 35), (6 * 60 + 25, 5)],
    "day9": [(2 * 60 + 5, 15), (2 * 60 + 30, 5), (2 * 60 + 40, 5), (2 * 60 + 50, 10), (3 * 60 + 25, 15)],
    "day10": [(1 * 60 + 25, 5), (1 * 60 + 35, 60), (2 * 60 + 40, 5), (2 * 60 + 50, 5), (3 * 60, 5), (3 * 60 + 10, 5),
              (3 * 60 + 25, 5), (3 * 60 + 45, 5), (4 * 60, 5), (4 * 60 + 20, 5), (4 * 60 + 40, 5), (5 * 60 + 5, 5)],
}


def run(name, mode, seed=1, recovery_retry=None, fail_profile=None, phase_gate=True, boost_rule=True, **world_kw):
    w = World(seed=seed, **MODES[mode], **world_kw)
    tmp = pathlib.Path(tempfile.mkdtemp())
    w.build(tmp)
    if not phase_gate:
        w.b.DEMAND_REUSE_PHASE_S = None
    if not boost_rule:
        w.b.DEMAND_BOOST_SETPOINT_C = 1e9
    if recovery_retry is not None:
        w.b.DEMAND_RECOVERY_RETRY_S = recovery_retry
    if fail_profile is not None:
        w.cloud.fail_profile = fail_profile
    excl = []
    if name == "composite":
        excl = _setup_composite(w)
    elif name == "cap468":
        w.cloud.cap = 468
    elif name == "preempt":                   # native thermostat/app mode change every hour, held 15 min
        for d in (0, 1):
            for k in range(24):
                t = hms(d, k, w.rng.randint(5, 35), w.rng.randint(0, 59))
                w.native_change(t, 3)
                w.native_change(t + 900, 9)
    elif name.startswith("cmds"):
        n = int(name[4:])                     # boost windows per day, 2 commands each
        for d in (0, 1):
            for i in range(n):
                h = 6 + (14 * i) / max(n, 1)
                w.command(hms(d, int(h), int((h % 1) * 60) // 5 * 5, 5), "TEMP", "50")
                w.command(hms(d, int(h), int((h % 1) * 60) // 5 * 5 + 20, 5), "TEMP", "58")
    elif name.startswith("p19"):               # Package 19 model: p19idle, p19day8, p19day9, p19day10
        w.p19 = True
        prof = PROFILES.get(name[3:], [])
        for d in (0, 1):
            for start_min, dur in prof:
                for i in range(dur // 5):
                    w.wants.add(d * 288 + start_min // 5 + i)
    elif name == "nodemand":                  # Package 19 pre-cycle automation disabled 6 h
        w.ha_automation_off.append((hms(0, 8), hms(0, 14)))
        excl = [(hms(0, 8), hms(0, 14, 5))]
    elif name in ("p128", "p015"):            # all-day repeated transport failure, per request
        w.cloud.p_fail = 0.128 if name == "p128" else 0.015
    elif name.startswith("jitter"):           # sensitivity: HA/MQTT latency jitter sd in seconds (jitterN)
        pass
    elif name == "failed_refresh":
        w.at(hms(0, 2, 0), lambda: setattr(w.cloud, "fail_next_refresh", 1))
        excl = [(hms(0, 2), hms(0, 2, 20))]
    try:
        w.run()
    finally:
        w.close()          # never leak the patched clock into other tests
    log = w._out.getvalue()
    res = {
        "name": name, "mode": mode, "per_day": w.per_day(), "stale": w.stale_cycles(excl),
        "stale_all": w.stale_cycles(), "flaps": w.offline_flaps(excl), "flaps_all": w.offline_flaps(), "offline_s": w.offline_seconds(),
        "max_status_gap": max(w.success_gaps()) if w.success_gaps() else None,
        "gaps_over_300": sum(1 for g in w.success_gaps() if g > 300.0),
        "reuse": dict(w.b._demand_stats), "refresh_publishes": w.refresh_publishes,
        "confirmed": len(re.findall(r"confirmed by fresh readback", log)),
        "commands_sent": len(w.commands),
        "cooldown_refused": len(re.findall(r"Cooldown active", log)),
        "cmd_failed": len(re.findall(r"command (?:NOT sent|failed|transport failure)", log)),
        "401_statuses": w.cloud.statuses_401, "replays": w.cloud.replays,
        "latencies": [x for x in w.preemption_latencies() if x is not None],
        "preempt_unseen": sum(1 for x in w.preemption_latencies() if x is None),
        "rejected": dict(w.cloud.rejected), "cycles": len(w.cycles), "ages": w.cycle_ages(excl),
        "excluded_windows": excl, "world": w, "gate": list(w.gate_log), "handbacks": list(w.handbacks),
        "acquisitions": w.acquisitions, "ownership_end": w.ownership,
    }
    w.close()
    return res


def deadline_fault(mode, kind, severity=0.0, seed=3, recovery=None, which="deadline"):
    """One fault on one /status request: the deadline poll (last success >= 285 s old) or, for the
    legacy build, the /status at the same point of its cycle. kind: slow | reset | connect | read.
    Returns the gap between the successful polls around it, offline publications / seconds, stale cycles."""
    import tempfile
    from tests.sim_harness import SN
    w = World(seed=seed, days=1, **MODES[mode])
    w.build(pathlib.Path(tempfile.mkdtemp()))
    if recovery is not None:
        w.b.DEMAND_RECOVERY_RETRY_S = recovery
    state = {}
    t_arm = T0 + 3 * 3600

    def hook(now, endpoint):
        if endpoint != "/status" or now < t_arm or "t" in state:
            return None
        ts = w.b._last_successful_poll.get(SN)
        age = (now - ts) if ts else 0
        if age < (285 if which == "deadline" else 170):
            return None
        state["t"] = now
        return ("slow", severity) if kind == "slow" else (kind,)
    w.cloud.fault_hook = hook
    try:
        w.run()
    finally:
        w.close()          # never leak the patched clock into other tests
    t = state.get("t")
    if t is None:
        return None
    pubs = [x for x, topic, _ in w.publishes if topic == f"P/{SN}/STATUS"]
    before = [x for x in pubs if x < t - 1]
    after = [x for x in pubs if x >= t - 1]
    off, since = 0.0, None
    for ta, sa in w.avail:
        if ta < t - 5:
            continue
        if ta > t + 900:
            break
        if sa == "offline" and since is None:
            since = ta
        if sa == "online" and since is not None:
            off += ta - since
            since = None
    return {"gap": after[0] - before[-1], "offline_publications": sum(1 for ta, sa in w.avail
            if sa == "offline" and t - 5 <= ta <= t + 900), "offline_s": off,
            "stale_cycles": sum(1 for c in w.cycles if not c["fresh"] and t - 5 <= c["t"] <= t + 900)}


def detection_delays(mode, n=20000, seed=5):
    """Seconds from a physical change (thermostat self-termination, native mode change) to the first
    EMHASS read (T+3 s) that can see it, for events uniform over ~11 h of a healthy idle run."""
    import bisect
    r = run("normal", mode)
    w = r["world"]
    ts = sorted(t for t, topic, _ in w.publishes if topic == "P/SN/STATUS")
    rng = __import__("random").Random(seed)
    out = []
    for _ in range(n):
        ev = T0 + 3600 + rng.random() * 40000
        k = math.ceil((ev - T0) / 300)
        while True:
            T = T0 + 300 * k
            i = bisect.bisect_right(ts, T + 3.0) - 1
            if ts[i] > ev:
                out.append(T - ev)
                break
            k += 1
    return sorted(out)


def native_during_boost(mode, ha_down=None):
    """Owned boost for 6 cycles; the app switches the heater to Eco 500 s after acquisition."""
    import tempfile
    w = World(seed=2, days=1, **MODES[mode])
    w.p19 = True
    w.build(pathlib.Path(tempfile.mkdtemp()))
    for i in range(6):
        w.wants.add(100 + i)
    w.native_change(T0 + 300 * 100 + 510, 3)
    if ha_down:
        w.ha_down.append(ha_down)
    try:
        w.run()
    finally:
        w.close()          # never leak the patched clock into other tests
    return {"outcomes": [h["outcome"] for h in w.handbacks], "owned": w.ownership,
            "commands_after_native": sum(1 for t, _, _ in w.commands if t > T0 + 300 * 100 + 510)}


def day_total(res, d):
    return res["per_day"].get(d, {}).get("total", 0)


def _p(vals, q):
    vals = sorted(vals)
    return vals[min(len(vals) - 1, int(q * len(vals)))] if vals else None


def report():  # pragma: no cover - reporting
    out = []
    out.append("## Healthy idle, 48 h (requests/UTC day: status + auth = total; per-cycle polls = (status - replays)/288)")
    for m in ("L", "LP", "D", "DP"):
        r = run("normal", m)
        d = r["per_day"]
        out.append(f"{m:3} day0 {d[0]['status']}+{d[0]['auth']}={d[0]['total']}  day1 {d[1]['status']}+{d[1]['auth']}={d[1]['total']}  "
                   f"stale={r['stale']} flaps={r['flaps']} maxgap={r['max_status_gap']:.1f}s >300s={r['gaps_over_300']} "
                   f"reuse={r['reuse']} 401s={r['401_statuses']} replays={r['replays']}")
    out.append("\n## Composite 48 h (all faults)")
    for m in ("L", "D", "DP"):
        r = run("composite", m)
        d = r["per_day"]
        out.append(f"{m:3} day0 {d[0]['total']} (rej {d[0]['rejected']})  day1 {d[1]['total']} (rej {d[1]['rejected']})  "
                   f"stale(outside faults)={r['stale']} flaps(outside)={r['flaps']} flaps(all)={r['flaps_all']} "
                   f"cmds={r['commands_sent']} confirmed={r['confirmed']} failed={r['cmd_failed']} "
                   f"preempt lat mean={st.mean(r['latencies']):.0f}s p95={_p(r['latencies'], .95):.0f}s max={max(r['latencies']):.0f}s "
                   f"unseen={r['preempt_unseen']}")
    return "\n".join(out)


if __name__ == "__main__":  # pragma: no cover
    print(report())
