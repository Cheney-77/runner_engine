from __future__ import annotations

import hashlib
import json
import logging
import threading

from .catalog import Catalog
from .errors import RunnerError
from .model import ActiveRun, Lease, RunRequest, RunResult, SecurityPolicy
from .pool import WorkerPool
from .state import RunnerDB


MAX_INLINE_BYTES = 8 * 1024 * 1024
MAX_ATTRIBUTES = 128
MAX_ATTRIBUTE_BYTES = 64 * 1024

DEFAULT_IDEMPOTENCY_RETENTION_MS = 7 * 24 * 60 * 60 * 1000
DEFAULT_RUN_RETENTION_MS = 90 * 24 * 60 * 60 * 1000

logger = logging.getLogger(__name__)


def _filter_attributes(
    values: dict[str, str],
    allowed: tuple[str, ...],
) -> dict[str, str]:
    if not allowed:
        return {}
    allow = set(allowed)
    return {
        key: value
        for key, value in values.items()
        if key in allow
    }


def _validate_attributes(values: dict[str, str]) -> None:
    if len(values) > MAX_ATTRIBUTES:
        raise RunnerError(
            "TOO_MANY_ATTRIBUTES",
            "attribute count exceeds limit",
        )

    total = 0
    for key, value in values.items():
        if not isinstance(key, str) or not isinstance(value, str):
            raise RunnerError(
                "INVALID_ATTRIBUTES",
                "attributes must be dict[str, str]",
            )
        total += len(key.encode("utf-8")) + len(value.encode("utf-8"))

    if total > MAX_ATTRIBUTE_BYTES:
        raise RunnerError(
            "ATTRIBUTES_TOO_LARGE",
            "attributes exceed byte limit",
        )


def _request_fingerprint(request: RunRequest) -> str:
    identity = {
        "schema": 1,
        "release_id": request.release_id,
        "content_sha256": hashlib.sha256(request.content).hexdigest(),
        "attributes": dict(sorted(request.attributes.items())),
        "parameters": dict(sorted(request.parameters.items())),
        "timeout_ms": request.timeout_ms,
    }
    canonical = json.dumps(
        identity,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


class RunnerService:
    def __init__(
        self,
        catalog: Catalog,
        db: RunnerDB,
        pool: WorkerPool,
        policies: dict[str, SecurityPolicy],
        *,
        lease_ttl_ms: int = 15 * 60 * 1000,
        idempotency_retention_ms: int = DEFAULT_IDEMPOTENCY_RETENTION_MS,
        run_retention_ms: int = DEFAULT_RUN_RETENTION_MS,
    ):
        self.catalog = catalog
        self.db = db
        self.pool = pool
        self.policies = policies
        self.lease_ttl_ms = lease_ttl_ms
        self.idempotency_retention_ms = idempotency_retention_ms
        self.run_retention_ms = run_retention_ms

        self._active: dict[str, ActiveRun] = {}
        self._active_lock = threading.RLock()
        self._stop = threading.Event()
        self._reaper_thread: threading.Thread | None = None

    def startup(self) -> int:
        cleaned = self.pool.reset_after_restart()
        abandoned = self.db.reset_running()
        if abandoned:
            logger.warning(
                "marked %d stale RUNNING executions as ABANDONED",
                abandoned,
            )
        self.db.purge_expired_leases()
        self.db.purge_expired_idempotency()
        return cleaned

    def start_background(self, *, interval_seconds: int = 30) -> None:
        if interval_seconds <= 0:
            raise ValueError("interval_seconds must be positive")
        if self._reaper_thread is not None:
            return

        def loop() -> None:
            while not self._stop.wait(interval_seconds):
                try:
                    self.pool.reap()
                    self.db.purge_expired_leases()
                    self.db.purge_expired_idempotency()
                    self.db.purge_old_runs(self.run_retention_ms)
                except Exception:
                    logger.exception("background Runner maintenance failed")

        self._reaper_thread = threading.Thread(
            target=loop,
            name="managed-python-reaper",
            daemon=True,
        )
        self._reaper_thread.start()

    def shutdown(self) -> None:
        self._stop.set()
        if self._reaper_thread is not None:
            self._reaper_thread.join(timeout=5)
        self.db.close()

    def acquire_lease(
        self,
        *,
        tenant_id: str,
        project_id: str,
        processor_id: str,
        release_id: str,
    ) -> Lease:
        release = self.catalog.get(release_id)
        if release.profile not in self.policies:
            raise RunnerError(
                "UNKNOWN_SECURITY_POLICY",
                f"release uses unknown profile {release.profile}",
            )

        return self.db.create_lease(
            tenant_id,
            project_id,
            processor_id,
            release_id,
            self.lease_ttl_ms,
        )

    def get_lease(self, *, tenant_id: str, lease_id: str) -> Lease:
        return self.db.require_lease(
            lease_id,
            tenant_id=tenant_id,
        )

    def renew_lease(self, *, tenant_id: str, lease_id: str) -> Lease:
        return self.db.renew_lease(
            lease_id,
            tenant_id=tenant_id,
            ttl_ms=self.lease_ttl_ms,
        )

    def release_lease(self, *, tenant_id: str, lease_id: str) -> None:
        self.db.delete_lease(
            lease_id,
            tenant_id=tenant_id,
        )

    def invoke(self, request: RunRequest) -> RunResult:
        if len(request.content) > MAX_INLINE_BYTES:
            raise RunnerError(
                "INLINE_CONTENT_TOO_LARGE",
                "inline content exceeds 8 MiB",
            )
        if not request.idempotency_key:
            raise RunnerError(
                "IDEMPOTENCY_REQUIRED",
                "idempotency_key is required",
            )

        lease = self.db.require_lease(
            request.lease_id,
            tenant_id=request.tenant_id,
            release_id=request.release_id,
        )
        release = self.catalog.get(request.release_id)

        try:
            policy = self.policies[release.profile]
        except KeyError as exc:
            raise RunnerError(
                "UNKNOWN_SECURITY_POLICY",
                f"unknown profile {release.profile}",
            ) from exc

        safe_attributes = _filter_attributes(
            request.attributes,
            release.input_attributes,
        )
        _validate_attributes(safe_attributes)
        _validate_attributes(request.parameters)

        safe_request = RunRequest(
            tenant_id=request.tenant_id,
            lease_id=request.lease_id,
            release_id=request.release_id,
            invocation_id=request.invocation_id,
            idempotency_key=request.idempotency_key,
            content=request.content,
            attributes=safe_attributes,
            parameters=request.parameters,
            timeout_ms=min(
                max(1, request.timeout_ms),
                policy.max_timeout_ms,
            ),
        )
        fingerprint = _request_fingerprint(safe_request)

        with self._active_lock:
            if request.invocation_id in self._active:
                raise RunnerError(
                    "INVOCATION_ID_IN_USE",
                    "invocation id is already active",
                )

        claim = self.db.begin_run(
            key=request.idempotency_key,
            tenant_id=request.tenant_id,
            project_id=lease.project_id,
            processor_id=lease.processor_id,
            lease_id=request.lease_id,
            release_id=request.release_id,
            invocation_id=request.invocation_id,
            request_fingerprint=fingerprint,
        )

        if claim.cached_result is not None:
            return claim.cached_result

        run_id = claim.run_id
        if run_id is None:
            raise RunnerError(
                "RUN_STATE_INVALID",
                "new execution did not receive a run id",
                retryable=True,
            )

        worker = None
        run_finished = False

        try:
            worker = self.pool.acquire(
                request.tenant_id,
                release,
                policy,
            )

            with self._active_lock:
                if request.invocation_id in self._active:
                    self.pool.invalidate(worker)
                    worker = None
                    raise RunnerError(
                        "INVOCATION_ID_IN_USE",
                        "invocation id became active concurrently",
                    )

                self._active[request.invocation_id] = ActiveRun(
                    tenant_id=request.tenant_id,
                    lease_id=request.lease_id,
                    release_id=request.release_id,
                    worker=worker,
                )

            result = self.pool.backend.run(
                worker,
                release,
                safe_request,
                timeout_ms=safe_request.timeout_ms,
            )

            safe_output_attributes = _filter_attributes(
                result.attributes,
                release.output_attributes,
            )
            _validate_attributes(safe_output_attributes)

            if len(result.content) > MAX_INLINE_BYTES:
                raise RunnerError(
                    "OUTPUT_TOO_LARGE",
                    "operator output exceeds 8 MiB",
                )

            result = RunResult(
                status=result.status,
                relationship=result.relationship,
                content=result.content,
                attributes=safe_output_attributes,
                retryable=result.retryable,
                error_code=result.error_code,
                error_message=result.error_message,
                worker_id=result.worker_id,
                duration_ms=result.duration_ms,
            )

            self.db.finish_run(
                run_id=run_id,
                key=request.idempotency_key,
                tenant_id=request.tenant_id,
                release_id=request.release_id,
                result=result,
                idempotency_retention_ms=self.idempotency_retention_ms,
            )
            run_finished = True

            reusable_errors = {
                "TIMEOUT",
                "CANCELLED",
                "CPU_LIMIT",
                "USER_EXCEPTION",
                "USER_IMPORT_ERROR",
                "ENTRYPOINT_NOT_FOUND",
                "INVALID_USER_RESULT",
                "USER_PROCESS_EXITED",
                "USER_PROCESS_SIGNALED",
                "OUTPUT_LIMIT",
                "LOG_LIMIT",
                "CHILD_PROTOCOL_ERROR",
                "INVALID_CHILD_RESPONSE",
                "OPERATOR_REPORTED_FAILURE",
            }

            try:
                if result.status == "SUCCEEDED" or result.error_code in reusable_errors:
                    self.pool.release(worker, policy)
                else:
                    self.pool.invalidate(worker)
            except Exception:
                logger.exception(
                    "worker cleanup failed after terminal result; result remains authoritative"
                )

            worker = None
            return result

        except RunnerError as exc:
            if worker is not None:
                try:
                    self.pool.invalidate(worker)
                except Exception:
                    logger.exception("failed to invalidate worker after RunnerError")

            if not run_finished:
                self.db.abandon_run(
                    run_id=run_id,
                    key=request.idempotency_key,
                    tenant_id=request.tenant_id,
                    release_id=request.release_id,
                    error_code=exc.code,
                    error_message=str(exc),
                )
            raise

        except Exception as exc:
            if worker is not None:
                try:
                    self.pool.invalidate(worker)
                except Exception:
                    logger.exception(
                        "failed to invalidate worker after infrastructure error"
                    )

            if not run_finished:
                self.db.abandon_run(
                    run_id=run_id,
                    key=request.idempotency_key,
                    tenant_id=request.tenant_id,
                    release_id=request.release_id,
                    error_code="RUNNER_INFRASTRUCTURE_ERROR",
                    error_message=str(exc),
                )

            raise RunnerError(
                "RUNNER_INFRASTRUCTURE_ERROR",
                str(exc),
                retryable=True,
            ) from exc

        finally:
            with self._active_lock:
                self._active.pop(request.invocation_id, None)

    def cancel(
        self,
        *,
        tenant_id: str,
        lease_id: str,
        invocation_id: str,
    ) -> bool:
        self.db.require_lease(
            lease_id,
            tenant_id=tenant_id,
        )

        with self._active_lock:
            active = self._active.get(invocation_id)
            if active is None:
                return False

            if active.tenant_id != tenant_id or active.lease_id != lease_id:
                raise RunnerError(
                    "CANCEL_FORBIDDEN",
                    "invocation does not belong to this tenant/lease",
                )

            worker = active.worker

        try:
            return bool(
                self.pool.backend.cancel(
                    worker,
                    invocation_id=invocation_id,
                )
            )
        except Exception as exc:
            try:
                self.pool.invalidate(worker)
            except Exception:
                logger.exception(
                    "failed to invalidate worker after cancel transport failure"
                )

            raise RunnerError(
                "CANCEL_FAILED",
                str(exc),
                retryable=True,
            ) from exc
