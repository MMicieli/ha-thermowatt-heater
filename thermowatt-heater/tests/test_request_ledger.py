"""Regression tests for issue #17 Stage 1: request-volume observability.

Covers the rolling-24h request ledger, persistence across restarts (including
malformed persisted data), the immutable first-rejection and confirmed-recovery
quota-episode snapshots, and that diagnostics stay counts-only (no credentials,
tokens, device identifiers or cloud response text).

Nothing here changes request cadence or any rate-limit / command safety behaviour.
"""

import json
import pathlib
import sys
from unittest.mock import MagicMock, patch

import pytest

sys.argv = ["thermowatt_bridge.py"]

import thermowatt_bridge as bridge_module  # noqa: E402
from thermowatt_bridge import MyThermowattBridge  # noqa: E402

DAILY = {"success": False, "error": "Daily request limit exceeded"}
# Aligned to a ledger bucket boundary (900 s) so bucket arithmetic in tests is exact.
T0 = 1_000_000.0 - (1_000_000 % 900)
DAY = 86400
SECRET_TOKEN = "SECRET.ACCESS.TOKEN"
SECRET_REFRESH = "SECRET-REFRESH-TOKEN"
SECRET_SERIAL = "SERIAL-123456"
SECRET_BODY = "vendor-body-text-must-not-leak"


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


def _make(tmp_path, monkeypatch):
    monkeypatch.setattr(bridge_module, "CONFIG_FILE", str(tmp_path / "config.json"))
    with patch("thermowatt_bridge.mqtt.Client") as mock_client:
        mqtt_client = MagicMock()
        mock_client.return_value = mqtt_client
        instance = MyThermowattBridge()
        instance.mqtt_client = mqtt_client
    instance.config["devices"][SECRET_SERIAL] = {"name": "HWS", "last_setpoint": 60}
    instance.config.update({"access_token": SECRET_TOKEN, "refresh_token": SECRET_REFRESH})
    return instance


@pytest.fixture
def bridge(tmp_path, monkeypatch, clock):
    return _make(tmp_path, monkeypatch)


def _restart(tmp_path, monkeypatch):
    """A fresh bridge instance reading the same persisted config file."""
    return _make(tmp_path, monkeypatch)


def _resp(code=200, body=None):
    r = MagicMock()
    r.status_code = code
    r.headers = {}
    r.json.return_value = body
    return r


def _persist(bridge):
    bridge._maybe_flush_counters(force=True)


def _rolling(bridge):
    return bridge._requests_diagnostics()["rolling_24h"]


def _episodes(bridge):
    return bridge._episodes_diagnostics()


# ───────────────────────────── rolling ledger ─────────────────────────────
class TestRollingLedger:
    def test_sent_requests_counted_by_category(self, bridge):
        for cat in ("status", "status", "command", "auth", "other"):
            bridge._count_request(cat)
        r = _rolling(bridge)
        assert (r["status"], r["command"], r["auth"], r["other"]) == (2, 1, 1, 1)
        assert r["total"] == 5
        assert r["rate_limited_responses"] == 0 and r["blocked"] == 0

    def test_real_request_path_feeds_ledger_and_utc_day_counters_agree(self, bridge):
        bridge.session = MagicMock()
        bridge.session.request.return_value = _resp(200, {"result": {}})
        bridge.request("GET", "/status", serial=SECRET_SERIAL)
        bridge.request("POST", "/cmd", serial=SECRET_SERIAL)
        diag = bridge._requests_diagnostics()
        assert diag["status"] == 1 and diag["command"] == 1
        assert diag["rolling_24h"]["total"] == diag["total"] == 2

    def test_rejections_and_local_blocks_are_counted_but_not_in_total(self, bridge, clock):
        bridge.session = MagicMock()
        bridge.session.request.return_value = _resp(200, DAILY)
        bridge.request("GET", "/status", serial=SECRET_SERIAL)       # sent + rejected
        with pytest.raises(bridge_module.RateLimitedError):
            bridge.request("GET", "/status", serial=SECRET_SERIAL)   # blocked locally
        r = _rolling(bridge)
        assert r["total"] == 1                       # only the request actually sent
        assert r["rate_limited_responses"] == 1
        assert r["blocked"] == 1

    def test_buckets_age_out_of_the_window(self, bridge, clock):
        bridge._count_request("status")
        clock.now = T0 + DAY - 1
        assert _rolling(bridge)["total"] == 1
        clock.now = T0 + DAY + 900            # bucket [T0, T0+900) ends exactly at the horizon
        assert _rolling(bridge)["total"] == 0

    def test_window_is_an_upper_bound_by_at_most_one_bucket(self, bridge, clock):
        bridge._count_request("status")                      # bucket [T0, T0+900)
        clock.now = T0 + DAY + 100                           # 100 s past 24 h but bucket overlaps
        assert _rolling(bridge)["total"] == 1                # conservative upper bound
        clock.now = T0 + DAY + 900                           # bucket ends exactly at horizon
        assert _rolling(bridge)["total"] == 0

    def test_old_buckets_are_pruned_from_memory(self, bridge, clock):
        for hour in range(60):
            clock.now = T0 + hour * 3600
            bridge._count_request("status")
        assert len(bridge._ledger) <= (DAY // 900) + 1

    def test_complete_flag_reports_partial_history(self, bridge, clock):
        bridge._count_request("status")
        r = _rolling(bridge)
        assert r["complete"] is False
        assert r["counting_since"] == bridge._isoformat_epoch(T0)
        clock.now = T0 + DAY + 1
        assert _rolling(bridge)["complete"] is True

    def test_unrelated_counters_unchanged(self, bridge):
        bridge._count_request("status")
        diag = bridge._requests_diagnostics()
        for key in ("day", "status", "command", "auth", "other", "total",
                    "rate_limited_responses", "blocked"):
            assert key in diag
        assert diag["since_recovery"]["sent"] == 1


# ───────────────────────────── persistence ─────────────────────────────
class TestPersistence:
    def test_ledger_survives_restart(self, bridge, clock, tmp_path, monkeypatch):
        for _ in range(7):
            bridge._count_request("status")
        clock.now += 1800
        bridge._count_request("auth")
        _persist(bridge)
        restarted = _restart(tmp_path, monkeypatch)
        r = _rolling(restarted)
        assert (r["status"], r["auth"], r["total"]) == (7, 1, 8)
        assert restarted._requests_diagnostics()["since_recovery"]["sent"] == 8
        assert restarted._ledger_since_ts == T0

    def test_restart_keeps_window_aging(self, bridge, clock, tmp_path, monkeypatch):
        bridge._count_request("status")
        _persist(bridge)
        clock.now = T0 + DAY + 2000
        restarted = _restart(tmp_path, monkeypatch)
        assert _rolling(restarted)["total"] == 0

    @pytest.mark.parametrize("bad", [
        "not-a-dict",
        {"bucket_s": 60, "buckets": {"0": [1, 1, 1, 1, 1, 1]}},          # wrong resolution
        {"bucket_s": 900, "buckets": "nope"},
        {"bucket_s": 900, "buckets": {"x": [1, 1, 1, 1, 1, 1]}},          # bad key
        {"bucket_s": 900, "buckets": {str(int(T0)): [1, 2, 3]}},          # wrong width
        {"bucket_s": 900, "buckets": {str(int(T0)): [-1, 0, 0, 0, 0, 0]}},  # negative
        {"bucket_s": 900, "buckets": {str(int(T0)): ["a", 0, 0, 0, 0, 0]}},
        {"bucket_s": 900, "buckets": {str(int(T0) + 7): [1, 0, 0, 0, 0, 0]}},  # misaligned
        {"bucket_s": 900, "buckets": {str(int(T0) + 10 * DAY): [9, 9, 9, 9, 9, 9]}},  # future
        {"bucket_s": 900, "since_ts": "yesterday", "since_recovery": "x", "buckets": {}},
    ])
    def test_malformed_ledger_is_ignored_without_crashing(self, bad, tmp_path, monkeypatch, clock):
        (tmp_path / "config.json").write_text(json.dumps({
            "devices": {SECRET_SERIAL: {"name": "HWS"}}, "request_ledger": bad}))
        b = _make(tmp_path, monkeypatch)
        r = _rolling(b)
        assert r["total"] == 0 and r["rate_limited_responses"] == 0

    def test_valid_buckets_kept_when_neighbours_are_corrupt(self, tmp_path, monkeypatch, clock):
        (tmp_path / "config.json").write_text(json.dumps({
            "devices": {SECRET_SERIAL: {"name": "HWS"}},
            "request_ledger": {"bucket_s": 900, "buckets": {
                str(int(T0)): [3, 0, 1, 0, 0, 0], "junk": [1], str(int(T0) + 900): [-5, 0, 0, 0, 0, 0]}}}))
        b = _make(tmp_path, monkeypatch)
        assert _rolling(b)["total"] == 4

    @pytest.mark.parametrize("bad", ["x", [1, 2], [{"id": "a"}], [{"id": 1}], [{"id": 1, "rejected_responses": 1,
                                                                              "first_rejection": "s"}]])
    def test_malformed_episodes_are_ignored(self, bad, tmp_path, monkeypatch, clock):
        (tmp_path / "config.json").write_text(json.dumps({
            "devices": {SECRET_SERIAL: {"name": "HWS"}}, "quota_episodes": bad}))
        assert _episodes(_make(tmp_path, monkeypatch)) == []


# ───────────────────────────── quota episodes ─────────────────────────────
class TestQuotaEpisodes:
    def _reject(self, bridge):
        bridge._note_response(_resp(200, DAILY))

    def test_first_rejection_snapshot_is_recorded_once_with_counts(self, bridge, clock):
        for _ in range(5):
            bridge._count_request("status")
        bridge._count_request("auth")
        bridge._count_request("status")          # the request that gets rejected
        self._reject(bridge)
        (ep,) = _episodes(bridge)
        first = ep["first_rejection"]
        assert ep["id"] == 1 and ep["recovery"] is None
        assert first["at"] == bridge._isoformat_epoch(T0)
        assert first["reason_class"] == "quota_message"
        assert first["includes_rejected_request"] is True
        assert first["utc_day_counters"]["total"] == 7
        assert first["utc_day_counters"]["status"] == 6 and first["utc_day_counters"]["auth"] == 1
        assert first["rolling_24h"]["total"] == 7
        assert first["rolling_24h"]["rate_limited_responses"] == 1
        assert first["since_recovery"]["sent"] == 7

    def test_http_429_reason_class(self, bridge):
        r = _resp(429, None)
        bridge._note_response(r)
        assert _episodes(bridge)[0]["first_rejection"]["reason_class"] == "http_429"

    def test_snapshot_is_immutable_across_later_rejections(self, bridge, clock):
        bridge._count_request("status")
        self._reject(bridge)
        before = json.dumps(_episodes(bridge)[0]["first_rejection"], sort_keys=True)
        for step in range(1, 6):
            clock.now += 130 * step
            bridge._count_request("status")
            self._reject(bridge)
        ep = _episodes(bridge)[0]
        assert json.dumps(ep["first_rejection"], sort_keys=True) == before
        assert ep["rejected_responses"] == 6
        assert len(_episodes(bridge)) == 1       # still one episode

    def test_returned_diagnostics_are_detached_copies(self, bridge):
        self._reject(bridge)
        copy = _episodes(bridge)
        copy[0]["first_rejection"]["rolling_24h"]["total"] = 999
        copy[0]["recovery"] = {"forged": True}
        again = _episodes(bridge)
        assert again[0]["first_rejection"]["rolling_24h"]["total"] != 999
        assert again[0]["recovery"] is None

    def test_recovery_snapshot_recorded_once_and_resets_since_recovery(self, bridge, clock):
        for _ in range(3):
            bridge._count_request("status")
        self._reject(bridge)
        clock.now += 4000
        bridge._count_request("status")
        gen = bridge._rate_limit_gen()
        assert bridge._clear_rate_limit(gen) is True
        (ep,) = _episodes(bridge)
        rec = ep["recovery"]
        assert rec["at"] == bridge._isoformat_epoch(T0 + 4000)
        assert rec["episode_duration_s"] == 4000.0
        assert rec["rejected_responses_in_episode"] == 1
        assert rec["sent_after_first_rejection"] == 1
        assert rec["since_recovery"]["sent"] == 4
        assert bridge._requests_diagnostics()["since_recovery"]["sent"] == 0
        # A second clear is a no-op and must not rewrite the evidence.
        frozen = json.dumps(ep, sort_keys=True)
        assert bridge._clear_rate_limit(bridge._rate_limit_gen()) is False
        assert json.dumps(_episodes(bridge)[0], sort_keys=True) == frozen

    def test_stale_poll_cannot_close_an_episode(self, bridge, clock):
        gen = bridge._rate_limit_gen()
        self._reject(bridge)                      # newer than the poll's generation
        clock.now += 4000
        assert bridge._clear_rate_limit(gen) is False
        assert _episodes(bridge)[0]["recovery"] is None

    def test_unexpired_window_cannot_close_an_episode(self, bridge, clock):
        self._reject(bridge)
        assert bridge._clear_rate_limit(bridge._rate_limit_gen()) is False
        assert _episodes(bridge)[0]["recovery"] is None

    def test_second_episode_gets_new_id_and_independent_snapshot(self, bridge, clock):
        self._reject(bridge)
        clock.now += 4000
        bridge._clear_rate_limit(bridge._rate_limit_gen())
        clock.now += 500
        for _ in range(2):
            bridge._count_request("status")
        self._reject(bridge)
        eps = _episodes(bridge)
        assert [e["id"] for e in eps] == [1, 2]
        assert eps[1]["first_rejection"]["since_recovery"]["sent"] == 2
        assert eps[1]["recovery"] is None

    def test_history_is_capped(self, bridge, clock):
        for _ in range(bridge.QUOTA_EPISODE_HISTORY + 4):
            self._reject(bridge)
            clock.now += 4000
            bridge._clear_rate_limit(bridge._rate_limit_gen())
            clock.now += 10
        assert len(bridge._quota_episodes) == bridge.QUOTA_EPISODE_HISTORY
        assert bridge._quota_episodes[-1]["id"] == bridge.QUOTA_EPISODE_HISTORY + 4
        assert len(_episodes(bridge)) == bridge.QUOTA_EPISODE_DIAG

    def test_restart_mid_episode_then_recovery_closes_same_episode(self, bridge, clock, tmp_path, monkeypatch):
        for _ in range(4):
            bridge._count_request("status")
        self._reject(bridge)                       # persists immediately (state transition)
        first = json.dumps(_episodes(bridge)[0]["first_rejection"], sort_keys=True)
        restarted = _restart(tmp_path, monkeypatch)
        assert restarted.rate_limit_backoff == 1
        clock.now += 4000
        restarted._count_request("status")
        assert restarted._clear_rate_limit(restarted._rate_limit_gen()) is True
        (ep,) = _episodes(restarted)
        assert json.dumps(ep["first_rejection"], sort_keys=True) == first
        assert ep["recovery"]["rejected_responses_in_episode"] == 1
        assert ep["recovery"]["episode_duration_s"] == 4000.0

    def test_episode_persisted_immediately_without_waiting_for_flush(self, bridge, tmp_path):
        self._reject(bridge)
        saved = json.loads(pathlib.Path(bridge_module.CONFIG_FILE).read_text())
        assert saved["quota_episodes"][0]["first_rejection"]["reason_class"] == "quota_message"

    def test_recovery_of_a_limit_that_predates_the_ledger_is_recorded_honestly(self, bridge, clock):
        # Simulate a persisted 1.8.0 backoff window with no episode evidence.
        bridge.rate_limit_backoff = 3
        bridge._rate_limit_until = T0 + 10
        clock.now += 4000
        assert bridge._clear_rate_limit(bridge._rate_limit_gen()) is True
        (ep,) = _episodes(bridge)
        assert ep["first_rejection"] is None
        assert ep["recovery"]["episode_duration_s"] is None
        assert ep["recovery"]["sent_after_first_rejection"] is None

    def test_clearing_a_limit_with_no_open_episode_never_rewrites_a_closed_one(self, bridge, clock):
        self._reject(bridge)
        clock.now += 4000
        bridge._clear_rate_limit(bridge._rate_limit_gen())
        closed = json.dumps(_episodes(bridge)[0], sort_keys=True)
        # A later limit state with no recorded episode (e.g. persisted by an older version).
        bridge.rate_limit_backoff = 2
        bridge._rate_limit_until = clock.now - 1
        clock.now += 10
        assert bridge._clear_rate_limit(bridge._rate_limit_gen()) is True
        eps = _episodes(bridge)
        assert [e["id"] for e in eps] == [1, 2]
        assert json.dumps(eps[0], sort_keys=True) == closed          # episode 1 untouched
        assert eps[1]["first_rejection"] is None and eps[1]["recovery"]["at"]

    def test_rejection_inside_active_window_does_not_start_a_new_episode(self, bridge, clock):
        self._reject(bridge)
        clock.now += 5
        self._reject(bridge)                       # inside the 120 s window: no new step
        assert len(_episodes(bridge)) == 1
        assert _episodes(bridge)[0]["rejected_responses"] == 2


# ───────────────────────────── diagnostics hygiene ─────────────────────────────
class TestDiagnosticsHygiene:
    def test_published_diagnostics_are_counts_only(self, bridge, clock):
        bridge.session = MagicMock()
        body = {"success": False, "error": "Daily request limit exceeded " + SECRET_BODY}
        bridge.session.request.return_value = _resp(200, body)
        bridge.request("GET", "/status", serial=SECRET_SERIAL)
        clock.now += 4000
        bridge._clear_rate_limit(bridge._rate_limit_gen())
        bridge._publish_diagnostics(SECRET_SERIAL)
        calls = [c for c in bridge.mqtt_client.publish.call_args_list
                 if c.args and c.args[0].endswith("/diagnostics")]
        assert calls
        text = calls[-1].args[1]
        payload = json.loads(text)
        for secret in (SECRET_TOKEN, SECRET_REFRESH, SECRET_BODY, "password", "Bearer"):
            assert secret not in text
        assert payload["requests"]["rolling_24h"]["total"] == 1
        assert payload["rate_limit"]["episodes"][0]["first_rejection"]["reason_class"] == "quota_message"
        assert payload["rate_limit"]["episodes"][0]["recovery"]["at"]

    def test_persisted_evidence_contains_no_response_text_or_tokens(self, bridge):
        bridge.session = MagicMock()
        bridge.session.request.return_value = _resp(200, {"error": "Daily request limit exceeded " + SECRET_BODY})
        bridge.request("GET", "/status", serial=SECRET_SERIAL)
        evidence = json.dumps(bridge.config["quota_episodes"]) + json.dumps(bridge.config["request_ledger"])
        for secret in (SECRET_TOKEN, SECRET_REFRESH, SECRET_BODY, SECRET_SERIAL):
            assert secret not in evidence

    def test_existing_diagnostic_keys_remain(self, bridge):
        bridge._publish_diagnostics(SECRET_SERIAL)
        payload = json.loads([c for c in bridge.mqtt_client.publish.call_args_list
                              if c.args and c.args[0].endswith("/diagnostics")][-1].args[1])
        for key in ("poll_interval", "poll_status", "telemetry", "command_status", "commands",
                    "rate_limit", "requests"):
            assert key in payload
        for key in ("state", "active", "retry_at", "backoff_step", "events_total"):
            assert key in payload["rate_limit"]


# ───────────────────────────── no behavioural change ─────────────────────────────
class TestNoBehaviourChange:
    def test_polling_and_backoff_constants_unchanged(self):
        assert MyThermowattBridge.POLL_INTERVAL == 60
        assert MyThermowattBridge.POLL_INTERVAL_CONFIRM == 20
        assert MyThermowattBridge.CONFIRM_WINDOW == 60
        assert MyThermowattBridge.CMD_COOLDOWN == 15
        assert MyThermowattBridge.RATE_LIMIT_BASE_BACKOFF == 120
        assert MyThermowattBridge.RATE_LIMIT_MAX_BACKOFF == 3600

    def test_backoff_still_blocks_all_io_and_ledger_does_not_alter_it(self, bridge, clock):
        bridge.session = MagicMock()
        bridge.session.request.return_value = _resp(200, DAILY)
        bridge.request("GET", "/status", serial=SECRET_SERIAL)
        sent = bridge.session.request.call_count
        for endpoint, method in (("/status", "GET"), ("/cmd", "POST")):
            with pytest.raises(bridge_module.RateLimitedError):
                bridge.request(method, endpoint, serial=SECRET_SERIAL)
        assert bridge.session.request.call_count == sent
