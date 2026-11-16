"""Virtual-time simulation harness for the Thermowatt bridge scheduler (issue #17, Stage 3).

Runs the REAL bridge code (`_run_poll_cycle`, `_wait_for_next_poll`, `request()` with its 401
-> refresh -> replay path, command submission and readback reconciliation, availability, quota
backoff, proactive refresh) against a fake cloud and a model of Package 19's pre-cycle
refresh automation. Nothing here touches the network, MQTT, Home Assistant or a heater: the
HTTP session, the MQTT client, the clock and the wake-up event are replaced.

Fidelity limits (stated, not hidden):
* External events (HA refresh publishes, commands) that fall due *while* a request is in flight
  are delivered when it returns (a few hundred milliseconds late); the real MQTT thread would
  deliver them immediately. The bridge coalesces/handles both the same way.
* The cloud, its latency, jitter and fault timing are models calibrated to measured data
  (issue #17): HA demand lands 2.18 s +/- 0.16 s after :30, request round trip ~0.35 s.
* Package 19 is modelled only by the documented pre-cycle refresh automation (:30 primary,
  :48 conditional retry, gated on poll_interval > 120 s) and the 120 s freshness test it applies.
"""
import base64
import contextlib
import heapq
import io
import json
import random
import types
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import requests

import thermowatt_bridge as module
from thermowatt_bridge import MyThermowattBridge

DAY = 86400.0
T0 = 1794009600.0          # 2026-11-07T00:00:00Z : a UTC midnight, multiple of 300 s
SN = "SN"


class Clock:
    def __init__(self, now=T0):
        self.now = now

    def time(self):
        return self.now

    def sleep(self, seconds):
        self.now += max(0.0, float(seconds))


def _jwt(exp):
    def b64(d):
        return base64.urlsafe_b64encode(json.dumps(d).encode()).rstrip(b"=").decode()
    return f"{b64({'alg': 'none'})}.{b64({'exp': exp})}.sig"


class Resp:
    def __init__(self, status=200, body=None):
        self.status_code = status
        self._body = body
        self.headers = {}
        self.text = json.dumps(body) if body is not None else ""

    def json(self):
        if self._body is None:
            raise ValueError("no body")
        return self._body

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(str(self.status_code))


class FakeCloud:
    """Vendor model. Counts every request the bridge sends, by UTC day and category."""

    # Measured: consecutive reactive refreshes are 1,800 s apart (median, sd 24 s, 68 events 14-15 Nov), i.e.
    # the poll exactly 1,800 s after a refresh already sees an expired token (JWT exp is whole-second).
    TOKEN_TTL = 1799.0

    def __init__(self, world, rng):
        self.w = world
        self.rng = rng
        self.tokens = {}                 # access token -> exp
        self.refresh_ok = set()
        self.heater = {"WaterHeaterSts": 0, "T_Avg": 52.0, "T_SetPoint": 58.0, "Cmd": 9}
        self.sent = {}                   # (day, category) -> n
        self.rejected = {}               # day -> n
        self.accepted_by_day = {}        # day -> n received & counted by quota
        self.cap = None                  # accepted requests per UTC day before quota rejection
        self.quota_windows = []          # forced (start, end) rejection windows
        self.p_fail = 0.0                # per-request transport failure probability
        self.p_fail_windows = []         # (start, end, p)
        self.outages = []                # (start, end): every request fails at transport
        self.fail_next_refresh = 0
        self.statuses_401 = 0
        self.replays = 0
        self._last_was_401 = False

    def _day(self):
        return int((self.w.clk.now - T0) // DAY)

    def _bump(self, cat):
        k = (self._day(), cat)
        self.sent[k] = self.sent.get(k, 0) + 1

    def _rtt(self):
        return max(0.08, self.rng.gauss(0.35, 0.08))

    def _transport_fail(self):
        now = self.w.clk.now
        if any(a <= now < b for a, b in self.outages):
            return True
        p = self.p_fail
        for a, b, pp in self.p_fail_windows:
            if a <= now < b:
                p = pp
        return self.rng.random() < p

    def _quota_rejects(self):
        now = self.w.clk.now
        if any(a <= now < b for a, b in self.quota_windows):
            return True
        if self.cap is not None and self.accepted_by_day.get(self._day(), 0) >= self.cap:
            return True
        return False

    def handle(self, method, endpoint, headers, category):
        self._bump(category)
        if self._transport_fail():
            self.w.clk.sleep(5.0 if any(a <= self.w.clk.now < b for a, b in self.outages) else 0.3)
            raise requests.exceptions.ConnectionError("RemoteDisconnected")
        self.w.clk.sleep(self._rtt())
        if self._quota_rejects():
            self.rejected[self._day()] = self.rejected.get(self._day(), 0) + 1
            return Resp(200, {"success": False, "error": "Daily request limit exceeded"})
        d = self._day()
        self.accepted_by_day[d] = self.accepted_by_day.get(d, 0) + 1
        if endpoint in ("/login", "/refresh"):
            if endpoint == "/refresh" and self.fail_next_refresh > 0:
                self.fail_next_refresh -= 1
                return Resp(401, {"error": "refresh failed"})
            exp = self.w.clk.now + self.TOKEN_TTL
            tok = _jwt(exp)
            self.tokens[tok] = exp
            return Resp(200, {"result": {"accessToken": tok, "refreshToken": "R"}})
        auth = (headers.get("Authorization") or "").replace("Bearer ", "")
        if self.tokens.get(auth, 0) <= self.w.clk.now:
            self.statuses_401 += 1
            self._last_was_401 = True
            return Resp(401, {"error": "token expired"})
        if endpoint == "/status":
            if self._last_was_401:
                self.replays += 1
            self._last_was_401 = False
            return Resp(200, {"result": dict(self.heater)})
        self._last_was_401 = False
        body = {}
        if endpoint == "/manual":
            self.heater["Cmd"] = 9
            self.heater["T_SetPoint"] = float((self.w.last_json or {}).get("T_SetPoint", self.heater["T_SetPoint"]))
        elif endpoint == "/eco":
            self.heater["Cmd"] = 3
        elif endpoint == "/auto":
            self.heater["Cmd"] = 17
        return Resp(200, body)


class World:
    def __init__(self, seed=1, poll_interval=180, demand_aware=False, proactive=False,
                 days=2, ha_enabled=True, jitter_sd=0.16, reuse_age=None, margin=None, post_delay=None):
        self.rng = random.Random(seed)
        self.clk = Clock()
        self.end = T0 + days * DAY
        self.heap = []
        self._seq = 0
        self.jitter_sd = jitter_sd
        self.ha_enabled = ha_enabled
        self.ha_down = []                # (start, end) HA not publishing refreshes
        self.ha_automation_off = []      # (start, end) pre-cycle automation disabled
        self.last_json = None
        self.cloud = FakeCloud(self, self.rng)
        self.publishes = []              # (t, topic, payload)
        self.cycles = []                 # per EMHASS cycle record
        self.success_times = []
        self.avail = []                  # (t, state)
        self.refresh_publishes = 0
        self.native_changes = []         # (t, new_cmd)
        self.commands = []               # (t, topic, payload)
        self._out = io.StringIO()
        self.poll_interval = poll_interval
        self.post_delay = post_delay      # None or callable(rng)->seconds spent after the status stamp (publishes, flush)
        self.cfg = dict(demand_aware=demand_aware, proactive=proactive, reuse_age=reuse_age, margin=margin)

    # -- scheduling -------------------------------------------------------
    def at(self, t, fn):
        self._seq += 1
        heapq.heappush(self.heap, (t, self._seq, fn))

    def next_event_time(self):
        return self.heap[0][0] if self.heap else None

    def run_due(self):
        while self.heap and self.heap[0][0] <= self.clk.now:
            _, _, fn = heapq.heappop(self.heap)
            fn()

    # -- build the bridge -------------------------------------------------
    def build(self, tmp_path):
        w = self
        patcher_cfg = patch.object(module, "CONFIG_FILE", str(tmp_path / "bridge.json"))
        patcher_cfg.start()
        with patch("thermowatt_bridge.mqtt.Client") as factory:
            factory.return_value = MagicMock()
            b = MyThermowattBridge()
        self.patchers = [patcher_cfg]
        fake_time = SimpleNamespace(time=self.clk.time, sleep=self.clk.sleep, monotonic=self.clk.time,
                                    strftime=__import__("time").strftime, gmtime=__import__("time").gmtime)
        p = patch.object(module, "time", fake_time)
        p.start()
        self.patchers.append(p)
        b.POLL_INTERVAL = self.poll_interval
        b.current_poll_interval = self.poll_interval
        b.DEMAND_AWARE_FLAG = self.cfg["demand_aware"]
        b.PROACTIVE_REFRESH = self.cfg["proactive"]
        if self.cfg["reuse_age"] is not None:
            b.DEMAND_REUSE_MAX_AGE_S = self.cfg["reuse_age"]
        if self.cfg["margin"] is not None:
            b.OFFLINE_SAFETY_MARGIN_S = self.cfg["margin"]
        b.config["devices"][SN] = {"name": "HWS", "last_setpoint": 58}
        b._save_config = lambda: None
        if self.post_delay is not None:
            real_diag = b._publish_diagnostics

            def diag(serial, _real=real_diag):
                self.clk.sleep(self.post_delay(self.rng))     # work between the success stamp and the next wait
                return _real(serial)
            b._publish_diagnostics = diag
        b._poll_wakeup = types.SimpleNamespace()
        ev = self._make_event()
        b._poll_wakeup = ev

        def pub(topic, payload=None, *a, **k):
            w.publishes.append((w.clk.now, topic, payload))
            if topic == f"P/{SN}/availability":
                w.avail.append((w.clk.now, payload))
            return MagicMock()
        b.mqtt_client.publish.side_effect = pub

        def cat(endpoint, method):
            return "status" if endpoint == "/status" else ("auth" if endpoint in ("/login", "/refresh")
                                                          else "command")

        def request(method, url, verify=None, **kw):
            endpoint = url.replace(b.BASE_URL, "")
            w.last_json = kw.get("json")
            return w.cloud.handle(method, endpoint, dict(b.session.headers), cat(endpoint, method))

        def post(url, json=None, verify=None, timeout=None, **kw):
            endpoint = url.replace(b.BASE_URL, "")
            return w.cloud.handle("POST", endpoint, dict(b.session.headers), "auth")

        b.session.request = request
        b.session.post = post
        self.b = b
        return b

    def _make_event(self):
        w = self

        class Ev:
            def __init__(s):
                s.flag = False

            def set(s):
                s.flag = True

            def clear(s):
                s.flag = False

            def is_set(s):
                return s.flag

            def wait(s, timeout=None):
                target = w.clk.now + (timeout if timeout is not None else 1e12)
                while True:
                    if s.flag:
                        return True
                    nxt = w.next_event_time()
                    if nxt is not None and nxt <= target:
                        w.clk.now = max(w.clk.now, nxt)
                        w.run_due()
                        continue
                    w.clk.now = max(w.clk.now, target)
                    return s.flag
        return Ev()

    def close(self):
        for p in reversed(self.patchers):
            p.stop()

    # -- Package 19 pre-cycle refresh model + EMHASS cycle observer -------
    def _ha_down(self, t):
        return any(a <= t < b for a, b in self.ha_down)

    def _automation_off(self, t):
        return any(a <= t < b for a, b in self.ha_automation_off)

    def _healthy_since(self, t_cut):
        b = self.b
        ts = b._last_successful_poll.get(SN)
        lim = b._rate_limit_snapshot()["step"] > 0
        return (ts is not None and int(ts) >= t_cut and not lim
                and b._consecutive_failures.get(SN, 0) == 0)

    def publish_refresh(self, kind, t_nominal):
        t = self.clk.now
        if not self.ha_enabled or self._ha_down(t) or self._automation_off(t):
            return
        if not (self.b.current_poll_interval > 120):         # Package 19 gate on the poll-interval sensor
            return
        if kind == "retry" and self._healthy_since(t_nominal - 18 - 10):   # primary was at :30 = retry - 18 s
            return
        self.refresh_publishes += 1
        self.b.on_mqtt_message(None, None, SimpleNamespace(
            topic=f"P/{SN}/refresh", payload=b"1", retain=False))

    def schedule_ha(self):
        """Package 19 pre-cycle refresh: at :30 of every minute with minute % 5 == 4 (30 s before the
        :x5:00 EMHASS cycle) and a retry at :48 that is skipped when healthy telemetry arrived at or
        after :30 - 10 s. Each publish lands the measured 2.18 s +/- jitter after the trigger."""
        k = 0
        while T0 + 300 * k < self.end + 300:
            base = T0 + 300 * k
            for off, kind in ((270, "primary"), (288, "retry")):
                lat = max(1.9, self.rng.gauss(2.18, self.jitter_sd))
                tn = base + off
                self.at(tn + lat, lambda kind=kind, tn=tn: self.publish_refresh(kind, tn))
            k += 1

    def schedule_cycles(self):
        k = 1
        while T0 + 300 * k <= self.end:
            T = T0 + 300 * k
            self.at(T, lambda T=T: self.observe_cycle(T))
            k += 1

    def observe_cycle(self, T):
        b = self.b
        ts = b._last_successful_poll.get(SN)
        age = (T - int(ts)) if ts is not None else None   # Package 19 sees the second-resolution sensor
        lim = b._rate_limit_snapshot()["step"] > 0
        online = b._availability_for(SN, T) == "online"
        fresh = (age is not None and age <= 120 and b._consecutive_failures.get(SN, 0) == 0
                 and not lim and online)
        self.cycles.append({"t": T, "age": age, "fresh": fresh, "online": online,
                            "ha_down": self._ha_down(T)})

    # -- run ----------------------------------------------------------------
    def run(self):
        b = self.b
        self.schedule_ha()
        self.schedule_cycles()
        with contextlib.redirect_stdout(self._out):
            for _ in range(200):                  # boot login, retried like _boot_authenticate_and_discover
                try:
                    b.login()
                    break
                except Exception:
                    self.clk.sleep(5)
            while self.clk.now < self.end:
                b._run_poll_cycle()
                b._wait_for_next_poll(b.current_poll_interval)
        return self

    # -- injected events ----------------------------------------------------
    def command(self, t, topic_suffix, payload):
        def fire():
            self.commands.append((self.clk.now, topic_suffix, payload))
            self.b.on_mqtt_message(None, None, SimpleNamespace(
                topic=f"P/{SN}/CMD/{topic_suffix}", payload=payload.encode(), retain=False))
        self.at(t, fire)

    def native_change(self, t, cmd):
        def fire():
            self.cloud.heater["Cmd"] = cmd
            self.native_changes.append((self.clk.now, cmd))
        self.at(t, fire)

    # -- metrics --------------------------------------------------------------
    def per_day(self):
        out = {}
        for (d, cat), n in self.cloud.sent.items():
            out.setdefault(d, {"status": 0, "auth": 0, "command": 0})[cat] += n
        for d, row in out.items():
            row["total"] = sum(row.values())
            row["rejected"] = self.cloud.rejected.get(d, 0)
        return out

    def success_gaps(self):
        ts = [t for t, topic, payload in self.publishes if topic == f"P/{SN}/STATUS"]
        return [b - a for a, b in zip(ts, ts[1:])]

    def offline_flaps(self, exclude=()):
        n = 0
        prev = None
        for t, s in self.avail:
            if s == "offline" and prev == "online" and not any(a <= t < b for a, b in exclude):
                n += 1
            prev = s
        return n

    def stale_cycles(self, exclude=()):
        return sum(1 for c in self.cycles
                   if not c["fresh"] and not c["ha_down"] and not any(a <= c["t"] < b for a, b in exclude))

    def preemption_latencies(self):
        """Seconds from a native heater change to the next published STATUS that shows it."""
        pubs = [(t, json.loads(p)) for t, topic, p in self.publishes if topic == f"P/{SN}/STATUS"]
        lat = []
        for tc, cmd in self.native_changes:
            seen = next((t for t, pl in pubs if t >= tc and pl.get("result", {}).get("Cmd") == cmd), None)
            lat.append(None if seen is None else seen - tc)
        return lat

    def cycle_ages(self, exclude=()):
        """Age (s) of the telemetry Package 19 sees at each EMHASS cycle start, outside fault windows."""
        return [c["age"] for c in self.cycles
                if c["age"] is not None and not c["ha_down"] and not any(a <= c["t"] < b for a, b in exclude)]
