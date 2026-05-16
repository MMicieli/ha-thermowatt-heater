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


class MyThermowattBridge:
    API_KEY  = "YVjArWssxKH631jv1dnnWOTr6gijsSAGz7rQJ4hJoUNRffxYvbQaMbePBEZalena"
    BASE_URL = "https://myapp-connectivity.com/api/v1"

    # Normal polling 60s — reduces cloud rate-limit risk and produces cleaner
    # InfluxDB calibration data. Post-command confirmation window uses 20s.
    POLL_INTERVAL         = 60   # seconds — normal operation
    POLL_INTERVAL_CONFIRM = 20   # seconds — post-command confirmation window
    CONFIRM_WINDOW        = 60   # seconds — how long to stay in fast-poll after a command
    STATUS_LOG_INTERVAL   = 300  # seconds — 5-minute summary log

    # Firmware-confirmed temperature ceiling from T_set_max attribute.
    # Hardware mechanical cutout at ~90°C is independent.
    TEMP_MIN = 20
    TEMP_MAX = 70  # matches confirmed T_set_max: 70 from device attributes

    # CMD_COOLDOWN 15s — sufficient anti-spam for a resistive element.
    # Strategic dwell time (20–30 min between EMS decisions) is enforced at HA/EMHASS layer.
    CMD_COOLDOWN = 15  # seconds

    def __init__(self):
        self.config = self._load_config()
        self._config_lock = threading.Lock()

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

        # Post-command fast-poll window tracking
        self._confirm_until: float = 0.0  # epoch time until which fast-poll is active

        # Per-device status cache — used by _inject_fake_status
        self._last_status: dict = {}  # {serial: {result: {...}}}

        # Per-device command cooldown tracking
        self._last_cmd_time: dict = {}  # {serial: epoch float}

        # Per-device last successful poll timestamp — for energy accumulation
        self._last_poll_ts: dict = {}  # {serial: epoch float}

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
        self.config.update({"access_token": access, "refresh_token": refresh})
        self.session.headers["Authorization"] = f"Bearer {access}"
        self._save_config()

    def login(self):
        self._reset_headers()
        self.session.headers["x-api-key"] = self.API_KEY
        payload = {"username": EMAIL, "password": PASSWORD, "device_id": self.config["device_uuid"]}
        r = self.session.post(f"{self.BASE_URL}/login", json=payload, verify=TLS_VERIFY, timeout=HTTP_TIMEOUT)
        r.raise_for_status()
        res = r.json()['result']
        self._update_auth(res['accessToken'], res['refreshToken'])

    def refresh_session(self):
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

    def _availability_block(self):
        return {
            "availability_topic":    AVAILABILITY_TOPIC,
            "payload_available":     "online",
            "payload_not_available": "offline",
        }

    def publish_discovery(self, serial, name):
        device       = self._device_block(serial, name)
        avail        = self._availability_block()
        status_topic = f"P/{serial}/STATUS"

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
            "mode_state_template": (
                "{% set cmd = value_json.result.Cmd | default(0) | int %}"
                "{% if cmd == 9 %}Manual"
                "{% elif cmd == 3 %}Eco"
                "{% elif cmd == 17 %}Auto"
                "{% elif cmd == 65 %}Holiday"
                "{% elif cmd == 16 %}Off"
                "{% else %}Off{% endif %}"
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
        # 3000 W when heating, 0 W otherwise. Published on STATUS topic.
        # HA entity: sensor.{device_slug}_{name_slug}_power
        # (e.g. sensor.hws_hws_power for device "HWS", name "HWS Power")
        power_payload = {
            "unique_id":            f"thermowatt_{serial}_power_w",
            "name":                 f"{name} Power",
            "state_topic":          status_topic,
            "value_template":       "{{ 3000 if value_json.result.heating else 0 }}",
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
        # Published on a dedicated topic; value is a plain float string.
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
        # Time_eco / Time_prog: state_class measurement until semantics confirmed.
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

    def poll_status(self, serial):
        """Poll status for a device — returns (success, status_code)."""
        try:
            r           = self.request("GET", "/status", serial=serial)
            status_code = r.status_code

            if status_code == 200:
                status_data = r.json()
                status_data = self._compute_status(status_data)
                self._last_status[serial] = status_data
                # QoS=1 — at-least-once delivery ensures HA always receives status updates.
                self.mqtt_client.publish(f"P/{serial}/STATUS", json.dumps(status_data), qos=1, retain=True)

                # Energy accumulation — integrates 3 kW × elapsed_hours when heating.
                # Persisted in config.json so the counter survives bridge restarts.
                now = time.time()
                if serial in self._last_poll_ts:
                    elapsed_h = (now - self._last_poll_ts[serial]) / 3600.0
                    if status_data.get('result', {}).get('heating'):
                        with self._config_lock:
                            bucket = self.config.setdefault('energy_kwh', {})
                            bucket[serial] = round(bucket.get(serial, 0.0) + 3.0 * elapsed_h, 4)
                            self._save_config()
                self._last_poll_ts[serial] = now

                with self._config_lock:
                    kwh = self.config.get('energy_kwh', {}).get(serial, 0.0)
                self.mqtt_client.publish(f"P/{serial}/energy_kwh", kwh, qos=1, retain=True)

                return (True, status_code)
            else:
                return (False, status_code)
        except Exception as e:
            print(f"Poll error for {serial}: {e}")
            return (False, None)

    # ------------------------------------------------------------------ #
    #  Command handling                                                    #
    # ------------------------------------------------------------------ #

    def _check_cooldown(self, serial) -> bool:
        """Returns True if enough time has passed since the last command.
        Prevents rapid element cycling which stresses the heating element.
        """
        last    = self._last_cmd_time.get(serial, 0)
        elapsed = time.time() - last
        if elapsed < self.CMD_COOLDOWN:
            remaining = int(self.CMD_COOLDOWN - elapsed)
            print(f"[CMD] Cooldown active for {serial} — {remaining}s remaining. Command ignored.")
            return False
        return True

    def _record_command(self, serial):
        """Record command timestamp and enter fast-poll confirmation window."""
        self._last_cmd_time[serial] = time.time()
        self._confirm_until         = time.time() + self.CONFIRM_WINDOW
        self.current_poll_interval  = self.POLL_INTERVAL_CONFIRM
        print(f"[CMD] Confirmation window active — polling at {self.POLL_INTERVAL_CONFIRM}s for {self.CONFIRM_WINDOW}s")

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
            if len(parts) < 2:
                return
            sn = parts[1]

            with self._config_lock:
                device_config = dict(self.config.get('devices', {}).get(sn, {}))
            if not device_config:
                print(f"⚠️  Unknown device serial: {sn}")
                return

            if not self._check_cooldown(sn):
                return

            current_fav = device_config.get("last_setpoint", 60)

            if f"P/{sn}/CMD/TEMP" in msg.topic:
                # Clamp to firmware-confirmed safe range before sending.
                # Values above 70 are silently rejected by the device.
                raw_temp = int(float(payload))
                temp     = max(self.TEMP_MIN, min(self.TEMP_MAX, raw_temp))
                if temp != raw_temp:
                    print(f"[CMD] Temperature {raw_temp}°C clamped to {temp}°C (firmware range {self.TEMP_MIN}–{self.TEMP_MAX}°C)")
                print(f"[CMD] Setting Temperature to {temp}°C for {sn}...")

                resp = self.request("POST", "/manual", serial=sn, json={"T_SetPoint": temp})
                if resp is not None and 200 <= resp.status_code < 300:
                    with self._config_lock:
                        self.config['devices'][sn]["last_setpoint"] = temp
                        self._save_config()
                    self._inject_fake_status(sn, {"T_SetPoint": str(temp)})
                    self._record_command(sn)
                else:
                    code = resp.status_code if resp else "no response"
                    print(f"[ERROR] Temperature command failed ({code}) — HA state not updated")

            elif f"P/{sn}/CMD/MODE" in msg.topic:
                print(f"[CMD] Setting Mode to {payload} for {sn}...")

                if payload == "Manual":
                    resp = self.request("POST", "/manual", serial=sn, json={"T_SetPoint": current_fav})
                    if resp is not None and 200 <= resp.status_code < 300:
                        self._inject_fake_status(sn, {"Cmd": "9", "T_SetPoint": str(current_fav)})
                        self._record_command(sn)
                    else:
                        print(f"[ERROR] Manual command failed ({resp.status_code if resp else 'no response'})")

                elif payload == "Eco":
                    resp = self.request("POST", "/eco", serial=sn, headers={"Content-Type": "text/plain"}, data="")
                    if resp is not None and 200 <= resp.status_code < 300:
                        self._inject_fake_status(sn, {"Cmd": "3"})
                        self._record_command(sn)
                    else:
                        print(f"[ERROR] Eco command failed ({resp.status_code if resp else 'no response'})")

                elif payload == "Auto":
                    resp = self.request("POST", "/auto", serial=sn, headers={"Content-Type": "text/plain"}, data="")
                    if resp is not None and 200 <= resp.status_code < 300:
                        self._inject_fake_status(sn, {"Cmd": "17"})
                        self._record_command(sn)
                    else:
                        print(f"[ERROR] Auto command failed ({resp.status_code if resp else 'no response'})")

                elif payload == "Holiday":
                    future_date = (datetime.datetime.now() + datetime.timedelta(days=30)).strftime("%Y-%m-%d")
                    print(f"[CMD] Setting Holiday Mode until {future_date} for {sn}...")
                    resp = self.request("POST", "/holiday", serial=sn, json={"end_date": future_date})
                    if resp is not None and 200 <= resp.status_code < 300:
                        self._inject_fake_status(sn, {"Cmd": "65"})
                        self._record_command(sn)
                    else:
                        print(f"[ERROR] Holiday command failed ({resp.status_code if resp else 'no response'})")

                elif payload == "Off":
                    print(f"[CMD] Turning Boiler OFF for {sn}...")
                    resp = self.request("POST", "/off", serial=sn, headers={"Content-Type": "text/plain"}, data="")
                    if resp is not None and 200 <= resp.status_code < 300:
                        self._inject_fake_status(sn, {"Cmd": "16"})
                        self._record_command(sn)
                        print(f"[SUCCESS] Boiler {sn} is now OFF")
                    else:
                        print(f"[ERROR] Off command failed ({resp.status_code if resp else 'no response'})")

        except Exception as e:
            print(f"MQTT Cmd Error: {e}")

    def _inject_fake_status(self, serial, overrides):
        """Immediately updates HA state to prevent flipping while cloud syncs.
        Only called after a confirmed 2xx API response.
        """
        try:
            status = json.loads(json.dumps(self._last_status.get(serial, {"result": {}})))
            result = status.get('result', {})

            for k, v in overrides.items():
                result[k] = str(v)

            # Recompute heating flag from overridden WaterHeaterSts if present
            water_heater_sts  = int(result.get('WaterHeaterSts', 0))
            result['heating'] = (water_heater_sts & 1) != 0
            result['last_polled_at'] = datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%dT%H:%M:%S+00:00')

            status['result'] = result
            # QoS=1 matches poll_status — ensures HA receives the fake-state update.
            self.mqtt_client.publish(f"P/{serial}/STATUS", json.dumps(status), qos=1, retain=True)
        except Exception as e:
            print(f"⚠️  Status injection failed for {serial}: {e}")

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

                self.publish_discovery(serial, name)
                print(f"🌉 Bridge active for: {name} ({serial})")

        except Exception as e:
            print(f"FAILED: Step 4 - Could not retrieve thermostat list: {e}")
            sys.exit(1)

        print("OK: Step 5 - Device discovery published.")

        self.mqtt_client.on_message = self.on_mqtt_message
        self.mqtt_client.loop_start()

        print(f"OK: Step 6 - Polling loop starting (normal={self.POLL_INTERVAL}s, confirm={self.POLL_INTERVAL_CONFIRM}s).")

        while True:
            try:
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
                time.sleep(self.current_poll_interval)

            except KeyboardInterrupt:
                print("Stopping...")
                break
            except Exception as e:
                print(f"[ERROR] Polling loop error: {e}")
                try:
                    self.login()
                except Exception as e2:
                    print(f"[ERROR] Re-login failed: {e2}")
                time.sleep(self.current_poll_interval)

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
