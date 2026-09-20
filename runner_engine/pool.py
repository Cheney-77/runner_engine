from __future__ import annotations

import threading
import time

from .model import OperatorRelease, SecurityPolicy, Worker
from .quota import Quota
from .sandbox.backend import SandboxBackend


class WorkerPool:
    """
    Warm sandbox pool keyed by tenant + runtime + security profile.

    A sandbox is not owned by a Processor or a Release. Multiple releases that use the
    same immutable runtime image can be bootstrapped into the same idle sandbox.
    """

    def __init__(
        self,
        backend: SandboxBackend,
        quota: Quota,
        *,
        idle_seconds: int = 600,
        orphan_ttl_seconds: int = 3600,
        renew_before_seconds: int = 900,
        max_installed_releases: int = 256,
    ):
        if idle_seconds <= 0:
            raise ValueError("idle_seconds must be positive")
        if orphan_ttl_seconds <= idle_seconds + 60:
            raise ValueError("orphan_ttl_seconds must be comfortably larger than idle_seconds")
        if renew_before_seconds <= 0 or renew_before_seconds >= orphan_ttl_seconds:
            raise ValueError("renew_before_seconds must be between 0 and orphan_ttl_seconds")
        if max_installed_releases <= 0:
            raise ValueError("max_installed_releases must be positive")

        self.backend = backend
        self.quota = quota
        self.idle_seconds = idle_seconds
        self.orphan_ttl_seconds = orphan_ttl_seconds
        self.renew_before_seconds = renew_before_seconds
        self.max_installed_releases = max_installed_releases
        self._lock = threading.RLock()
        self._idle: dict[tuple[str, str, str], list[Worker]] = {}

    @staticmethod
    def _key(tenant_id: str, release: OperatorRelease) -> tuple[str, str, str]:
        return tenant_id, release.runtime.id, release.profile

    def _idle_expired(self, worker: Worker) -> bool:
        if worker.destroyed or not worker.healthy:
            return True
        return time.monotonic() - worker.last_used_monotonic > self.idle_seconds

    def _ensure_sandbox_ttl(self, worker: Worker, policy: SecurityPolicy) -> None:
        if worker.sandbox_expires_monotonic is None:
            return

        remaining = worker.sandbox_expires_monotonic - time.monotonic()
        execution_headroom = policy.max_timeout_ms / 1000.0 + 120.0
        renew_threshold = max(self.renew_before_seconds, execution_headroom)

        if remaining <= renew_threshold:
            self.backend.renew(worker, orphan_ttl_seconds=self.orphan_ttl_seconds)

    def _ensure_release(self, worker: Worker, release: OperatorRelease) -> None:
        if release.id in worker.installed_release_ids:
            return
        self.backend.install(worker, release)
        worker.installed_release_ids.add(release.id)

    def acquire(self, tenant_id: str, release: OperatorRelease, policy: SecurityPolicy) -> Worker:
        key = self._key(tenant_id, release)
        stale: list[Worker] = []
        candidate: Worker | None = None

        with self._lock:
            bucket = self._idle.get(key, [])
            while bucket:
                worker = bucket.pop()
                if self._idle_expired(worker):
                    stale.append(worker)
                    continue
                if (
                    release.id not in worker.installed_release_ids
                    and len(worker.installed_release_ids) >= self.max_installed_releases
                ):
                    stale.append(worker)
                    continue
                candidate = worker
                break

            if not bucket:
                self._idle.pop(key, None)

        for worker in stale:
            self._destroy(worker)

        if candidate is not None:
            try:
                self._ensure_sandbox_ttl(candidate, policy)
                self._ensure_release(candidate, release)
                candidate.last_used_monotonic = time.monotonic()
                return candidate
            except Exception:
                self.invalidate(candidate)
                raise

        self.quota.reserve_live(tenant_id)
        worker: Worker | None = None

        try:
            with self.quota.creation_slot():
                worker = self.backend.create(
                    tenant_id=tenant_id,
                    release=release,
                    policy=policy,
                    orphan_ttl_seconds=self.orphan_ttl_seconds,
                )
                self._ensure_release(worker, release)
            return worker
        except Exception:
            if worker is not None:
                try:
                    self.backend.destroy(worker)
                except Exception:
                    pass
            self.quota.release_live(tenant_id)
            raise

    def release(self, worker: Worker, policy: SecurityPolicy) -> None:
        worker.runs += 1
        worker.last_used_monotonic = time.monotonic()

        if worker.destroyed or not worker.healthy or not policy.reuse_sandbox:
            self._destroy(worker)
            return

        key = (worker.tenant_id, worker.runtime_id, worker.profile)
        with self._lock:
            self._idle.setdefault(key, []).append(worker)

    def invalidate(self, worker: Worker) -> None:
        worker.healthy = False
        self._destroy(worker)

    def _destroy(self, worker: Worker) -> None:
        # Cancel and an in-flight Invoke can race to retire the same sandbox.
        # Backend deletion and quota release must therefore be exactly once per Worker object.
        with self._lock:
            if worker.destroyed:
                return
            worker.destroyed = True

        try:
            self.backend.destroy(worker)
        finally:
            self.quota.release_live(worker.tenant_id)

    def reap(self) -> int:
        stale: list[Worker] = []

        with self._lock:
            for key, bucket in list(self._idle.items()):
                kept: list[Worker] = []
                for worker in bucket:
                    if self._idle_expired(worker):
                        stale.append(worker)
                    else:
                        kept.append(worker)

                if kept:
                    self._idle[key] = kept
                else:
                    self._idle.pop(key, None)

        for worker in stale:
            self._destroy(worker)

        return len(stale)

    def reset_after_restart(self) -> int:
        """Destroy owned leftovers instead of pretending the in-memory pool can be recovered."""
        with self._lock:
            self._idle.clear()
        return self.backend.cleanup_managed()
