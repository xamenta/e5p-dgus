"""A tiny fake Moonraker (HTTP + websocket on one port) for tests."""
import asyncio
import base64
import hashlib
import json
import struct
import threading
import time
import urllib.parse

GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


class FakeMoonraker:
    def __init__(self, status, settings, websocket=True):
        self.status = status
        self.settings = settings
        self.websocket = websocket
        self.klippy = "ready"
        self.scripts = []
        self.calls = []
        self.files = [{"path": "benchy.gcode", "modified": 2, "size": 1000},
                      {"path": "cube.gcode", "modified": 1, "size": 500}]
        self.subscribed = None
        self.clients = []
        self.loop = None
        self.port = None
        self.ws_connections = 0

    # -- control from the test thread --------------------------------------
    def start(self):
        ready = threading.Event()

        def run():
            self.loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self.loop)
            srv = self.loop.run_until_complete(asyncio.start_server(self._client, "127.0.0.1", 0))
            self.port = srv.sockets[0].getsockname()[1]
            ready.set()
            self.loop.run_forever()

        threading.Thread(target=run, daemon=True).start()
        ready.wait(5)
        return self

    def call(self, fn, *args):
        self.loop.call_soon_threadsafe(fn, *args)

    def push_status(self, diff):
        """Update state and notify websocket subscribers (thread safe)."""
        def do():
            for k, v in diff.items():
                self.status.setdefault(k, {}).update(v)
            self._broadcast("notify_status_update", [diff, time.time()])
        self.call(do)

    def notify(self, method, params=None):
        self.call(self._broadcast, method, params or [])

    def set_klippy(self, state):
        def do():
            self.klippy = state
            if state == "shutdown":
                self._broadcast("notify_klippy_shutdown", [])
            elif state == "ready":
                self._broadcast("notify_klippy_ready", [])
            elif state == "disconnected":
                self._broadcast("notify_klippy_disconnected", [])
        self.call(do)

    # -- server ----------------------------------------------------------------
    def _broadcast(self, method, params):
        msg = json.dumps({"jsonrpc": "2.0", "method": method, "params": params})
        for w in list(self.clients):
            try:
                w.write(self._frame(msg.encode()))
            except Exception:
                self.clients.remove(w)

    @staticmethod
    def _frame(payload, opcode=1):
        n = len(payload)
        if n < 126:
            head = bytes([0x80 | opcode, n])
        elif n < 65536:
            head = bytes([0x80 | opcode, 126]) + struct.pack(">H", n)
        else:
            head = bytes([0x80 | opcode, 127]) + struct.pack(">Q", n)
        return head + payload

    def info(self):
        return {"klippy_connected": self.klippy != "disconnected", "klippy_state": self.klippy}

    def printer_info(self):
        return {"state": self.klippy, "software_version": "v0.12.0-fake",
                "state_message": "MCU 'mcu' shutdown: Timer too close\nOnce the underlying issue..."}

    def query(self, names):
        out = {}
        for name in names:
            if name == "configfile":
                out[name] = {"settings": self.settings, "save_config_pending_items": {}}
            elif name in self.status:
                out[name] = dict(self.status[name])
        return out

    async def _client(self, reader, writer):
        try:
            head = await reader.readuntil(b"\r\n\r\n")
        except Exception:
            writer.close()
            return
        lines = head.decode().split("\r\n")
        method, path, _ = lines[0].split(" ")
        headers = {l.split(":", 1)[0].lower(): l.split(":", 1)[1].strip() for l in lines[1:] if ":" in l}
        if headers.get("upgrade", "").lower() == "websocket":
            if not self.websocket:
                writer.write(b"HTTP/1.1 404 Not Found\r\nContent-Length: 0\r\n\r\n")
                await writer.drain()
                writer.close()
                return
            return await self._ws(reader, writer, headers)
        body = b""
        if "content-length" in headers:
            body = await reader.readexactly(int(headers["content-length"]))
        u = urllib.parse.urlparse(path)
        q = urllib.parse.parse_qs(u.query, keep_blank_values=True)
        code, result = 200, None
        if u.path == "/server/info":
            result = self.info()
        elif u.path == "/printer/info":
            result = self.printer_info()
        elif u.path == "/printer/objects/list":
            result = {"objects": list(self.status) + ["configfile"]}
        elif u.path == "/printer/objects/query":
            result = {"eventtime": time.time(), "status": self.query(q)}
        elif u.path == "/server/gcode_store":
            result = {"gcode_store": []}
        elif u.path == "/printer/gcode/script":
            self.scripts.append(json.loads(body)["script"])
            result = "ok"
        elif u.path == "/server/files/list":
            result = self.files
        elif u.path == "/server/files/metadata":
            result = {"estimated_time": 3600, "filament_total": 1000.0, "slicer": "OrcaSlicer"}
        elif u.path in ("/printer/print/start", "/printer/emergency_stop",
                        "/printer/firmware_restart", "/machine/services/restart"):
            self.calls.append((u.path, json.loads(body) if body else None))
            result = "ok"
        else:
            code = 404
        payload = json.dumps({"result": result} if code == 200 else {"error": {"message": "nf"}}).encode()
        writer.write(b"HTTP/1.1 %d OK\r\nContent-Type: application/json\r\nContent-Length: %d\r\n"
                     b"Connection: close\r\n\r\n" % (code, len(payload)) + payload)
        await writer.drain()
        writer.close()

    async def _ws(self, reader, writer, headers):
        accept = base64.b64encode(hashlib.sha1((headers["sec-websocket-key"] + GUID).encode()).digest())
        writer.write(b"HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                     b"Sec-WebSocket-Accept: " + accept + b"\r\n\r\n")
        await writer.drain()
        self.clients.append(writer)
        self.ws_connections += 1
        try:
            while True:
                b1, b2 = await reader.readexactly(2)
                n = b2 & 0x7F
                if n == 126:
                    n = struct.unpack(">H", await reader.readexactly(2))[0]
                elif n == 127:
                    n = struct.unpack(">Q", await reader.readexactly(8))[0]
                mask = await reader.readexactly(4)
                data = bytes(b ^ mask[i & 3] for i, b in enumerate(await reader.readexactly(n)))
                if b1 & 0x0F == 8:
                    break
                req = json.loads(data)
                m, p = req["method"], req.get("params") or {}
                if m == "server.info":
                    res = self.info()
                elif m == "printer.info":
                    res = self.printer_info()
                elif m == "printer.objects.list":
                    res = {"objects": list(self.status) + ["configfile"]}
                elif m == "printer.objects.subscribe":
                    self.subscribed = p["objects"]
                    res = {"eventtime": time.time(), "status": self.query(p["objects"])}
                elif m == "server.connection.identify":
                    res = {"connection_id": 1}
                else:
                    res = "ok"
                writer.write(self._frame(json.dumps({"jsonrpc": "2.0", "id": req["id"], "result": res}).encode()))
                await writer.drain()
        except (asyncio.IncompleteReadError, ConnectionError):
            pass
        finally:
            if writer in self.clients:
                self.clients.remove(writer)
            writer.close()

    def drop_websockets(self):
        def do():
            for w in list(self.clients):
                w.close()
            self.clients.clear()
        self.call(do)
