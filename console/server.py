"""Local-only HTTP server for the minimal Control Console."""

from __future__ import annotations

import argparse
import json
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from core.task_store import TaskNotFound

from .app import ControlConsole, WorkspaceBlocked, build_default_console

STATIC = Path(__file__).resolve().parent / "static"


def make_server(app: ControlConsole, *, host: str = "127.0.0.1", port: int = 8765) -> ThreadingHTTPServer:
    class Handler(BaseHTTPRequestHandler):
        server_version = "ACPConsole/0.1"

        def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
            return

        def do_GET(self) -> None:  # noqa: N802
            path = urlparse(self.path).path
            if path in ("/", "/index.html"):
                self._file(STATIC / "index.html", "text/html; charset=utf-8")
                return
            if path == "/api/capabilities":
                self._json({"capabilities": app.capabilities()})
                return
            if path.startswith("/api/tasks/"):
                parts = path.strip("/").split("/")
                if len(parts) == 3:
                    self._state(parts[2])
                    return
                if len(parts) == 4 and parts[3] == "timeline":
                    self._json({"timeline": app.timeline(parts[2])})
                    return
            self._error(HTTPStatus.NOT_FOUND, "NOT_FOUND", "unknown path")

        def do_POST(self) -> None:  # noqa: N802
            path = urlparse(self.path).path
            try:
                body = self._body()
                if path == "/api/workspaces/verify":
                    self._verify(body)
                    return
                if path == "/api/tasks":
                    self._create(body)
                    return
                if path.startswith("/api/tasks/"):
                    parts = path.strip("/").split("/")
                    if len(parts) == 4 and parts[3] == "cancel":
                        self._json({"cancelled": app.cancel_task(parts[2])})
                        return
                    if len(parts) == 4 and parts[3] == "continue":
                        app.continue_task(parts[2])
                        self._json({"ok": True})
                        return
                    if len(parts) == 4 and parts[3] == "human-decision":
                        decision = app.decide(
                            parts[2],
                            str(body.get("action", "")),
                            body.get("instruction"),
                        )
                        self._json({"decision": decision})
                        return
            except WorkspaceBlocked as exc:
                self._json(
                    {"error": "WORKSPACE_BLOCKED", "verification": exc.verification.to_dict()},
                    HTTPStatus.CONFLICT,
                )
                return
            except TaskNotFound as exc:
                self._error(HTTPStatus.NOT_FOUND, "TASK_NOT_FOUND", str(exc))
                return
            except ValueError as exc:
                self._error(HTTPStatus.BAD_REQUEST, "BAD_REQUEST", str(exc))
                return
            except Exception as exc:  # noqa: BLE001
                self._error(HTTPStatus.INTERNAL_SERVER_ERROR, type(exc).__name__, str(exc))
                return
            self._error(HTTPStatus.NOT_FOUND, "NOT_FOUND", "unknown path")

        # ------------------------------------------------------------- handlers

        def _verify(self, body: dict[str, Any]) -> None:
            path = str(body.get("path", "")).strip()
            if not path:
                raise ValueError("path is required")
            binding, verification = app.verify_workspace(
                path,
                branch=body.get("branch") or None,
                tracking_ref=body.get("tracking_ref") or None,
                baseline_commit=body.get("baseline_commit") or None,
                mode=str(body.get("mode", "read_only")),
            )
            self._json({
                "binding": binding.to_payload() if binding else None,
                "verification": verification.to_dict(),
            })

        def _create(self, body: dict[str, Any]) -> None:
            binding = body.get("binding")
            if not isinstance(binding, dict):
                raise ValueError("binding is required")
            task_id = app.create_task(
                objective=str(body.get("objective", "")),
                binding_payload=binding,
                mode=str(body.get("mode", "read_only")),
                capability_id=str(body.get("capability_id", "")),
            )
            self._json({"task_id": task_id})

        def _state(self, task_id: str) -> None:
            self._json(app.state(task_id))

        def _body(self) -> dict[str, Any]:
            length = int(self.headers.get("Content-Length", "0") or 0)
            if length <= 0:
                return {}
            raw = self.rfile.read(length)
            try:
                data = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise ValueError("invalid JSON body") from exc
            if not isinstance(data, dict):
                raise ValueError("JSON body must be an object")
            return data

        def _json(self, payload: Any, status: HTTPStatus = HTTPStatus.OK) -> None:
            encoded = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(encoded)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(encoded)

        def _file(self, path: Path, content_type: str) -> None:
            try:
                data = path.read_bytes()
            except OSError:
                self._error(HTTPStatus.NOT_FOUND, "NOT_FOUND", "missing static asset")
                return
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _error(self, status: HTTPStatus, code: str, message: str) -> None:
            self._json({"error": code, "message": message}, status)

    server = ThreadingHTTPServer((host, port), Handler)
    server.daemon_threads = True
    return server


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="AI Control Plane local console")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--data-dir", default=None)
    args = parser.parse_args(argv)

    app = build_default_console(args.data_dir)
    server = make_server(app, port=args.port)
    url = f"http://127.0.0.1:{server.server_address[1]}"
    print(f"AI Control Plane Console: {url}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
