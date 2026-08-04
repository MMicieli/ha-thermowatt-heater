import sys, json, time, uuid, os, signal, threading, requests, urllib3, datetime
import paho.mqtt.client as mqtt
from paho.mqtt.enums import CallbackAPIVersion

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


class MyThermowattBridge:
    API_KEY  = "YVjArWssxKH631jv1dnnWOTr6gijsSAGz7rQJ4hJoUNRffxYvbQaMbePBEZalena"
    BASE_URL = "https://myapp-connectivity.com/api/v1"

    # Normal polling 60s — reduces cloud rate-limit risk and produces cleaner
    # InfluxDB calibration data. Post-command confirmation window uses 20s.
    POLL_INTERVAL         = 60   # seconds — normal operation
    POLL_INTERVAL_CONFIRM = 20   # seconds — post-command confirmation window
    CONFIRM_WINDOW        = 60   # seconds — how long to stay in fast-poll after a command
    STATUS_LOG_INTERVAL   = 300  # seconds — 5-minute summary log

    # Publish per-device "offline" availability after this many consecutive poll failures.
    # At the 60s normal interval this is ~5 min of cloud silence before marking degraded.
    DEGRADED_THRESHOLD = 5

    # Firmware-confirmed temperature ceiling from T_set_max attribute.
    # Hardware mechanical cutout at ~90°C is independent.
    TEMP_MIN = 20
    TEMP_MAX = 70  # matches confirmed T_set_max: 70 from device attributes

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

        self.mqtt_client = mqtt.Client(CallbackAPIVersion.VERSION2)
        self.mqtt_client.will_set(AVAILABILITY_TOPIC, "offline", retain=True)
        if MQTT_USER:
            self.mqtt_client.username_pw_set(MQTT_USER, MQTT_PASS)

        # Polling state
        self.poll_count            = 0
        self.success_count         = 0
        self.error_count           = 0
        self.last_status_log_time  = time.time()
        self.rate_limit_backoff    = 0  # 0=none, 1+=backoff steps
        self.current_poll_interval = self.POLL_INTERVAL

        # Post-command fast-poll window tracking. The event wakes the poll loop so
        # a command submitted just after a normal poll does not wait up to 60 seconds
        # before its first eligible confirmation poll.
        self._confirm_until: float = 0.0  # epoch time until which fast-poll is active
        self._poll_wakeup = threading.Event()

        # Per-device command cooldown and confirmation state, keyed by command type.
        # MODE and TEMP remain independent so a valid paired sequence is supported.
        self._command_lock = threading.RLock()
        self._last_cmd_time: dict = {}  # {serial: {"MODE": epoch, "TEMP": epoch}}
        self._command_state: dict = {}  # {serial: {"MODE"/"TEMP": confirmation record}}

        # Per-device last successful poll timestamp — for energy accumulation
        self._last_poll_ts: dict = {}  # {serial: epoch float}

        # Per-device degraded state tracking
        self._consecutive_failures: dict = {}  # {serial: int}
        self._last_successful_poll: dict = {}  # {serial: epoch float}

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
            self._reset_headers()
            self.session.headers["x-api-key"] = self.API_KEY
            payload = {"username": EMAIL, "password": PASSWORD, "device_id": self.config["device_uuid"]}
            r = self.session.post(f"{self.BASE_URL}/login", json=payload, verify=TLS_VERIFY, timeout=HTTP_TIMEOUT)
            r.raise_for_status()
            res = r.json()['result']
            self._update_auth(res['accessToken'], res['refreshToken'])

    def refresh_session(self):
        with self._http_lock:
            self._reset_headers()
            self.session.headers["x-api-key"] = self.API_KEY
            payload = {"username": EMAIL, "refreshToken": self.config["refresh_token"]}
            r = self.session.post(f"{self.BASE_URL}/refresh", json=payload, verify=TLS_VERIFY, timeout=HTTP_TIMEOUT)
            if r.status_code == 200:
                res = r.json()['result']
                self._update_auth(res['accessToken'], res['refreshToken'])
                return True
            return False

    def request(self, method, endpoint, serial=None, **kwargs):
        with self._http_lock:
            self._reset_headers()
            url = f"{self.BASE_URL}{endpoint}"
            if serial:
                self.session.headers["seriale"] = serial
            kwargs.setdefault("timeout", HTTP_TIMEOUT)
            resp = self.session.request(method, url, verify=TLS_VERIFY, **kwargs)
            if resp.status_code == 401:
                if self.refresh_session():
                    self._reset_headers()
                    if serial:
                        self.session.headers["seriale"] = serial
                    resp = self.session.request(method, url, verify=TLS_VERIFY, **kwargs)
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
                # Restore per-device availability consistent with current failure state
                failures  = self._consecutive_failures.get(serial, 0)
                dev_avail = "offline" if failures >= self.DEGRADED_THRESHOLD else "online"
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
            # Unknown/absent Cmd values return empty string so HA treats the mode as
            # unknown rather than falsely showing "Off" for an unrecognised state.
            "mode_state_template": (
                "{% set cmd = value_json.result.Cmd | default(none) %}"
                "{% if cmd is none %}"
                "{% elif cmd | int(-1) == 9 %}Manual"
                "{% elif cmd | int(-1) == 3 %}Eco"
                "{% elif cmd | int(-1) == 17 %}Auto"
                "{% elif cmd | int(-1) == 65 %}Holiday"
                "{% elif cmd | int(-1) == 16 %}Off"
                "{% else %}{% endif %}"
            ),
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
        result['last_polled_at'] = datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%dT%H:%M:%S+00:00')
        return status_data

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
        """Compare raw readback with the normalised command expectation."""
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

    def _reconcile_pending_commands(
        self, serial: str, status_data: dict, poll_started_at: float, now: float
    ):
        """Confirm only from a real status poll that started after submission."""
        result = status_data.get("result", {})
        self._expire_pending_commands(serial, now)
        with self._command_lock:
            for record in self._command_state.get(serial, {}).values():
                if record.get("status") != "pending":
                    continue
                if poll_started_at <= record["submitted_at"]:
                    continue

                observed = result.get(record["field"])
                record["fresh_poll_seen"] = True
                record["observed"] = observed
                record["checked_at"] = now

                if self._command_value_matches(record["field"], observed, record["requested"]):
                    record["status"] = "confirmed"
                    record["completed_at"] = now
                    if record["field"] == "T_SetPoint":
                        with self._config_lock:
                            device = self.config.get("devices", {}).get(serial)
                            if device is not None:
                                device["last_setpoint"] = float(observed)
                                self._save_config()
                    print(
                        f"[CMD] {serial}/{record['command_type']} confirmed by fresh readback — "
                        f"{record['field']}={observed!r}"
                    )

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
                    "error": record.get("error"),
                }
            return summary, serialised

    def _publish_diagnostics(self, serial: str):
        """Publish bridge, poll-health and command-confirmation diagnostics."""
        now = time.time()
        ts = self._last_successful_poll.get(serial)
        failures = self._consecutive_failures.get(serial, 0)
        command_status, commands = self._command_diagnostics(serial, now)
        payload = {
            "poll_interval":        self.current_poll_interval,
            "consecutive_failures": failures,
            "last_successful_poll": self._isoformat_epoch(ts),
            "element_wattage":      self.element_wattage,
            "poll_status":          "degraded" if failures >= self.DEGRADED_THRESHOLD else "ok",
            "command_status":       command_status,
            "commands":             commands,
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

            if status_code == 200:
                status_data = r.json()
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
                if prev_failures >= self.DEGRADED_THRESHOLD:
                    self.mqtt_client.publish(f"P/{serial}/availability", "online", retain=True)
                    print(f"[POLL] {serial} recovered after {prev_failures} consecutive failures — device availability online")

                self._publish_diagnostics(serial)
                return (True, status_code)

            else:
                failures = self._consecutive_failures.get(serial, 0) + 1
                self._consecutive_failures[serial] = failures
                if failures == self.DEGRADED_THRESHOLD:
                    self.mqtt_client.publish(f"P/{serial}/availability", "offline", retain=True)
                    print(f"[WARN] {serial} — {failures} consecutive poll failures. Device availability set offline.")
                self._publish_diagnostics(serial)
                return (False, status_code)

        except Exception as e:
            print(f"Poll error for {serial}: {e}")
            failures = self._consecutive_failures.get(serial, 0) + 1
            self._consecutive_failures[serial] = failures
            if failures == self.DEGRADED_THRESHOLD:
                try:
                    self.mqtt_client.publish(f"P/{serial}/availability", "offline", retain=True)
                except Exception:
                    pass
                print(f"[WARN] {serial} — {failures} consecutive poll failures (exception). Device availability set offline.")
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
        self._poll_wakeup.set()

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

    def on_mqtt_message(self, client, userdata, msg):
        """Local HA → REST API command handler."""
        try:
            # Ignore retained commands — a retained CMD topic replayed on (re)connect
            # could execute a stale mode/temperature command.
            if getattr(msg, "retain", False):
                print(f"[CMD] Ignoring retained command on {msg.topic}")
                return

            payload = msg.payload.decode()
            parts   = msg.topic.split('/')
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

                resp = self.request("POST", "/manual", serial=sn, json={"T_SetPoint": temp})
                if resp is not None and 200 <= resp.status_code < 300:
                    self._record_command(sn, cmd_type, "T_SetPoint", temp)
                else:
                    code = resp.status_code if resp is not None else "no response"
                    self._record_submission_failure(sn, cmd_type, "T_SetPoint", temp, code)
                    print(f"[ERROR] Temperature command failed ({code}) — confirmed state unchanged")

            elif cmd_type == "MODE":
                print(f"[CMD] Setting Mode to {payload} for {sn}...")

                if payload == "Manual":
                    resp = self.request("POST", "/manual", serial=sn, json={"T_SetPoint": current_fav})
                    if resp is not None and 200 <= resp.status_code < 300:
                        self._record_command(sn, cmd_type, "Cmd", 9)
                    else:
                        code = resp.status_code if resp is not None else "no response"
                        self._record_submission_failure(sn, cmd_type, "Cmd", 9, code)
                        print(f"[ERROR] Manual command failed ({code})")

                elif payload == "Eco":
                    resp = self.request("POST", "/eco", serial=sn, headers={"Content-Type": "text/plain"}, data="")
                    if resp is not None and 200 <= resp.status_code < 300:
                        self._record_command(sn, cmd_type, "Cmd", 3)
                    else:
                        code = resp.status_code if resp is not None else "no response"
                        self._record_submission_failure(sn, cmd_type, "Cmd", 3, code)
                        print(f"[ERROR] Eco command failed ({code})")

                elif payload == "Auto":
                    resp = self.request("POST", "/auto", serial=sn, headers={"Content-Type": "text/plain"}, data="")
                    if resp is not None and 200 <= resp.status_code < 300:
                        self._record_command(sn, cmd_type, "Cmd", 17)
                    else:
                        code = resp.status_code if resp is not None else "no response"
                        self._record_submission_failure(sn, cmd_type, "Cmd", 17, code)
                        print(f"[ERROR] Auto command failed ({code})")

                elif payload == "Holiday":
                    future_date = (datetime.datetime.now() + datetime.timedelta(days=30)).strftime("%Y-%m-%d")
                    print(f"[CMD] Setting Holiday Mode until {future_date} for {sn}...")
                    resp = self.request("POST", "/holiday", serial=sn, json={"end_date": future_date})
                    if resp is not None and 200 <= resp.status_code < 300:
                        self._record_command(sn, cmd_type, "Cmd", 65)
                    else:
                        code = resp.status_code if resp is not None else "no response"
                        self._record_submission_failure(sn, cmd_type, "Cmd", 65, code)
                        print(f"[ERROR] Holiday command failed ({code})")

                elif payload == "Off":
                    print(f"[CMD] Turning Boiler OFF for {sn}...")
                    resp = self.request("POST", "/off", serial=sn, headers={"Content-Type": "text/plain"}, data="")
                    if resp is not None and 200 <= resp.status_code < 300:
                        self._record_command(sn, cmd_type, "Cmd", 16)
                        print(f"[CMD] Off command submitted for {sn}; awaiting fresh readback")
                    else:
                        code = resp.status_code if resp is not None else "no response"
                        self._record_submission_failure(sn, cmd_type, "Cmd", 16, code)
                        print(f"[ERROR] Off command failed ({code})")

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
            self.login()
            print("OK: Step 3 - Logged in to Thermowatt backend.")
        except Exception as e:
            print(f"FAILED: Step 3 - Backend authentication failed: {e}")
            sys.exit(1)

        try:
            r       = self.request("GET", "/user-info")
            devices = r.json().get('result', {}).get('termostati', [])
            if not devices:
                raise Exception("Zero devices returned")

            print(f"OK: Step 4 - Found {len(devices)} thermostats.")

            for dev in devices:
                serial = dev['seriale']
                name   = dev.get('nome', 'Boiler')

                with self._config_lock:
                    if serial not in self.config['devices']:
                        self.config['devices'][serial] = {"name": name, "last_setpoint": 60}
                    else:
                        self.config['devices'][serial]["name"] = name
                    self._save_config()

                # Publish per-device availability before discovery so sensors are not
                # immediately unavailable between config publish and first poll.
                self.mqtt_client.publish(f"P/{serial}/availability", "online", retain=True)
                self.publish_discovery(serial, name)
                print(f"🌉 Bridge active for: {name} ({serial})")

        except Exception as e:
            print(f"FAILED: Step 4 - Could not retrieve thermostat list: {e}")
            sys.exit(1)

        print(f"OK: Step 5 - Device discovery published. Element wattage: {self.element_wattage} W")

        self.mqtt_client.on_message = self.on_mqtt_message
        self.mqtt_client.loop_start()

        print(f"OK: Step 6 - Polling loop starting (normal={self.POLL_INTERVAL}s, confirm={self.POLL_INTERVAL_CONFIRM}s, degraded_threshold={self.DEGRADED_THRESHOLD}).")

        while True:
            try:
                # Clear at the start of the cycle. A command arriving during polling
                # or the following wait remains set and wakes the next eligible poll.
                self._poll_wakeup.clear()

                # Exit fast-poll window once confirm period has elapsed
                if self.current_poll_interval == self.POLL_INTERVAL_CONFIRM:
                    if time.time() >= self._confirm_until:
                        self.current_poll_interval = self.POLL_INTERVAL
                        print(f"[POLL] Confirmation window closed — resuming normal polling ({self.POLL_INTERVAL}s)")

                with self._config_lock:
                    device_serials = list(self.config.get('devices', {}).keys())

                for serial in device_serials:
                    self.poll_count += 1
                    success, status_code = self.poll_status(serial)

                    if success:
                        self.success_count += 1
                        if self.rate_limit_backoff > 0:
                            self.rate_limit_backoff = 0
                            self.current_poll_interval = (
                                self.POLL_INTERVAL_CONFIRM
                                if time.time() < self._confirm_until
                                else self.POLL_INTERVAL
                            )
                    else:
                        self.error_count += 1
                        if status_code == 429:
                            self.rate_limit_backoff += 1
                            backoff_interval = min(60 * (self.rate_limit_backoff + 1), 180)
                            self.current_poll_interval = backoff_interval
                            print(f"[RATE LIMIT] 429 from {serial}, backing off to {self.current_poll_interval}s")
                            # continue — don't skip remaining devices; each has its own serial.
                            # The backoff sleep below applies to the whole next iteration.
                            continue
                        elif status_code is not None:
                            print(f"[ERROR] Got status {status_code} for {serial}, re-logging in...")
                            try:
                                self.login()
                            except Exception as e:
                                print(f"[ERROR] Re-login failed: {e}")

                self.log_status_summary()
                self._poll_wakeup.wait(timeout=self.current_poll_interval)

            except KeyboardInterrupt:
                print("Stopping...")
                break
            except Exception as e:
                print(f"[ERROR] Polling loop error: {e}")
                try:
                    self.login()
                except Exception as e2:
                    print(f"[ERROR] Re-login failed: {e2}")
                self._poll_wakeup.wait(timeout=self.current_poll_interval)

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
