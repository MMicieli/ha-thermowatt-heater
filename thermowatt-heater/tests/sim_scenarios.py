"""Scenario definitions + reporting for the Stage 3 virtual-time simulation (issue #17).

    python -m tests.sim_scenarios            # prints the comparison tables (needs the test deps)

Modes: L = legacy (poll_interval 180, reactive tokens), D = demand-aware polling,
DP = demand-aware + proactive token refresh, LP = legacy + proactive token refresh.
"""
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


def run(name, mode, seed=1, **world_kw):
    w = World(seed=seed, **MODES[mode], **world_kw)
    tmp = pathlib.Path(tempfile.mkdtemp())
    w.build(tmp)
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
    w.run()
    log = w._out.getvalue()
    res = {
        "name": name, "mode": mode, "per_day": w.per_day(), "stale": w.stale_cycles(excl),
        "stale_all": w.stale_cycles(), "flaps": w.offline_flaps(excl), "flaps_all": w.offline_flaps(),
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
        "excluded_windows": excl, "world": w,
    }
    w.close()
    return res


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
