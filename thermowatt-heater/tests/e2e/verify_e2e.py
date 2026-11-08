"""Assertions for the stubbed container e2e (run_e2e.sh). Exits non-zero on any violation.

Usage: verify_e2e.py <A|B|C|D|F|G|H> <out-dir>

A and B (rate-limit windows, opaque tokens) are unchanged. C, D, F and G use synthetic JWT access
tokens with short controlled expiry (see stub_api.py JWT_LIFETIME) to exercise the proactive
token-refresh branch (issue #17, PR #21).

The check_* functions are pure (parsed data in, list of failure strings out) so they are
unit-tested with injected violations in tests/test_e2e_verifier.py.
Times are seconds since the stub API's first request; TOL absorbs container scheduling jitter.
"""
import json
import pathlib
import sys

TOL = 6.0
BASE = 120  # first backoff step (s)
COMMAND_PATHS = {"/manual", "/eco", "/auto", "/holiday", "/off"}
ALLOWED_SUPERVISOR = {"/addons/self/options/config", "/services/mqtt"}
SECRETS = ("stubpass-SECRET", "tw-pass-SECRET", "stub-supervisor-token-SECRET", "stub-a", "stub-r")
STATE_ORDER_A = ["none", "active", "recovery_unverified", "active", "recovery_unverified", "recovered"]


def parse_json_lines(text):
    out = []
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("{"):
            try:
                out.append(json.loads(line))
            except ValueError:
                pass
    return out


def parse_mqtt(text):
    """mosquitto_sub -F '%I %t %p' lines -> (topic, payload-str) pairs."""
    out = []
    for line in text.splitlines():
        parts = line.split(" ", 2)
        if len(parts) == 3:
            out.append((parts[1], parts[2]))
    return out


def diagnostics(mqtt):
    result = []
    for topic, payload in mqtt:
        if topic.endswith("/diagnostics"):
            try:
                result.append(json.loads(payload))
            except ValueError:
                pass
    return result


def is_subsequence(wanted, seen):
    it = iter(seen)
    return all(any(w == s for s in it) for w in wanted)


def dedupe(seq):
    out = []
    for item in seq:
        if not out or out[-1] != item:
            out.append(item)
    return out


def close(actual, expected, tol=TOL):
    return abs(actual - expected) <= tol


def check_common(api, bridge_log, mqtt, supervisor, meta):
    f = []
    # Real entrypoint (run.sh + bashio + Supervisor stub + authenticated MQTT service)
    if "Starting Thermowatt Bridge for" not in bridge_log:
        f.append("run.sh log line missing: entrypoint not exercised")
    seen = {(e["p"], e.get("authed"), e.get("known")) for e in supervisor}
    for path in ALLOWED_SUPERVISOR:
        if (path, True, True) not in seen:
            f.append(f"supervisor never served authorised {path}")
    for e in supervisor:
        if e["p"] not in ALLOWED_SUPERVISOR or not e.get("authed"):
            f.append(f"unexpected supervisor request {e['p']} authed={e.get('authed')}")
    if not any(t == "P/STUBSN1/STATUS" for t, _ in mqtt):
        f.append("no STATUS published via authenticated broker")
    # Hygiene
    for secret in SECRETS:
        for name, blob in (("bridge log", bridge_log), ("mqtt", "\n".join(p for _, p in mqtt)),
                           ("api log", json.dumps(api))):
            if secret in blob:
                f.append(f"secret {secret!r} leaked in {name}")
    for bad in ("Traceback", "FAILED:"):
        if bad in bridge_log:
            f.append(f"bridge log contains {bad!r}")
    unexpected = [e for e in api if e["p"] not in {"/login", "/user-info", "/status"} | COMMAND_PATHS]
    if unexpected:
        f.append(f"unexpected API paths: {sorted({e['p'] for e in unexpected})}")
    return f


def check_backoff_spacing(api, limited_times_expected, f, label="backoff"):
    """After the k-th limited response the next API request must be BASE*2^(k-1) later (+/- TOL)."""
    limited = [e for e in api if e["limited"]]
    if len(limited) != len(limited_times_expected):
        f.append(f"{label}: expected {len(limited_times_expected)} limited responses, saw {len(limited)}")
        return
    for k, ev in enumerate(limited):
        if not close(ev["t"], limited_times_expected[k]):
            f.append(f"{label}: limited response {k + 1} at t={ev['t']} expected ~{limited_times_expected[k]}")
        later = [e["t"] for e in api if e["t"] > ev["t"] + 0.5]
        if not later:
            f.append(f"{label}: no request after limited response {k + 1}")
            continue
        gap = min(later) - ev["t"]
        expected = BASE * 2 ** k
        if gap < expected - TOL:
            f.append(f"{label}: request {gap:.1f}s after limited response {k + 1}, backoff is {expected}s")
        if gap > expected + TOL + 25:
            f.append(f"{label}: resumed {gap:.1f}s after limited response {k + 1}, expected ~{expected}s")


def check_scenario_a(api, bridge_log, mqtt, supervisor, meta):
    f = check_common(api, bridge_log, mqtt, supervisor, meta)
    status = [e for e in api if e["p"] == "/status"]
    logins = [e for e in api if e["p"] == "/login"]
    if len(logins) != 1:
        f.append(f"expected exactly 1 login, saw {len(logins)} (quota error must not trigger re-auth)")
    for got, want in zip(status[:5], [0, 60, 120, 240, 480]):
        if not close(got["t"], want):
            f.append(f"status poll at t={got['t']} expected ~{want}")
    if len(status) < 6:
        f.append("polling did not continue after recovery")
    check_backoff_spacing(api, [120, 240], f)
    # Commands: refused in-window (never sent); exactly one accepted after recovery; no replay.
    cmds = [e for e in api if e["p"] in COMMAND_PATHS]
    if [(e["m"], e["p"]) for e in cmds] != [("POST", "/eco")]:
        f.append(f"command requests reaching the API: {[(e['m'], e['p'], e['t']) for e in cmds]}, expected one POST /eco")
    elif cmds[0]["t"] < 480 - TOL:
        f.append(f"command sent at t={cmds[0]['t']} inside/before recovery")
    if bridge_log.count("command NOT sent") != 2:
        f.append(f"expected 2 locally refused commands, saw {bridge_log.count('command NOT sent')}")
    if "[RATE LIMIT] Recovered" not in bridge_log:
        f.append("recovery log line missing")
    if "Stopping..." not in bridge_log:
        f.append("no clean-shutdown log line")
    if meta.get("exit_code") != 0:
        f.append(f"container exit code {meta.get('exit_code')}")
    diags = diagnostics(mqtt)
    states = dedupe(d["rate_limit"]["state"] for d in diags)
    if not is_subsequence(STATE_ORDER_A, states):
        f.append(f"rate_limit.state transitions {states} missing ordered {STATE_ORDER_A}")
    polls = dedupe(d["poll_status"] for d in diags)
    if not is_subsequence(["ok", "rate_limited", "ok"], polls):
        f.append(f"poll_status transitions {polls}")
    if diags:
        last = diags[-1]
        if last["command_status"] != "confirmed":
            f.append(f"final command_status {last['command_status']}")
        if last["rate_limit"]["state"] != "recovered":
            f.append(f"final rate_limit.state {last['rate_limit']['state']}")
        if last["requests"]["rate_limited_responses"] != 2 or last["requests"]["blocked"] != 2:
            f.append(f"final counters {last['requests']}")
    else:
        f.append("no diagnostics captured")
    return f


def check_scenario_b(api, bridge_log, mqtt, supervisor, meta):
    """bridge_log is the concatenation of both runs (first, then second)."""
    f = check_common(api, bridge_log, mqtt, supervisor, meta)
    first, _, second = bridge_log.partition("--- SECOND START ---")
    if not second:
        return f + ["second-run marker missing"]
    persisted = meta.get("persisted_rate_limit") or {}
    if persisted.get("step") != 1:
        f.append(f"persisted rate_limit after SIGTERM: {persisted}")
    check_backoff_spacing(api, [60, 180], f)
    if any(e["p"] in COMMAND_PATHS for e in api):
        f.append("a command reached the API")
    # No request at all between the rate-limited poll and the persisted expiry (~180 s).
    mid = [e["t"] for e in api if 60 + 1 < e["t"] < 180 - TOL]
    if mid:
        f.append(f"API requests during persisted backoff: {mid}")
    for needle in ("Persisted backoff active", "Step 3 deferred", "[RATE LIMIT] Recovered",
                   "OK: Step 4 - Found 1 thermostats"):
        if needle not in second:
            f.append(f"second run log missing {needle!r}")
    if "FAILED" in second:
        f.append("second run reported a boot FAILED step during rate limit")
    if meta.get("second_restart_count") != 0 or meta.get("second_status") != "running":
        f.append(f"second container state {meta.get('second_status')} restarts={meta.get('second_restart_count')}")
    if meta.get("first_exit_code") != 0 or meta.get("second_exit_code") != 0:
        f.append(f"exit codes {meta.get('first_exit_code')}/{meta.get('second_exit_code')}")
    post_login = [e for e in api if e["p"] == "/login" and not e["limited"] and e["t"] > 100]  # second run
    if len(post_login) != 1 or not close(post_login[0]["t"], 420, 12):
        f.append(f"successful login expected ~t=420, saw {[e['t'] for e in post_login]}")
    states = dedupe(d["rate_limit"]["state"] for d in diagnostics(mqtt))
    if not is_subsequence(["active", "recovered"], states):
        f.append(f"rate_limit.state transitions {states}")
    return f


# ───────────────────── JWT / proactive-refresh scenarios (C, D, F, G) ─────────────────────
JWT_SECRETS = ("eyJhbGci", "SIGNATURE-SECRET-JWT", "stub-refresh-SECRET")
JWT_MARGIN = 120      # PROACTIVE_REFRESH_MARGIN_S
POLL = 62             # effective poll interval (s)


def _jwt_common(api, bridge_log, mqtt, supervisor, meta, *, flag_expected):
    """Hygiene and plumbing shared by C/D/F/G (check_common minus the A/B-only path/secret lists)."""
    f = []
    if "Starting Thermowatt Bridge for" not in bridge_log:
        f.append("run.sh log line missing: entrypoint not exercised")
    want = f"proactive token refresh: {1 if flag_expected else 0}"
    if want not in bridge_log:
        f.append(f"run.sh did not report {want!r}")
    seen = {(e["p"], e.get("authed"), e.get("known")) for e in supervisor}
    for path in ALLOWED_SUPERVISOR:
        if (path, True, True) not in seen:
            f.append(f"supervisor never served authorised {path}")
    for e in supervisor:
        if e["p"] not in ALLOWED_SUPERVISOR or not e.get("authed"):
            f.append(f"unexpected supervisor request {e['p']} authed={e.get('authed')}")
    if not any(t == "P/STUBSN1/STATUS" for t, _ in mqtt):
        f.append("no STATUS published via authenticated broker")
    blobs = (("bridge log", bridge_log), ("mqtt", "\n".join(p for _, p in mqtt)), ("api log", json.dumps(api)))
    for secret in SECRETS[:3] + JWT_SECRETS:
        for name, blob in blobs:
            if secret in blob:
                f.append(f"secret {secret!r} leaked in {name}")
    for bad in ("Traceback", "FAILED:"):
        if bad in bridge_log:
            f.append(f"bridge log contains {bad!r}")
    allowed = {"/login", "/refresh", "/user-info", "/status"} | COMMAND_PATHS
    unexpected = sorted({e["p"] for e in api if e["p"] not in allowed})
    if unexpected:
        f.append(f"unexpected API paths: {unexpected}")
    if meta.get("first_exit_code", meta.get("exit_code")) != 0:
        f.append(f"container exit code {meta.get('first_exit_code', meta.get('exit_code'))}")
    if meta.get("second_exit_code", 0) != 0:
        f.append(f"second container exit code {meta.get('second_exit_code')}")
    diags = diagnostics(mqtt)
    if not diags:
        f.append("no diagnostics captured")
    for d in diags:
        if d.get("proactive_token_refresh") is not flag_expected:
            f.append(f"diagnostics proactive_token_refresh={d.get('proactive_token_refresh')!r}, expected {flag_expected}")
            break
    return f


def _by_path(api, path):
    return [e for e in api if e["p"] == path]


def _unauthorised(api):
    """Requests the stub answered 401 (JWT mode logs `auth` for everything but login/refresh)."""
    return [e for e in api if e.get("auth") in ("expired", "none") and not e["limited"]]


def _check_reactive_replay(api, f, label):
    """Every 401 is followed, in log order, by exactly one /refresh and then ONE replayed request.

    Order is taken from the log sequence, not the timestamps: the stub rounds time to 0.1 s and the
    three requests of a replay normally share one timestamp."""
    for i, ev in enumerate(api):
        if ev.get("auth") not in ("expired", "none") or ev["limited"]:
            continue
        nxt = api[i + 1:i + 4]
        if not nxt or nxt[0]["p"] != "/refresh" or nxt[0]["t"] - ev["t"] > 6:
            f.append(f"{label}: 401 at t={ev['t']} not followed by /refresh ({[e['p'] for e in nxt[:1]]})")
            continue
        replay = nxt[1:3]
        if (not replay or replay[0]["p"] != ev["p"] or replay[0].get("auth") != "ok"
                or replay[0]["t"] - nxt[0]["t"] > 6):
            f.append(f"{label}: no authorised replay of {ev['p']} after the refresh at t={nxt[0]['t']}")
        elif len(replay) > 1 and replay[1]["p"] == ev["p"] and replay[1]["t"] - replay[0]["t"] < 3:
            f.append(f"{label}: replay of {ev['p']} sent more than once")


def _ledger_total(ledger):
    if not isinstance(ledger, dict):
        return None
    total = 0
    for row in (ledger.get("buckets") or {}).values():
        total += sum(row[:4])          # status, command, auth, other = attempted
    return total


def _check_ledger_continuity(diags, f):
    """rolling_24h.attempted never decreases across the whole run, restart included."""
    seq = [d["requests"]["rolling_24h"]["attempted"] for d in diags if "rolling_24h" in d.get("requests", {})]
    if not seq:
        f.append("no requests.rolling_24h in diagnostics")
        return seq
    if any(b < a for a, b in zip(seq, seq[1:])):
        f.append(f"rolling_24h.attempted decreased across the restart: {seq}")
    for d in diags:
        r = d["requests"]["rolling_24h"]
        if r["attempted"] != r["transport_failed"] + r["responses_received"]:
            f.append(f"attempted != transport_failed + responses_received: {r}")
            break
    return seq


def _check_persisted_ledger(api, meta, f):
    """Persisted request_ledger after the first stop equals the API requests the stub logged."""
    seen = meta.get("api_requests_run1")
    total = _ledger_total(meta.get("persisted_ledger"))
    if seen is None or total is None:
        f.append("persisted request_ledger or run-1 API request count missing")
    elif total != seen:
        f.append(f"persisted ledger counts {total} attempted requests, stub saw {seen}")


def check_scenario_c(api, bridge_log, mqtt, supervisor, meta):
    """Proactive refresh ON, token lifetime 300 s, no quota: exactly one refresh, before expiry, no 401."""
    f = _jwt_common(api, bridge_log, mqtt, supervisor, meta, flag_expected=True)
    if len(_by_path(api, "/login")) != 2:     # the bridge logs in once per boot: run 1 and the restart
        f.append(f"expected 2 logins (one per boot), saw {len(_by_path(api, '/login'))}")
    run1 = [e for e in api if e["t"] <= meta.get("run1_end_t", 1e9)]
    refreshes = _by_path(run1, "/refresh")
    login_t = _by_path(api, "/login")[0]["t"] if _by_path(api, "/login") else 0
    if len(refreshes) != 1:
        f.append(f"expected exactly 1 proactive refresh in run 1, saw {[e['t'] for e in refreshes]}")
    else:
        lo, hi = login_t + 300 - JWT_MARGIN - TOL, login_t + 300 - JWT_MARGIN + POLL + TOL
        if not lo <= refreshes[0]["t"] <= hi:
            f.append(f"proactive refresh at t={refreshes[0]['t']}, expected within [{lo:.0f}, {hi:.0f}] (before expiry at {login_t + 300:.0f})")
        nxt = [e for e in run1 if e["t"] > refreshes[0]["t"]][:1]
        if not nxt or nxt[0]["p"] != "/status" or nxt[0].get("auth") != "ok":
            f.append("the poll after the proactive refresh was not an authorised /status")
    if _unauthorised(api):
        f.append(f"401 responses despite proactive refresh: {[(e['p'], e['t']) for e in _unauthorised(api)]}")
    if "Proactive token refresh succeeded" not in bridge_log:
        f.append("log line 'Proactive token refresh succeeded' missing")
    if any(e["limited"] for e in api):
        f.append("unexpected quota response in scenario C")
    seq = _check_ledger_continuity(diagnostics(mqtt), f)
    _check_persisted_ledger(api, meta, f)
    if seq and meta.get("api_requests_run1") is not None and seq[-1] <= meta["api_requests_run1"]:
        f.append("second run did not continue from the persisted ledger")
    return f


def check_scenario_d(api, bridge_log, mqtt, supervisor, meta):
    """Option ABSENT from the Supervisor config (existing install), JWT tokens: reactive path only."""
    f = _jwt_common(api, bridge_log, mqtt, supervisor, meta, flag_expected=False)
    refreshes = _by_path(api, "/refresh")
    expired = _unauthorised(api)
    if len(expired) != 1:
        f.append(f"expected exactly 1 expired-token 401, saw {[(e['p'], e['t']) for e in expired]}")
    if len(refreshes) != 1:
        f.append(f"expected exactly 1 (reactive) refresh, saw {[e['t'] for e in refreshes]}")
    if expired and refreshes and refreshes[0]["t"] < expired[0]["t"]:
        f.append("a refresh was sent BEFORE the 401: proactive refresh is not off by default")
    _check_reactive_replay(api, f, "default-off")
    if "Proactive token refresh" in bridge_log:
        f.append("proactive refresh logged although the option is absent")
    if len(_by_path(api, "/login")) != 1:
        f.append(f"expected 1 login, saw {len(_by_path(api, '/login'))}")
    return f


def check_scenario_h(api, bridge_log, mqtt, supervisor, meta):
    """Stage 2: poll_interval=120 (option), a retained refresh before start, then a burst of 5
    refresh messages at ~40 s. Expect exactly 3 /status requests: boot, ONE coalesced demand
    poll, and the next background poll one full interval after the demand poll."""
    f = _jwt_common(api, bridge_log, mqtt, supervisor, meta, flag_expected=False)
    if "poll interval option: 120" not in bridge_log or "background=120s" not in bridge_log:
        f.append("run.sh/bridge did not report the 120 s background interval option")
    status = _by_path(api, "/status")
    if len(status) != 3:
        f.append(f"expected exactly 3 /status requests (boot, 1 demand, 1 background), saw {[e['t'] for e in status]}")
    else:
        t0 = status[0]["t"]
        if not 38 - TOL <= status[1]["t"] - t0 <= 48 + TOL:
            f.append(f"demand poll at +{status[1]['t'] - t0:.1f}s, expected about +40 s (retained or burst misbehaviour)")
        if not 120 - TOL <= status[2]["t"] - status[1]["t"] <= 120 + 2 * TOL:
            f.append(f"background poll {status[2]['t'] - status[1]['t']:.1f}s after the demand poll, expected about 120 s")
    for path in ("/refresh",) + tuple(COMMAND_PATHS):
        if _by_path(api, path):
            f.append(f"unexpected {path} request: demand refresh must be read-only status polling")
    if len(_by_path(api, "/login")) != 1:
        f.append(f"expected 1 login, saw {len(_by_path(api, '/login'))}")
    intervals = {d.get("poll_interval") for d in diagnostics(mqtt)}
    if intervals != {120}:
        f.append(f"diagnostics poll_interval values {sorted(map(str, intervals))}, expected only 120")
    if any(p == "offline" for t, p in mqtt if t == "P/STUBSN1/availability"):
        f.append("device published offline although every poll succeeded")
    return f


def check_scenario_f(api, bridge_log, mqtt, supervisor, meta):
    """Proactive ON, first refresh answered HTTP 500: attempted once, then the reactive path recovers."""
    f = _jwt_common(api, bridge_log, mqtt, supervisor, meta, flag_expected=True)
    refreshes = _by_path(api, "/refresh")
    expired = _unauthorised(api)
    if len(refreshes) != 2:
        f.append(f"expected exactly 2 refreshes (1 failed proactive + 1 reactive), saw {[e['t'] for e in refreshes]}")
    if len(expired) != 1:
        f.append(f"expected exactly 1 401 after the failed proactive refresh, saw {[(e['p'], e['t']) for e in expired]}")
    elif len(refreshes) == 2:
        if not api.index(refreshes[0]) < api.index(expired[0]) < api.index(refreshes[1]):  # log order
            f.append("order must be: failed proactive refresh, 401, reactive refresh")
        if refreshes[1]["t"] - refreshes[0]["t"] < POLL:
            f.append("second refresh followed the first too closely: proactive retried within the token's life")
    _check_reactive_replay(api, f, "fallback")
    if "Proactive token refresh rejected" not in bridge_log:
        f.append("log line 'Proactive token refresh rejected' missing")
    if len(_by_path(api, "/login")) != 1:
        f.append(f"expected 1 login (no re-login), saw {len(_by_path(api, '/login'))}")
    return f


def check_scenario_g(api, bridge_log, mqtt, supervisor, meta):
    """Proactive ON, quota window opens as the token nears expiry: nothing sent during backoff."""
    f = _jwt_common(api, bridge_log, mqtt, supervisor, meta, flag_expected=True)
    limited = [e for e in api if e["limited"]]
    if len(limited) != 1 or limited[0]["p"] != "/refresh":
        f.append(f"expected exactly one limited response, to the proactive /refresh; saw {[(e['p'], e['t']) for e in limited]}")
        return f
    lim = limited[0]
    window = [e for e in api if lim["t"] + 0.5 < e["t"] < lim["t"] + BASE - TOL]
    if window:
        f.append(f"API requests during quota backoff: {[(e['p'], e['t']) for e in window]}")
    after = [e for e in api if e["t"] >= lim["t"] + BASE - TOL][:1]
    if not after or after[0]["p"] != "/status":
        f.append(f"first request after backoff must be the /status probe, not auth: {[e['p'] for e in after]}")
    elif after[0].get("auth") != "expired":
        f.append("probe should have used the expired token (no proactive refresh while recovery is unverified)")
    check_backoff_spacing(api, [lim["t"]], f, label="proactive-refresh quota")
    _check_reactive_replay(api, f, "post-backoff")
    cmds = [e for e in api if e["p"] in COMMAND_PATHS]
    if cmds:
        f.append(f"command reached the API: {[(e['p'], e['t']) for e in cmds]}")
    if bridge_log.count("command NOT sent") != 1:
        f.append(f"expected 1 locally refused command, saw {bridge_log.count('command NOT sent')}")
    run1 = [e for e in api if e["t"] <= meta.get("run1_end_t", 1e9)]
    if len(_by_path(run1, "/refresh")) != 2:
        f.append(f"expected 2 /refresh in run 1 (1 limited, 1 reactive), saw {[e['t'] for e in _by_path(run1, '/refresh')]}")
    if len(_by_path(api, "/login")) != 2:
        f.append(f"expected 2 logins (one per boot), saw {len(_by_path(api, '/login'))}")
    if "Proactive token refresh deferred" not in bridge_log:
        f.append("log line 'Proactive token refresh deferred' missing")
    # evidence: persisted and published, before and after restart
    eps = meta.get("persisted_episodes") or []
    idx = api.index(lim) + 1
    if len(eps) != 1:
        f.append(f"expected 1 persisted episode, saw {len(eps)}")
    else:
        ep = eps[0]
        first, rec = ep.get("first_rejection"), ep.get("recovery")
        if not first or first.get("reason_class") != "quota_message":
            f.append(f"persisted first_rejection {first}")
        elif first["rolling_24h"]["attempted"] != idx:
            f.append(f"first_rejection counted {first['rolling_24h']['attempted']} attempted requests, stub saw {idx}")
        if not rec or rec.get("rejected_responses_in_episode") != 1:
            f.append(f"persisted recovery {rec}")
    diags = diagnostics(mqtt)
    last = diags[-1] if diags else {}
    published = (last.get("rate_limit") or {}).get("episodes") or []
    if len(published) != 1 or published[0].get("recovery") is None:
        f.append(f"published episodes after restart: {published}")
    _check_ledger_continuity(diags, f)
    _check_persisted_ledger(api, meta, f)
    return f


def main(argv):
    scenario, out = argv[1].upper(), pathlib.Path(argv[2])
    read = lambda n: (out / n).read_text(encoding="utf-8", errors="replace") if (out / n).exists() else ""  # noqa: E731
    api = parse_json_lines(read("api.log"))
    supervisor = parse_json_lines(read("supervisor.log"))
    mqtt = parse_mqtt(read("mqtt.log"))
    meta = json.loads(read("meta.json") or "{}")
    jwt_checks = {"H": check_scenario_h, "C": check_scenario_c, "D": check_scenario_d, "F": check_scenario_f, "G": check_scenario_g}
    if scenario in jwt_checks:
        log = read("bridge.log") + "\n--- SECOND START ---\n" + read("bridge2.log")
        failures = jwt_checks[scenario](api, log, mqtt, supervisor, meta)
    elif scenario == "A":
        failures = check_scenario_a(api, read("bridge.log"), mqtt, supervisor, meta)
    else:
        log = read("bridge.log") + "\n--- SECOND START ---\n" + read("bridge2.log")
        failures = check_scenario_b(api, log, mqtt, supervisor, meta)
    if failures:
        print(f"E2E {scenario}: FAIL ({len(failures)})")
        for item in failures:
            print(" -", item)
        return 1
    print(f"E2E {scenario}: PASS (api requests={len(api)}, diagnostics={len(diagnostics(mqtt))})")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
