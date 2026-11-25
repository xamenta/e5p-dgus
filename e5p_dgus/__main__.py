# e5p-dgus entry point: python3 -m e5p_dgus -c ~/printer_data/config/e5p_dgus.cfg
#
# Part of e5p-dgus. GPLv3.
import argparse
import asyncio
import configparser
import logging
import logging.handlers
import os
import signal
import sys

from . import __version__
from .moonraker import Moonraker, MoonrakerError
from .runtime import Screen
from .live import LiveLink

log = logging.getLogger("e5p_dgus")

DEFAULTS = {
    "serial_port": "/dev/serial0",
    "baud": "115200",
    "moonraker_url": "http://127.0.0.1:7125",
    "moonraker_api_key": "",
    "poll_interval": "0.5",
    "config_poll_interval": "10",
    "update_interval": "2",
    "machine_name": "Ender 5 Plus",
    "volume": "75",
    "brightness": "100",
    "boot_sound": "1",
    "notification_sound": "3",
    "state_file": "~/printer_data/config/.e5p_dgus_state.json",
    "log_file": "~/printer_data/logs/e5p_dgus.log",
    "log_level": "INFO",
    "connection": "websocket",
}

# Printer objects the screen definitions read. Only the ones that exist in
# your Klipper config get queried.
WANTED_OBJECTS = (
    "toolhead", "gcode_move", "extruder", "extruder1", "heater_bed", "fan",
    "pause_resume", "virtual_sdcard", "print_stats", "display_status",
    "idle_timeout", "bed_mesh", "probe", "bltouch", "stepper_enable", "webhooks",
)
# bed_mesh can be large; only ask for what the screen uses.
OBJECT_FIELDS = {
    "bed_mesh": ["profile_name", "probed_matrix"],
}


def load_config(path):
    parser = configparser.ConfigParser(defaults=DEFAULTS, inline_comment_prefixes=("#", ";"))
    if path and os.path.exists(path):
        parser.read(path)
    elif path:
        print("Config %s not found, using defaults" % path, file=sys.stderr)
    if not parser.has_section("dgus"):
        parser.add_section("dgus")
    return parser["dgus"]


def setup_logging(cfg, verbose):
    level = logging.DEBUG if verbose else getattr(logging, cfg.get("log_level").upper(), logging.INFO)
    root = logging.getLogger()
    root.setLevel(level)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    sh = logging.StreamHandler()
    sh.setFormatter(fmt)
    root.addHandler(sh)
    path = os.path.expanduser(cfg.get("log_file"))
    if path:
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            fh = logging.handlers.RotatingFileHandler(path, maxBytes=1_000_000, backupCount=2)
            fh.setFormatter(fmt)
            root.addHandler(fh)
        except OSError as e:
            log.warning("Cannot log to %s: %s", path, e)


class Poller:
    """Keeps the screen in sync with Klipper through Moonraker."""

    def __init__(self, cfg, mr, screen):
        self.cfg = cfg
        self.mr = mr
        self.screen = screen
        self.poll_interval = cfg.getfloat("poll_interval")
        self.config_poll_interval = cfg.getfloat("config_poll_interval")
        self.state = None
        self.query = {}
        self.last_console = 0.0

    async def call(self, fn, *args):
        return await asyncio.get_running_loop().run_in_executor(None, fn, *args)

    async def run(self):
        since_config = 0.0
        while True:
            try:
                info = await self.call(self.mr.server_info)
                state = info.get("klippy_state", "disconnected")
                if not info.get("klippy_connected", True):
                    state = "disconnected"
            except MoonrakerError as e:
                state, info = "moonraker_down", {"error": str(e)}

            if state != self.state:
                log.info("Klipper state: %s -> %s", self.state, state)
                await self.on_state(state)
                since_config = 0.0

            if state == "ready":
                try:
                    status = await self.call(self.mr.query, self.query)
                    self.screen.on_status(status)
                    # follow the console (M300 beeps, probing progress)
                    entries = await self.call(self.mr.gcode_store, 50)
                    for e in entries:
                        t = e.get("time", 0)
                        if t > self.last_console:
                            self.last_console = t
                            self.screen.on_gcode_line(e.get("message", ""))
                    since_config += self.poll_interval
                    if since_config >= self.config_poll_interval:
                        since_config = 0.0
                        cf = await self.call(self.mr.query, {"configfile": ["save_config_pending_items"]})
                        self.screen.pending_config = cf.get("configfile", {}).get(
                            "save_config_pending_items", {}) or {}
                except MoonrakerError as e:
                    log.warning("Status poll failed: %s", e)
            await asyncio.sleep(self.poll_interval)

    async def on_state(self, state):
        self.state = state
        if state == "ready":
            try:
                objects = await self.call(self.mr.objects_list)
                pinfo = await self.call(self.mr.printer_info)
                self.query = {o: OBJECT_FIELDS.get(o) for o in WANTED_OBJECTS if o in objects}
                cf = await self.call(self.mr.query, {"configfile": ["settings", "save_config_pending_items"]})
                cf = cf.get("configfile", {})
                status = await self.call(self.mr.query, self.query)
            except MoonrakerError as e:
                log.warning("Reading printer setup failed (%s), will retry", e)
                self.state = None
                return
            try:  # don't replay old console lines
                entries = await self.call(self.mr.gcode_store, 1)
                self.last_console = max([e.get("time", 0) for e in entries] + [self.last_console])
            except MoonrakerError:
                pass
            self.screen.status = {}
            self.screen.on_status(status)
            self.screen.on_ready(objects, cf.get("settings", {}) or {},
                                 cf.get("save_config_pending_items", {}) or {},
                                 pinfo.get("software_version", ""))
            return
        if state == "moonraker_down":
            msg = self.screen.moonraker_down_message()
        elif state == "disconnected":
            msg = "Klipper not connected"
        elif state == "startup":
            msg = "Klipper starting..."
        else:
            try:
                msg = (await self.call(self.mr.printer_info)).get("state_message", "")
            except MoonrakerError:
                msg = ""
            msg = msg or "Klipper %s" % state
            # Klipper's messages end with a long "Once the underlying issue is
            # corrected..." paragraph; the screen has room for ~4 lines.
            msg = msg.split("\n")[0].strip() or msg
        self.screen.on_not_ready(msg)


async def connect(cfg, mr, screen):
    """Use Moonraker's websocket (push updates). Fall back to HTTP polling
    if the websocket can't be used but plain HTTP works (very old Moonraker,
    proxies), or if connection: polling is set."""
    mode = cfg.get("connection").strip().lower()
    if mode != "polling":
        link = LiveLink(cfg, screen, WANTED_OBJECTS, OBJECT_FIELDS)
        failures = 0
        while not link.connected_once:
            try:
                await link.run_once()
            except Exception as e:
                failures += 1
                log.warning("Websocket connect failed (%s)", e or type(e).__name__)
                http_ok = True
                try:
                    await asyncio.get_running_loop().run_in_executor(None, mr.server_info)
                except MoonrakerError:
                    http_ok = False
                if http_ok and failures >= 3:
                    log.warning("Moonraker answers HTTP but not the websocket; using polling")
                    break
                if not http_ok:
                    screen.on_not_ready(screen.moonraker_down_message())
                await asyncio.sleep(min(2 * failures, 10))
        else:
            await link.run()  # connected at least once: keep reconnecting
            return
        if link.connected_once:
            await link.run()
            return
    log.info("Using HTTP polling every %ss", cfg.get("poll_interval"))
    await Poller(cfg, mr, screen).run()


def main(argv=None):
    ap = argparse.ArgumentParser(prog="e5p_dgus", description="DWIN T5UID1 screen service for Klipper/Moonraker")
    ap.add_argument("-c", "--config", default=os.path.expanduser("~/printer_data/config/e5p_dgus.cfg"))
    ap.add_argument("-v", "--verbose", action="store_true")
    ap.add_argument("--version", action="version", version=__version__)
    args = ap.parse_args(argv)

    cfg = load_config(args.config)
    setup_logging(cfg, args.verbose)
    log.info("e5p-dgus %s starting", __version__)

    mr = Moonraker(cfg.get("moonraker_url"), cfg.get("moonraker_api_key") or None)
    screen = Screen(cfg, mr)

    from .dwin import DwinSerial
    transport = DwinSerial(cfg.get("serial_port"), cfg.getint("baud"), screen.handle_frame)
    screen.transport = transport

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, loop.stop)
    transport.start()
    screen.start(loop)
    main_task = loop.create_task(connect(cfg, mr, screen))  # keep a reference
    try:
        loop.run_forever()
    finally:
        # cancel everything and let it finish, so shutdown is quiet
        screen.stop()
        tasks = [t for t in asyncio.all_tasks(loop) if not t.done()]
        for t in tasks:
            t.cancel()
        if tasks:
            loop.run_until_complete(asyncio.gather(*tasks, return_exceptions=True))
        loop.close()
        transport.stop()
        log.info("Stopped")


if __name__ == "__main__":
    main()
