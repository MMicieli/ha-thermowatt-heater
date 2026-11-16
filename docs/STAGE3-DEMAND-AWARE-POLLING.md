# Stage 3: demand-aware polling (issue #17)

**Status: implemented behind an option that defaults OFF; source-qualified offline only (unit tests + 48 h virtual-time simulation of the real scheduler). NOT deployed, NOT physically qualified, no add-on version bump.** All request counts below are model results calibrated to the measured system, not vendor quotas.

## 1. Problem
At `poll_interval: 180` with Package 19's pre-cycle refresh the bridge sends ~672 requests per UTC day (28/h) and the vendor has repeatedly answered `Daily request limit exceeded` after 469 recorded requests (435 status + 34 auth) on 14 and 15 November. Requirements that must not change: source telemetry ≤ 120 s old when the five-minute EMHASS run starts (Package 19), device offline after a **fixed 300 s** without a successful poll, command confirmation (20 s for 60 s), handback and fail-closed behaviour.

## 2. Where the traffic goes (installed 1.9.0, measured and reproduced by the simulation)
Per five-minute cycle: (a) Package 19 publishes `P/<sn>/refresh` at `:x4:30` (primary) and `:x4:48` (conditional retry); the bridge serves it ~2.2 s later; (b) the bridge's loop waits a **full** `poll_interval` after *every* cycle, so the timed poll fires 180 s after the demand poll; the next demand arrives 120 s later and restarts the wait. Result: **2 `/status` per cycle = 576/day**, plus token upkeep: access token life 1,800 s ⇒ 48 tokens/day, each costing a refresh **and** a replayed `/status` (the 401 itself is a poll that would have happened anyway) = **+96/day** → 672.

**Redundant under healthy operation:** the timed poll only (≈288/day) — plus the 48 replays that proactive refresh removes. The demand poll is *not* redundant: it is the only request that lands inside the 120 s window (the timed poll is ~147 s before the cycle). The timed poll's sole job is keeping the 300 s offline bound, and a fixed 180 s timer restarted by every request is far stronger than that job needs.

## 3. Design (all behind `demand_aware_polling`, default false)
Applies only when **(i)** the option is on, **(ii)** `poll_interval` > 120 (Package 19 only requests refreshes above 120; otherwise ignored, fail closed to legacy and logged), and **(iii)** refresh requests have been seen in the last 660 s (`DEMAND_ACTIVE_WINDOW_S`). When HA stops asking (restart, automation disabled, older Package 19) the legacy cadence returns within 11 minutes, the 300 s bound never being at risk.

1. **Reuse.** A requested refresh needs no request when the last poll was healthy (validated, no failures, no quota window/unverified recovery) and ≤ `DEMAND_REUSE_MAX_AGE_S` = 80 s old. 80 s + Package 19's 30 s lead + 10 s margin = 120 s. Never applies inside a command-confirmation window, to the first poll after start, or after any failure.
2. **Deadline poll.** After every cycle the loop waits until `last_success + 300 − OFFLINE_SAFETY_MARGIN_S` (10 s) instead of a fixed interval. A failed deadline poll gets one 5 s (`DEMAND_RETRY_DELAY`) retry while it still fits inside the bound; any other failure state keeps the legacy interval. Quota backoff, unverified recovery and the 20 s confirmation cadence are untouched (the wait is only changed at the normal interval).
3. Side effects that follow the cadence: energy-integration cap floors at 300 s; the proactive-refresh margin follows the 290 s wake gap (350 s); diagnostics publish `demand_aware_polling` (effective value) and `demand_refresh {served, reused}`.

Behaviour: the deadline poll lands ahead of the cycle by an amount that grows 10 s per cycle; once the data would be too old for the next requested refresh (≈ 8 cycles) that refresh is served, which resets the phase. Measured: 36 demand-served + 290 deadline polls per day = **1.132 polls/cycle**; age of telemetry at the cycle start is 0–86 s (mean 51, max 86) instead of a constant 28 s.

## 4. Can one poll per cycle hold the strict 300 s threshold?
**Not with a margin.** Let `a_k ∈ [lo, hi]` be the age of the last success at cycle k. One success per cycle gives gaps `g_k = 300 + a_k − a_{k+1}`: the mean gap is exactly 300 s, so every gap ≤ 300 − m requires `a_{k+1} ≥ a_k + m`, which can hold for at most `(hi−lo)/m` consecutive cycles before a reset gap of `300 + (hi−lo)`. With `hi−lo = 80` s (reuse limit) and `m = 10` s (jitter ≈ 0.2 s, round trip ≈ 0.4 s, plus one 5 s retry), the sawtooth is 8 cycles, so the **minimum extra polls are 1 per 9 cycles ≈ 36/day** on top of the 288 mandatory ones (324/day), exactly what the simulation shows.

What the real code does at `poll_interval: 300` (one request per cycle, no code change, ≈ 384/day): availability is evaluated only when the loop is idle (wake at `last_success + 300 + 0.05 s`) and after a failed poll, so a poll that **starts** ≤ 300.05 s after the previous success never flags offline even when completion gaps exceed 300 s (they do in 53 % of cycles, max 301.2 s). The timed poll is due at `last_success + 300 + δ` where δ is the post-stamp work in the cycle (MQTT publishes, ledger/config flush). That is a **50 ms − δ margin**, with **no failure tolerance** (one failed poll = offline until the next attempt). Simulation: 0 flaps with δ = 0, **21 flaps/48 h at δ = 60 ms**, 11 at 1.5 % and 46 at 12.8 % per-request failure versus 1, 9 for demand-aware and 2, 8 for today's 180 s. It may work on a quiet system, but the margin is unmeasured and it fails the "do not weaken the 300 s bound" requirement in spirit; it is offered only as the zero-code alternative.

## 5. Budgets (UTC day; model results, healthy idle unless stated; full tables in `tests/sim_scenarios.py` output)
| Configuration | requests/day | Notes |
|---|---|---|
| Current 1.9.0 | 672 | 2.00 polls/cycle + 96 token overhead (calibrated: measured 672) |
| + `proactive_token_refresh` | 630 | −42 (96 − 53); the 68/day figure is not reproduced |
| `poll_interval: 300` (no code) | 384 | 50 ms margin, no failure tolerance (above) |
| **Demand-aware** | **418** | 1.132 polls/cycle; ≤ 450 planning budget ✔ |
| **Demand-aware + proactive** | **383** | ≤ 400 target ✔ |
Reserve for commands: ≈ 4.5 requests per command (1 POST + confirmation polls, reuse-assisted). Under 450: demand-aware alone leaves room for ~6 commands/day, with proactive ~14/day; under a hypothetical 468 cap ~10 / ~18. A hypothetical first-rejection at 469 is hit by the current build (176 stale cycles in the run) and not by either demand-aware configuration.

## 6. Failure modes simulated (48 h composite, identical faults for every mode)
Failed token refresh (re-login path); 12.8 % repeated transport failure 3 h; 12 min total outage; HA restart; native heater pre-emption 16/day; ownership-on boost commands (24/48 h); a baseline-restore pending across an HA restart; quota window 7.5 h; refresh demand missing 3 h 20 min. Demand-aware matches today on every safety measure: 0 stale cycles and 0 unexplained offline flaps outside the injected windows, identical quota backoff/probe behaviour (12 rejections), 18/26 commands confirmed and the 8 issued during the quota window refused locally (fail closed), handback across the HA restart confirmed. Trade-off: native pre-emption is seen after ≤ 288 s (mean 153 s) instead of ≤ 178 s (mean 85 s); Package 19 only decides at cycle starts and its freshness gate is unchanged.

## 7. Package 19 interaction (no Package 18/19 change)
Package 19's retry at `:48` fires when the last success is older than `:20`; the bridge reuses or serves it under the same rule, so no retry storm and no new HA behaviour. Required: `poll_interval` above 120 s (180 stays). If Package 19's pre-cycle automation is disabled the option reverts to the legacy cadence after 660 s.

## 8. Supervised commissioning (operator-approved; merge ≠ release ≠ deploy)
Pre-conditions: this change released as a separate add-on version (the PR does **not** bump the version; do not combine with PR #24); HWS ownership OFF (Manual/58 °C baseline) for the whole trial; Package 19 pre-cycle automation enabled; `poll_interval: 180`; record the ledger (`requests`, `rolling_24h`) before the change.
1. Install with `demand_aware_polling: false`; for 1 h require 24–30 requests/h and diagnostics `demand_aware_polling=false` (proves the default path).
2. Set `demand_aware_polling: true`, restart the add-on. Diagnostics must show `demand_aware_polling=true` and, after 12 min, `demand_refresh.reused > 0`.
3. Observe ≥ 48 h (two full UTC days) and accept only if **all** hold, measured from the recorder/ledger: (a) ≤ 430 requests per full UTC day (target ≤ 420); (b) source age ≤ 120 s at every five-minute cycle outside quota windows, max observed ≤ 95 s; (c) zero offline publications of the device outside quota/network outages and max gap between successful polls ≤ 300 s; (d) transport failures ≤ 1.5 % of requests; (e) `reused : served` ≥ 3 : 1; (f) every command (if any) confirmed by readback; (g) the quota ledger records first-rejection counts so a rejection below 469 is visible.
4. Only then, with separate approval, trial ownership ON.
**Rollback (any trigger):** one stale cycle outside a quota window, an unexplained offline, a success gap > 300 s, > 460 requests/day, a command not confirmed, or reuse stuck at 0. Set `demand_aware_polling: false` and restart the add-on (restores the 1.9.0 policy exactly; HA store only offers higher versions, so a code rollback is a roll-forward release of the 1.9.0 code). After rollback expect 28 requests/h within 15 min.

## 9. Not covered / NOT_RUN
Container e2e (Docker) and a physical qualification were not run; the simulation models the cloud, Package 19's pre-cycle automation and timing jitter (see `tests/sim_harness.py`); post-stamp work δ in production is unmeasured; whether the vendor's limit scales with request count at all is unproven (issue #17) — if the limit is cost- or cadence-weighted, fewer requests may not help and step 3(g) will show it.
