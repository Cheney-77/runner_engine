-- Run ONCE with a migration/DBA role against the ADMIN_DB_URL database.
-- This schema is owned by the console, not by publish/build/runner services.
CREATE SCHEMA IF NOT EXISTS console_ops;

CREATE TABLE IF NOT EXISTS console_ops.asset_notes (
    id UUID PRIMARY KEY,
    asset_type TEXT NOT NULL CHECK (asset_type IN (
        'build_environment','runner_lease','runner_run','publish_variant'
    )),
    asset_id TEXT NOT NULL CHECK (length(asset_id) BETWEEN 1 AND 160),
    note TEXT NOT NULL CHECK (length(note) BETWEEN 1 AND 1000),
    version INTEGER NOT NULL DEFAULT 1 CHECK (version > 0),
    created_by TEXT NOT NULL,
    updated_by TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE(asset_type, asset_id)
);

CREATE TABLE IF NOT EXISTS console_ops.cleanup_requests (
    id UUID PRIMARY KEY,
    environment_id UUID NOT NULL,
    env_key TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('DRAFT','CANCELLED')),
    reason TEXT NOT NULL CHECK (length(reason) BETWEEN 10 AND 1000),
    preflight_snapshot JSONB NOT NULL CHECK (jsonb_typeof(preflight_snapshot)='object'),
    created_by TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ix_console_ops_cleanup_env
    ON console_ops.cleanup_requests(environment_id, created_at DESC);

CREATE TABLE IF NOT EXISTS console_ops.audit_events (
    id UUID PRIMARY KEY,
    actor TEXT NOT NULL,
    action TEXT NOT NULL,
    resource_type TEXT NOT NULL,
    resource_id TEXT NOT NULL,
    occurred_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ix_console_ops_audit_latest
    ON console_ops.audit_events(occurred_at DESC);

-- Example permissions, adapt to your role and DBA practices:
-- GRANT USAGE ON SCHEMA console_ops TO console_admin;
-- GRANT SELECT, INSERT, UPDATE, DELETE ON console_ops.asset_notes TO console_admin;
-- GRANT SELECT, INSERT, UPDATE ON console_ops.cleanup_requests TO console_admin;
-- GRANT SELECT, INSERT ON console_ops.audit_events TO console_admin;
--
-- The console_admin role must NOT have UPDATE/DELETE on publish.*, build.*, runner.*.
