from __future__ import annotations

import hmac
import json
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any


MAX_BODY = 256 * 1024


class RunnerAdminHTTP:
    def __init__(self, service, *, host: str, port: int, token: str):
        if not token:
            raise ValueError("RUNNER_ADMIN_TOKEN is required")

        self.service = service
        self.host = host
        self.port = port
        self.token = token
        self.server = ThreadingHTTPServer((host, port), self._handler())
        self.thread: threading.Thread | None = None

    def _handler(self):
        owner = self

        class Handler(BaseHTTPRequestHandler):
            server_version = "ManagedPythonRunnerAdmin/1"

            def log_message(self, format, *args):
                return

            def _authorized(self) -> bool:
                value = self.headers.get("Authorization", "")
                return (
                    value.startswith("Bearer ")
                    and hmac.compare_digest(value[7:], owner.token)
                )

            def _json(self, status: int, payload: dict[str, Any]) -> None:
                body = json.dumps(
                    payload,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                    default=str,
                ).encode("utf-8")

                self.send_response(status)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(body)

            def _payload(self) -> dict[str, Any]:
                size = int(self.headers.get("Content-Length", "0"))
                if size < 0 or size > MAX_BODY:
                    raise ValueError("request body too large")

                value = json.loads(self.rfile.read(size) or b"{}")
                if not isinstance(value, dict):
                    raise ValueError("request body must be an object")
                return value

            def _query(self) -> dict[str, str]:
                parsed = urllib.parse.urlsplit(self.path)
                values = urllib.parse.parse_qs(parsed.query)
                return {key: items[-1] for key, items in values.items() if items}

            def do_GET(self):
                if not self._authorized():
                    self._json(401, {"detail": "unauthorized"})
                    return

                path = urllib.parse.urlsplit(self.path).path
                try:
                    if path == "/health":
                        self._json(200, {"ok": True, "service": "runner-admin"})
                        return

                    if path == "/v1/observe/runtime":
                        self._json(200, owner.service.runtime_snapshot())
                        return

                    if path == "/v1/admin/runtime-images/status":
                        image_ref = self._query().get("imageRef", "")
                        if not image_ref:
                            raise ValueError("imageRef is required")
                        self._json(
                            200,
                            owner.service.runtime_image_status(image_ref),
                        )
                        return

                    self._json(404, {"detail": "not found"})
                except ValueError as exc:
                    self._json(400, {"detail": str(exc)})
                except Exception as exc:
                    self._json(
                        500,
                        {"detail": f"{type(exc).__name__}: {exc}"},
                    )

            def do_POST(self):
                if not self._authorized():
                    self._json(401, {"detail": "unauthorized"})
                    return

                path = urllib.parse.urlsplit(self.path).path
                try:
                    payload = self._payload()

                    if path == "/v1/admin/runtime-images/retire":
                        image_ref = str(payload.get("imageRef") or "")
                        reason = str(payload.get("reason") or "")
                        if not image_ref or len(reason.strip()) < 5:
                            raise ValueError(
                                "imageRef and a meaningful reason are required"
                            )
                        self._json(
                            200,
                            owner.service.begin_runtime_retirement(
                                image_ref,
                                reason=reason.strip(),
                            ),
                        )
                        return

                    if path == "/v1/admin/runtime-images/cancel-retirement":
                        image_ref = str(payload.get("imageRef") or "")
                        if not image_ref:
                            raise ValueError("imageRef is required")
                        self._json(
                            200,
                            owner.service.cancel_runtime_retirement(image_ref),
                        )
                        return

                    if path == "/v1/admin/runtime-images/finalize-retirement":
                        image_ref = str(payload.get("imageRef") or "")
                        if not image_ref:
                            raise ValueError("imageRef is required")
                        self._json(
                            200,
                            owner.service.finalize_runtime_retirement(image_ref),
                        )
                        return

                    if path == "/v1/admin/sandboxes/retire-idle":
                        worker_id = str(payload.get("workerId") or "")
                        if not worker_id:
                            raise ValueError("workerId is required")
                        self._json(
                            200,
                            owner.service.retire_idle_worker(worker_id),
                        )
                        return

                    self._json(404, {"detail": "not found"})
                except ValueError as exc:
                    self._json(400, {"detail": str(exc)})
                except Exception as exc:
                    self._json(
                        409,
                        {"detail": f"{type(exc).__name__}: {exc}"},
                    )

        return Handler

    def start(self) -> None:
        if self.thread is not None:
            return

        self.thread = threading.Thread(
            target=self.server.serve_forever,
            name="runner-admin-http",
            daemon=True,
        )
        self.thread.start()

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        if self.thread is not None:
            self.thread.join(timeout=5)
            self.thread = None
