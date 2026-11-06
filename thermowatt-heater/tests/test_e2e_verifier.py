"""The container e2e verifier must fail (non-zero) on unexpected API calls, wrong backoff timing,
command replay, or missing recovery transitions, and pass on a correct run. Synthetic data only."""

import copy
import importlib.util
import json
import pathlib

import pytest

_spec = importlib.util.spec_from_file_location(
    "verify_e2e", pathlib.Path(__file__).parent / "e2e" / "verify_e2e.py")
v = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(v)


def ev(t, m, p, limited=False):
    return {"t": t, "m": m, "p": p, "limited": limited, "body": ""}


def diag(state, poll="ok", telemetry="fresh", cmd="idle", rl=0, blocked=0):
    return json.dumps({"rate_limit": {"state": state}, "poll_status": poll, "telemetry": {"state": telemetry},
                       "command_status": cmd,
                       "requests": {"rate_limited_responses": rl, "blocked": blocked}})


A_API = [ev(0, "POST", "/login"), ev(0, "GET", "/user-info"), ev(0, "GET", "/status"), ev(60, "GET", "/status"),
         ev(120, "GET", "/status", True), ev(240, "GET", "/status", True), ev(480, "GET", "/status"),
         ev(520, "POST", "/eco"), ev(520, "GET", "/status"), ev(540, "GET", "/status")]
A_LOG = ("Starting Thermowatt Bridge for u\nstuff\ncommand NOT sent\ncommand NOT sent\n"
         "[RATE LIMIT] Recovered — backoff cleared\nStopping...\n")
A_MQTT = [("P/STUBSN1/STATUS", "{}")] + [("P/STUBSN1/diagnostics", d) for d in (
    diag("none"), diag("active", "rate_limited", "rate_limited", rl=1),
    diag("recovery_unverified", "rate_limited", "rate_limited", rl=1, blocked=1),
    diag("active", "rate_limited", "rate_limited", rl=2, blocked=1),
    diag("recovery_unverified", "rate_limited", "rate_limited", rl=2, blocked=2),
    diag("recovered", rl=2, blocked=2), diag("recovered", cmd="pending", rl=2, blocked=2),
    diag("recovered", cmd="confirmed", rl=2, blocked=2))]
SUP = [{"p": "/addons/self/options/config", "authed": True, "known": True},
       {"p": "/services/mqtt", "authed": True, "known": True}]
META_A = {"exit_code": 0}


def run_a(api=A_API, log=A_LOG, mqtt=A_MQTT, sup=SUP, meta=META_A):
    return v.check_scenario_a(api, log, mqtt, sup, meta)


def test_correct_scenario_a_passes():
    assert run_a() == []


@pytest.mark.parametrize("name,mutate,needle", [
    ("unexpected_call_in_window", lambda api: api.append(ev(180, "GET", "/status")), "backoff"),
    ("early_resume_after_first_limit", lambda api: api.insert(5, ev(150, "GET", "/status")), "backoff"),
    ("backoff_too_short", lambda api: api.__setitem__(6, ev(300, "GET", "/status")), "backoff"),
    ("backoff_too_long", lambda api: api.__setitem__(6, ev(700, "GET", "/status")), "resumed"),
    ("extra_limited_response", lambda api: api.append(ev(560, "GET", "/status", True)), "limited responses"),
    ("command_replayed", lambda api: api.append(ev(530, "POST", "/eco")), "command requests"),
    ("command_sent_in_window", lambda api: api.insert(5, ev(150, "POST", "/off")), "command requests"),
    ("reauth_on_quota", lambda api: api.insert(5, ev(121, "POST", "/login")), "login"),
    ("unexpected_path", lambda api: api.append(ev(545, "GET", "/user-info/extra")), "unexpected API paths"),
    ("polling_never_resumes", lambda api: api.__delitem__(slice(6, None)), "polling did not continue"),
])
def test_api_violations_fail(name, mutate, needle):
    api = copy.deepcopy(A_API)
    mutate(api)
    failures = run_a(api=api)
    assert failures and any(needle in f for f in failures), failures


@pytest.mark.parametrize("name,drop", [
    ("missing_recovered_state", "recovered"),
    ("missing_recovery_unverified", "recovery_unverified"),
    ("missing_active", "active"),
])
def test_missing_recovery_transitions_fail(name, drop):
    mqtt = [(t, p) for t, p in A_MQTT if t != "P/STUBSN1/diagnostics" or json.loads(p)["rate_limit"]["state"] != drop]
    failures = run_a(mqtt=mqtt)
    assert any("rate_limit.state transitions" in f for f in failures), failures


def test_poll_status_never_recovering_fails():
    mqtt = [(t, p.replace('"poll_status": "ok"', '"poll_status": "rate_limited"')) for t, p in A_MQTT]
    assert any("poll_status transitions" in f for f in run_a(mqtt=mqtt))


@pytest.mark.parametrize("log,needle", [
    (A_LOG.replace("command NOT sent\n", "", 1), "locally refused"),
    (A_LOG.replace("[RATE LIMIT] Recovered", "x"), "recovery log line"),
    (A_LOG + "Traceback (most recent call last)\n", "Traceback"),
    (A_LOG.replace("Starting Thermowatt Bridge for", "x"), "entrypoint not exercised"),
    (A_LOG + "stubpass-SECRET\n", "leaked"),
])
def test_log_violations_fail(log, needle):
    assert any(needle in f for f in run_a(log=log)), needle


def test_entrypoint_and_exit_checks():
    assert any("supervisor" in f for f in run_a(sup=SUP[:1]))
    assert any("unexpected supervisor" in f for f in run_a(sup=SUP + [{"p": "/other", "authed": True, "known": False}]))
    assert any("exit code" in f for f in run_a(meta={"exit_code": 137}))


def test_final_counters_and_command_status_checked():
    last = json.loads(A_MQTT[-1][1])
    last["requests"]["blocked"] = 0
    mqtt = A_MQTT[:-1] + [("P/STUBSN1/diagnostics", json.dumps(last))]
    assert any("final counters" in f for f in run_a(mqtt=mqtt))
    last = json.loads(A_MQTT[-1][1])
    last["command_status"] = "submit_failed"
    mqtt = A_MQTT[:-1] + [("P/STUBSN1/diagnostics", json.dumps(last))]
    assert any("final command_status" in f for f in run_a(mqtt=mqtt))


# ---- scenario B ----
B_API = [ev(0, "POST", "/login"), ev(0, "GET", "/user-info"), ev(0, "GET", "/status"),
         ev(60, "GET", "/status", True), ev(180, "POST", "/login", True), ev(420, "POST", "/login"),
         ev(420, "GET", "/user-info"), ev(420, "GET", "/status"), ev(480, "GET", "/status")]
B_LOG = ("Starting Thermowatt Bridge for u\n--- SECOND START ---\nPersisted backoff active\nStep 3 deferred\n"
         "OK: Step 4 - Found 1 thermostats\n[RATE LIMIT] Recovered\nStopping...\n")
B_MQTT = [("P/STUBSN1/STATUS", "{}")] + [("P/STUBSN1/diagnostics", d) for d in (diag("none"), diag("active"), diag("recovered"))]
META_B = {"first_exit_code": 0, "second_exit_code": 0, "second_status": "running", "second_restart_count": 0,
          "persisted_rate_limit": {"step": 1}}


def run_b(api=B_API, log=B_LOG, mqtt=B_MQTT, meta=META_B):
    return v.check_scenario_b(api, "Starting Thermowatt Bridge for u\n" + log.split("--- SECOND START ---")[0] + "--- SECOND START ---" +
                              log.split("--- SECOND START ---")[1], mqtt, SUP, meta)


def test_correct_scenario_b_passes():
    assert run_b() == []


@pytest.mark.parametrize("mutate,needle", [
    (lambda a: a.insert(4, ev(100, "GET", "/status")), "persisted backoff"),
    (lambda a: a.__setitem__(4, ev(120, "POST", "/login", True)), "backoff"),
    (lambda a: a.__setitem__(5, ev(200, "POST", "/login")), "backoff"),
    (lambda a: a.append(ev(500, "POST", "/off")), "command"),
])
def test_scenario_b_api_violations_fail(mutate, needle):
    api = copy.deepcopy(B_API)
    mutate(api)
    assert any(needle in f for f in run_b(api=api)), run_b(api=api)


def test_scenario_b_state_violations_fail():
    assert any("persisted" in f for f in run_b(meta={**META_B, "persisted_rate_limit": {"step": 0}}))
    assert any("restarts" in f for f in run_b(meta={**META_B, "second_restart_count": 1}))
    assert any("missing 'Step 3 deferred'" in f for f in run_b(log=B_LOG.replace("Step 3 deferred", "x")))
    assert any("rate_limit.state" in f for f in run_b(mqtt=B_MQTT[:2]))
