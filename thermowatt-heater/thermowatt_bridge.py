import sys, json, time, uuid, os, signal, requests, urllib3, datetime
import paho.mqtt.client as mqtt
from paho.mqtt.enums import CallbackAPIVersion

# FIX 10: verify=False only if explicitly opted in via env var.
# Default is proper TLS verification. Set THERMOWATT_TLS_NO_VERIFY=1 only
# if the backend genuinely rejects certificate validation (debug fallback).
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

# v1.5.1 FIX 2: HTTP timeouts — (connect_timeout, read_timeout) in seconds.
# Prevents hung cloud requests from freezing polling and command confirmation.
# 5s connect is generous for a known hostname; 15s read covers slow API responses.
HTTP_TIMEOUT = (5, 15)


class MyThermowattBridge:
    API_KEY  = "YVjArWssxKH631jv1dnnWOTr6gijsSAGz7rQJ4hJoUNRffxYvbQaMbePBEZalena"
    BASE_URL = "https://myapp-connectivity.com/api/v1"

    # FIX 7: Normal polling 60s — reduces cloud rate-limit risk and produces
    # cleaner InfluxDB calibration data (T_Avg changes ~0.14°C/interval at 60s
    # which is above thermistor noise; 20s produces noise-dominated readings).
    # Post-command confirmation window drops to 20s for 60s then resumes normal.
    POLL_INTERVAL         = 60   # seconds — normal operation
    POLL_INTERVAL_CONFIRM = 20   # seconds — post-command confirmation window
    CONFIRM_WINDOW        = 60   # seconds — how long to stay in fast-poll after a command
    STATUS_LOG_INTERVAL   = 300  # seconds — 5-minute summary log

    # FIX 2 / Safety: firmware-confirmed ceiling from T_set_max attribute.
    # Hardware mechanical cutout at ~90°C is independent of this.
    # EMS boost ceiling is 70°C — do not raise without re-verifying firmware.
    TEMP_MIN = 20
    TEMP_MAX = 70  # matches confirmed T_set_max: 70 from device attributes

    # v1.5.1 FIX 4: CMD_COOLDOWN reduced 60s → 15s.
    # 60s blocked the paired set_temperature + set_operation_mode sequence that
    # ems_v5_2_hws_follow_emhass_deferrable0 sends in a single automation action.
    # 15s is sufficient anti-spam protection for a resistive element.
    # Strategic dwell time (minimum 20–30 min between EMS decisions) is enforced
    # at the HA/EMHASS layer, not the bridge.
    CMD_COOLDOWN = 15  # seconds

    def __init__(self):
        self.config = self._load_config()
        # Single session instance — preserves cookies from AWS load balancers
        self.session = requests.Session()

        # FIX 5 (LWT): set Last Will before connect so broker publishes
        # "offline" automatically if bridge disconnects uncleanly.
        self.mqtt_client = mqtt.Client(CallbackAPIVersion.VERSION2)
        self.mqtt_client.will_set(AVAILABILITY_TOPIC, "offline", retain=True)
        if MQTT_USER:
            self.mqtt_client.username_pw_set(MQTT_USER, MQTT_PASS)

        # Polling state
        self.poll_count           = 0
        self.success_count        = 0
        self.error_count          = 0
        self.last_status_log_time = time.time()
        self.rate_limit_backoff   = 0  # 0=none, 1+=backoff steps
        self.current_poll_interval = self.POLL_INTERVAL

        # FIX 7: post-command fast-poll window tracking
        self._confirm_until: float = 0.0  # epoch time until which fast-poll is active

        # Per-device status cache — used by _inject_fake_status (FIX 3)
        self._last_status: dict = {}  # {serial: {result: {...}}}

        # Per-device command cooldown tracking (FIX 4)
        self._last_cmd_time: dict = {}  # {serial: epoch float}

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
    #  MQTT discovery                                                      #
    # ------------------------------------------------------------------ #

    def _device_block(self, serial, name):
        return {"identifiers": [f"tw_{serial}"], "manufacturer": "Thermowatt", "name": name}

    def _availability_block(self):
        return {
            "availability_topic":       AVAILABILITY_TOPIC,
            "payload_available":        "online",
            "payload_not_available":    "offline",
        }

    def publish_discovery(self, serial, name):
        device       = self._device_block(serial, name)
        avail        = self._availability_block()
        status_topic = f"P/{serial}/STATUS"

        # ── Water Heater entity ─────────────────────────────────────────
        # FIX 1: max_temp corrected to 70 — matches firmware T_set_max: 70.
        #        Advertising 75 caused HA to allow commands the firmware silently rejects.
        # FIX 6: removed explicit optimistic:True — HA derives optimistic=False
        #        automatically when mode_state_topic is present, giving confirmed state.
        # FIX 5: availability_topic added so entity shows unavailable when bridge dies.
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
            # FIX 2 (carried forward): all branches match modes list exactly
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
        # FIX 1 (carried forward): pre-computed heating bool with | lower
        # FIX 5: availability added
        heating_payload = {
            "unique_id":        f"thermowatt_{serial}_heating",
            "name":             f"{name} Heating",
            "state_topic":      status_topic,
            "value_template":   "{{ value_json.result.heating | default(false) | lower }}",
            "payload_on":       "true",
            "payload_off":      "false",
            "device_class":     "heat",
            "icon":             "mdi:fire",
            "device":           device,
            **avail,
        }
        self.mqtt_client.publish(
            f"homeassistant/binary_sensor/{serial}/heating/config",
            json.dumps(heating_payload), retain=True
        )

        # ── Individual sensors for EMS-critical fields ──────────────────
        # FIX 8: Time_eco / Time_prog changed to state_class: measurement
        #        until confirmed as lifetime-increasing counters.
        # FIX 9: last_polled value_template returns none (not 'unknown') on
        #        missing field so HA timestamp device_class does not error.
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
                "state_class":          "total_increasing",
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
                # FIX 9: return none (null) not string 'unknown' — HA timestamp
                # device_class requires a valid ISO8601 value or none, not a string.
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
                self.mqtt_client.publish(f"P/{serial}/STATUS", json.dumps(status_data), retain=True)
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
        FIX 4: prevents rapid element cycling which stresses the heating element.
        """
        last = self._last_cmd_time.get(serial, 0)
        elapsed = time.time() - last
        if elapsed < self.CMD_COOLDOWN:
            remaining = int(self.CMD_COOLDOWN - elapsed)
            print(f"[CMD] Cooldown active for {serial} — {remaining}s remaining. Command ignored.")
            return False
        return True

    def _record_command(self, serial):
        """Record command timestamp and enter fast-poll confirmation window.
        FIX 7: fast-poll window gives HA confirmed state feedback within 20s.
        """
        self._last_cmd_time[serial] = time.time()
        self._confirm_until = time.time() + self.CONFIRM_WINDOW
        self.current_poll_interval = self.POLL_INTERVAL_CONFIRM
        print(f"[CMD] Confirmation window active — polling at {self.POLL_INTERVAL_CONFIRM}s for {self.CONFIRM_WINDOW}s")

    def on_mqtt_message(self, client, userdata, msg):
        """Local HA → REST API command handler."""
        try:
            # v1.5.1 FIX 1: Ignore retained commands.
            # MQTT brokers replay the last retained message to new subscribers on connect.
            # A retained CMD topic could execute a stale mode/temperature command on
            # bridge restart. Command topics must never be acted on from retained state.
            if getattr(msg, "retain", False):
                print(f"[CMD] Ignoring retained command on {msg.topic}")
                return

            payload = msg.payload.decode()
            parts   = msg.topic.split('/')
            if len(parts) < 2:
                return
            sn = parts[1]

            device_config = self.config.get('devices', {}).get(sn, {})
            if not device_config:
                print(f"⚠️  Unknown device serial: {sn}")
                return

            # FIX 4: enforce cooldown before any command
            if not self._check_cooldown(sn):
                return

            current_fav = device_config.get("last_setpoint", 60)

            if f"P/{sn}/CMD/TEMP" in msg.topic:
                # FIX 2 (safety): clamp to firmware-confirmed safe range before sending.
                # Firmware T_set_max: 70 — values above 70 are silently rejected by
                # the device. Mechanical cutout at ~90°C is independent hardware safety.
                raw_temp = int(float(payload))
                temp = max(self.TEMP_MIN, min(self.TEMP_MAX, raw_temp))
                if temp != raw_temp:
                    print(f"[CMD] Temperature {raw_temp}°C clamped to {temp}°C (firmware safe range {self.TEMP_MIN}–{self.TEMP_MAX}°C)")
                print(f"[CMD] Setting Temperature to {temp}°C for {sn}...")

                # FIX 3: only inject fake status if API call succeeds (2xx)
                resp = self.request("POST", "/manual", serial=sn, json={"T_SetPoint": temp})
                if resp is not None and 200 <= resp.status_code < 300:
                    device_config["last_setpoint"] = temp
                    self.config['devices'][sn]     = device_config
                    self._inject_fake_status(sn, {"T_SetPoint": str(temp)})
                    self._record_command(sn)
                else:
                    code = resp.status_code if resp else "no response"
                    print(f"[ERROR] Temperature command failed ({code}) — HA state not updated")

            elif f"P/{sn}/CMD/MODE" in msg.topic:
                print(f"[CMD] Setting Mode to {payload} for {sn}...")

                # FIX 3: check response before injecting fake status on every branch
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

            self._save_config()
        except Exception as e:
            print(f"MQTT Cmd Error: {e}")

    def _inject_fake_status(self, serial, overrides):
        """Immediately updates HA state to prevent flipping while cloud syncs.
        Only called after a confirmed 2xx API response (FIX 3).
        Uses cached last known status instead of a live GET (FIX 3 carried forward).
        """
        try:
            status = json.loads(json.dumps(self._last_status.get(serial, {"result": {}})))
            result = status.get('result', {})

            for k, v in overrides.items():
                result[k] = str(v)

            # Recompute heating flag from overridden WaterHeaterSts if present
            water_heater_sts    = int(result.get('WaterHeaterSts', 0))
            result['heating']   = (water_heater_sts & 1) != 0
            result['last_polled_at'] = datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%dT%H:%M:%S+00:00')

            status['result'] = result
            self.mqtt_client.publish(f"P/{serial}/STATUS", json.dumps(status), retain=True)
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
            self.poll_count    = 0
            self.success_count = 0
            self.error_count   = 0
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
            self.mqtt_client.connect(MQTT_HOST, MQTT_PORT, 60)
            # FIX 5: publish online after successful connect
            self.mqtt_client.publish(AVAILABILITY_TOPIC, "online", retain=True)
            print("OK: Step 2 & 3 - Connected to MQTT. Availability: online.")
        except Exception as e:
            print(f"FAILED: Step 2/3 - MQTT Connection Error: {e}")
            sys.exit(1)

        try:
            self.login()
            print("OK: Step 4 - Logged in to Thermowatt backend.")
        except Exception as e:
            print(f"FAILED: Step 4 - Backend authentication failed: {e}")
            sys.exit(1)

        try:
            r       = self.request("GET", "/user-info")
            devices = r.json().get('result', {}).get('termostati', [])
            if not devices:
                raise Exception("Zero devices returned")

            if 'devices' not in self.config:
                self.config['devices'] = {}

            print(f"OK: Step 5 - Found {len(devices)} thermostats.")

            for dev in devices:
                serial = dev['seriale']
                name   = dev.get('nome', 'Boiler')

                if serial not in self.config['devices']:
                    self.config['devices'][serial] = {"name": name, "last_setpoint": 60}
                else:
                    self.config['devices'][serial]["name"] = name

                self.publish_discovery(serial, name)
                self.mqtt_client.subscribe(f"P/{serial}/CMD/#")
                print(f"🌉 Bridge active for: {name} ({serial})")

            self._save_config()

        except Exception as e:
            print(f"FAILED: Step 5 - Could not retrieve thermostat list: {e}")
            sys.exit(1)

        print("OK: Step 6 - Booted successfully.")

        self.mqtt_client.on_message = self.on_mqtt_message
        self.mqtt_client.loop_start()

        print(f"OK: Step 7 - Starting polling loop (normal={self.POLL_INTERVAL}s, confirm={self.POLL_INTERVAL_CONFIRM}s).")

        while True:
            try:
                # FIX 7: exit fast-poll window once confirm period has elapsed
                if self.current_poll_interval == self.POLL_INTERVAL_CONFIRM:
                    if time.time() >= self._confirm_until:
                        self.current_poll_interval = self.POLL_INTERVAL
                        print(f"[POLL] Confirmation window closed — resuming normal polling ({self.POLL_INTERVAL}s)")

                for serial in self.config.get('devices', {}).keys():
                    self.poll_count += 1
                    success, status_code = self.poll_status(serial)

                    if success:
                        self.success_count += 1
                        if self.rate_limit_backoff > 0:
                            self.rate_limit_backoff    = 0
                            # Restore normal or confirm interval, not hardcoded 20s
                            if time.time() < self._confirm_until:
                                self.current_poll_interval = self.POLL_INTERVAL_CONFIRM
                            else:
                                self.current_poll_interval = self.POLL_INTERVAL
                    else:
                        self.error_count += 1
                        if status_code == 429:
                            self.rate_limit_backoff += 1
                            # Backoff: 60s → 120s → 180s (already at 60s normal, so steps above it)
                            backoff_interval = min(60 * (self.rate_limit_backoff + 1), 180)
                            self.current_poll_interval = backoff_interval
                            print(f"[RATE LIMIT] 429 received, backing off to {self.current_poll_interval}s")
                            break
                        elif status_code is not None:
                            print(f"[ERROR] Got status {status_code}, re-logging in...")
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

        # v1.5.1 FIX 3: Explicitly publish offline on clean shutdown.
        # MQTT LWT fires on unclean disconnect only. A clean add-on stop/restart
        # leaves entities showing available without this explicit publish.
        self.mqtt_client.publish(AVAILABILITY_TOPIC, "offline", retain=True)
        time.sleep(0.2)  # allow publish to flush before disconnect
        self.mqtt_client.disconnect()


def _sigterm_handler(signum, frame):
    """SIGTERM handler — HA add-on supervisor sends SIGTERM on stop.
    Raises KeyboardInterrupt so the main loop's clean shutdown path executes.
    """
    raise KeyboardInterrupt


if __name__ == "__main__":
    signal.signal(signal.SIGTERM, _sigterm_handler)
    bridge = MyThermowattBridge()
    bridge.run()
