import json, os, ssl, sys, threading, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

LIMIT_FROM = float(os.environ.get("LIMIT_FROM", "-1"))
LIMIT_UNTIL = float(os.environ.get("LIMIT_UNTIL", "-1"))
STYLE = os.environ.get("LIMIT_STYLE", "200")
T0 = None
STATE = {"setpoint": 60, "cmd": 9, "t_avg": 55.0}
LOCK = threading.Lock()


def log(**kw):
    kw["t"] = round(time.time() - T0, 1) if T0 else 0
    print(json.dumps(kw), flush=True)


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
        global T0
        n = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(n) if n else b""
        with LOCK:
            if T0 is None:
                T0 = time.time()
            now = time.time() - T0
            path = self.path.split("/api/v1", 1)[-1]
            limited = LIMIT_FROM >= 0 and LIMIT_FROM <= now < LIMIT_UNTIL
            log(m=self.command, p=path, limited=limited,
                body="<redacted>" if path in ("/login", "/refresh") else body.decode()[:80])
            if limited:
                if STYLE == "429":
                    return self._send(429, {"message": "Too many requests"})
                return self._send(200, {"success": False, "error": "Daily request limit exceeded"})
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
ctx.load_cert_chain("/certs/ca.pem", "/certs/key.pem")
srv = ThreadingHTTPServer(("0.0.0.0", 443), H)
srv.socket = ctx.wrap_socket(srv.socket, server_side=True)
print("stub ready", flush=True)
srv.serve_forever()
