# Changelog
All notable changes to this project will be documented in this file.
The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

> **Fork note:** This repository is forked from [waterheater-dev/ha-thermowatt-heater](https://github.com/waterheater-dev/ha-thermowatt-heater) at v1.3.0. Versions 1.0.0–1.3.0 reflect upstream history. Changes from v1.4.0 onwards are specific to this fork.

##[1.6.1] - 2026-05-16
 ### Added            
+- `_inject_fake_status` heating flag no longer incorrectly re-applies the stale cached `WaterHeaterSts` value for mode-only commands. Previously, the bitmask recomputation always ran regardless of whether `WaterHeate
          +rSts` was in the overrides — meaning mode changes (Eco, Auto, Manual, Holiday, Off) left `heating` reflecting the pre-command poll value for up to 20 seconds.                                                          
       11 +- Off command now explicitly passes `heating=False` so `sensor.hws_hws_power` and `binary_sensor.hws_heating` immediately show 0 W / off after a successful Off command.   

##[1.6.0] - 2026-05-16
 ### Added                                                                                                                                                                                                                           
  - Power sensor (sensor.hws_hws_power) — MQTT discovery on P/{serial}/STATUS, publishes 3000 W when heating, 0 W otherwise. device_class: power, state_class: measurement.                                                               
  - Energy sensor (sensor.hws_hws_energy_kwh) — separate topic P/{serial}/energy_kwh. Bridge accumulates 3 kW × elapsed hours each poll cycle, persisted in config.json across restarts. state_class: total_increasing — qualifies for the
   HA Energy Dashboard directly.                                                                                                                                                                                                          
                                                                                                                                                                                                                                          
  ### Fixed                                                                                                                                                                                                                           
  - on_connect callback added — re-subscribes to all P/{serial}/CMD/# topics and re-publishes "online" on every (re)connect. Without this, MQTT reconnects silently lost all command subscriptions.                                       
  - on_disconnect callback added for logging.                                                                                                                                                                                             
  - Callbacks registered before connect() so they fire on the initial connection too.                                                                                                                                                     
  - STATUS topic publishes changed to QoS=1 (was 0) in both poll_status and _inject_fake_status.                                                                                                                                       
  - 429 handling: break → continue so remaining devices still get polled after one device hits a rate limit.                                                                                                                              
  - threading.Lock (_config_lock) protects self.config for concurrent reads/writes between the main poll thread and the MQTT callback thread.  

##[1.5.3] - 2026-05-09
### Fixed
- `Time_prog` state_class corrected from `measurement` to `total_increasing` — confirmed lifetime accumulating counter (observed delta 282 min over single day)

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
