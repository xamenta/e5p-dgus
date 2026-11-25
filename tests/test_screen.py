"""Offline tests: fake Moonraker + fake serial transport.
Run: python3 -m unittest discover -s tests -v   (from the e5p-dgus folder)"""
import asyncio
import configparser
import os
import struct
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from e5p_dgus import dwin  # noqa: E402
from e5p_dgus.runtime import Screen, ADDR_PAGE  # noqa: E402

SETTINGS = {
    "stepper_x": {"position_min": -5.0, "position_max": 350.0},
    "stepper_y": {"position_min": 0.0, "position_max": 350.0},
    "stepper_z": {"position_min": -3.0, "position_max": 400.0},
    "extruder": {"min_temp": 0, "max_temp": 300, "min_extrude_temp": 170,
                 "max_extrude_only_distance": 100.0, "pid_kp": 22.2, "pid_ki": 1.08, "pid_kd": 114.0},
    "heater_bed": {"min_temp": 0, "max_temp": 130, "pid_kp": 70.0, "pid_ki": 1.5, "pid_kd": 900.0},
    "bed_mesh": {"probe_count": [7, 7]},
    "probe": {"z_offset": 1.5},
}


def status(**over):
    s = {
        "toolhead": {"homed_axes": "xyz", "extruder": "extruder"},
        "gcode_move": {"gcode_position": [10.0, 20.0, 5.0, 0.0], "homing_origin": [0, 0, -0.05, 0],
                       "speed_factor": 1.0, "extrude_factor": 1.0},
        "extruder": {"temperature": 201.4, "target": 200.0},
        "heater_bed": {"temperature": 59.6, "target": 60.0},
        "fan": {"speed": 0.5},
        "pause_resume": {"is_paused": False},
        "virtual_sdcard": {"file_position": 0, "progress": 0.0},
        "print_stats": {"state": "standby", "print_duration": 0.0},
        "display_status": {"progress": 0.0, "message": None},
        "idle_timeout": {"state": "Idle"},
        "bed_mesh": {"profile_name": "default",
                     "probed_matrix": [[round(0.01 * (r * 7 + c), 3) for c in range(7)] for r in range(7)]},
        "probe": {},
        "stepper_enable": {"steppers": {"stepper_x": True, "stepper_y": True, "stepper_z": True}},
        "webhooks": {"state": "ready"},
    }
    for k, v in over.items():
        s.setdefault(k, {}).update(v)
    return s


class FakeTransport:
    def __init__(self):
        self.frames = []

    def send(self, cmd, data):
        self.frames.append((cmd, bytes(data)))

    def writes(self, address):
        return [d[2:] for c, d in self.frames
                if c == dwin.CMD_WRITEVAR and (d[0] << 8 | d[1]) == address]

    def pages(self):
        return [d[3] for d in self.writes(ADDR_PAGE)]


class FakeMoonraker:
    def __init__(self):
        self.scripts = []
        self.fail = None

    calls = None
    files = [
        {"path": "old_part.gcode", "modified": 100, "size": 10},
        {"path": "benchy.gcode", "modified": 300, "size": 10},
        {"path": "notes.txt", "modified": 400, "size": 1},
        {"path": "sub/bracket_v2_really_long_name_for_testing_truncation_ok.gcode", "modified": 200, "size": 10},
    ] + [{"path": "f%02d.gcode" % i, "modified": i, "size": 1} for i in range(6)]

    def _call(self, name, *args):
        if self.calls is None:
            self.calls = []
        self.calls.append((name,) + args)
        if self.fail:
            raise Exception(self.fail)

    def files_list(self):
        self._call("files_list")
        return self.files

    def file_metadata(self, filename):
        self._call("file_metadata", filename)
        return {"estimated_time": 8040, "filament_total": 23456.0, "layer_height": 0.2,
                "object_height": 48.0, "filament_type": "PETG", "first_layer_extr_temp": 240,
                "first_layer_bed_temp": 75, "slicer": "OrcaSlicer"}

    def print_start(self, filename):
        self._call("print_start", filename)

    def emergency_stop(self):
        self._call("emergency_stop")

    def firmware_restart(self):
        self._call("firmware_restart")

    def restart_service(self, service="klipper"):
        self._call("restart_service", service)

    def gcode(self, script):
        self.scripts.append(script)
        time.sleep(0.01)
        if self.fail:
            raise Exception(self.fail)
        return "ok"


def make_screen(tmp):
    p = configparser.ConfigParser()
    p.read_dict({"dgus": {"state_file": os.path.join(tmp, "state.json"), "update_interval": "100"}})
    mr = FakeMoonraker()
    tr = FakeTransport()
    sc = Screen(p["dgus"], mr, tr)
    return sc, mr, tr


def touch(sc, address, value, fmt=">H"):
    payload = struct.pack(fmt, value) if fmt else b"\x00\x00"
    words = len(payload) // 2
    sc._handle_frame(dwin.CMD_READVAR, bytes([address >> 8, address & 0xFF, words]) + payload)


class ScreenTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)
        self.sc, self.mr, self.tr = make_screen(self.tmp)
        self.sc.start(self.loop)
        self.sc.on_status(status())
        self.sc.on_ready(list(status().keys()) + ["configfile"], SETTINGS, {}, "v0.12.0-test")

    def tearDown(self):
        self.sc.stop()
        self.spin(0.01)
        self.loop.close()

    def spin(self, secs=0.2):
        self.loop.run_until_complete(asyncio.sleep(secs))

    def test_frame_parser(self):
        buf = bytearray(b"\x00\x5a\xa5\x06\x83\x20\x00\x01\x00\x05\x5a")
        frames = dwin.parse_frames(buf)
        self.assertEqual(frames, [(0x83, b"\x20\x00\x01\x00\x05")])
        self.assertEqual(bytes(buf), b"\x5a")
        buf.extend(b"\xa5\x03\x82\x4f\x4b")
        self.assertEqual(dwin.parse_frames(buf), [(0x82, b"OK")])

    def test_boot_then_home(self):
        self.assertEqual(self.tr.pages(), [0])
        self.sc.routines["__boot"]._timer_run()
        self.assertEqual(self.sc.current_page, "home")

    def test_every_page_renders(self):
        for name in self.sc.pages:
            for var in self.sc.pages[name].var + self.sc.pages[name].var_auto:
                self.sc.send_var(var)  # raises on any template error

    def test_temps_written(self):
        self.sc.switch_page("home")
        self.assertEqual(struct.unpack(">h", self.tr.writes(0x30FF)[-1])[0], 201)
        self.assertEqual(struct.unpack(">h", self.tr.writes(0x30FD)[-1])[0], 60)

    def test_mesh_resampled_to_5x5(self):
        self.sc.send_var("level_auto_grid")
        vals = struct.unpack(">25h", self.tr.writes(0x3109)[-1])
        # corners of the 7x7 mesh: 0.00, 0.06, 0.42, 0.48
        self.assertEqual((vals[0], vals[4], vals[20], vals[24]), (0, 60, 420, 480))

    def test_preset_and_cool(self):
        self.sc.switch_page("temp_menu")
        touch(self.sc, 0x2010, 3)  # PETG
        touch(self.sc, 0x2014, 0xFFFE, ">H")  # -2 = all heaters off (int16 on wire)
        self.spin()
        joined = "\n".join(self.mr.scripts)
        self.assertIn("TARGET=240", joined)
        self.assertIn("HEATER=heater_bed TARGET=75", joined)
        self.assertIn("HEATER=extruder TARGET=0", joined)

    def test_temp_clamped(self):
        touch(self.sc, 0x2012, 999, ">h")
        self.spin()
        self.assertIn("TARGET=285", self.mr.scripts[-1])

    def test_move_step_clamped(self):
        self.sc.set_variable("move_steps", 0)
        touch(self.sc, 0x2023, 1)  # x-
        self.spin()
        self.assertIn("G1 X0.0 F4800", self.mr.scripts[-1])

    def test_move_page_blocked_when_busy(self):
        self.sc.on_status({"idle_timeout": {"state": "Printing"}})
        touch(self.sc, 0x2000, 8)  # switch to move page
        self.assertNotEqual(self.sc.current_page, "move")
        self.assertEqual(self.sc.get_variable("message"), "Busy")

    def test_home_shows_wait_then_returns(self):
        self.sc.switch_page("home")
        touch(self.sc, 0x201F, 0)
        self.spin(0.05)
        self.assertEqual(self.sc.current_page, "wait")
        self.assertTrue(self.sc.is_busy())
        self.spin(2.5)
        self.assertEqual(self.sc.current_page, "home")
        self.assertIn("G28 X Y Z", self.mr.scripts[0])

    def test_manual_level_center_fixed(self):
        touch(self.sc, 0x2019, 1)
        self.spin()
        # x: (-5+30 + 350-30)/2 = 172.5 ; y: (0+30+350-30)/2 = 175
        self.assertIn("G1 X172.5 Y175.0", self.mr.scripts[-1])

    def test_save_applies_z_offset(self):
        touch(self.sc, 0x2030, 0, None)
        self.spin()
        s = self.mr.scripts[-1]
        self.assertTrue(s.index("Z_OFFSET_APPLY_PROBE") < s.index("SAVE_CONFIG"))

    def test_print_start_and_end(self):
        self.sc.switch_page("home")
        self.sc.on_status({"print_stats": {"state": "printing", "print_duration": 3725},
                           "display_status": {"progress": 0.42}})
        self.spin()
        self.assertEqual(self.sc.current_page, "print_status")
        self.assertEqual(struct.unpack(">H", self.tr.writes(0x30F6)[-1])[0], 42)
        self.assertTrue(self.tr.writes(0x30E7)[-1].startswith(b"1h 2m 5s"))
        self.sc.on_status({"print_stats": {"state": "complete"}})
        self.spin()
        self.assertEqual(self.sc.current_page, "print_finished")

    def test_pause_button(self):
        self.sc.on_status({"print_stats": {"state": "printing"}, "virtual_sdcard": {"file_position": 100}})
        self.spin()
        touch(self.sc, 0x2008, 1)
        self.spin()
        self.assertEqual(self.mr.scripts[-1], "PAUSE")

    def test_m117_message(self):
        self.sc.on_status({"display_status": {"message": "Heating bed"}})
        self.assertTrue(self.tr.writes(0x3000)[-1].strip(b"\x00").endswith(b"Heating bed"))

    def test_gcode_error_shown(self):
        self.mr.fail = "Must home axis first: 10.000 0.000 5.000 [0.000]"
        touch(self.sc, 0x2022, 50, ">h")
        self.spin()
        self.assertTrue(self.sc.get_variable("message").startswith("Must home axis first"))

    def test_shutdown_and_recover(self):
        self.sc.on_not_ready("MCU 'mcu' shutdown: Timer too close")
        self.assertEqual(self.sc.current_page, "kill")
        self.assertIn("Timer too close", " ".join(self.sc.get_variable("line%d" % i) for i in range(1, 5)))
        touch(self.sc, 0x201F, 0)  # ignored while not ready
        self.spin()
        self.assertEqual(self.mr.scripts, [])
        self.sc.on_ready(list(status().keys()), SETTINGS, {}, "v")
        self.assertEqual(self.sc.current_page, "boot")

    def test_probing_progress(self):
        self.sc._inflight = 1
        self.sc.switch_page("leveling_probing")
        now = time.time() + 1
        self.sc.on_gcode_store([{"time": now, "message": "probe at 1,1 is z=0.1"}] * 49)
        self.assertEqual(bin(self.sc.probed_matrix()).count("1"), 25)

    def test_volume_persisted(self):
        touch(self.sc, 0x4022, 40)
        self.assertEqual(self.sc.volume, 40)
        sc2, _, _ = make_screen(self.tmp)
        self.assertEqual(sc2.volume, 40)

    def test_pid_pending_values(self):
        self.sc.pending_config = {"extruder": {"pid_kp": "25.5"}}
        self.sc.switch_page("pid")
        self.assertEqual(struct.unpack(">i", self.tr.writes(0x312D)[-1])[0], 2550)

    def test_unchanged_values_not_resent(self):
        self.sc.switch_page("home")
        n = len(self.tr.frames)
        self.sc.send_page_vars(complete=False)
        self.assertEqual(len(self.tr.frames), n)          # nothing changed
        self.sc.on_status({"extruder": {"temperature": 180.0}})
        self.sc.send_page_vars(complete=False)
        self.assertEqual(len(self.tr.frames), n + 1)      # only the hotend temp
        self.assertEqual(self.tr.frames[-1][1][:2], b"\x30\xff")

    def test_control_enable_cached(self):
        self.sc.switch_page("print_status")
        n = len(self.tr.writes(0x00B0))
        self.sc.send_page_vars(complete=False)
        self.assertEqual(len(self.tr.writes(0x00B0)), n)  # pause/resume state unchanged
        self.sc.on_status({"print_stats": {"state": "printing"}})
        self.sc.send_page_vars(complete=False)
        self.assertGreater(len(self.tr.writes(0x00B0)), n)

    def test_touch_invalidates_cached_value(self):
        self.sc.switch_page("fan")                       # fan speed 50 written to 0x4000
        touch(self.sc, 0x4000, 80)                       # screen slider now shows 80
        self.mr.fail = "printer busy"                    # ...but the command fails
        self.spin()
        n = len(self.tr.writes(0x4000))
        self.sc.send_page_vars(complete=False)
        self.assertEqual(len(self.tr.writes(0x4000)), n + 1)  # 50 rewritten, screen corrected

    def test_page_resync(self):
        self.sc.switch_page("home")
        n = len(self.tr.pages())
        self.sc._handle_frame(dwin.CMD_READVAR, bytes([0x00, 0x14, 0x01, 0x00, 0x00]))  # shows boot page
        self.assertEqual(self.tr.pages()[n:], [1])
        self.sc._handle_frame(dwin.CMD_READVAR, bytes([0x00, 0x14, 0x01, 0x00, 0xCA]))  # popup 202: fine
        self.assertEqual(len(self.tr.pages()), n + 1)

    def test_probing_from_console_lines(self):
        self.sc._inflight = 1  # BED_MESH_CALIBRATE running
        self.sc.switch_page("leveling_probing")
        for _ in range(49):
            self.sc.on_gcode_line("// probe at 10.000,10.000 is z=0.120")
        self.sc.on_gcode_line("Mesh Bed Leveling Complete")
        self.assertEqual(bin(self.sc.probed_matrix()).count("1"), 25)

    def test_websocket_url(self):
        from e5p_dgus.live import ws_url
        self.assertEqual(ws_url("http://127.0.0.1:7125"), "ws://127.0.0.1:7125/websocket")
        self.assertEqual(ws_url("http://printer.local/"), "ws://printer.local/websocket")

    # -- v1.2 features ------------------------------------------------------
    def names(self):
        return [c[0] for c in (self.mr.calls or [])]

    def test_tpu_preset(self):
        touch(self.sc, 0x2010, 4)
        self.spin()
        self.assertIn("TARGET=225", self.mr.scripts[-1])
        self.assertIn("HEATER=heater_bed TARGET=50", self.mr.scripts[-1])

    def test_estop_needs_two_presses(self):
        touch(self.sc, 0x2040, 1)
        self.spin()
        self.assertNotIn("emergency_stop", self.names())
        self.assertIn("again", self.sc.get_variable("message"))
        touch(self.sc, 0x2040, 1)
        self.spin()
        self.assertIn("emergency_stop", self.names())

    def test_estop_press_expires(self):
        touch(self.sc, 0x2040, 1)
        self.sc._estop_armed -= 5   # pretend 5 s passed
        touch(self.sc, 0x2040, 1)
        self.spin()
        self.assertNotIn("emergency_stop", self.names())

    def test_restart_works_while_shut_down(self):
        self.sc.on_not_ready("MCU 'mcu' shutdown: Timer too close")
        touch(self.sc, 0x2041, 1)
        self.spin()
        self.assertEqual(self.names(), ["firmware_restart"])
        self.assertEqual(self.sc.get_variable("line4"), "Restarting...")

    def test_restart_falls_back_to_service_restart(self):
        self.sc.on_not_ready("Klipper not connected")
        self.mr.fail = "Klippy Disconnected"
        touch(self.sc, 0x2041, 1)
        self.spin()
        self.assertEqual(self.names(), ["firmware_restart", "restart_service"])

    def test_file_browser(self):
        self.sc.switch_page("home")
        touch(self.sc, 0x2000, 21)
        self.spin()
        self.assertEqual(self.sc.current_page, "files")
        rows = [self.sc.get_variable("file_row%d" % i) for i in range(5)]
        self.assertEqual(rows[0], "benchy")                       # newest first, no .txt
        self.assertTrue(rows[1].startswith("sub/bracket_v2") and rows[1].endswith(".."))
        self.assertEqual(len(rows[1]), 38)
        self.assertEqual(self.sc.get_variable("file_page"), "1/2")
        written = self.tr.writes(0x3200)[-1]
        self.assertEqual(len(written), 38)
        self.assertTrue(written.startswith(b"benchy "))           # space padded
        touch(self.sc, 0x2043, 1)                                 # next page
        self.assertEqual(self.sc.get_variable("file_page"), "2/2")
        touch(self.sc, 0x2043, 1)                                 # no page 3
        self.assertEqual(self.sc.get_variable("file_page"), "2/2")
        touch(self.sc, 0x2043, 0)
        touch(self.sc, 0x2042, 0)                                 # tap benchy
        self.spin()
        self.assertEqual(self.sc.current_page, "print_confirm")
        self.assertEqual(self.sc.get_variable("confirm_name1"), "benchy")
        self.assertEqual(self.sc.get_variable("confirm_info1"), "Time: 2h 14m 0s   Filament: 23.5 m")
        self.assertIn("PETG", self.sc.get_variable("confirm_info3"))
        touch(self.sc, 0x2044, 1)                                 # Print
        self.spin()
        self.assertIn(("print_start", "benchy.gcode"), self.mr.calls)

    def test_print_refused_while_printing(self):
        touch(self.sc, 0x2000, 21)
        self.spin()
        touch(self.sc, 0x2042, 0)
        self.spin()
        self.sc.on_status({"print_stats": {"state": "printing"}})
        self.spin()
        self.mr.calls = []
        touch(self.sc, 0x2044, 1)
        self.spin()
        self.assertNotIn("print_start", self.names())

    def test_empty_row_tap_ignored(self):
        self.mr.files = self.mr.files[:2]
        touch(self.sc, 0x2000, 21)
        self.spin()
        touch(self.sc, 0x2042, 4)
        self.spin()
        self.assertEqual(self.sc.current_page, "files")

    # -- v1.3 beeps ---------------------------------------------------------
    def sounds(self):
        return [d[0] for d in self.tr.writes(0x00A0)]

    def test_m300_short_and_long(self):
        n = len(self.sounds())
        self.sc.on_gcode_line("// e5p_dgus:beep S=440 P=100")
        self.sc.on_gcode_line("// e5p_dgus:beep S=1100 P=300")
        self.sc.on_gcode_line("// e5p_dgus:beep S=2600 P=50")
        self.sc.on_gcode_line("// e5p_dgus:beep S=9000 P=250")
        self.assertEqual(self.sounds()[n:], [10, 13, 16, 17])

    def test_m300_long_repeats(self):
        n = len(self.sounds())
        self.sc.on_gcode_line("// e5p_dgus:beep S=1000 P=1500")
        self.spin(1.2)
        self.assertEqual(self.sounds()[n:], [13, 13, 13])

    def test_screen_sound_and_volume(self):
        self.sc.on_gcode_line("// e5p_dgus:sound 2 V=50")
        frame = self.tr.writes(0x00A0)[-1]
        self.assertEqual((frame[0], frame[1], frame[2]), (2, 1, 127))

    def test_beep_without_clips_uses_notification(self):
        self.sc.beep_clips = False
        self.sc.on_gcode_line("// e5p_dgus:beep S=440 P=100")
        self.assertEqual(self.sounds()[-1], 3)

    def test_other_console_lines_ignored(self):
        n = len(self.tr.frames)
        self.sc.on_gcode_line("// Klipper state: Ready")
        self.sc.on_gcode_line("echo: M300 not a real beep")
        self.assertEqual(len(self.tr.frames), n)

    def test_volume_set_on_ready_before_boot_sound(self):
        ws = [(c, d[:2]) for c, d in self.tr.frames if c == 0x82 and d[:2] in (b"\x00\xa1", b"\x00\xa0")]
        self.assertEqual(ws[0][1], b"\x00\xa1")   # master volume first
        self.assertEqual(ws[1][1], b"\x00\xa0")   # then the boot chime

    def test_startup_message_then_alarm(self):
        self.assertEqual(self.sc.moonraker_down_message(), "Starting up...")
        self.sc.started -= 120
        self.assertEqual(self.sc.moonraker_down_message(), "Moonraker not reachable")


if __name__ == "__main__":
    unittest.main()
