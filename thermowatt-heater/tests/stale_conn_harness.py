"""Local, vendor-free harness for the stale keep-alive connection failure (issue #23).

Models the one situation urllib3 cannot protect against: an intermediary closes an idle
keep-alive connection, but its FIN reaches the client *after* the client has already sent
the next request on that connection (one-way latency).  urllib3 only discards a pooled
connection it has already seen closed, so the request goes out, the peer is gone, and the
client gets ``RemoteDisconnected: Remote end closed connection without response``.

* ``IdleClosingServer``  - minimal HTTP/1.1 keep-alive server that closes a connection after
  ``idle_close_s`` without a request (the simulated load balancer idle timeout).
* ``DelayedFinProxy``    - TCP relay that delays every server->client byte *and EOF* by
  ``delay_s`` (simulated one-way latency).  A request sent in
  ``[idle_close_s, idle_close_s + delay_s)`` after the previous response therefore hits a
  connection the server has already closed while the client has not been told yet.

Pure standard library; binds to 127.0.0.1 only; never contacts any real host.
"""
import json
import socket
import threading
import time


class IdleClosingServer:
    def __init__(self, idle_close_s, honour_connection_close=True, body=None, status=200,
                 extra_headers=None):
        self.idle_close_s = idle_close_s
        self.honour_connection_close = honour_connection_close
        self.body = body if body is not None else {"result": {"ok": True}}
        self.status = status
        self.extra_headers = extra_headers or {}
        self.requests = []          # one dict per request seen: method, path, headers, body
        self.connections = 0        # TCP connections accepted
        self._lock = threading.Lock()
        self._sock = socket.socket()
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind(("127.0.0.1", 0))
        self._sock.listen(16)
        self.port = self._sock.getsockname()[1]
        self._stop = False
        threading.Thread(target=self._accept, daemon=True).start()

    def close(self):
        self._stop = True
        try:
            self._sock.close()
        except OSError:
            pass

    def _accept(self):
        while not self._stop:
            try:
                conn, _ = self._sock.accept()
            except OSError:
                return
            with self._lock:
                self.connections += 1
            threading.Thread(target=self._serve, args=(conn,), daemon=True).start()

    def _read_request(self, conn):
        data = b""
        while b"\r\n\r\n" not in data:
            chunk = conn.recv(65536)
            if not chunk:
                return None
            data += chunk
        head, _, rest = data.partition(b"\r\n\r\n")
        lines = head.decode("latin-1").split("\r\n")
        method, path, _ = lines[0].split(" ", 2)
        headers = {}
        for ln in lines[1:]:
            k, _, v = ln.partition(":")
            headers[k.strip().lower()] = v.strip()
        need = int(headers.get("content-length", "0"))
        while len(rest) < need:
            rest += conn.recv(65536)
        return {"method": method, "path": path, "headers": headers, "body": rest[:need]}

    def _serve(self, conn):
        first = True
        try:
            while True:
                # The first request may take the connect time; later ones must arrive within
                # the simulated idle timeout or the connection is closed (FIN).
                conn.settimeout(None if first else self.idle_close_s)
                first = False
                try:
                    req = self._read_request(conn)
                except socket.timeout:
                    return
                if req is None:
                    return
                with self._lock:
                    self.requests.append(req)
                payload = json.dumps(self.body).encode()
                close_after = (self.honour_connection_close
                               and req["headers"].get("connection", "").lower() == "close")
                hdr = [f"HTTP/1.1 {self.status} OK", "Content-Type: application/json",
                       f"Content-Length: {len(payload)}"]
                # Load-balancer style stickiness cookie, issued on every response.
                hdr.append("Set-Cookie: AWSALB=sticky-1; Path=/")
                for k, v in self.extra_headers.items():
                    hdr.append(f"{k}: {v}")
                if close_after:
                    hdr.append("Connection: close")
                conn.sendall(("\r\n".join(hdr) + "\r\n\r\n").encode() + payload)
                if close_after:
                    return
        except OSError:
            return
        finally:
            try:
                conn.close()
            except OSError:
                pass


class DelayedFinProxy:
    """Relays client<->upstream; server->client bytes and EOF are delayed by ``delay_s``."""

    def __init__(self, upstream_port, delay_s):
        self.upstream_port = upstream_port
        self.delay_s = delay_s
        self._sock = socket.socket()
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind(("127.0.0.1", 0))
        self._sock.listen(16)
        self.port = self._sock.getsockname()[1]
        self._stop = False
        threading.Thread(target=self._accept, daemon=True).start()

    def close(self):
        self._stop = True
        try:
            self._sock.close()
        except OSError:
            pass

    def _accept(self):
        while not self._stop:
            try:
                client, _ = self._sock.accept()
            except OSError:
                return
            up = socket.create_connection(("127.0.0.1", self.upstream_port))
            threading.Thread(target=self._c2s, args=(client, up), daemon=True).start()
            threading.Thread(target=self._s2c, args=(client, up), daemon=True).start()

    @staticmethod
    def _c2s(client, up):
        try:
            while True:
                data = client.recv(65536)
                if not data:
                    break
                try:
                    up.sendall(data)
                except OSError:
                    break  # upstream already closed: the request is silently lost
        except OSError:
            pass
        finally:
            try:
                up.shutdown(socket.SHUT_WR)
            except OSError:
                pass

    def _s2c(self, client, up):
        try:
            while True:
                data = up.recv(65536)
                if self.delay_s:
                    time.sleep(self.delay_s)
                if not data:
                    break
                client.sendall(data)
        except OSError:
            pass
        finally:
            try:
                # shutdown() (not just close()): the _c2s thread is blocked in recv() on this
                # socket, so close() alone would not release the descriptor or send the FIN.
                client.shutdown(socket.SHUT_RDWR)  # the delayed FIN
            except OSError:
                pass
            try:
                client.close()
            except OSError:
                pass
            try:
                up.close()
            except OSError:
                pass
