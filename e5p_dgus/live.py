# Push-based link to Moonraker over its websocket (JSON-RPC 2.0).
#
# Instead of asking for printer status twice a second, we subscribe once
# and Moonraker sends only what changed, as it changes. Klipper restarts,
# shutdowns and console output (used for probing progress) arrive as
# notifications too.
#
# Part of e5p-dgus. GPLv3.
import asyncio
import itertools
import json
import logging
import urllib.parse

from . import __version__
from .websocket import WebSocket, WebSocketClosed

log = logging.getLogger("e5p_dgus.live")


class RpcError(Exception):
    pass


def ws_url(moonraker_url):
    u = urllib.parse.urlparse(moonraker_url)
    scheme = "ws" if u.scheme in ("http", "ws", "") else u.scheme
    return urllib.parse.urlunparse((scheme, u.netloc, "/websocket", "", "", ""))


class LiveLink:
    def __init__(self, cfg, screen, query_objects, object_fields):
        self.cfg = cfg
        self.screen = screen
        self.url = ws_url(cfg.get("moonraker_url"))
        self.api_key = cfg.get("moonraker_api_key") or None
        self.wanted = query_objects
        self.fields = object_fields
        self.ws = None
        self._ids = itertools.count(1)
        self._pending = {}
        self._state = None
        self._lock = asyncio.Lock()
        self._watchdog = None
        self._tasks = set()  # strong refs: asyncio only keeps weak ones
        self.connected_once = False

    def _spawn(self, coro):
        task = asyncio.get_running_loop().create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    # -- JSON-RPC ----------------------------------------------------------------
    async def rpc(self, method, params=None, timeout=10.0):
        if self.ws is None or self.ws.closed:
            raise RpcError("not connected")
        rid = next(self._ids)
        fut = asyncio.get_running_loop().create_future()
        self._pending[rid] = fut
        msg = {"jsonrpc": "2.0", "method": method, "id": rid}
        if params is not None:
            msg["params"] = params
        await self.ws.send(json.dumps(msg))
        try:
            return await asyncio.wait_for(fut, timeout)
        finally:
            self._pending.pop(rid, None)

    async def _reader(self):
        while True:
            msg = json.loads(await self.ws.recv())
            if "id" in msg and msg["id"] in self._pending:
                fut = self._pending[msg["id"]]
                if not fut.done():
                    if "error" in msg:
                        fut.set_exception(RpcError(msg["error"].get("message", "error")))
                    else:
                        fut.set_result(msg.get("result"))
                continue
            method = msg.get("method")
            params = msg.get("params") or []
            if method == "notify_status_update" and params:
                self._apply_status(params[0])
            elif method == "notify_gcode_response" and params:
                for line in str(params[0]).split("\n"):
                    self.screen.on_gcode_line(line)
            elif method == "notify_klippy_ready":
                self._spawn(self._set_state("ready"))
            elif method == "notify_klippy_shutdown":
                self._spawn(self._set_state("shutdown"))
            elif method == "notify_klippy_disconnected":
                self._spawn(self._set_state("disconnected"))

    def _apply_status(self, status):
        status = dict(status)
        cf = status.pop("configfile", None)
        if cf:
            if "settings" in cf:
                self.screen.settings = cf["settings"] or {}
            if "save_config_pending_items" in cf:
                self.screen.pending_config = cf["save_config_pending_items"] or {}
        if status:
            self.screen.on_status(status)

    # -- Klipper state ---------------------------------------------------------------
    async def _set_state(self, state, message=None):
        async with self._lock:
            if state == self._state:
                return
            log.info("Klipper state: %s -> %s", self._state, state)
            self._state = state
            if state == "ready":
                try:
                    await self._setup()
                except (RpcError, asyncio.TimeoutError) as e:
                    log.warning("Setup after ready failed (%s); retrying", e)
                    self._state = None
                    self._start_watchdog()
                return
            if message is None:
                if state == "disconnected":
                    message = "Klipper not connected"
                elif state == "startup":
                    message = "Klipper starting..."
                else:
                    try:
                        info = await self.rpc("printer.info")
                        message = (info.get("state_message") or "").split("\n")[0].strip()
                    except Exception:
                        message = ""
                    message = message or "Klipper %s" % state
            self.screen.on_not_ready(message)
            self._start_watchdog()

    async def _setup(self):
        objects = (await self.rpc("printer.objects.list")).get("objects", [])
        pinfo = await self.rpc("printer.info")
        sub = {o: self.fields.get(o) for o in self.wanted if o in objects}
        sub["configfile"] = ["settings", "save_config_pending_items"]
        res = await self.rpc("printer.objects.subscribe", {"objects": sub})
        status = dict(res.get("status", {}))
        cf = status.pop("configfile", {}) or {}
        self.screen.status = {}
        self.screen.on_status(status)
        self.screen.on_ready(objects, cf.get("settings") or {},
                             cf.get("save_config_pending_items") or {},
                             pinfo.get("software_version", ""))
        log.info("Subscribed to %d printer objects", len(sub))

    def _start_watchdog(self):
        """While Klipper isn't ready, check its state every 2 s: Moonraker
        doesn't send a notification for every transition (e.g. a config
        error after a restart)."""
        if self._watchdog is None or self._watchdog.done():
            self._watchdog = self._spawn(self._watch())

    async def _watch(self):
        while self._state != "ready" and self.ws is not None and not self.ws.closed:
            await asyncio.sleep(2)
            try:
                info = await self.rpc("server.info")
            except Exception:
                continue
            state = info.get("klippy_state", "disconnected")
            if not info.get("klippy_connected", True):
                state = "disconnected"
            if state != self._state:
                await self._set_state(state)

    # -- connection loop ----------------------------------------------------------------
    async def run_once(self):
        """Connect, sync and process messages until the connection drops."""
        headers = {"X-Api-Key": self.api_key} if self.api_key else None
        self.ws = await WebSocket.connect(self.url, headers)
        self.connected_once = True
        self._state = None
        reader = self._spawn(self._reader())
        try:
            try:
                await self.rpc("server.connection.identify", {
                    "client_name": "e5p-dgus", "version": __version__,
                    "type": "other", "url": "https://github.com/"})
            except RpcError:
                pass  # older Moonraker
            info = await self.rpc("server.info")
            state = info.get("klippy_state", "disconnected")
            if not info.get("klippy_connected", True):
                state = "disconnected"
            await self._set_state(state)
            await reader
        finally:
            reader.cancel()
            for fut in self._pending.values():
                if not fut.done():
                    fut.set_exception(RpcError("connection closed"))
            self._pending.clear()
            await self.ws.close()

    async def run(self):
        delay = 1
        while True:
            try:
                await self.run_once()
                delay = 1
            except (OSError, ConnectionError, WebSocketClosed, RpcError,
                    asyncio.TimeoutError, json.JSONDecodeError) as e:
                log.warning("Moonraker websocket: %s", e or type(e).__name__)
            if self._state != "moonraker_down":
                self._state = "moonraker_down"
                self.screen.on_not_ready(self.screen.moonraker_down_message())
            await asyncio.sleep(delay)
            delay = min(delay * 2, 10)
