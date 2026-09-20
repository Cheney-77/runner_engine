from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from ..errors import SandboxError
from ..model import OperatorRelease, RunRequest, RunResult, SecurityPolicy, Worker
from ..worker.http_api import call as worker_call


CONTROL_REQUEST_TIMEOUT_SECONDS = 30.0
SANDBOX_READY_WAIT_SECONDS = 60.0
AGENT_READY_WAIT_SECONDS = 20.0
AGENT_HEALTH_REQUEST_SECONDS = 1.0
BOOTSTRAP_REQUEST_TIMEOUT_SECONDS = 30.0
CANCEL_REQUEST_TIMEOUT_SECONDS = 5.0
DESTROY_REQUEST_TIMEOUT_SECONDS = 10.0
RUN_TRANSPORT_GRACE_SECONDS = 10.0


class OpenSandboxBackend:
    """
    OpenSandbox adapter for the current Docker + gVisor + direct-ingress deployment.
    """

    MANAGED_BY = "managed-python-runner-v3.3"

    def __init__(
        self,
        clusters: dict[str, dict[str, str]],
        *,
        owner_id: str,
        agent_port: int = 9080,
    ):
        if not owner_id:
            raise ValueError("owner_id is required")

        self.clusters = clusters
        self.owner_id = owner_id
        self.agent_port = agent_port

    def _cluster(self, name: str) -> dict[str, str]:
        try:
            return self.clusters[name]
        except KeyError as exc:
            raise SandboxError(
                "UNKNOWN_SANDBOX_CLUSTER",
                f"no OpenSandbox cluster named {name}",
            ) from exc

    def _request(
        self,
        cluster: str,
        method: str,
        path: str,
        payload: dict | None = None,
        *,
        timeout_seconds: float = CONTROL_REQUEST_TIMEOUT_SECONDS,
    ) -> Any:
        config = self._cluster(cluster)
        url = config["url"].rstrip("/") + path
        body = (
            None
            if payload is None
            else json.dumps(payload, separators=(",", ":")).encode("utf-8")
        )

        headers = {"Accept": "application/json"}
        if body is not None:
            headers["Content-Type"] = "application/json"
        if config.get("api_key"):
            headers["OPEN-SANDBOX-API-KEY"] = config["api_key"]

        request = urllib.request.Request(
            url,
            data=body,
            method=method,
            headers=headers,
        )

        try:
            with urllib.request.urlopen(
                request,
                timeout=timeout_seconds,
            ) as response:
                raw = response.read(16 * 1024 * 1024)
                return json.loads(raw or b"{}")
        except urllib.error.HTTPError as exc:
            detail = exc.read(4096).decode("utf-8", "replace")
            raise SandboxError(
                "OPENSANDBOX_HTTP",
                f"{method} {path}: HTTP {exc.code}: {detail}",
                retryable=True,
            ) from exc
        except (urllib.error.URLError, TimeoutError) as exc:
            raise SandboxError(
                "OPENSANDBOX_UNREACHABLE",
                f"{method} {path}: {exc}",
                retryable=True,
            ) from exc

    @staticmethod
    def _sandbox_id(value: dict) -> str:
        for key in ("id", "sandboxId", "sandbox_id"):
            if value.get(key):
                return str(value[key])

        if isinstance(value.get("sandbox"), dict):
            return OpenSandboxBackend._sandbox_id(value["sandbox"])

        raise SandboxError(
            "OPENSANDBOX_PROTOCOL",
            "create response did not contain a sandbox id",
        )

    @staticmethod
    def _state(value: dict) -> str:
        status = value.get("status")
        if isinstance(status, dict) and status.get("state"):
            return str(status["state"]).upper()
        if isinstance(status, str):
            return status.upper()
        if value.get("state"):
            return str(value["state"]).upper()
        return ""

    @staticmethod
    def _label_hash(value: str) -> str:
        return hashlib.sha256(value.encode("utf-8")).hexdigest()[:32]

    def create(
        self,
        *,
        tenant_id: str,
        release: OperatorRelease,
        policy: SecurityPolicy,
        orphan_ttl_seconds: int,
    ) -> Worker:
        cluster = policy.sandbox_cluster
        token = secrets.token_urlsafe(32)

        payload: dict[str, Any] = {
            "image": {"uri": release.runtime.image},
            "timeout": orphan_ttl_seconds,
            "resourceLimits": {
                "cpu": policy.cpu,
                "memory": policy.memory,
            },
            "env": {
                "RUNNER_AGENT_TOKEN": token,
                "RUNNER_AGENT_PORT": str(self.agent_port),
                "RUNNER_RELEASES_ROOT": "/opt/runner/releases",
                "RUNNER_INVOCATIONS_ROOT": "/opt/runner/invocations",
                "RUNNER_DEPENDENCY_ROOT": "/opt/python-deps",
            },
            "entrypoint": [
                "python",
                "-m",
                "runner_engine.worker.agent",
                "--listen",
                "0.0.0.0",
                "--port",
                str(self.agent_port),
            ],
            "metadata": {
                "managed-by": self.MANAGED_BY,
                "runner-owner": self._label_hash(self.owner_id),
                "tenant": self._label_hash(tenant_id),
                "runtime": release.runtime.id[:32],
                "profile": self._label_hash(release.profile),
            },
        }

        created = self._request(
            cluster,
            "POST",
            "/v1/sandboxes",
            payload,
        )
        sandbox_id = self._sandbox_id(created)

        try:
            ready_deadline = time.monotonic() + SANDBOX_READY_WAIT_SECONDS

            while time.monotonic() < ready_deadline:
                status = self._request(
                    cluster,
                    "GET",
                    f"/v1/sandboxes/{urllib.parse.quote(sandbox_id)}",
                )
                state = self._state(status)

                if state in {"RUNNING", "READY", "STARTED"}:
                    break

                if state in {"FAILED", "STOPPED", "ERROR", "TERMINATED"}:
                    raise SandboxError(
                        "SANDBOX_START_FAILED",
                        f"sandbox entered state {state}",
                        retryable=True,
                    )

                time.sleep(0.2)
            else:
                raise SandboxError(
                    "SANDBOX_START_TIMEOUT",
                    "sandbox did not become ready",
                    retryable=True,
                )

            endpoint_data = self._request(
                cluster,
                "GET",
                (
                    f"/v1/sandboxes/{urllib.parse.quote(sandbox_id)}"
                    f"/endpoints/{self.agent_port}"
                ),
            )
            endpoint = endpoint_data.get("endpoint") or endpoint_data.get("url")
            if not endpoint:
                raise SandboxError(
                    "SANDBOX_ENDPOINT_MISSING",
                    "OpenSandbox endpoint response had no endpoint",
                )

            endpoint = str(endpoint).strip()
            if "://" not in endpoint:
                endpoint = "http://" + endpoint

            now = time.monotonic()
            worker = Worker(
                id=sandbox_id,
                endpoint=endpoint,
                token=token,
                tenant_id=tenant_id,
                runtime_id=release.runtime.id,
                profile=release.profile,
                backend_group=cluster,
                created_monotonic=now,
                last_used_monotonic=now,
                sandbox_expires_monotonic=now + orphan_ttl_seconds,
                metadata={
                    "endpoint_headers": dict(
                        endpoint_data.get("headers") or {}
                    )
                },
            )

            last_error: Exception | None = None
            health_deadline = time.monotonic() + AGENT_READY_WAIT_SECONDS

            while time.monotonic() < health_deadline:
                try:
                    response = worker_call(
                        worker.endpoint,
                        worker.token,
                        "/health",
                        timeout_s=AGENT_HEALTH_REQUEST_SECONDS,
                        extra_headers=worker.metadata.get("endpoint_headers"),
                    )
                    if response.get("ok"):
                        return worker

                    last_error = RuntimeError(
                        f"unexpected health response: {response}"
                    )
                except Exception as exc:
                    last_error = exc

                time.sleep(0.2)

            detail = "trusted agent did not become healthy"
            if last_error is not None:
                detail += f": {type(last_error).__name__}: {last_error}"

            raise SandboxError(
                "AGENT_START_TIMEOUT",
                detail,
                retryable=True,
            )

        except Exception:
            try:
                self._request(
                    cluster,
                    "DELETE",
                    f"/v1/sandboxes/{urllib.parse.quote(sandbox_id)}",
                    timeout_seconds=DESTROY_REQUEST_TIMEOUT_SECONDS,
                )
            except Exception:
                pass
            raise

    def renew(self, worker: Worker, *, orphan_ttl_seconds: int) -> None:
        expires_at = datetime.now(timezone.utc) + timedelta(
            seconds=orphan_ttl_seconds
        )

        self._request(
            worker.backend_group,
            "POST",
            (
                f"/v1/sandboxes/{urllib.parse.quote(worker.id)}"
                "/renew-expiration"
            ),
            {
                "expiresAt": expires_at.isoformat().replace("+00:00", "Z")
            },
        )
        worker.sandbox_expires_monotonic = (
            time.monotonic() + orphan_ttl_seconds
        )

    def install(self, worker: Worker, release: OperatorRelease) -> None:
        artifact = Path(release.artifact_path).read_bytes()
        worker_call(
            worker.endpoint,
            worker.token,
            "/bootstrap",
            payload={
                "release_id": release.id,
                "artifact_sha256": release.artifact_sha256,
                "artifact_b64": base64.b64encode(artifact).decode("ascii"),
            },
            timeout_s=BOOTSTRAP_REQUEST_TIMEOUT_SECONDS,
            extra_headers=worker.metadata.get("endpoint_headers"),
        )

    def run(
        self,
        worker: Worker,
        release: OperatorRelease,
        request: RunRequest,
        *,
        timeout_ms: int,
    ) -> RunResult:
        response = worker_call(
            worker.endpoint,
            worker.token,
            "/run",
            payload={
                "release_id": release.id,
                "invocation_id": request.invocation_id,
                "content_b64": base64.b64encode(request.content).decode("ascii"),
                "attributes": request.attributes,
                "parameters": request.parameters,
                "timeout_ms": timeout_ms,
                "max_output_bytes": 8 * 1024 * 1024,
                "max_attributes": 128,
                "max_attribute_bytes": 64 * 1024,
            },
            timeout_s=timeout_ms / 1000.0 + RUN_TRANSPORT_GRACE_SECONDS,
            extra_headers=worker.metadata.get("endpoint_headers"),
        )

        return RunResult(
            status=response.get("status", "FAILED"),
            relationship=response.get("relationship", "failure"),
            content=base64.b64decode(response.get("content_b64", "")),
            attributes=dict(response.get("attributes", {})),
            retryable=bool(response.get("retryable", False)),
            error_code=response.get("error_code", ""),
            error_message=response.get("error_message", ""),
            worker_id=worker.id,
            duration_ms=int(response.get("duration_ms", 0)),
        )

    def cancel(self, worker: Worker, *, invocation_id: str) -> bool:
        response = worker_call(
            worker.endpoint,
            worker.token,
            "/cancel",
            payload={"invocation_id": invocation_id},
            timeout_s=CANCEL_REQUEST_TIMEOUT_SECONDS,
            extra_headers=worker.metadata.get("endpoint_headers"),
        )
        return bool(response.get("cancelled", False))

    def destroy(self, worker: Worker) -> None:
        try:
            self._request(
                worker.backend_group,
                "DELETE",
                f"/v1/sandboxes/{urllib.parse.quote(worker.id)}",
                timeout_seconds=DESTROY_REQUEST_TIMEOUT_SECONDS,
            )
        except SandboxError as exc:
            if "404" not in str(exc):
                raise

    def cleanup_managed(self) -> int:
        deleted = 0

        for cluster in self.clusters:
            try:
                response = self._request(
                    cluster,
                    "GET",
                    "/v1/sandboxes?page=1&pageSize=100",
                )
            except Exception:
                continue

            items = (
                response
                if isinstance(response, list)
                else response.get("items") or response.get("sandboxes") or []
            )

            for item in items:
                metadata = item.get("metadata") or item.get("labels") or {}
                if metadata.get("managed-by") != self.MANAGED_BY:
                    continue
                if metadata.get("runner-owner") != self._label_hash(self.owner_id):
                    continue

                try:
                    sandbox_id = self._sandbox_id(item)
                    self._request(
                        cluster,
                        "DELETE",
                        f"/v1/sandboxes/{urllib.parse.quote(sandbox_id)}",
                        timeout_seconds=DESTROY_REQUEST_TIMEOUT_SECONDS,
                    )
                    deleted += 1
                except Exception:
                    continue

        return deleted
