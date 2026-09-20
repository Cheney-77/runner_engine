from __future__ import annotations

import threading
from contextlib import contextmanager

from .errors import QuotaError


class Quota:
    """Tracks both creation concurrency and actually-live sandbox count."""

    def __init__(
        self,
        *,
        max_creating: int = 8,
        max_live: int = 64,
        max_live_per_tenant: int = 16,
    ):
        if min(max_creating, max_live, max_live_per_tenant) <= 0:
            raise ValueError("quota values must be positive")
        self._creating = threading.BoundedSemaphore(max_creating)
        self._max_live = max_live
        self._max_live_per_tenant = max_live_per_tenant
        self._lock = threading.Lock()
        self._live = 0
        self._tenant_live: dict[str, int] = {}

    @contextmanager
    def creation_slot(self):
        self._creating.acquire()
        try:
            yield
        finally:
            self._creating.release()

    def reserve_live(self, tenant_id: str) -> None:
        with self._lock:
            tenant_count = self._tenant_live.get(tenant_id, 0)
            if self._live >= self._max_live:
                raise QuotaError("GLOBAL_SANDBOX_QUOTA", "global live-sandbox quota reached", retryable=True)
            if tenant_count >= self._max_live_per_tenant:
                raise QuotaError("TENANT_SANDBOX_QUOTA", "tenant live-sandbox quota reached", retryable=True)
            self._live += 1
            self._tenant_live[tenant_id] = tenant_count + 1

    def release_live(self, tenant_id: str) -> None:
        with self._lock:
            tenant_count = self._tenant_live.get(tenant_id, 0)
            if tenant_count <= 0:
                return
            self._live -= 1
            if tenant_count == 1:
                self._tenant_live.pop(tenant_id, None)
            else:
                self._tenant_live[tenant_id] = tenant_count - 1

    @property
    def live(self) -> int:
        with self._lock:
            return self._live
