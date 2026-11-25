# Minimal asyncio WebSocket client (RFC 6455), stdlib only.
# Enough for Moonraker's JSON-RPC: text frames, ping/pong, close,
# fragmented messages. No TLS (Moonraker is local).
#
# Part of e5p-dgus. GPLv3.
import asyncio
import base64
import hashlib
import os
import struct
import urllib.parse

GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
OP_CONT, OP_TEXT, OP_BIN, OP_CLOSE, OP_PING, OP_PONG = 0x0, 0x1, 0x2, 0x8, 0x9, 0xA


class WebSocketClosed(Exception):
    pass


class WebSocket:
    def __init__(self, reader, writer):
        self.reader = reader
        self.writer = writer
        self.closed = False
        self._wlock = asyncio.Lock()

    @classmethod
    async def connect(cls, url, headers=None, timeout=10.0):
        u = urllib.parse.urlparse(url)
        if u.scheme not in ("ws", "http"):
            raise ValueError("only ws:// URLs are supported")
        host, port = u.hostname, u.port or 80
        path = u.path or "/"
        if u.query:
            path += "?" + u.query
        reader, writer = await asyncio.wait_for(asyncio.open_connection(host, port), timeout)
        key = base64.b64encode(os.urandom(16)).decode()
        lines = ["GET %s HTTP/1.1" % path, "Host: %s:%d" % (host, port),
                 "Upgrade: websocket", "Connection: Upgrade",
                 "Sec-WebSocket-Key: " + key, "Sec-WebSocket-Version: 13"]
        for k, v in (headers or {}).items():
            lines.append("%s: %s" % (k, v))
        writer.write(("\r\n".join(lines) + "\r\n\r\n").encode())
        await writer.drain()
        head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout)
        status, *hdrs = head.decode("latin-1").split("\r\n")
        if " 101 " not in status + " ":
            writer.close()
            raise ConnectionError("WebSocket handshake failed: %s" % status)
        expect = base64.b64encode(hashlib.sha1((key + GUID).encode()).digest()).decode()
        accept = ""
        for h in hdrs:
            if h.lower().startswith("sec-websocket-accept:"):
                accept = h.split(":", 1)[1].strip()
        if accept != expect:
            writer.close()
            raise ConnectionError("WebSocket handshake: bad accept key")
        return cls(reader, writer)

    async def _send_frame(self, opcode, payload):
        header = bytearray([0x80 | opcode])
        n = len(payload)
        if n < 126:
            header.append(0x80 | n)
        elif n < 65536:
            header.append(0x80 | 126)
            header += struct.pack(">H", n)
        else:
            header.append(0x80 | 127)
            header += struct.pack(">Q", n)
        mask = os.urandom(4)
        header += mask
        masked = bytes(b ^ mask[i & 3] for i, b in enumerate(payload))
        async with self._wlock:
            self.writer.write(bytes(header) + masked)
            await self.writer.drain()

    async def send(self, text):
        if self.closed:
            raise WebSocketClosed()
        await self._send_frame(OP_TEXT, text.encode())

    async def recv(self):
        """Return the next text message (str). Raises WebSocketClosed."""
        parts = []
        while True:
            try:
                b1, b2 = await self.reader.readexactly(2)
                fin, opcode = b1 & 0x80, b1 & 0x0F
                n = b2 & 0x7F
                if n == 126:
                    n = struct.unpack(">H", await self.reader.readexactly(2))[0]
                elif n == 127:
                    n = struct.unpack(">Q", await self.reader.readexactly(8))[0]
                mask = await self.reader.readexactly(4) if b2 & 0x80 else None
                payload = await self.reader.readexactly(n)
            except (asyncio.IncompleteReadError, ConnectionError, OSError):
                self.closed = True
                raise WebSocketClosed()
            if mask:
                payload = bytes(b ^ mask[i & 3] for i, b in enumerate(payload))
            if opcode == OP_PING:
                await self._send_frame(OP_PONG, payload)
                continue
            if opcode == OP_PONG:
                continue
            if opcode == OP_CLOSE:
                self.closed = True
                try:
                    await self._send_frame(OP_CLOSE, payload[:2])
                except Exception:
                    pass
                raise WebSocketClosed()
            parts.append(payload)
            if fin:
                return b"".join(parts).decode("utf-8", "replace")

    async def close(self):
        if not self.closed:
            self.closed = True
            try:
                await self._send_frame(OP_CLOSE, struct.pack(">H", 1000))
            except Exception:
                pass
        try:
            self.writer.close()
        except Exception:
            pass
