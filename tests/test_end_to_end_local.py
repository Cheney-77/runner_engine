from runner_engine.model import RunRequest, SecurityPolicy
from runner_engine.pool import WorkerPool
from runner_engine.quota import Quota
from runner_engine.sandbox.local import LocalBackend
from runner_engine.service import RunnerService
from runner_engine.state import RunnerDB


def test_local_backend_end_to_end(tmp_path, published):
    _, catalog, release = published
    backend = LocalBackend()
    pool = WorkerPool(backend, Quota(max_creating=1, max_live=2, max_live_per_tenant=2))
    service = RunnerService(
        catalog,
        RunnerDB(tmp_path / "db.sqlite"),
        pool,
        {"standard": SecurityPolicy(name="standard", sandbox_cluster="local")},
    )
    lease = service.acquire_lease(tenant_id="t", project_id="p", processor_id="proc", release_id=release.id)
    result = service.invoke(RunRequest(
        tenant_id="t",
        lease_id=lease.id,
        release_id=release.id,
        invocation_id="i1",
        idempotency_key="flowfile-1",
        content=b"hello",
        attributes={"filename": "x", "private": "not-visible"},
    ))
    assert result.status == "SUCCEEDED"
    assert result.content == b"HELLO"
    assert result.attributes == {"mime.type": "text/plain"}
    backend.cleanup_managed()
