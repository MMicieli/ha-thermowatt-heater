"""Verifier for the JWT / proactive-refresh container scenarios C, D, F, G (issue #17, PR #21).

These tests check the VERIFIER: that it passes a correct synthetic timeline and fails each
injected violation. They are not the Docker e2e and do not stand in for it.
"""

import copy
import importlib.util
import json
import pathlib

import pytest

_spec = importlib.util.spec_from_file_location(
    "verify_e2e", pathlib.Path(__file__).parent / "e2e" / "verify_e2e.py")
v = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(v)

SUP = [{"p": "/addons/self/options/config", "authed": True, "known": True},
       {"p": "/services/mqtt", "authed": True, "known": True}]


def ev(t, m, p, auth=None, limited=False):
    e = {"t": t, "m": m, "p": p, "limited": limited, "body": ""}
    if auth is not None:
        e["auth"] = auth
    return e


def S(t, auth="ok", **kw):
    return ev(t, "GET", "/status", auth, **kw)


def diag(attempted, flag=True, episodes=(), rl=0):
    return ("P/STUBSN1/diagnostics", json.dumps({
        "proactive_token_refresh": flag,
        "rate_limit": {"state": "none", "episodes": list(episodes)},
        "requests": {"rolling_24h": {"attempted": attempted, "transport_failed": 0,
                                     "responses_received": attempted, "rate_limited_responses": rl}}}))


def ledger(n):
    return {"buckets": {"900": [n - 2, 0, 1, 1, 0, 0, 0]}}


def logtext(flag, *extra):
    return "Starting Thermowatt Bridge for u (proactive token refresh: %d)...\n%s\n" % (
        1 if flag else 0, "\n".join(extra))


MQTT_STATUS = ("P/STUBSN1/STATUS", "{}")

# ───────────────────────────── C ─────────────────────────────
C_API = [ev(0, "POST", "/login"), ev(0.2, "GET", "/user-info", "ok"), S(1), S(63), S(125),
         ev(187, "POST", "/refresh"), S(187.2), S(249), S(311), S(373),
         ev(420, "POST", "/login"), ev(420.2, "GET", "/user-info", "ok"), S(421), S(483)]
C_META = {"first_exit_code": 0, "second_exit_code": 0, "api_requests_run1": 10, "run1_end_t": 373,
          "persisted_ledger": ledger(10)}
C_MQTT = [MQTT_STATUS, diag(3), diag(6), diag(10), diag(12), diag(14)]
C_LOG = logtext(True, "[AUTH] Proactive token refresh succeeded") * 1


def run_c(api=C_API, log=C_LOG, mqtt=C_MQTT, meta=C_META):
    return v.check_scenario_c(api, log, mqtt, SUP, meta)


def test_correct_c_passes():
    assert run_c() == []


@pytest.mark.parametrize("name,mutate,needle", [
    ("no_proactive_refresh", lambda api: api.pop(5), "exactly 1 proactive refresh"),
    ("two_refreshes", lambda api: api.insert(8, ev(260, "POST", "/refresh")), "exactly 1 proactive refresh"),
    ("refresh_too_early", lambda api: api.__setitem__(5, ev(60, "POST", "/refresh")), "expected within"),
    ("refresh_too_late_after_expiry", lambda api: api.__setitem__(5, ev(305, "POST", "/refresh")), "expected within"),
    ("a_401_happened", lambda api: api.insert(9, S(312, auth="expired")), "401 responses"),
    ("quota_response", lambda api: api.append(S(500, limited=True)), "quota"),
    ("extra_login", lambda api: api.append(ev(500, "POST", "/login")), "logins"),
])
def test_c_violations_fail(name, mutate, needle):
    api = copy.deepcopy(C_API)
    mutate(api)
    assert any(needle in x for x in run_c(api=api)), run_c(api=api)


def test_c_ledger_and_flag_violations():
    assert any("decreased" in x for x in run_c(mqtt=[MQTT_STATUS, diag(10), diag(4)]))
    assert any("proactive_token_refresh" in x for x in run_c(mqtt=[MQTT_STATUS, diag(10, flag=False), diag(12)]))
    assert any("persisted ledger counts" in x for x in run_c(meta=dict(C_META, persisted_ledger=ledger(7))))
    assert any("missing" in x for x in run_c(meta={k: v_ for k, v_ in C_META.items() if k != "persisted_ledger"}))
    assert any("did not continue" in x for x in run_c(mqtt=[MQTT_STATUS, diag(3), diag(10)]))


def test_c_leaks_and_log_checks():
    assert any("eyJhbGci" in x for x in run_c(log=C_LOG + "token eyJhbGciOiJub25lIn0\n"))
    assert any("Traceback" in x for x in run_c(log=C_LOG + "Traceback\n"))
    assert any("did not report" in x for x in run_c(log=C_LOG.replace("refresh: 1)", "refresh: 0)")))
    assert any("succeeded" in x for x in run_c(log=logtext(True)))


# ───────────────────────────── D (default off, option absent) ─────────────────────────────
D_API = [ev(0, "POST", "/login"), ev(0.2, "GET", "/user-info", "ok"), S(1), S(63), S(125), S(187), S(249),
         S(311, auth="expired"), ev(311.1, "POST", "/refresh"), S(311.2), S(373)]
D_META = {"exit_code": 0}
D_MQTT = [MQTT_STATUS, diag(5, flag=False), diag(9, flag=False)]
D_LOG = logtext(False)


def run_d(api=D_API, log=D_LOG, mqtt=D_MQTT, meta=D_META):
    return v.check_scenario_d(api, log, mqtt, SUP, meta)


def test_correct_d_passes():
    assert run_d() == []


@pytest.mark.parametrize("name,mutate,needle", [
    ("refresh_before_401", lambda api: api.insert(7, ev(190, "POST", "/refresh")), "BEFORE the 401"),
    ("no_401_at_all", lambda api: api.__setitem__(7, S(311)), "expired-token 401"),
    ("no_replay", lambda api: api.pop(9), "no authorised replay"),
    ("replay_twice", lambda api: api.insert(10, S(311.3)), "more than once"),
    ("refresh_missing", lambda api: api.pop(8), "not followed by /refresh"),
    ("relogin", lambda api: api.append(ev(380, "POST", "/login")), "login"),
])
def test_d_violations_fail(name, mutate, needle):
    api = copy.deepcopy(D_API)
    mutate(api)
    assert any(needle in x for x in run_d(api=api)), run_d(api=api)


def test_d_flag_and_log_violations():
    assert any("proactive_token_refresh" in x for x in run_d(mqtt=[MQTT_STATUS, diag(5, flag=True)]))
    assert any("did not report" in x for x in run_d(log=logtext(True)))
    assert any("proactive refresh logged" in x for x in run_d(log=D_LOG + "[AUTH] Proactive token refresh succeeded\n"))


# ───────────────────────────── F (failed proactive refresh) ─────────────────────────────
F_API = [ev(0, "POST", "/login"), ev(0.2, "GET", "/user-info", "ok"), S(1), S(63), S(125),
         ev(187, "POST", "/refresh"), S(187.2), S(249), S(311, auth="expired"),
         ev(311.1, "POST", "/refresh"), S(311.2), S(373)]
F_LOG = logtext(True, "[AUTH] Proactive token refresh rejected; reactive refresh remains available")


F_MQTT = [MQTT_STATUS, diag(5), diag(9)]


def run_f(api=F_API, log=F_LOG, mqtt=F_MQTT, meta=D_META):
    return v.check_scenario_f(api, log, mqtt, SUP, meta)


def test_correct_f_passes():
    assert run_f() == []


@pytest.mark.parametrize("name,mutate,needle", [
    ("proactive_retried", lambda api: api.insert(8, ev(250, "POST", "/refresh")), "exactly 2 refreshes"),
    ("no_fallback_refresh", lambda api: api.pop(9), "exactly 2 refreshes"),
    ("no_401", lambda api: api.__setitem__(8, S(311)), "exactly 1 401"),
    ("replay_missing", lambda api: api.pop(10), "no authorised replay"),
    ("refresh_too_close", lambda api: api.__setitem__(5, ev(300, "POST", "/refresh")), "too closely"),
    ("relogin", lambda api: api.append(ev(380, "POST", "/login")), "re-login"),
])
def test_f_violations_fail(name, mutate, needle):
    api = copy.deepcopy(F_API)
    mutate(api)
    assert any(needle in x for x in run_f(api=api)), run_f(api=api)


def test_f_log_missing():
    assert any("rejected" in x for x in run_f(log=logtext(True)))


# ───────────────────────────── G (quota during proactive window) ─────────────────────────────
EP = {"id": 1, "rejected_responses": 1,
      "first_rejection": {"reason_class": "quota_message", "rolling_24h": {"attempted": 4}},
      "recovery": {"rejected_responses_in_episode": 1}}
G_API = [ev(0, "POST", "/login"), ev(0.2, "GET", "/user-info", "ok"), S(1),
         ev(63, "POST", "/refresh", limited=True),
         S(184, auth="expired"), ev(184.1, "POST", "/refresh"), S(184.2), S(246),
         ev(320, "POST", "/login"), ev(320.2, "GET", "/user-info", "ok"), S(321)]
G_META = {"first_exit_code": 0, "second_exit_code": 0, "api_requests_run1": 8, "run1_end_t": 250,
          "persisted_ledger": ledger(8), "persisted_episodes": [EP]}
G_LOG = logtext(True, "[AUTH] Proactive token refresh deferred: rate limited",
                "[ERROR] P/STUBSN1/CMD/MODE command NOT sent (rate limited)")
G_MQTT = [MQTT_STATUS, diag(3), diag(8), diag(10, episodes=[EP])]


def run_g(api=G_API, log=G_LOG, mqtt=G_MQTT, meta=G_META):
    return v.check_scenario_g(api, log, mqtt, SUP, meta)


def test_correct_g_passes():
    assert run_g() == []


@pytest.mark.parametrize("name,mutate,needle", [
    ("request_in_backoff", lambda api: api.insert(4, S(100)), "during quota backoff"),
    ("auth_in_backoff", lambda api: api.insert(4, ev(120, "POST", "/refresh")), "during quota backoff"),
    ("command_in_backoff", lambda api: api.insert(4, ev(120, "POST", "/off")), "command reached"),
    ("login_in_backoff", lambda api: api.insert(4, ev(90, "POST", "/login")), "during quota backoff"),
    ("proactive_refresh_after_backoff", lambda api: api.insert(4, ev(183.9, "POST", "/refresh")), "probe"),
    ("early_resume", lambda api: api.__setitem__(4, S(150, auth="expired")), "backoff"),
    ("no_replay", lambda api: api.pop(6), "no authorised replay"),
    ("limited_is_not_the_refresh", lambda api: api.__setitem__(3, S(63, limited=True)), "limited response"),
    ("relogin", lambda api: api.append(ev(260, "POST", "/login")), "logins"),
])
def test_g_violations_fail(name, mutate, needle):
    api = copy.deepcopy(G_API)
    mutate(api)
    assert any(needle in x for x in run_g(api=api)), run_g(api=api)


def test_g_evidence_violations():
    assert any("persisted episode" in x for x in run_g(meta=dict(G_META, persisted_episodes=[])))
    bad = copy.deepcopy(EP)
    bad["first_rejection"]["rolling_24h"]["attempted"] = 9
    assert any("stub saw" in x for x in run_g(meta=dict(G_META, persisted_episodes=[bad])))
    bad = copy.deepcopy(EP)
    bad["recovery"] = None
    assert any("recovery" in x for x in run_g(meta=dict(G_META, persisted_episodes=[bad])))
    assert any("published episodes" in x for x in run_g(mqtt=[MQTT_STATUS, diag(8), diag(10)]))
    assert any("decreased" in x for x in run_g(mqtt=G_MQTT + [diag(3, episodes=[EP])]))
    assert any("command NOT sent" in x or "refused" in x for x in run_g(log=logtext(True, "deferred: rate limited")))
    assert any("deferred" in x for x in run_g(log=logtext(True, "command NOT sent")))


# ───────────────────── H (Stage 2: 120 s background + demand refresh) ─────────────────────
def hdiag(n):
    return ("P/STUBSN1/diagnostics", json.dumps({"proactive_token_refresh": False, "poll_interval": 120}))


H_API = [ev(0, "POST", "/login"), ev(0.2, "GET", "/user-info", "ok"), S(1), S(38), S(158)]
H_META = {"exit_code": 0}
H_MQTT = [MQTT_STATUS, hdiag(1), hdiag(2), hdiag(3)]
H_LOG = logtext(False, "poll interval option: 120", "Polling loop starting (background=120s,")


def run_h(api=H_API, log=H_LOG, mqtt=H_MQTT, meta=H_META):
    return v.check_scenario_h(api, log, mqtt, SUP, meta)


def test_correct_h_passes():
    assert run_h() == []


@pytest.mark.parametrize("name,mutate,needle", [
    ("retained_or_burst_extra_poll", lambda api: api.insert(4, S(39)), "exactly 3"),
    ("burst_not_coalesced", lambda api: api.insert(4, S(40)), "exactly 3"),
    ("retained_caused_early_poll", lambda api: api.__setitem__(3, S(2)), "demand poll at"),
    ("demand_missing", lambda api: api.pop(3), "exactly 3"),
    ("background_not_a_full_interval", lambda api: api.__setitem__(4, S(80)), "background poll"),
    ("refresh_request_sent", lambda api: api.append(ev(50, "POST", "/refresh")), "read-only"),
    ("command_sent", lambda api: api.append(ev(50, "POST", "/off")), "read-only"),
])
def test_h_violations_fail(name, mutate, needle):
    api = copy.deepcopy(H_API)
    mutate(api)
    assert any(needle in x for x in run_h(api=api)), run_h(api=api)


def test_h_interval_and_availability_checks():
    assert any("120 s background" in x for x in run_h(log=logtext(False)))
    assert any("poll_interval" in x for x in run_h(mqtt=[MQTT_STATUS, ("P/STUBSN1/diagnostics",
               json.dumps({"proactive_token_refresh": False, "poll_interval": 60}))]))
    assert any("offline" in x for x in run_h(mqtt=H_MQTT + [("P/STUBSN1/availability", "offline")]))
