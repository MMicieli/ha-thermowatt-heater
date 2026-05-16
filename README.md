# Thermowatt Smart Boiler Bridge for Home Assistant

This add-on bridges Thermowatt-based smart water heaters into Home Assistant via local MQTT. It polls the Thermowatt cloud API, publishes device state as MQTT Discovery entities, and routes HA commands back to the cloud — with safety hardening for use as an EMS-controlled deferrable load.

[![Open your Home Assistant instance and show the add add-on repository dialog with a specific repository URL pre-filled.](https://my.home-assistant.io/badges/supervisor_add_addon_repository.svg)](https://my.home-assistant.io/redirect/supervisor_add_addon_repository/?repository_url=https%3A%2F%2Fgithub.com%2FMMicieli%2Fha-thermowatt-heater)

## Features

- **Real-time Monitoring**: Tank temperature via `T_Avg` (firmware average — more accurate than display value)
- **Full Control**: Set target temperatures and operation modes (Manual / Eco / Auto / Holiday / Off)
- **MQTT Discovery**: Automatically creates a Water Heater device with 9 sensors + 1 binary sensor
- **EMS-ready Sensors**: Dedicated first-class entities for `T_Avg`, `T_dsrd`, `TBoost`, `TAmb`, `Time_eco`, `Time_prog`, `Rssi`, `WaterHeaterSts`, and `last_polled_at` — directly loggable to InfluxDB
- **Availability Tracking**: MQTT LWT publishes `offline` when bridge stops; all entities show unavailable in HA
- **Stale Data Protection**: `last_polled_at` timestamp enables HA-side stale detection
- **Power & Energy Monitoring**: Real-time 3 kW draw sensor + accumulated kWh counter (persisted across restarts). Both qualify for the HA Energy Dashboard
- **Safety Hardening**: Server-side temperature clamp (20–70°C), retained command rejection, HTTP timeouts, clean shutdown

## Entities Created

| Entity | Type | Description |
|---|---|---|
| `water_heater.<name>_boiler_<name>` | Water Heater | Mode and temperature control |
| `binary_sensor.<name>_<name>_heating` | Binary Sensor | Element active state |
| `sensor.<name>_<name>_average_temperature` | Sensor | T_Avg — firmware average tank temp (more accurate than display value) |
| `sensor.<name>_<name>_desired_temperature` | Sensor | T_dsrd — current target setpoint |
| `sensor.<name>_<name>_boost_ceiling` | Sensor | TBoost — maximum boost temperature |
| `sensor.<name>_<name>_ambient_temperature` | Sensor | TAmb — installation environment temp (diagnostic) |
| `sensor.<name>_<name>_eco_runtime` | Sensor | Time_eco — eco mode runtime counter (unit/reset behaviour unconfirmed) |
| `sensor.<name>_<name>_programme_runtime` | Sensor | Time_prog — programme runtime counter (unit/reset behaviour unconfirmed) |
| `sensor.<name>_<name>_wifi_signal` | Sensor | Rssi — WiFi signal strength in dBm (diagnostic) |
| `sensor.<name>_<name>_water_heater_status_raw` | Sensor | WaterHeaterSts — raw status bitmask (diagnostic) |
| `sensor.<name>_<name>_last_polled` | Sensor | last_polled_at — UTC timestamp of last successful poll (diagnostic) |
| `sensor.<name>_<name>_power` | Sensor | Real-time power draw — 3000 W when heating, 0 W otherwise. Qualifies for HA Energy Dashboard |
| `sensor.<name>_<name>_energy_kwh` | Sensor | Accumulated energy (kWh) — bridge-side integration, persisted across restarts. `state_class: total_increasing` |

> **Note:** `<name>` is the device name set in the MyThermowatt app. For a device named `HWS`, entity IDs will be `water_heater.hws_boiler_hws`, `sensor.hws_hws_average_temperature`, etc.

## Installation

1. Install and start the **Mosquitto MQTT** broker add-on in Home Assistant.
2. Click **Add Repository** above, or manually add this repository URL to your HA Add-on Store:
   `https://github.com/MMicieli/ha-thermowatt-heater`
3. Install the **Thermowatt Smart Boiler** add-on.
4. Enter your MyThermowatt credentials in the **Configuration** tab.
5. Start the add-on and check the **Log** tab.

## Configuration

```yaml
email: "your-email@example.com"
password: "your-password"
```

### Optional environment variables

| Variable | Default | Description |
|---|---|---|
| `THERMOWATT_TLS_NO_VERIFY` | `0` | Set to `1` to disable TLS certificate verification (debug only) |
| `MQTT_HOST` | `core-mosquitto` | MQTT broker hostname |
| `MQTT_PORT` | `1883` | MQTT broker port |
| `MQTT_USER` | — | MQTT username if required |
| `MQTT_PASSWORD` | — | MQTT password if required |

## Polling Behaviour

| Mode | Interval | When |
|---|---|---|
| Normal | 60s | Default operation |
| Post-command | 20s | 60s window after a command is sent |
| Backoff | 120s–180s | After a 429 rate-limit response |

## Safety Design

- Temperature commands are clamped to 20–70°C server-side before reaching the API (firmware `T_set_max: 70`)
- Retained MQTT command messages are ignored on restart — prevents stale command replay
- All cloud API calls have a (5s connect, 15s read) timeout — prevents hung requests freezing polling
- Bridge publishes `offline` to availability topic on both clean shutdown and unclean disconnect (LWT)
- SIGTERM from HA Supervisor triggers clean shutdown path

## Healthy Boot Log

```
--- BOOT SEQUENCE START ---
OK: Step 1 - Credentials present.
OK: Step 2 - MQTT TCP connection initiated.
OK: Step 3 - Logged in to Thermowatt backend.
OK: Step 4 - Found 1 thermostats.
🌉 Bridge active for: HWS (4032429241482944)
OK: Step 5 - Device discovery published.
[MQTT] Connected — subscriptions restored, availability published online.
OK: Step 6 - Polling loop starting (normal=60s, confirm=20s).
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

