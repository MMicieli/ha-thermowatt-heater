# Thermowatt Smart Boiler Bridge for Home Assistant

This add-on bridges Thermowatt-based smart water heaters into Home Assistant via local MQTT. It polls the Thermowatt cloud API, publishes device state as MQTT Discovery entities, and routes HA commands back to the cloud — with safety hardening for use as an EMS-controlled deferrable load.

[![Open your Home Assistant instance and show the add add-on repository dialog with a specific repository URL pre-filled.](https://my.home-assistant.io/badges/supervisor_add_addon_repository.svg)](https://my.home-assistant.io/redirect/supervisor_add_addon_repository/?repository_url=https%3A%2F%2Fgithub.com%2FMMicieli%2Fha-thermowatt-heater)

## Features

- **Polled Device Monitoring**: Thermowatt cloud-status polling (normally every 60 seconds), forwarded to Home Assistant via local MQTT; this is **not** a real-time local-device feed. Device-reported tank temperature uses `T_Avg`. Do not assume this is a well-mixed whole-tank average for thermal modelling; the verified Thermann Smart Electric installation documented by this project uses bottom-of-tank temperature sensing.
- **Full Control**: Set target temperatures and operation modes (Manual / Eco / Auto / Holiday / Off)
- **MQTT Discovery**: Automatically creates the water-heater entity, heating-state binary sensor, operational telemetry and bridge diagnostics
- **EMS-ready Sensors**: Dedicated first-class entities for actual heating state, temperatures, mode, setpoint, poll freshness and estimated element power
- **Availability Tracking**: Operational entities require both bridge connectivity and healthy device polling; diagnostic entities remain visible while device polling is degraded
- **Stale Data Protection**: `last_polled_at` advances only after a successful device-status poll
- **Command Confirmation**: HTTP success means submitted, not applied; MODE and TEMP remain pending until confirmed by newer device readback
- **Power & Energy Monitoring**: Configurable heating-state power estimate plus accumulated estimated energy persisted across restarts
- **Rate-Limit Protection**: HTTP 429 and the API's `Daily request limit exceeded` response pause all requests with bounded exponential backoff (120 s up to 1 h), persisted across restarts. Commands are refused (never queued or replayed) while paused; `Rate Limit Status` and `API Requests (UTC day)` diagnostics show the state
- **Safety Hardening**: Temperature clamp (20–70°C), retained-command rejection, bounded confirmation, HTTP timeouts and clean shutdown

## Entities Created

| Entity | Type | Description |
|---|---|---|
| `water_heater.<name>_boiler_<name>` | Water Heater | Mode and temperature control using confirmed device state |
| `binary_sensor.<name>_<name>_heating` | Binary Sensor | Actual element-active state derived from `WaterHeaterSts` |
| `sensor.<name>_<name>_average_temperature` | Sensor | `T_Avg` — device-reported tank temperature; do not interpret as a proven well-mixed whole-tank average |
| `sensor.<name>_<name>_desired_temperature` | Sensor | `T_dsrd` — desired temperature reported by the device |
| `sensor.<name>_<name>_boost_ceiling` | Sensor | `TBoost` telemetry; this is not a selectable Boost operating mode |
| `sensor.<name>_<name>_ambient_temperature` | Diagnostic Sensor | `TAmb` — installation-environment temperature |
| `sensor.<name>_<name>_eco_runtime` | Sensor | `Time_eco` — unit/reset behaviour remains unconfirmed |
| `sensor.<name>_<name>_programme_runtime` | Sensor | `Time_prog` — unit/reset behaviour remains unconfirmed |
| `sensor.<name>_<name>_wifi_signal` | Diagnostic Sensor | `Rssi` — device Wi-Fi signal strength in dBm |
| `sensor.<name>_<name>_water_heater_status_raw` | Diagnostic Sensor | Raw `WaterHeaterSts` bitmask |
| `sensor.<name>_<name>_last_polled` | Diagnostic Sensor | UTC timestamp of the latest successful real status poll |
| `sensor.<name>_<name>_power` | Sensor | Estimated element power: configured wattage while heating, otherwise 0 W; not a CT measurement |
| `sensor.<name>_<name>_energy_kwh` | Sensor | Accumulated estimated energy from heating-state integration; persisted across restarts |
| `sensor.<name>_<name>_poll_interval` | Diagnostic Sensor | Current bridge polling interval |
| `sensor.<name>_<name>_consecutive_poll_failures` | Diagnostic Sensor | Consecutive failed device-status polls |
| `sensor.<name>_<name>_last_successful_poll` | Diagnostic Sensor | UTC timestamp of the latest successful device-status poll |
| `sensor.<name>_<name>_element_wattage` | Diagnostic Sensor | Configured wattage used for estimated power and energy |
| `sensor.<name>_<name>_poll_status` | Diagnostic Sensor | Poll health: `ok` or `degraded` |
| `sensor.<name>_<name>_command_status` | Diagnostic Sensor | Latest bounded command result; independent MODE and TEMP details are exposed as attributes |

> **Note:** `<name>` is the device name set in the MyThermowatt app. For a device named `HWS`, entity IDs will be `water_heater.hws_boiler_hws`, `binary_sensor.hws_hws_heating`, and corresponding `sensor.hws_hws_*` entities.

## Rate limiting, diagnostics and handback visibility

The Thermowatt cloud reports quota exhaustion either as HTTP 429 or as HTTP 200 with the error `Daily request limit exceeded`. On detection the bridge stops **all** API traffic (polls, token refresh, commands) for 120 s, doubling per consecutive event up to 3600 s; the window survives restarts, including during startup login/discovery. Normal polling stays at 60 s, the command cooldown at 15 s and the confirmation window at 60 s.

- **Commands are never queued, and never re-sent after rate-limit recovery.** While a window is active a MODE/TEMP command is refused locally and recorded as `submit_failed` (`rate_limited: ...`). A command rejected by the API (including a 2xx body with the quota error or `success: false`) is `submit_failed`, never `pending`. After recovery nothing is re-sent; the next command must come from the caller.
- **Separate, pre-existing HTTP 401 behaviour (not part of rate-limit recovery).** If a request, including a command POST, receives HTTP 401, the bridge refreshes the token and re-sends the *same* request (same URL and body) **once, inside that same call**. A second 401, a failed refresh, or a rate limit during the refresh/replay ends the call (`submit_failed`); there is no loop, queue or later re-send, and a command is not repeated by polls or by recovery. It assumes a 401 means the first POST was not executed (vendor behaviour not verified). Both attempts count as requests attempted.
- **Diagnostics** (`P/<serial>/diagnostics`): `poll_status` is `rate_limited` from first detection until a good poll confirms recovery; `telemetry` = `{state, age_s, stale_after_s}`; `rate_limit` = `{active, retry_at, backoff_step, last_reason, last_at, events_total}`; `requests` = per-UTC-day counts of requests **attempted** (`status`, `command`, `auth`, `other`, `total`; counted before the transport result is known), `rate_limited_responses` (responses classified as limited, once each) and `blocked` (refused locally, not in `total`).
- **Counters are not a quota meter.** `requests` (per UTC calendar day) and `requests.rolling_24h` count only what *this bridge attempted* (plus rate-limited responses and locally blocked attempts). They are **not** a count of vendor-accepted requests: `rolling_24h.transport_failed` attempts ended in a timeout, reset, DNS or TLS error, so it is unknown whether the vendor received or counted them; `responses_received` (attempted minus transport failures) includes responses that were quota rejections (`rate_limited_responses`); the vendor's limit, reset rule and the mobile app's share of the quota are unknown. `rolling_24h` uses 15-minute buckets, so it can include up to 15 minutes of older requests (an upper bound), and `complete` is `false` until the ledger has been counting for 24 hours. The ledger is persisted with the counters (at most every 10 minutes and on shutdown), so a crash can lose up to 10 minutes of counts.
- **Quota-episode evidence** (`rate_limit.episodes`, newest 3 of the last 10). Each episode holds an immutable `first_rejection` snapshot (UTC-day counters, rolling-24 h counts and requests attempted since the last confirmed recovery, *including* the rejected request, which did receive a response) and, once a good poll confirms recovery, an immutable `recovery` snapshot (duration, rejected responses, requests attempted after the first rejection). They are written once per transition and persisted immediately. They contain counts and fixed labels only (`reason_class` is `http_429` or `quota_message`), never tokens, credentials, device identifiers or vendor response text. A limit that was already active before this ledger existed is recorded with `first_rejection: null` rather than an invented start. These figures describe what the bridge attempted; they are evidence for estimating the vendor limit, not the limit itself.
- **Proactive access-token refresh.** The access token lasts about 30 minutes. Instead of waiting for the 401 (three requests: failed `/status`, refresh, replayed `/status`), the poll thread refreshes once when the token has 120 s or less left (two requests including the poll). It never sends anything during a backoff window or while recovery is unverified, tries at most once per token and at most once per 10 minutes whatever the token says (a non-finite, absent or absurd `exp` is ignored), treats any failure as non-fatal (the reactive 401 path and re-login remain the fallback) and is never used for commands. **It is off by default** and is enabled with the add-on option `proactive_token_refresh: true` (Settings → Add-ons → Thermowatt Smart Boiler → Configuration), which `run.sh` turns into the bridge setting at startup; turning the option off and restarting the add-on is the kill switch. The effective setting is published as `proactive_token_refresh` in `P/<serial>/diagnostics` and logged at startup.
- **Estimated energy can under-count** after a prolonged telemetry outage: integration per poll gap is capped at twice the poll interval and the remainder is not back-filled.

**Identifying an outstanding physical handback** (e.g. 70 C boost confirmed, then rate limited, then the baseline write failed): the durable evidence is `input_boolean.ems_hws_ownership_active = on`, which only a confirmed handback clears. The bridge side shows `sensor.<name>_<name>_command_status = submit_failed` with attribute `TEMP.requested` (50 or the baseline) and `TEMP.error` starting `rate_limited`, plus `Rate Limit Status = limited` and `poll_status = rate_limited`. A restart clears the in-memory command record; ownership remains on, but see the retry limits below.

**Rate Limit Status lifecycle.** `limited` (backoff window running, no API traffic) -> `recovery_unverified` (window over, but no successful poll has confirmed recovery yet; `poll_status` stays `rate_limited`, so Package 19 stays fail-closed) -> `ok` (a poll that started after the last event succeeded; attribute `state` = `recovered`, `recovered_at` set). Before any event the attribute `state` is `none`.

**Ownership release (Package 19, unchanged).** Ownership is cleared by exactly two paths: a *confirmed baseline restoration*, or a recognised *native non-Manual mode* (Off/Eco/Auto/Holiday) taking authority at one of the script's two native-mode guards (no temperature command is issued). An unknown/unavailable/unrecognised mode is fail-closed: no command, ownership untouched. A rate limit therefore never clears ownership by itself.

**Recovery after an outage and the latched `command_status`.** A refused or rejected command leaves `command_status = submit_failed` until a *later command completes* (the summary shows the most recently completed command of either type) or the bridge restarts (the in-memory record is lost, state `idle`). Polling and rate-limit recovery do not clear it. Consequences: (1) while ownership is on, Package 19 does not consult `command_status` for acquisition; the outstanding handback is completed by the handback script's own confirmed TEMP commands, after which the summary becomes `confirmed`. (2) With ownership off (for example released by the native-mode path after a failed soft stop), `submit_failed` blocks *new acquisition* (which needs `idle`/`confirmed`) until the operator clears it by a deliberate command or an add-on restart. Neither is automated here, to avoid emitting physical commands.

**Handback retry is bounded, not recurring.** Package 19 retries a handback only on (a) the next EMHASS publication where the device is deselected and ownership is on, or (b) Home Assistant *start* (the restart-handback automation, one attempt per HA start). A bridge/add-on restart does not trigger it, and if EMHASS publication stops there is no recurring retry: ownership stays on until the operator runs `script.hws_thermowatt_handback` (or restarts HA).

## Installation

1. Install and start the **Mosquitto MQTT** broker add-on in Home Assistant.
2. Click **Add Repository** above, or manually add this repository URL to your HA Add-on Store:
   `https://github.com/MMicieli/ha-thermowatt-heater`
3. Install the **Thermowatt Smart Boiler** add-on.
4. Enter your MyThermowatt credentials in the **Configuration** tab.
5. Set `element_wattage` to the heater element rating if it differs from 3000 W.
6. Start the add-on and check the **Log** tab.

## Configuration

```yaml
email: "your-email@example.com"
password: "your-password"
element_wattage: 3000
```

`element_wattage` accepts an integer from 100 to 10000 W. Invalid values fall back to 3000 W with a warning. It affects estimated power and energy only.

### Optional environment variables

| Variable | Default | Description |
|---|---|---|
| `THERMOWATT_TLS_NO_VERIFY` | `0` | Set to `1` to disable TLS certificate verification (debug only) |
| `MQTT_HOST` | `core-mosquitto` | MQTT broker hostname |
| `MQTT_PORT` | `1883` | MQTT broker port |
| `MQTT_USER` | — | MQTT username if required |
| `MQTT_PASSWORD` | — | MQTT password if required |

## Polling and Command Confirmation

| Mode | Interval | When |
|---|---|---|
| Normal | 60s | Default operation |
| Post-command | 20s | Bounded 60s window after successful command submission |
| Backoff | 120s–180s | After a 429 rate-limit response |

A successful command response means the Thermowatt API accepted the submission; it does not prove the heater applied it. The bridge therefore:

- records MODE and TEMP independently as `pending`;
- wakes the polling loop so confirmation does not wait for the normal 60-second interval;
- confirms MODE only from raw `Cmd` and TEMP only from `T_SetPoint`;
- accepts only a status poll that started after submission;
- reports a fresh non-matching result as `mismatched` after the deadline;
- reports absence of a fresh status poll as `timed_out`;
- never retries or republishes a command automatically.

Operational `STATUS` and `last_polled_at` are never synthesised from a command request. They remain device-readback state suitable for Home Assistant orchestration and future EMHASS runtime accounting.

A poll counts as successful only when it returns usable device status — HTTP 200 alone is not enough. The Thermowatt cloud API can return HTTP 200 with a body such as `{"success": false, "error": "Water heater not found, check the Wi-Fi connection"}` when it cannot locate the device. That response is treated as a poll failure through the same consecutive-failure/degraded-threshold path as a non-200 response: it does not advance `last_successful_poll`, does not reset the failure counter, is not published to `STATUS`, does not accumulate energy, and is not used to confirm a pending MODE/TEMP command. Diagnostics expose the reason as `last_poll_error`, cleared on the next valid poll.

## Raw `Cmd` semantics

Raw `Cmd` appears to encode a mode-family value plus a low enabled bit: `family = Cmd & ~1`, `enabled = bool(Cmd & 1)`. A family with the enabled bit set reports the named mode; the same family with the enabled bit cleared is that family's inactive/Off sub-state. This is decoded once, bridge-side, by `decode_cmd()` and published as `ha_mode` in `STATUS` — the MQTT discovery `mode_state_template` is a straight passthrough of that field, so the bitfield logic exists in one place rather than being duplicated in Jinja.

Off is **not** exclusive to `Cmd=8`. Disabled values `8` (Manual family), `16` (Auto family) and `64` (Holiday family) have all been directly observed on-device and all resolve to Off. `Cmd=2` (Eco family, disabled) follows from the same bitfield contract but has **not** been captured in device history — it is an inferred value, not observed evidence. Home Assistant presents any recognised disabled family as `Off`; an unrecognised `Cmd` value (or an absent/malformed one) is reported as unknown, never defaulted to `Off` or to a stale prior mode. Raw `Cmd` remains available as a diagnostic attribute regardless of how `ha_mode` resolves it.

The `/off` command endpoint and payload are unchanged. Confirmation of an Off command now accepts a fresh readback whose `Cmd` belongs to any recognised family with the enabled bit cleared, rather than requiring an exact `Cmd=8` match — this reflects that the device's disabled-state raw value depends on which family was active beforehand.

A 2026-08-17 device-history anomaly (`Cmd=64`) appeared at a local-midnight boundary while Manual was active after an earlier Holiday period. Retained Holiday/date-boundary behaviour is a plausible hypothesis, but causation is not proven — treat it as unconfirmed background, not documented device behaviour.

## Safety Design

- Temperature commands are clamped to 20–70°C before reaching the API (`T_set_max: 70`)
- Retained MQTT command messages are ignored on restart to prevent stale command replay
- MODE and TEMP have independent cooldowns so a valid paired sequence is not cross-blocked
- All cloud API calls have a 5s connect and 15s read timeout
- Five consecutive status-poll failures mark operational device entities unavailable — including an HTTP-200 response that does not carry usable device status
- Bridge-level MQTT availability and per-device poll availability remain independent
- SIGTERM from Home Assistant Supervisor follows the clean shutdown path

## Healthy Boot Log

```text
--- BOOT SEQUENCE START ---
OK: Step 1 - Credentials present.
OK: Step 2 - MQTT TCP connection initiated.
OK: Step 3 - Logged in to Thermowatt backend.
OK: Step 4 - Found 1 thermostats.
🌉 Bridge active for: HWS (serial)
OK: Step 5 - Device discovery published. Element wattage: 3000 W
[MQTT] Connected — subscriptions restored, availability published online.
OK: Step 6 - Polling loop starting (normal=60s, confirm=20s, degraded_threshold=5).
[STATUS] Polled 5 times, 5 x 200, 0 errors, interval=60s
```

## Historical compatibility observations (not a current-version guarantee)

These versions were previously reported as working; they do not represent the latest Home Assistant release or certify all subsequent versions. For the current deployed EMS host/version, consult the separate main EMS repository's `docs/CURRENT_STATE.md`.

- **Home Assistant OS** — Core 2025.12.5, Supervisor 2026.01.1, OS 16.3, Frontend 20251203.3
- **Mosquitto MQTT** 6.5.2
- **MyThermowatt App** 3.14
- **Thermann** (Australian Reece brand) — confirmed working

_Tip: Help others by adding your version here if it works._

---

_Disclaimer: This project is not affiliated with or endorsed by Thermowatt or Ariston._
