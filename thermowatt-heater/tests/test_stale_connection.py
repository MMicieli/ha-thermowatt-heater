"""Issue #23: a keep-alive connection closed by the peer must not be reused for the next request.

All traffic is local (127.0.0.1) against ``stale_conn_harness``: a keep-alive server that closes
idle connections and a relay that delays the FIN (one-way latency).  Times are scaled down
(server idle timeout 0.6 s, FIN delay 0.2 s); the failure window is client idle ~0.4-0.6 s.
Runs with the repository's pinned requests/urllib3.
"""
import sys
import time
from unittest.mock import MagicMock, patch

import pytest
import requests

sys.argv = ["thermowatt_bridge.py"]
import thermowatt_bridge as module
from thermowatt_bridge import MyThermowattBridge, _IdleSafeAdapter, RateLimitedError

from tests.stale_conn_harness import DelayedFinProxy, IdleClosingServer

T, D = 0.6, 0.2          # simulated server idle timeout / one-way FIN delay
IN_WINDOW = 0.5          # client idle that lands inside the stale-connection window
GUARD_S = 0.3            # adapter threshold used in tests (< IN_WINDOW, as 30 s < 60 s in production)
NEVER = 1e9              # adapter threshold that disables the guard (= the pre-fix behaviour)


@pytest.fixture
def rig():
    servers = []

    def make(**kw):
        srv = IdleClosingServer(T, **kw)
        px = DelayedFinProxy(srv.port, D)
        servers.append((srv, px))
        return srv, f"http://127.0.0.1:{px.port}"

    yield make
    for srv, px in servers:
        srv.close()
        px.close()


@pytest.fixture
def make_bridge(monkeypatch, tmp_path):
    def make(base_url, max_idle):
        monkeypatch.setattr(module, "CONFIG_FILE", str(tmp_path / "bridge.json"))
        with patch("thermowatt_bridge.mqtt.Client") as factory:
            factory.return_value = MagicMock()
            b = MyThermowattBridge()
        b.BASE_URL = base_url
        b.session.get_adapter(base_url + "/").max_idle_s = max_idle
        b.session.headers["Authorization"] = "Bearer test-token"
        return b
    return make


def poll(bridge, n, idle=IN_WINDOW, method="GET", endpoint="/status", **kw):
    """n requests spaced ``idle`` seconds apart; returns the outcome string, e.g. 'SFSF'."""
    out = []
    for _ in range(n):
        try:
            bridge.request(method, endpoint, serial="SN", **kw)
            out.append("S")
        except requests.exceptions.ConnectionError as e:
            assert "RemoteDisconnected" in repr(e)
            out.append("F")
        time.sleep(idle)
    return "".join(out)


# --------------------------------------------------------------------------- the failure itself
def test_plain_session_reproduces_remote_disconnected_in_the_window(rig):
    srv, url = rig()
    s = requests.Session()
    out = ""
    for _ in range(6):
        try:
            s.get(url + "/status", timeout=(2, 2))
            out += "S"
        except requests.exceptions.ConnectionError as e:
            assert "RemoteDisconnected" in repr(e)
            out += "F"
        time.sleep(IN_WINDOW)
    assert out.count("F") >= 2
    assert "FF" not in out            # a failed connection is discarded, so the next request succeeds
    assert srv.connections == out.count("S")          # each failure is followed by a fresh connection


def test_urllib3_already_copes_when_the_close_is_visible_before_reuse(rig):
    # Outside the in-flight-FIN window the stock pool replaces the connection by itself, so the
    # failure is a race and the fix only has to cover idle ages where that check is blind.
    srv, url = rig()
    s = requests.Session()
    for _ in range(4):
        assert s.get(url + "/status", timeout=(2, 2)).status_code == 200
        time.sleep(T + D + 0.3)
    assert srv.connections == 4


# --------------------------------------------------------------------------- the fix
def test_bridge_without_guard_fails_and_with_guard_does_not(rig, make_bridge):
    srv_a, url_a = rig()
    unguarded = make_bridge(url_a, NEVER)
    out_a = poll(unguarded, 6)
    assert out_a.count("F") >= 2 and "FF" not in out_a
    assert unguarded._request_counters["status"] == 6          # failed attempts are still counted
    assert srv_a.requests and len(srv_a.requests) == out_a.count("S")

    srv_b, url_b = rig()
    guarded = make_bridge(url_b, GUARD_S)
    assert poll(guarded, 6) == "SSSSSS"
    assert len(srv_b.requests) == 6          # exactly one vendor request per attempt: no retry, no extra
    assert srv_b.connections == 6            # every poll used a fresh connection
    assert guarded._request_counters["status"] == 6
    assert guarded._request_counters["total"] == 6


def test_guarded_transport_failure_ledger_has_no_failures(rig, make_bridge):
    _, url = rig()
    b = make_bridge(url, GUARD_S)
    poll(b, 4)
    assert sum(row[b.LEDGER_FIELDS.index("transport_failed")] for row in b._ledger.values()) == 0


def test_unguarded_failure_is_a_transport_failure_in_the_ledger(rig, make_bridge):
    _, url = rig()
    b = make_bridge(url, NEVER)
    out = poll(b, 4)
    failed = sum(row[b.LEDGER_FIELDS.index("transport_failed")] for row in b._ledger.values())
    assert failed == out.count("F") >= 1


def test_short_idle_keeps_reusing_the_connection(rig, make_bridge):
    # Command-confirmation (20 s) and demand-retry (5 s) spacing stay below the threshold in
    # production; here 0.1 s < 0.3 s. The guard must not turn every request into a new connection.
    srv, url = rig()
    b = make_bridge(url, GUARD_S)
    assert poll(b, 5, idle=0.1) == "SSSSS"
    assert srv.connections == 1


def test_session_headers_and_cookies_survive_fresh_connections(rig, make_bridge):
    srv, url = rig()
    b = make_bridge(url, GUARD_S)
    assert poll(b, 4) == "SSSS"
    assert srv.connections == 4
    for req in srv.requests:
        assert req["headers"]["authorization"] == "Bearer test-token"
        assert req["headers"]["seriale"] == "SN"
    # Load-balancer stickiness cookie from response 1 is replayed on every later (new) connection.
    assert "cookie" not in srv.requests[0]["headers"]
    for req in srv.requests[1:]:
        assert req["headers"]["cookie"] == "AWSALB=sticky-1"
    assert b.session.cookies.get("AWSALB") == "sticky-1"


def test_login_and_refresh_also_get_fresh_connections(rig, make_bridge):
    body = {"result": {"accessToken": "a", "refreshToken": "r"}}
    srv, url = rig(body=body)
    b = make_bridge(url, GUARD_S)
    b._update_auth = MagicMock()
    b.config["device_uuid"] = "u"
    b.config["refresh_token"] = "r"
    for call in (b.login, b.refresh_session, b.login, b.refresh_session):
        call()
        time.sleep(IN_WINDOW)
    assert len(srv.requests) == 4 and srv.connections == 4
    assert [r["path"] for r in srv.requests] == ["/login", "/refresh", "/login", "/refresh"]
    assert b._request_counters["auth"] == 4


def test_command_post_is_sent_exactly_once_and_never_replayed(rig, make_bridge):
    # Guarded: the command after a long idle goes out on a fresh connection, once.
    srv, url = rig()
    b = make_bridge(url, GUARD_S)
    assert poll(b, 4, method="POST", endpoint="/command", json={"x": 1}) == "SSSS"
    assert len(srv.requests) == 4 and all(r["method"] == "POST" for r in srv.requests)
    assert b._request_counters["command"] == 4

    # Unguarded (pre-fix): a command that hits the stale connection is NOT silently retried.
    srv2, url2 = rig()
    c = make_bridge(url2, NEVER)
    out = poll(c, 4, method="POST", endpoint="/command", json={"x": 1})
    assert out.count("F") >= 1
    assert len(srv2.requests) == out.count("S")             # failed attempts were not re-sent
    assert c._request_counters["command"] == 4              # ...but they were counted as attempts
    assert c._io.transport_failed is True


def test_rate_limit_still_fails_closed_without_io(rig, make_bridge):
    srv, url = rig(status=429, body={"error": "Daily request limit exceeded"})
    b = make_bridge(url, GUARD_S)
    b.request("GET", "/status", serial="SN")
    assert b._rate_limit_remaining() > 0
    seen, conns = len(srv.requests), srv.connections
    time.sleep(IN_WINDOW)
    with pytest.raises(RateLimitedError):
        b.request("GET", "/status", serial="SN")
    assert (len(srv.requests), srv.connections) == (seen, conns)     # no I/O during backoff


def test_recovery_after_a_real_transport_failure(rig, make_bridge):
    # Even with the guard, a genuine failure (nothing listening) is raised once, counted as an
    # attempt and a transport failure, never retried, and the next request recovers.
    srv, url = rig()
    b = make_bridge(url, GUARD_S)
    assert poll(b, 1, idle=0) == "S"
    dead = "http://127.0.0.1:9"                      # discard port: connection refused
    b.BASE_URL = dead
    with pytest.raises(requests.exceptions.ConnectionError):
        b.request("GET", "/status", serial="SN")
    assert b._io.transport_failed is True
    assert b._request_counters["status"] == 2        # one attempt, not two (no retry)
    assert sum(r[b.LEDGER_FIELDS.index("transport_failed")] for r in b._ledger.values()) == 1
    b.BASE_URL = url
    assert poll(b, 1, idle=0) == "S"
    assert b._request_counters["status"] == 3
    assert len(srv.requests) == 2


# --------------------------------------------------------------------------- adapter unit tests
class _Pool:
    def __init__(self):
        self.cleared = 0

    def clear(self):
        self.cleared += 1


def _adapter(max_idle=30):
    a = _IdleSafeAdapter(max_idle)
    a.poolmanager = _Pool()
    a.proxy_manager = {"p": _Pool()}
    return a


def test_adapter_first_request_and_recent_reuse_keep_the_pool():
    a = _adapter()
    with patch.object(requests.adapters.HTTPAdapter, "send", return_value="r") as send:
        a.send("req", timeout=(5, 15), verify=True)
        a.send("req2", timeout=(5, 15), verify=True)
    assert a.poolmanager.cleared == 0
    assert send.call_args.kwargs == {"timeout": (5, 15), "verify": True}


def test_adapter_clears_every_pool_when_idle_exceeds_threshold(monkeypatch):
    a = _adapter(30)
    clock = [1000.0]
    monkeypatch.setattr(module.time, "monotonic", lambda: clock[0])
    with patch.object(requests.adapters.HTTPAdapter, "send", return_value="r"):
        a.send("req")
        clock[0] += 30.0            # exactly the threshold: still reusable
        a.send("req")
        assert a.poolmanager.cleared == 0
        clock[0] += 30.5            # beyond it: discarded
        a.send("req")
    assert a.poolmanager.cleared == 1 and a.proxy_manager["p"].cleared == 1


def test_adapter_records_idle_start_even_when_the_request_raises():
    a = _adapter()
    with patch.object(requests.adapters.HTTPAdapter, "send", side_effect=requests.ConnectionError("x")):
        with pytest.raises(requests.ConnectionError):
            a.send("req")
    assert a._last_done is not None


def test_constants_cadence_and_mounting(make_bridge):
    assert module.HTTP_MAX_IDLE_S == 30
    assert 20 < module.HTTP_MAX_IDLE_S < MyThermowattBridge.POLL_INTERVAL == 60
    assert MyThermowattBridge.POLL_INTERVAL_CONFIRM == 20
    assert module.HTTP_TIMEOUT == (5, 15)
    b = make_bridge("http://127.0.0.1:1", GUARD_S)
    assert isinstance(b.session.get_adapter("https://example.invalid/x"), _IdleSafeAdapter)
    assert isinstance(b.session.get_adapter("http://example.invalid/x"), _IdleSafeAdapter)
    assert b.session.get_adapter("https://a/") is b.session.get_adapter("http://b/")  # one shared idle clock
