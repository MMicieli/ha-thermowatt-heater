"""Regression tests for issue #17 Stage 1b: proactive access-token refresh.

Independent of the observability tests. Proves the refresh happens only when it is
safe and useful, and that the existing fail-closed rate-limit and command-safety
guarantees are unchanged:

- never any request during an active quota backoff, nor while recovery is unverified
- at most one attempt per token (no refresh-per-cycle loop), failures are swallowed
- the reactive 401 -> refresh -> replay path remains the fallback
- not used on the command path
- tokens never appear in log output
- reduces requests per token lifetime from 3 (reactive) to 2 (refresh + status)
"""

import base64
import json
import sys
from unittest.mock import MagicMock, patch

import pytest

sys.argv = ["thermowatt_bridge.py"]

import thermowatt_bridge as bridge_module  # noqa: E402
from thermowatt_bridge import MyThermowattBridge, RateLimitedError  # noqa: E402

DAILY = {"success": False, "error": "Daily request limit exceeded"}
T0 = 1_000_000.0
LIFETIME = 1800
SERIAL = "SN"
REFRESH_SECRET = "SECRET-REFRESH-TOKEN"


def _jwt(exp, **extra):
    def enc(obj):
        return base64.urlsafe_b64encode(json.dumps(obj).encode()).decode().rstrip("=")
    claims = {"exp": exp, "iat": exp - LIFETIME}
    claims.update(extra)
    return f"{enc({'alg': 'none'})}.{enc(claims)}.SIGNATURE-SECRET"


class FakeClock:
    def __init__(self, now=T0):
        self.now = now

    def __call__(self):
        return self.now


@pytest.fixture
def clock(monkeypatch):
    c = FakeClock()
    monkeypatch.setattr(bridge_module.time, "time", c)
    return c


@pytest.fixture
def bridge(tmp_path, monkeypatch, clock):
    monkeypatch.setattr(bridge_module, "CONFIG_FILE", str(tmp_path / "config.json"))
    # The feature is default-OFF (see test_proactive_config.py); these tests exercise it enabled.
    monkeypatch.setattr(MyThermowattBridge, "PROACTIVE_REFRESH", True)
    with patch("thermowatt_bridge.mqtt.Client") as mock_client:
        mqtt_client = MagicMock()
        mock_client.return_value = mqtt_client
        instance = MyThermowattBridge()
        instance.mqtt_client = mqtt_client
    instance.config["devices"][SERIAL] = {"name": "HWS", "last_setpoint": 60}
    instance.config["refresh_token"] = REFRESH_SECRET
    return instance


def _resp(code=200, body=None):
    r = MagicMock()
    r.status_code = code
    r.headers = {}
    r.json.return_value = body
    return r


def _good_status():
    return _resp(200, {"result": {"WaterHeaterSts": 1, "T_Avg": 55.0, "T_SetPoint": 60, "Cmd": 9}})


def _set_token(bridge, exp):
    bridge.config["access_token"] = _jwt(exp)


class FakeCloud:
    """Scripted session: 401 for an expired access token, 200 otherwise.

    /refresh issues a new token valid for LIFETIME seconds. Every HTTP call is
    recorded so request counts can be compared between behaviours.
    """

    def __init__(self, clock):
        self.clock = clock
        self.headers = {}
        self.calls = []

    def _token_valid(self):
        auth = self.headers.get("Authorization", "")
        exp = MyThermowattBridge._jwt_exp(auth.replace("Bearer ", ""))
        return exp is not None and exp > self.clock.now

    def request(self, method, url, **kwargs):
        endpoint = url.rsplit("/", 1)[-1]
        self.calls.append((method, endpoint))
        if not self._token_valid():
            return _resp(401, {"error": "expired"})
        return _good_status()

    def post(self, url, json=None, **kwargs):  # noqa: A002 - mirrors requests.Session.post
        endpoint = url.rsplit("/", 1)[-1]
        self.calls.append(("POST", endpoint))
        assert endpoint == "refresh"
        return _resp(200, {"result": {"accessToken": _jwt(self.clock.now + LIFETIME),
                                      "refreshToken": REFRESH_SECRET}})


def _cloud_bridge(bridge, clock, first_exp=None):
    cloud = FakeCloud(clock)
    bridge.session = cloud
    bridge._update_auth(_jwt(first_exp if first_exp is not None else clock.now + LIFETIME), REFRESH_SECRET)
    return cloud


def _count(cloud, endpoint):
    return sum(1 for _, e in cloud.calls if e == endpoint)


# ───────────────────────────── when it refreshes ─────────────────────────────
class TestWhenItRefreshes:
    def test_no_refresh_while_token_has_plenty_of_life(self, bridge, clock):
        _set_token(bridge, T0 + bridge.PROACTIVE_REFRESH_MARGIN_S + 1)
        bridge.refresh_session = MagicMock()
        bridge.request = MagicMock(return_value=_good_status())
        bridge._run_poll_cycle()
        bridge.refresh_session.assert_not_called()
        bridge.request.assert_called_once()                    # normal poll unaffected

    def test_refreshes_at_the_margin_before_the_poll(self, bridge, clock):
        _set_token(bridge, T0 + bridge.PROACTIVE_REFRESH_MARGIN_S)
        order = MagicMock()
        bridge.refresh_session = order.refresh
        bridge.refresh_session.return_value = True
        bridge.request = order.request
        bridge.request.return_value = _good_status()
        bridge._run_poll_cycle()
        assert [c[0] for c in order.mock_calls[:2]] == ["refresh", "request"]

    def test_already_expired_token_is_refreshed_before_polling(self, bridge, clock):
        _set_token(bridge, T0 - 600)
        bridge.refresh_session = MagicMock(return_value=True)
        bridge.request = MagicMock(return_value=_good_status())
        bridge._run_poll_cycle()
        bridge.refresh_session.assert_called_once()

    def test_one_attempt_per_token(self, bridge, clock):
        _set_token(bridge, T0 + 60)
        bridge.refresh_session = MagicMock(return_value=False)     # rejected, token unchanged
        bridge.request = MagicMock(return_value=_good_status())
        for _ in range(5):
            bridge._run_poll_cycle()
            clock.now += 20
        assert bridge.refresh_session.call_count == 1

    def test_new_token_gets_its_own_attempt(self, bridge, clock):
        _set_token(bridge, T0 + 60)

        def rotate():
            bridge.config["access_token"] = _jwt(clock.now + LIFETIME)
            return True
        bridge.refresh_session = MagicMock(side_effect=rotate)
        bridge.request = MagicMock(return_value=_good_status())
        bridge._run_poll_cycle()
        assert bridge.refresh_session.call_count == 1
        clock.now += LIFETIME - 60                             # new token now near expiry
        bridge._run_poll_cycle()
        assert bridge.refresh_session.call_count == 2

    @pytest.mark.parametrize("token", [
        None, "", "not-a-jwt", "a.b.c", "a..c",
        "x." + base64.urlsafe_b64encode(b"not json").decode() + ".z",
        "x." + base64.urlsafe_b64encode(b'{"no":"exp"}').decode().rstrip("=") + ".z",
        "x." + base64.urlsafe_b64encode(b'{"exp":"soon"}').decode().rstrip("=") + ".z",
        "x." + base64.urlsafe_b64encode(b'{"exp":true}').decode().rstrip("=") + ".z",
        "x." + base64.urlsafe_b64encode(b'{"exp":-5}').decode().rstrip("=") + ".z",
        "x." + base64.urlsafe_b64encode(b'[1,2]').decode().rstrip("=") + ".z",
    ])
    def test_unreadable_token_never_triggers_proactive_refresh(self, bridge, token):
        bridge.config["access_token"] = token
        bridge.refresh_session = MagicMock()
        bridge.request = MagicMock(return_value=_good_status())
        bridge._run_poll_cycle()
        bridge.refresh_session.assert_not_called()
        bridge.request.assert_called_once()

    def test_missing_refresh_token_never_triggers_proactive_refresh(self, bridge):
        _set_token(bridge, T0 - 10)
        bridge.config["refresh_token"] = None
        bridge.refresh_session = MagicMock()
        bridge.request = MagicMock(return_value=_good_status())
        bridge._run_poll_cycle()
        bridge.refresh_session.assert_not_called()

    def test_kill_switch_disables_it(self, bridge, monkeypatch):
        monkeypatch.setattr(MyThermowattBridge, "PROACTIVE_REFRESH", False)
        _set_token(bridge, T0 - 10)
        bridge.refresh_session = MagicMock()
        bridge.request = MagicMock(return_value=_good_status())
        bridge._run_poll_cycle()
        bridge.refresh_session.assert_not_called()

    def test_margin_constant(self):
        assert MyThermowattBridge.PROACTIVE_REFRESH_MARGIN_S == 120


# ───────────────────────────── rate-limit safety ─────────────────────────────
class TestRateLimitSafety:
    def test_no_io_at_all_during_active_backoff(self, bridge, clock):
        cloud = _cloud_bridge(bridge, clock, first_exp=T0 - 600)       # expired token
        bridge._note_response(_resp(200, DAILY))                       # enter backoff
        assert bridge._rate_limit_remaining() > 0
        bridge._run_poll_cycle()
        bridge._run_poll_cycle()
        assert cloud.calls == []                                       # nothing sent, nothing blocked-and-sent

    def test_not_while_recovery_is_unverified(self, bridge, clock):
        cloud = _cloud_bridge(bridge, clock, first_exp=T0 - 600)
        bridge._note_response(_resp(200, DAILY))
        clock.now += 200                                               # window over, no good poll yet
        assert bridge._rate_limit_remaining() == 0 and bridge.rate_limit_backoff == 1
        bridge._maybe_proactive_refresh()
        assert cloud.calls == []

    def test_recovery_probe_still_uses_the_reactive_path(self, bridge, clock):
        cloud = _cloud_bridge(bridge, clock, first_exp=T0 - 600)
        bridge._note_response(_resp(200, DAILY))
        clock.now += 200
        bridge._run_poll_cycle()
        # status(401) -> refresh -> status(200): exactly the pre-existing sequence
        assert [e for _, e in cloud.calls] == ["status", "refresh", "status"]
        assert bridge.rate_limit_backoff == 0                          # recovery confirmed by the poll

    def test_after_confirmed_recovery_proactive_refresh_resumes(self, bridge, clock):
        cloud = _cloud_bridge(bridge, clock, first_exp=T0 - 600)
        bridge._note_response(_resp(200, DAILY))
        clock.now += 200
        bridge._run_poll_cycle()                                       # recovers (reactive)
        cloud.calls.clear()
        clock.now += LIFETIME - 60                                     # token near expiry again
        bridge._run_poll_cycle()
        assert [e for _, e in cloud.calls] == ["refresh", "status"]

    def test_quota_response_to_proactive_refresh_enters_normal_backoff(self, bridge, clock):
        _set_token(bridge, T0 - 10)
        bridge.session = MagicMock()
        bridge.session.headers = {}
        bridge.session.post.return_value = _resp(200, DAILY)
        bridge.session.request.return_value = _good_status()
        bridge._run_poll_cycle()
        assert bridge.rate_limit_backoff == 1 and bridge._rate_limit_remaining() > 0
        bridge.session.request.assert_not_called()                     # poll skipped: rate limit has priority
        assert _count_logins(bridge) == 0
        # And it is not retried while the window is active.
        bridge.session.post.reset_mock()
        bridge._run_poll_cycle()
        bridge.session.post.assert_not_called()

    def test_quota_after_refresh_is_not_treated_as_poll_failure_of_another_kind(self, bridge, clock):
        _set_token(bridge, T0 - 10)
        bridge.refresh_session = MagicMock(side_effect=RateLimitedError("x"))
        bridge.request = MagicMock(return_value=_good_status())
        bridge._run_poll_cycle()                                       # must not raise
        assert bridge.refresh_session.call_count == 1


def _count_logins(bridge):
    return bridge._request_counters["auth"] - 1 if bridge._request_counters["auth"] else 0


# ───────────────────────────── failure handling ─────────────────────────────
class TestFailureHandling:
    def test_failed_proactive_refresh_is_not_a_poll_failure(self, bridge, clock):
        _set_token(bridge, T0 - 10)
        bridge.refresh_session = MagicMock(return_value=False)
        bridge.request = MagicMock(return_value=_good_status())
        bridge._run_poll_cycle()
        assert bridge._consecutive_failures.get(SERIAL, 0) == 0
        assert bridge.success_count == 1 and bridge.error_count == 0

    def test_exception_in_proactive_refresh_is_swallowed(self, bridge, clock):
        _set_token(bridge, T0 - 10)
        bridge.refresh_session = MagicMock(side_effect=ConnectionError("boom"))
        bridge.request = MagicMock(return_value=_good_status())
        bridge._run_poll_cycle()
        assert bridge.success_count == 1

    def test_rejected_proactive_refresh_falls_back_to_reactive_path(self, bridge, clock):
        cloud = _cloud_bridge(bridge, clock, first_exp=T0 - 600)
        # /refresh is rejected once, then works for the reactive attempt.
        responses = [_resp(401, {"error": "bad refresh"}),
                     _resp(200, {"result": {"accessToken": _jwt(clock.now + LIFETIME),
                                            "refreshToken": REFRESH_SECRET}})]
        cloud.post = MagicMock(side_effect=lambda *a, **k: responses.pop(0))
        bridge._run_poll_cycle()
        assert cloud.post.call_count == 2                              # proactive (rejected) + reactive
        assert bridge.success_count == 1


# ───────────────────────────── command path ─────────────────────────────
class TestCommandPathUnchanged:
    def test_commands_never_trigger_proactive_refresh(self, bridge, clock):
        cloud = _cloud_bridge(bridge, clock, first_exp=T0 + 30)        # inside the margin
        bridge.refresh_session = MagicMock()
        r = bridge.request("POST", "/cmd", serial=SERIAL)
        assert r.status_code == 200
        bridge.refresh_session.assert_not_called()
        assert [e for _, e in cloud.calls] == ["cmd"]

    def test_command_with_expired_token_keeps_the_single_bounded_replay(self, bridge, clock):
        cloud = _cloud_bridge(bridge, clock, first_exp=T0 - 600)
        r = bridge.request("POST", "/cmd", serial=SERIAL)
        assert r.status_code == 200
        assert [e for _, e in cloud.calls] == ["cmd", "refresh", "cmd"]   # unchanged reactive behaviour


# ───────────────────────────── hygiene ─────────────────────────────
class TestHygiene:
    def test_no_token_material_is_logged_or_published(self, bridge, clock, capsys):
        cloud = _cloud_bridge(bridge, clock, first_exp=T0 + 30)
        old_access = bridge.config["access_token"]
        bridge._run_poll_cycle()
        new_access = bridge.config["access_token"]
        assert new_access != old_access                                # refresh really happened
        out = capsys.readouterr()
        text = out.out + out.err
        assert "Proactive token refresh succeeded" in text
        published = json.dumps([c.args for c in bridge.mqtt_client.publish.call_args_list], default=str)
        for secret in (old_access, new_access, REFRESH_SECRET, "SIGNATURE-SECRET"):
            assert secret not in text
            assert secret not in published


# ───────────────────────────── request volume ─────────────────────────────
class TestRequestVolume:
    def _run(self, bridge, clock, cycles):
        cloud = _cloud_bridge(bridge, clock)
        for _ in range(cycles):
            bridge._run_poll_cycle()
            clock.now += 60
        return cloud

    def test_reactive_baseline_costs_three_requests_per_expiry(self, bridge, clock, monkeypatch):
        monkeypatch.setattr(MyThermowattBridge, "PROACTIVE_REFRESH", False)
        cloud = self._run(bridge, clock, 70)               # 70 min: two token expiries
        refreshes = _count(cloud, "refresh")
        assert refreshes >= 2
        assert _count(cloud, "status") == 70 + refreshes   # one replayed status per expiry

    def test_proactive_costs_two_requests_per_expiry(self, bridge, clock):
        cloud = self._run(bridge, clock, 70)
        refreshes = _count(cloud, "refresh")
        assert refreshes >= 2
        assert _count(cloud, "status") == 70               # no 401, no replay
        assert bridge._consecutive_failures.get(SERIAL, 0) == 0
        assert bridge._last_successful_poll[SERIAL] == clock.now - 60   # last cycle succeeded

    def test_proactive_saves_one_request_per_expiry(self, tmp_path, monkeypatch, clock):
        results = {}
        for mode in (False, True):
            monkeypatch.setattr(bridge_module, "CONFIG_FILE", str(tmp_path / f"c{mode}.json"))
            monkeypatch.setattr(MyThermowattBridge, "PROACTIVE_REFRESH", mode)
            clock.now = T0
            with patch("thermowatt_bridge.mqtt.Client") as mock_client:
                mock_client.return_value = MagicMock()
                b = MyThermowattBridge()
                b.mqtt_client = MagicMock()
            b.config["devices"][SERIAL] = {"name": "HWS", "last_setpoint": 60}
            cloud = self._run(b, clock, 130)               # just over two hours
            results[mode] = len(cloud.calls)
        assert results[False] - results[True] == _expiries(130)


def _expiries(minutes):
    # Reactive replays one /status per expiry; the saving per expiry is exactly one request.
    # Expiries at 30, 60, 90, 120 min (token lifetime 1800 s, polled every 60 s).
    return minutes // 30
