from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .errors import RunnerError
from .model import RunRequest
from .service import RunnerService


class AccessControl:
    """Maps authenticated client identity to allowed tenant/project pairs."""

    def __init__(self, identities: dict[str, dict[str, list[str]]]):
        self.identities = identities

    @classmethod
    def from_json(cls, path: str | Path) -> "AccessControl":
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
        return cls(raw.get("identities", {}))

    def allow(self, identity: str, tenant_id: str, project_id: str | None = None) -> bool:
        tenant_map = self.identities.get(identity, {})
        projects = tenant_map.get(tenant_id)
        if projects is None:
            return False
        if project_id is None:
            return True
        return "*" in projects or project_id in projects


def _peer_identity(context) -> str:
    auth = context.auth_context()
    # grpc Python commonly exposes x509_common_name or x509_subject_alternative_name as bytes lists.
    for key in ("x509_subject_alternative_name", "x509_common_name"):
        values = auth.get(key)
        if values:
            value = values[0]
            return value.decode("utf-8") if isinstance(value, bytes) else str(value)
    raise RunnerError("UNAUTHENTICATED", "mTLS peer identity is missing")


def serve(
    service: RunnerService,
    access: AccessControl,
    *,
    address: str,
    cert_file: str,
    key_file: str,
    client_ca_file: str,
    max_workers: int = 32,
):
    """
    Start a mutual-TLS gRPC server.

    Generate `runner_engine/generated/*_pb2.py` from `proto/runner.proto` first.
    Plaintext mode is deliberately not implemented in this production entrypoint.
    """
    import grpc
    from concurrent import futures
    try:
        from .generated import runner_pb2, runner_pb2_grpc
    except ImportError as exc:
        raise RuntimeError("generate gRPC stubs first: python scripts/generate_proto.py") from exc

    class Servicer(runner_pb2_grpc.ManagedPythonRunnerServicer):
        def _abort_runner_error(self, context, exc: RunnerError):
            if exc.code in {"UNAUTHENTICATED"}:
                status = grpc.StatusCode.UNAUTHENTICATED
            elif exc.code.endswith("FORBIDDEN") or exc.code in {"CANCEL_FORBIDDEN"}:
                status = grpc.StatusCode.PERMISSION_DENIED
            elif exc.code in {"RELEASE_NOT_FOUND", "LEASE_UNKNOWN"}:
                status = grpc.StatusCode.NOT_FOUND
            elif exc.retryable:
                status = grpc.StatusCode.UNAVAILABLE
            else:
                status = grpc.StatusCode.FAILED_PRECONDITION
            context.abort(status, f"{exc.code}:{exc}")

        def _authorize(self, context, tenant_id: str, project_id: str | None = None) -> str:
            identity = _peer_identity(context)
            if not access.allow(identity, tenant_id, project_id):
                context.abort(grpc.StatusCode.PERMISSION_DENIED, "identity is not allowed for tenant/project")
            return identity

        def _authorize_lease(self, context, tenant_id: str, lease_id: str):
            identity = _peer_identity(context)
            try:
                lease = service.get_lease(tenant_id=tenant_id, lease_id=lease_id)
            except RunnerError as exc:
                self._abort_runner_error(context, exc)
                raise AssertionError("unreachable")
            if not access.allow(identity, tenant_id, lease.project_id):
                context.abort(grpc.StatusCode.PERMISSION_DENIED, "identity is not allowed for lease project")
            return lease

        def Health(self, request, context):
            _peer_identity(context)
            return runner_pb2.HealthResponse(ok=True, version="3.3.0")

        def AcquireLease(self, request, context):
            self._authorize(context, request.tenant_id, request.project_id)
            try:
                lease = service.acquire_lease(
                    tenant_id=request.tenant_id,
                    project_id=request.project_id,
                    processor_id=request.processor_id,
                    release_id=request.release_id,
                )
                return runner_pb2.LeaseResponse(
                    lease_id=lease.id,
                    expires_at_epoch_ms=lease.expires_at_ms,
                )
            except RunnerError as exc:
                self._abort_runner_error(context, exc)

        def RenewLease(self, request, context):
            self._authorize_lease(context, request.tenant_id, request.lease_id)
            try:
                lease = service.renew_lease(tenant_id=request.tenant_id, lease_id=request.lease_id)
                return runner_pb2.LeaseResponse(
                    lease_id=lease.id,
                    expires_at_epoch_ms=lease.expires_at_ms,
                )
            except RunnerError as exc:
                self._abort_runner_error(context, exc)

        def Invoke(self, request, context):
            self._authorize_lease(context, request.tenant_id, request.lease_id)
            try:
                result = service.invoke(
                    RunRequest(
                        tenant_id=request.tenant_id,
                        lease_id=request.lease_id,
                        release_id=request.release_id,
                        invocation_id=request.invocation_id,
                        idempotency_key=request.idempotency_key,
                        content=bytes(request.content),
                        attributes=dict(request.attributes),
                        parameters=dict(request.parameters),
                        timeout_ms=request.timeout_ms or 30_000,
                    )
                )
                return runner_pb2.InvokeResponse(
                    status=result.status,
                    relationship=result.relationship,
                    content=result.content,
                    attributes=result.attributes,
                    retryable=result.retryable,
                    error_code=result.error_code,
                    error_message=result.error_message,
                    worker_id=result.worker_id,
                    duration_ms=result.duration_ms,
                )
            except RunnerError as exc:
                self._abort_runner_error(context, exc)

        def Cancel(self, request, context):
            self._authorize_lease(context, request.tenant_id, request.lease_id)
            try:
                cancelled = service.cancel(
                    tenant_id=request.tenant_id,
                    lease_id=request.lease_id,
                    invocation_id=request.invocation_id,
                )
                return runner_pb2.CancelResponse(cancelled=cancelled)
            except RunnerError as exc:
                self._abort_runner_error(context, exc)

        def ReleaseLease(self, request, context):
            self._authorize_lease(context, request.tenant_id, request.lease_id)
            try:
                service.release_lease(tenant_id=request.tenant_id, lease_id=request.lease_id)
                return runner_pb2.ReleaseLeaseResponse(released=True)
            except RunnerError as exc:
                self._abort_runner_error(context, exc)

    credentials = grpc.ssl_server_credentials(
        [(Path(key_file).read_bytes(), Path(cert_file).read_bytes())],
        root_certificates=Path(client_ca_file).read_bytes(),
        require_client_auth=True,
    )
    server = grpc.server(
        futures.ThreadPoolExecutor(max_workers=max_workers),
        options=[
            ("grpc.max_receive_message_length", 10 * 1024 * 1024),
            ("grpc.max_send_message_length", 10 * 1024 * 1024),
        ],
    )
    runner_pb2_grpc.add_ManagedPythonRunnerServicer_to_server(Servicer(), server)
    server.add_secure_port(address, credentials)
    server.start()
    return server
