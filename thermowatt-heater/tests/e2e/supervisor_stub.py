"""Minimal stand-in for the Home Assistant Supervisor API used by bashio in run.sh.

Serves only what run.sh needs and logs every request (path + whether the bearer token matched,
never the token itself) so the harness can assert the real entrypoint used it:
  GET /addons/self/options/config -> add-on options (email, password, element_wattage)
  GET /services/mqtt              -> MQTT service discovery (host/port/username/password)
Anything else is logged as unexpected and answered 404.
"""
import json
import os
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

TOKEN = os.environ["SUPERVISOR_TOKEN"]
OPTIONS = json.loads(os.environ["ADDON_OPTIONS_JSON"])
MQTT = json.loads(os.environ["MQTT_SERVICE_JSON"])
T0 = time.time()


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        authed = self.headers.get("Authorization") == f"Bearer {TOKEN}"
        known = {
            "/addons/self/options/config": OPTIONS,
            "/services/mqtt": MQTT,
        }
        print(json.dumps({"t": round(time.time() - T0, 1), "m": "GET", "p": self.path,
                          "authed": authed, "known": self.path in known}), flush=True)
        if authed and self.path in known:
            body, code = {"result": "ok", "data": known[self.path]}, 200
        else:
            body, code = {"result": "error", "message": "unexpected or unauthorised"}, 404 if authed else 401
        raw = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)


print("supervisor stub ready", flush=True)
ThreadingHTTPServer(("0.0.0.0", int(os.environ.get("STUB_PORT", "80"))), Handler).serve_forever()
