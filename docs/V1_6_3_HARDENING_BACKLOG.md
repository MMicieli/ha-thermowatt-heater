# v1.6.3 Hardening Backlog

Future-consideration backlog after Thermowatt bridge v1.6.2.

This file is not implementation approval. It records candidate hardening work only.

Current posture:

- Thermowatt bridge v1.6.2 is deployed and runtime healthy.
- HA EMS wrapper/gate layer is active.
- Monitoring is authorised.
- Manual/staged HWS calibration planning is authorised.
- Autonomous EMHASS HWS dispatch is **not authorised**.

## Candidate items

### P1 — Configurable element wattage

Current bridge behaviour assumes `3000 W` for `sensor.hws_hws_power` and bridge-side `sensor.hws_hws_energy_kwh` integration.

Future option:

- Add add-on option/env setting such as `element_watts`, default `3000`.
- Use it consistently for MQTT power discovery and energy integration.
- Validate a conservative sane range.
- Preserve existing default behaviour.

Rationale: EMS calibration should match measured HWS element draw rather than a hardcoded assumption.

### P1 — Sustained poll-failure degraded/offline handling

Current bridge behaviour publishes `offline` on clean shutdown and relies on MQTT LWT for unclean disconnects. Repeated cloud/API poll failures are counted/logged but do not necessarily mark the bridge unavailable.

Future option:

- Track consecutive poll failures.
- Publish degraded/offline diagnostic state after a conservative threshold.
- Recover only after successful status polling resumes.

Rationale: avoid stale retained MQTT state appearing healthy during sustained cloud/API failure.

### P1 — Per-command-type cooldown

Current cooldown is per device. A paired `MODE` + `TEMP` sequence can be dropped if issued too quickly. HA currently mitigates this with staged 15-second spacing.

Future option:

- Split cooldown by command type, e.g. `TEMP` and `MODE`.
- Or add an explicit controlled paired-command path.
- Preserve retained-command rejection and anti-spam protection.

Rationale: safer staged control without accidental paired-command loss.

### P2 — Bridge diagnostic MQTT sensors

Future option: publish Home Assistant MQTT discovery sensors for bridge diagnostics, such as:

- Current poll interval
- Consecutive poll failures
- Poll success/error count window
- Last successful poll timestamp
- Rate-limit backoff level
- Last HTTP status

Rationale: make bridge health visible in HA without parsing add-on logs.

### P2 — Unknown `Cmd` handling

Current water-heater mode template maps unrecognised `Cmd` values to `Off`.

Future option:

- Avoid hiding unknown vendor modes as `Off`.
- Preserve Home Assistant water_heater compatibility.
- Use raw/diagnostic entities for unknown mode visibility.

Rationale: semantically unknown device state should not be presented as confirmed Off.

### P2 — Atomic config writes

Current `_config_lock` protects config writes, but `_save_config()` writes directly to the target JSON file.

Future option:

- Write to a temporary file.
- Flush/fsync if practical.
- Atomically rename into place.
- Preserve backwards-compatible JSON structure.

Rationale: reduce risk of partial/corrupt config if the add-on stops during write.

### P3 — API key environment override

Current vendor app API key is hardcoded in code.

Future option:

- Allow env/add-on option override while retaining the current default.

Rationale: improves maintainability if the vendor key changes. This is lower priority because it is a vendor app key, not a user secret.

## Explicit non-goals

Do not add these without a separate safety review:

- Schedule/program editing through undocumented endpoints.
- Faster normal polling that increases cloud rate-limit risk.
- Autonomous EMHASS dispatch logic inside the bridge.
- Sigenergy, Modbus, inverter, or battery writes.
- Any change that bypasses HA EMS gates.

## Acceptance bar for any future PR

Any future PR should:

- Be branch-based, not direct-to-main, where practical.
- Preserve existing safe defaults.
- Pass Python syntax checks.
- Pass add-on runtime boot/log verification.
- Keep bridge as a narrow cloud-to-MQTT adapter.
- Leave EMS optimisation and dispatch gating in Home Assistant / EMHASS.

## Current recommendation

Do not implement this backlog until after P3-02 HWS runtime calibration is complete.

For the EMS safety case, better telemetry and fail-closed behaviour are more valuable than expanding Thermowatt cloud control features.
