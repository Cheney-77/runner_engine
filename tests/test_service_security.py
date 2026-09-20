import threading
import time
import pytest

from runner_engine.errors import RunnerError
from runner_engine.model import RunRequest, RunResult, SecurityPolicy, Worker
from runner_engine.pool import WorkerPool
from runner_engine.quota import Quota
from runner_engine.service import RunnerService
from runner_engine.state import RunnerDB


class BlockingBackend:
    def __init__(self):
        self.created = []
        self.started = threading.Event()
        self.finish = threading.Event()

    def create(self, *, tenant_id, release, policy):
        worker = Worker(
            id=f"w-{len(self.created)}",
            endpoint="",
            token="",
            tenant_id=tenant_id,
            release_id=release.id,
            runtime_id=release.runtime.id,
            profile=release.profile,
            backend_group="test",
            created_monotonic=time.monotonic(),
            last_used_monotonic=time.monotonic(),
        )
        self.created.append(worker)
        return worker

    def install(self, worker, release):
        pass

    def run(self, worker, release, request, *, timeout_ms):
        self.started.set()
        self.finish.wait(2)
        return RunResult(
            status="SUCCEEDED",
            content=request.content,
            attributes={"mime.type": "ok", "secret.out": "drop"},
            worker_id=worker.id,
        )

    def destroy(self, worker):
        worker.healthy = False
        self.finish.set()

    def cleanup_managed(self):
        return 0


def _service(tmp_path, published):
    _, catalog, release = published
    backend = BlockingBackend()
    pool = WorkerPool(backend, Quota(max_creating=2, max_live=4, max_live_per_tenant=2))
    policy = SecurityPolicy(name="standard", sandbox_cluster="test")
    service = RunnerService(catalog, RunnerDB(tmp_path / "db.sqlite"), pool, {"standard": policy})
    return service, backend, release


def test_server_filters_attributes(tmp_path, published):
    service, backend, release = _service(tmp_path, published)
    lease = service.acquire_lease(tenant_id="a", project_id="p", processor_id="x", release_id=release.id)
    backend.finish.set()
    result = service.invoke(RunRequest(
        tenant_id="a",
        lease_id=lease.id,
        release_id=release.id,
        invocation_id="i",
        idempotency_key="k",
        content=b"x",
        attributes={"filename": "ok", "secret.in": "drop"},
    ))
    assert result.attributes == {"mime.type": "ok"}


def test_cross_lease_cancel_is_rejected(tmp_path, published):
    service, backend, release = _service(tmp_path, published)
    lease_a = service.acquire_lease(tenant_id="a", project_id="p", processor_id="x", release_id=release.id)
    lease_b = service.acquire_lease(tenant_id="b", project_id="p", processor_id="x", release_id=release.id)

    request = RunRequest(
        tenant_id="b",
        lease_id=lease_b.id,
        release_id=release.id,
        invocation_id="victim",
        idempotency_key="victim-key",
        content=b"x",
    )
    error = []
    def invoke():
        try:
            service.invoke(request)
        except Exception as exc:
            error.append(exc)

    thread = threading.Thread(target=invoke)
    thread.start()
    assert backend.started.wait(1)

    with pytest.raises(RunnerError) as exc:
        service.cancel(tenant_id="a", lease_id=lease_a.id, invocation_id="victim")
    assert exc.value.code == "CANCEL_FORBIDDEN"

    backend.finish.set()
    thread.join(2)
    assert not error
