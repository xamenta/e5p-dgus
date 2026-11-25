# Screen runtime: a host-side port of Desuuuu's klippy/extras/t5uid1.
#
# The original ran inside Klipper and talked to the screen through the MCU.
# This version runs as its own service on the Pi, reads printer state from
# Moonraker and sends G-code through Moonraker, so mainline Klipper is used
# unchanged. The page/var/routine definitions (screen/*.cfg) are Desuuuu's
# DGUS-reloaded files with a few fixes, so they match the DWIN_SET firmware.
#
# Original work Copyright (C) 2020 Desuuuu <contact@desuuuu.com>
# Port and changes Copyright (C) 2026. GPLv3.
import asyncio
import collections
import configparser
import json
import logging
import math
import os
import re
import struct
import textwrap
import time

import jinja2

from . import dwin

log = logging.getLogger("e5p_dgus")

ADDR_VERSION = 0x0F
ADDR_PAGE_NOW = 0x14  # DGUS II: id of the page currently shown
ADDR_BRIGHTNESS = 0x82
ADDR_PAGE = 0x84
ADDR_SOUND = 0xA0
ADDR_VOLUME = 0xA1
ADDR_CONTROL = 0xB0

CONTROL_TYPES = {
    "variable_data_input": 0x00,
    "popup_window": 0x01,
    "incremental_adjust": 0x02,
    "slider_adjust": 0x03,
    "rtc_settings": 0x04,
    "return_key_code": 0x05,
    "text_input": 0x06,
    "firmware_settings": 0x07,
}

TYPES_LEN = {"int16": 2, "uint16": 2, "int32": 4, "uint32": 4, "float": 4}
TYPES_FMT = {"int16": ">h", "uint16": ">H", "int32": ">i", "uint32": ">I", "float": ">f"}

# M300 beeps: the e5p_dgus_macros.cfg M300 macro prints "e5p_dgus:beep S=.. P=.."
BEEP_RE = re.compile(r"e5p_dgus:beep\s+S=(\d+)\s+P=(\d+)", re.I)
SOUND_RE = re.compile(r"e5p_dgus:sound\s+(\d+)(?:\s+V=(\d+))?", re.I)
BEEP_PITCHES = (500, 1000, 2000, 3000)   # screen sound slots 10..17 (tools/make_beeps.py)
BEEP_FIRST_ID = 10
BEEP_LONG_SECS = 0.45

# Inputs accepted while Klipper isn't ready (Restart button on the error page)
ALWAYS_ALLOWED = {0x2041}
GCODE_EXT = (".gcode", ".g", ".gco", ".bgcode")
FILES_PER_PAGE = 5

# Command registers: every write is an action, so never skip "duplicates"
NO_CACHE = (ADDR_PAGE, ADDR_SOUND, ADDR_CONTROL)

ROUTINE_TRIGGERS = ("enter_pre", "enter", "leave", "manual")
ABORT_PAGE_SWITCH = "DGUS_ABORT_PAGE_SWITCH"

CONTROLS = {
    "pause": 1, "resume": 2,      # print_status (popup_window)
    "disable": 5,                 # leveling_automatic (return_key_code)
    "extra2": 4,                  # settings_menu2 (return_key_code)
    "abort": 1, "continue": 2,    # wait
}

BASE_CONSTANTS = {
    "popup_confirmed": 1,
    "adjust_increment": 0, "adjust_decrement": 1,
    "preset_pla": 1, "preset_abs": 2, "preset_petg": 3, "preset_tpu": 4,
    "extruder_current": -1, "extruder_e0": 0, "extruder_e1": 1,
    "heater_all": -2, "heater_bed": -1, "heater_h0": 0, "heater_h1": 1,
    "stepper_enable": 1, "stepper_disable": 2,
    "step_size_10": 0, "step_size_1": 1, "step_size_0.1": 2, "step_size_0.01": 3,
    "filament_retract": 0, "filament_extrude": 1,
    "axis_xyz": 0, "axis_xy": 1, "axis_z": 2,
    "move_x+": 0, "move_x-": 1, "move_y+": 2, "move_y-": 3, "move_z+": 4, "move_z-": 5,
    "extra_button1": 0, "extra_button2": 1,
    "disabled": 0, "enabled": 1,
    "status_icon_pause": 1 << 0, "status_icon_resume": 1 << 1,
    "step_icon_10": 1 << 0, "step_icon_1": 1 << 1,
    "step_icon_0.1": 1 << 2, "step_icon_0.01": 1 << 3,
    "extruder_icon_e0": 1 << 0, "extruder_icon_e1": 1 << 1,
    "heater_icon_bed": 1 << 0, "heater_icon_h0": 1 << 1, "heater_icon_h1": 1 << 2,
    "wait_icon_abort": 1 << 0, "wait_icon_continue": 1 << 1,
}

Coord = collections.namedtuple("Coord", ("x", "y", "z", "e"))


def map_value_range(x, in_min, in_max, out_min, out_max):
    return int(round((x - in_min) * (out_max - out_min) // (in_max - in_min) + out_min))


def get_duration(seconds):
    seconds = max(0, int(seconds))
    minutes, seconds = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    days, hours = divmod(hours, 24)
    days %= 365
    result = "%ds" % seconds
    if minutes:
        result = "%dm %s" % (minutes, result)
    if hours:
        result = "%dh %s" % (hours, result)
    if days:
        result = "%dd %s" % (days, result)
    return result


def _to_float(v, default=0.0):
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _parse_count(v, default=(3, 3)):
    """bed_mesh probe_count may arrive as '5,5', '5', [5, 5] or 5."""
    if v is None:
        return default
    if isinstance(v, (int, float)):
        return int(v), int(v)
    if isinstance(v, str):
        parts = [p for p in v.replace(" ", "").split(",") if p]
    else:
        parts = list(v)
    try:
        nums = [int(float(p)) for p in parts]
    except (TypeError, ValueError):
        return default
    if len(nums) == 1:
        return nums[0], nums[0]
    return nums[0], nums[1]


def resample(seq, n):
    """Pick n evenly spaced items from seq (keeps first and last)."""
    if len(seq) <= n:
        return list(seq)
    return [seq[round(i * (len(seq) - 1) / (n - 1))] for i in range(n)]


# ---------------------------------------------------------------------------
# Definitions loaded from screen/*.cfg
# ---------------------------------------------------------------------------
class Template:
    def __init__(self, env, name, script):
        self.name = name
        try:
            self.template = env.from_string(script)
        except Exception as e:
            raise ValueError("Error compiling template %s: %s" % (name, e))

    def render(self, context):
        return self.template.render(context)


class Var:
    def __init__(self, env, name, sec):
        self.name = name
        self.type = sec.get("type")
        if self.type not in ("input", "output"):
            raise ValueError("var %s: bad type" % name)
        self.address = int(sec.get("address"), 16)
        dtype = sec.get("data_type")
        self.array_len = 1
        if dtype.startswith("array[") and dtype.endswith("]") and self.type == "output":
            dtype = dtype[6:-1]
            self.array_len = int(sec.get("array_len"))
        self.data_type = dtype
        if dtype == "none":
            self.data_len = 0
        elif dtype == "str":
            self.data_len = int(sec.get("data_len"))
        elif dtype in TYPES_LEN:
            self.data_len = TYPES_LEN[dtype]
        else:
            raise ValueError("var %s: bad data_type %s" % (name, dtype))
        self.run_as_gcode = self.type == "input" and sec.get(
            "run_as_gcode", "false").strip().lower() in ("1", "true", "yes")
        self.template = Template(env, "t5uid1_var %s:script" % name, sec.get("script"))

    def decode(self, data):
        if self.data_type == "none" or self.data_len == 0:
            return None
        if self.data_type == "str":
            buf = bytearray()
            for i, b in enumerate(data):
                if i >= self.data_len or b == 0x00:
                    break
                if b == 0xFF and i + 1 < len(data) and data[i + 1] == 0xFF:
                    break
                buf.append(b)
            return buf.decode("ascii", "replace")
        if len(data) != self.data_len:
            raise ValueError("Expected %d bytes, got %d" % (self.data_len, len(data)))
        return struct.unpack(TYPES_FMT[self.data_type], data)[0]

    def encode(self, rendered):
        if self.data_type == "str":
            result = bytearray(rendered.replace("\n", ""), "ascii", "replace")
            target = self.data_len + (self.data_len % 2)
            if len(result) > target:
                result = result[:target]
            else:
                result.extend(b"\x00" * (target - len(result)))
            return result
        result = bytearray()
        count = 0
        for part in rendered.strip().split(","):
            part = part.strip()
            if not part:
                continue
            value = float(part) if self.data_type == "float" else int(float(part))
            result.extend(struct.pack(TYPES_FMT[self.data_type], value))
            count += 1
        if count != self.array_len:
            raise ValueError("Expected %d values, got %d" % (self.array_len, count))
        return result


class Page:
    def __init__(self, name, sec, var_names):
        self.name = name
        self.id = int(sec.get("id"))
        b = lambda k: sec.get(k, "false").strip().lower() in ("1", "true", "yes")
        self.is_boot, self.is_timeout, self.is_shutdown = b("boot"), b("timeout"), b("shutdown")
        self.var_auto, self.var = [], []
        for key, target in (("var_auto", self.var_auto), ("var", self.var)):
            for v in sec.get(key, "").split(","):
                v = v.strip()
                if not v or v in self.var_auto or v in self.var:
                    continue
                if v not in var_names:
                    raise ValueError("page %s: unknown var %s" % (name, v))
                target.append(v)


class Routine:
    def __init__(self, screen, env, name, sec, page_names):
        self.screen = screen
        self.name = name
        self.trigger = sec.get("trigger")
        if self.trigger not in ROUTINE_TRIGGERS:
            raise ValueError("routine %s: bad trigger" % name)
        self.page = None
        if self.trigger != "manual":
            self.page = sec.get("page")
            if self.page not in page_names:
                raise ValueError("routine %s: bad page" % name)
        self.delay = None
        if self.trigger != "enter_pre" and sec.get("delay") is not None:
            self.delay = int(sec.get("delay"))
        self.interval = 0
        if self.trigger in ("enter", "manual"):
            self.interval = int(sec.get("interval", "0"))
        self.run_as_gcode = sec.get("run_as_gcode", "false").strip().lower() in ("1", "true", "yes")
        self.template = Template(env, "t5uid1_routine %s:script" % name, sec.get("script"))
        self._handle = None
        self._should_stop = False

    def _schedule(self, delay):
        self._cancel()
        self._handle = asyncio.get_event_loop().call_later(delay, self._timer_run)

    def _cancel(self):
        if self._handle is not None:
            self._handle.cancel()
            self._handle = None

    def _timer_run(self):
        self._handle = None
        if self._should_stop:
            return
        self.run(is_timer=True)
        if not self._should_stop and self.interval > 0 and self._handle is None:
            self._schedule(self.interval)

    def run(self, repeat=True, is_timer=False):
        if not is_timer:
            self._should_stop = False
            if self.delay is not None:
                self._schedule(self.delay)
                return None
        context = self.screen.context("routine")
        context["is_timer"] = is_timer
        try:
            result = self.template.render(context).strip()
        except Exception:
            log.exception("Routine %s failed", self.name)
            result = ""
        if self.run_as_gcode and result:
            self.screen.run_gcode(result)
        if not is_timer and self.interval > 0:
            if repeat:
                self._schedule(self.interval)
            return None
        if self.trigger == "enter_pre":
            if not result:
                return True
            return ABORT_PAGE_SWITCH not in [c.strip() for c in result.split("\n")]
        return None

    def stop(self):
        self._should_stop = True
        self._cancel()


# ---------------------------------------------------------------------------
# The screen
# ---------------------------------------------------------------------------
class Screen:
    def __init__(self, cfg, moonraker, transport=None):
        self.cfg = cfg
        self.moonraker = moonraker
        self.transport = transport  # object with .send(cmd, data)
        self.loop = None

        self.machine_name = cfg.get("machine_name", "Ender 5 Plus")
        self.update_interval = cfg.getfloat("update_interval", 2.0)
        self.min_update_interval = cfg.getfloat("min_update_interval", 0.25)
        self.resync_interval = cfg.getfloat("resync_interval", 30.0)
        self.boot_sound = cfg.getint("boot_sound", 1)
        self.notification_sound = cfg.getint("notification_sound", 3)
        self.state_file = os.path.expanduser(cfg.get("state_file"))
        state = self._load_state()
        self.volume = int(state.get("volume", cfg.getint("volume", 75)))
        self.brightness = int(state.get("brightness", cfg.getint("brightness", 100)))
        self.insets = {k: cfg.getfloat(k, 30.0) for k in
                       ("x_min_inset", "x_max_inset", "y_min_inset", "y_max_inset")}
        self.limit_overrides = {k: cfg.getfloat(k, None) for k in
                                ("x_min", "x_max", "y_min", "y_max", "z_min", "z_max")}
        self.save_z_offset = cfg.getboolean("save_z_offset_on_save", True)

        self.constants = dict(BASE_CONSTANTS)
        defaults = {"pla": (200, 60), "abs": (240, 100), "petg": (240, 75), "tpu": (225, 50)}
        for mat, (hotend, bed) in defaults.items():
            self.constants["temp_" + mat] = {
                "hotend": cfg.getint("%s_hotend" % mat, hotend),
                "bed": cfg.getint("%s_bed" % mat, bed),
            }
        self.estop_confirm = cfg.getboolean("estop_confirm", True)
        self.beep_clips = cfg.getboolean("beep_clips", True)

        # live printer data (filled by the poller)
        self.status = {}
        self.objects = set()
        self.settings = {}
        self.pending_config = {}
        self.software_version = ""
        self.ready = False

        self._gui_version = self._os_version = 0
        self._current_page = ""
        self._page_enter_time = time.time()
        self._variable_data = {}
        self._inflight = 0
        self._last_gcode_done = 0.0
        self._last_message = None
        self._last_print_state = None
        self._probe_count = 0
        self._gcode_queue = None
        self._dirty = False
        self._vp_cache = {}
        self._ctl_cache = {}
        self.stats = {"writes": 0, "skipped": 0}
        self._jobs = set()
        self._estop_armed = 0.0
        self._files = []
        self._file_offset = 0
        self._selected_file = None

        self.env = jinja2.Environment("{%", "%}", "{", "}", trim_blocks=True,
                                      lstrip_blocks=True, extensions=["jinja2.ext.do"])
        self.vars, self.pages, self.routines = {}, {}, {}
        self._load_definitions()
        self.boot_page = next(p for p in self.pages.values() if p.is_boot).name
        self.shutdown_page = next((p.name for p in self.pages.values() if p.is_shutdown),
                                  self.boot_page)

    # -- definitions ---------------------------------------------------------
    def _load_definitions(self):
        base = os.path.join(os.path.dirname(__file__), "screen")
        parser = configparser.RawConfigParser(strict=False, inline_comment_prefixes=(";", "#"))
        for fname in ("routines.cfg", "pages.cfg", "vars_in.cfg", "vars_out.cfg"):
            with open(os.path.join(base, fname)) as f:
                parser.read_file(f, fname)
        overrides = self.cfg.get("extra_screen_config", "")
        if overrides:
            parser.read(os.path.expanduser(overrides))
        sections = parser.sections()
        for s in sections:
            if s.startswith("t5uid1_var "):
                name = s.split(None, 1)[1]
                self.vars[name] = Var(self.env, name, parser[s])
        for s in sections:
            if s.startswith("t5uid1_page "):
                name = s.split(None, 1)[1]
                self.pages[name] = Page(name, parser[s], self.vars)
        for s in sections:
            if s.startswith("t5uid1_routine "):
                name = s.split(None, 1)[1]
                self.routines[name] = Routine(self, self.env, name, parser[s], self.pages)
        log.info("Loaded %d vars, %d pages, %d routines",
                 len(self.vars), len(self.pages), len(self.routines))

    # -- persistence -----------------------------------------------------------
    def _load_state(self):
        try:
            with open(self.state_file) as f:
                return json.load(f)
        except Exception:
            return {}

    def _save_state(self):
        try:
            os.makedirs(os.path.dirname(self.state_file), exist_ok=True)
            with open(self.state_file, "w") as f:
                json.dump({"volume": self.volume, "brightness": self.brightness}, f)
        except Exception as e:
            log.warning("Could not save %s: %s", self.state_file, e)

    # -- template contexts -----------------------------------------------------
    def context(self, kind):
        ctx = {
            "printer": self.printer_view(),
            "get_variable": self.get_variable,
            "set_variable": self.set_variable,
            "enable_control": self.enable_control,
            "disable_control": self.disable_control,
            "start_routine": self.start_routine,
            "stop_routine": self.stop_routine,
            "set_message": self.set_message,
            "bitwise_and": lambda a, b: a & b,
            "bitwise_or": lambda a, b: a | b,
            "heater_min_temp": self.heater_min_temp,
            "heater_max_temp": self.heater_max_temp,
            "is_busy": self.is_busy,
        }
        if kind in ("input", "routine"):
            ctx.update({
                "page_name": self.page_name,
                "switch_page": self.switch_page,
                "play_sound": self.play_sound,
                "set_volume": self.set_volume,
                "set_brightness": self.set_brightness,
            })
        if kind == "input":
            ctx.update({
                "limit_extrude": self.limit_extrude,
                "heater_min_extrude_temp": self.heater_min_extrude_temp,
                "t5uid1_save_z_offset": self.save_z_offset,
                "estop_press": self.estop_press,
                "firmware_restart": self.firmware_restart,
                "files_select": self.files_select,
                "files_scroll": self.files_scroll,
                "print_confirm": self.print_confirm,
            })
        if kind == "output":
            ctx.update({
                "all_steppers_enabled": self.all_steppers_enabled,
                "probed_matrix": self.probed_matrix,
                "mesh_grid": self.mesh_grid,
                "pid_param": self.pid_param,
                "get_duration": get_duration,
            })
        if kind == "routine":
            ctx.update({
                "abort_page_switch": lambda: ABORT_PAGE_SWITCH,
                "files_load": self.files_load,
                "full_update": self.full_update,
                "check_paused": lambda: None,
            })
        return ctx

    def printer_view(self):
        view = {}
        for name, data in self.status.items():
            if name not in self.objects:
                continue
            data = dict(data)
            if name == "gcode_move":
                for key in ("gcode_position", "position", "homing_origin"):
                    if isinstance(data.get(key), list) and len(data[key]) >= 4:
                        data[key] = Coord(*data[key][:4])
            view[name] = data
        if "configfile" in self.objects:
            view["configfile"] = {"settings": self.settings,
                                  "save_config_pending_items": self.pending_config}
        view["t5uid1"] = self.get_status()
        return view

    def get_status(self):
        ps = self.status.get("print_stats", {})
        state = ps.get("state", "standby")
        ds = self.status.get("display_status", {})
        progress = ds.get("progress")
        if progress is None:
            progress = self.status.get("virtual_sdcard", {}).get("progress", 0)
        if state == "complete":
            progress = 1.0
        return {
            "version": self.software_version,
            "machine_name": self.machine_name,
            "gui_version": self._gui_version,
            "os_version": self._os_version,
            "notification_sound": self.notification_sound,
            "page": self._current_page,
            "volume": self.volume,
            "brightness": self.brightness,
            "pages": {p: self.pages[p].id for p in self.pages},
            "control_types": CONTROL_TYPES,
            "controls": CONTROLS,
            "constants": self.constants,
            "is_printing": state in ("printing", "paused"),
            "print_progress": int(round(max(0.0, min(1.0, _to_float(progress))) * 100)),
            "print_duration": max(0.0, _to_float(ps.get("print_duration"))),
            "limits": self.limits(),
            "has_bltouch": "bltouch" in self.settings,
        }

    # -- helpers used by templates --------------------------------------------
    class _Sentinel:
        pass

    def get_variable(self, name, default=_Sentinel):
        if name not in self._variable_data:
            if default is not Screen._Sentinel:
                return default
            raise KeyError("Variable '%s' not found" % name)
        return self._variable_data[name]

    def set_variable(self, name, value):
        self._variable_data[name] = value
        return ""

    def page_name(self, page_id):
        page_id = int(page_id)
        for name, page in self.pages.items():
            if page.id == page_id:
                return name
        raise KeyError("Page %d not found" % page_id)

    def heater_min_temp(self, heater):
        return _to_float(self.settings.get(heater, {}).get("min_temp"), 0)

    def heater_max_temp(self, heater, margin=0):
        mx = self.settings.get(heater, {}).get("max_temp")
        return max(0.0, _to_float(mx) - margin) if mx is not None else 0

    def heater_min_extrude_temp(self, heater):
        return _to_float(self.settings.get(heater, {}).get("min_extrude_temp"), 170.0)

    def limit_extrude(self, extruder, val):
        sec = self.settings.get(extruder)
        if sec is None:
            return 0
        return min(_to_float(sec.get("max_extrude_only_distance"), 50.0), val)

    def all_steppers_enabled(self):
        steppers = self.status.get("stepper_enable", {}).get("steppers")
        if not steppers:
            return "xyz" == self.status.get("toolhead", {}).get("homed_axes", "")
        return all(steppers.get(s, False) for s in ("stepper_x", "stepper_y", "stepper_z"))

    def pid_param(self, heater, param):
        key = "pid_k" + param
        pending = self.pending_config.get(heater, {})
        if key in pending:
            return _to_float(pending[key])
        return _to_float(self.settings.get(heater, {}).get(key), 0)

    def mesh_points_total(self):
        x, y = _parse_count(self.settings.get("bed_mesh", {}).get("probe_count"))
        return max(1, x * y)

    def probed_matrix(self):
        """Bitmask lighting up the 5x5 probe icons as probing progresses.
        Probe progress comes from Klipper's console output; meshes bigger
        than 5x5 are scaled down to 25 icons."""
        count = int(round(self._probe_count * 25 / self.mesh_points_total()))
        points_map = [0, 1, 2, 3, 4, 9, 8, 7, 6, 5, 10, 11, 12, 13, 14,
                      19, 18, 17, 16, 15, 20, 21, 22, 23, 24]
        res = 0
        for i in range(25):
            if count > points_map[i]:
                res |= (1 << (i + 16)) if i < 16 else (1 << (i - 16))
        return res

    def mesh_grid(self):
        """The bed mesh as 5 rows x 5 values (larger meshes are resampled,
        smaller ones are padded with zeros) for the leveling screen."""
        grid = self.status.get("bed_mesh", {}).get("probed_matrix") or []
        rows = [resample(list(r), 5) for r in resample(list(grid), 5)]
        rows = [r + [0] * (5 - len(r)) for r in rows]
        while len(rows) < 5:
            rows.append([0] * 5)
        return rows

    def limits(self):
        res = {}
        for axis in "xyz":
            sec = self.settings.get("stepper_" + axis, {})
            lo = _to_float(sec.get("position_min"), 0.0)
            hi = _to_float(sec.get("position_max"), 0.0)
            olo, ohi = self.limit_overrides[axis + "_min"], self.limit_overrides[axis + "_max"]
            if olo is not None and lo < olo < hi:
                lo = olo
            if ohi is not None and lo < ohi < hi:
                hi = ohi
            res[axis + "_min"], res[axis + "_max"] = lo, hi
        for axis in "xy":
            span = (res[axis + "_max"] - res[axis + "_min"]) / 2.0
            for side in ("min", "max"):
                k = "%s_%s_inset" % (axis, side)
                res[k] = min(self.insets[k], span)
        return res

    def is_busy(self):
        if self._inflight > 0 or time.monotonic() - self._last_gcode_done < 1.0:
            return True
        return self.status.get("idle_timeout", {}).get("state") == "Printing"

    def set_message(self, message):
        message = message or ""
        self.set_variable("message", message)
        if "message" in self.vars and self.ready:
            self.send_var("message")
        if message and "message_timeout" in self.routines:
            self.start_routine("message_timeout")
        return ""

    # -- routines --------------------------------------------------------------
    def start_routine(self, name):
        r = self.routines[name]
        if r.trigger != "manual":
            raise ValueError("Routine %s cannot be started manually" % name)
        r.run()
        return ""

    def stop_routine(self, name):
        self.routines[name].stop()
        return ""

    def _page_routines(self, page, trigger):
        results = []
        for r in self.routines.values():
            if r.page == page and r.trigger == trigger:
                res = r.run()
                if res is not None:
                    results.append(res)
        return all(results)

    def _stop_page_routines(self, page):
        for r in self.routines.values():
            if r.page == page:
                r.stop()

    # -- screen I/O ------------------------------------------------------------
    def write(self, address, data, cache=True):
        data = bytes(data)
        if not 0 <= address <= 0xFFFF or not data or len(data) > 64 or len(data) % 2:
            raise ValueError("invalid write %s len %d" % (hex(address), len(data)))
        if cache and address not in NO_CACHE:
            if self._vp_cache.get(address) == data:
                self.stats["skipped"] += 1
                return
            self._vp_cache[address] = data
        self.stats["writes"] += 1
        if self.transport is not None:
            self.transport.send(dwin.CMD_WRITEVAR, bytes([address >> 8, address & 0xFF]) + bytes(data))

    def read(self, address, words):
        if self.transport is not None:
            self.transport.send(dwin.CMD_READVAR, bytes([address >> 8, address & 0xFF, words]))

    def send_var(self, name):
        var = self.vars[name]
        if var.type != "output":
            raise ValueError("%s is not an output" % name)
        rendered = var.template.render(self.context("output"))
        self.write(var.address, var.encode(rendered))

    def send_page_vars(self, page=None, complete=False):
        page = self.pages[page or self._current_page]
        names = (page.var + page.var_auto) if complete else page.var_auto
        for name in names:
            try:
                self.send_var(name)
            except Exception:
                log.exception("Updating var %s failed", name)

    def full_update(self):
        if self._current_page:
            self.send_page_vars(complete=True)
        return ""

    def switch_page(self, name, force=False):
        if name not in self.pages:
            raise ValueError("invalid page %s" % name)
        if name == self._current_page and not force:
            return ""
        if not self._page_routines(name, "enter_pre"):
            return ""
        page = self.pages[name]
        self.send_page_vars(name, complete=True)
        self.write(ADDR_PAGE, [0x5A, 0x01, 0x00, page.id])
        if self._current_page:
            self._stop_page_routines(self._current_page)
            self._page_routines(self._current_page, "leave")
        self._current_page = name
        self._page_enter_time = time.time()
        if name == "leveling_probing":
            self._probe_count = 0
        self._page_routines(name, "enter")
        return ""

    def play_sound(self, start, slen=1, volume=-1):
        if volume is None or volume < 0:
            volume = self.volume
        log.info("Sound %d (volume %d)", int(start), volume)
        self.write(ADDR_SOUND, [int(start) & 0xFF, int(slen) & 0xFF,
                                map_value_range(min(volume, 100), 0, 100, 0, 255), 0])
        return ""

    def _set_control(self, page, ctype, control, state):
        key = (page, ctype, control)
        if self._ctl_cache.get(key) == state:
            self.stats["skipped"] += 1
            return ""
        self._ctl_cache[key] = state
        self.write(ADDR_CONTROL, [0x5A, 0xA5, 0, page, control, ctype, 0, state])
        return ""

    def enable_control(self, page, ctype, control):
        return self._set_control(page, ctype, control, 1)

    def disable_control(self, page, ctype, control):
        return self._set_control(page, ctype, control, 0)

    def invalidate_cache(self):
        """Forget what we think the screen shows; the next refresh rewrites
        everything on the current page."""
        self._vp_cache.clear()
        self._ctl_cache.clear()

    def set_brightness(self, brightness, persist=True):
        brightness = max(0, min(100, int(brightness)))
        val = map_value_range(brightness, 0, 100, 5, 100)
        self.write(ADDR_BRIGHTNESS, [val, val])
        if persist and brightness != self.brightness:
            self.brightness = brightness
            self._save_state()
        return ""

    def set_volume(self, volume, persist=True):
        volume = max(0, min(100, int(volume)))
        self.write(ADDR_VOLUME, [map_value_range(volume, 0, 100, 0, 255), 0])
        if persist and volume != self.volume:
            self.volume = volume
            self._save_state()
        return ""

    # -- G-code ----------------------------------------------------------------
    def run_gcode(self, script):
        lines = [l.strip() for l in script.split("\n")]
        lines = [l for l in lines if l and l != ABORT_PAGE_SWITCH]
        if not lines:
            return
        self._inflight += 1
        self._gcode_queue.put_nowait("\n".join(lines))

    async def _gcode_worker(self):
        while True:
            script = await self._gcode_queue.get()
            log.info("G-code: %s", script.replace("\n", " | "))
            try:
                await self.loop.run_in_executor(None, self.moonraker.gcode, script)
            except Exception as e:
                msg = str(e).replace("\n", " ").strip()
                log.warning("G-code failed: %s", msg)
                if not msg.lower().startswith("klipper state") and self.ready:
                    self.set_message(msg[:32])
            finally:
                self._inflight -= 1
                self._last_gcode_done = time.monotonic()

    # -- background Moonraker calls ------------------------------------------
    def _background(self, fn, *args, done=None, fail=None):
        """Run a blocking Moonraker call off the event loop; call done(result)
        or fail(message) back on the loop."""
        async def job():
            try:
                res = await self.loop.run_in_executor(None, fn, *args)
            except Exception as e:
                msg = str(e).replace("\n", " ").strip() or type(e).__name__
                log.warning("%s failed: %s", getattr(fn, "__name__", fn), msg)
                if fail:
                    fail(msg)
                elif self.ready:
                    self.set_message(msg[:32])
                return
            if done:
                done(res)
        if self.loop is None:
            return
        task = self.loop.create_task(job())
        self._jobs.add(task)
        task.add_done_callback(self._jobs.discard)

    # -- emergency stop / restart ------------------------------------------------
    def estop_press(self):
        """STOP button. With estop_confirm (default) it needs a second press
        within 3 s, so a stray touch can't kill a print."""
        now = time.monotonic()
        if self.estop_confirm and now - self._estop_armed > 3.0:
            self._estop_armed = now
            self.set_message("Press STOP again to halt")
            if self.notification_sound >= 0:
                self.play_sound(self.notification_sound)
            return ""
        self._estop_armed = 0.0
        log.warning("Emergency stop from the screen")
        self.set_message("EMERGENCY STOP")
        self._background(self.moonraker.emergency_stop)
        return ""

    def firmware_restart(self):
        """Restart button on the error page: FIRMWARE_RESTART, or restart the
        Klipper service if Klipper isn't even connected."""
        self.set_variable("line4", "Restarting...")
        try:
            self.send_var("line4")
        except Exception:
            pass

        def fallback(msg):
            log.info("FIRMWARE_RESTART failed (%s); restarting the klipper service", msg)
            self._background(self.moonraker.restart_service, "klipper",
                             fail=lambda m: self._kill_line("Restart failed: " + m))
        self._background(self.moonraker.firmware_restart, fail=fallback)
        return ""

    def _kill_line(self, text):
        self.set_variable("line4", text[:32])
        try:
            self.send_var("line4")
        except Exception:
            pass

    # -- file browser ------------------------------------------------------------
    def files_load(self):
        """Entering the file list: show what we have, refresh from Moonraker."""
        if not self._files:
            self._set_rows(["Loading..."], "")
        self._background(self.moonraker.files_list, done=self._files_loaded,
                         fail=lambda m: self._set_rows(["Can't read files:", m[:38]], ""))
        return ""

    def _files_loaded(self, result):
        files = [f for f in (result or []) if f.get("path", "").lower().endswith(GCODE_EXT)]
        files.sort(key=lambda f: f.get("modified", 0), reverse=True)
        keep = self._files[self._file_offset]["path"] if self._files else None
        self._files = files
        self._file_offset = 0
        if keep:  # stay on the same page when coming back from "Start print"
            for i, f in enumerate(files):
                if f["path"] == keep:
                    self._file_offset = i - i % FILES_PER_PAGE
                    break
        self._render_files()

    @staticmethod
    def display_name(path, width=38):
        name = path
        for ext in GCODE_EXT:
            if name.lower().endswith(ext):
                name = name[:-len(ext)]
                break
        name = name.encode("ascii", "replace").decode()
        return name if len(name) <= width else name[:width - 2] + ".."

    def _render_files(self):
        if not self._files:
            self._set_rows(["No G-code files", "Upload from Mainsail or your slicer"], "")
            return
        page = self._files[self._file_offset:self._file_offset + FILES_PER_PAGE]
        pages = (len(self._files) + FILES_PER_PAGE - 1) // FILES_PER_PAGE
        self._set_rows([self.display_name(f["path"]) for f in page],
                       "%d/%d" % (self._file_offset // FILES_PER_PAGE + 1, pages))

    def _set_rows(self, rows, page_label):
        rows = list(rows) + [""] * (FILES_PER_PAGE - len(rows))
        for i, text in enumerate(rows[:FILES_PER_PAGE]):
            self.set_variable("file_row%d" % i, text[:38])
        self.set_variable("file_page", page_label)
        if self._current_page == "files":
            self.send_page_vars(complete=True)

    def files_scroll(self, direction):
        step = -FILES_PER_PAGE if int(direction) == 0 else FILES_PER_PAGE
        new = self._file_offset + step
        if 0 <= new < len(self._files):
            self._file_offset = new
            self._render_files()
        return ""

    def files_select(self, row):
        idx = self._file_offset + int(row)
        if idx >= len(self._files):
            return ""
        f = self._files[idx]
        self._selected_file = f["path"]
        name = self.display_name(f["path"], 80)
        self.set_variable("confirm_name1", name[:40])
        self.set_variable("confirm_name2", name[40:80])
        self.set_variable("confirm_info1", "Reading file details...")
        self.set_variable("confirm_info2", "")
        self.set_variable("confirm_info3", "")
        self.switch_page("print_confirm")
        self._background(self.moonraker.file_metadata, f["path"], done=self._show_metadata,
                         fail=lambda m: self._set_info([m[:48], "", ""]))
        return ""

    def _show_metadata(self, md):
        md = md or {}
        lines = []
        part = []
        if md.get("estimated_time"):
            part.append("Time: " + get_duration(md["estimated_time"]))
        if md.get("filament_total"):
            part.append("Filament: %.1f m" % (md["filament_total"] / 1000.0))
        lines.append("   ".join(part))
        part = []
        if md.get("layer_height"):
            part.append("Layer: %.2f mm" % md["layer_height"])
        if md.get("object_height"):
            part.append("Height: %.1f mm" % md["object_height"])
        lines.append("   ".join(part))
        part = []
        if md.get("filament_type"):
            part.append(str(md["filament_type"]).split(";")[0])
        if md.get("first_layer_extr_temp") or md.get("first_layer_bed_temp"):
            part.append("%d/%d C" % (md.get("first_layer_extr_temp") or 0,
                                     md.get("first_layer_bed_temp") or 0))
        if md.get("slicer"):
            part.append(str(md["slicer"]))
        lines.append("   ".join(part))
        if not any(lines):
            lines = ["No slicer details in this file", "", ""]
        self._set_info(lines)

    def _set_info(self, lines):
        for i, text in enumerate(lines[:3], 1):
            self.set_variable("confirm_info%d" % i, text.encode("ascii", "replace").decode()[:48])
        if self._current_page == "print_confirm":
            self.send_page_vars(complete=True)

    def print_confirm(self):
        if not self._selected_file:
            return ""
        if self.get_status()["is_printing"]:
            self.set_message("Already printing")
            return ""
        log.info("Starting print from the screen: %s", self._selected_file)
        self.set_message("Starting print...")
        self._background(self.moonraker.print_start, self._selected_file)
        return ""

    # -- frames from the screen ------------------------------------------------
    def handle_frame(self, cmd, data):
        """Called from the serial thread."""
        if self.loop is not None:
            self.loop.call_soon_threadsafe(self._handle_frame, cmd, data)

    def _handle_frame(self, cmd, data):
        if cmd != dwin.CMD_READVAR or len(data) < 3:
            return
        address = (data[0] << 8) | data[1]
        words = data[2]
        payload = data[3:3 + words * 2]
        if len(payload) < words * 2:
            log.warning("Truncated frame for %s", hex(address))
            return
        if address == ADDR_PAGE_NOW and len(payload) >= 2:
            self._check_page(payload[1])
            return
        if address == ADDR_VERSION and len(payload) >= 2:
            self._gui_version, self._os_version = payload[0], payload[1]
            log.info("Screen GUI v%d, OS v%d", self._gui_version, self._os_version)
            return
        self.handle_input(address, payload)

    def _check_page(self, shown_id):
        """The screen reported which page it shows. Popups and local page
        changes are normal, so only correct it if it shows one of our full
        pages that isn't the current one (e.g. the screen was power cycled)."""
        cur = self.pages.get(self._current_page)
        if cur is None or shown_id == cur.id:
            return
        if shown_id in {p.id for p in self.pages.values()}:
            log.info("Screen shows page %d, expected %s (%d); resyncing",
                     shown_id, cur.name, cur.id)
            self.invalidate_cache()
            self.send_page_vars(complete=True)
            self.write(ADDR_PAGE, [0x5A, 0x01, 0x00, cur.id])

    def handle_input(self, address, payload):
        # The screen changed this VP itself (slider, numeric entry), so our
        # cached copy is stale; make sure the next refresh rewrites it.
        self._vp_cache.pop(address, None)
        if not self.ready and address not in ALWAYS_ALLOWED:
            log.debug("Ignoring touch on %s, printer not ready", hex(address))
            return
        handled = False
        for var in self.vars.values():
            if var.type != "input" or var.address != address:
                continue
            handled = True
            try:
                ctx = self.context("input")
                value = var.decode(payload)
                if value is not None:
                    ctx["data"] = value
                result = var.template.render(ctx).strip()
                if var.run_as_gcode and result:
                    self.run_gcode(result)
            except Exception:
                log.exception("Input handler %s failed", var.name)
        if not handled:
            log.debug("Unhandled screen input at %s", hex(address))

    # -- printer state changes (called by the poller) --------------------------
    def on_ready(self, objects, settings, pending, version):
        first = not self.ready and self._current_page == ""
        self.objects = set(objects)
        self.settings = settings
        self.pending_config = pending
        self.software_version = version
        self.ready = True
        self.invalidate_cache()
        self._last_print_state = self.status.get("print_stats", {}).get("state")
        self._last_message = self.status.get("display_status", {}).get("message")
        self.read(ADDR_VERSION, 1)
        self.set_brightness(self.brightness, persist=False)
        # Always set the screen's master volume: after a flash with the
        # memory-wiping T5UID1.CFG it can be 0, which mutes every sound.
        self.set_volume(self.volume, persist=False)
        self._current_page = ""
        self.switch_page(self.boot_page)
        if self.boot_sound >= 0 and first:
            self.play_sound(self.boot_sound)

    def on_not_ready(self, message):
        was_ready = self.ready
        self.ready = False
        for r in self.routines.values():
            r.stop()
        parts = textwrap.wrap(message or "Klipper not ready", 32)[:4]
        parts += [""] * (4 - len(parts))
        for i, p in enumerate(parts, 1):
            self.set_variable("line%d" % i, p.strip())
        try:
            self.switch_page(self.shutdown_page, force=True)
        except Exception:
            log.exception("Showing shutdown page failed")
        if was_ready and self.notification_sound >= 0:
            self.play_sound(self.notification_sound)

    def on_status(self, status):
        for name, data in status.items():
            self.status.setdefault(name, {}).update(data)
        self._dirty = True
        if not self.ready:
            return
        state = self.status.get("print_stats", {}).get("state")
        prev = self._last_print_state
        self._last_print_state = state
        active = ("printing", "paused")
        if state in active and prev not in active and "print_start" in self.routines:
            self.start_routine("print_start")
        elif prev in active and state not in active and "print_end" in self.routines:
            self.start_routine("print_end")
        message = self.status.get("display_status", {}).get("message")
        if message != self._last_message:
            self._last_message = message
            self.set_message(message or "")

    def on_gcode_store(self, entries):
        since = self._page_enter_time
        self._probe_count = sum(
            1 for e in entries
            if e.get("time", 0) >= since and e.get("message", "").startswith("probe at "))

    def on_gcode_line(self, line):
        """Klipper console output: probing progress and M300 beeps."""
        text = line.lstrip("/ ").strip()
        if self._current_page == "leveling_probing" and text.startswith("probe at "):
            self._probe_count += 1
            self._dirty = True
            return
        if "e5p_dgus:" not in text:
            return
        log.debug("Console: %s", text)
        m = BEEP_RE.search(text)
        if m:
            self.beep(int(m.group(1)), int(m.group(2)))
            return
        m = SOUND_RE.search(text)
        if m:
            vol = int(m.group(2)) if m.group(2) else -1
            self.play_sound(min(int(m.group(1)), 255), volume=min(vol, 100))

    def beep(self, freq, ms):
        """M300 S<freq> P<ms>: closest of 4 pitches, short (0.1 s) or long
        (0.45 s) clip; longer beeps repeat the long clip (max ~3 s)."""
        if not self.beep_clips:
            if self.notification_sound >= 0:
                self.play_sound(self.notification_sound)
            return
        freq = max(1, freq)
        idx = min(range(len(BEEP_PITCHES)),
                  key=lambda i: abs(math.log(BEEP_PITCHES[i] / freq)))
        long_clip = ms > 200
        sid = BEEP_FIRST_ID + 2 * idx + (1 if long_clip else 0)
        repeats = 1
        if ms > 600:
            repeats = min(7, int(round(ms / 1000.0 / BEEP_LONG_SECS)))
        self.play_sound(sid)
        for k in range(1, repeats):
            self.loop.call_later(k * BEEP_LONG_SECS, self.play_sound, sid)

    @property
    def current_page(self):
        return self._current_page

    # -- main loops --------------------------------------------------------------
    async def run_update_loop(self):
        """Refresh the current page soon after printer data changes (at most
        every min_update_interval), and at least every update_interval for
        clocks like elapsed time. Only changed values are actually sent."""
        last_full = last_resync = time.monotonic()
        while True:
            await asyncio.sleep(self.min_update_interval)
            if not self.ready or not self._current_page:
                continue
            now = time.monotonic()
            if self._dirty or now - last_full >= self.update_interval:
                self._dirty = False
                last_full = now
                self.send_page_vars(complete=False)
            if self.resync_interval > 0 and now - last_resync >= self.resync_interval:
                last_resync = now
                log.debug("Resync: %d writes, %d skipped so far",
                          self.stats["writes"], self.stats["skipped"])
                self.invalidate_cache()
                self.read(ADDR_PAGE_NOW, 1)

    def start(self, loop):
        self.loop = loop
        self._gcode_queue = asyncio.Queue()
        self._tasks = [loop.create_task(self._gcode_worker()),
                       loop.create_task(self.run_update_loop())]

    def stop(self):
        for r in self.routines.values():
            r.stop()
        for t in getattr(self, "_tasks", []):
            t.cancel()
