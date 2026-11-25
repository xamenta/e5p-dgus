# DWIN T5UID1 (DGUS) serial transport
#
# Frame format: 0x5A 0xA5 <len> <cmd> <data...>   (len counts cmd + data)
#   0x82 = write variable(s):  addr_hi addr_lo data...
#   0x83 = read variable(s):   addr_hi addr_lo word_count   (and the reply /
#                              touch auto-upload: addr_hi addr_lo word_count data...)
#
# Part of e5p-dgus, a host-side port of Desuuuu's DGUS-reloaded Klipper
# integration. GPLv3.
import logging
import queue
import threading
import time

HEADER = b"\x5a\xa5"
CMD_WRITEVAR = 0x82
CMD_READVAR = 0x83

log = logging.getLogger("e5p_dgus.dwin")


class DwinSerial:
    """Talks to the screen over a UART. Reads run in a background thread and
    are handed to `on_frame(cmd, data)`; writes are queued and paced so the
    screen never gets flooded (the original Klipper MCU code spaced commands
    20 ms apart)."""

    def __init__(self, port, baud, on_frame, cmd_delay=0.02):
        import serial  # pyserial (apt: python3-serial)
        self._serial_mod = serial
        self.port = port
        self.baud = baud
        self.on_frame = on_frame
        self.cmd_delay = cmd_delay
        self._ser = None
        self._writes = queue.Queue()
        self._stop = threading.Event()
        self._threads = []

    # -- lifecycle ---------------------------------------------------------
    def start(self):
        self._open()
        for target, name in ((self._reader, "dwin-rx"), (self._writer, "dwin-tx")):
            t = threading.Thread(target=target, name=name, daemon=True)
            t.start()
            self._threads.append(t)

    def stop(self):
        self._stop.set()
        self._writes.put(None)
        try:
            if self._ser:
                self._ser.close()
        except Exception:
            pass

    def _open(self):
        while not self._stop.is_set():
            try:
                self._ser = self._serial_mod.Serial(self.port, self.baud, timeout=0.1)
                log.info("Opened %s @ %d baud", self.port, self.baud)
                return
            except Exception as e:
                log.error("Cannot open %s (%s), retrying in 5s", self.port, e)
                time.sleep(5)

    # -- public API ----------------------------------------------------------
    def send(self, cmd, data):
        data = bytes(data)
        if len(data) + 1 > 0xFF:
            raise ValueError("frame too long")
        frame = HEADER + bytes([len(data) + 1, cmd]) + data
        self._writes.put(frame)

    # -- threads -------------------------------------------------------------
    def _writer(self):
        last = 0.0
        while not self._stop.is_set():
            frame = self._writes.get()
            if frame is None:
                return
            wait = self.cmd_delay - (time.monotonic() - last)
            if wait > 0:
                time.sleep(wait)
            try:
                self._ser.write(frame)
            except Exception as e:
                log.error("Serial write failed: %s", e)
                time.sleep(1)
            last = time.monotonic()

    def _reader(self):
        buf = bytearray()
        while not self._stop.is_set():
            try:
                chunk = self._ser.read(64)
            except Exception as e:
                if self._stop.is_set():
                    return
                log.error("Serial read failed: %s; reopening", e)
                time.sleep(1)
                try:
                    self._ser.close()
                except Exception:
                    pass
                self._open()
                continue
            if not chunk:
                continue
            buf.extend(chunk)
            for cmd, data in parse_frames(buf):
                try:
                    self.on_frame(cmd, data)
                except Exception:
                    log.exception("Frame handler failed")


def parse_frames(buf):
    """Extract complete frames from `buf` (a bytearray, modified in place)."""
    frames = []
    while True:
        idx = buf.find(HEADER)
        if idx < 0:
            # keep a trailing 0x5A in case the header is split
            del buf[:-1 if buf[-1:] == b"\x5a" else len(buf)]
            return frames
        if idx:
            del buf[:idx]
        if len(buf) < 4:
            return frames
        length = buf[2]
        if length < 1:
            del buf[:2]
            continue
        if len(buf) < 3 + length:
            return frames
        cmd = buf[3]
        data = bytes(buf[4:3 + length])
        del buf[:3 + length]
        frames.append((cmd, data))
