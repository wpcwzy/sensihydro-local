"""JSON HTTP API + web remote-control UI for the indoor units."""

from __future__ import annotations

import json
import logging
import pathlib
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlparse

from . import hostmode, units
from .relay import DtuBridge, ModbusTimeoutError, NotConnectedError
from .state import StateStore

_LOGGER = logging.getLogger(__name__)

WEB_DIR = pathlib.Path(__file__).resolve().parent / "web"
STATE_CACHE_TTL = 1.5  # kept for backwards compatibility


class SensiHydroApi:
    """Request handler implementation, shared by all HTTP worker threads."""

    def __init__(self, bridge: DtuBridge, store: StateStore) -> None:
        self.bridge = bridge
        self.store = store

    # ------------------------------------------------------------------ state

    def get_state(self) -> dict:
        """Return the cached snapshot (the background worker keeps it fresh)."""
        state, _host, _age = self.store.get()
        return state

    def get_host_mode(self) -> dict:
        _state, host, _age = self.store.get()
        return host or hostmode.read_host_mode(self.bridge)

    def invalidate(self) -> None:
        self.store.invalidate()

    # ---------------------------------------------------------------- routing

    def handle(self, handler: "Handler") -> None:
        parsed = urlparse(handler.path)
        path = parsed.path.rstrip("/") or "/"
        handler.query_params = parse_qs(parsed.query)

        if handler.command == "GET" and path in ("/", "/index.html", "/ui"):
            return handler.send_web()

        try:
            if path in ("/health", "/api/status"):
                return handler.send_json({"ok": True, **self.bridge.status()})

            if path == "/api/mapping":
                try:
                    state, _host, _age = self.store.get()
                except Exception:  # noqa: BLE001 - still report the mapping
                    state = {}
                entries = []
                for item in units.mapping():
                    unit = state.get(item["name"]) or {}
                    firmware = unit.get("firmware") or {}
                    entries.append(
                        {
                            **item,
                            "identity_code": [
                                firmware.get("code_high"),
                                firmware.get("code_middle"),
                                firmware.get("code_low"),
                            ],
                            "room_temperature": unit.get("room_temperature"),
                        }
                    )
                return handler.send_json({"ok": True, "units": entries})

            if path == "/api/hostmode":
                if handler.command == "GET":
                    return handler.send_json(
                        {"ok": True, "hostmode": self.get_host_mode()}
                    )
                body = handler.read_json()
                if "mode" in body and body["mode"] is not None:
                    result = hostmode.set_host_mode(self.bridge, str(body["mode"]))
                elif any(k in body for k in ("quiet", "eco", "boost")):
                    current = hostmode.read_host_mode(self.bridge)
                    result = hostmode.write_host_mode(
                        self.bridge,
                        bool(body.get("quiet", current["quiet"])),
                        bool(body.get("eco", current["eco"])),
                        bool(body.get("boost", current["boost"])),
                    )
                else:
                    raise ValueError("provide 'mode' or quiet/eco/boost flags")
                self.invalidate()
                return handler.send_json({"ok": True, "hostmode": result})

            if path == "/api/units":
                return handler.send_json({"ok": True, "units": list(units.UNITS)})

            if path == "/api/state":
                data = self.get_state()
                return handler.send_json({"ok": True, "units": data})

            if path.startswith("/api/unit/"):
                return self._handle_unit(handler, path)

            if path == "/api/read":
                addr = int(handler.query_params.get("addr", ["0"])[0], 0)
                count = int(
                    handler.query_params.get("count", handler.query_params.get("cnt", ["1"]))[0]
                )
                values = self.bridge.read_registers(addr, count)
                return handler.send_json(
                    {"ok": True, "addr": "0x%04x" % addr, "values": values}
                )

            if path == "/api/write":
                body = handler.read_json()
                addr = int(body["addr"], 0) if isinstance(body["addr"], str) else int(body["addr"])
                value = int(body["value"], 0) if isinstance(body["value"], str) else int(body["value"])
                self.bridge.write_register(addr, value)
                self.invalidate()
                return handler.send_json({"ok": True})

            if path == "/api/relay/reset":
                handler.send_json({"ok": True})
                self.bridge.drop_connection()
                return

            return handler.send_json({"error": "not found", "path": path}, 404)

        except KeyError as err:
            return handler.send_json({"ok": False, "error": str(err)}, 404)
        except NotConnectedError as err:
            return handler.send_json({"ok": False, "error": str(err)}, 503)
        except (ModbusTimeoutError, TimeoutError) as err:
            return handler.send_json({"ok": False, "error": str(err)}, 504)
        except Exception as err:  # noqa: BLE001 - API must never crash
            _LOGGER.exception("HTTP API error on %s %s", handler.command, path)
            return handler.send_json({"ok": False, "error": str(err)}, 500)

    # ------------------------------------------------------------- unit logic

    def _handle_unit(self, handler: "Handler", path: str) -> None:
        rest = unquote(path[len("/api/unit/") :]).strip("/")
        parts = [p for p in rest.split("/") if p]
        if not parts:
            return handler.send_json({"ok": False, "error": "unit name required"}, 400)
        name = parts[0]
        action = parts[1] if len(parts) > 1 else None

        if handler.command == "GET" and action is None:
            state, _host, _age = self.store.get()
            unit = state.get(units.resolve(name).name)
            if unit is None:
                unit = units.read_unit(self.bridge, name)
            return handler.send_json({"ok": True, "unit": unit})

        body = handler.read_json()

        if action == "target":
            value = body.get("temperature", body.get("target"))
            if value is None:
                raise ValueError("temperature is required")
            unit = units.set_target(self.bridge, name, float(value))
        elif action == "power":
            if "on" not in body:
                raise ValueError("on (bool) is required")
            unit = units.set_power(self.bridge, name, bool(body["on"]))
        elif action == "mode":
            mode = body.get("mode")
            if isinstance(mode, str):
                if mode.isdigit():
                    mode = int(mode)
                else:
                    key = mode.strip().lower()
                    if key not in units.MODE_BY_NAME:
                        raise ValueError(
                            "unknown mode %r (use 0..5 or %s)"
                            % (mode, ", ".join(units.MODE_BY_NAME))
                        )
                    mode = units.MODE_BY_NAME[key]
            if mode is None:
                raise ValueError("mode is required")
            unit = units.set_mode(self.bridge, name, int(mode))
        elif action == "fan":
            auto = bool(body.get("auto", False))
            speed = body.get("speed", body.get("fan_speed"))
            unit = units.set_fan(
                self.bridge, name, speed=None if auto else int(speed), auto=auto
            )
        else:
            return handler.send_json(
                {"ok": False, "error": "unknown action %r" % (action,)}, 404
            )

        self.invalidate()
        return handler.send_json({"ok": True, "unit": unit})


class Handler(BaseHTTPRequestHandler):
    """Threaded HTTP handler with JSON helpers."""

    api: SensiHydroApi
    server_version = "SensiHydroBridge/1.0"
    query_params: dict = {}

    def log_message(self, fmt: str, *args) -> None:  # noqa: A003
        _LOGGER.debug("HTTP %s - %s", self.address_string(), fmt % args)

    # ------------------------------------------------------------- primitives

    def read_json(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return {}
        raw = self.rfile.read(length)
        return json.loads(raw.decode("utf-8") or "{}") if raw else {}

    def send_json(self, payload: dict, code: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def send_web(self) -> None:
        index = WEB_DIR / "index.html"
        try:
            body = index.read_bytes()
        except OSError:
            return self.send_json({"error": "web ui missing"}, 500)
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(body)

    # ------------------------------------------------------------------ verbs

    def do_GET(self) -> None:  # noqa: N802
        self.api.handle(self)

    def do_POST(self) -> None:  # noqa: N802
        self.api.handle(self)

    def do_PUT(self) -> None:  # noqa: N802
        self.api.handle(self)


def create_server(bridge: DtuBridge, host: str, port: int) -> ThreadingHTTPServer:
    store = StateStore(bridge)
    store.start()
    api = SensiHydroApi(bridge, store)

    class BoundHandler(Handler):
        pass

    BoundHandler.api = api
    server = ThreadingHTTPServer((host, port), BoundHandler)
    server.daemon_threads = True
    server.state_store = store  # type: ignore[attr-defined]
    return server
