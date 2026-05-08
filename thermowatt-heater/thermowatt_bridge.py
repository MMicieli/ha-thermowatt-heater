import sys, json, time, uuid, os, requests, urllib3, datetime
import paho.mqtt.client as mqtt
from paho.mqtt.enums import CallbackAPIVersion

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# --- CONFIGURATION ---
EMAIL = sys.argv[1] if len(sys.argv) > 1 else None
PASSWORD = sys.argv[2] if len(sys.argv) > 2 else None
CONFIG_FILE = "/data/thermowatt_config.json" if os.path.exists("/data") else "thermowatt_config.json"
MQTT_HOST = os.getenv("MQTT_HOST", "core-mosquitto")
MQTT_PORT = int(os.getenv("MQTT_PORT", 1883))
MQTT_USER = os.getenv("MQTT_USER")
MQTT_PASS = os.getenv("MQTT_PASSWORD")


class MyThermowattBridge:
    API_KEY = "YVjArWssxKH631jv1dnnWOTr6gijsSAGz7rQJ4hJoUNRffxYvbQaMbePBEZalena"
    BASE_URL = "https://myapp-connectivity.com/api/v1"
    POLL_INTERVAL = 20          # seconds (matches their TimeBeforeNextRefresh)
    STATUS_LOG_INTERVAL = 300   # 5 minutes

    def __init__(self):
        self.config = self._load_config()
        # Single session instance — preserves cookies from AWS load balancers
        self.session = requests.Session()
        self.mqtt_client = mqtt.Client(CallbackAPIVersion.VERSION2)
        if MQTT_USER:
            self.mqtt_client.username_pw_set(MQTT_USER, MQTT_PASS)

        # Polling state
        self.poll_count = 0
        self.success_count = 0
        self.error_count = 0
        self.last_status_log_time = time.time()
        self.rate_limit_backoff = 0  # 0=none, 1=20s, 2=40s, 3+=60s
        self.current_poll_interval = self.POLL_INTERVAL

        # FIX 3: Per-device status cache — used by _inject_fake_status
        # Avoids an extra live GET on every command
        self._last_status: dict = {}  # {serial: {result: {...}}}

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
            "device_uuid": str(uuid.uuid4()),
            "access_token": None,
            "refresh_token": None,
            "devices": {}
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
            "app": "MyThermowatt",
            "platform": "iOS",
            "version": "3.14",
            "lang": "en"
        })

    def _update_auth(self, access, refresh):
        self.config.update({"access_token": access, "refresh_token": refresh})
        self.session.headers["Authorization"] = f"Bearer {access}"
        self._save_config()

    def login(self):
        self._reset_headers()
        self.session.headers["x-api-key"] = self.API_KEY
        payload = {"username": EMAIL, "password": PASSWORD, "device_id": self.config["device_uuid"]}
        r = self.session.post(f"{self.BASE_URL}/login", json=payload, verify=False)
        r.raise_for_status()
        res = r.json()['result']
        self._update_auth(res['accessToken'], res['refreshToken'])

    def refresh_session(self):
        self._reset_headers()
        self.session.headers["x-api-key"] = self.API_KEY
        payload = {"username": EMAIL, "refreshToken": self.config["refresh_token"]}
        r = self.session.post(f"{self.BASE_URL}/refresh", json=payload, verify=False)
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
        resp = self.session.request(method, url, verify=False, **kwargs)
        if resp.status_code == 401:
            if self.refresh_session():
                self._reset_headers()
                if serial:
                    self.session.headers["seriale"] = serial
                resp = self.session.request(method, url, verify=False, **kwargs)
        return resp

    # ------------------------------------------------------------------ #
    #  MQTT discovery                                                      #
    # ------------------------------------------------------------------ #

    def _device_block(self, serial, name):
        return {"identifiers": [f"tw_{serial}"], "manufacturer": "Thermowatt", "name": name}

    def publish_discovery(self, serial, name):
        device = self._device_block(serial, name)
        status_topic = f"P/{serial}/STATUS"

        # ── Water Heater entity ─────────────────────────────────────────
        wh_payload = {
            "unique_id": f"thermowatt_{serial}_v314",
            "name": f"Boiler {name}",
            "temp_unit": "C",
            "min_temp": 20,
            "max_temp": 75,
            "optimistic": True,
            "current_temperature_topic": status_topic,
            "current_temperature_template": "{{ value_json.result.T_Avg | default(0) | float }}",
            "temperature_state_topic": status_topic,
            "temperature_state_template": "{{ value_json.result.T_SetPoint | default(0) | float }}",
            "temperature_command_topic": f"P/{serial}/CMD/TEMP",
            "mode_state_topic": status_topic,
            # FIX 2: All branches return strings that exactly match the modes list
            "mode_state_template": (
                "{% set cmd = value_json.result.Cmd | default(0) | int %}"
                "{% if cmd == 9 %}Manual"
                "{% elif cmd == 3 %}Eco"
                "{% elif cmd == 17 %}Auto"
                "{% elif cmd == 65 %}Holiday"
                "{% elif cmd == 16 %}Off"
                "{% else %}Off{% endif %}"
            ),
            "mode_command_topic": f"P/{serial}/CMD/MODE",
            "modes": ["Off", "Eco", "Manual", "Auto", "Holiday"],
            "json_attributes_topic": status_topic,
            "json_attributes_template": "{{ value_json.result | tojson }}",
            "device": device,
        }
        self.mqtt_client.publish(
            f"homeassistant/water_heater/{serial}/config",
            json.dumps(wh_payload), retain=True
        )

        # ── Binary sensor: Heating active (flame indicator) ─────────────
        # FIX 1: Use computed result.heating bool instead of raw bitmask expression
        # which rendered as Python "True"/"False" instead of matching payload_on "true"
        heating_payload = {
            "unique_id": f"thermowatt_{serial}_heating",
            "name": f"{name} Heating",
            "state_topic": status_topic,
            "value_template": "{{ value_json.result.heating | default(false) | lower }}",
            "payload_on": "true",
            "payload_off": "false",
            "device_class": "heat",
            "icon": "mdi:fire",
            "device": device,
        }
        self.mqtt_client.publish(
            f"homeassistant/binary_sensor/{serial}/heating/config",
            json.dumps(heating_payload), retain=True
        )

        # ── FIX 5: Individual sensors for EMS-critical fields ────────────
        sensors = [
            {
                "unique_id": f"thermowatt_{serial}_t_avg",
                "name": f"{name} Average Temperature",
                "state_topic": status_topic,
                "value_template": "{{ value_json.result.T_Avg | default(0) | float | round(1) }}",
                "unit_of_measurement": "°C",
                "device_class": "temperature",
                "state_class": "measurement",
                "icon": "mdi:thermometer-water",
                "slug": "t_avg",
            },
            {
                "unique_id": f"thermowatt_{serial}_t_desired",
                "name": f"{name} Desired Temperature",
                "state_topic": status_topic,
                "value_template": "{{ value_json.result.T_dsrd | default(0) | float | round(1) }}",
                "unit_of_measurement": "°C",
                "device_class": "temperature",
                "state_class": "measurement",
                "icon": "mdi:thermometer-chevron-up",
                "slug": "t_desired",
            },
            {
                "unique_id": f"thermowatt_{serial}_t_boost",
                "name": f"{name} Boost Ceiling",
                "state_topic": status_topic,
                "value_template": "{{ value_json.result.TBoost | default(0) | float | round(1) }}",
                "unit_of_measurement": "°C",
                "device_class": "temperature",
                "state_class": "measurement",
                "icon": "mdi:thermometer-high",
                "slug": "t_boost",
            },
            {
                "unique_id": f"thermowatt_{serial}_time_eco",
                "name": f"{name} Eco Runtime",
                "state_topic": status_topic,
                "value_template": "{{ value_json.result.Time_eco | default(0) | int }}",
                "unit_of_measurement": "min",
                "state_class": "total_increasing",
                "icon": "mdi:timer-outline",
                "slug": "time_eco",
            },
            {
                "unique_id": f"thermowatt_{serial}_time_prog",
                "name": f"{name} Programme Runtime",
                "state_topic": status_topic,
                "value_template": "{{ value_json.result.Time_prog | default(0) | int }}",
                "unit_of_measurement": "min",
                "state_class": "total_increasing",
                "icon": "mdi:timer-check-outline",
                "slug": "time_prog",
            },
            {
                "unique_id": f"thermowatt_{serial}_rssi",
                "name": f"{name} WiFi Signal",
                "state_topic": status_topic,
                "value_template": "{{ value_json.result.Rssi | default(0) | int }}",
                "unit_of_measurement": "dBm",
                "device_class": "signal_strength",
                "state_class": "measurement",
                "entity_category": "diagnostic",
                "icon": "mdi:wifi",
                "slug": "rssi",
            },
            # FIX 4: Restored truncated last_polled sensor
            {
                "unique_id": f"thermowatt_{serial}_last_polled",
                "name": f"{name} Last Polled",
                "state_topic": status_topic,
                "value_template": "{{ value_json.result.last_polled_at | default('unknown') }}",
                "device_class": "timestamp",
                "entity_category": "diagnostic",
                "icon": "mdi:clock-check-outline",
                "slug": "last_polled",
            },
            # Phase 2 calibration: ambient temperature for cooling_constant derivation
            {
                "unique_id": f"thermowatt_{serial}_t_amb",
                "name": f"{name} Ambient Temperature",
                "state_topic": status_topic,
                "value_template": "{{ value_json.result.TAmb | default(0) | float | round(1) }}",
                "unit_of_measurement": "°C",
                "device_class": "temperature",
                "state_class": "measurement",
                "entity_category": "diagnostic",
                "icon": "mdi:thermometer-lines",
                "slug": "t_amb",
            },
            # Phase 2 calibration: raw bitmask for anomaly filtering in InfluxDB queries
            {
                "unique_id": f"thermowatt_{serial}_water_heater_sts",
                "name": f"{name} Water Heater Status Raw",
                "state_topic": status_topic,
                "value_template": "{{ value_json.result.WaterHeaterSts | default(0) | int }}",
                "state_class": "measurement",
                "entity_category": "diagnostic",
                "icon": "mdi:state-machine",
                "slug": "water_heater_sts",
            },
        ]

        for s in sensors:
            slug = s.pop("slug")
            s["device"] = device
            self.mqtt_client.publish(
                f"homeassistant/sensor/{serial}/{slug}/config",
                json.dumps(s), retain=True
            )

    # ------------------------------------------------------------------ #
    #  Status publishing                                                   #
    # ------------------------------------------------------------------ #

    def _compute_status(self, status_data: dict) -> dict:
        """Adds computed fields to the result dict in-place and returns it."""
        result = status_data.get('result', {})
        water_heater_sts = int(result.get('WaterHeaterSts', 0))
        result['heating'] = (water_heater_sts & 1) != 0
        # FIX 4: Inject poll timestamp so HA can detect stale data
        result['last_polled_at'] = datetime.datetime.utcnow().strftime('%Y-%m-%dT%H:%M:%S+00:00')
        return status_data

    def poll_status(self, serial):
        """Poll status for a device — returns (success, status_code)."""
        try:
            r = self.request("GET", "/status", serial=serial)
            status_code = r.status_code

            if status_code == 200:
                status_data = r.json()
                status_data = self._compute_status(status_data)
                # FIX 3: Cache for use by _inject_fake_status
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

    def on_mqtt_message(self, client, userdata, msg):
        """Local HA → REST API (Commands)."""
        try:
            payload = msg.payload.decode()
            parts = msg.topic.split('/')
            if len(parts) < 2:
                return
            sn = parts[1]

            device_config = self.config.get('devices', {}).get(sn, {})
            if not device_config:
                print(f"⚠️  Unknown device serial: {sn}")
                return

            current_fav = device_config.get("last_setpoint", 60)

            if f"P/{sn}/CMD/TEMP" in msg.topic:
                temp = int(float(payload))
                print(f"[CMD] Setting Temperature to {temp}°C for {sn}...")
                self.request("POST", "/manual", serial=sn, json={"T_SetPoint": temp})
                device_config["last_setpoint"] = temp
                self.config['devices'][sn] = device_config
                self._inject_fake_status(sn, {"T_SetPoint": str(temp)})

            elif f"P/{sn}/CMD/MODE" in msg.topic:
                print(f"[CMD] Setting Mode to {payload} for {sn}...")

                if payload == "Manual":
                    self.request("POST", "/manual", serial=sn, json={"T_SetPoint": current_fav})
                    self._inject_fake_status(sn, {"Cmd": "9", "T_SetPoint": str(current_fav)})
                elif payload == "Eco":
                    self.request("POST", "/eco", serial=sn, headers={"Content-Type": "text/plain"}, data="")
                    self._inject_fake_status(sn, {"Cmd": "3"})
                elif payload == "Auto":
                    self.request("POST", "/auto", serial=sn, headers={"Content-Type": "text/plain"}, data="")
                    self._inject_fake_status(sn, {"Cmd": "17"})
                elif payload == "Holiday":
                    future_date = (datetime.datetime.now() + datetime.timedelta(days=30)).strftime("%Y-%m-%d")
                    print(f"[CMD] Setting Holiday Mode until {future_date} for {sn}...")
                    self.request("POST", "/holiday", serial=sn, json={"end_date": future_date})
                    self._inject_fake_status(sn, {"Cmd": "65"})
                elif payload == "Off":
                    print(f"[CMD] Turning Boiler OFF for {sn}...")
                    resp = self.request("POST", "/off", serial=sn, headers={"Content-Type": "text/plain"}, data="")
                    self._inject_fake_status(sn, {"Cmd": "16"})
                    if resp:
                        print(f"[SUCCESS] Boiler {sn} is now OFF: {resp.text}")

            self._save_config()
        except Exception as e:
            print(f"MQTT Cmd Error: {e}")

    def _inject_fake_status(self, serial, overrides):
        """Immediately updates HA state to prevent flipping while cloud syncs.

        FIX 3: Uses cached last known status instead of a live GET,
        eliminating an extra API call on every command.
        """
        try:
            # Use cached status; fall back to empty result if not yet polled
            status = json.loads(json.dumps(self._last_status.get(serial, {"result": {}})))
            result = status.get('result', {})

            for k, v in overrides.items():
                result[k] = str(v)

            # Recompute heating flag from overridden WaterHeaterSts if present
            water_heater_sts = int(result.get('WaterHeaterSts', 0))
            result['heating'] = (water_heater_sts & 1) != 0
            result['last_polled_at'] = datetime.datetime.utcnow().strftime('%Y-%m-%dT%H:%M:%S+00:00')

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
            print(f"[STATUS] Polled {self.poll_count} times, got {self.success_count} x 200, {self.error_count} errors")
            self.poll_count = 0
            self.success_count = 0
            self.error_count = 0
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
            print("OK: Step 2 & 3 - Connected and authenticated with local MQTT.")
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
            r = self.request("GET", "/user-info")
            devices = r.json().get('result', {}).get('termostati', [])
            if not devices:
                raise Exception("Zero devices returned")

            if 'devices' not in self.config:
                self.config['devices'] = {}

            print(f"OK: Step 5 - Found {len(devices)} thermostats.")

            for dev in devices:
                serial = dev['seriale']
                name = dev.get('nome', 'Boiler')

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

        print(f"OK: Step 7 - Starting polling loop (interval: {self.POLL_INTERVAL}s).")

        while True:
            try:
                for serial in self.config.get('devices', {}).keys():
                    self.poll_count += 1
                    success, status_code = self.poll_status(serial)

                    if success:
                        self.success_count += 1
                        if self.rate_limit_backoff > 0:
                            self.rate_limit_backoff = 0
                            self.current_poll_interval = self.POLL_INTERVAL
                    else:
                        self.error_count += 1
                        if status_code == 429:
                            self.rate_limit_backoff += 1
                            if self.rate_limit_backoff == 1:
                                self.current_poll_interval = 20
                            elif self.rate_limit_backoff == 2:
                                self.current_poll_interval = 40
                            else:
                                self.current_poll_interval = 60
                            print(f"[RATE LIMIT] 429 received, backing off to {self.current_poll_interval}s interval")
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

        self.mqtt_client.disconnect()


if __name__ == "__main__":
    bridge = MyThermowattBridge()
    bridge.run()
