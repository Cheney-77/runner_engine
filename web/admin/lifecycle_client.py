from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any


class LifecycleUnavailable(RuntimeError):
    pass


class LifecycleRejected(RuntimeError):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


@dataclass(frozen=True)
class OwnerEndpoint:
    base_url: str
    token: str

    @property
    def enabled(self) -> bool:
        return bool(self.base_url and self.token)


class LifecycleClient:
    def __init__(
        self,
        *,
        build: OwnerEndpoint,
        publish: OwnerEndpoint,
        runner: OwnerEndpoint,
        timeout_seconds: float = 20.0,
    ):
        self.build = build
        self.publish = publish
        self.runner = runner
        self.timeout_seconds = timeout_seconds

    @classmethod
    def environment(cls) -> "LifecycleClient":
        return cls(
            build=OwnerEndpoint(
                os.environ.get("ADMIN_BUILD_SERVICE_URL", "").rstrip("/"),
                os.environ.get("ADMIN_BUILD_ADMIN_TOKEN", ""),
            ),
            publish=OwnerEndpoint(
                os.environ.get("ADMIN_PUBLISH_SERVICE_URL", "").rstrip("/"),
                os.environ.get("ADMIN_PUBLISH_ADMIN_TOKEN", ""),
            ),
            runner=OwnerEndpoint(
                os.environ.get("ADMIN_RUNNER_ADMIN_URL", "").rstrip("/"),
                os.environ.get("ADMIN_RUNNER_ADMIN_TOKEN", ""),
            ),
        )

    @property
    def enabled(self) -> bool:
        return self.build.enabled and self.publish.enabled and self.runner.enabled

    def _request(
        self,
        endpoint: OwnerEndpoint,
        method: str,
        path: str,
        body: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if not endpoint.enabled:
            raise LifecycleUnavailable("owner-service lifecycle endpoint is not configured")

        raw = None if body is None else json.dumps(body).encode("utf-8")
        headers = {
            "Accept": "application/json",
            "Authorization": f"Bearer {endpoint.token}",
        }
        if raw is not None:
            headers["Content-Type"] = "application/json"

        request = urllib.request.Request(
            endpoint.base_url + path,
            data=raw,
            method=method,
            headers=headers,
        )

        try:
            with urllib.request.urlopen(
                request,
                timeout=self.timeout_seconds,
            ) as response:
                payload = json.loads(response.read() or b"{}")
                if not isinstance(payload, dict):
                    raise LifecycleUnavailable(
                        "owner service returned a non-object response"
                    )
                return payload
        except urllib.error.HTTPError as exc:
            raw_detail = exc.read(8192)
            try:
                detail = json.loads(raw_detail or b"{}")
            except json.JSONDecodeError:
                detail = {"detail": raw_detail.decode("utf-8", "replace")}
            message = detail.get("detail") if isinstance(detail, dict) else str(detail)
            raise LifecycleRejected(exc.code, str(message)) from exc
        except (urllib.error.URLError, TimeoutError) as exc:
            raise LifecycleUnavailable(str(exc)) from exc

    def build_plan(self, environment_id: str) -> dict[str, Any]:
        return self._request(
            self.build,
            "GET",
            f"/v1/admin/runtime-environments/{urllib.parse.quote(environment_id)}/cleanup-plan",
        )

    def build_retire(self, environment_id: str, *, reason: str) -> dict[str, Any]:
        return self._request(
            self.build,
            "POST",
            f"/v1/admin/runtime-environments/{urllib.parse.quote(environment_id)}/retire",
            {"reason": reason},
        )

    def build_cancel_retirement(self, environment_id: str) -> dict[str, Any]:
        return self._request(
            self.build,
            "POST",
            f"/v1/admin/runtime-environments/{urllib.parse.quote(environment_id)}/cancel-retirement",
            {},
        )

    def build_delete_artifact(
        self,
        environment_id: str,
        *,
        expected_image_ref: str,
    ) -> dict[str, Any]:
        return self._request(
            self.build,
            "DELETE",
            f"/v1/admin/runtime-environments/{urllib.parse.quote(environment_id)}/artifact",
            {"expectedImageRef": expected_image_ref},
        )

    def publish_begin_retirement(
        self,
        *,
        runtime_env_key: str,
        image_ref: str,
        reason: str,
    ) -> dict[str, Any]:
        return self._request(
            self.publish,
            "POST",
            "/v1/admin/runner-runtimes/retire",
            {
                "runtimeEnvKey": runtime_env_key,
                "imageRef": image_ref,
                "reason": reason,
            },
        )

    def publish_cancel_retirement(self, *, runtime_env_key: str) -> dict[str, Any]:
        return self._request(
            self.publish,
            "POST",
            "/v1/admin/runner-runtimes/cancel-retirement",
            {"runtimeEnvKey": runtime_env_key},
        )

    def publish_finalize_retirement(self, *, runtime_env_key: str) -> dict[str, Any]:
        return self._request(
            self.publish,
            "POST",
            "/v1/admin/runner-runtimes/finalize-retirement",
            {"runtimeEnvKey": runtime_env_key},
        )

    def runner_status(self, image_ref: str) -> dict[str, Any]:
        query = urllib.parse.urlencode({"imageRef": image_ref})
        return self._request(
            self.runner,
            "GET",
            f"/v1/admin/runtime-images/status?{query}",
        )

    def runner_begin_retirement(self, image_ref: str, *, reason: str) -> dict[str, Any]:
        return self._request(
            self.runner,
            "POST",
            "/v1/admin/runtime-images/retire",
            {"imageRef": image_ref, "reason": reason},
        )

    def runner_cancel_retirement(self, image_ref: str) -> dict[str, Any]:
        return self._request(
            self.runner,
            "POST",
            "/v1/admin/runtime-images/cancel-retirement",
            {"imageRef": image_ref},
        )

    def runner_finalize_retirement(self, image_ref: str) -> dict[str, Any]:
        return self._request(
            self.runner,
            "POST",
            "/v1/admin/runtime-images/finalize-retirement",
            {"imageRef": image_ref},
        )
