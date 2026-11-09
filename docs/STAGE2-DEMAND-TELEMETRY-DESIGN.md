# Stage 2 design: demand-triggered telemetry with a reduced background poll (issue #17)

**Status: implementation staged, NOT deployed.** Draft bridge PR #22 includes on-demand MQTT refresh, configurable 60–300 s polling (default 60 s), fixed 300 s age availability, and cadence-aware energy accounting. Separate draft [Package 19 PR #584](https://github.com/MMicieli/home-assistant-config/pull/584) adds pre-cycle refresh and handback freshness checks, with an existing poll-interval diagnostic guard to suppress pre-cycle traffic at the 60 s default. Stage 1 observability was merged in PR #21. Both Stage 2 PRs remain unmerged; source qualification does not authorise the physical rollout. This design depends on facts about the consuming Home Assistant packages that live in a different repository (`home-assistant-config`: Package 18 `18_ems_production_control_v1.yaml`, Package 19 `19_hws_thermowatt_translation_v1.yaml`); they are quoted below as read on 2026-11-08 and must be re-verified at implementation time.

The vendor quota is **not known** (see issue #17). Nothing here assumes a limit; the goal is to cut request volume as far as is safe, and to use the Stage 1 evidence snapshots to decide how much further is needed.

## 1. Problem and constraint summary

At the current 60 s cadence the bridge sends about 1,500 requests/day (measured 62.4/h on 2026-11-07), and was quota-rejected twice in three days. A fixed longer interval (75–90 s) saves only 19–30 % and weakens Package 19's 120 s freshness gate (a single missed poll fails the gate in 22–35 % of cycles, with about 8 % observed transient poll failures). The telemetry is only *consumed for decisions* at EMHASS publication, once every five minutes. Demand-triggered refresh aligns requests with the moments data is actually needed.

Constraints that must not be weakened:

| Constraint | Current contract |
|---|---|
| Freshness | `binary_sensor.ems_hws_current_action_available`: `poll_status == ok`, 0 consecutive failures, `last_successful_poll` age ≤ **120 s** (ownership off) |
| Package 18 timing | `time_pattern minutes: "/5"`; runner `--timeout 50`; fires `ems_emhass_cycle_published` after the runner returns zero |
| Package 19 trigger | event `ems_emhass_cycle_published`; acts only if `sensor.p_deferrable0_state` was rewritten at or after the Package 18 run started (#562) |
| Command cooldown | 15 s per command type |
| Confirmation | 20 s fast polls for a 60 s window after every command; Package 19 bounded waits are 60 s |
| Boost / handback | acquire with TEMP 70 only from the eligibility table; hand back via the single `script.hws_thermowatt_handback` transaction (TEMP 50 if heating and T_Avg > 50, baseline restore, clear ownership only on confirmed restoration or a recognised native non-Manual mode) |
| Ownership | `input_boolean.ems_hws_ownership_active` is cleared only by those two paths; an unknown mode leaves it set |

## 2. Proposed design

### 2.1 Bridge (this repository, Stage 2 PR)

1. **Refresh request (Stage 2a draft).** Subscribe to non-retained MQTT topic `P/<serial>/refresh`. The existing MQTT callback flags the device and wakes the **existing** main polling loop; it never issues HTTP from the callback. Requested polls are limited to one attempt per 10 s whatever the outcome (storm guard); a request inside that window is logged as coalesced and says whether the recent attempt actually produced fresh telemetry. A demand wake polls only the requested device (other devices are polled only when due). The existing `poll_status` path retains validation and quota protection.
   - **No new response protocol:** callers already receive `last_successful_poll`, `poll_status` and retained status through existing discovery entities. No request IDs, additional MQTT diagnostic payload, worker thread or separate queue.
   - Refresh requests are ignored while quota backoff is active or recovery is unverified; only the normal poll can probe the vendor.
   - One bounded retry (5 s later) only when a requested refresh ended without any HTTP response (decided at the I/O boundary); never for an HTTP error, an invalid 200 payload, a local processing error or a quota response. A future Package 19 pre-cycle automation may send **one** further refresh if no new successful timestamp is observed by T-12 s; never retry a quota rejection.
2. **Background poll.** The scheduled poll is configurable with the add-on option `poll_interval` (60–300 s, default and fallback **60 s**; 300 s is a later operator decision). After any poll cycle, including a demand-triggered one, the existing loop waits one full interval, so a demand refresh doubles as the background poll without a second timer.
3. **Availability by age, not by failure count.** Today the device is marked offline after 5 consecutive failed polls (about 5 minutes at 60 s). At a 300 s cadence the same count would mean about 25 minutes of possibly stale "heating on" data. The bridge therefore also marks the device offline when `now - last_successful_poll > 300 s` — a **fixed** bound, deliberately not relaxed for a slower cadence, so at 300 s polling an unaided background poll fails closed and demand refreshes are what keep the device available. It is evaluated by the existing wait loop (wakes at the deadline) and on every diagnostics publish, including during quota backoff. Per-device availability starts **offline** at every process start and becomes online only after the first validated poll, so retained STATUS/availability from a previous run is never presented as live; diagnostic sensors use bridge-level availability only. This preserves today's worst-case stale-data exposure. This is the most important invariant of the design (see section 4.1, which also compares it with a Package 19 guard).
4. **Proactive token refresh margin scales with cadence**: `margin = max(120 s, background_interval + 60 s)`, so a 300 s cadence never lets a token expire between polls (which would revert to the 3-request reactive sequence).
5. **Counters.** Reuse Stage 1's existing attempted-request ledger and poll timestamps. No additional counters or attribution fields unless a demonstrated measurement gap justifies them.
6. **Unchanged:** command path, cooldown, confirmation window and 20 s fast polling, rate-limit backoff and persistence, command rejection while limited, no queue/replay.

### 2.2 Home Assistant (Package 19 / companion, not implemented)

- **Pre-cycle refresh automation (new, independent of Package 18).** Publishes the refresh request at `hh:m4:30` (30 s before each five-minute boundary: `time_pattern minutes: "/1", seconds: 30` with condition `minute % 5 == 4`, so no dependence on range support in `time_pattern`). If, at `hh:m4:48`, `last_successful_poll` is still older than the first request, publish one second request. It performs no waits, holds no lock Package 18 uses, and never blocks Package 18: Package 18's trigger, runner timeout and event are untouched.
- **Handback pre-write refresh.** At the start of `script.hws_thermowatt_handback`, before the first temperature write, request a refresh and wait (bounded, ≤ 20 s) for fresh telemetry; on timeout the existing pattern applies (explicit `condition:` halts the run, ownership untouched, retried at the next EMHASS publication). This makes the data behind the native-mode guard fresher than today for the first write (≤ about 20 s instead of up to about 62 s).
- **Not changed:** `binary_sensor.ems_hws_current_action_available`, its 120 s test, the acquisition sequence, ownership handling, the restart-handback automation (HA start: add a refresh request first, still fail-closed), any Package 18 logic.

## 3. Timing against EMHASS publication

Timeline of one five-minute cycle (`T` = the `:00` boundary; Package 18 `time_pattern`):

| Time | Event | Telemetry age when used |
|---|---|---|
| T-30 s | demand refresh #1 | 0 s at completion (about T-28 s) |
| T-12 s | refresh #2 only if #1 failed | 0 s on success |
| T+0 s | Package 18 run starts; runner reads `binary_sensor.ems_hws_current_action_available` to build `operating_timesteps_of_each_deferrable_load` | about 12–32 s (worst case when #2 recovered: about 14 s) |
| T+about 5–55 s | runner returns, `ems_emhass_cycle_published` fires (runner timeout is 50 s) | up to about 85 s at the very worst |
| event + about 1 s | Package 19 evaluates `binary_sensor.ems_hws_current_action_available` and acts | **at most about 86 s < 120 s guard** with a 34 s margin |

The 120 s guard is unchanged and is satisfied by construction when refresh #1 or #2 succeeds. If both fail, the age reaches 120 s during the cycle and the gate fails closed (`available` off: EMHASS is told HWS is unavailable for that cycle, no acquisition), exactly as a stale-telemetry cycle behaves today. Package 18 and Sigenergy dispatch are never delayed because the refresh is neither on its trigger path nor awaited by it, and the refresh is sent 30 s before the boundary, outside the runner's 50 s budget.

## 4. Failure and edge cases

| Case | Behaviour |
|---|---|
| **Stale or failed refresh #1** | Package 19 may make one conditional second request at T-12 s if no newer valid telemetry arrived. Bridge does not automatically retry. Quota rejections are never retried; two failures leave the 120 s acquisition gate fail-closed. No command queue or replay. |
| **Retries and quota** | Maximum one initial + one conditional HA request per decision cycle; the existing bridge backoff refuses demand requests while limited or recovery is unverified. About 8 % first-attempt failures implies roughly 1.08 attempts per decision on average, subject to measurements. |
| **Command confirmation** | Unchanged: 20 s fast polls for 60 s after each command, the 15 s cooldown, Package 19's 60 s bounded waits. Demand refresh is not used for confirmation. The pre-write refresh in the handback script precedes the first write, not each confirmation. |
| **Native mode change (Off/Eco/Auto/Holiday)** | Detected at the next refresh (up to 5 minutes later, versus 1 minute today). The guard still re-reads the mode immediately before each temperature write, and the handback pre-write refresh makes the first write's mode reading at most about 20 s old. A known non-Manual mode issues no command and releases ownership (existing behaviour). Acquisition (ownership off) requires `Manual` with telemetry at most about 86 s old versus up to about 122 s today. |
| **Cloud unavailable during an active boost** | Telemetry ages past 300 s → device offline (section 2.1, item 3) → `available` off → EMHASS deselects → handback script waits ≤ 60 s for the water heater to be available, then halts with ownership **retained** and the captured baseline intact; one fresh attempt at each next EMHASS publication. The native thermostat remains the physical limit at the 70 °C setpoint. This is the same fail-closed outcome as today; the difference is only that quota exhaustion becomes less likely. |
| **Baseline handback** | Unchanged transaction: ownership recheck → baseline validate → bounded bridge wait → sample → TEMP 50 if heating and T_Avg > 50 → baseline restore with confirmation → clear ownership. Never skipped to save requests. |
| **Retained ownership evidence** | `input_boolean.ems_hws_ownership_active` and the captured baseline are never touched by refresh logic, bridge restarts or rate limits. Stage 1 episode snapshots are counts only and do not contain ownership data. |
| **Rate limit during a command** | Refused locally (`submit_failed`, `rate_limited`), never queued, as in 1.8.0; ownership retained. |
| **Bridge or HA restart** | The bridge polls immediately at boot (as today) and honours a persisted quota window before any request; the restart-handback automation requests a refresh first and stays fail-closed. |
| **Package 18 / Sigenergy** | No change to Package 18's trigger, runner, timeout, event or gates. The new automation is independent and never waits on or blocks it. |
| **Duplicate or rapid refresh requests** | Coalesced / spaced (10 s) by the bridge. |

### 4.1 Ownership-on freshness: bridge-side age availability vs an explicit Package 19 guard

**The gap.** With ownership on, `binary_sensor.ems_hws_current_action_available` is just `heating_state == 'on'` with no age test (the 120 s test applies only to acquisition). Today stale "heating on" is bounded only because the bridge makes the entities `unavailable` after five failed 60 s polls (about 5 minutes). A 300 s background cadence with the failure-count rule unchanged would stretch that to about 25 minutes. Some bound is therefore a precondition of Stage 2.

| | **A. Bridge: unavailable when `now - last_successful_poll > 300 s`** | **B. Package 19: explicit age test in the ownership-on branch** |
|---|---|---|
| Covers | every consumer (dashboard, acquisition, ownership-on, handback wait, restart handback) | only the one template |
| Bound on stale "heating on" | 300 s, equal to today's worst case | whatever threshold is chosen |
| Deploy order | bridge first or together; protects even if Package 19 is untouched | inert until the bridge cadence changes; never protects other consumers |
| Failure of its own input | none (uses the bridge's own clock) | depends on parsing the `last_successful_poll` attribute in Jinja; unparsable must fail closed **without** touching ownership |
| Test level | Python unit tests against the real bridge | Jinja replay only; lives in another repository and needs a reviewed PR plus separate operator approval |
| Behaviour on a dual refresh failure (about 0.6 % of cycles, about 2 a day at 8 % per-attempt failure) | tolerated up to 300 s: no churn | at a threshold below 300 s, `available` turns off, EMHASS deselects and the handback script starts a baseline restore through a cloud that just failed twice: avoidable commands, cooldown and 70 °C-to-baseline churn. At a threshold of 300 s it only duplicates A |
| Cost | one bounded state-machine change plus an `unavailable` transition when data ages | one more Jinja condition to keep consistent with the 120 s acquisition test |

**Recommendation.** A is the required, primary bound; B is not added by default. B is only worth adding if the qualification replay shows a residual hazard that A does not cover, and then at a threshold no tighter than A's, because a tighter guard buys churn rather than safety (the native thermostat is the physical limit at 70 °C either way). If B is later adopted it is a separate Package 19 commit with its own replay, never bundled with the bridge change.

**What neither option may change (handback and native-mode authority).**
- Ownership clears only on a confirmed baseline restore or a recognised native non-Manual mode, exactly as today. Stale or `unavailable` data is **not** a native mode: it must retain ownership and the captured baseline (the same path as a cloud outage in 1.8.0, so this is not a new case; to be re-verified in Package 19 at implementation time).
- While data is unavailable, native mode changes cannot be observed. They are recognised on the first fresh sample after recovery, and every temperature write still re-reads the mode immediately before writing.
- The handback script's pre-write refresh runs before any freshness verdict is applied to a write, so a stale background reading can never block the refresh that would make it fresh.
- A guard must never be the reason ownership is released, and never the reason a baseline restore is skipped.

## 5. Expected request volume (no vendor quota assumed)

Assumptions, each from measured data in issue #17: effective cycle 62 s today; transient failure 8 % per attempt; access token 1,800 s; one command = 1 request plus 3 fast confirmation polls (20 s × 60 s window); a boost cycle = acquisition (4) + conditional TEMP 50 (4) + baseline restore (4) = about 12 requests, with a bounded 401 replay adding at most 1 per command.

| Component | Requests/day | Basis |
|---|---|---|
| Demand refresh | about 311 | 288 × (1 + 0.08 retry) |
| Authentication (proactive) | about 58 | refresh about every 1,500 s (margin 360 s of 1,800 s) |
| Commands + confirmation | about 24–90 | 2–6 boost cycles/day × about 12 (+ replays) |
| Background poll | about 0 | skipped when demand refresh is recent |
| **Total expected** | **about 390–460** | |
| Same with 20 % transient failures | about 430–500 | 288 × 1.2 = 346 |
| Today (Stage 1 baseline) | about 1,500 | measured |
| **Reduction** | **about 69–74 %** | |
| Recovery probes while limited | about 72 | only during a quota window (hourly cap, 3 requests each) |

This is a request-*volume* estimate for the bridge only. App usage and the vendor's accounting (whether rejected, refresh or login calls count) are unknown, so no claim is made that this volume avoids rejection; the Stage 1 first-rejection and recovery snapshots exist to find out.

## 6. Qualification plan

1. **Bridge PR (Stage 2), defaults unchanged:** refresh topic, age-based offline threshold, configurable background interval (default 60 s) and the energy-integration cap (`2 * POLL_INTERVAL`, which follows the configured interval; #20) in one draft PR, with one bounded transport retry for requested refreshes. Unit tests: coalescing, spacing, no HTTP from MQTT callback, no I/O during backoff or recovery-unverified, age-based offline, token margin scaling, command path regression.
2. **Offline replay:** drive the Jinja availability template and the Package 19 conditions against recorded timelines (nominal, one failed refresh, both failed, 5-minute cloud outage during ownership, native mode change mid-boost) and show the decision and ownership outcome equals the 60 s baseline or is more conservative.
3. **Shadow in production (operator-approved):** install the bridge with default 60 s polling and the Home Assistant refresh automation *disabled*; send manual refresh requests and verify diagnostics.
4. **Additive HA automation (operator-approved):** enable the pre-cycle refresh while the background poll stays at 60 s; confirm in the ledger that demand refresh succeeds ahead of every cycle and the freshness age at decision time stays below 90 s.
5. **Reduce the background poll (operator-approved)** to 300 s only after step 4 evidence; measure requests/day from the Stage 1 ledger.
6. Physical qualification of one full boost → handback cycle with a deliberately interrupted refresh remains an operator-approved live step.

## 7. Rollback

Each step is independently reversible: disable the Home Assistant refresh automation (data continues at the background interval); set the background interval back to 60 s; restore the previous add-on version (configuration is backward compatible, as in 1.8.0); the Package 19 handback pre-refresh is a separate commit. No state migration is involved.

## 8. Decisions for the operator (not assumed here)

- Confirm A (bridge-side age availability, section 4.1) as the sole ownership-on bound, or ask for B (a Package 19 ownership-on age test, at a threshold no tighter than 300 s) as a second layer.
- Background interval target (300 s versus 600 s) after Stage 1 evidence.
- Whether to add the handback pre-write refresh (recommended) given it touches `script.hws_thermowatt_handback`.

## 9. Limitations

Volumes are estimates from the bridge's own counters; the vendor quota, reset rule, app share and how rejected or authentication requests are counted are unknown; the 8 % transient failure rate comes from about 4 hours of retained logs and may differ; Package 18/19 behaviour is quoted from the file contents on 2026-11-08, not from a live behavioural test.
