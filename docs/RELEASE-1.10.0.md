# Thermowatt 1.10.0 — staged release and controlled installation

**Status: release candidate only. Do not merge this release branch or install the add-on until a contemporaneous live Home Assistant preflight has passed.** PR #25 Stage 3 runtime is already merged in `main` at `93bc67d5cc4ff977cf5db632a44eee117ae1c2b2`. Stage 3 was qualified using stubbed Docker services (scenarios A, B, C, D, F, G, H, S, S0). The real Thermowatt cloud and physical heater have **not** been qualified for Stage 3.

The add-on version changes from 1.9.0 to **1.10.0**, with `demand_aware_polling: false`. **The operator has chosen to keep Home Assistant's add-on Auto update ON. Do not disable it as part of this rollout.** The `breaking_versions` entry intentionally blocks unattended installation across 1.10.0 so that the heater's physical-control state can be checked before the Supervisor recreates the container. This is a manual-install safety safeguard, not a claim that the default-off feature is a backwards-incompatible runtime change.

**Future-release limitation:** Once 1.10.0 has been manually installed, Auto update remains enabled. A later release without a new intervening `breaking_versions` entry may therefore restart the add-on automatically, potentially during an owned boost or handback. Each future version must be reviewed for safe automatic restart or carry its own breaking-version/manual-update requirement; the 1.10.0 marker is not a permanent restart interlock.

## Mandatory live preflight — immediately before any update, option change or restart

Inspect live Home Assistant state *at the time of the operation*:

1. `input_boolean.ems_hws_ownership_active` is **off**. If **on**, stop; permit normal confirmed handback. Never restart the bridge during an outstanding owned boost.
2. `script.hws_thermowatt_handback` is **off**, the HWS translation automation is idle (not mid-cycle), and `sensor.p_deferrable0_state` is **off**.
3. `sensor.outside_hws_hws_command_status` is `idle` or `confirmed`, with no pending MODE/TEMP command and no 60-second confirmation window.
4. `water_heater.hws_boiler_hws` reports the expected native Manual mode and physical baseline setpoint; compare against `input_number.ems_hws_captured_baseline_setpoint_c` if present. Do not assume a particular temperature.
5. `sensor.outside_hws_hws_poll_status` is healthy, failures are zero, the most recent real poll is fresh and there is no active or unverified rate-limit recovery.
6. Record the add-on's **currently installed version**, full options, relevant HWS entity states, diagnostics, and a restorable backup. Leave Auto update enabled as requested. Before merging the 1.10.0 release, verify that Supervisor recognises its breaking-version boundary and will require manual installation; if this cannot be verified, do not publish the release.

If any condition is unknown, unavailable or false, **do not update or restart**. Do not attempt to fix a stuck handback by restarting the bridge.

## Controlled release / installation

1. Review and approve this release candidate separately from PR #25. Ensure the version bump is the only runtime packaging change. Do not merge or expose the update until the manual-install safeguard and HA state have been confirmed.
2. Merge the release PR under operator control, refresh the add-on repository listing, and manually install the 1.10.0 update only after repeating the preflight immediately before installation. Installation replaces/restarts the add-on container; it is **not** a no-op.
3. Preserve existing options and keep `demand_aware_polling: false`. Do not introduce the Package 19 precycle marker or acquisition budget gate during the initial update.
4. After installation, verify the actual installed version is 1.10.0 and the effective log reports `demand-aware polling: 0`. Confirm one healthy post-start real status poll, correct discovered entities, ownership still off, baseline intact, no unexpected MODE/TEMP commands, and availability online within six minutes.
5. Observe the default-OFF bridge for at least one hour. The current operating cadence and API rate should match the existing options; do not assume 60-second polling if the installed `poll_interval` differs.
6. Only in a separately approved commissioning stage: apply the Package 19 `precycle` marker, then repeat the full restart preflight, switch the demand-aware option on, and run an **ownership-OFF supervised trial for at least 48 hours** against the telemetry freshness, request budget and offline thresholds documented in `STAGE3-DEMAND-AWARE-POLLING.md` §9.
7. **Do not enable automatic HWS ownership** until the separate acquisition-gate policy, quota risk, real handback tests and explicit operator approval are complete.

## Stop and rollback rules

Stop commissioning for stale telemetry outside quota backoff, a failed or pending handback, unexpected commands or setpoints, unexplained offline status, quota onset during an owned boost, or inconsistent ownership state. Only change options or restart **after a fresh successful restart preflight**. Returning `demand_aware_polling` to false is the first safe configuration rollback; the prior image may not be available as a downgrade and may require a new roll-forward release.

See `docs/STAGE3-DEMAND-AWARE-POLLING.md` §§5, 6 and 9 for measured-model limitations, safety gates and monitoring thresholds. Neither this branch nor its existence constitutes permission to install or change the running heater.
