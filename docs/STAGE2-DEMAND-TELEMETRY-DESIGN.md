# Stage 2 design: demand-triggered telemetry with a reduced background poll (issue #17)

**Status: design only. Nothing in this document is implemented, released or deployed.** No Package 19 change is authorised by it. Stage 1 (observability and proactive token refresh) is a separate pull request. This design depends on facts about the consuming Home Assistant packages that live in a different repository (`home-assistant-config`: Package 18 `18_ems_production_control_v1.yaml`, Package 19 `19_hws_thermowatt_translation_v1.yaml`); they are quoted below as read on 2026-11-08 and must be re-verified at implementation time.

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

1. **Refresh request.** New MQTT topic `P/<serial>/refresh` (payload: optional `{"id": "<opaque>"}`). On receipt the bridge performs one `/status` poll immediately through the normal `poll_status` path, so every existing validation, rate-limit and counter rule applies.
   - Single flight: a request received while a poll is running is coalesced into it (no second request).
   - Minimum spacing between demand-triggered polls (default 10 s): a request inside the spacing returns the existing fresh reading instead of sending a request.
   - Refused (no I/O) while a quota window is active or recovery is unverified, exactly like polls and commands.
   - One bounded transport retry after 5 s **only** for a transport failure (e.g. `RemoteDisconnected`); never for a quota response, never more than one.
   - The result is published in the diagnostics as `refresh: {id, status: ok|failed|rate_limited|coalesced|spaced, completed_at}`. No Package 19 change is needed to read it.
2. **Background poll.** The scheduled poll becomes configurable (`background_poll_interval`, default **60 s until Stage 2 is qualified**, target 300 s) and is skipped when a successful poll (demand or background) happened within half the interval, so the demand refresh doubles as the background poll.
3. **Availability by age, not by failure count.** Today the device is marked offline after 5 consecutive failed polls (about 5 minutes at 60 s). At a 300 s cadence the same count would mean about 25 minutes of possibly stale "heating on" data. The bridge must therefore mark the device offline when `now - last_successful_poll > 300 s` (cadence-independent), preserving today's worst-case stale-data exposure. This is the most important invariant of the design (see "Why the bridge-side offline threshold is mandatory").
4. **Proactive token refresh margin scales with cadence**: `margin = max(120 s, background_interval + 60 s)`, so a 300 s cadence never lets a token expire between polls (which would revert to the 3-request reactive sequence).
5. **Counters.** The Stage 1 ledger gains a `demand` attribution so the evidence shows how many requests were demand-triggered versus background versus commands.
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
| **Stale or failed refresh #1** | One bounded transport retry in the bridge after 5 s, plus the conditional #2 at T-12 s. A rejected (quota) response is never retried. Both failing leaves telemetry old; the 120 s gate fails closed for that cycle. No command is queued or replayed. |
| **Retries and quota** | Retry budget is at most 1 bridge retry + 1 HA re-request per cycle (about 8 % observed failure rate: about 1.08 attempts per cycle expected). Nothing is ever sent during a quota window or while recovery is unverified. |
| **Command confirmation** | Unchanged: 20 s fast polls for 60 s after each command, the 15 s cooldown, Package 19's 60 s bounded waits. Demand refresh is not used for confirmation. The pre-write refresh in the handback script precedes the first write, not each confirmation. |
| **Native mode change (Off/Eco/Auto/Holiday)** | Detected at the next refresh (up to 5 minutes later, versus 1 minute today). The guard still re-reads the mode immediately before each temperature write, and the handback pre-write refresh makes the first write's mode reading at most about 20 s old. A known non-Manual mode issues no command and releases ownership (existing behaviour). Acquisition (ownership off) requires `Manual` with telemetry at most about 86 s old versus up to about 122 s today. |
| **Cloud unavailable during an active boost** | Telemetry ages past 300 s → device offline (section 2.1, item 3) → `available` off → EMHASS deselects → handback script waits ≤ 60 s for the water heater to be available, then halts with ownership **retained** and the captured baseline intact; one fresh attempt at each next EMHASS publication. The native thermostat remains the physical limit at the 70 °C setpoint. This is the same fail-closed outcome as today; the difference is only that quota exhaustion becomes less likely. |
| **Baseline handback** | Unchanged transaction: ownership recheck → baseline validate → bounded bridge wait → sample → TEMP 50 if heating and T_Avg > 50 → baseline restore with confirmation → clear ownership. Never skipped to save requests. |
| **Retained ownership evidence** | `input_boolean.ems_hws_ownership_active` and the captured baseline are never touched by refresh logic, bridge restarts or rate limits. Stage 1 episode snapshots are counts only and do not contain ownership data. |
| **Rate limit during a command** | Refused locally (`submit_failed`, `rate_limited`), never queued, as in 1.8.0; ownership retained. |
| **Bridge or HA restart** | The bridge polls immediately at boot (as today) and honours a persisted quota window before any request; the restart-handback automation requests a refresh first and stays fail-closed. |
| **Package 18 / Sigenergy** | No change to Package 18's trigger, runner, timeout, event or gates. The new automation is independent and never waits on or blocks it. |
| **Duplicate or rapid refresh requests** | Coalesced / spaced (10 s) by the bridge. |

### Why the bridge-side offline threshold is mandatory

With ownership on, `binary_sensor.ems_hws_current_action_available` is just `heating_state == 'on'` with no age test. Today that is safe only because the bridge flips the entities to `unavailable` after five failed 60 s polls (about 5 minutes). If the cadence became 300 s without changing that rule, stale "heating on" could persist for about 25 minutes. A time-based threshold (section 2.1, item 3) keeps the worst-case exposure at about 5 minutes. Alternatively the ownership-on branch could gain an explicit age test in Package 19; that is a Package 19 change and needs a separate review. The bridge change is preferred because it keeps every consumer safe.

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

1. **Bridge PR (Stage 2a), defaults unchanged:** refresh topic, spacing/coalescing, one retry, age-based offline threshold (flagged so default behaviour equals 1.8.0), configurable background interval (default 60 s). Unit tests: coalescing, spacing, no I/O during backoff and recovery-unverified, no retry on quota, single transport retry, age-based offline, token margin scaling, command path regression.
2. **Offline replay:** drive the Jinja availability template and the Package 19 conditions against recorded timelines (nominal, one failed refresh, both failed, 5-minute cloud outage during ownership, native mode change mid-boost) and show the decision and ownership outcome equals the 60 s baseline or is more conservative.
3. **Shadow in production (operator-approved):** install the bridge with default 60 s polling and the Home Assistant refresh automation *disabled*; send manual refresh requests and verify diagnostics.
4. **Additive HA automation (operator-approved):** enable the pre-cycle refresh while the background poll stays at 60 s; confirm in the ledger that demand refresh succeeds ahead of every cycle and the freshness age at decision time stays below 90 s.
5. **Reduce the background poll (operator-approved)** to 300 s only after step 4 evidence; measure requests/day from the Stage 1 ledger.
6. Physical qualification of one full boost → handback cycle with a deliberately interrupted refresh remains an operator-approved live step.

## 7. Rollback

Each step is independently reversible: disable the Home Assistant refresh automation (data continues at the background interval); set the background interval back to 60 s; restore the previous add-on version (configuration is backward compatible, as in 1.8.0); the Package 19 handback pre-refresh is a separate commit. No state migration is involved.

## 8. Decisions for the operator (not assumed here)

- Whether the age-based offline threshold should live in the bridge (recommended) or whether Package 19's ownership-on branch should gain its own age test.
- Background interval target (300 s versus 600 s) after Stage 1 evidence.
- Whether to add the handback pre-write refresh (recommended) given it touches `script.hws_thermowatt_handback`.

## 9. Limitations

Volumes are estimates from the bridge's own counters; the vendor quota, reset rule, app share and how rejected or authentication requests are counted are unknown; the 8 % transient failure rate comes from about 4 hours of retained logs and may differ; Package 18/19 behaviour is quoted from the file contents on 2026-11-08, not from a live behavioural test.
