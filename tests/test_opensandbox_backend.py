import time

from runner_engine.model import OperatorRelease, RuntimeEnv, SecurityPolicy
from runner_engine.sandbox.opensandbox import OpenSandboxBackend


class FakeOpenSandbox(OpenSandboxBackend):
    def __init__(self):
        super().__init__({"gvisor": {"url": "http://fake"}}, owner_id="runner/with unsafe owner id")
        self.calls = []
        self.create_payload = None

    def _request(self, cluster, method, path, payload=None, *, timeout=30.0):
        self.calls.append((cluster, method, path))
        if method == "POST" and path == "/v1/sandboxes":
            self.create_payload = payload
            return {"id": "sandbox-1", "status": {"state": "Running"}}
        if method == "GET" and path == "/v1/sandboxes/sandbox-1":
            return {"id": "sandbox-1", "status": {"state": "Running"}}
        if method == "GET" and path.endswith("/endpoints/9080"):
            return {"endpoint": "http://worker", "headers": {"X-OpenSandbox-Route": "abc"}}
        if method == "DELETE":
            return {}
        raise AssertionError((cluster, method, path, payload))


def test_create_uses_real_policy_and_safe_metadata(monkeypatch, tmp_path):
    import runner_engine.sandbox.opensandbox as module
    monkeypatch.setattr(module, "worker_call", lambda *a, **k: {"ok": True})

    artifact = tmp_path / "x"
    artifact.write_bytes(b"x")
    release = OperatorRelease(
        id="a" * 64,
        artifact_path=str(artifact),
        artifact_sha256="b" * 64,
        runtime=RuntimeEnv(id="c" * 64, image="repo/runtime@sha256:" + "d" * 64),
        entrypoint="main:process",
        profile="standard profile with spaces",
    )
    policy = SecurityPolicy(
        name="standard",
        sandbox_cluster="gvisor",
        cpu="500m",
        memory="256Mi",
        network_allow=("api.example.com",),
    )
    backend = FakeOpenSandbox()
    worker = backend.create(tenant_id="tenant/unsafe:value", release=release, policy=policy)

    payload = backend.create_payload
    assert payload["resourceLimits"] == {"cpu": "500m", "memory": "256Mi"}
    assert payload["networkPolicy"]["defaultAction"] == "deny"
    assert payload["networkPolicy"]["egress"] == [{"action": "allow", "target": "api.example.com"}]
    assert payload["secureAccess"] is True
    assert payload["metadata"]["release"] == "a" * 32
    assert all(len(v) <= 63 for v in payload["metadata"].values())
    assert worker.metadata["endpoint_headers"] == {"X-OpenSandbox-Route": "abc"}
