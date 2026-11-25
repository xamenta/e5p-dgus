# Minimal Moonraker HTTP client (stdlib only)
#
# Part of e5p-dgus. GPLv3.
import json
import logging
import urllib.error
import urllib.parse
import urllib.request

log = logging.getLogger("e5p_dgus.moonraker")


class MoonrakerError(Exception):
    pass


class Moonraker:
    def __init__(self, url, api_key=None, timeout=5.0):
        self.url = url.rstrip("/")
        self.api_key = api_key
        self.timeout = timeout

    def _request(self, method, path, params=None, body=None, timeout=-1):
        url = self.url + path
        if params:
            url += "?" + urllib.parse.urlencode(params, safe=",")
        data = None
        headers = {"Accept": "application/json"}
        if body is not None:
            data = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        if self.api_key:
            headers["X-Api-Key"] = self.api_key
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        if timeout == -1:
            timeout = self.timeout
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                payload = json.loads(resp.read().decode() or "{}")
        except urllib.error.HTTPError as e:
            msg = "HTTP %d" % e.code
            try:
                err = json.loads(e.read().decode())
                msg = err.get("error", {}).get("message", msg)
            except Exception:
                pass
            raise MoonrakerError(msg)
        except (urllib.error.URLError, OSError) as e:
            raise MoonrakerError("Moonraker unreachable: %s" % (e,))
        return payload.get("result", payload)

    # -- endpoints -----------------------------------------------------------
    def server_info(self):
        return self._request("GET", "/server/info")

    def printer_info(self):
        return self._request("GET", "/printer/info")

    def objects_list(self):
        return self._request("GET", "/printer/objects/list").get("objects", [])

    def query(self, objects):
        """objects: dict name -> list of fields (or None for all)."""
        params = {}
        for name, fields in objects.items():
            params[name] = ",".join(fields) if fields else ""
        res = self._request("GET", "/printer/objects/query", params)
        return res.get("status", {})

    def gcode_store(self, count=100):
        return self._request("GET", "/server/gcode_store",
                             {"count": count}).get("gcode_store", [])

    def files_list(self, root="gcodes"):
        return self._request("GET", "/server/files/list", {"root": root})

    def file_metadata(self, filename):
        return self._request("GET", "/server/files/metadata", {"filename": filename})

    def print_start(self, filename):
        return self._request("POST", "/printer/print/start", body={"filename": filename})

    def emergency_stop(self):
        return self._request("POST", "/printer/emergency_stop")

    def firmware_restart(self):
        return self._request("POST", "/printer/firmware_restart", timeout=30)

    def restart_service(self, service="klipper"):
        return self._request("POST", "/machine/services/restart", body={"service": service})

    def gcode(self, script):
        # G-code requests block until the commands finish (G28, PID tuning,
        # bed meshing...), so no timeout here.
        return self._request("POST", "/printer/gcode/script",
                             body={"script": script}, timeout=None)
