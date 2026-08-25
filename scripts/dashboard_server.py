#!/usr/bin/env python3
"""Zero-dependency local HTTP server for live and historical runs."""

import argparse
import json
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = PROJECT_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from open_pit_agent.dashboard import (
    DashboardDataError,
    RunRepository,
)
from open_pit_agent.control import (
    ControlError,
    DemoControlManager,
)
from open_pit_agent.adapters.carla_adapter import (
    CarlaAdapterError,
)
from open_pit_agent.config import ConfigError
from open_pit_agent.map_data import CarlaMapRepository


STATIC_FILES = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/index.html": (
        "index.html",
        "text/html; charset=utf-8",
    ),
    "/dashboard.css": (
        "dashboard.css",
        "text/css; charset=utf-8",
    ),
    "/dashboard.js": (
        "dashboard.js",
        "application/javascript; charset=utf-8",
    ),
}


def make_handler(
    repository,
    control_manager,
    map_repository,
    static_root,
):
    class DashboardHandler(BaseHTTPRequestHandler):
        def do_GET(self):
            parsed = urlparse(self.path)
            try:
                if parsed.path == "/api/health":
                    self._json(
                        {
                            "status": "ok",
                            "service": (
                                "open-pit-agent-dashboard"
                            ),
                        }
                    )
                elif parsed.path == "/api/runs":
                    self._json(
                        {"runs": repository.list_runs()}
                    )
                elif parsed.path == "/api/state":
                    query = parse_qs(parsed.query)
                    run_id = _first(query, "run_id")
                    self._json(repository.state(run_id))
                elif parsed.path == "/api/events":
                    query = parse_qs(parsed.query)
                    run_id = _first(query, "run_id")
                    limit = int(
                        _first(query, "limit") or "100"
                    )
                    self._json(
                        {
                            "events": repository.events(
                                run_id, limit
                            )
                        }
                    )
                elif parsed.path == "/api/control/scenarios":
                    self._json(
                        {
                            "scenarios": (
                                control_manager.scenarios()
                            )
                        }
                    )
                elif parsed.path == "/api/control/status":
                    self._json(control_manager.status())
                elif parsed.path == "/api/map":
                    query = parse_qs(parsed.query)
                    refresh = (
                        _first(query, "refresh") == "true"
                    )
                    self._json(
                        map_repository.load(
                            refresh=refresh
                        )
                    )
                elif parsed.path in STATIC_FILES:
                    filename, content_type = STATIC_FILES[
                        parsed.path
                    ]
                    self._static(
                        static_root / filename,
                        content_type,
                    )
                else:
                    self._json(
                        {"error": "not_found"}, status=404
                    )
            except (
                DashboardDataError,
                CarlaAdapterError,
                ConfigError,
                OSError,
                ValueError,
            ) as exc:
                self._json(
                    {"error": str(exc)}, status=400
                )

        def do_POST(self):
            parsed = urlparse(self.path)
            try:
                if parsed.path == "/api/control/start":
                    payload = self._read_json_body()
                    scenario_id = payload.get(
                        "scenario_id"
                    )
                    if not scenario_id:
                        raise ControlError(
                            "scenario_id is required"
                        )
                    self._json(
                        control_manager.start(
                            str(scenario_id)
                        ),
                        status=202,
                    )
                elif parsed.path == "/api/control/stop":
                    self._read_json_body()
                    self._json(
                        control_manager.stop(),
                        status=202,
                    )
                else:
                    self._json(
                        {"error": "not_found"}, status=404
                    )
            except ControlError as exc:
                self._json(
                    {"error": str(exc)}, status=409
                )
            except (OSError, ValueError) as exc:
                self._json(
                    {"error": str(exc)}, status=400
                )

        def _read_json_body(self):
            length = int(
                self.headers.get("Content-Length", "0")
            )
            if length < 0 or length > 4096:
                raise ValueError(
                    "Request body exceeds 4096 bytes"
                )
            if length == 0:
                return {}
            body = self.rfile.read(length)
            payload = json.loads(body.decode("utf-8"))
            if not isinstance(payload, dict):
                raise ValueError(
                    "JSON request body must be an object"
                )
            return payload

        def _json(self, payload, status=200):
            body = json.dumps(
                payload,
                ensure_ascii=False,
                sort_keys=True,
            ).encode("utf-8")
            self.send_response(status)
            self.send_header(
                "Content-Type",
                "application/json; charset=utf-8",
            )
            self.send_header(
                "Cache-Control", "no-store"
            )
            self.send_header(
                "Content-Length", str(len(body))
            )
            self.end_headers()
            self.wfile.write(body)

        def _static(self, path, content_type):
            body = path.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header(
                "Cache-Control", "no-cache"
            )
            self.send_header(
                "Content-Length", str(len(body))
            )
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, message, *args):
            sys.stdout.write(
                "[dashboard] {}\n".format(
                    message % args
                )
            )

    return DashboardHandler


def _first(query, key):
    values = query.get(key, [])
    return values[0] if values else None


def main():
    parser = argparse.ArgumentParser(
        description="Local visualization data service"
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument(
        "--artifacts",
        type=Path,
        default=PROJECT_ROOT / "artifacts" / "runs",
    )
    args = parser.parse_args()
    if args.host not in {
        "127.0.0.1",
        "localhost",
        "::1",
    }:
        raise SystemExit(
            "ERROR: control service may only listen on localhost"
        )

    repository = RunRepository(args.artifacts)
    control_manager = DemoControlManager(
        project_root=PROJECT_ROOT,
        artifacts_root=args.artifacts,
    )
    map_repository = CarlaMapRepository(
        PROJECT_ROOT / "configs" / "town03.json"
    )
    handler = make_handler(
        repository,
        control_manager,
        map_repository,
        PROJECT_ROOT / "dashboard",
    )
    server = ThreadingHTTPServer(
        (args.host, args.port), handler
    )
    print(
        "可视化服务已启动：http://{}:{}".format(
            args.host, args.port
        )
    )
    print(
        "数据目录：{}".format(
            repository.artifacts_root
        )
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n正在关闭可视化服务")
    finally:
        control_manager.close()
        server.server_close()


if __name__ == "__main__":
    main()
