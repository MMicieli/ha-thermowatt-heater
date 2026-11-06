# Changelog
All notable changes to this project will be documented in this file.
The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

> **Fork note:** This repository is forked from [waterheater-dev/ha-thermowatt-heater](https://github.com/waterheater-dev/ha-thermowatt-heater) at v1.3.0. Versions 1.0.0–1.3.0 reflect upstream history. Changes from v1.4.0 onwards are specific to this fork.

## [Unreleased]

### Fixed
- **Rate-limit detection.** HTTP 429, and HTTP 200 bodies whose `error` is the confirmed `Daily request limit exceeded` message, are now recognised as rate limits. Previously only HTTP 429 was handled; the HTTP 200 form was counted as a generic invalid payload, so the bridge kept polling every 60 s (and fast-polling after commands) against an exhausted daily quota. Unrelated invalid payloads (for example `Water heater not found`) are still ordinary poll failures, not rate limits.
- **Bounded exponential backoff.** On a rate limit all API requests stop for 120, 240, 480, 960, 1920 then at most 3600 s (a `Retry-After` header can lengthen, never shorten or exceed that ceiling). The window takes priority over the 60 s poll, command-triggered fast polling and poll wake-ups, and quota errors never trigger token refresh or re-login. A successful poll clears the backoff. Normal polling stays at 60 s.
- **Restart persistence.** The backoff window and step are persisted (written only on transitions) and honoured on boot before any API request, so a restart loop cannot hammer an exhausted quota. Persisted values are validated and clamped.
- **Command response validation.** A 2xx command response whose JSON body has `success: false` is now `submit_failed` (with a truncated reason), not `pending`, and does not start the confirmation window or consume the cooldown. Body-less and other 2xx responses remain successful submissions.
- **No command queue/replay.** While a rate-limit window is active, MODE/TEMP commands are refused locally (`submit_failed`, `rate_limited`), never queued and never replayed after recovery. Pending commands still expire on their normal 60 s deadline during a backoff.

### Added
- Diagnostics: `rate_limit` (active, retry time, backoff step, last reason/time, lifetime events) and per-UTC-day `requests` counters (status/command/auth/other/total/rate_limited_responses/blocked), exposed as `Rate Limit Status` and `API Requests (UTC day)` diagnostic sensors. Counters are persisted at most every 10 minutes and on shutdown. No tokens, credentials or API response bodies are published.
- Command diagnostics gain `confirm_latency_s` and `confirm_poll_number` (which fresh poll first matched) to provide the evidence needed to decide whether the immediate post-command poll is worthwhile.

### Review corrections (PR #16)
- A successful poll that began before a newer rate-limit event (for example a command response handled on the MQTT thread) can no longer clear that event: backoff is cleared only by a poll whose request began after the latest classified rate-limit response, and never while a window is unexpired.
- Rate-limit detection and command rejection now share one predicate: a body carrying the `Daily request limit exceeded` error is a quota rejection whether or not it has `success: false`, always starts the backoff, and can never create a `pending` command.
- Startup login and `/user-info` discovery rate limits (including a window persisted from the previous run) are waited out in-process and retried instead of `exit(1)` and a supervisor restart loop. Other startup failures still exit; SIGTERM during the wait exits cleanly.
- Each HTTP response is classified once (memoised on the response). Counter semantics: `status`/`command`/`auth`/`other` and `total` count requests actually sent; `rate_limited_responses` counts responses classified as a rate limit; `blocked` counts requests refused locally by an active window (never in `total`); `events_total` counts backoff windows.
- `poll_status` reports `rate_limited` from the first detection until a good poll confirms recovery (any value other than `ok` keeps Package 19's `poll_status == 'ok'` freshness test fail-closed; `consecutive_poll_failures` still increments on the first detection and `last_successful_poll` age is unchanged). New `telemetry` diagnostics block (`state`: `fresh`/`stale`/`rate_limited`/`unknown`, `age_s`, `stale_after_s`; informational only) and diagnostics are republished at least once a minute during backoff so age and retry time stay current. No entity availability change.

### Final review amendment
- `rate_limit.state` is now `none` / `active` / `recovery_unverified` / `recovered` (plus `recovered_at`); the `Rate Limit Status` sensor shows `limited`, `recovery_unverified` or `ok`, so it no longer reads `ok` between window expiry and the first successful poll. Diagnostics are republished when the window ends. Package 19 inputs are unchanged (`poll_status` stays `rate_limited` until recovery is verified).
- Cross-system tests now cover both Package 19 ownership-release paths (confirmed baseline; native Off/Eco/Auto/Holiday at either guard, issuing no command) and the fail-closed unknown/unavailable/unrecognised-mode cases. Package 19 is not modified.
- Documented: latched `command_status=submit_failed` and how it clears; that the restart-handback automation is a bounded attempt per Home Assistant start (not a recurring retry, not triggered by a bridge restart); and that "no replay" refers to rate-limit recovery. The existing HTTP 401 path re-sends the same request (including a command POST) once after a successful token refresh, within the same call; this is now tested (single replay, no third attempt, no replay on failed/rate-limited refresh) and commented in code.

### Documented limitations
- The request counters are per **UTC calendar day**, bridge-wide and in-memory between throttled flushes. They do **not** measure a rolling 24-hour quota and the vendor's reset instant is unknown; treat them as a relative indicator only. Requests made by the Thermowatt app or other clients are not counted.
- Estimated HWS energy integrates `element_wattage x elapsed` only for a gap capped at 2x the 60 s poll interval. After a prolonged telemetry outage (rate limit, cloud or network) the uncounted heating time is **not** back-filled, so estimated energy can under-count.
- While a rate-limit window is active the entity states are the last retained values; Package 19 must continue to use `last_successful_poll` age and `poll_status` rather than entity availability.

### Unchanged (deliberately)
- The immediate post-command poll is retained: no in-repo evidence establishes that it never confirms, and removing it without a replacement would leave the first confirmation poll up to 60 s away (the whole confirmation window). Revisit with the new latency diagnostics.
- 15 s command cooldown, 60 s confirmation window, 60 s normal polling and fresh-readback-only confirmation.

---

## [1.7.3] - 2026-08-17

### Fixed
- Raw `Cmd` is decoded as a bitfield (`family = Cmd & ~1`, `enabled = bool(Cmd & 1)`) instead of a hard-coded value list. Home Assistant mode state and Off command confirmation previously recognised only `Cmd=8` as Off; a read-only device-history audit (issue #13) found the device also reports `Cmd=64` (Holiday family, disabled) and, historically, `Cmd=16` (Auto family, disabled) as off-like states, none of which the bridge recognised — `Cmd=64` in particular caused Home Assistant to retain a stale prior mode (`Manual`) instead of showing `Off`. The new `decode_cmd()` helper resolves any recognised family with the enabled bit cleared to `Off`; unrecognised or absent/malformed `Cmd` remains unknown and is never defaulted to `Off`. The decoded mode is now computed once bridge-side and published as `ha_mode` in `STATUS`, and the MQTT discovery `mode_state_template` passes it through directly rather than duplicating bit logic in Jinja. Off command confirmation now accepts a fresh readback from any recognised family with the enabled bit cleared, not only an exact `Cmd=8` match. The `/off` endpoint and payload are unchanged.

---

## [1.7.2] - 2026-08-13

### Fixed
- A poll is now only treated as successful when it returns usable device status, not merely HTTP 200. The Thermowatt cloud API has been observed returning HTTP 200 with `{"success": false, "error": "Water heater not found, check the Wi-Fi connection"}` — no `result` object at all — which the bridge previously counted as a healthy poll (`poll_status: ok`, `consecutive_failures: 0`, `last_successful_poll` still advancing) while publishing that invalid body to the retained `STATUS` topic and reconciling pending MODE/TEMP commands against it (`observed=None`, false `mismatched`). Such a payload is now counted as a poll failure through the existing consecutive-failure/`DEGRADED_THRESHOLD`/availability machinery: it does not update `last_successful_poll`, does not reset the failure counter, does not advance energy accumulation, is not published to `STATUS`, and is not used to reconcile a pending command's `fresh_poll_seen`. Diagnostics gain a single `last_poll_error` field (cleared on the next valid poll) reflecting the reason for the most recent poll failure. An empty `result` dict (`{"result": {}}`) is rejected the same way — a dict with no fields still carries no usable device status.

---

## [1.7.1] - 2026-08-11

### Fixed
- Off mode now uses the live device readback `Cmd=8` for Home Assistant state discovery and bounded `/off` command confirmation, replacing the inherited `Cmd=16` assumption disproved during supervised device testing.

### Changed
- Existing bridge log output is now prefixed with ISO-8601 UTC timestamps at millisecond precision for easier command/poll correlation.

---

## [1.7.0] - 2026-08-11

### Added
- Bounded per-device command confirmation for MODE and TEMP. A successful API submission is recorded as `pending` until a newer device-status poll confirms the requested raw field.
- `Command Status` MQTT diagnostic sensor with independent MODE and TEMP details, including requested and observed values plus confirmation timestamps.

### Changed
- Successful command submission no longer publishes synthetic operational `STATUS` data or advances `last_polled_at`.
- The polling loop wakes promptly after successful submission and uses the existing 20-second interval for a bounded 60-second confirmation window.
- A fresh non-matching readback becomes `mismatched` at the deadline; absence of a fresh readback becomes `timed_out`.
- Commands are never automatically retried or republished after mismatch or timeout.
- README terminology now identifies bridge-derived power and energy as estimates rather than measurements.

---

## [1.6.3] - 2026-06-22

### Added
- `element_wattage` config option (default 3000 W) — used for MQTT power sensor template and bridge-side energy integration. Accepts integers in the range [100, 10000] W; values outside that range or non-integers fall back to 3000 W. Not a CT measurement — this is a heating-state estimate only.
- Per-device degraded/offline availability: after `DEGRADED_THRESHOLD` (5) consecutive poll failures the per-device MQTT availability topic is set to `offline`, marking all device entities unavailable in HA. Entities recover to `online` automatically when polling resumes. Bridge-level LWT behaviour for unclean disconnects is preserved and independent.
- Per-command-type cooldown: MODE and TEMP each maintain independent 15-second cooldown windows so a valid paired sequence (e.g. set mode then set temperature immediately after) is never incorrectly blocked by the other type's cooldown.
- Diagnostic MQTT sensors on `P/{serial}/diagnostics`: `poll_interval` (s), `consecutive_failures` (count), `last_successful_poll` (ISO8601 timestamp), `element_wattage` (W), `poll_status` (`ok`/`degraded`). All read-only; remain available even when per-device polling is failing.
- Multi-availability discovery payloads: operational entities now declare both the bridge-level LWT topic and the per-device availability topic (`availability_mode: all`). Diagnostic sensors use bridge-level availability only.

### Fixed
- `mode_state_template` unknown/absent `Cmd` values previously fell through to `Off` — they now return an empty string so HA treats the mode as unknown instead of falsely showing the device as Off.
- Unknown MODE command payloads (values not in the valid set) now log a warning and perform no API call, rather than silently no-op without any log output.
- Energy integration inherits the 2× poll-interval cap introduced in v1.6.2 (`elapsed_s` capped at `2 × POLL_INTERVAL`) and applies it with the configured `element_wattage` instead of the hardcoded 3 kW.

---

## [1.6.2] - 2026-06-21
### Fixed
- **Thread-safety of the shared `requests.Session`** — `login()`, `refresh_session()`, and `request()` now run under a re-entrant `_http_lock`. The session is not thread-safe and was mutated (`_reset_headers()`, `seriale`/`x-api-key`/`Authorization`) from both the main poll thread and the paho MQTT callback thread, which could clear/overwrite headers mid-flight and cause spurious 401s or a misrouted `seriale` on multi-device installs.
- **`_update_auth` now writes config under `_config_lock`** — token refresh (reachable from the MQTT callback thread via a 401) previously wrote `config.json` without the lock the poll thread uses, so two threads could `json.dump()` the same dict concurrently and corrupt the file or drop the persisted energy counter.
- **Energy integration step is now capped at 2× the poll interval** — `_last_poll_ts` only advances on a successful poll, so after a poll outage (429 backoff, cloud/network downtime) the next good poll could attribute the entire gap to heating and inject a large jump into the `total_increasing` energy sensor.

### Changed
- Pinned Python dependencies in the Dockerfile (`paho-mqtt>=2.1,<3`, `requests>=2.31,<3`, `urllib3>=2,<3`) for reproducible builds. `paho-mqtt>=2` is required for `CallbackAPIVersion.VERSION2`.

---

## [1.6.1] - 2026-05-16
### Fixed
- `_inject_fake_status` heating flag no longer incorrectly re-applies the stale cached `WaterHeaterSts` value for mode-only commands. Previously, the bitmask recomputation always ran regardless of whether `WaterHeaterSts` was in the overrides — meaning mode changes (Eco, Auto, Manual, Holiday, Off) left `heating` reflecting the pre-command poll value for up to 20 seconds.
- Off command now explicitly passes `heating=False` so `sensor.hws_hws_power` and `binary_sensor.hws_heating` immediately show 0 W / off after a successful Off command.

### Changed
- `_inject_fake_status` gains optional `heating` kwarg. `WaterHeaterSts` bitmask recomputation now only runs when `WaterHeaterSts` is itself present in the overrides dict; otherwise the last-polled `heating` value is preserved unchanged (or overridden directly via the new kwarg).

---

## [1.6.0] - 2026-05-16
### Added
- Power sensor (sensor.hws_hws_power) — MQTT discovery on P/{serial}/STATUS, publishes 3000 W when heating, 0 W otherwise. device_class: power, state_class: measurement.
- Energy sensor (sensor.hws_hws_energy_kwh) — separate topic P/{serial}/energy_kwh. Bridge accumulates 3 kW × elapsed hours each poll cycle, persisted in config.json across restarts. state_class: total_increasing — qualifies for the HA Energy Dashboard directly.

### Fixed
- on_connect callback added — re-subscribes to all P/{serial}/CMD/# topics and re-publishes "online" on every (re)connect. Without this, MQTT reconnects silently lost all command subscriptions.
- on_disconnect callback added for logging.
- Callbacks registered before connect() so they fire on the initial connection too.
- STATUS topic publishes changed to QoS=1 (was 0) in both poll_status and _inject_fake_status.
- 429 handling: break → continue so remaining devices still get polled after one device hits a rate limit.
- threading.Lock (_config_lock) protects self.config for concurrent reads/writes between the main poll thread and the MQTT callback thread.

---

## [1.5.3] - 2026-05-09
### Fixed
- `Time_prog` state_class corrected from `measurement` to `total_increasing` — confirmed lifetime accumulating counter (observed delta 282 min over single day)

---

## [1.5.2] - 2026-05-08
### Fixed
- Replace deprecated `datetime.utcnow()` with timezone-aware `datetime.now(UTC)` (Python 3.12 compatibility)

---

## [1.5.1] - 2026-05-08
### Added
- MQTT LWT and availability_topic on all discovery payloads — entities show unavailable when bridge dies
- TAmb (Ambient Temperature) as first-class sensor entity — required for Phase 2 thermal calibration
- WaterHeaterSts raw integer as first-class sensor entity — for anomaly filtering in InfluxDB queries
- HTTP_TIMEOUT = (5, 15) on all cloud API calls — prevents hung requests freezing polling
- SIGTERM handler — HA Supervisor stop now triggers clean shutdown path
- Post-command fast-poll confirmation window (20s for 60s after command, then resumes 60s)

### Changed
- Polling interval 20s → 60s normal operation (reduces cloud rate-limit risk, cleaner calibration data)
- CMD_COOLDOWN 60s → 15s (was blocking valid paired set_temperature + set_operation_mode sequences)
- max_temp corrected 75°C → 70°C (matches confirmed firmware T_set_max)
- Time_eco / Time_prog state_class: total_increasing → measurement (unconfirmed counter behaviour)
- TLS verify=False now opt-in only via THERMOWATT_TLS_NO_VERIFY env var (default: verify enabled)

### Fixed
- optimistic:True removed from water_heater discovery — contradicted mode_state_topic confirmed state
- last_polled value_template returns none instead of string 'unknown' (HA timestamp device_class compatibility)
- Retained MQTT commands ignored on restart — prevents stale CMD topic replay after bridge restart
- Offline published explicitly on clean shutdown (LWT alone only fires on unclean disconnect)
- Server-side temperature clamp max(20, min(70, temp)) — firmware silently rejects values above 70°C
- Restored truncated last_polled_at sensor discovery payload (was missing 6 of 8 required keys)
- Binary sensor heating value_template case mismatch (Python True/False vs payload_on 'true')
- Mode display broken when unit off (lowercase 'off' did not match operation_list 'Off')
- Extra API call eliminated on every command (_inject_fake_status now deep-copies from cache)

---

## [1.4.0] - 2026-05-08
> Not released as a standalone version — changes included in v1.5.1.
### Added
- Binary sensor for heating active state using WaterHeaterSts bitmask
- Computed heating boolean injected into every poll and fake status
- T_Avg used as current_temperature (more accurate than display value)
- json_attributes_template exposing full result payload as HA attributes
- Exponential backoff on 429 responses
- 401 auto-refresh with session retry
- Six dedicated MQTT sensor discoveries: T_Avg, T_dsrd, TBoost, Time_eco, Time_prog, Rssi, last_polled_at

### Fixed
- Binary sensor never fired — value_template rendered Python True/False, payload_on expected lowercase true
- Mode display broken when unit off — Cmd 16 returned lowercase off, not matching operation_list Off
- Extra API call on every command — _inject_fake_status now deep-copies from cache instead of live GET
- No staleness detection — last_polled_at UTC timestamp added and exposed as dedicated sensor
- EMS-critical fields buried in attributes — promoted to first-class sensor entities for InfluxDB logging

---

## [1.3.0] - 2026-01-27
### Removed
- AWS MQTT bridge for real-time status updates
- Certificate-based AWS IoT authentication
### Added
- Polling loop to avoid rate limiting issues
### Fixed
- Rate limiting issues from frequent API polling

---

## [1.2.0] - 2026-01-25
### Added
- AWS MQTT bridge for real-time status updates (replaces polling)
- Support for multiple devices with per-device AWS MQTT clients
- Certificate-based AWS IoT authentication
- Command cooldown mechanism to prevent stale status updates after commands
### Changed
- Commands now use REST API (matching app behavior)
- Status updates come from AWS MQTT instead of polling
- Removed polling loop to avoid rate limiting issues
- Updated status format handling to match AWS MQTT format
### Fixed
- Rate limiting issues from frequent API polling
- Status update cooldown after commands to prevent stale values overwriting optimistic updates

---

## [1.1.1] - 2026-01-24
### Changed
- Polling interval set to 60 seconds
- Updated HomeAssistant mode names to match app behavior

---

## [1.1.0] - 2026-01-24
### Changed
- Upgraded to support breaking backend changes observed after the release of app version 3.14

---

## [1.0.0] - 2026-01-18
### Added
- Initial release
- Basic MQTT bridge functionality
- Home Assistant discovery integration
- Support for temperature and mode control

---
[1.7.3]: https://github.com/MMicieli/ha-thermowatt-heater/compare/v1.7.2...v1.7.3
[1.7.2]: https://github.com/MMicieli/ha-thermowatt-heater/compare/v1.7.1...v1.7.2
[1.7.1]: https://github.com/MMicieli/ha-thermowatt-heater/compare/v1.7.0...v1.7.1
[1.7.0]: https://github.com/MMicieli/ha-thermowatt-heater/compare/v1.6.3...v1.7.0
[1.6.3]: https://github.com/MMicieli/ha-thermowatt-heater/compare/v1.6.2...v1.6.3
[1.6.2]: https://github.com/MMicieli/ha-thermowatt-heater/compare/v1.6.1...v1.6.2
[1.6.1]: https://github.com/MMicieli/ha-thermowatt-heater/compare/v1.6.0...v1.6.1
[1.6.0]: https://github.com/MMicieli/ha-thermowatt-heater/compare/v1.5.3...v1.6.0
[1.5.3]: https://github.com/MMicieli/ha-thermowatt-heater/compare/v1.5.2...v1.5.3
[1.5.2]: https://github.com/MMicieli/ha-thermowatt-heater/compare/v1.5.1...v1.5.2
[1.5.1]: https://github.com/MMicieli/ha-thermowatt-heater/compare/1.3.0...v1.5.1
[1.4.0]: https://github.com/MMicieli/ha-thermowatt-heater/compare/1.3.0...v1.5.1
[1.3.0]: https://github.com/waterheater-dev/ha-thermowatt-heater/compare/1.2.0...1.3.0
[1.2.0]: https://github.com/waterheater-dev/ha-thermowatt-heater/compare/1.1.1...1.2.0
[1.1.1]: https://github.com/waterheater-dev/ha-thermowatt-heater/compare/1.1.0...1.1.1
[1.1.0]: https://github.com/waterheater-dev/ha-thermowatt-heater/compare/1.0.0...1.1.0
[1.0.0]: https://github.com/waterheater-dev/ha-thermowatt-heater/releases/tag/1.0.0
