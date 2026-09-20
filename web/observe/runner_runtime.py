from __future__ import annotations

import json
import os
import urllib.error
import urllib.request


class RunnerRuntimeReader:
    def __init__(self, base_url: str, token: str, *, timeout_seconds: float = 3.0):
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.timeout_seconds = timeout_seconds

    @classmethod
    def environment(cls) -> "RunnerRuntimeReader":
        return cls(
            os.environ.get("OBS_RUNNER_ADMIN_URL", ""),
            os.environ.get("OBS_RUNNER_ADMIN_TOKEN", ""),
        )

    @property
    def enabled(self) -> bool:
        return bool(self.base_url and self.token)

    def report(self) -> dict:
        if not self.enabled:
            return {
                "status": "unconfigured",
                "message": (
                    "OBS_RUNNER_ADMIN_URL / OBS_RUNNER_ADMIN_TOKEN not configured"
                ),
            }

        request = urllib.request.Request(
            self.base_url + "/v1/observe/runtime",
            headers={
                "Accept": "application/json",
                "Authorization": f"Bearer {self.token}",
            },
        )

        try:
            with urllib.request.urlopen(
                request,
                timeout=self.timeout_seconds,
            ) as response:
                payload = json.loads(response.read() or b"{}")
                if not isinstance(payload, dict):
                    raise ValueError("runner runtime response is not an object")
                payload["status"] = "ok"
                return payload
        except urllib.error.HTTPError as exc:
            return {
                "status": "error",
                "message": f"Runner admin endpoint returned HTTP {exc.code}",
            }
        except Exception as exc:
            return {
                "status": "error",
                "message": f"{type(exc).__name__}: {exc}",
            }
