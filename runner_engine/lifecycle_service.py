from __future__ import annotations

from .errors import RunnerError
from .service import RunnerService


class LifecycleRunnerService(RunnerService):
    """RunnerService with durable runtime lifecycle gates.

    The WorkerPool remains the authoritative sandbox-acquire gate. These
    service-level checks also stop new leases, lease renewals and invocations
    once a runtime enters RETIRING/RETIRED.
    """

    def __init__(self, *args, lifecycle, **kwargs):
        super().__init__(*args, **kwargs)
        self.lifecycle = lifecycle

    def _assert_runtime_active(self, release) -> None:
        row = self.lifecycle.get_by_runtime_id(release.runtime.id)
        if row is None:
            return

        state = str(row["state"])
        raise RunnerError(
            "RUNTIME_RETIRED" if state == "RETIRED" else "RUNTIME_RETIRING",
            (
                f"runtime image is {state.lower()} and cannot accept "
                "new execution"
            ),
            retryable=state == "RETIRING",
        )

    def acquire_lease(
        self,
        *,
        tenant_id: str,
        project_id: str,
        processor_id: str,
        release_id: str,
    ):
        release = self.catalog.get(release_id)
        self._assert_runtime_active(release)
        return super().acquire_lease(
            tenant_id=tenant_id,
            project_id=project_id,
            processor_id=processor_id,
            release_id=release_id,
        )

    def renew_lease(self, *, tenant_id: str, lease_id: str):
        current = self.db.require_lease(
            lease_id,
            tenant_id=tenant_id,
        )
        release = self.catalog.get(current.release_id)
        self._assert_runtime_active(release)
        return super().renew_lease(
            tenant_id=tenant_id,
            lease_id=lease_id,
        )

    def invoke(self, request):
        release = self.catalog.get(request.release_id)
        self._assert_runtime_active(release)

        # A retirement can start after this check. LifecycleWorkerPool.acquire()
        # re-checks the same durable lifecycle store, so the race is closed at
        # the sandbox acquisition boundary.
        return super().invoke(request)
