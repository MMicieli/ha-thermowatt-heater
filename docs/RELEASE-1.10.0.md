# Thermowatt 1.10.0 — supervised manual release with Auto update ON

**Status: draft release candidate. Not merged or installed.** PR #25 Stage 3 was merged to `main` at `93bc67d5cc4ff977cf5db632a44eee117ae1c2b2`. Nine stubbed Docker scenarios passed; the real cloud and physical heater have not been commissioned for Stage 3.

The release changes the add-on version from **1.9.0 to 1.10.0**. The new `demand_aware_polling` option remains **false** and the installed `poll_interval: 180` must be preserved.

## Automatic-update constraint: do not rely on breaking_versions for the first upgrade

**Operator policy: Auto update stays ON. Do not disable it.**

The Supervisor 2026.11.0 `auto_update_available` check reads `breaking_versions` from the **installed app's stored configuration**, not from the new store candidate. Installed Thermowatt 1.9.0 has no such marker. Therefore adding `breaking_versions: ["1.10.0"]` only to the proposed 1.10.0 config would **not block the first automatic upgrade**. The ineffective marker has been removed from this release candidate.

Supervisor has a one-day age condition before its automatic-update task installs a newly recognised version. **This is a short operational window, not a guaranteed restart interlock.** Once the new version is visible in the store, leaving it uninstalled risks an unattended restart, even while a heater handback is in progress. Reverting `main` may not immediately invalidate a cached or already scheduled update. **Do not merge this release PR unless the operator can manually complete the installation and verify the new version in the same supervised session.**

Future releases with Auto update ON may restart the bridge automatically. Their safety must be reviewed separately; a marker added only to the future version is not a reliable safeguard for an already installed predecessor.

## Mandatory fresh live preflight — immediately before release and again before installation

Read current Home Assistant states and add-on diagnostics (do not rely on an earlier inspection).

1. `input_boolean.ems_hws_ownership_active` is **off**; otherwise stop and allow a confirmed normal handback.
2. `script.hws_thermowatt_handback` and the translation / precycle automations are idle, and `sensor.p_deferrable0_state` is **off**. Work outside an active five-minute HWS cycle.
3. `sensor.outside_hws_hws_command_status` is `idle` or `confirmed`, with no pending MODE/TEMP command or confirmation window.
4. `water_heater.hws_boiler_hws` reports the normal native **Manual** mode and baseline setpoint matching `input_number.ems_hws_captured_baseline_setpoint_c` where available (58 °C at the 2026-11-17 observation; do not assume this cannot change).
5. Poll status is **ok**, consecutive failures are zero, last validated device poll is fresh, rate-limit backoff is inactive and recovery is verified.
6. Record the installed add-on version, options and request totals. Keep **Auto update ON**, `poll_interval: 180`, `proactive_token_refresh: false` and `demand_aware_polling: false`. Verify an available, completed and recoverable backup containing the add-on. Prefer a fresh backup just before installation.
7. Check recent UTC-day API request count and historical quota onset. The observed first-rejection count of 469 in three episodes is **not** a contract; do not install near a likely quota window.

If a critical value is unavailable, stale or unsafe, **NO-GO**. Never restart an owned or unconfirmed handback.

## Controlled same-session release and installation

1. Have the manual installation operator and read-only HA supervisor/diagnostic access ready. Complete the preflight and obtain explicit approval for the live add-on update.
2. Take a fresh Thermowatt-inclusive backup, verify it completed and record its ID and the current add-on options. Restore is untested; do not claim otherwise.
3. Only now merge the version bump to `main` and refresh the Home Assistant add-on store listing. Do not leave the update published unattended.
4. **Repeat the full live preflight immediately before initiating the manual update.** If it fails, stop and investigate the published update risk. Do not rely solely on reverting the repository to protect the running system.
5. Install **1.10.0 manually** with Auto update ON and Stage 3 option **OFF**. Preserve existing options; the update recreates/restarts the add-on container and is not a no-op.
6. Verify installed version 1.10.0, effective `demand-aware polling: 0`, a successful real status poll, HA entity availability within six minutes, unchanged ownership OFF and baseline, zero unexpected MODE/TEMP commands, and no new quota backoff.
7. Observe at least one hour with the existing 180 s cadence and measure the API request rate, errors, stale telemetry, and mode/setpoint stability.
8. A separate approved stage may deploy Package 19's `precycle` marker and enable demand-aware polling under supervision **with HWS ownership OFF for at least 48 hours**. Automatic ownership / boosts remain unapproved until acquisition-gate, quota and real handback checks pass.

## Stops, rollback, and open risks

Stop for ownership ON, an active or unresolved handback, unexpected commands/setpoints, stale or unvalidated polling, quota backoff, or abnormal restarts. Never restart to fix an owned heater without the Package 19 recovery runbook. Disabling the option is the preferred configuration rollback **only after a safe restart preflight**; downgrading an add-on image may not be supported, and a roll-forward release or tested backup restore may be needed.

If the release is merged but manual installation cannot proceed, investigate promptly while the installed state is still safe. Reverting the commit may reduce update exposure, but **is not a verified way to cancel an update already detected or scheduled by Supervisor**.

This is an installation plan, **not permission to merge or deploy unattended**. See `docs/STAGE3-DEMAND-AWARE-POLLING.md` §9 for Stage 3 commissioning thresholds.
