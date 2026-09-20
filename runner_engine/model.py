from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class RuntimeEnv:
    id: str
    image: str
    python: str = "3.12"


@dataclass(frozen=True)
class OperatorRelease:
    id: str
    artifact_path: str
    artifact_sha256: str
    runtime: RuntimeEnv
    entrypoint: str
    profile: str = "standard"
    input_attributes: tuple[str, ...] = ()
    output_attributes: tuple[str, ...] = ()


@dataclass(frozen=True)
class SecurityPolicy:
    name: str
    sandbox_cluster: str
    cpu: str = "1"
    memory: str = "512Mi"
    max_timeout_ms: int = 60_000
    network_allow: tuple[str, ...] = ()
    reuse_sandbox: bool = True


@dataclass(frozen=True)
class Lease:
    id: str
    tenant_id: str
    project_id: str
    processor_id: str
    release_id: str
    expires_at_ms: int


@dataclass(frozen=True)
class RunRequest:
    tenant_id: str
    lease_id: str
    release_id: str
    invocation_id: str
    idempotency_key: str
    content: bytes
    attributes: dict[str, str] = field(default_factory=dict)
    parameters: dict[str, str] = field(default_factory=dict)
    timeout_ms: int = 30_000


@dataclass(frozen=True)
class RunResult:
    status: str
    relationship: str = "success"
    content: bytes = b""
    attributes: dict[str, str] = field(default_factory=dict)
    retryable: bool = False
    error_code: str = ""
    error_message: str = ""
    worker_id: str = ""
    duration_ms: int = 0


@dataclass
class Worker:
    id: str
    endpoint: str
    token: str
    tenant_id: str
    runtime_id: str
    profile: str
    backend_group: str
    created_monotonic: float
    last_used_monotonic: float
    installed_release_ids: set[str] = field(default_factory=set)
    sandbox_expires_monotonic: float | None = None
    runs: int = 0
    healthy: bool = True
    destroyed: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ActiveRun:
    tenant_id: str
    lease_id: str
    release_id: str
    worker: Worker
