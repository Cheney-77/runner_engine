-- 多边端发布：仅扩展现有发布记录，不创建边端设备主表。
-- 必须在部署新版 Publish Service 之前执行；建议先备份数据库和 Bundle 目录。
-- 历史记录保持 token_pair/edge_name=NULL，不自动归属到任何新边端设备。
BEGIN;

ALTER TABLE publish.edge_native_deployments
    ADD COLUMN IF NOT EXISTS token_pair TEXT,
    ADD COLUMN IF NOT EXISTS edge_name TEXT;
ALTER TABLE publish.edge_dependency_bundles
    ADD COLUMN IF NOT EXISTS token_pair TEXT,
    ADD COLUMN IF NOT EXISTS edge_name TEXT;

DO $$
DECLARE
    old_constraint RECORD;
BEGIN
    FOR old_constraint IN
        SELECT c.conname
        FROM pg_constraint c
        WHERE c.conrelid = 'publish.edge_native_deployments'::regclass
          AND c.contype = 'u'
          AND pg_get_constraintdef(c.oid) = 'UNIQUE (user_id, operator_id, target_os, target_arch, python_version)'
    LOOP
        EXECUTE format('ALTER TABLE publish.edge_native_deployments DROP CONSTRAINT %I', old_constraint.conname);
    END LOOP;

    FOR old_constraint IN
        SELECT c.conname
        FROM pg_constraint c
        WHERE c.conrelid = 'publish.edge_dependency_bundles'::regclass
          AND c.contype = 'u'
          AND pg_get_constraintdef(c.oid) = 'UNIQUE (user_id, target_os, target_arch, python_version, revision)'
    LOOP
        EXECUTE format('ALTER TABLE publish.edge_dependency_bundles DROP CONSTRAINT %I', old_constraint.conname);
    END LOOP;
END;
$$;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'publish.edge_native_deployments'::regclass
          AND contype = 'u'
          AND pg_get_constraintdef(oid) = 'UNIQUE (user_id, operator_id, target_os, target_arch, python_version, token_pair, edge_name)'
    ) THEN
        ALTER TABLE publish.edge_native_deployments
            ADD CONSTRAINT edge_native_deployments_machine_key UNIQUE (
                user_id, operator_id, target_os, target_arch, python_version, token_pair, edge_name
            );
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'publish.edge_dependency_bundles'::regclass
          AND contype = 'u'
          AND pg_get_constraintdef(oid) = 'UNIQUE (user_id, target_os, target_arch, python_version, token_pair, edge_name, revision)'
    ) THEN
        ALTER TABLE publish.edge_dependency_bundles
            ADD CONSTRAINT edge_dependency_bundles_machine_key UNIQUE (
                user_id, target_os, target_arch, python_version, token_pair, edge_name, revision
            );
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'publish.edge_native_deployments'::regclass
          AND conname = 'edge_native_device_pair_check'
    ) THEN
        ALTER TABLE publish.edge_native_deployments
            ADD CONSTRAINT edge_native_device_pair_check CHECK (
                (token_pair IS NULL AND edge_name IS NULL) OR
                (token_pair IS NOT NULL AND edge_name IS NOT NULL AND btrim(token_pair) <> '' AND btrim(edge_name) <> '')
            );
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'publish.edge_dependency_bundles'::regclass
          AND conname = 'edge_bundle_device_pair_check'
    ) THEN
        ALTER TABLE publish.edge_dependency_bundles
            ADD CONSTRAINT edge_bundle_device_pair_check CHECK (
                (token_pair IS NULL AND edge_name IS NULL) OR
                (token_pair IS NOT NULL AND edge_name IS NOT NULL AND btrim(token_pair) <> '' AND btrim(edge_name) <> '')
            );
    END IF;
END;
$$;

-- 保证历史 NULL 身份数据仍遵守原有唯一性，且与新设备发布互不冲突。
CREATE UNIQUE INDEX IF NOT EXISTS ux_publish_edge_deployment_legacy
    ON publish.edge_native_deployments(user_id, operator_id, target_os, target_arch, python_version)
    WHERE token_pair IS NULL AND edge_name IS NULL;
CREATE UNIQUE INDEX IF NOT EXISTS ux_publish_edge_bundle_legacy
    ON publish.edge_dependency_bundles(user_id, target_os, target_arch, python_version, revision)
    WHERE token_pair IS NULL AND edge_name IS NULL;

CREATE INDEX IF NOT EXISTS ix_publish_edge_native_device_target
    ON publish.edge_native_deployments(user_id, target_os, target_arch, python_version, token_pair, edge_name);
CREATE INDEX IF NOT EXISTS ix_publish_edge_bundle_device_revision
    ON publish.edge_dependency_bundles(user_id, target_os, target_arch, python_version, token_pair, edge_name, revision DESC);

COMMIT;
