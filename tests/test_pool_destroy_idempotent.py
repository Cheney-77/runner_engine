import time

from runner_engine.model import Worker
from runner_engine.pool import WorkerPool
from runner_engine.quota import Quota


class Backend:
    def __init__(self):
        self.destroy_count = 0

    def destroy(self, worker):
        self.destroy_count += 1

    def cleanup_managed(self):
        return 0


def test_destroy_is_idempotent_for_quota():
    backend = Backend()
    quota = Quota(max_creating=1, max_live=2, max_live_per_tenant=2)
    pool = WorkerPool(backend, quota)
    quota.reserve_live("tenant")
    worker = Worker(
        id="w",
        endpoint="",
        token="",
        tenant_id="tenant",
        release_id="r",
        runtime_id="e",
        profile="standard",
        backend_group="test",
        created_monotonic=time.monotonic(),
        last_used_monotonic=time.monotonic(),
    )
    pool.invalidate(worker)
    pool.invalidate(worker)
    assert backend.destroy_count == 1
    assert quota.live == 0
