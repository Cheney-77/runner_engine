import pytest
from runner_engine.errors import LeaseError
from runner_engine.model import RunResult
from runner_engine.state import RunnerDB


def test_lease_persists_and_renews(tmp_path):
    path = tmp_path / "runner.db"
    db = RunnerDB(path)
    lease = db.create_lease("a", "p", "proc", "release", 60_000)

    reopened = RunnerDB(path)
    assert reopened.require_lease(lease.id, tenant_id="a").release_id == "release"
    renewed = reopened.renew_lease(lease.id, tenant_id="a", ttl_ms=120_000)
    assert renewed.expires_at_ms >= lease.expires_at_ms


def test_idempotency_replays_completed_result(tmp_path):
    db = RunnerDB(tmp_path / "runner.db")
    db.start_run(key="k", tenant_id="a", lease_id="l", release_id="r", invocation_id="i")
    db.finish_run("k", RunResult(status="SUCCEEDED", content=b"done"))
    result = db.get_completed("k", tenant_id="a", release_id="r")
    assert result.content == b"done"
