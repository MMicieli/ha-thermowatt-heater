"""Assertions for the stubbed container e2e (run_e2e.sh). Exits non-zero on any violation.

Usage: verify_e2e.py <A|B> <out-dir>

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


def main(argv):
    scenario, out = argv[1].upper(), pathlib.Path(argv[2])
    read = lambda n: (out / n).read_text(encoding="utf-8", errors="replace") if (out / n).exists() else ""  # noqa: E731
    api = parse_json_lines(read("api.log"))
    supervisor = parse_json_lines(read("supervisor.log"))
    mqtt = parse_mqtt(read("mqtt.log"))
    meta = json.loads(read("meta.json") or "{}")
    if scenario == "A":
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
