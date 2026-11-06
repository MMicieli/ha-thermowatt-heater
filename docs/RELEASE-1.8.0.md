# Thermowatt Smart Boiler add-on 1.8.0 — release candidate

Status: **release candidate, not released.** PR #16 (`fix/rate-limit-reliability`). Nothing in this document has been
executed against the production Home Assistant, the installed add-on or the live HWS. Merging, tagging, updating the
installed add-on and every live HWS command require explicit operator approval at the time.

## 1. Release notes

**Why:** the Thermowatt cloud signals quota exhaustion as HTTP 429 or as HTTP 200 `{"success": false, "error": "Daily
request limit exceeded"}`. The 1.7.3 bridge only handled 429 (with a short, unpersisted backoff), so the HTTP 200 form
kept polling every 60 s (20 s after commands) against an exhausted quota, and a command answered with that body was
recorded as `pending`.

**What changes for operators**
- All API traffic (polls, token refresh, commands) stops during a bounded exponential backoff: 120, 240, 480, 960, 1920
  s, capped at 3600 s. It survives add-on restarts, including during startup login/discovery. Normal polling stays at
  60 s; command cooldown 15 s; confirmation window 60 s; fresh-readback-only confirmation.
- Commands are **never queued and never re-sent after rate-limit recovery.** While limited they are refused locally
  (`command_status = submit_failed`, error `rate_limited ...`). A command rejected by the API, including a 2xx body with
  the quota error or `success:false`, is `submit_failed`, never `pending`.
- Diagnostics: `poll_status = rate_limited` from first detection until a good poll confirms recovery; new
  `Rate Limit Status` sensor (`limited` -> `recovery_unverified` -> `ok`) and `API Requests (UTC day)` sensor; extra
  fields in `P/<serial>/diagnostics` (`rate_limit`, `requests`, `telemetry`, per-command `confirm_latency_s`).
- Package 19's freshness inputs are unchanged and fail closed (`poll_status != ok` and failures >= 1 from the first
  detection). No entity availability change.
- Dockerfile pins Python dependencies exactly (below).

**Known limitations (documented in README):** request counters are per UTC calendar day, not a rolling 24 h quota
meter; estimated HWS energy can under-count after a prolonged telemetry outage (per-gap integration cap of 2 x poll
interval, no back-fill); a latched `submit_failed` clears only on a later completed command or a bridge restart.

**Version:** `1.7.3 -> 1.8.0` (minor: additive entities/diagnostics and behaviour fix; no config-schema change, no
required user action).

## 2. Validation evidence (head `2acac09` + release-prep commit)

| Check | Result |
|---|---|
| Base image | `ghcr.io/home-assistant/amd64-base:3.20` (digest `sha256:e708939c…55bd20`), Alpine Linux, Python **3.12.13** |
| Image build | amd64 OK; **aarch64 and armv7 also built (QEMU) and import-checked** (`config.yaml` arch list) |
| Exact dependencies (all three arch) | paho-mqtt **2.2.0**, requests **2.34.2**, urllib3 **2.8.0**, certifi 2026.10.23, idna 3.20, charset-normalizer 3.5.2 — now pinned in the Dockerfile; resolved list identical to the previously validated range-resolved build |
| Test suite inside the Alpine image | **314 passed** (incl. verbatim Package 19 template check) |
| Test suite on host | 314 passed on Python 3.11.15, 3.12.0, 3.13.15 |
| Stubbed e2e, scenario A (real Mosquitto 2, TLS verification ON via private CA, stub API) | 60 s polls at t=0/60; HTTP-200 quota from t=90: poll at 120 s limited (window 120 s), poll at 240 s limited (window 240 s), **no API request between 120-240 and 240-480 s**; Off at t=150 and TEMP 50 at t=300 **refused locally, never sent**; recovery poll at 480 s; Eco at t=520 accepted, POST sent once, confirmed; exactly 1 login; clean SIGTERM exit code 0 |
| Diagnostics lifecycle observed on the broker (A) | `none/ok` -> `active/rate_limited/rate_limited` (failures 1) -> `recovery_unverified` at window end (still `rate_limited`) -> `active` step 2 -> `recovery_unverified` -> `recovered/ok/fresh`; request counters sent 9, rate-limited responses 2, blocked 2 |
| Stubbed e2e, scenario B (HTTP 429, restart inside window) | Persisted `rate_limit` step 1 written at SIGTERM; restarted container logged `Persisted backoff active — waiting 69s before any API request`; login at t=180 limited again -> `Step 3 deferred` (240 s) **without exiting** (RestartCount 0); login/discovery succeeded at t=420; recovery cleared on the first good poll |
| **Final smoke via the real entrypoint** (image `ENTRYPOINT /init` + `CMD /run.sh`, bashio, no `--entrypoint` override) | Stub Supervisor served `/addons/self/options/config` and `/services/mqtt` (bearer-token checked); Mosquitto with `allow_anonymous false` + password file; TLS verification on. Scenario A (HTTP-200 quota) **PASS**: 1 login, polls at 0/60, limited at 120 and 240 s, next request exactly at the 120 s / 240 s backoff, 2 commands refused locally, recovery poll at 480 s, one `POST /eco` at 520 s, states `none -> active -> recovery_unverified -> active -> recovery_unverified -> recovered`, exit 0. Scenario B (HTTP 429 + container restart in window) **PASS**: persisted step 1, second start waited 69 s, deferred login at 180 s without exiting, login at 420 s, RestartCount 0, exit 0. Logs scrubbed of credentials/e-mail; verified secret-free |
| Harness assertions | `tests/e2e/verify_e2e.py` returns non-zero on unexpected API paths/calls, calls inside a window, wrong backoff spacing, extra/replayed command POSTs, re-login on quota, missing recovery transitions, wrong counters, leaked secrets, bad exit codes; 28 unit tests inject each violation (`tests/test_e2e_verifier.py`); live tamper check returned exit 1 |
| Not covered | Real Supervisor/HA; real Thermowatt cloud; real HWS |

Harness: `thermowatt-heater/tests/e2e/` (`build_image.sh`, then `run_e2e.sh A|B`, ~10 min each, exit status = verdict).

**Finding from the smoke test (no production impact):** building the image from a Windows working tree with
`core.autocrlf=true` gives `run.sh` CRLF endings and the container fails with `exec: fatal: unable to exec bashio`. The
committed blob has LF endings (verified) and the Supervisor builds from a Linux clone, so production is unaffected;
`git archive` also applies autocrlf, so `build_image.sh` builds from the raw blobs, and a root `.gitattributes` now pins
`*.sh`, `Dockerfile` and `run.sh` to LF. Cosmetic note from the run: the poll warning
reads `invalid device status payload (HTTP 200): rate limited: ...` (wording only).

### Rollback compatibility (checked)
- The new version adds only `rate_limit` and `request_counters` keys to `/data/thermowatt_config.json`. Verified by script:
  1.7.3 loads a 1.8.0-written file, preserves `devices`/`energy_kwh`, and carries the extra keys harmlessly; a 1.7.3-written
  file (no new keys) loads in 1.8.0 with backoff 0. Energy accumulator and device config are compatible both ways.
- Rolling back leaves two **orphaned retained MQTT discovery configs** (`.../rate_limit_status/config`,
  `.../api_requests/config`); clear them (below). Retained `diagnostics` is overwritten by the old bridge on its first poll.
- No entity unique_id changed; existing entities, `command_status` and the Package 19 inputs keep their names/shape.

## 3. Supervised production commissioning checklist (operator-run)

Roles: **Operator** (executes), **Reviewer** (independent second pair of eyes). Record every value below with a
timestamp. Abort and roll back at any red flag marked **STOP**. No step requires a physical HWS command unless the
optional step 3.4 is separately approved.

### 3.0 Preconditions (do not update unless all true)
- [ ] PR #16 merged to fork `main` by explicit approval; `config.yaml` version is `1.8.0`; release commit SHA recorded.
- [ ] `input_boolean.ems_hws_ownership_active` is **off** (no outstanding handback). **STOP** if on: complete the handback
      first (or let EMHASS deselect and the script confirm baseline) — do not update with a handback outstanding.
- [ ] `input_number.ems_hws_captured_baseline_setpoint_c` recorded.
- [ ] No EMHASS boost scheduled in the next window; EMHASS publication healthy.
- [ ] `sensor.outside_hws_hws_command_status` is `idle` or `confirmed`; `poll_status = ok`; consecutive failures 0;
      `last_successful_poll` < 120 s old.
- [ ] Quota not currently exhausted (no `rate_limit.state` other than absent/`none`; poll succeeding).

### 3.1 Pre-update capture (read-only)
- [ ] Physical HWS state as seen in the **Thermowatt app** and in HA: mode (`water_heater.hws_boiler_hws`), setpoint
      (`temperature`), `T_Avg`, heating on/off. Screenshot the app. Record `Cmd`/`WaterHeaterSts` from
      `P/<serial>/STATUS`.
- [ ] Record `sensor.*_energy_kwh`, `Last Polled`, add-on version (1.7.3) and image id.
- [ ] Take a Supervisor **partial backup including the add-on and its data**; confirm it lists add-on `thermowatt_boiler`
      1.7.3. This is the primary rollback artefact.
- [ ] Save the retained discovery list: `mosquitto_sub -v -t 'homeassistant/+/<serial>/#' -W 3`.
- [ ] Note the git SHA of the 1.7.3 code (`b01a3e0`) for a revert-based rollback.

### 3.2 Deployment (operator approval required at this step)
- [ ] Update the add-on to 1.8.0 from the Supervisor (rebuilds from the Dockerfile; confirm the build log shows the pinned
      versions and no pip errors). Expect a short telemetry gap.
- [ ] Start; read the add-on log: Steps 1-6 OK, `Polling loop starting (normal=60s ...)`, no `FAILED`, no TLS warning.
- [ ] **Physical no-command check:** HWS mode/setpoint in the app and HA unchanged from 3.1; `command_status = idle`;
      no `[CMD]` lines in the log. **STOP** if anything changed.

### 3.3 Post-update verification (first 15 minutes)
- [ ] Existing entities back and unchanged names; `Last Polled` advancing every ~60 s; `poll_status = ok`, failures 0.
- [ ] New diagnostics present: `Rate Limit Status = ok`, attribute `state = none`; `API Requests (UTC day)` increasing
      about once a minute with `rate_limited_responses = 0`, `blocked = 0`.
- [ ] Energy sensor continued from the 3.1 value (persisted), no reset or jump.
- [ ] Package 19 inputs: `binary_sensor.ems_hws_current_action_available` behaves as before (compare to 3.1 for the same
      physical state); ownership still off; baseline helper unchanged.
- [ ] Watch for an EMHASS cycle: no unexpected acquisition while telemetry is fresh; if EMHASS selects HWS, follow the
      normal supervised path.

### 3.4 Optional supervised functional check (needs separate explicit approval; not required for release)
Single deliberate TEMP round trip by the operator through the existing HA entity, then restore to the recorded baseline:
verify `command_status` goes `pending -> confirmed` with `confirm_latency_s` recorded and the setpoint matches. Do
**not** attempt to induce a real rate limit in production; the quota behaviour is covered by the stub e2e.

### 3.5 If a real rate limit occurs after deployment
Expected: `Rate Limit Status = limited` then `recovery_unverified` then `ok`; `poll_status = rate_limited`;
log `[RATE LIMIT] ... backing off Ns`; no API traffic inside the window. Verify:
- [ ] Ownership/handback: if `ems_hws_ownership_active` is on, the physical state is **not** assumed — check the app.
      A failed handback leaves ownership on and `command_status = submit_failed` (`TEMP.error = rate_limited ...`).
- [ ] No command was queued: after recovery the HWS setpoint equals what it was before recovery (app check).
- [ ] After recovery (`Rate Limit Status = ok`), retry the handback only through Package 19 (next EMHASS deselected
      publish or HA start) or, if EMHASS publication has stopped, by the operator running
      `script.hws_thermowatt_handback`; confirm `TEMP ... confirmed` and ownership off.
- [ ] Latched `submit_failed` with ownership off blocks new acquisition until a later command completes or the add-on is
      restarted (restart sends no command). See follow-up issue.
- [ ] Record `API Requests` and the time of first/last limited response for the quota-headroom follow-up.

### 3.6 Rollback
Triggers: any **STOP** above; entities lost/renamed; polling not recovering; unexpected commands; energy reset.
1. Preferred: restore the 3.1 Supervisor partial backup (add-on 1.7.3 + its `/data`). If the energy counter advanced
   meanwhile, accept the backup's value or edit afterwards; note the gap.
2. Alternative: revert the release on the fork `main` with a **new higher version** (e.g. 1.8.1 containing 1.7.3 code),
   update the add-on. (Do not rely on the Supervisor offering a lower version number.)
3. Clear orphaned discovery topics: publish empty retained payloads to
   `homeassistant/sensor/<serial>/rate_limit_status/config` and `homeassistant/sensor/<serial>/api_requests/config`
   (`mosquitto_pub -r -n -t ...`); leave `rate_limit`/`request_counters` in `/data/thermowatt_config.json` (ignored).
4. Re-verify as 3.1/3.3: HWS state unchanged in the app, ownership/baseline helpers, polling fresh, `command_status`.
5. If ownership was on at any point, complete handback and confirm in the app before closing the change.

## 4. Follow-up issues (separate from this release)
- Baseline API quota / request headroom: https://github.com/MMicieli/ha-thermowatt-heater/issues/17
- HTTP 401 POST replay semantics: https://github.com/MMicieli/ha-thermowatt-heater/issues/18
- Latched `command_status` recovery: https://github.com/MMicieli/ha-thermowatt-heater/issues/19
- Post-outage energy accuracy: https://github.com/MMicieli/ha-thermowatt-heater/issues/20

## 5. Outstanding deployment blockers
1. Explicit operator approval to merge PR #16 and to update the installed add-on (neither done).
2. A suitable maintenance window with ownership off and a verified pre-update backup (section 3.0/3.1).
3. Not validated here: a real Supervisor/HA (the entrypoint was exercised against a stub), the real cloud's responses to quota, and the real device.
