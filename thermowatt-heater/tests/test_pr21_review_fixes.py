"""Regression tests for the independent review of PR #21 (issue #17 Stage 1).

1. restored quota-episode evidence is rebuilt through strict nested allowlists
2. non-finite / absurd JWT `exp` values are rejected and cannot cause repeat refreshes
3. the ledger separates ATTEMPTED requests from answered ones (transport failures)
4. concurrency: request / token refresh / quota transitions / persistence / diagnostics
   under real threads, with lock-order recording (an observation, not a proof)
"""

import base64
import faulthandler
import itertools
import json
import random
import sys
import threading
import time
from unittest.mock import MagicMock, patch

import pytest
import requests

sys.argv = ["thermowatt_bridge.py"]

import thermowatt_bridge as bridge_module  # noqa: E402
from thermowatt_bridge import MyThermowattBridge  # noqa: E402

SERIAL = "SN"
DAILY = {"success": False, "error": "Daily request limit exceeded"}
SECRETS = ("SECRET-ACCESS", "SECRET-REFRESH", "hunter2", "SECRET-DEVICE-ID")


def _make(tmp_path, monkeypatch, config=None):
    cfg = tmp_path / "config.json"
    if config is not None:
        cfg.write_text(json.dumps(config))
    monkeypatch.setattr(bridge_module, "CONFIG_FILE", str(cfg))
    with patch("thermowatt_bridge.mqtt.Client") as mc:
        mq = MagicMock()
        mc.return_value = mq
        b = MyThermowattBridge()
        b.mqtt_client = mq
    b.config.setdefault("devices", {})[SERIAL] = {"name": "HWS", "last_setpoint": 60}
    return b


def _resp(code=200, body=None):
    r = MagicMock()
    r.status_code = code
    r.headers = {}
    r.json.return_value = body
    return r


def _full_episode(b):
    """A genuine, closed episode produced by the real code path."""
    b._enter_rate_limit("Daily request limit exceeded")
    b._rate_limit_until = time.time() - 1  # window over
    assert b._clear_rate_limit()
    return json.loads(json.dumps(b._quota_episodes))


# ───────────────────────── 1. strict nested allowlists ──────────────────────────
class TestEpisodeAllowlist:
    def test_genuine_episodes_round_trip_unchanged(self, tmp_path, monkeypatch):
        b = _make(tmp_path, monkeypatch)
        b._count_request("status")
        episodes = _full_episode(b)
        b._enter_rate_limit("HTTP 429")          # second episode left open
        episodes = json.loads(json.dumps(b._quota_episodes))
        assert len(episodes) == 2
        b2 = _make(tmp_path, monkeypatch, config={"quota_episodes": episodes})
        assert b2._quota_episodes == episodes

    @staticmethod
    def _poisoned(b):
        eps = _full_episode(b)
        ep = eps[0]
        bad = {"access_token": "SECRET-ACCESS", "password": "hunter2",
               "refresh_token": "SECRET-REFRESH", "serial": "SECRET-DEVICE-ID"}
        ep.update(bad)
        for key in ("first_rejection", "recovery"):
            ep[key].update(bad)
            ep[key]["rolling_24h"].update(bad)
            ep[key]["utc_day_counters"].update(bad)
            ep[key]["since_recovery"].update(bad)
        return eps

    def test_unexpected_fields_are_stripped_at_every_level(self, tmp_path, monkeypatch):
        b = _make(tmp_path, monkeypatch)
        eps = self._poisoned(b)
        b2 = _make(tmp_path, monkeypatch, config={"quota_episodes": eps})
        flat = json.dumps(b2._episodes_diagnostics())
        for s in SECRETS:
            assert s not in flat
        for key in ("access_token", "password", "refresh_token", "serial"):
            assert f'"{key}"' not in flat
        assert len(b2._quota_episodes) == 1                       # still restored, just cleaned

    def test_published_mqtt_payload_never_carries_them(self, tmp_path, monkeypatch):
        b = _make(tmp_path, monkeypatch)
        eps = self._poisoned(b)
        b2 = _make(tmp_path, monkeypatch, config={
            "quota_episodes": eps,
            "rate_limit": {"reason": "SECRET-ACCESS", "last_at": "hunter2",
                           "recovered_at": "SECRET-REFRESH", "step": 0, "until": 0,
                           "events_total": 1},
            "request_counters": {"day": "x", "access_token": "SECRET-ACCESS"},
        })
        b2._publish_diagnostics(SERIAL)
        payload = b2.mqtt_client.publish.call_args[0][1]
        for s in SECRETS:
            assert s not in payload

    @pytest.mark.parametrize("path,value", [
        (("first_rejection", "at"), "SECRET-ACCESS"),                 # free text where a time belongs
        (("first_rejection", "reason_class"), "SECRET-ACCESS"),
        (("first_rejection", "rolling_24h", "status"), "SECRET-ACCESS"),
        (("first_rejection", "rolling_24h", "status"), True),         # bool is not a count
        (("first_rejection", "rolling_24h", "status"), -1),
        (("first_rejection", "rolling_24h", "status"), float("nan")),
        (("first_rejection", "rolling_24h", "complete"), "yes"),
        (("first_rejection", "utc_day"), {"a": "SECRET-ACCESS"}),
        (("first_rejection", "since_recovery"), "SECRET-ACCESS"),
        (("first_rejection", "utc_day_counters"), ["SECRET-ACCESS"]),
        (("recovery", "at"), {"x": 1}),
        (("recovery", "episode_duration_s"), "SECRET-ACCESS"),
        (("recovery", "episode_duration_s"), float("inf")),
        (("recovery", "rejected_responses_in_episode"), 1.5),
        (("id",), "SECRET-ACCESS"),
        (("rejected_responses",), None),
        (("first_rejection",), "SECRET-ACCESS"),
        (("recovery",), ["SECRET-ACCESS"]),
    ])
    def test_malformed_values_drop_the_episode_not_republish_it(self, tmp_path, monkeypatch, path, value):
        b = _make(tmp_path, monkeypatch)
        eps = _full_episode(b)
        node = eps[0]
        for k in path[:-1]:
            node = node[k]
        node[path[-1]] = value
        # json round trip like a real config file (NaN/Infinity survive as Python's json extension)
        b2 = _make(tmp_path, monkeypatch, config={"quota_episodes": eps})
        assert b2._quota_episodes == []
        assert "SECRET-ACCESS" not in json.dumps(b2._episodes_diagnostics())

    def test_non_dict_and_non_list_inputs(self, tmp_path, monkeypatch):
        for junk in ("SECRET-ACCESS", 5, {"a": 1}, [None, 3, "SECRET-ACCESS", [1]]):
            b = _make(tmp_path, monkeypatch, config={"quota_episodes": junk})
            assert b._quota_episodes == []

    def test_reason_class_outside_the_allowlist_is_rejected(self, tmp_path, monkeypatch):
        b = _make(tmp_path, monkeypatch)
        eps = _full_episode(b)
        eps[0]["first_rejection"]["reason_class"] = "http_500"
        assert _make(tmp_path, monkeypatch, config={"quota_episodes": eps})._quota_episodes == []

    def test_history_cap_survives_restore(self, tmp_path, monkeypatch):
        b = _make(tmp_path, monkeypatch)
        eps = []
        for _ in range(MyThermowattBridge.QUOTA_EPISODE_HISTORY + 4):
            b._quota_episodes = []
            eps.append(_full_episode(b)[0])
        for i, e in enumerate(eps):
            e["id"] = i + 1
        b2 = _make(tmp_path, monkeypatch, config={"quota_episodes": eps})
        assert len(b2._quota_episodes) == MyThermowattBridge.QUOTA_EPISODE_HISTORY
        assert b2._quota_episodes[-1]["id"] == len(eps)

    def test_restored_rate_limit_labels_are_fixed_shapes(self, tmp_path, monkeypatch):
        b = _make(tmp_path, monkeypatch, config={"rate_limit": {
            "reason": "HTTP 429", "last_at": "2026-11-07T18:48:20+00:00",
            "recovered_at": "2026-11-06T19:50:00+00:00", "step": 0, "until": 0, "events_total": 2}})
        assert b._rate_limit_reason == "HTTP 429"
        assert b._rate_limit_last_at == "2026-11-07T18:48:20+00:00"
        assert b._rate_limit_recovered_at == "2026-11-06T19:50:00+00:00"


# ───────────────────────────── 2. JWT exp hardening ─────────────────────────────
def _raw_jwt(payload_bytes):
    enc = lambda raw: base64.urlsafe_b64encode(raw).decode().rstrip("=")  # noqa: E731
    return f"{enc(b'{}')}.{enc(payload_bytes)}.SIG"


BAD_EXP = [b'{"exp": NaN}', b'{"exp": Infinity}', b'{"exp": -Infinity}', b'{"exp": 1e999}',
           b'{"exp": -1e999}', b'{"exp": ' + b"9" * 500 + b'}', b'{"exp": 9007199254740993}',
           b'{"exp": 32503680001}', b'{"exp": 0}', b'{"exp": -5}', b'{"exp": true}',
           b'{"exp": null}', b'{"exp": "123"}', b'{"exp": [1]}', b'{}', b'[]', b'not json',
           b'{"exp": 1, "exp": NaN}']


class TestJwtExp:
    @pytest.mark.parametrize("payload", BAD_EXP)
    def test_rejected(self, payload):
        assert MyThermowattBridge._jwt_exp(_raw_jwt(payload)) is None

    @pytest.mark.parametrize("payload,expected", [
        (b'{"exp": 1800000000}', 1800000000.0), (b'{"exp": 1800000000.5}', 1800000000.5)])
    def test_valid(self, payload, expected):
        assert MyThermowattBridge._jwt_exp(_raw_jwt(payload)) == expected

    @pytest.mark.parametrize("payload", [b'{"exp": NaN}', b'{"exp": Infinity}', b'{"exp": 1e999}'])
    def test_non_finite_exp_cannot_cause_refresh_attempts(self, tmp_path, monkeypatch, payload):
        """NaN != NaN used to defeat the once-per-token guard (repeat refresh each poll)."""
        monkeypatch.setattr(MyThermowattBridge, "PROACTIVE_REFRESH", True)
        b = _make(tmp_path, monkeypatch)
        b.config["access_token"] = _raw_jwt(payload)
        b.config["refresh_token"] = "SECRET-REFRESH"
        b.refresh_session = MagicMock(return_value=True)
        for _ in range(50):
            b._maybe_proactive_refresh()
        b.refresh_session.assert_not_called()

    def test_original_nan_defect_is_real_without_the_guards(self):
        # documents why: NaN never equals itself, so an equality guard cannot stop a repeat
        nan = float("nan")
        assert (nan == nan) is False and (nan - 1e9 > 120) is False

    def test_short_lived_tokens_cannot_cause_a_refresh_per_poll(self, tmp_path, monkeypatch):
        """A server issuing tokens that are always within the margin: bounded by the floor."""
        monkeypatch.setattr(MyThermowattBridge, "PROACTIVE_REFRESH", True)
        now = [1_000_000.0]
        monkeypatch.setattr(bridge_module.time, "time", lambda: now[0])
        b = _make(tmp_path, monkeypatch)
        counter = itertools.count()

        def fake_refresh():
            b.config["access_token"] = _raw_jwt(json.dumps({"exp": now[0] + 60 + next(counter)}).encode())
            return True
        b.config["access_token"] = _raw_jwt(json.dumps({"exp": now[0] + 30}).encode())
        b.config["refresh_token"] = "SECRET-REFRESH"
        b.refresh_session = MagicMock(side_effect=fake_refresh)
        polls = 200
        for _ in range(polls):
            now[0] += 60
            b._maybe_proactive_refresh()
        # 12000 s of 60 s polls: at most one per 600 s (+1), never one per poll
        assert b.refresh_session.call_count <= 12000 // 600 + 1
        assert b.refresh_session.call_count >= 1


# ───────────────── 3. attempted vs answered (transport failures) ─────────────────
class TestAttemptedVersusAnswered:
    def _bridge(self, tmp_path, monkeypatch, behaviours):
        b = _make(tmp_path, monkeypatch)
        it = iter(behaviours)

        def step():
            nxt = next(it)
            if isinstance(nxt, Exception):
                raise nxt
            return nxt
        b.session = MagicMock()
        b.session.request.side_effect = lambda method, url, **kw: step()
        b.session.post.side_effect = lambda url, **kw: step()
        b.session.headers = {}
        return b

    def test_transport_failure_is_attempted_but_not_answered(self, tmp_path, monkeypatch):
        b = self._bridge(tmp_path, monkeypatch, [requests.exceptions.ConnectionError("reset")])
        with pytest.raises(requests.exceptions.ConnectionError):
            b.request("GET", "/status", serial=SERIAL)
        roll = b._requests_diagnostics()["rolling_24h"]
        assert (roll["status"], roll["attempted"], roll["transport_failed"],
                roll["responses_received"], roll["rate_limited_responses"]) == (1, 1, 1, 0, 0)

    def test_mixed_outcomes_are_separated(self, tmp_path, monkeypatch):
        b = self._bridge(tmp_path, monkeypatch, [
            _resp(200, {"result": {}}), requests.exceptions.ReadTimeout("t"),
            _resp(200, DAILY), requests.exceptions.ConnectionError("x")])
        for _ in range(4):
            try:
                b.request("GET", "/status", serial=SERIAL)
            except requests.exceptions.RequestException:
                pass
            b._rate_limit_until = 0.0     # let the loop continue past the quota window
        roll = b._requests_diagnostics()["rolling_24h"]
        assert roll["attempted"] == 4
        assert roll["transport_failed"] == 2
        assert roll["responses_received"] == 2
        assert roll["rate_limited_responses"] == 1
        # nothing in the diagnostics claims a vendor-accepted count
        assert "accepted" not in json.dumps(b._requests_diagnostics()).lower()

    def test_auth_transport_failures_are_counted_as_attempted_auth(self, tmp_path, monkeypatch):
        b = self._bridge(tmp_path, monkeypatch, [requests.exceptions.ConnectTimeout("t")])
        b.config["refresh_token"] = "SECRET-REFRESH"
        with pytest.raises(requests.exceptions.ConnectTimeout):
            b.refresh_session()
        roll = b._requests_diagnostics()["rolling_24h"]
        assert (roll["auth"], roll["transport_failed"], roll["responses_received"]) == (1, 1, 0)

    def test_a_transport_failure_is_never_a_quota_event(self, tmp_path, monkeypatch):
        b = self._bridge(tmp_path, monkeypatch, [requests.exceptions.ConnectionError("x")] * 3)
        for _ in range(3):
            with pytest.raises(requests.exceptions.ConnectionError):
                b.request("GET", "/status", serial=SERIAL)
        assert b._quota_episodes == [] and b._rate_limit_remaining() == 0

    def test_snapshot_records_transport_failures_and_attempted_wording(self, tmp_path, monkeypatch):
        b = self._bridge(tmp_path, monkeypatch, [requests.exceptions.ConnectionError("x"),
                                                 _resp(200, DAILY)])
        with pytest.raises(requests.exceptions.ConnectionError):
            b.request("GET", "/status", serial=SERIAL)
        b.request("GET", "/status", serial=SERIAL)
        snap = b._quota_episodes[0]["first_rejection"]
        assert snap["rolling_24h"]["attempted"] == 2
        assert snap["rolling_24h"]["transport_failed"] == 1
        assert snap["since_recovery"]["attempted"] == 2
        assert "sent" not in snap["since_recovery"]

    def test_transport_failures_persist_and_legacy_rows_load(self, tmp_path, monkeypatch):
        b = self._bridge(tmp_path, monkeypatch, [requests.exceptions.ConnectionError("x")])
        with pytest.raises(requests.exceptions.ConnectionError):
            b.request("GET", "/status", serial=SERIAL)
        b._maybe_flush_counters(force=True)
        saved = json.loads(open(bridge_module.CONFIG_FILE).read())
        b2 = _make(tmp_path, monkeypatch, config=saved)
        assert b2._requests_diagnostics()["rolling_24h"]["transport_failed"] == 1
        # a 6-wide row written before the field existed is padded, not discarded
        led = saved["request_ledger"]
        key = next(iter(led["buckets"]))
        led["buckets"][key] = led["buckets"][key][:6]
        b3 = _make(tmp_path, monkeypatch, config=saved)
        assert b3._requests_diagnostics()["rolling_24h"]["status"] == 1


# ───────────────────────────── 4. concurrency ─────────────────────────────
class _Registry:
    def __init__(self):
        self.edges = set()
        self.local = threading.local()
        self.guard = threading.Lock()

    def stack(self):
        if not hasattr(self.local, "s"):
            self.local.s = []
        return self.local.s

    def on_acquire(self, name):
        st = self.stack()
        with self.guard:
            for held in set(st):
                if held != name:
                    self.edges.add((held, name))
        st.append(name)

    def on_release(self, name):
        st = self.stack()
        for i in range(len(st) - 1, -1, -1):
            if st[i] == name:
                del st[i]
                return

    def holds(self, name):
        return name in self.stack()


class _Tracked:
    def __init__(self, name, real, reg):
        self._name, self._real, self._reg = name, real, reg

    def acquire(self, *a, **k):
        ok = self._real.acquire(*a, **k)
        if ok:
            self._reg.on_acquire(self._name)
        return ok

    def release(self):
        self._reg.on_release(self._name)
        self._real.release()

    __enter__ = lambda self: (self.acquire(), self)[1]            # noqa: E731
    __exit__ = lambda self, *a: self.release()                    # noqa: E731

    def locked(self):
        return self._real.locked()


def _has_cycle(edges):
    graph = {}
    for a, b in edges:
        graph.setdefault(a, set()).add(b)
    state = {}

    def visit(n):
        state[n] = 1
        for m in graph.get(n, ()):
            if state.get(m) == 1 or (state.get(m) is None and visit(m)):
                return True
        state[n] = 2
        return False
    return any(state.get(n) is None and visit(n) for n in list(graph))


def _jwt_exp_at(exp):
    return _raw_jwt(json.dumps({"exp": exp}).encode())


class TestConcurrency:
    """Real threads against the real request / refresh / quota / persistence code.

    Passing is evidence of no deadlock or lost update in THESE interleavings; it is not
    a proof. The lock-order graph is what was observed, and the documented order is
    asserted against it.
    """

    DOCUMENTED_ORDER = {("_http_lock", "_config_lock"), ("_http_lock", "_rate_limit_lock"),
                        ("_config_lock", "_rate_limit_lock")}

    def _storm(self, tmp_path, monkeypatch, seed, seconds=2.0):
        monkeypatch.setattr(MyThermowattBridge, "PROACTIVE_REFRESH", True)
        monkeypatch.setattr(MyThermowattBridge, "RATE_LIMIT_BASE_BACKOFF", 0.002)
        monkeypatch.setattr(MyThermowattBridge, "RATE_LIMIT_MAX_BACKOFF", 0.01)
        b = _make(tmp_path, monkeypatch)
        reg = _Registry()
        for name in ("_http_lock", "_config_lock", "_rate_limit_lock", "_command_lock"):
            setattr(b, name, _Tracked(name, getattr(b, name), reg))
        rng, rng_lock = random.Random(seed), threading.Lock()
        io_under_lock = []
        counter = itertools.count()

        def roll():
            with rng_lock:
                return rng.random()

        def check_io():
            for name in ("_rate_limit_lock", "_config_lock"):
                if reg.holds(name):
                    io_under_lock.append(name)

        class Session:
            headers = {}

            def request(self, method, url, **kw):
                check_io()
                x = roll()
                if x < .08:
                    raise requests.exceptions.ConnectionError("reset")
                if x < .16:
                    return _resp(200, DAILY)
                if x < .20:
                    return _resp(429, None)
                if x < .30:
                    return _resp(401, {"error": "expired"})
                return _resp(200, {"result": {"termostati": []}})

            def post(self, url, json=None, **kw):  # noqa: A002
                check_io()
                if roll() < .1:
                    raise requests.exceptions.ReadTimeout("t")
                return _resp(200, {"result": {
                    "accessToken": _jwt_exp_at(time.time() + 100 + next(counter) % 3),
                    "refreshToken": "SECRET-REFRESH"}})
        b.session = Session()
        b.config["access_token"] = _jwt_exp_at(time.time() + 100)
        b.config["refresh_token"] = "SECRET-REFRESH"

        stop = threading.Event()
        errors = []

        def loop(fn):
            def run():
                try:
                    while not stop.is_set():
                        fn()
                except Exception as e:  # noqa: BLE001
                    if not isinstance(e, (requests.exceptions.RequestException,
                                          bridge_module.RateLimitedError)):
                        errors.append(repr(e))
                        return
            return run

        def poller():
            try:
                b.request("GET", "/status", serial=SERIAL)
            except (requests.exceptions.RequestException, bridge_module.RateLimitedError):
                pass
            b._maybe_proactive_refresh()

        def commander():                      # MQTT-thread style command POST
            try:
                b.request("POST", "/mode", serial=SERIAL, json={})
            except (requests.exceptions.RequestException, bridge_module.RateLimitedError):
                pass

        def refresher():
            try:
                b.refresh_session()
            except (requests.exceptions.RequestException, bridge_module.RateLimitedError):
                pass

        def clearer():
            gen = b._rate_limit_gen()
            b._rate_limit_until = 0.0 if b._rate_limit_remaining() < 0.005 else b._rate_limit_until
            b._clear_rate_limit(gen)
            time.sleep(0.0005)

        def flusher():
            b._maybe_flush_counters(force=True)
            time.sleep(0.0005)

        def publisher():
            b._publish_diagnostics(SERIAL)

        threads = [threading.Thread(target=loop(f), daemon=True)
                   for f in (poller, poller, commander, refresher, clearer, flusher, publisher)]
        for t in threads:
            t.start()
        time.sleep(seconds)
        stop.set()
        deadline = time.time() + 20          # one shared budget, so a deadlock fails fast
        for t in threads:
            t.join(timeout=max(0.0, deadline - time.time()))
        alive = [t for t in threads if t.is_alive()]
        if alive:
            faulthandler.dump_traceback(all_threads=True)
        assert not alive, "deadlock or livelock: threads did not stop"
        assert not errors, errors
        return b, reg, io_under_lock

    @pytest.mark.parametrize("seed", [1, 2, 3, 4, 5])
    def test_storm_no_deadlock_consistent_state_and_lock_order(self, tmp_path, monkeypatch, seed):
        b, reg, io_under_lock = self._storm(tmp_path, monkeypatch, seed)
        # lock order: acyclic and within the documented order
        assert not _has_cycle(reg.edges), reg.edges
        assert {(a, b_) for a, b_ in reg.edges} <= self.DOCUMENTED_ORDER, reg.edges
        # no network I/O while holding the state locks
        assert io_under_lock == []
        # counters: internally consistent, ledger and day counters agree
        c = b._request_counters
        assert c["total"] == c["status"] + c["command"] + c["auth"] + c["other"]
        roll = b._requests_diagnostics()["rolling_24h"]
        assert roll["total"] == c["total"] and roll["rate_limited_responses"] == c["rate_limited_responses"]
        assert roll["blocked"] == c["blocked"]
        assert 0 <= roll["transport_failed"] <= roll["attempted"]
        # episodes: unique ids, only the newest may be open, closed ones carry a recovery
        eps = b._quota_episodes
        assert len({e["id"] for e in eps}) == len(eps)
        assert all(e["recovery"] is not None for e in eps[:-1])
        for e in eps:
            assert MyThermowattBridge._sanitize_episode(json.loads(json.dumps(e))) == e
        # persisted file is valid JSON and restorable
        b._maybe_flush_counters(force=True)
        saved = json.loads(open(bridge_module.CONFIG_FILE).read())
        b2 = _make(tmp_path, monkeypatch, config=saved)
        assert b2._quota_episodes == eps
        assert "SECRET" not in b.mqtt_client.publish.call_args[0][1]

    def test_the_storm_actually_exercises_the_paths(self, tmp_path, monkeypatch):
        b, reg, _ = self._storm(tmp_path, monkeypatch, seed=11)
        roll = b._requests_diagnostics()["rolling_24h"]
        assert roll["attempted"] > 200
        assert roll["auth"] > 5 and roll["transport_failed"] > 5
        assert roll["rate_limited_responses"] > 5 and b._rate_limit_events_total > 2
        assert len(b._quota_episodes) >= 1
        assert {("_http_lock", "_config_lock"), ("_config_lock", "_rate_limit_lock"),
                ("_http_lock", "_rate_limit_lock")} <= reg.edges

    def test_lock_checker_detects_an_inverted_order(self):
        # the detector itself: a deliberate A->B and B->A pair must be reported as a cycle
        assert _has_cycle({("a", "b"), ("b", "a")})
        assert not _has_cycle({("a", "b"), ("b", "c"), ("a", "c")})

    def test_a_late_poll_cannot_clear_a_newer_rejection(self, tmp_path, monkeypatch):
        b = _make(tmp_path, monkeypatch)
        gen = b._rate_limit_gen()
        b._enter_rate_limit("Daily request limit exceeded")      # rejection after the poll began
        b._rate_limit_until = 0.0
        assert b._clear_rate_limit(gen) is False
        assert b._quota_episodes[-1]["recovery"] is None
