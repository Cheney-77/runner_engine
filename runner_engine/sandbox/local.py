from __future__ import annotations

import base64
import os
import secrets
import tempfile
import time
from pathlib import Path

from ..model import OperatorRelease, RunRequest, RunResult, SecurityPolicy, Worker
from ..worker.agent import AgentState


class LocalBackend:
    """Development/test backend. This is not a security sandbox."""

    def __init__(self):
        self._workers: dict[
            str,
            tuple[AgentState, tempfile.TemporaryDirectory],
        ] = {}
        self._sequence = 0

    def create(
        self,
        *,
        tenant_id: str,
        release: OperatorRelease,
        policy: SecurityPolicy,
        orphan_ttl_seconds: int,
    ) -> Worker:
        self._sequence += 1
        worker_id = f"local-{self._sequence}"
        token = secrets.token_urlsafe(32)
        temp = tempfile.TemporaryDirectory(prefix="mpr-worker-")
        Path(temp.name).chmod(0o755)

        state = AgentState(
            token=token,
            releases_root=Path(temp.name) / "releases",
            invocations_root=Path(temp.name) / "invocations",
            dependency_root=os.environ.get("RUNNER_DEPENDENCY_ROOT"),
            child_uid=os.getuid() if os.name == "posix" else 10001,
            child_gid=os.getgid() if os.name == "posix" else 10001,
            child_nproc=0,
            child_nofile=256,
        )
        self._workers[worker_id] = (state, temp)

        now = time.monotonic()
        return Worker(
            id=worker_id,
            endpoint="local://agent-state",
            token=token,
            tenant_id=tenant_id,
            runtime_id=release.runtime.id,
            profile=release.profile,
            backend_group="local",
            created_monotonic=now,
            last_used_monotonic=now,
            sandbox_expires_monotonic=now + orphan_ttl_seconds,
        )

    def renew(self, worker: Worker, *, orphan_ttl_seconds: int) -> None:
        worker.sandbox_expires_monotonic = time.monotonic() + orphan_ttl_seconds

    def install(self, worker: Worker, release: OperatorRelease) -> None:
        state, _ = self._workers[worker.id]
        artifact = Path(release.artifact_path).read_bytes()
        state.bootstrap(
            {
                "release_id": release.id,
                "artifact_sha256": release.artifact_sha256,
                "artifact_b64": base64.b64encode(artifact).decode("ascii"),
            }
        )

    def run(
        self,
        worker: Worker,
        release: OperatorRelease,
        request: RunRequest,
        *,
        timeout_ms: int,
    ) -> RunResult:
        state, _ = self._workers[worker.id]
        response = state.run(
            {
                "release_id": release.id,
                "invocation_id": request.invocation_id,
                "content_b64": base64.b64encode(request.content).decode("ascii"),
                "attributes": request.attributes,
                "parameters": request.parameters,
                "timeout_ms": timeout_ms,
                "max_output_bytes": 8 * 1024 * 1024,
                "max_attributes": 128,
                "max_attribute_bytes": 64 * 1024,
            }
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
        state, _ = self._workers[worker.id]
        response = state.cancel({"invocation_id": invocation_id})
        return bool(response.get("cancelled", False))

    def destroy(self, worker: Worker) -> None:
        item = self._workers.pop(worker.id, None)
        if item is not None:
            _, temp = item
            temp.cleanup()

    def cleanup_managed(self) -> int:
        ids = list(self._workers)
        for worker_id in ids:
            item = self._workers.pop(worker_id, None)
            if item is not None:
                _, temp = item
                temp.cleanup()
        return len(ids)
