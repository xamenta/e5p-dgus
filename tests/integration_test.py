"""End-to-end test: the real service process, a pseudo-terminal standing in
for the screen, and a fake Moonraker (HTTP + websocket).
Run from the e5p-dgus folder:  python3 tests/integration_test.py
Needs pyserial + jinja2 (Linux only, uses a pty)."""
import os
import select
import subprocess
import sys
import tempfile
import time
import tty

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from e5p_dgus.dwin import parse_frames  # noqa: E402
from fake_moonraker import FakeMoonraker  # noqa: E402


def base_status():
    return {
        "toolhead": {"homed_axes": "", "extruder": "extruder"},
        "gcode_move": {"gcode_position": [0, 0, 0, 0], "homing_origin": [0, 0, 0, 0],
                       "speed_factor": 1.0, "extrude_factor": 1.0},
        "extruder": {"temperature": 22.0, "target": 0.0},
        "heater_bed": {"temperature": 21.0, "target": 0.0},
        "fan": {"speed": 0.0},
        "pause_resume": {"is_paused": False},
        "virtual_sdcard": {"file_position": 0, "progress": 0.0},
        "print_stats": {"state": "standby", "print_duration": 0.0},
        "display_status": {"progress": 0.0, "message": None},
        "idle_timeout": {"state": "Idle"},
        "bed_mesh": {"profile_name": "", "probed_matrix": [[0.0]]},
        "probe": {},
        "webhooks": {"state": "ready"},
    }


SETTINGS = {
    "stepper_x": {"position_max": 350.0}, "stepper_y": {"position_max": 350.0},
    "stepper_z": {"position_max": 400.0},
    "extruder": {"max_temp": 300, "min_temp": 0}, "heater_bed": {"max_temp": 130, "min_temp": 0},
    "bed_mesh": {"probe_count": "5,5"},
}


class Harness:
    def __init__(self, mr, extra_cfg=""):
        self.mr = mr
        self.master, slave = os.openpty()
        tty.setraw(self.master)
        tty.setraw(slave)
        tmp = tempfile.mkdtemp()
        cfg = os.path.join(tmp, "e5p_dgus.cfg")
        with open(cfg, "w") as f:
            f.write("[dgus]\nserial_port: %s\nmoonraker_url: http://127.0.0.1:%d\n"
                    "state_file: %s/state.json\nlog_file: %s/e5p.log\npoll_interval: 0.2\n%s"
                    % (os.ttyname(slave), mr.port, tmp, tmp, extra_cfg))
        self.log = os.path.join(tmp, "e5p.log")
        self.proc = subprocess.Popen([sys.executable, "-m", "e5p_dgus", "-c", cfg], cwd=ROOT,
                                     stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        self.buf, self.frames = bytearray(), []

    def pump(self, secs):
        end = time.time() + secs
        while time.time() < end:
            r, _, _ = select.select([self.master], [], [], 0.05)
            if r:
                self.buf.extend(os.read(self.master, 4096))
                for cmd, data in parse_frames(self.buf):
                    self.frames.append((time.time(), cmd, data))

    def wait_for(self, pred, secs):
        end = time.time() + secs
        while time.time() < end:
            self.pump(0.05)
            if pred():
                return True
        return False

    def writes(self, addr, since=0):
        a = bytes([addr >> 8, addr & 0xFF])
        return [(t, d[2:]) for t, c, d in self.frames if c == 0x82 and d[:2] == a and t >= since]

    def pages(self):
        return [d[3] for t, d in self.writes(0x0084)]

    def touch(self, addr, value):
        os.write(self.master, bytes([0x5A, 0xA5, 0x06, 0x83, addr >> 8, addr & 0xFF, 0x01,
                                     (value >> 8) & 0xFF, value & 0xFF]))

    def check(self, cond, what):
        print(("  PASS " if cond else "  FAIL ") + what)
        if not cond:
            self.stop()
            print(open(self.log).read()[-4000:])
            sys.exit(1)

    def stop(self):
        self.proc.terminate()
        try:
            self.proc.wait(5)
        except subprocess.TimeoutExpired:
            self.proc.kill()


def test_websocket():
    print("websocket mode")
    mr = FakeMoonraker(base_status(), SETTINGS).start()
    h = Harness(mr)
    try:
        h.check(h.wait_for(lambda: h.pages()[:2] == [0, 1], 8), "boot page, then home")
        h.check(mr.subscribed is not None and "extruder" in mr.subscribed, "subscribed to printer objects")
        h.check(h.writes(0x30FF)[-1][1] == b"\x00\x16", "hotend 22C on screen")

        t0 = time.time()
        mr.push_status({"extruder": {"temperature": 150.4}})
        ok = h.wait_for(lambda: any(d == b"\x00\x96" for t, d in h.writes(0x30FF, t0)), 2)
        latency = time.time() - t0
        h.check(ok and latency < 0.6, "pushed temp change shown in %.2f s" % latency)

        t1 = time.time()
        h.pump(3)
        h.check(len(h.writes(0x30FF, t1)) == 0, "unchanged temp not re-sent during 3 s")
        h.check(len(h.writes(0x0084, t1)) == 0, "no page writes while idle")

        h.touch(0x2010, 1)
        h.check(h.wait_for(lambda: any("TARGET=200" in s for s in mr.scripts), 2), "PLA preset touch -> G-code")

        t2 = time.time()
        mr.set_klippy("shutdown")
        h.check(h.wait_for(lambda: h.pages()[-1] == 250, 2), "kill page on shutdown in %.2f s" % (time.time() - t2))
        mr.set_klippy("ready")
        h.check(h.wait_for(lambda: h.pages()[-1] == 1, 6), "back to home after restart")

        mr.push_status({"print_stats": {"state": "printing"}, "display_status": {"progress": 0.25}})
        h.check(h.wait_for(lambda: h.pages()[-1] == 2, 2), "print started -> status page")
        h.check(h.wait_for(lambda: any(d == b"\x00\x19" for t, d in h.writes(0x30F6)), 2), "progress 25%")
        mr.push_status({"print_stats": {"state": "complete"}})
        h.check(h.wait_for(lambda: h.pages()[-1] == 4, 2), "print complete -> finished page")

        # Klipper disconnects and comes back
        mr.set_klippy("disconnected")
        h.check(h.wait_for(lambda: h.pages()[-1] == 250, 2), "kill page when Klipper disconnects")
        mr.set_klippy("ready")
        h.check(h.wait_for(lambda: h.pages()[-1] == 1, 6), "recovers after Klipper reconnects")

        # v1.2: STOP needs two presses; file list -> start print; restart
        h.touch(0x2040, 1)
        h.pump(0.5)
        h.check(not any(c[0] == "/printer/emergency_stop" for c in mr.calls), "single STOP press only arms")
        h.touch(0x2040, 1)
        h.check(h.wait_for(lambda: any(c[0] == "/printer/emergency_stop" for c in mr.calls), 2),
                "second STOP press -> emergency stop")
        h.touch(0x2000, 21)
        h.check(h.wait_for(lambda: h.pages()[-1] == 21, 2), "Print tile -> file list page")
        h.check(h.wait_for(lambda: any(d.startswith(b"benchy ") for t, d in h.writes(0x3200)), 2),
                "newest file shown in row 1")
        h.touch(0x2042, 0)
        h.check(h.wait_for(lambda: h.pages()[-1] == 22, 2), "tap file -> start print page")
        h.check(h.wait_for(lambda: any(d.startswith(b"Time: 1h") for t, d in h.writes(0x3340)), 2),
                "slicer details shown")
        h.touch(0x2044, 1)
        h.check(h.wait_for(lambda: ("/printer/print/start", {"filename": "benchy.gcode"}) in mr.calls, 2),
                "Print -> Moonraker print start")
        mr.set_klippy("shutdown")
        h.check(h.wait_for(lambda: h.pages()[-1] == 250, 2), "error page")
        h.touch(0x2041, 1)
        h.check(h.wait_for(lambda: any(c[0] == "/printer/firmware_restart" for c in mr.calls), 2),
                "Restart button works while shut down")
        mr.set_klippy("ready")
        h.check(h.wait_for(lambda: h.pages()[-1] == 1, 6), "home again after restart")

        # v1.3: M300 macro output on the console -> beep clip on the screen
        t3 = time.time()
        mr.notify("notify_gcode_response", ["// e5p_dgus:beep S=2000 P=100"])
        h.check(h.wait_for(lambda: any(d[0] == 14 for t, d in h.writes(0x00A0, t3)), 2),
                "M300 S2000 P100 -> beep clip 14")

        # Moonraker websocket drops -> reconnects by itself
        n = mr.ws_connections
        mr.drop_websockets()
        h.check(h.wait_for(lambda: mr.ws_connections > n, 6), "websocket reconnected")
        h.check(h.wait_for(lambda: h.pages()[-1] == 1, 6), "home page after reconnect")
    finally:
        h.stop()


def test_polling_fallback():
    print("polling fallback (websocket refused)")
    mr = FakeMoonraker(base_status(), SETTINGS, websocket=False).start()
    h = Harness(mr)
    try:
        h.check(h.wait_for(lambda: 1 in h.pages(), 20), "falls back to polling and reaches home")
        h.touch(0x2010, 3)
        h.check(h.wait_for(lambda: any("TARGET=240" in s for s in mr.scripts), 2), "PETG preset via polling")
        mr.status["print_stats"]["state"] = "printing"
        h.check(h.wait_for(lambda: h.pages()[-1] == 2, 2), "print status page via polling")
    finally:
        h.stop()


if __name__ == "__main__":
    test_websocket()
    test_polling_fallback()
    print("ALL PASSED")
