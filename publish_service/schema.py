from __future__ import annotations


PUBLISH_SCHEMA_STATEMENTS = (
    "CREATE SCHEMA IF NOT EXISTS publish",
    """
    CREATE TABLE IF NOT EXISTS publish.operators (
        id UUID PRIMARY KEY,
        user_id BIGINT NOT NULL DEFAULT 1,
        workspace TEXT NOT NULL,
        name TEXT NOT NULL,
        display_name TEXT NOT NULL,
        description TEXT NOT NULL DEFAULT '',
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
    )
    """,
    """
    ALTER TABLE publish.operators
    ADD COLUMN IF NOT EXISTS user_id BIGINT NOT NULL DEFAULT 1
    """,
    """
    ALTER TABLE publish.operators
    DROP CONSTRAINT IF EXISTS operators_workspace_name_key
    """,
    """
    CREATE UNIQUE INDEX IF NOT EXISTS ux_publish_operators_user_workspace_name
    ON publish.operators(user_id, workspace, name)
    """,
    """
    CREATE TABLE IF NOT EXISTS publish.virtual_contract_versions (
        id UUID PRIMARY KEY,
        operator_id UUID NOT NULL REFERENCES publish.operators(id),
        version INTEGER NOT NULL CHECK (version > 0),
        source_revision VARCHAR(64) NOT NULL,
        source_ref TEXT NOT NULL,
        contract_sha256 VARCHAR(64) NOT NULL,
        contract_json JSONB NOT NULL CHECK (jsonb_typeof(contract_json) = 'object'),
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        UNIQUE(operator_id, version),
        UNIQUE(operator_id, contract_sha256)
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS ix_publish_contract_operator_created
    ON publish.virtual_contract_versions(operator_id, version DESC)
    """,
    """
    CREATE INDEX IF NOT EXISTS ix_publish_contract_source_revision
    ON publish.virtual_contract_versions(source_revision)
    """,
    """
    CREATE TABLE IF NOT EXISTS publish.backend_variants (
        id UUID PRIMARY KEY,
        contract_id UUID NOT NULL REFERENCES publish.virtual_contract_versions(id),
        backend TEXT NOT NULL,
        compiler_version TEXT NOT NULL,
        variant_key VARCHAR(64) NOT NULL,
        options_json JSONB NOT NULL CHECK (jsonb_typeof(options_json) = 'object'),
        backend_contract_sha256 VARCHAR(64) NOT NULL,
        backend_contract_json JSONB NOT NULL CHECK (jsonb_typeof(backend_contract_json) = 'object'),
        status TEXT NOT NULL CHECK (status IN ('COMPILED', 'PUBLISHED', 'FAILED')),
        published_ref TEXT,
        published_metadata JSONB,
        last_error TEXT,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        UNIQUE(contract_id, backend, variant_key)
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS ix_publish_variant_contract_backend
    ON publish.backend_variants(contract_id, backend, created_at DESC)
    """,
    """
    CREATE TABLE IF NOT EXISTS publish.publish_jobs (
        id UUID PRIMARY KEY,
        variant_id UUID NOT NULL REFERENCES publish.backend_variants(id),
        status TEXT NOT NULL CHECK (status IN ('PENDING', 'RUNNING', 'READY', 'FAILED')),
        artifact_ref TEXT,
        result_json JSONB,
        error_message TEXT,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        started_at TIMESTAMPTZ,
        finished_at TIMESTAMPTZ
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS ix_publish_jobs_variant_created
    ON publish.publish_jobs(variant_id, created_at DESC)
    """,
    """
    CREATE TABLE IF NOT EXISTS publish.edge_native_deployments (
        id UUID PRIMARY KEY,
        user_id BIGINT NOT NULL,
        operator_id UUID NOT NULL REFERENCES publish.operators(id),
        variant_id UUID NOT NULL REFERENCES publish.backend_variants(id),
        target_os TEXT NOT NULL CHECK (target_os IN ('linux', 'windows')),
        target_arch TEXT NOT NULL CHECK (target_arch IN ('x86_64', 'aarch64')),
        python_version TEXT NOT NULL,
        uv_python_platform TEXT NOT NULL,
        package_name TEXT NOT NULL,
        artifact_file TEXT NOT NULL,
        requirements_json JSONB NOT NULL CHECK (jsonb_typeof(requirements_json) = 'array'),
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        UNIQUE(user_id, operator_id, target_os, target_arch, python_version)
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS ix_publish_edge_native_target
    ON publish.edge_native_deployments(user_id, target_os, target_arch, python_version)
    """,
    """
    CREATE TABLE IF NOT EXISTS publish.edge_dependency_bundles (
        id UUID PRIMARY KEY,
        user_id BIGINT NOT NULL,
        target_os TEXT NOT NULL CHECK (target_os IN ('linux', 'windows')),
        target_arch TEXT NOT NULL CHECK (target_arch IN ('x86_64', 'aarch64')),
        python_version TEXT NOT NULL,
        uv_python_platform TEXT NOT NULL,
        revision INTEGER NOT NULL CHECK (revision > 0),
        requirements_json JSONB NOT NULL CHECK (jsonb_typeof(requirements_json) = 'array'),
        requirements_lock TEXT NOT NULL,
        lock_sha256 VARCHAR(64) NOT NULL,
        artifact_ref TEXT NOT NULL,
        artifact_sha256 VARCHAR(64) NOT NULL,
        manifest_json JSONB NOT NULL CHECK (jsonb_typeof(manifest_json) = 'object'),
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        UNIQUE(user_id, target_os, target_arch, python_version, revision)
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS ix_publish_edge_bundle_target_revision
    ON publish.edge_dependency_bundles(
        user_id, target_os, target_arch, python_version, revision DESC
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS publish.edge_bundle_members (
        bundle_id UUID NOT NULL REFERENCES publish.edge_dependency_bundles(id) ON DELETE CASCADE,
        operator_id UUID NOT NULL REFERENCES publish.operators(id),
        variant_id UUID NOT NULL REFERENCES publish.backend_variants(id),
        package_name TEXT NOT NULL,
        artifact_file TEXT NOT NULL,
        requirements_json JSONB NOT NULL CHECK (jsonb_typeof(requirements_json) = 'array'),
        PRIMARY KEY(bundle_id, operator_id)
    )
    """,
)
