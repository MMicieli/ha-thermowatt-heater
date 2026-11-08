import base64, json, os, ssl, sys, threading, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

LIMIT_FROM = float(os.environ.get("LIMIT_FROM", "-1"))
LIMIT_UNTIL = float(os.environ.get("LIMIT_UNTIL", "-1"))
STYLE = os.environ.get("LIMIT_STYLE", "200")
# Optional JWT mode (scenarios C/D/F/G of run_e2e.sh). Unset/0 keeps the original behaviour
# byte for byte: opaque tokens "stub-a"/"stub-r" that are never validated.
JWT_LIFETIME = float(os.environ.get("JWT_LIFETIME", "0"))              # access-token lifetime, s
JWT_REFRESH_LIFETIME = float(os.environ.get("JWT_REFRESH_LIFETIME", str(JWT_LIFETIME)))
REFRESH_FAIL_FIRST = int(os.environ.get("REFRESH_FAIL_FIRST", "0"))   # first N /refresh -> HTTP 500
REFRESH_SEEN = 0
JWT_SIG = "SIGNATURE-SECRET-JWT"
REFRESH_TOKEN_VALUE = "stub-refresh-SECRET"
T0 = None
STATE = {"setpoint": 60, "cmd": 9, "t_avg": 55.0}
LOCK = threading.Lock()


def log(**kw):
    kw["t"] = round(time.time() - T0, 1) if T0 else 0
    print(json.dumps(kw), flush=True)


def make_jwt(lifetime):
    enc = lambda o: base64.urlsafe_b64encode(json.dumps(o).encode()).decode().rstrip("=")  # noqa: E731
    now = time.time()
    return f"{enc({'alg': 'none'})}.{enc({'iat': int(now), 'exp': now + lifetime})}.{JWT_SIG}"


def token_state(header):
    """'ok' / 'expired' / 'none' for the Authorization header (JWT mode only)."""
    tok = (header or "").replace("Bearer ", "", 1)
    try:
        payload = tok.split(".")[1]
        exp = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))["exp"]
    except Exception:  # noqa: BLE001
        return "none"
    return "ok" if exp > time.time() else "expired"


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, body=None):
        raw = b"" if body is None else json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json" if body is not None else "text/plain")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _handle(self):
        global T0, REFRESH_SEEN
        n = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(n) if n else b""
        with LOCK:
            if T0 is None:
                T0 = time.time()
            now = time.time() - T0
            path = self.path.split("/api/v1", 1)[-1]
            limited = LIMIT_FROM >= 0 and LIMIT_FROM <= now < LIMIT_UNTIL
            extra = {}
            if JWT_LIFETIME > 0 and path not in ("/login", "/refresh"):
                extra["auth"] = token_state(self.headers.get("Authorization"))
            log(m=self.command, p=path, limited=limited,
                body="<redacted>" if path in ("/login", "/refresh") else body.decode()[:80], **extra)
            if limited:
                if STYLE == "429":
                    return self._send(429, {"message": "Too many requests"})
                return self._send(200, {"success": False, "error": "Daily request limit exceeded"})
            if JWT_LIFETIME > 0 and path not in ("/login", "/refresh") and extra["auth"] != "ok":
                return self._send(401, {"success": False, "error": "token expired or missing"})
            if JWT_LIFETIME > 0 and path == "/refresh":
                REFRESH_SEEN += 1
                if REFRESH_SEEN <= REFRESH_FAIL_FIRST:
                    return self._send(500, {"message": "stub refresh failure"})
                return self._send(200, {"result": {"accessToken": make_jwt(JWT_REFRESH_LIFETIME),
                                                   "refreshToken": REFRESH_TOKEN_VALUE}})
            if path == "/login" and JWT_LIFETIME > 0:
                return self._send(200, {"result": {"accessToken": make_jwt(JWT_LIFETIME),
                                                   "refreshToken": REFRESH_TOKEN_VALUE}})
            if path == "/login":
                return self._send(200, {"result": {"accessToken": "stub-a", "refreshToken": "stub-r"}})
            if path == "/user-info":
                return self._send(200, {"result": {"termostati": [{"seriale": "STUBSN1", "nome": "HWS"}]}})
            if path == "/status":
                return self._send(200, {"result": {"WaterHeaterSts": int(STATE["setpoint"] > STATE["t_avg"]),
                                                   "T_Avg": STATE["t_avg"], "T_SetPoint": STATE["setpoint"],
                                                   "Cmd": STATE["cmd"]}})
            if self.command == "POST":
                if path == "/manual" and body:
                    STATE["setpoint"] = json.loads(body).get("T_SetPoint", STATE["setpoint"])
                if path == "/eco":
                    STATE["cmd"] = 3
                if path == "/off":
                    STATE["cmd"] = 8
                return self._send(200)  # body-less success
            return self._send(404, {"success": False, "error": "unknown"})

    do_GET = do_POST = _handle


ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
ctx.load_cert_chain(os.environ.get("CERT_PEM", "/certs/ca.pem"), os.environ.get("KEY_PEM", "/certs/key.pem"))
srv = ThreadingHTTPServer(("0.0.0.0", int(os.environ.get("STUB_API_PORT", "443"))), H)
srv.socket = ctx.wrap_socket(srv.socket, server_side=True)
print("stub ready", flush=True)
srv.serve_forever()
