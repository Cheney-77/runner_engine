from __future__ import annotations

# Admin owns only console_ops. Statements are idempotent so the Admin process
# can initialize/upgrade its own schema on startup like the other services.
ADMIN_SCHEMA_STATEMENTS = (
    "CREATE SCHEMA IF NOT EXISTS console_ops",
    """
    CREATE TABLE IF NOT EXISTS console_ops.asset_notes (
        id UUID PRIMARY KEY,
        asset_type TEXT NOT NULL CHECK (
            asset_type IN (
                'build_environment',
                'runner_lease',
                'runner_run',
                'publish_variant'
            )
        ),
        asset_id TEXT NOT NULL CHECK (length(asset_id) BETWEEN 1 AND 160),
        note TEXT NOT NULL CHECK (length(note) BETWEEN 1 AND 1000),
        version INTEGER NOT NULL DEFAULT 1 CHECK (version > 0),
        created_by TEXT NOT NULL,
        updated_by TEXT NOT NULL,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        UNIQUE(asset_type, asset_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS console_ops.cleanup_requests (
        id UUID PRIMARY KEY,
        environment_id UUID NOT NULL,
        env_key TEXT NOT NULL,
        status TEXT NOT NULL CHECK (status IN ('DRAFT', 'CANCELLED')),
        reason TEXT NOT NULL CHECK (length(reason) BETWEEN 10 AND 1000),
        preflight_snapshot JSONB NOT NULL CHECK (
            jsonb_typeof(preflight_snapshot) = 'object'
        ),
        created_by TEXT NOT NULL,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS ix_console_ops_cleanup_env
    ON console_ops.cleanup_requests(environment_id, created_at DESC)
    """,
    """
    CREATE TABLE IF NOT EXISTS console_ops.audit_events (
        id UUID PRIMARY KEY,
        actor TEXT NOT NULL,
        action TEXT NOT NULL,
        resource_type TEXT NOT NULL,
        resource_id TEXT NOT NULL,
        occurred_at TIMESTAMPTZ NOT NULL DEFAULT now()
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS ix_console_ops_audit_latest
    ON console_ops.audit_events(occurred_at DESC)
    """,
    """
    CREATE TABLE IF NOT EXISTS console_ops.cleanup_executions (
        id UUID PRIMARY KEY,
        request_id UUID NOT NULL REFERENCES console_ops.cleanup_requests(id),
        environment_id UUID NOT NULL,
        status TEXT NOT NULL CHECK (
            status IN (
                'RUNNING',
                'BLOCKED',
                'WAITING',
                'SUCCEEDED',
                'FAILED',
                'PARTIAL'
            )
        ),
        message TEXT NOT NULL DEFAULT '',
        started_by TEXT NOT NULL,
        started_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        finished_at TIMESTAMPTZ,
        updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS ix_console_cleanup_executions_request
    ON console_ops.cleanup_executions(request_id, started_at DESC)
    """,
    """
    CREATE INDEX IF NOT EXISTS ix_console_cleanup_executions_status
    ON console_ops.cleanup_executions(status, updated_at DESC)
    """,
    """
    CREATE TABLE IF NOT EXISTS console_ops.cleanup_execution_steps (
        id UUID PRIMARY KEY,
        execution_id UUID NOT NULL REFERENCES console_ops.cleanup_executions(id)
            ON DELETE CASCADE,
        step_name TEXT NOT NULL,
        status TEXT NOT NULL CHECK (
            status IN (
                'RUNNING',
                'SUCCEEDED',
                'BLOCKED',
                'WAITING',
                'FAILED'
            )
        ),
        detail_json JSONB NOT NULL DEFAULT '{}'::jsonb CHECK (
            jsonb_typeof(detail_json) = 'object'
        ),
        occurred_at TIMESTAMPTZ NOT NULL DEFAULT now()
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS ix_console_cleanup_steps_execution
    ON console_ops.cleanup_execution_steps(execution_id, occurred_at)
    """,
)


def ensure_admin_schema(conn) -> None:
    for statement in ADMIN_SCHEMA_STATEMENTS:
        conn.execute(statement)
