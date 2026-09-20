from __future__ import annotations

from typing import Protocol

from ..model import (
    OperatorRelease,
    RunRequest,
    RunResult,
    SecurityPolicy,
    Worker,
)


class SandboxBackend(Protocol):
    def create(
        self,
        *,
        tenant_id: str,
        release: OperatorRelease,
        policy: SecurityPolicy,
        orphan_ttl_seconds: int,
    ) -> Worker:
        ...

    def renew(self, worker: Worker, *, orphan_ttl_seconds: int) -> None:
        ...

    def install(self, worker: Worker, release: OperatorRelease) -> None:
        ...

    def run(
        self,
        worker: Worker,
        release: OperatorRelease,
        request: RunRequest,
        *,
        timeout_ms: int,
    ) -> RunResult:
        ...

    def cancel(self, worker: Worker, *, invocation_id: str) -> bool:
        ...

    def destroy(self, worker: Worker) -> None:
        ...

    def cleanup_managed(self) -> int:
        ...
