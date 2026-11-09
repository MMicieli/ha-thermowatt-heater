import sys, json, time, uuid, os, signal, threading, requests, urllib3, datetime, builtins, base64, math, re, contextlib
import paho.mqtt.client as mqtt
from requests.adapters import HTTPAdapter
from paho.mqtt.enums import CallbackAPIVersion

_ORIGINAL_PRINT = builtins.print


def _timestamped_print(*args, **kwargs):
    timestamp = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    _ORIGINAL_PRINT(timestamp, *args, **kwargs)


print = _timestamped_print

# Proper TLS verification by default. Set THERMOWATT_TLS_NO_VERIFY=1 only if
# the backend genuinely rejects certificate validation (debug fallback).
TLS_VERIFY = os.getenv("THERMOWATT_TLS_NO_VERIFY", "0") != "1"
if not TLS_VERIFY:
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
    print("[WARN] TLS certificate verification is DISABLED (THERMOWATT_TLS_NO_VERIFY=1)")

# --- CONFIGURATION ---
EMAIL    = sys.argv[1] if len(sys.argv) > 1 else None
PASSWORD = sys.argv[2] if len(sys.argv) > 2 else None
CONFIG_FILE  = "/data/thermowatt_config.json" if os.path.exists("/data") else "thermowatt_config.json"
MQTT_HOST    = os.getenv("MQTT_HOST", "core-mosquitto")
MQTT_PORT    = int(os.getenv("MQTT_PORT", 1883))
MQTT_USER    = os.getenv("MQTT_USER")
MQTT_PASS    = os.getenv("MQTT_PASSWORD")

# Bridge availability topic — used for LWT and all discovery payloads
AVAILABILITY_TOPIC = "thermowatt/bridge/status"

# HTTP timeouts — (connect_timeout, read_timeout) in seconds.
HTTP_TIMEOUT = (5, 15)

# A pooled keep-alive connection idle for longer than this is discarded before the next
# request, so a poll never reuses a socket that an intermediary may already have closed
# (issue #23: a background poll every ~60 s was hitting exactly that window and failing with
# RemoteDisconnected). Must stay below the smallest plausible server/load-balancer idle
# timeout (60 s is a common default) and above the 20 s command-confirmation and 5 s retry
# spacing, which keep reusing the connection.
HTTP_MAX_IDLE_S = 30


def _load_element_wattage() -> int:
    """Read and validate ELEMENT_WATTAGE env var. Returns 3000 on any error."""
    raw = os.getenv("ELEMENT_WATTAGE", "3000")
    try:
        w = int(raw)
    except (ValueError, TypeError):
        print(f"[WARN] Invalid ELEMENT_WATTAGE={raw!r} — defaulting to 3000 W")
        return 3000
    if not (100 <= w <= 10000):
        print(f"[WARN] ELEMENT_WATTAGE={w} out of range [100, 10000] — defaulting to 3000 W")
        return 3000
    return w


ELEMENT_WATTAGE: int = _load_element_wattage()


def _load_poll_interval() -> int:
    """Background poll interval (add-on option `poll_interval`, via run.sh).

    Default and fallback for anything unset, malformed or out of range is 60 s, so
    an older saved configuration keeps today's behaviour. 300 s is the ceiling because
    availability goes offline after a fixed 300 s without a real poll.
    """
    raw = os.getenv("THERMOWATT_POLL_INTERVAL", "60")
    try:
        v = int(raw)
    except (ValueError, TypeError):
        v = 60
        if str(raw).strip() not in ("", "null"):
            print(f"[WARN] Invalid THERMOWATT_POLL_INTERVAL={raw!r} — defaulting to 60 s")
        return v
    if not (60 <= v <= 300):
        print(f"[WARN] THERMOWATT_POLL_INTERVAL={v} out of range [60, 300] — defaulting to 60 s")
        return 60
    return v


# Raw Thermowatt Cmd is a bitfield: bit0 = enabled/on, the remaining bits select
# a mode family. Enabled+known-family -> the named mode; the same family with
# bit0 cleared is that family's inactive/Off sub-state.
#
# Manual(8), Auto(16) and Holiday(64) disabled (bit0=0) have been directly
# observed on-device (issue #13 read-only audit). Eco family disabled (2) is
# NOT an observed device value — it follows from the same bitfield contract
# and is included so the decoder does not special-case only the values seen
# so far, but must not be presented as captured evidence.
KNOWN_CMD_FAMILIES = {
    2:  "Eco",
    8:  "Manual",
    16: "Auto",
    64: "Holiday",
}


class _IdleSafeAdapter(HTTPAdapter):
    """HTTPAdapter that never reuses a pooled connection idle for more than ``max_idle_s``.

    urllib3 only drops a pooled connection it has already seen closed. If the peer closed it
    just before the next request is sent (its FIN still in flight), the request is written to
    a dead socket and fails with ``RemoteDisconnected`` without any response. Clearing the
    pools when the adapter has been idle that long forces a fresh connection instead, with no
    extra request and no retry. Cookies and headers live on the ``requests.Session``, not on
    the TCP connection, so authentication and load-balancer cookies are unaffected.

    Not thread-safe by itself: every caller holds ``MyThermowattBridge._http_lock``.
    """

    def __init__(self, max_idle_s, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.max_idle_s = max_idle_s
        self._last_done = None  # time.monotonic() when the previous request finished

    def send(self, request, **kwargs):
        if self._last_done is not None and time.monotonic() - self._last_done > self.max_idle_s:
            self.poolmanager.clear()
            for manager in self.proxy_manager.values():
                manager.clear()
        try:
            return super().send(request, **kwargs)
        finally:
            self._last_done = time.monotonic()


class RateLimitedError(Exception):
    """Raised instead of sending an HTTP request while rate-limit backoff is active."""


class MyThermowattBridge:
    API_KEY  = "YVjArWssxKH631jv1dnnWOTr6gijsSAGz7rQJ4hJoUNRffxYvbQaMbePBEZalena"
    BASE_URL = "https://myapp-connectivity.com/api/v1"

    # Sentinel `requested` value for Off MODE commands — confirmation is family-based
    # (known family, bit0 cleared), not an exact-Cmd-value match. See decode_cmd().
    CMD_OFF_SENTINEL = "OFF"

    @staticmethod
    def decode_cmd(raw):
        """Decode a raw Thermowatt Cmd value into family/enabled/mode.

        Returns {"known": bool, "family": int|None, "enabled": bool|None, "mode": str|None}.
        `mode` is a named mode ("Eco"/"Manual"/"Auto"/"Holiday") when the family
        is known and enabled, "Off" when the family is known and disabled, and
        None when the family is unrecognised or raw is absent/malformed. Never
        defaults an unknown value to "Off".
        """
        try:
            cmd = int(raw)
        except (TypeError, ValueError):
            return {"known": False, "family": None, "enabled": None, "mode": None}
        family  = cmd & ~1
        enabled = bool(cmd & 1)
        if family not in KNOWN_CMD_FAMILIES:
            return {"known": False, "family": family, "enabled": enabled, "mode": None}
        mode = KNOWN_CMD_FAMILIES[family] if enabled else "Off"
        return {"known": True, "family": family, "enabled": enabled, "mode": mode}

    # Normal polling 60s — reduces cloud rate-limit risk and produces cleaner
    # InfluxDB calibration data. Post-command confirmation window uses 20s.
    POLL_INTERVAL         = _load_poll_interval()  # seconds — background polling (default 60)
    POLL_INTERVAL_CONFIRM = 20   # seconds — post-command confirmation window
    CONFIRM_WINDOW        = 60   # seconds — how long to stay in fast-poll after a command
    STATUS_LOG_INTERVAL   = 300  # seconds — 5-minute summary log

    # Publish per-device "offline" availability after this many consecutive poll failures.
    # At the 60s normal interval this is ~5 min of cloud silence before marking degraded.
    DEGRADED_THRESHOLD = 5
    # Cadence-independent availability: offline after this long without a real
    # successful status poll, whatever the failure count and the configured cadence.
    OFFLINE_AFTER_S = 300  # fixed: never relaxed for a slower cadence (ownership-on safety bound)

    # Firmware-confirmed temperature ceiling from T_set_max attribute.
    # Hardware mechanical cutout at ~90°C is independent.
    TEMP_MIN = 20
    TEMP_MAX = 70  # matches confirmed T_set_max: 70 from device attributes

    # Rate-limit handling. The API signals quota exhaustion either with HTTP 429 or
    # with HTTP 200 and {"success": false, "error": "Daily request limit exceeded"}.
    # Backoff doubles per consecutive rate-limit event: 120, 240, 480, 960, 1920 then
    # capped at 3600 s. Normal polling stays at POLL_INTERVAL.
    RATE_LIMIT_BASE_BACKOFF = 120   # seconds — first backoff step
    RATE_LIMIT_MAX_BACKOFF  = 3600  # seconds — hard ceiling (also bounds Retry-After)
    RATE_LIMIT_API_MESSAGE  = "daily request limit exceeded"  # matched case-insensitively
    COUNTER_FLUSH_INTERVAL  = 600   # seconds — max one request-counter config write per interval
    # Rolling request statistics (observability only; never gates a request).
    LEDGER_BUCKET_S         = 900    # resolution of the rolling ledger (15 min)
    LEDGER_WINDOW_S         = 86400  # rolling window length (24 h)
    # status/command/auth/other = HTTP requests ATTEMPTED (counted before the transport
    # result is known). transport_failed = attempts that ended in an exception (timeout,
    # reset, DNS, TLS): for those it is UNKNOWN whether the vendor received, counted or
    # executed the request. rate_limited_responses = responses classified as a quota
    # rejection. blocked = refused locally, never attempted. transport_failed is last so
    # rows persisted without it (6 wide) still load.
    LEDGER_FIELDS           = ("status", "command", "auth", "other",
                               "rate_limited_responses", "blocked", "transport_failed")
    LEDGER_FIELDS_LEGACY_N  = 6
    QUOTA_EPISODE_HISTORY   = 10     # quota episodes (rejection → recovery) retained
    QUOTA_EPISODE_DIAG      = 3      # most recent episodes published in diagnostics
    # Proactive access-token refresh (issue #17 Stage 1b). Replaces the reactive
    # 401 -> refresh -> replay sequence (3 requests per ~30 min token lifetime) with a
    # single refresh just before expiry (1 request). Kill switch: set the environment
    # DEFAULT OFF until commissioned. The supported switch is the add-on option
    # `proactive_token_refresh` (config.yaml): run.sh exports THERMOWATT_PROACTIVE_REFRESH=1
    # only when it is true. Only the exact values 1/true/yes/on enable it; anything else
    # (unset, 0, empty, garbage) leaves it off. The reactive 401 path is unchanged and
    # remains the fallback.
    PROACTIVE_REFRESH          = (os.environ.get("THERMOWATT_PROACTIVE_REFRESH", "")
                                  .strip().lower() in ("1", "true", "yes", "on"))
    PROACTIVE_REFRESH_MARGIN_S = 120    # refresh when the access token has <= this long left
    PROACTIVE_MIN_SPACING_S    = 600    # hard floor between proactive attempts, whatever exp says
    COMMAND_ERROR_MAX_LEN   = 120   # truncate API-supplied error text kept in diagnostics
    # Informational only: age after which diagnostics label telemetry "stale". Mirrors
    # Package 19's 120 s freshness test but does NOT replace it; Package 19 still
    # evaluates poll_status / failures / last_successful_poll itself.
    TELEMETRY_STALE_AFTER   = 120
    DEMAND_MIN_SPACING      = 10    # seconds — min spacing of attempts for requested refreshes
    DEMAND_RETRY_DELAY      = 5     # seconds — single transport-failure retry of a requested refresh
    DIAG_REFRESH_INTERVAL   = 60    # seconds — republish diagnostics during backoff (MQTT only)

    # CMD_COOLDOWN per command type — MODE and TEMP each have their own 15s window so a
    # valid paired sequence (MODE then TEMP, or vice versa) is never incorrectly blocked.
    CMD_COOLDOWN = 15  # seconds

    def __init__(self):
        self.config = self._load_config()
        self._config_lock = threading.Lock()

        # Serialises all HTTP via the shared requests.Session, which is NOT
        # thread-safe. login()/refresh_session()/request() are reachable from
        # both the main poll thread and the paho MQTT callback thread; without
        # this lock, concurrent _reset_headers()/seriale mutations corrupt each
        # other's headers. Re-entrant because request() → refresh_session().
        self._http_lock = threading.RLock()

        # Configured element wattage (W) — used for power sensor template and energy integration.
        # Not a CT measurement; this is an estimate based on heating state only.
        self.element_wattage: int = ELEMENT_WATTAGE

        # Single session instance — preserves cookies from AWS load balancers
        self.session = requests.Session()
        _adapter = _IdleSafeAdapter(HTTP_MAX_IDLE_S)
        self.session.mount("https://", _adapter)
        self.session.mount("http://", _adapter)  # same behaviour for any non-TLS endpoint

        self.mqtt_client = mqtt.Client(CallbackAPIVersion.VERSION2)
        self.mqtt_client.will_set(AVAILABILITY_TOPIC, "offline", retain=True)
        if MQTT_USER:
            self.mqtt_client.username_pw_set(MQTT_USER, MQTT_PASS)

        # Polling state
        self.poll_count            = 0
        self.success_count         = 0
        self.error_count           = 0
        self.last_status_log_time  = time.time()
        self.rate_limit_backoff    = 0  # 0=none, 1+=consecutive rate-limit backoff steps
        self.current_poll_interval = self.POLL_INTERVAL
        self._avail_state = {}         # last published per-device availability
        self._io = threading.local()   # per-thread: did the last I/O end without any response?

        # Post-command fast-poll window tracking. The event wakes the poll loop so
        # a command submitted just after a normal poll does not wait up to 60 seconds
        # before its first eligible confirmation poll.
        self._confirm_until: float = 0.0  # epoch time until which fast-poll is active
        self._poll_wakeup = threading.Event()
        # Read-only refresh requests wake this same polling loop. No worker,
        # HTTP call or command queue is created on the MQTT callback thread.
        self._demand_requested = set()  # guarded by _config_lock
        self._last_status_attempt = {}

        # Per-device command cooldown and confirmation state, keyed by command type.
        # MODE and TEMP remain independent so a valid paired sequence is supported.
        self._command_lock = threading.RLock()
        self._last_cmd_time: dict = {}  # {serial: {"MODE": epoch, "TEMP": epoch}}
        self._command_state: dict = {}  # {serial: {"MODE"/"TEMP": confirmation record}}

        # Per-device last successful poll timestamp — for energy accumulation
        self._last_poll_ts: dict = {}  # {serial: epoch float}

        # Rate-limit state. Guarded by _rate_limit_lock (never held across I/O).
        # Observed global lock order (recorded under threads by
        # tests/test_pr21_review_fixes.py::TestConcurrency): _http_lock -> _config_lock
        # -> _rate_limit_lock. _rate_limit_lock is a leaf: nothing else is acquired
        # while it is held, and _config_lock is never taken while holding it.
        self._rate_limit_lock = threading.Lock()
        self._rate_limit_until: float = 0.0
        self._rate_limit_reason = None
        self._rate_limit_last_at = None
        self._rate_limit_recovered_at = None
        self._rate_limit_events_total: int = 0   # backoff windows started (lifetime)
        self._rate_limit_generation: int = 0     # +1 per classified rate-limit response
        self._request_counters: dict = self._new_counters(self._utc_day())
        self._counters_dirty = False
        self._counters_last_flush: float = 0.0
        # Rolling ledger of requests actually SENT (plus rejections / local blocks),
        # in LEDGER_BUCKET_S buckets, and immutable quota-episode evidence. Guarded by
        # _rate_limit_lock. Counts only: no tokens, identifiers or response content.
        self._ledger: dict = {}               # {bucket_start:int -> [6 ints] per LEDGER_FIELDS}
        self._ledger_since_ts: float = time.time()   # when this ledger began counting
        self._sent_since_recovery: int = 0    # requests sent since the last confirmed recovery
        self._since_recovery_ts: float = self._ledger_since_ts
        self._quota_episodes: list = []       # oldest → newest, capped at QUOTA_EPISODE_HISTORY
        self._proactive_attempt_exp = None    # token `exp` for which a proactive refresh was tried
        self._proactive_last_attempt_at = None  # wall time of the last proactive attempt
        self._restore_persisted_state()

        # Per-device degraded state tracking
        self._consecutive_failures: dict = {}  # {serial: int}
        self._last_successful_poll: dict = {}  # {serial: epoch float}
        self._last_poll_error: dict = {}  # {serial: str|None} — reason for the most recent poll failure

    # ------------------------------------------------------------------ #
    #  Config helpers                                                      #
    # ------------------------------------------------------------------ #

    def _load_config(self):
        if os.path.exists(CONFIG_FILE):
            with open(CONFIG_FILE, 'r') as f:
                config = json.load(f)
                if 'devices' not in config:
                    config['devices'] = {}
                return config
        return {
            "device_uuid":    str(uuid.uuid4()),
            "access_token":   None,
            "refresh_token":  None,
            "devices":        {}
        }

    def _save_config(self):
        with open(CONFIG_FILE, 'w') as f:
            json.dump(self.config, f)

    # ------------------------------------------------------------------ #
    #  Rate limiting + request counters                                    #
    # ------------------------------------------------------------------ #

    @staticmethod
    def _utc_day() -> str:
        return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d")

    @staticmethod
    def _new_counters(day: str) -> dict:
        # status/command/auth/other/total = HTTP requests actually SENT (total is
        # their sum). rate_limited_responses = responses classified as a rate
        # limit, counted once per response. blocked = requests refused locally by
        # an active backoff window (never sent, never in `total`).
        return {"day": day, "status": 0, "command": 0, "auth": 0, "other": 0,
                "total": 0, "rate_limited_responses": 0, "blocked": 0}

    def _restore_persisted_state(self):
        """Restore rate-limit window and per-day request counters after a restart.

        Only the backoff window/step and counters are restored — never any
        command. A persisted window far beyond the ceiling (clock change or
        corrupt file) is clamped so a bad value can never stall the bridge.
        """
        now = time.time()
        try:
            rl = self.config.get("rate_limit")
            rl = rl if isinstance(rl, dict) else {}
            until = float(rl.get("until") or 0.0)
            self._rate_limit_until = min(until, now + self.RATE_LIMIT_MAX_BACKOFF)
            self.rate_limit_backoff = max(0, int(rl.get("step") or 0))
            reason = rl.get("reason")
            self._rate_limit_reason = reason if reason in (
                "HTTP 429", "Daily request limit exceeded") else None
            for attr, key in (("_rate_limit_last_at", "last_at"),
                              ("_rate_limit_recovered_at", "recovered_at")):
                v = rl.get(key)
                setattr(self, attr, v if (isinstance(v, str) and len(v) <= 40
                                          and self._ISO_RE.match(v)) else None)
            self._rate_limit_events_total = max(0, int(rl.get("events_total") or 0))
        except (TypeError, ValueError):
            self._rate_limit_until, self.rate_limit_backoff = 0.0, 0
        try:
            saved = self.config.get("request_counters")
            saved = saved if isinstance(saved, dict) else {}
            if saved.get("day") == self._request_counters["day"]:
                for key in self._request_counters:
                    if key != "day":
                        self._request_counters[key] = max(0, int(saved.get(key) or 0))
        except (TypeError, ValueError):
            pass
        self._restore_ledger_and_episodes(now)

    def _restore_ledger_and_episodes(self, now: float):
        """Restore the rolling ledger and quota-episode evidence; never raises.

        Anything malformed is skipped or ignored: observability data must never be
        able to stall or crash the bridge. Buckets starting in the future (clock
        change or corrupt file) are dropped so they cannot sit in the window forever.
        """
        try:
            led = self.config.get("request_ledger")
            if isinstance(led, dict) and led.get("bucket_s") == self.LEDGER_BUCKET_S:
                width = self.LEDGER_BUCKET_S
                buckets = {}
                raw = led.get("buckets")
                for key, row in (raw.items() if isinstance(raw, dict) else ()):
                    try:
                        start = int(key)
                        vals = [int(v) for v in row]
                    except (TypeError, ValueError):
                        continue
                    if len(vals) == self.LEDGER_FIELDS_LEGACY_N:
                        vals = vals + [0] * (len(self.LEDGER_FIELDS) - len(vals))
                    if (len(vals) != len(self.LEDGER_FIELDS) or min(vals) < 0
                            or start % width != 0 or start > now + width
                            or start + width <= now - self.LEDGER_WINDOW_S):
                        continue
                    buckets[start] = vals
                self._ledger = buckets
                since = led.get("since_ts")
                if isinstance(since, (int, float)) and not isinstance(since, bool) and 0 < since <= now:
                    self._ledger_since_ts = float(since)
                sr = led.get("since_recovery")
                if isinstance(sr, dict):
                    sent = sr.get("attempted", sr.get("sent"))
                    sent = sent if (isinstance(sent, int) and not isinstance(sent, bool)) else -1
                    ts = sr.get("since_ts")
                    if sent >= 0 and isinstance(ts, (int, float)) and not isinstance(ts, bool) and 0 < ts <= now:
                        self._sent_since_recovery = sent
                        self._since_recovery_ts = float(ts)
        except Exception:  # noqa: BLE001
            self._ledger = {}
        try:
            eps = self.config.get("quota_episodes")
            clean = []
            for ep in (eps if isinstance(eps, list) else ()):
                ok = self._sanitize_episode(ep)
                if ok is not None:
                    clean.append(ok)
            self._quota_episodes = clean[-self.QUOTA_EPISODE_HISTORY:]
        except Exception:  # noqa: BLE001
            self._quota_episodes = []

    # --- strict allowlist for persisted quota-episode evidence -------------------- #
    # Anything restored from disk is rebuilt field by field from this schema, so an
    # unexpected key (or an unexpected value in an expected key) can never reach MQTT
    # diagnostics. Strings must match a fixed shape; there is no free-text field.
    _ISO_RE      = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\+00:00$")
    _DAY_RE      = re.compile(r"^\d{4}-\d{2}-\d{2}$")
    _REASON_OK   = ("http_429", "quota_message")
    _MAX_COUNT   = 10 ** 9

    @classmethod
    def _c_int(cls, v, nullable=False):
        if v is None and nullable:
            return None
        if isinstance(v, bool) or not isinstance(v, int) or not 0 <= v <= cls._MAX_COUNT:
            raise ValueError("count")
        return v

    @classmethod
    def _c_str(cls, v, rx, nullable=False):
        if v is None and nullable:
            return None
        if not isinstance(v, str) or len(v) > 40 or not rx.match(v):
            raise ValueError("string")
        return v

    @classmethod
    def _c_num(cls, v, nullable=False):
        if v is None and nullable:
            return None
        if (isinstance(v, bool) or not isinstance(v, (int, float))
                or not math.isfinite(v) or not 0 <= v <= 10 ** 9):
            raise ValueError("number")
        return v

    @classmethod
    def _c_counts(cls, d, keys):
        if not isinstance(d, dict):
            raise ValueError("counts")
        return {k: cls._c_int(d[k]) for k in keys}

    @classmethod
    def _c_evidence(cls, d):
        """Rebuild the counts-only evidence block shared by both snapshots."""
        if not isinstance(d, dict):
            raise ValueError("evidence")
        day_keys = ("status", "command", "auth", "other", "total",
                    "rate_limited_responses", "blocked")
        roll = d["rolling_24h"]
        if not isinstance(roll, dict):
            raise ValueError("rolling")
        roll_keys = cls.LEDGER_FIELDS + ("total", "attempted", "responses_received")
        rolling = {k: cls._c_int(roll[k]) for k in roll_keys}
        rolling["window_s"] = cls._c_int(roll["window_s"])
        rolling["resolution_s"] = cls._c_int(roll["resolution_s"])
        rolling["oldest_bucket"] = cls._c_str(roll["oldest_bucket"], cls._ISO_RE, True)
        rolling["counting_since"] = cls._c_str(roll["counting_since"], cls._ISO_RE, True)
        if not isinstance(roll["complete"], bool):
            raise ValueError("complete")
        rolling["complete"] = roll["complete"]
        sr = d["since_recovery"]
        if not isinstance(sr, dict):
            raise ValueError("since_recovery")
        att = sr.get("attempted", sr.get("sent"))
        return {
            "utc_day": cls._c_str(d["utc_day"], cls._DAY_RE),
            "utc_day_counters": cls._c_counts(d["utc_day_counters"], day_keys),
            "rolling_24h": rolling,
            "since_recovery": {"attempted": cls._c_int(att),
                               "since": cls._c_str(sr.get("since"), cls._ISO_RE, True)},
        }

    @classmethod
    def _sanitize_episode(cls, ep):
        """Rebuilt, allowlisted copy of a persisted episode, or None if it is invalid."""
        try:
            if not isinstance(ep, dict):
                return None
            out = {"id": cls._c_int(ep["id"]),
                   "rejected_responses": cls._c_int(ep["rejected_responses"])}
            first, rec = ep.get("first_rejection"), ep.get("recovery")
            if first is None:
                out["first_rejection"] = None
            else:
                if not isinstance(first, dict) or first.get("reason_class") not in cls._REASON_OK:
                    return None
                f = {"at": cls._c_str(first["at"], cls._ISO_RE),
                     "reason_class": first["reason_class"],
                     "includes_rejected_request": first["includes_rejected_request"] is True}
                f.update(cls._c_evidence(first))
                out["first_rejection"] = f
            if rec is None:
                out["recovery"] = None
            else:
                if not isinstance(rec, dict):
                    return None
                r = {"at": cls._c_str(rec["at"], cls._ISO_RE)}
                r.update(cls._c_evidence(rec))
                r["rejected_responses_in_episode"] = cls._c_int(rec["rejected_responses_in_episode"])
                r["attempted_after_first_rejection"] = cls._c_int(
                    rec.get("attempted_after_first_rejection", rec.get("sent_after_first_rejection")),
                    nullable=True)
                r["episode_duration_s"] = cls._c_num(rec.get("episode_duration_s"), nullable=True)
                out["recovery"] = r
            return out
        except (KeyError, TypeError, ValueError):
            return None

    def _rate_limit_remaining(self, now=None) -> float:
        """Seconds of rate-limit backoff left (0 when not limited)."""
        now = time.time() if now is None else now
        with self._rate_limit_lock:
            return max(0.0, self._rate_limit_until - now)

    def _raise_if_rate_limited(self):
        remaining = self._rate_limit_remaining()
        if remaining > 0:
            with self._rate_limit_lock:
                self._request_counters["blocked"] += 1
                self._ledger_add_locked("blocked", time.time())
                self._counters_dirty = True
            raise RateLimitedError(f"rate limited — no requests for {int(remaining)}s")

    @staticmethod
    def _json_body(resp):
        """Parsed JSON body or None. Body-less or non-JSON responses are normal."""
        try:
            return resp.json()
        except Exception:
            return None

    def _detect_rate_limit(self, resp):
        """Return (reason, retry_after_s) when resp is a confirmed rate limit, else None.

        Recognised: HTTP 429, or a JSON object whose `error` is the confirmed
        'Daily request limit exceeded' message. The `success` flag is deliberately
        ignored: a body carrying the quota message is never a clean success, and the
        same predicate drives both backoff and command rejection so they cannot
        disagree. Any other invalid payload (e.g. 'Water heater not found') is NOT
        a rate limit.
        """
        if resp is None:
            return None
        retry_after = None
        if resp.status_code == 429:
            headers = getattr(resp, "headers", None)
            raw = headers.get("Retry-After") if hasattr(headers, "get") else None
            if isinstance(raw, (str, int, float)) and not isinstance(raw, bool):
                try:
                    retry_after = max(0.0, float(raw))
                except ValueError:
                    retry_after = None
            return "HTTP 429", retry_after
        body = self._json_body(resp)
        if isinstance(body, dict):
            error = body.get("error")
            if isinstance(error, str) and self.RATE_LIMIT_API_MESSAGE in error.strip().lower():
                return "Daily request limit exceeded", None
        return None

    def _note_response(self, resp):
        """Classify resp once; register a rate limit event if it is one.

        The verdict is memoised on the response object, so the same HTTP response
        reaching request(), poll_status() and _submit_command() is counted and
        registered exactly once. Returns True iff resp is a rate limit.
        """
        try:
            cached = resp.__dict__.get("_tw_rate_limit_verdict")
        except Exception:  # noqa: BLE001 - exotic response objects: classify, don't memoise
            cached = None
        if cached is not None:
            return cached
        try:
            detected = self._detect_rate_limit(resp)
        except Exception:  # noqa: BLE001
            detected = None
        verdict = detected is not None
        try:
            resp.__dict__["_tw_rate_limit_verdict"] = verdict
        except Exception:  # noqa: BLE001
            pass
        if verdict:
            self._enter_rate_limit(*detected)
        return verdict

    def _enter_rate_limit(self, reason: str, retry_after=None):
        """Start (or extend a no-op into) a bounded exponential backoff window.

        Only the first detection after a window has expired advances the step, so
        concurrent detections from the poll and MQTT threads never double-step.
        """
        now = time.time()
        with self._rate_limit_lock:
            self._request_counters["rate_limited_responses"] += 1
            self._ledger_add_locked("rate_limited_responses", now)
            open_ep = self._open_episode_locked()
            if open_ep is not None:
                open_ep["rejected_responses"] += 1
            self._rate_limit_generation += 1
            self._counters_dirty = True
            self._rate_limit_last_at = self._isoformat_epoch(now)
            self._rate_limit_reason = reason
            if now < self._rate_limit_until:
                return
            self.rate_limit_backoff += 1
            self._rate_limit_events_total += 1
            if self.rate_limit_backoff == 1 and open_ep is None:
                self._open_quota_episode_locked(now, reason)
            delay = min(
                self.RATE_LIMIT_BASE_BACKOFF * 2 ** (self.rate_limit_backoff - 1),
                self.RATE_LIMIT_MAX_BACKOFF,
            )
            if retry_after:
                delay = min(max(delay, retry_after), self.RATE_LIMIT_MAX_BACKOFF)
            self._rate_limit_until = now + delay
            self.current_poll_interval = int(delay)
            step = self.rate_limit_backoff
        print(f"[RATE LIMIT] {reason} — backing off {int(delay)}s (step {step}); "
              "no requests or commands will be sent until the window ends")
        self._persist_rate_limit()

    def _rate_limit_gen(self) -> int:
        with self._rate_limit_lock:
            return self._rate_limit_generation

    def _clear_rate_limit(self, observed_generation=None) -> bool:
        """Reset backoff after a good poll. Returns True if a limit state was cleared.

        `observed_generation` is the rate-limit generation read BEFORE the poll
        began. If any rate-limit response has been classified since (for example
        from a command on the MQTT thread), the poll is older than that event and
        must not clear it. An unexpired window is never cleared either.
        """
        with self._rate_limit_lock:
            if self.rate_limit_backoff == 0 and self._rate_limit_until == 0.0:
                return False
            if observed_generation is not None and observed_generation != self._rate_limit_generation:
                return False
            if time.time() < self._rate_limit_until:
                return False
            self.rate_limit_backoff = 0
            self._rate_limit_until = 0.0
            self._rate_limit_recovered_at = self._isoformat_epoch(time.time())
            self._close_quota_episode_locked(time.time())
        self._persist_rate_limit()
        print("[RATE LIMIT] Recovered — backoff cleared")
        return True

    def _rate_limit_snapshot(self) -> dict:
        with self._rate_limit_lock:
            return {
                "until": self._rate_limit_until,
                "step": self.rate_limit_backoff,
                "reason": self._rate_limit_reason,
                "last_at": self._rate_limit_last_at,
                "recovered_at": self._rate_limit_recovered_at,
                "events_total": self._rate_limit_events_total,
            }

    def _persist_rate_limit(self):
        """Persist backoff state (written only on state transitions) plus counters."""
        snap = self._rate_limit_snapshot()
        try:
            with self._config_lock:
                self.config["rate_limit"] = snap
                self._store_counters_locked()
                self._save_config()
        except Exception as e:
            print(f"[WARN] Failed to persist rate-limit state: {e}")

    def _store_counters_locked(self):
        """Copy counters into config (caller holds _config_lock and saves)."""
        with self._rate_limit_lock:
            self.config["request_counters"] = dict(self._request_counters)
            self.config["request_ledger"] = {
                "bucket_s": self.LEDGER_BUCKET_S,
                "since_ts": self._ledger_since_ts,
                "since_recovery": {"attempted": self._sent_since_recovery,
                                   "since_ts": self._since_recovery_ts},
                "buckets": {str(k): list(v) for k, v in self._ledger.items()},
            }
            self.config["quota_episodes"] = json.loads(json.dumps(self._quota_episodes))
            self._counters_dirty = False
        self._counters_last_flush = time.time()

    @contextlib.contextmanager
    def _transport_guard(self):
        """Record an attempt that ended without any HTTP response (timeout, reset, DNS,
        TLS). Whether the vendor received or counted such a request is unknown, so it is
        kept apart from requests that were answered. The exception is always re-raised."""
        try:
            yield
        except Exception:
            self._io.transport_failed = True  # lets the poll loop retry only real transport failures
            try:
                with self._rate_limit_lock:
                    self._ledger_add_locked("transport_failed", time.time())
                    self._counters_dirty = True
            except Exception:  # noqa: BLE001 - observability must not mask the real error
                pass
            raise

    def _count_request(self, category: str):
        today = self._utc_day()
        with self._rate_limit_lock:
            if self._request_counters["day"] != today:
                self._request_counters = self._new_counters(today)
            self._request_counters[category] += 1
            self._request_counters["total"] += 1
            self._ledger_add_locked(category, time.time())
            self._sent_since_recovery += 1
            self._counters_dirty = True

    # ------------------------------------------------------------------ #
    #  Rolling request ledger and quota-episode evidence (observability)  #
    # ------------------------------------------------------------------ #

    def _ledger_add_locked(self, field: str, now: float):
        """Add one event to the current ledger bucket. Caller holds _rate_limit_lock."""
        width = self.LEDGER_BUCKET_S
        start = int(now // width) * width
        row = self._ledger.get(start)
        if row is None:
            row = self._ledger[start] = [0] * len(self.LEDGER_FIELDS)
            horizon = now - self.LEDGER_WINDOW_S
            for old in [k for k in self._ledger if k + width <= horizon]:
                del self._ledger[old]
        row[self.LEDGER_FIELDS.index(field)] += 1

    def _rolling_locked(self, now: float) -> dict:
        """Rolling-24h counts. Caller holds _rate_limit_lock.

        A bucket is included while any part of it is inside the window, so the
        figures are an upper bound that can include up to LEDGER_BUCKET_S (15 min)
        of older requests. `complete` is False until the ledger has been counting
        for a full window (for example the first 24 h after install).
        """
        width = self.LEDGER_BUCKET_S
        horizon = now - self.LEDGER_WINDOW_S
        totals = [0] * len(self.LEDGER_FIELDS)
        oldest = None
        for start, row in self._ledger.items():
            if start + width > horizon and start <= now:
                oldest = start if oldest is None else min(oldest, start)
                for i, v in enumerate(row):
                    totals[i] += v
        out = dict(zip(self.LEDGER_FIELDS, totals))
        # `attempted` = requests this bridge tried to send. It is NOT a vendor-accepted
        # count: `transport_failed` of them ended without a response (vendor receipt
        # unknown) and `rate_limited_responses` were answered with a quota rejection.
        out["total"] = out["status"] + out["command"] + out["auth"] + out["other"]
        out["attempted"] = out["total"]
        out["responses_received"] = out["total"] - out["transport_failed"]
        out["window_s"] = self.LEDGER_WINDOW_S
        out["resolution_s"] = width
        out["oldest_bucket"] = self._isoformat_epoch(oldest)
        out["counting_since"] = self._isoformat_epoch(self._ledger_since_ts)
        out["complete"] = self._ledger_since_ts <= horizon
        return out

    def _evidence_counts_locked(self, now: float) -> dict:
        """Counts-only evidence block shared by episode snapshots. Caller holds the lock."""
        day = self._utc_day()
        c = self._request_counters
        fields = ("status", "command", "auth", "other", "total", "rate_limited_responses", "blocked")
        day_counts = {k: (c[k] if c["day"] == day else 0) for k in fields}
        return {
            "utc_day": day,
            "utc_day_counters": day_counts,
            "rolling_24h": self._rolling_locked(now),
            "since_recovery": {"attempted": self._sent_since_recovery,
                               "since": self._isoformat_epoch(self._since_recovery_ts)},
        }

    def _open_episode_locked(self):
        if self._quota_episodes and self._quota_episodes[-1].get("recovery") is None:
            return self._quota_episodes[-1]
        return None

    def _open_quota_episode_locked(self, now: float, reason: str):
        """Record the immutable first-rejection snapshot of a new quota episode.

        Counts include the rejected request itself (it was attempted and counted before
        its response was classified; a response was received, so the vendor did see it). `reason_class` is a fixed label, never response
        text.
        """
        nxt = (self._quota_episodes[-1]["id"] + 1) if self._quota_episodes else 1
        snapshot = {
            "at": self._isoformat_epoch(now),
            "reason_class": "http_429" if reason == "HTTP 429" else "quota_message",
            "includes_rejected_request": True,
        }
        snapshot.update(self._evidence_counts_locked(now))
        self._quota_episodes.append({
            "id": nxt,
            "first_rejection": snapshot,
            "rejected_responses": 1,
            "recovery": None,
        })
        del self._quota_episodes[:-self.QUOTA_EPISODE_HISTORY]

    def _close_quota_episode_locked(self, now: float):
        """Record the immutable confirmed-recovery snapshot, once, then restart the
        since-recovery counter. Caller holds _rate_limit_lock."""
        ep = self._open_episode_locked()
        if ep is None:
            # Recovery of a limit whose start predates this ledger (for example a
            # window persisted by 1.8.0): record it without inventing a start.
            nxt = (self._quota_episodes[-1]["id"] + 1) if self._quota_episodes else 1
            ep = {"id": nxt, "first_rejection": None, "rejected_responses": 0, "recovery": None}
            self._quota_episodes.append(ep)
            del self._quota_episodes[:-self.QUOTA_EPISODE_HISTORY]
        first = ep.get("first_rejection")
        recovery = {"at": self._isoformat_epoch(now)}
        recovery.update(self._evidence_counts_locked(now))
        recovery["rejected_responses_in_episode"] = ep["rejected_responses"]
        recovery["attempted_after_first_rejection"] = None
        recovery["episode_duration_s"] = None
        if first is not None:
            first_since = (first.get("since_recovery") or {}).get("attempted")
            if isinstance(first_since, int) and self._sent_since_recovery >= first_since:
                recovery["attempted_after_first_rejection"] = self._sent_since_recovery - first_since
            try:
                t0 = datetime.datetime.strptime(first["at"], "%Y-%m-%dT%H:%M:%S+00:00").replace(
                    tzinfo=datetime.timezone.utc).timestamp()
                recovery["episode_duration_s"] = round(max(0.0, now - t0), 1)
            except (KeyError, TypeError, ValueError):
                pass
        ep["recovery"] = recovery
        self._sent_since_recovery = 0
        self._since_recovery_ts = now

    def _maybe_flush_counters(self, force: bool = False):
        """Throttled counter persistence: at most one write per COUNTER_FLUSH_INTERVAL."""
        with self._rate_limit_lock:
            dirty = self._counters_dirty
        if not dirty:
            return
        if not force and time.time() - self._counters_last_flush < self.COUNTER_FLUSH_INTERVAL:
            return
        try:
            with self._config_lock:
                self._store_counters_locked()
                self._save_config()
        except Exception as e:
            print(f"[WARN] Failed to persist request counters: {e}")

    def _rate_limit_diagnostics(self, now: float) -> dict:
        snap = self._rate_limit_snapshot()
        active = snap["until"] > now
        # Three-way lifecycle (plus "none" before any event):
        #   active               - backoff window running, no API traffic
        #   recovery_unverified  - window over, but no successful poll has confirmed recovery
        #   recovered            - a successful poll started after the last event cleared it
        if snap["step"] > 0:
            state = "active" if active else "recovery_unverified"
        else:
            state = "recovered" if snap["events_total"] > 0 else "none"
        return {
            "state": state,
            "active": active,
            "retry_at": self._isoformat_epoch(snap["until"]) if active else None,
            "backoff_step": snap["step"],
            "last_reason": snap["reason"],
            "last_at": snap["last_at"],
            "recovered_at": snap["recovered_at"],
            "events_total": snap["events_total"],
            "episodes": self._episodes_diagnostics(),
        }

    def _episodes_diagnostics(self) -> list:
        """Most recent quota episodes (counts only), newest last, as detached copies."""
        with self._rate_limit_lock:
            recent = self._quota_episodes[-self.QUOTA_EPISODE_DIAG:]
            return json.loads(json.dumps(recent))

    def _next_pending_deadline(self):
        """Earliest unexpired pending-command deadline across devices, or None."""
        with self._command_lock:
            deadlines = [
                r["deadline"]
                for records in self._command_state.values()
                for r in records.values()
                if r.get("status") == "pending"
            ]
        return min(deadlines) if deadlines else None

    def _publish_all_diagnostics(self):
        with self._config_lock:
            serials = list(self.config.get("devices", {}).keys())
        for serial in serials:
            self._publish_diagnostics(serial)

    def _wait_for_next_poll(self, interval: float):
        """Sleep until the next poll is due.

        A command wake-up ends the wait early ONLY when no rate-limit window is
        active; while limited, wake-ups are discarded so neither the 60 s poll,
        command-triggered fast polling nor a stale wake can reach the API early.
        Pending commands are still expired (and republished) on their deadline.
        """
        end = time.time() + interval
        was_limited = False
        while True:
            now = time.time()
            limited_for = self._rate_limit_remaining(now)
            if limited_for <= 0 and was_limited:
                was_limited = False
                self._publish_all_diagnostics()  # window over: show recovery_unverified now
            if limited_for > 0:
                was_limited = True
                end = max(end, now + limited_for)
                timeout = min(limited_for, self.DIAG_REFRESH_INTERVAL)
                deadline = self._next_pending_deadline()
                if deadline is not None:
                    timeout = min(timeout, max(deadline - now, 0.0) + 0.05)
                if self._poll_wakeup.wait(timeout=timeout):
                    self._poll_wakeup.clear()  # discard — rate limit has priority
                    continue
                # Keep telemetry age / retry time / pending expiry current (MQTT only).
                self._publish_all_diagnostics()
                continue
            remaining = end - now
            if remaining <= 0:
                return
            # A slow background cadence must not delay availability: also wake when an
            # online device would age out, publish (MQTT only), and keep waiting.
            deadline = self._next_offline_deadline()
            timeout = remaining
            if deadline is not None and now < deadline < end:
                timeout = deadline - now + 0.05
            if self._poll_wakeup.wait(timeout=timeout):
                if self._rate_limit_remaining() > 0:
                    continue  # limit began while waiting — re-evaluate
                return
            if timeout < remaining:
                self._publish_all_diagnostics()

    def _wait_out_persisted_rate_limit(self):
        """On boot, honour a backoff window persisted by the previous run."""
        remaining = self._rate_limit_remaining()
        if remaining <= 0:
            return
        print(f"[RATE LIMIT] Persisted backoff active — waiting {int(remaining)}s before any API request")
        self._publish_all_diagnostics()
        while True:
            remaining = self._rate_limit_remaining()
            if remaining <= 0:
                break
            time.sleep(min(remaining, 30))

    # ------------------------------------------------------------------ #
    #  HTTP session helpers                                                #
    # ------------------------------------------------------------------ #

    def _reset_headers(self):
        """Mirrors ResetHeaders() from the C# app — keeps Auth, clears rest."""
        auth_header = self.session.headers.get("Authorization")
        self.session.headers.clear()
        if auth_header:
            self.session.headers["Authorization"] = auth_header
        self.session.headers.update({
            "app":      "MyThermowatt",
            "platform": "iOS",
            "version":  "3.14",
            "lang":     "en"
        })

    def _update_auth(self, access, refresh):
        # Hold the same lock the poll thread uses for config writes. This path is
        # reachable from the MQTT callback thread (command → 401 → refresh_session)
        # concurrently with the main poll thread's energy_kwh write; without the
        # lock, two threads json.dump() the same dict and can corrupt the file or
        # drop the persisted energy counter. (Caller already holds _http_lock; the
        # _http_lock → _config_lock order is consistent everywhere, so no deadlock.)
        with self._config_lock:
            self.config.update({"access_token": access, "refresh_token": refresh})
            self._save_config()
        self.session.headers["Authorization"] = f"Bearer {access}"

    def login(self):
        with self._http_lock:
            self._raise_if_rate_limited()
            self._reset_headers()
            self.session.headers["x-api-key"] = self.API_KEY
            payload = {"username": EMAIL, "password": PASSWORD, "device_id": self.config["device_uuid"]}
            self._count_request("auth")
            with self._transport_guard():
                r = self.session.post(f"{self.BASE_URL}/login", json=payload, verify=TLS_VERIFY, timeout=HTTP_TIMEOUT)
            if self._note_response(r):
                raise RateLimitedError("login rejected: rate limited")
            r.raise_for_status()
            res = r.json()['result']
            self._update_auth(res['accessToken'], res['refreshToken'])

    def refresh_session(self):
        with self._http_lock:
            self._raise_if_rate_limited()
            self._reset_headers()
            self.session.headers["x-api-key"] = self.API_KEY
            payload = {"username": EMAIL, "refreshToken": self.config["refresh_token"]}
            self._count_request("auth")
            with self._transport_guard():
                r = self.session.post(f"{self.BASE_URL}/refresh", json=payload, verify=TLS_VERIFY, timeout=HTTP_TIMEOUT)
            if self._note_response(r):
                raise RateLimitedError("token refresh rejected: rate limited")
            if r.status_code == 200:
                res = r.json()['result']
                self._update_auth(res['accessToken'], res['refreshToken'])
                return True
            return False

    @staticmethod
    def _jwt_exp(token):
        """The `exp` claim (epoch seconds) of a JWT, or None if absent/malformed.

        Only the payload's `exp` is read; the token is never verified, logged or
        stored by this helper.
        """
        try:
            payload = token.split(".")[1]
            def _reject(_name):  # NaN / Infinity / -Infinity literals are not valid JSON
                raise ValueError("non-finite JSON constant")
            claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)),
                                parse_constant=_reject)
            exp = claims.get("exp")
            if isinstance(exp, bool) or not isinstance(exp, (int, float)):
                return None
            exp = float(exp)  # 1e999 parses to inf; huge ints raise OverflowError (caught)
            if not math.isfinite(exp) or not 0 < exp < 32503680000:  # before year 3000
                return None
            return exp
        except Exception:  # noqa: BLE001
            return None

    def _maybe_proactive_refresh(self):
        """Refresh the access token shortly before it expires (poll thread only).

        Safety properties (each covered by tests):
        - never sends anything while a quota backoff window is active, nor while
          recovery is still unverified (rate_limit step > 0): those probes keep the
          existing minimal reactive behaviour;
        - at most one attempt per access token, so a rejected or failing refresh can
          never turn into a refresh-per-cycle loop;
        - failures are swallowed: the reactive 401 -> refresh -> replay path and
          re-login remain the fallback, and a failed proactive refresh is not a poll
          failure;
        - a quota response during the refresh enters the normal backoff (via
          _note_response) like any other request;
        - never used on the command path.
        """
        if not self.PROACTIVE_REFRESH:
            return
        if self._rate_limit_remaining() > 0 or self._rate_limit_snapshot()["step"] > 0:
            return
        with self._config_lock:
            token = self.config.get("access_token")
            refresh = self.config.get("refresh_token")
        if not token or not refresh:
            return
        exp = self._jwt_exp(token)
        if exp is None or exp - time.time() > max(self.PROACTIVE_REFRESH_MARGIN_S, self.POLL_INTERVAL + 60):
            return
        if self._proactive_attempt_exp == exp:
            return
        now = time.time()
        last = self._proactive_last_attempt_at
        if last is not None and 0 <= now - last < self.PROACTIVE_MIN_SPACING_S:
            return  # hard floor: a server issuing short-lived tokens cannot cause a refresh per poll
        self._proactive_attempt_exp = exp
        self._proactive_last_attempt_at = now
        try:
            ok = self.refresh_session()
        except RateLimitedError:
            print("[AUTH] Proactive token refresh deferred: rate limited")
            return
        except Exception as e:  # noqa: BLE001
            print(f"[AUTH] Proactive token refresh failed ({type(e).__name__}); "
                  "reactive refresh remains available")
            return
        print("[AUTH] Proactive token refresh " + (
            "succeeded" if ok else "rejected; reactive refresh remains available"))

    def request(self, method, endpoint, serial=None, **kwargs):
        with self._http_lock:
            # Fail closed before any I/O while a rate-limit window is active.
            self._raise_if_rate_limited()
            category = (
                "status" if endpoint == "/status"
                else "command" if method.upper() == "POST"
                else "other"
            )
            self._reset_headers()
            url = f"{self.BASE_URL}{endpoint}"
            if serial:
                self.session.headers["seriale"] = serial
            kwargs.setdefault("timeout", HTTP_TIMEOUT)
            self._count_request(category)
            with self._transport_guard():
                resp = self.session.request(method, url, verify=TLS_VERIFY, **kwargs)
            if self._note_response(resp):
                return resp  # quota errors never trigger re-authentication
            if resp.status_code == 401:
                # Pre-existing, bounded auth replay (unrelated to rate-limit recovery):
                # on HTTP 401 the token is refreshed and the SAME request - including a
                # command POST with identical body - is re-sent exactly once, inside this
                # call. A 401 is an authentication refusal, so the first attempt is
                # assumed not to have been executed (vendor behaviour unverified). A
                # second 401, a failed refresh or a rate limit ends the call; there is no
                # loop, queue or later re-send.
                if self.refresh_session():
                    self._reset_headers()
                    if serial:
                        self.session.headers["seriale"] = serial
                    self._count_request(category)
                    with self._transport_guard():
                        resp = self.session.request(method, url, verify=TLS_VERIFY, **kwargs)
                    self._note_response(resp)
            return resp

    # ------------------------------------------------------------------ #
    #  MQTT callbacks                                                      #
    # ------------------------------------------------------------------ #

    def on_connect(self, client, userdata, connect_flags, reason_code, properties):
        """Re-publishes availability and re-subscribes CMD topics on every (re)connect.
        Fires in the paho background thread, not the main polling thread.
        """
        if reason_code == 0:
            client.publish(AVAILABILITY_TOPIC, "online", retain=True)
            with self._config_lock:
                device_serials = list(self.config.get('devices', {}).keys())
            for serial in device_serials:
                client.subscribe(f"P/{serial}/CMD/#")
                client.subscribe(f"P/{serial}/refresh")
                # Restore per-device availability consistent with current failure state
                dev_avail = self._avail_state[serial] = self._availability_for(serial, time.time())
                client.publish(f"P/{serial}/availability", dev_avail, retain=True)
                print(f"[MQTT] (Re)subscribed to P/{serial}/CMD/#")
            print("[MQTT] Connected — subscriptions restored, availability published online.")
        else:
            print(f"[MQTT] Connection failed: reason_code={reason_code}")

    def on_disconnect(self, client, userdata, disconnect_flags, reason_code, properties):
        print(f"[MQTT] Disconnected: reason_code={reason_code}. Paho will auto-reconnect.")

    # ------------------------------------------------------------------ #
    #  MQTT discovery                                                      #
    # ------------------------------------------------------------------ #

    def _device_block(self, serial, name):
        return {"identifiers": [f"tw_{serial}"], "manufacturer": "Thermowatt", "name": name}

    def _availability_block(self, serial: str) -> dict:
        """Multi-availability: bridge-level LWT AND per-device poll health.
        The entity goes unavailable if either the bridge disconnects (LWT fires)
        or sustained poll failures mark the device offline.
        """
        return {
            "availability": [
                {
                    "topic":                 AVAILABILITY_TOPIC,
                    "payload_available":     "online",
                    "payload_not_available": "offline",
                },
                {
                    "topic":                 f"P/{serial}/availability",
                    "payload_available":     "online",
                    "payload_not_available": "offline",
                },
            ],
            "availability_mode": "all",
        }

    def _bridge_availability_block(self) -> dict:
        """Bridge-level availability only — used for diagnostic sensors so they remain
        visible (and readable) even when per-device polling is failing.
        """
        return {
            "availability_topic":    AVAILABILITY_TOPIC,
            "payload_available":     "online",
            "payload_not_available": "offline",
        }

    def publish_discovery(self, serial, name):
        device       = self._device_block(serial, name)
        avail        = self._availability_block(serial)
        diag_avail   = self._bridge_availability_block()
        status_topic = f"P/{serial}/STATUS"
        diag_topic   = f"P/{serial}/diagnostics"

        # ── Water Heater entity ─────────────────────────────────────────
        wh_payload = {
            "unique_id":                    f"thermowatt_{serial}_v314",
            "name":                         f"Boiler {name}",
            "temp_unit":                    "C",
            "min_temp":                     self.TEMP_MIN,
            "max_temp":                     self.TEMP_MAX,
            "current_temperature_topic":    status_topic,
            "current_temperature_template": "{{ value_json.result.T_Avg | default(0) | float }}",
            "temperature_state_topic":      status_topic,
            "temperature_state_template":   "{{ value_json.result.T_SetPoint | default(0) | float }}",
            "temperature_command_topic":    f"P/{serial}/CMD/TEMP",
            "mode_state_topic":             status_topic,
            # ha_mode is computed bridge-side (decode_cmd) from the freshly polled raw
            # Cmd bitfield so the family/enabled decode logic lives in one place, not
            # duplicated in Jinja. Per HA MQTT water_heater semantics, an empty payload
            # is IGNORED (it would retain a stale prior mode), while the literal string
            # "None" resets the operation mode to unknown. Unknown/absent/malformed Cmd
            # must therefore render as "None", not "" — never as "Off".
            "mode_state_template": "{{ value_json.result.ha_mode | default('None', true) }}",
            "mode_command_topic":       f"P/{serial}/CMD/MODE",
            "modes":                    ["Off", "Eco", "Manual", "Auto", "Holiday"],
            "json_attributes_topic":    status_topic,
            "json_attributes_template": "{{ value_json.result | tojson }}",
            "device":                   device,
            **avail,
        }
        self.mqtt_client.publish(
            f"homeassistant/water_heater/{serial}/config",
            json.dumps(wh_payload), retain=True
        )

        # ── Binary sensor: Heating active ───────────────────────────────
        heating_payload = {
            "unique_id":      f"thermowatt_{serial}_heating",
            "name":           f"{name} Heating",
            "state_topic":    status_topic,
            "value_template": "{{ value_json.result.heating | default(false) | lower }}",
            "payload_on":     "true",
            "payload_off":    "false",
            "device_class":   "heat",
            "icon":           "mdi:fire",
            "device":         device,
            **avail,
        }
        self.mqtt_client.publish(
            f"homeassistant/binary_sensor/{serial}/heating/config",
            json.dumps(heating_payload), retain=True
        )

        # ── Power sensor — real-time draw derived from heating state ────
        # Uses configured element wattage; not a CT measurement — heating-state estimate only.
        power_payload = {
            "unique_id":            f"thermowatt_{serial}_power_w",
            "name":                 f"{name} Power",
            "state_topic":          status_topic,
            "value_template":       f"{{{{ {self.element_wattage} if value_json.result.heating else 0 }}}}",
            "unit_of_measurement":  "W",
            "device_class":         "power",
            "state_class":          "measurement",
            "icon":                 "mdi:lightning-bolt",
            "device":               device,
            **avail,
        }
        self.mqtt_client.publish(
            f"homeassistant/sensor/{serial}/power/config",
            json.dumps(power_payload), retain=True
        )

        # ── Energy sensor — accumulated kWh (bridge-side integration) ──
        # Persisted in config.json — survives bridge restarts.
        # state_class: total_increasing qualifies for HA Energy Dashboard.
        energy_payload = {
            "unique_id":            f"thermowatt_{serial}_energy_kwh",
            "name":                 f"{name} Energy kWh",
            "state_topic":          f"P/{serial}/energy_kwh",
            "value_template":       "{{ value | float(0) }}",
            "unit_of_measurement":  "kWh",
            "device_class":         "energy",
            "state_class":          "total_increasing",
            "icon":                 "mdi:lightning-bolt-circle",
            "device":               device,
            **avail,
        }
        self.mqtt_client.publish(
            f"homeassistant/sensor/{serial}/energy_kwh/config",
            json.dumps(energy_payload), retain=True
        )

        # ── Individual sensors for EMS-critical fields ──────────────────
        sensors = [
            {
                "unique_id":            f"thermowatt_{serial}_t_avg",
                "name":                 f"{name} Average Temperature",
                "state_topic":          status_topic,
                "value_template":       "{{ value_json.result.T_Avg | default(0) | float | round(1) }}",
                "unit_of_measurement":  "°C",
                "device_class":         "temperature",
                "state_class":          "measurement",
                "icon":                 "mdi:thermometer-water",
                "slug":                 "t_avg",
            },
            {
                "unique_id":            f"thermowatt_{serial}_t_desired",
                "name":                 f"{name} Desired Temperature",
                "state_topic":          status_topic,
                "value_template":       "{{ value_json.result.T_dsrd | default(0) | float | round(1) }}",
                "unit_of_measurement":  "°C",
                "device_class":         "temperature",
                "state_class":          "measurement",
                "icon":                 "mdi:thermometer-chevron-up",
                "slug":                 "t_desired",
            },
            {
                "unique_id":            f"thermowatt_{serial}_t_boost",
                "name":                 f"{name} Boost Ceiling",
                "state_topic":          status_topic,
                "value_template":       "{{ value_json.result.TBoost | default(0) | float | round(1) }}",
                "unit_of_measurement":  "°C",
                "device_class":         "temperature",
                "state_class":          "measurement",
                "icon":                 "mdi:thermometer-high",
                "slug":                 "t_boost",
            },
            {
                "unique_id":            f"thermowatt_{serial}_time_eco",
                "name":                 f"{name} Eco Runtime",
                "state_topic":          status_topic,
                "value_template":       "{{ value_json.result.Time_eco | default(0) | int }}",
                "unit_of_measurement":  "min",
                "state_class":          "measurement",
                "icon":                 "mdi:timer-outline",
                "slug":                 "time_eco",
            },
            {
                "unique_id":            f"thermowatt_{serial}_time_prog",
                "name":                 f"{name} Programme Runtime",
                "state_topic":          status_topic,
                "value_template":       "{{ value_json.result.Time_prog | default(0) | int }}",
                "unit_of_measurement":  "min",
                "state_class":          "measurement",
                "icon":                 "mdi:timer-check-outline",
                "slug":                 "time_prog",
            },
            {
                "unique_id":            f"thermowatt_{serial}_rssi",
                "name":                 f"{name} WiFi Signal",
                "state_topic":          status_topic,
                "value_template":       "{{ value_json.result.Rssi | default(0) | int }}",
                "unit_of_measurement":  "dBm",
                "device_class":         "signal_strength",
                "state_class":          "measurement",
                "entity_category":      "diagnostic",
                "icon":                 "mdi:wifi",
                "slug":                 "rssi",
            },
            {
                "unique_id":        f"thermowatt_{serial}_last_polled",
                "name":             f"{name} Last Polled",
                "state_topic":      status_topic,
                # Return none (null) not string 'unknown' — HA timestamp device_class
                # requires a valid ISO8601 value or none, not a bare string.
                "value_template":   (
                    "{% if value_json.result.last_polled_at is defined %}"
                    "{{ value_json.result.last_polled_at }}"
                    "{% else %}{{ none }}{% endif %}"
                ),
                "device_class":     "timestamp",
                "entity_category":  "diagnostic",
                "icon":             "mdi:clock-check-outline",
                "slug":             "last_polled",
            },
            {
                "unique_id":            f"thermowatt_{serial}_t_amb",
                "name":                 f"{name} Ambient Temperature",
                "state_topic":          status_topic,
                "value_template":       "{{ value_json.result.TAmb | default(0) | float | round(1) }}",
                "unit_of_measurement":  "°C",
                "device_class":         "temperature",
                "state_class":          "measurement",
                "entity_category":      "diagnostic",
                "icon":                 "mdi:thermometer-lines",
                "slug":                 "t_amb",
            },
            {
                "unique_id":        f"thermowatt_{serial}_water_heater_sts",
                "name":             f"{name} Water Heater Status Raw",
                "state_topic":      status_topic,
                "value_template":   "{{ value_json.result.WaterHeaterSts | default(0) | int }}",
                "state_class":      "measurement",
                "entity_category":  "diagnostic",
                "icon":             "mdi:state-machine",
                "slug":             "water_heater_sts",
            },
        ]

        for s in sensors:
            slug = s.pop("slug")
            s["device"] = device
            s.update(avail)
            self.mqtt_client.publish(
                f"homeassistant/sensor/{serial}/{slug}/config",
                json.dumps(s), retain=True
            )

        # ── Diagnostic sensors (poll health + bridge config) ────────────
        # These use bridge-level availability only so they remain readable
        # (and show "degraded") even when per-device polling is failing.
        diag_sensors = [
            {
                "unique_id":           f"thermowatt_{serial}_poll_interval",
                "name":                f"{name} Poll Interval",
                "state_topic":         diag_topic,
                "value_template":      "{{ value_json.poll_interval | int }}",
                "unit_of_measurement": "s",
                "state_class":         "measurement",
                "entity_category":     "diagnostic",
                "icon":                "mdi:timer-sync-outline",
                "slug":                "poll_interval",
            },
            {
                "unique_id":       f"thermowatt_{serial}_consecutive_failures",
                "name":            f"{name} Consecutive Poll Failures",
                "state_topic":     diag_topic,
                "value_template":  "{{ value_json.consecutive_failures | int }}",
                "state_class":     "measurement",
                "entity_category": "diagnostic",
                "icon":            "mdi:cloud-alert-outline",
                "slug":            "consecutive_failures",
            },
            {
                "unique_id":       f"thermowatt_{serial}_last_successful_poll",
                "name":            f"{name} Last Successful Poll",
                "state_topic":     diag_topic,
                "value_template":  (
                    "{{ value_json.last_successful_poll "
                    "if value_json.last_successful_poll is not none else none }}"
                ),
                "device_class":    "timestamp",
                "entity_category": "diagnostic",
                "icon":            "mdi:clock-check-outline",
                "slug":            "last_successful_poll",
            },
            {
                "unique_id":           f"thermowatt_{serial}_element_wattage",
                "name":                f"{name} Element Wattage",
                "state_topic":         diag_topic,
                "value_template":      "{{ value_json.element_wattage | int }}",
                "unit_of_measurement": "W",
                "entity_category":     "diagnostic",
                "icon":                "mdi:heating-coil",
                "slug":                "element_wattage",
            },
            {
                "unique_id":       f"thermowatt_{serial}_poll_status",
                "name":            f"{name} Poll Status",
                "state_topic":     diag_topic,
                "value_template":  "{{ value_json.poll_status }}",
                "entity_category": "diagnostic",
                "icon":            "mdi:cloud-sync-outline",
                "slug":            "poll_status",
            },
            {
                "unique_id":                f"thermowatt_{serial}_rate_limit_status",
                "name":                     f"{name} Rate Limit Status",
                "state_topic":              diag_topic,
                "value_template":           (
                    "{{ {'active': 'limited', 'recovery_unverified': 'recovery_unverified'}"
                    ".get(value_json.rate_limit.state, 'ok') }}"
                ),
                "json_attributes_topic":    diag_topic,
                "json_attributes_template": "{{ value_json.rate_limit | tojson }}",
                "entity_category":          "diagnostic",
                "icon":                     "mdi:speedometer-slow",
                "slug":                     "rate_limit_status",
            },
            {
                "unique_id":                f"thermowatt_{serial}_api_requests",
                "name":                     f"{name} API Requests (UTC day)",
                "state_topic":              diag_topic,
                "value_template":           "{{ value_json.requests.total | int }}",
                "state_class":              "measurement",
                "json_attributes_topic":    diag_topic,
                "json_attributes_template": "{{ value_json.requests | tojson }}",
                "entity_category":          "diagnostic",
                "icon":                     "mdi:counter",
                "slug":                     "api_requests",
            },
            {
                "unique_id":                f"thermowatt_{serial}_command_status",
                "name":                     f"{name} Command Status",
                "state_topic":              diag_topic,
                "value_template":           "{{ value_json.command_status }}",
                "json_attributes_topic":    diag_topic,
                "json_attributes_template": "{{ value_json.commands | tojson }}",
                "entity_category":          "diagnostic",
                "icon":                     "mdi:check-decagram-outline",
                "slug":                     "command_status",
            },
        ]

        for s in diag_sensors:
            slug = s.pop("slug")
            s["device"] = device
            s.update(diag_avail)
            self.mqtt_client.publish(
                f"homeassistant/sensor/{serial}/{slug}/config",
                json.dumps(s), retain=True
            )

    # ------------------------------------------------------------------ #
    #  Status publishing                                                   #
    # ------------------------------------------------------------------ #

    def _compute_status(self, status_data: dict) -> dict:
        """Adds computed fields to the result dict and returns it."""
        result = status_data.get('result', {})
        water_heater_sts    = int(result.get('WaterHeaterSts', 0))
        result['heating']   = (water_heater_sts & 1) != 0
        # Deterministic interpretation of the freshly polled raw Cmd — not a
        # fabricated/synthetic device state. None (unknown/absent Cmd) serialises
        # to JSON null; mode_state_template's `default('None', true)` renders that
        # as the literal string "None", which HA's MQTT water_heater integration
        # treats as an explicit mode reset to unknown (an empty payload is ignored
        # instead and would retain a stale prior mode).
        result['ha_mode']   = self.decode_cmd(result.get('Cmd')).get('mode')
        result['last_polled_at'] = datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%dT%H:%M:%S+00:00')
        return status_data

    @staticmethod
    def _validate_status_payload(status_data):
        """An HTTP 200 alone does not mean the poll returned usable device status.

        The Thermowatt cloud API can return HTTP 200 with a body such as
        {"success": false, "error": "Water heater not found, check the Wi-Fi
        connection"} when it cannot locate the device — no `result` object at
        all. Returns (True, None) for a usable payload, or (False, reason) for
        one that must be treated as a poll failure rather than fresh state.
        """
        if not isinstance(status_data, dict):
            return False, "response body is not a JSON object"
        if status_data.get("success") is False:
            return False, status_data.get("error") or "success=false"
        result = status_data.get("result")
        if not isinstance(result, dict) or not result:
            return False, "response missing usable 'result' object"
        return True, None

    @staticmethod
    def _isoformat_epoch(value):
        """Return an epoch timestamp as UTC ISO8601, or None."""
        if value is None:
            return None
        return datetime.datetime.fromtimestamp(
            value, tz=datetime.timezone.utc
        ).strftime('%Y-%m-%dT%H:%M:%S+00:00')

    @staticmethod
    def _command_value_matches(field, observed, requested):
        """Compare raw readback with the normalised command expectation.

        Off confirmation is family-based (recognised mode family with bit0
        cleared) rather than an exact Cmd match, since the device's disabled
        sub-state raw value depends on which family was active beforehand
        (e.g. 8, 16 or 64) — see decode_cmd(). All other MODE/TEMP commands
        keep exact-value matching.
        """
        if field == "Cmd" and requested == MyThermowattBridge.CMD_OFF_SENTINEL:
            decoded = MyThermowattBridge.decode_cmd(observed)
            return decoded["known"] and not decoded["enabled"]
        try:
            if field == "Cmd":
                return int(observed) == int(requested)
            if field == "T_SetPoint":
                return abs(float(observed) - float(requested)) <= 0.1
        except (TypeError, ValueError):
            return False
        return observed == requested

    def _expire_pending_commands(self, serial: str, now: float):
        """Bound pending commands without adding a retry or timer loop."""
        with self._command_lock:
            for record in self._command_state.get(serial, {}).values():
                if record.get("status") != "pending" or now < record["deadline"]:
                    continue
                record["status"] = "mismatched" if record.get("fresh_poll_seen") else "timed_out"
                record["completed_at"] = now
                print(
                    f"[CMD] {serial}/{record['command_type']} {record['status']} — "
                    f"requested {record['field']}={record['requested']!r}, "
                    f"observed={record.get('observed')!r}"
                )

    def _persist_confirmed_setpoint(self, serial: str, observed):
        """Best-effort cache update; persistence failure must not invalidate readback."""
        try:
            confirmed = float(observed)
            with self._config_lock:
                device = self.config.get("devices", {}).get(serial)
                if device is None:
                    return
                device["last_setpoint"] = confirmed
                self._save_config()
        except Exception as e:
            print(
                f"[WARN] Confirmed T_SetPoint={observed!r} for {serial}, "
                f"but failed to persist last_setpoint cache: {e}"
            )

    def _reconcile_pending_commands(
        self, serial: str, status_data: dict, poll_started_at: float, now: float
    ):
        """Confirm only from a real status poll that started after submission."""
        result = status_data.get("result", {})
        confirmed_setpoint = None
        self._expire_pending_commands(serial, now)
        with self._command_lock:
            for record in self._command_state.get(serial, {}).values():
                if record.get("status") != "pending":
                    continue
                if poll_started_at <= record["submitted_at"]:
                    continue

                observed = result.get(record["field"])
                record["fresh_poll_seen"] = True
                record["polls_seen"] = record.get("polls_seen", 0) + 1
                record["observed"] = observed
                record["checked_at"] = now

                if self._command_value_matches(record["field"], observed, record["requested"]):
                    record["status"] = "confirmed"
                    record["completed_at"] = now
                    # Evidence for deciding whether the immediate post-command poll
                    # is ever useful: seconds from submission to first matching
                    # readback, and which fresh poll (1 = the immediate one) matched.
                    record["confirm_latency_s"] = round(now - record["submitted_at"], 1)
                    record["confirm_poll_number"] = record["polls_seen"]
                    if record["field"] == "T_SetPoint":
                        confirmed_setpoint = observed
                    print(
                        f"[CMD] {serial}/{record['command_type']} confirmed by fresh readback — "
                        f"{record['field']}={observed!r}"
                    )

        if confirmed_setpoint is not None:
            self._persist_confirmed_setpoint(serial, confirmed_setpoint)

    def _command_diagnostics(self, serial: str, now: float):
        """Return one summary state plus per-command-type diagnostic details."""
        self._expire_pending_commands(serial, now)
        with self._command_lock:
            records = self._command_state.get(serial, {})

            if any(r.get("status") == "pending" for r in records.values()):
                summary = "pending"
            elif records:
                latest = max(
                    records.values(),
                    key=lambda r: r.get("completed_at") or r.get("submitted_at", 0),
                )
                summary = latest.get("status", "idle")
            else:
                summary = "idle"

            serialised = {}
            for cmd_type, record in records.items():
                serialised[cmd_type] = {
                    "status": record.get("status"),
                    "field": record.get("field"),
                    "requested": record.get("requested"),
                    "observed": record.get("observed"),
                    "submitted_at": self._isoformat_epoch(record.get("submitted_at")),
                    "deadline": self._isoformat_epoch(record.get("deadline")),
                    "checked_at": self._isoformat_epoch(record.get("checked_at")),
                    "completed_at": self._isoformat_epoch(record.get("completed_at")),
                    "fresh_poll_seen": bool(record.get("fresh_poll_seen")),
                    "confirm_latency_s": record.get("confirm_latency_s"),
                    "confirm_poll_number": record.get("confirm_poll_number"),
                    "error": record.get("error"),
                }
            return summary, serialised

    def _requests_diagnostics(self, now=None) -> dict:
        """Per-UTC-day API request counters (bridge-wide; counts, no payload data)."""
        today = self._utc_day()
        with self._rate_limit_lock:
            counters = dict(self._request_counters)
            rolling = self._rolling_locked(time.time() if now is None else now)
            since_recovery = {"attempted": self._sent_since_recovery,
                              "since": self._isoformat_epoch(self._since_recovery_ts)}
        if counters["day"] != today:
            counters = self._new_counters(today)
        counters["rolling_24h"] = rolling
        counters["since_recovery"] = since_recovery
        return counters

    def _availability_for(self, serial: str, now: float) -> str:
        """Online only with a real successful poll since this process started, no more
        than OFFLINE_AFTER_S (fixed, cadence-independent) old, and fewer than
        DEGRADED_THRESHOLD consecutive failures. `_last_successful_poll` is in-memory,
        so nothing persisted by a previous run (retained STATUS/availability) can count."""
        ts = self._last_successful_poll.get(serial)
        if (ts is None or now - ts > self.OFFLINE_AFTER_S
                or self._consecutive_failures.get(serial, 0) >= self.DEGRADED_THRESHOLD):
            return "offline"
        return "online"

    def _update_availability(self, serial: str, now: float):
        """Publish per-device availability on change (called from every diagnostics publish)."""
        state = self._availability_for(serial, now)
        if self._avail_state.get(serial) == state:
            return
        self._avail_state[serial] = state
        try:
            self.mqtt_client.publish(f"P/{serial}/availability", state, retain=True)
        except Exception as e:
            print(f"[WARN] Failed to publish availability for {serial}: {e}")
        print(f"[POLL] {serial} availability {state} "
              f"({self._consecutive_failures.get(serial, 0)} consecutive failures)")

    def _next_offline_deadline(self):
        """Earliest time an online device would age out, for the existing wait loop."""
        due = [ts + self.OFFLINE_AFTER_S for s, ts in list(self._last_successful_poll.items())
               if self._avail_state.get(s) == "online"]
        return min(due) if due else None

    def _publish_diagnostics(self, serial: str):
        """Publish bridge, poll-health and command-confirmation diagnostics."""
        now = time.time()
        self._update_availability(serial, now)
        ts = self._last_successful_poll.get(serial)
        failures = self._consecutive_failures.get(serial, 0)
        command_status, commands = self._command_diagnostics(serial, now)
        limited = self._rate_limit_snapshot()["step"] > 0  # until a good poll confirms recovery
        if ts is None:
            telemetry_state, age = "unknown", None
        else:
            age = round(max(0.0, now - ts), 1)
            telemetry_state = "stale" if age > self.TELEMETRY_STALE_AFTER else "fresh"
        if limited:
            telemetry_state = "rate_limited"
        degraded = failures >= self.DEGRADED_THRESHOLD
        payload = {
            "poll_interval":        self.current_poll_interval,
            "consecutive_failures": failures,
            "last_successful_poll": self._isoformat_epoch(ts),
            "element_wattage":      self.element_wattage,
            # "rate_limited" is anything-but-"ok", so Package 19's `poll_status == 'ok'`
            # telemetry test stays fail-closed (and is now also visible to operators).
            "poll_status":          "rate_limited" if limited else ("degraded" if degraded else "ok"),
            "telemetry":            {"state": telemetry_state, "age_s": age,
                                     "stale_after_s": self.TELEMETRY_STALE_AFTER},
            "last_poll_error":      self._last_poll_error.get(serial),
            "command_status":       command_status,
            "commands":             commands,
            "rate_limit":           self._rate_limit_diagnostics(now),
            "requests":             self._requests_diagnostics(now),
            "proactive_token_refresh": bool(self.PROACTIVE_REFRESH),
        }
        try:
            self.mqtt_client.publish(f"P/{serial}/diagnostics", json.dumps(payload), retain=True)
        except Exception as e:
            print(f"[WARN] Failed to publish diagnostics for {serial}: {e}")

    def poll_status(self, serial):
        """Poll status for a device — returns (success, status_code)."""
        try:
            poll_started_at = time.time()
            r           = self.request("GET", "/status", serial=serial)
            status_code = r.status_code
            self._note_response(r)  # idempotent; covers a mocked/wrapped request()

            if status_code == 200:
                status_data = r.json()
                is_valid, invalid_reason = self._validate_status_payload(status_data)
                if not is_valid:
                    if self._detect_rate_limit(r) is not None:
                        invalid_reason = "rate limited: Daily request limit exceeded"
                    # HTTP 200 with no usable device status — count it as a poll
                    # failure using the existing failure/threshold machinery, not
                    # as fresh state. Never falls through to STATUS publish,
                    # energy accumulation or command reconciliation below.
                    self._last_poll_error[serial] = invalid_reason
                    failures = self._consecutive_failures.get(serial, 0) + 1
                    self._consecutive_failures[serial] = failures
                    print(f"[WARN] {serial} — invalid device status payload (HTTP 200): {invalid_reason}")
                    self._publish_diagnostics(serial)
                    return (False, None)

                status_data = self._compute_status(status_data)
                now = time.time()
                self._reconcile_pending_commands(serial, status_data, poll_started_at, now)
                # QoS=1 — at-least-once delivery ensures HA receives real polled state.
                self.mqtt_client.publish(f"P/{serial}/STATUS", json.dumps(status_data), qos=1, retain=True)

                # Energy accumulation — integrates element_wattage × elapsed_hours when heating.
                # _last_poll_ts only advances on success, so after a poll outage (429 backoff,
                # cloud/network downtime) the next good poll could attribute the entire gap to
                # heating. Cap at 2× the normal poll interval to bound the jump.
                # Persisted in config.json so the counter survives bridge restarts.
                if serial in self._last_poll_ts:
                    elapsed_s = min(now - self._last_poll_ts[serial], 2 * self.POLL_INTERVAL)
                    elapsed_h = elapsed_s / 3600.0
                    if status_data.get('result', {}).get('heating'):
                        with self._config_lock:
                            bucket    = self.config.setdefault('energy_kwh', {})
                            kwh_delta = (self.element_wattage / 1000.0) * elapsed_h
                            bucket[serial] = round(bucket.get(serial, 0.0) + kwh_delta, 4)
                            self._save_config()
                self._last_poll_ts[serial] = now

                with self._config_lock:
                    kwh = self.config.get('energy_kwh', {}).get(serial, 0.0)
                self.mqtt_client.publish(f"P/{serial}/energy_kwh", kwh, qos=1, retain=True)

                # Recovery — clear degraded state if we were previously failing
                prev_failures = self._consecutive_failures.get(serial, 0)
                self._consecutive_failures[serial] = 0
                self._last_successful_poll[serial]  = now
                self._last_poll_error[serial]       = None
                if prev_failures >= self.DEGRADED_THRESHOLD:
                    print(f"[POLL] {serial} recovered after {prev_failures} consecutive failures")

                self._publish_diagnostics(serial)
                return (True, status_code)

            else:
                self._last_poll_error[serial] = f"HTTP {status_code}"
                failures = self._consecutive_failures.get(serial, 0) + 1
                self._consecutive_failures[serial] = failures
                self._publish_diagnostics(serial)
                return (False, status_code)

        except Exception as e:
            print(f"Poll error for {serial}: {e}")
            self._last_poll_error[serial] = str(e)
            failures = self._consecutive_failures.get(serial, 0) + 1
            self._consecutive_failures[serial] = failures
            self._publish_diagnostics(serial)
            return (False, None)

    # ------------------------------------------------------------------ #
    #  Command handling                                                    #
    # ------------------------------------------------------------------ #

    def _check_cooldown(self, serial: str, cmd_type: str) -> bool:
        """Returns True if enough time has passed since the last command of this type.
        MODE and TEMP track separate cooldown windows so a valid paired sequence
        (e.g. MODE then TEMP immediately after) is not incorrectly blocked.
        """
        last    = self._last_cmd_time.get(serial, {}).get(cmd_type, 0)
        elapsed = time.time() - last
        if elapsed < self.CMD_COOLDOWN:
            remaining = int(self.CMD_COOLDOWN - elapsed)
            print(f"[CMD] Cooldown active for {serial}/{cmd_type} — {remaining}s remaining. Command ignored.")
            return False
        return True

    def _record_command(self, serial: str, cmd_type: str, field=None, requested=None):
        """Record cooldown and, when supplied, a bounded readback expectation."""
        now = time.time()
        self._last_cmd_time.setdefault(serial, {})[cmd_type] = now
        self._confirm_until = max(self._confirm_until, now + self.CONFIRM_WINDOW)
        self.current_poll_interval = self.POLL_INTERVAL_CONFIRM

        if field is not None:
            with self._command_lock:
                self._command_state.setdefault(serial, {})[cmd_type] = {
                    "command_type": cmd_type,
                    "status": "pending",
                    "field": field,
                    "requested": requested,
                    "observed": None,
                    "submitted_at": now,
                    "deadline": now + self.CONFIRM_WINDOW,
                    "checked_at": None,
                    "completed_at": None,
                    "fresh_poll_seen": False,
                    "error": None,
                }
            self._publish_diagnostics(serial)

        # Wake only after the expectation exists, otherwise the poll thread could
        # complete a matching readback before there is a pending record to reconcile.
        self._poll_wakeup.set()

        print(
            f"[CMD] Confirmation window active — polling at "
            f"{self.POLL_INTERVAL_CONFIRM}s for {self.CONFIRM_WINDOW}s"
        )

    def _record_submission_failure(
        self, serial: str, cmd_type: str, field: str, requested, error
    ):
        """Surface a rejected submission without changing confirmed device state."""
        now = time.time()
        with self._command_lock:
            self._command_state.setdefault(serial, {})[cmd_type] = {
                "command_type": cmd_type,
                "status": "submit_failed",
                "field": field,
                "requested": requested,
                "observed": None,
                "submitted_at": now,
                "deadline": now,
                "checked_at": None,
                "completed_at": now,
                "fresh_poll_seen": False,
                "error": str(error),
            }
        self._publish_diagnostics(serial)

    def _submit_command(
        self, serial: str, cmd_type: str, field: str, requested, endpoint: str, **kwargs
    ) -> bool:
        """Submit once, then record either pending confirmation or submit_failed.

        Never queues, and never re-sends after rate-limit recovery: a command refused
        locally (rate-limit window), rejected by the API, or failed in transport is
        surfaced as submit_failed. The single exception is request()'s pre-existing
        one-shot replay of the same POST after a successful HTTP 401 token refresh
        (see request()); it happens inside this one call, never later.
        """
        try:
            resp = self.request("POST", endpoint, serial=serial, **kwargs)
        except RateLimitedError as e:
            self._record_submission_failure(serial, cmd_type, field, requested, f"rate_limited: {e}")
            print(
                f"[ERROR] {serial}/{cmd_type} command NOT sent ({e}) — "
                "not queued; confirmed state unchanged"
            )
            return False
        except Exception as e:
            error = f"{type(e).__name__}: {e}"
            self._record_submission_failure(serial, cmd_type, field, requested, error)
            print(
                f"[ERROR] {serial}/{cmd_type} command transport failure ({error}) — "
                "confirmed state unchanged"
            )
            return False

        if resp is not None and 200 <= resp.status_code < 300:
            rejection = self._command_rejection(resp)
            if rejection is None:
                self._record_command(serial, cmd_type, field, requested)
                return True
            self._record_submission_failure(serial, cmd_type, field, requested, rejection)
            print(
                f"[ERROR] {serial}/{cmd_type} command rejected by API despite HTTP "
                f"{resp.status_code} ({rejection}) — confirmed state unchanged"
            )
            return False

        code = resp.status_code if resp is not None else "no response"
        self._record_submission_failure(serial, cmd_type, field, requested, code)
        print(
            f"[ERROR] {serial}/{cmd_type} command failed ({code}) — "
            "confirmed state unchanged"
        )
        return False

    def _command_rejection(self, resp):
        """Return a short reason when a 2xx command response is an API-level rejection.

        A recognised quota error (with or without a `success` field) is always a
        rejection and starts the backoff. Otherwise only an explicit JSON object
        with `success: false` is a rejection (the API has been observed returning
        HTTP 200 with such a body). Body-less, non-JSON
        or other JSON responses remain successful submissions, since several
        endpoints legitimately reply with an empty body.
        """
        if self._note_response(resp):
            return "rate_limited: Daily request limit exceeded"
        body = self._json_body(resp)
        if isinstance(body, dict) and body.get("success") is False:
            error = body.get("error")
            detail = error if isinstance(error, str) and error.strip() else "success=false"
            return f"API rejected: {detail.strip()[:self.COMMAND_ERROR_MAX_LEN]}"
        return None

    def on_mqtt_message(self, client, userdata, msg):
        """Local HA → REST API command handler."""
        try:
            # Ignore retained commands — a retained CMD topic replayed on (re)connect
            # could execute a stale mode/temperature command.
            if getattr(msg, "retain", False):
                print(f"[CMD] Ignoring retained command on {msg.topic}")
                return

            parts = msg.topic.split('/')
            if len(parts) == 3 and parts[0] == "P" and parts[2] == "refresh":
                # Read-only request: no cloud I/O here, no new mode of control.
                # The normal poll loop alone can perform quota-recovery probes.
                serial = parts[1]
                if self._rate_limit_snapshot()["step"] > 0:
                    return
                with self._config_lock:
                    if serial not in self.config.get("devices", {}):
                        return
                    self._demand_requested.add(serial)
                self._poll_wakeup.set()
                return

            payload = msg.payload.decode()
            if len(parts) < 4:
                return
            sn = parts[1]

            with self._config_lock:
                device_config = dict(self.config.get('devices', {}).get(sn, {}))
            if not device_config:
                print(f"⚠️  Unknown device serial: {sn}")
                return

            # Determine command type from topic suffix
            if f"P/{sn}/CMD/TEMP" in msg.topic:
                cmd_type = "TEMP"
            elif f"P/{sn}/CMD/MODE" in msg.topic:
                cmd_type = "MODE"
            else:
                print(f"[CMD] Unknown CMD topic: {msg.topic}")
                return

            if not self._check_cooldown(sn, cmd_type):
                return

            current_fav = device_config.get("last_setpoint", 60)

            if cmd_type == "TEMP":
                # Clamp to firmware-confirmed safe range before sending.
                # Values above 70 are silently rejected by the device.
                raw_temp = int(float(payload))
                temp     = max(self.TEMP_MIN, min(self.TEMP_MAX, raw_temp))
                if temp != raw_temp:
                    print(f"[CMD] Temperature {raw_temp}°C clamped to {temp}°C (firmware range {self.TEMP_MIN}–{self.TEMP_MAX}°C)")
                print(f"[CMD] Setting Temperature to {temp}°C for {sn}...")

                self._submit_command(
                    sn, cmd_type, "T_SetPoint", temp, "/manual", json={"T_SetPoint": temp}
                )

            elif cmd_type == "MODE":
                print(f"[CMD] Setting Mode to {payload} for {sn}...")

                if payload == "Manual":
                    self._submit_command(
                        sn, cmd_type, "Cmd", 9, "/manual", json={"T_SetPoint": current_fav}
                    )

                elif payload == "Eco":
                    self._submit_command(
                        sn,
                        cmd_type,
                        "Cmd",
                        3,
                        "/eco",
                        headers={"Content-Type": "text/plain"},
                        data="",
                    )

                elif payload == "Auto":
                    self._submit_command(
                        sn,
                        cmd_type,
                        "Cmd",
                        17,
                        "/auto",
                        headers={"Content-Type": "text/plain"},
                        data="",
                    )

                elif payload == "Holiday":
                    future_date = (datetime.datetime.now() + datetime.timedelta(days=30)).strftime("%Y-%m-%d")
                    print(f"[CMD] Setting Holiday Mode until {future_date} for {sn}...")
                    self._submit_command(
                        sn,
                        cmd_type,
                        "Cmd",
                        65,
                        "/holiday",
                        json={"end_date": future_date},
                    )

                elif payload == "Off":
                    print(f"[CMD] Turning Boiler OFF for {sn}...")
                    if self._submit_command(
                        sn,
                        cmd_type,
                        "Cmd",
                        self.CMD_OFF_SENTINEL,
                        "/off",
                        headers={"Content-Type": "text/plain"},
                        data="",
                    ):
                        print(f"[CMD] Off command submitted for {sn}; awaiting fresh readback")

                else:
                    print(f"[CMD] Unknown MODE payload: {payload!r} for {sn} — ignored "
                          f"(valid: Off, Eco, Manual, Auto, Holiday)")

        except Exception as e:
            print(f"MQTT Cmd Error: {e}")

    # ------------------------------------------------------------------ #
    #  Logging                                                             #
    # ------------------------------------------------------------------ #

    def log_status_summary(self):
        elapsed = time.time() - self.last_status_log_time
        if elapsed >= self.STATUS_LOG_INTERVAL:
            print(
                f"[STATUS] Polled {self.poll_count} times, "
                f"{self.success_count} x 200, {self.error_count} errors, "
                f"interval={self.current_poll_interval}s"
            )
            self.poll_count           = 0
            self.success_count        = 0
            self.error_count          = 0
            self.last_status_log_time = time.time()

    # ------------------------------------------------------------------ #
    #  Boot + main loop                                                    #
    # ------------------------------------------------------------------ #

    def _run_poll_cycle(self):
        """One poll pass over all devices (no waiting). Rate limit takes priority."""
        # Clear at the start of the cycle. A command arriving during polling
        # or the following wait remains set and wakes the next eligible poll.
        self._poll_wakeup.clear()

        if self._rate_limit_remaining() <= 0 and self.current_poll_interval > self.POLL_INTERVAL:
            # Backoff window ended: the stretched interval was only for the window.
            self.current_poll_interval = self.POLL_INTERVAL

        # Exit fast-poll window once confirm period has elapsed
        if self.current_poll_interval == self.POLL_INTERVAL_CONFIRM:
            if time.time() >= self._confirm_until:
                self.current_poll_interval = self.POLL_INTERVAL
                print(f"[POLL] Confirmation window closed — resuming normal polling ({self.POLL_INTERVAL}s)")

        self._maybe_proactive_refresh()

        with self._config_lock:
            device_serials = list(self.config.get('devices', {}).keys())
            requested = set(self._demand_requested)
            self._demand_requested.clear()

        for serial in device_serials:
            if self._rate_limit_remaining() > 0:
                break  # rate limit has priority over normal polling
            now = time.time()
            last = self._last_status_attempt.get(serial, 0)
            if now >= self._confirm_until:  # confirmation cadence is never throttled
                if serial in requested:
                    # Storm guard on *attempts* of any outcome; the log says whether
                    # the recent attempt actually produced fresh telemetry.
                    if now - last < self.DEMAND_MIN_SPACING:
                        fresh = now - self._last_successful_poll.get(serial, 0) < self.DEMAND_MIN_SPACING
                        print(f"[POLL] Refresh for {serial} coalesced: recent attempt "
                              f"{'gave' if fresh else 'gave no'} fresh telemetry")
                        continue
                elif requested and now - last < self.current_poll_interval - 5:
                    continue  # demand wake for another device; this one is not due
            # One bounded retry, only for a requested refresh whose request ended
            # without any HTTP response (_transport_guard). Answered responses (HTTP
            # errors, invalid 200 payloads), local processing errors and quota are never retried.
            for attempt in range(2 if serial in requested else 1):
                if attempt:
                    time.sleep(self.DEMAND_RETRY_DELAY)
                    if self._rate_limit_remaining() > 0:
                        break
                self._last_status_attempt[serial] = time.time()
                self._io.transport_failed = False
                self.poll_count += 1
                generation = self._rate_limit_gen()  # before the request is sent
                success, status_code = self.poll_status(serial)
                if (success or not self._io.transport_failed or status_code is not None
                        or self._rate_limit_remaining() > 0):
                    break  # answered (even invalid), local failure or quota: never retried
                self.error_count += 1

            if success:
                self.success_count += 1
                if self._clear_rate_limit(generation):
                    self._publish_diagnostics(serial)  # recovery visible immediately
                    self.current_poll_interval = (
                        self.POLL_INTERVAL_CONFIRM
                        if time.time() < self._confirm_until
                        else self.POLL_INTERVAL
                    )
            else:
                self.error_count += 1
                if self._rate_limit_remaining() > 0:
                    # Quota errors never trigger re-authentication; the
                    # backoff window (already registered) gates all I/O.
                    print(f"[RATE LIMIT] {serial} rate limited, next request in "
                          f"{int(self._rate_limit_remaining())}s")
                    break
                elif status_code is not None:
                    print(f"[ERROR] Got status {status_code} for {serial}, re-logging in...")
                    try:
                        self.login()
                    except Exception as e:
                        print(f"[ERROR] Re-login failed: {e}")

        self.log_status_summary()
        self._maybe_flush_counters()

    def _boot_authenticate_and_discover(self):
        """Steps 3-4: login + device discovery, rate-limit aware.

        A rate limit (persisted from a previous run, or hit right now by login or
        /user-info) is waited out in-process and the step is retried once the
        window ends, instead of exiting and letting the supervisor restart-loop
        the add-on. Any other failure keeps the original fail-fast exit(1).
        Waiting sends no requests; backoff windows keep doubling if the API is
        still limiting, so total request rate stays bounded.
        """
        while True:
            self._wait_out_persisted_rate_limit()

            try:
                self.login()
                print("OK: Step 3 - Logged in to Thermowatt backend.")
            except RateLimitedError as e:
                print(f"[RATE LIMIT] Step 3 deferred: {e}")
                continue
            except Exception as e:  # noqa: BLE001
                print(f"FAILED: Step 3 - Backend authentication failed: {e}")
                sys.exit(1)

            try:
                r = self.request("GET", "/user-info")
                if self._note_response(r):
                    raise RateLimitedError("device discovery rejected: rate limited")
                devices = r.json().get('result', {}).get('termostati', [])
                if not devices:
                    raise Exception("Zero devices returned")  # noqa: TRY002
                print(f"OK: Step 4 - Found {len(devices)} thermostats.")
                return devices
            except RateLimitedError as e:
                print(f"[RATE LIMIT] Step 4 deferred: {e}")
                continue
            except Exception as e:  # noqa: BLE001
                print(f"FAILED: Step 4 - Could not retrieve thermostat list: {e}")
                sys.exit(1)

    def run(self):
        print("--- BOOT SEQUENCE START ---")

        if not EMAIL or not PASSWORD:
            print("FAILED: Step 1 - Missing EMAIL/PASSWORD in addon config.")
            sys.exit(1)
        print("OK: Step 1 - Credentials present.")

        try:
            # Register callbacks BEFORE connect so on_connect fires on initial connection.
            self.mqtt_client.on_connect    = self.on_connect
            self.mqtt_client.on_disconnect = self.on_disconnect
            self.mqtt_client.connect(MQTT_HOST, MQTT_PORT, 60)
            print("OK: Step 2 - MQTT TCP connection initiated.")
        except Exception as e:
            print(f"FAILED: Step 2 - MQTT Connection Error: {e}")
            sys.exit(1)

        try:
            devices = self._boot_authenticate_and_discover()
        except KeyboardInterrupt:
            print("Stopping during startup...")
            sys.exit(0)

        for dev in devices:
            serial = dev['seriale']
            name   = dev.get('nome', 'Boiler')

            with self._config_lock:
                if serial not in self.config['devices']:
                    self.config['devices'][serial] = {"name": name, "last_setpoint": 60}
                else:
                    self.config['devices'][serial]["name"] = name
                self._save_config()

            # Fail closed across restarts: the device stays unavailable (overriding any
            # retained "online") until the first validated post-start status poll.
            self._avail_state[serial] = "offline"
            self.mqtt_client.publish(f"P/{serial}/availability", "offline", retain=True)
            self.publish_discovery(serial, name)
            print(f"🌉 Bridge active for: {name} ({serial})")

        print(f"OK: Step 5 - Device discovery published. Element wattage: {self.element_wattage} W")

        self.mqtt_client.on_message = self.on_mqtt_message
        self.mqtt_client.loop_start()

        print(f"OK: Step 6 - Polling loop starting (background={self.POLL_INTERVAL}s, confirm={self.POLL_INTERVAL_CONFIRM}s, degraded_threshold={self.DEGRADED_THRESHOLD}).")

        while True:
            try:
                self._run_poll_cycle()
                self._wait_for_next_poll(self.current_poll_interval)

            except KeyboardInterrupt:
                print("Stopping...")
                break
            except Exception as e:
                print(f"[ERROR] Polling loop error: {e}")
                if self._rate_limit_remaining() <= 0:
                    try:
                        self.login()
                    except Exception as e2:
                        print(f"[ERROR] Re-login failed: {e2}")
                self._wait_for_next_poll(self.current_poll_interval)

        self._maybe_flush_counters(force=True)

        # Explicitly publish offline on clean shutdown.
        # LWT fires on unclean disconnect only; this covers clean add-on stop/restart.
        self.mqtt_client.publish(AVAILABILITY_TOPIC, "offline", retain=True)
        time.sleep(0.2)  # allow publish to flush before disconnect
        self.mqtt_client.disconnect()


def _sigterm_handler(signum, frame):
    """SIGTERM handler — HA add-on supervisor sends SIGTERM on stop."""
    raise KeyboardInterrupt


if __name__ == "__main__":
    signal.signal(signal.SIGTERM, _sigterm_handler)
    bridge = MyThermowattBridge()
    bridge.run()
