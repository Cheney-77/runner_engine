CREATE SCHEMA IF NOT EXISTS publish;

CREATE TABLE IF NOT EXISTS publish.operators (
        id UUID PRIMARY KEY,
        workspace TEXT NOT NULL,
        name TEXT NOT NULL,
        display_name TEXT NOT NULL,
        description TEXT NOT NULL DEFAULT '',
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        UNIQUE(workspace, name)
    );

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
    );

CREATE INDEX IF NOT EXISTS ix_publish_contract_operator_created
    ON publish.virtual_contract_versions(operator_id, version DESC);

CREATE INDEX IF NOT EXISTS ix_publish_contract_source_revision
    ON publish.virtual_contract_versions(source_revision);

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
    );

CREATE INDEX IF NOT EXISTS ix_publish_variant_contract_backend
    ON publish.backend_variants(contract_id, backend, created_at DESC);

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
    );

CREATE INDEX IF NOT EXISTS ix_publish_jobs_variant_created
    ON publish.publish_jobs(variant_id, created_at DESC);
