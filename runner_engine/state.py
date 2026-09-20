from __future__ import annotations

import base64
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool

from .errors import LeaseError, RunnerError
from .model import Lease, RunResult


LEASE_SCHEMA_STATEMENTS = (
    "CREATE SCHEMA IF NOT EXISTS runner",
    """
    CREATE TABLE IF NOT EXISTS runner.leases (
        id UUID PRIMARY KEY,
        tenant_id TEXT NOT NULL,
        project_id TEXT NOT NULL,
        processor_id TEXT NOT NULL,
        release_id VARCHAR(64) NOT NULL,
        expires_at TIMESTAMPTZ NOT NULL,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
    )
    """,
    "CREATE INDEX IF NOT EXISTS ix_runner_leases_expiry ON runner.leases(expires_at)",
    "CREATE INDEX IF NOT EXISTS ix_runner_leases_processor ON runner.leases(tenant_id, processor_id)",
)

RUNS_CREATE_SQL = """
CREATE TABLE runner.runs (
    run_id UUID PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    project_id TEXT,
    processor_id TEXT,
    release_id VARCHAR(64) NOT NULL,
    lease_id UUID NOT NULL,
    invocation_id TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    request_fingerprint VARCHAR(64) NOT NULL,
    state TEXT NOT NULL CHECK (state IN ('RUNNING', 'DONE', 'ABANDONED')),
    outcome_class TEXT,
    status TEXT,
    relationship TEXT,
    retryable BOOLEAN,
    error_code TEXT,
    error_message TEXT,
    worker_id TEXT,
    duration_ms BIGINT,
    replayable BOOLEAN NOT NULL DEFAULT FALSE,
    idempotency_expires_at TIMESTAMPTZ,
    started_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    completed_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
)
"""

IDEMPOTENCY_CREATE_SQL = """
CREATE TABLE IF NOT EXISTS runner.idempotency_keys (
    tenant_id TEXT NOT NULL,
    release_id VARCHAR(64) NOT NULL,
    idempotency_key TEXT NOT NULL,
    request_fingerprint VARCHAR(64) NOT NULL,
    state TEXT NOT NULL CHECK (state IN ('RUNNING', 'DONE')),
    current_run_id UUID NOT NULL,
    lease_id UUID NOT NULL,
    invocation_id TEXT NOT NULL,
    replayable BOOLEAN NOT NULL DEFAULT FALSE,
    result_json JSONB,
    expires_at TIMESTAMPTZ,
    replay_count BIGINT NOT NULL DEFAULT 0,
    last_replayed_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, release_id, idempotency_key)
)
"""


@dataclass(frozen=True)
class RunClaim:
    run_id: str | None
    cached_result: RunResult | None


def _epoch_ms(value: datetime) -> int:
    return int(value.timestamp() * 1000)


def _result_to_dict(result: RunResult) -> dict:
    return {
        "status": result.status,
        "relationship": result.relationship,
        "content_b64": base64.b64encode(result.content).decode("ascii"),
        "attributes": result.attributes,
        "retryable": result.retryable,
        "error_code": result.error_code,
        "error_message": result.error_message,
        "worker_id": result.worker_id,
        "duration_ms": result.duration_ms,
    }


def _result_from_dict(raw: dict) -> RunResult:
    return RunResult(
        status=raw["status"],
        relationship=raw.get("relationship", "success"),
        content=base64.b64decode(raw.get("content_b64", "")),
        attributes=dict(raw.get("attributes", {})),
        retryable=bool(raw.get("retryable", False)),
        error_code=raw.get("error_code", ""),
        error_message=raw.get("error_message", ""),
        worker_id=raw.get("worker_id", ""),
        duration_ms=int(raw.get("duration_ms", 0)),
    )


def _outcome_class(result: RunResult) -> str:
    if result.status == "SUCCEEDED":
        return "SUCCESS"
    if result.retryable:
        return "RETRYABLE_FAILURE"
    return "USER_FAILURE"


class RunnerDB:
    """Durable Runner state backed by PostgreSQL.

    runner.runs is execution history.
    runner.idempotency_keys is the short-lived replay/claim table.
    """

    def __init__(self, dsn: str, *, min_pool_size: int = 1, max_pool_size: int = 16):
        if not dsn:
            raise ValueError("PostgreSQL DSN is required")

        self.pool = ConnectionPool(
            conninfo=dsn,
            min_size=min_pool_size,
            max_size=max_pool_size,
            kwargs={"row_factory": dict_row},
            open=True,
        )
        self._init_schema()

    def _init_schema(self) -> None:
        with self.pool.connection() as conn:
            for statement in LEASE_SCHEMA_STATEMENTS:
                conn.execute(statement)

            table_exists = conn.execute(
                "SELECT to_regclass('runner.runs') IS NOT NULL AS present"
            ).fetchone()["present"]

            if not table_exists:
                conn.execute(RUNS_CREATE_SQL)
            else:
                self._migrate_legacy_runs(conn)

            conn.execute(IDEMPOTENCY_CREATE_SQL)

            index_statements = (
                "CREATE INDEX IF NOT EXISTS ix_runner_runs_started ON runner.runs(started_at)",
                "CREATE INDEX IF NOT EXISTS ix_runner_runs_completed ON runner.runs(completed_at)",
                "CREATE INDEX IF NOT EXISTS ix_runner_runs_release ON runner.runs(tenant_id, release_id, started_at)",
                "CREATE INDEX IF NOT EXISTS ix_runner_runs_error ON runner.runs(error_code, started_at)",
                "CREATE INDEX IF NOT EXISTS ix_runner_runs_invocation ON runner.runs(invocation_id)",
                "CREATE INDEX IF NOT EXISTS ix_runner_runs_idempotency ON runner.runs(tenant_id, release_id, idempotency_key)",
                "CREATE INDEX IF NOT EXISTS ix_runner_idempotency_expiry ON runner.idempotency_keys(expires_at)",
            )
            for statement in index_statements:
                conn.execute(statement)

    @staticmethod
    def _migrate_legacy_runs(conn) -> None:
        columns = {
            row["column_name"]
            for row in conn.execute(
                """
                SELECT column_name
                FROM information_schema.columns
                WHERE table_schema = 'runner' AND table_name = 'runs'
                """
            ).fetchall()
        }

        if "run_id" in columns:
            return

        # v3.3 pre-split schema used runs both as audit history and idempotency cache.
        # Migrate it in-place, preserving old rows as history. Old replay cache is intentionally
        # not copied into runner.idempotency_keys; the new cache starts clean after this upgrade.
        additions = (
            "ALTER TABLE runner.runs ADD COLUMN run_id UUID",
            "ALTER TABLE runner.runs ADD COLUMN project_id TEXT",
            "ALTER TABLE runner.runs ADD COLUMN processor_id TEXT",
            "ALTER TABLE runner.runs ADD COLUMN outcome_class TEXT",
            "ALTER TABLE runner.runs ADD COLUMN status TEXT",
            "ALTER TABLE runner.runs ADD COLUMN relationship TEXT",
            "ALTER TABLE runner.runs ADD COLUMN retryable BOOLEAN",
            "ALTER TABLE runner.runs ADD COLUMN error_code TEXT",
            "ALTER TABLE runner.runs ADD COLUMN error_message TEXT",
            "ALTER TABLE runner.runs ADD COLUMN worker_id TEXT",
            "ALTER TABLE runner.runs ADD COLUMN duration_ms BIGINT",
            "ALTER TABLE runner.runs ADD COLUMN replayable BOOLEAN NOT NULL DEFAULT FALSE",
            "ALTER TABLE runner.runs ADD COLUMN idempotency_expires_at TIMESTAMPTZ",
            "ALTER TABLE runner.runs ADD COLUMN started_at TIMESTAMPTZ",
            "ALTER TABLE runner.runs ADD COLUMN completed_at TIMESTAMPTZ",
        )
        for statement in additions:
            conn.execute(statement)

        legacy_rows = conn.execute(
            """
            SELECT tenant_id, release_id, idempotency_key
            FROM runner.runs
            WHERE run_id IS NULL
            """
        ).fetchall()

        for row in legacy_rows:
            conn.execute(
                """
                UPDATE runner.runs
                SET run_id = %s
                WHERE tenant_id = %s
                  AND release_id = %s
                  AND idempotency_key = %s
                """,
                (str(uuid.uuid4()), row["tenant_id"], row["release_id"], row["idempotency_key"]),
            )

        conn.execute("ALTER TABLE runner.runs DROP CONSTRAINT IF EXISTS runs_pkey")
        conn.execute("ALTER TABLE runner.runs DROP CONSTRAINT IF EXISTS runs_state_check")
        conn.execute("ALTER TABLE runner.runs ALTER COLUMN run_id SET NOT NULL")
        conn.execute("ALTER TABLE runner.runs ADD CONSTRAINT runs_pkey PRIMARY KEY (run_id)")
        conn.execute(
            """
            ALTER TABLE runner.runs
            ADD CONSTRAINT runs_state_check
            CHECK (state IN ('RUNNING', 'DONE', 'ABANDONED'))
            """
        )

        conn.execute(
            """
            UPDATE runner.runs
            SET started_at = COALESCE(started_at, created_at),
                completed_at = CASE
                    WHEN state = 'DONE' THEN COALESCE(completed_at, updated_at)
                    ELSE completed_at
                END,
                status = COALESCE(status, result_json->>'status'),
                relationship = COALESCE(relationship, result_json->>'relationship'),
                retryable = COALESCE(retryable, (result_json->>'retryable')::boolean),
                error_code = COALESCE(error_code, result_json->>'error_code'),
                error_message = COALESCE(error_message, result_json->>'error_message'),
                worker_id = COALESCE(worker_id, result_json->>'worker_id'),
                duration_ms = COALESCE(duration_ms, (result_json->>'duration_ms')::bigint),
                outcome_class = COALESCE(
                    outcome_class,
                    CASE
                        WHEN state = 'DONE' AND result_json->>'status' = 'SUCCEEDED' THEN 'SUCCESS'
                        WHEN state = 'DONE' AND COALESCE((result_json->>'retryable')::boolean, FALSE)
                            THEN 'RETRYABLE_FAILURE'
                        WHEN state = 'DONE' THEN 'USER_FAILURE'
                        ELSE NULL
                    END
                )
            """
        )
        conn.execute("ALTER TABLE runner.runs ALTER COLUMN started_at SET DEFAULT now()")
        conn.execute("ALTER TABLE runner.runs ALTER COLUMN started_at SET NOT NULL")

    def close(self) -> None:
        self.pool.close()

    def create_lease(
        self,
        tenant_id: str,
        project_id: str,
        processor_id: str,
        release_id: str,
        ttl_ms: int,
    ) -> Lease:
        lease_id = str(uuid.uuid4())
        expires_at = datetime.now(timezone.utc) + timedelta(milliseconds=ttl_ms)

        with self.pool.connection() as conn:
            conn.execute(
                """
                INSERT INTO runner.leases(
                    id, tenant_id, project_id, processor_id, release_id, expires_at
                ) VALUES (%s, %s, %s, %s, %s, %s)
                """,
                (lease_id, tenant_id, project_id, processor_id, release_id, expires_at),
            )

        return Lease(
            id=lease_id,
            tenant_id=tenant_id,
            project_id=project_id,
            processor_id=processor_id,
            release_id=release_id,
            expires_at_ms=_epoch_ms(expires_at),
        )

    def require_lease(
        self,
        lease_id: str,
        *,
        tenant_id: str,
        release_id: str | None = None,
    ) -> Lease:
        with self.pool.connection() as conn:
            row = conn.execute(
                """
                SELECT id, tenant_id, project_id, processor_id, release_id, expires_at
                FROM runner.leases
                WHERE id = %s
                """,
                (lease_id,),
            ).fetchone()

        if row is None:
            raise LeaseError("LEASE_UNKNOWN", "lease does not exist", retryable=True)
        if row["tenant_id"] != tenant_id:
            raise LeaseError("LEASE_FORBIDDEN", "lease belongs to another tenant")
        if release_id is not None and row["release_id"] != release_id:
            raise LeaseError("LEASE_RELEASE_MISMATCH", "lease is for another release")
        if row["expires_at"] <= datetime.now(timezone.utc):
            raise LeaseError("LEASE_EXPIRED", "lease expired", retryable=True)

        return Lease(
            id=str(row["id"]),
            tenant_id=row["tenant_id"],
            project_id=row["project_id"],
            processor_id=row["processor_id"],
            release_id=row["release_id"],
            expires_at_ms=_epoch_ms(row["expires_at"]),
        )

    def renew_lease(self, lease_id: str, *, tenant_id: str, ttl_ms: int) -> Lease:
        current = self.require_lease(lease_id, tenant_id=tenant_id)
        expires_at = datetime.now(timezone.utc) + timedelta(milliseconds=ttl_ms)

        with self.pool.connection() as conn:
            conn.execute(
                """
                UPDATE runner.leases
                SET expires_at = %s, updated_at = now()
                WHERE id = %s AND tenant_id = %s
                """,
                (expires_at, lease_id, tenant_id),
            )

        return Lease(
            id=current.id,
            tenant_id=current.tenant_id,
            project_id=current.project_id,
            processor_id=current.processor_id,
            release_id=current.release_id,
            expires_at_ms=_epoch_ms(expires_at),
        )

    def delete_lease(self, lease_id: str, *, tenant_id: str) -> None:
        with self.pool.connection() as conn:
            row = conn.execute(
                "SELECT tenant_id FROM runner.leases WHERE id = %s",
                (lease_id,),
            ).fetchone()
            if row is None:
                return
            if row["tenant_id"] != tenant_id:
                raise LeaseError("LEASE_FORBIDDEN", "lease belongs to another tenant")
            conn.execute("DELETE FROM runner.leases WHERE id = %s", (lease_id,))

    def purge_expired_leases(self) -> int:
        with self.pool.connection() as conn:
            cursor = conn.execute("DELETE FROM runner.leases WHERE expires_at <= now()")
            return cursor.rowcount

    def reset_running(self) -> int:
        """Convert stale executions from the previous Runner process into audit history."""
        with self.pool.connection() as conn:
            cursor = conn.execute(
                """
                UPDATE runner.runs
                SET state = 'ABANDONED',
                    outcome_class = 'INFRASTRUCTURE_FAILURE',
                    status = 'FAILED',
                    relationship = 'retry',
                    retryable = TRUE,
                    error_code = 'RUNNER_RESTART',
                    error_message = 'Runner restarted while invocation was active',
                    replayable = FALSE,
                    idempotency_expires_at = now(),
                    completed_at = now(),
                    updated_at = now()
                WHERE state = 'RUNNING'
                """
            )
            conn.execute("DELETE FROM runner.idempotency_keys WHERE state = 'RUNNING'")
            return cursor.rowcount

    def begin_run(
        self,
        *,
        key: str,
        tenant_id: str,
        project_id: str,
        processor_id: str,
        lease_id: str,
        release_id: str,
        invocation_id: str,
        request_fingerprint: str,
    ) -> RunClaim:
        now = datetime.now(timezone.utc)

        with self.pool.connection() as conn:
            row = conn.execute(
                """
                SELECT request_fingerprint, state, current_run_id, replayable, result_json, expires_at
                FROM runner.idempotency_keys
                WHERE tenant_id = %s AND release_id = %s AND idempotency_key = %s
                FOR UPDATE
                """,
                (tenant_id, release_id, key),
            ).fetchone()

            if row is not None:
                if row["request_fingerprint"] != request_fingerprint:
                    raise RunnerError(
                        "IDEMPOTENCY_CONFLICT",
                        "idempotency key was reused for different request content",
                    )

                if row["state"] == "RUNNING":
                    raise RunnerError(
                        "RUN_IN_PROGRESS",
                        "same logical request is already running",
                        retryable=True,
                    )

                cache_valid = (
                    row["state"] == "DONE"
                    and row["replayable"]
                    and row["result_json"] is not None
                    and row["expires_at"] is not None
                    and row["expires_at"] > now
                )
                if cache_valid:
                    conn.execute(
                        """
                        UPDATE runner.idempotency_keys
                        SET replay_count = replay_count + 1,
                            last_replayed_at = now(),
                            updated_at = now()
                        WHERE tenant_id = %s AND release_id = %s AND idempotency_key = %s
                        """,
                        (tenant_id, release_id, key),
                    )
                    return RunClaim(run_id=None, cached_result=_result_from_dict(row["result_json"]))

            run_id = str(uuid.uuid4())

            if row is None:
                conn.execute(
                    """
                    INSERT INTO runner.idempotency_keys(
                        tenant_id, release_id, idempotency_key, request_fingerprint,
                        state, current_run_id, lease_id, invocation_id
                    )
                    VALUES (%s, %s, %s, %s, 'RUNNING', %s, %s, %s)
                    """,
                    (tenant_id, release_id, key, request_fingerprint, run_id, lease_id, invocation_id),
                )
            else:
                conn.execute(
                    """
                    UPDATE runner.idempotency_keys
                    SET state = 'RUNNING',
                        current_run_id = %s,
                        lease_id = %s,
                        invocation_id = %s,
                        replayable = FALSE,
                        result_json = NULL,
                        expires_at = NULL,
                        updated_at = now()
                    WHERE tenant_id = %s AND release_id = %s AND idempotency_key = %s
                    """,
                    (run_id, lease_id, invocation_id, tenant_id, release_id, key),
                )

            conn.execute(
                """
                INSERT INTO runner.runs(
                    run_id, tenant_id, project_id, processor_id, release_id, lease_id,
                    invocation_id, idempotency_key, request_fingerprint, state, started_at
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, 'RUNNING', now())
                """,
                (
                    run_id,
                    tenant_id,
                    project_id,
                    processor_id,
                    release_id,
                    lease_id,
                    invocation_id,
                    key,
                    request_fingerprint,
                ),
            )

        return RunClaim(run_id=run_id, cached_result=None)

    def finish_run(
        self,
        *,
        run_id: str,
        key: str,
        tenant_id: str,
        release_id: str,
        result: RunResult,
        idempotency_retention_ms: int,
    ) -> None:
        replayable = not result.retryable
        expires_at = datetime.now(timezone.utc)
        if replayable:
            expires_at += timedelta(milliseconds=idempotency_retention_ms)

        with self.pool.connection() as conn:
            cursor = conn.execute(
                """
                UPDATE runner.runs
                SET state = 'DONE',
                    outcome_class = %s,
                    status = %s,
                    relationship = %s,
                    retryable = %s,
                    error_code = %s,
                    error_message = %s,
                    worker_id = %s,
                    duration_ms = %s,
                    replayable = %s,
                    idempotency_expires_at = %s,
                    completed_at = now(),
                    updated_at = now()
                WHERE run_id = %s AND state = 'RUNNING'
                """,
                (
                    _outcome_class(result),
                    result.status,
                    result.relationship,
                    result.retryable,
                    result.error_code,
                    result.error_message,
                    result.worker_id,
                    result.duration_ms,
                    replayable,
                    expires_at,
                    run_id,
                ),
            )
            if cursor.rowcount != 1:
                raise RunnerError(
                    "RUN_STATE_LOST",
                    "RUNNING audit row disappeared before completion",
                    retryable=True,
                )

            cursor = conn.execute(
                """
                UPDATE runner.idempotency_keys
                SET state = 'DONE',
                    replayable = %s,
                    result_json = %s,
                    expires_at = %s,
                    updated_at = now()
                WHERE tenant_id = %s
                  AND release_id = %s
                  AND idempotency_key = %s
                  AND current_run_id = %s
                  AND state = 'RUNNING'
                """,
                (
                    replayable,
                    Jsonb(_result_to_dict(result)),
                    expires_at,
                    tenant_id,
                    release_id,
                    key,
                    run_id,
                ),
            )
            if cursor.rowcount != 1:
                raise RunnerError(
                    "IDEMPOTENCY_STATE_LOST",
                    "idempotency claim disappeared before completion",
                    retryable=True,
                )

    def abandon_run(
        self,
        *,
        run_id: str,
        key: str,
        tenant_id: str,
        release_id: str,
        error_code: str,
        error_message: str,
    ) -> None:
        with self.pool.connection() as conn:
            conn.execute(
                """
                UPDATE runner.runs
                SET state = 'ABANDONED',
                    outcome_class = 'INFRASTRUCTURE_FAILURE',
                    status = 'FAILED',
                    relationship = 'retry',
                    retryable = TRUE,
                    error_code = %s,
                    error_message = %s,
                    replayable = FALSE,
                    idempotency_expires_at = now(),
                    completed_at = now(),
                    updated_at = now()
                WHERE run_id = %s AND state = 'RUNNING'
                """,
                (error_code, error_message, run_id),
            )
            conn.execute(
                """
                DELETE FROM runner.idempotency_keys
                WHERE tenant_id = %s
                  AND release_id = %s
                  AND idempotency_key = %s
                  AND current_run_id = %s
                  AND state = 'RUNNING'
                """,
                (tenant_id, release_id, key, run_id),
            )

    def purge_expired_idempotency(self) -> int:
        with self.pool.connection() as conn:
            cursor = conn.execute(
                """
                DELETE FROM runner.idempotency_keys
                WHERE state = 'DONE'
                  AND expires_at IS NOT NULL
                  AND expires_at <= now()
                """
            )
            return cursor.rowcount

    def purge_old_runs(self, older_than_ms: int) -> int:
        if older_than_ms <= 0:
            return 0

        cutoff = datetime.now(timezone.utc) - timedelta(milliseconds=older_than_ms)

        with self.pool.connection() as conn:
            cursor = conn.execute(
                """
                DELETE FROM runner.runs AS r
                WHERE r.state IN ('DONE', 'ABANDONED')
                  AND COALESCE(r.completed_at, r.updated_at) < %s
                  AND NOT EXISTS (
                      SELECT 1
                      FROM runner.idempotency_keys AS i
                      WHERE i.current_run_id = r.run_id
                        AND i.state = 'DONE'
                        AND i.expires_at > now()
                  )
                """,
                (cutoff,),
            )
            return cursor.rowcount
