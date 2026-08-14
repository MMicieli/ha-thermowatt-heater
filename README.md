# Thermowatt Smart Boiler Bridge for Home Assistant

This add-on bridges Thermowatt-based smart water heaters into Home Assistant via local MQTT. It polls the Thermowatt cloud API, publishes device state as MQTT Discovery entities, and routes HA commands back to the cloud — with safety hardening for use as an EMS-controlled deferrable load.

[![Open your Home Assistant instance and show the add add-on repository dialog with a specific repository URL pre-filled.](https://my.home-assistant.io/badges/supervisor_add_addon_repository.svg)](https://my.home-assistant.io/redirect/supervisor_add_addon_repository/?repository_url=https%3A%2F%2Fgithub.com%2FMMicieli%2Fha-thermowatt-heater)

## Features

- **Real-time Monitoring**: Device-reported tank temperature via `T_Avg`. Do not assume this is a well-mixed whole-tank average for thermal modelling; the verified Thermann Smart Electric installation documented by this project uses bottom-of-tank temperature sensing.
- **Full Control**: Set target temperatures and operation modes (Manual / Eco / Auto / Holiday / Off)
- **MQTT Discovery**: Automatically creates the water-heater entity, heating-state binary sensor, operational telemetry and bridge diagnostics
- **EMS-ready Sensors**: Dedicated first-class entities for actual heating state, temperatures, mode, setpoint, poll freshness and estimated element power
- **Availability Tracking**: Operational entities require both bridge connectivity and healthy device polling; diagnostic entities remain visible while device polling is degraded
- **Stale Data Protection**: `last_polled_at` advances only after a successful device-status poll
- **Command Confirmation**: HTTP success means submitted, not applied; MODE and TEMP remain pending until confirmed by newer device readback
- **Power & Energy Monitoring**: Configurable heating-state power estimate plus accumulated estimated energy persisted across restarts
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

## Known to Work On

- **Home Assistant OS** — Core 2025.12.5, Supervisor 2026.01.1, OS 16.3, Frontend 20251203.3
- **Mosquitto MQTT** 6.5.2
- **MyThermowatt App** 3.14
- **Thermann** (Australian Reece brand) — confirmed working

_Tip: Help others by adding your version here if it works._

---

_Disclaimer: This project is not affiliated with or endorsed by Thermowatt or Ariston._
