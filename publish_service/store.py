from __future__ import annotations

import uuid
from contextlib import contextmanager
from typing import Any, Iterator

from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool

from operator_authoring.compiler import contract_sha256
from operator_authoring.model import VirtualOperatorContract

from .schema import PUBLISH_SCHEMA_STATEMENTS


class PublishStore:
    def __init__(self, dsn: str, *, min_pool_size: int = 1, max_pool_size: int = 8):
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
            for statement in PUBLISH_SCHEMA_STATEMENTS:
                conn.execute(statement)

    def close(self) -> None:
        self.pool.close()

    def save_virtual_contract(
        self,
        workspace: str,
        contract: VirtualOperatorContract,
        *,
        user_id: int = 1,
    ):
        digest = contract_sha256(contract)
        payload = contract.model_dump(mode="json")
        operator_id = str(uuid.uuid4())
        contract_id = str(uuid.uuid4())

        with self.pool.connection() as conn:
            conn.execute(
                """
                INSERT INTO publish.operators(
                    id, user_id, workspace, name, display_name, description
                )
                VALUES (%s, %s, %s, %s, %s, %s)
                ON CONFLICT (user_id, workspace, name) DO UPDATE
                SET display_name = EXCLUDED.display_name,
                    description = EXCLUDED.description,
                    updated_at = now()
                """,
                (
                    operator_id,
                    user_id,
                    workspace,
                    contract.metadata.name,
                    contract.metadata.display_name,
                    contract.metadata.description,
                ),
            )

            operator = conn.execute(
                """
                SELECT *
                FROM publish.operators
                WHERE user_id = %s AND workspace = %s AND name = %s
                FOR UPDATE
                """,
                (user_id, workspace, contract.metadata.name),
            ).fetchone()

            if operator is None:
                raise RuntimeError("failed to create or load logical operator")

            existing = conn.execute(
                """
                SELECT *
                FROM publish.virtual_contract_versions
                WHERE operator_id = %s AND contract_sha256 = %s
                """,
                (operator["id"], digest),
            ).fetchone()

            if existing is not None:
                return operator, existing, False

            next_version = conn.execute(
                """
                SELECT COALESCE(MAX(version), 0) + 1 AS next_version
                FROM publish.virtual_contract_versions
                WHERE operator_id = %s
                """,
                (operator["id"],),
            ).fetchone()["next_version"]

            row = conn.execute(
                """
                INSERT INTO publish.virtual_contract_versions(
                    id, operator_id, version, source_revision, source_ref,
                    contract_sha256, contract_json
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                RETURNING *
                """,
                (
                    contract_id,
                    operator["id"],
                    next_version,
                    contract.source.source_revision,
                    contract.source.source_ref,
                    digest,
                    Jsonb(payload),
                ),
            ).fetchone()

            return operator, row, True

    def get_operator(self, operator_id: str, *, user_id: int = 1):
        with self.pool.connection() as conn:
            return conn.execute(
                """
                SELECT *
                FROM publish.operators
                WHERE id = %s AND user_id = %s
                """,
                (operator_id, user_id),
            ).fetchone()

    def get_latest_contract(self, operator_id: str, *, user_id: int = 1):
        with self.pool.connection() as conn:
            return conn.execute(
                """
                SELECT c.*
                FROM publish.virtual_contract_versions c
                JOIN publish.operators o ON o.id = c.operator_id
                WHERE c.operator_id = %s AND o.user_id = %s
                ORDER BY c.version DESC
                LIMIT 1
                """,
                (operator_id, user_id),
            ).fetchone()

    def get_contract(self, contract_id: str, *, user_id: int = 1):
        with self.pool.connection() as conn:
            return conn.execute(
                """
                SELECT c.*, o.workspace, o.name, o.display_name, o.description, o.user_id
                FROM publish.virtual_contract_versions c
                JOIN publish.operators o ON o.id = c.operator_id
                WHERE c.id = %s AND o.user_id = %s
                """,
                (contract_id, user_id),
            ).fetchone()

    def list_variants(self, contract_id: str):
        with self.pool.connection() as conn:
            return conn.execute(
                """
                SELECT *
                FROM publish.backend_variants
                WHERE contract_id = %s
                ORDER BY backend, created_at DESC
                """,
                (contract_id,),
            ).fetchall()

    def upsert_variant(
        self,
        *,
        contract_id: str,
        backend: str,
        compiler_version: str,
        variant_key: str,
        options: dict[str, Any],
        backend_contract_sha256: str,
        backend_contract: dict[str, Any],
    ):
        variant_id = str(uuid.uuid4())

        with self.pool.connection() as conn:
            return conn.execute(
                """
                INSERT INTO publish.backend_variants(
                    id, contract_id, backend, compiler_version, variant_key,
                    options_json, backend_contract_sha256, backend_contract_json, status
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 'COMPILED')
                ON CONFLICT (contract_id, backend, variant_key) DO UPDATE
                SET compiler_version = EXCLUDED.compiler_version,
                    options_json = EXCLUDED.options_json,
                    backend_contract_sha256 = EXCLUDED.backend_contract_sha256,
                    backend_contract_json = EXCLUDED.backend_contract_json,
                    status = CASE
                        WHEN publish.backend_variants.status = 'PUBLISHED' THEN 'PUBLISHED'
                        ELSE 'COMPILED'
                    END,
                    last_error = NULL,
                    updated_at = now()
                RETURNING *
                """,
                (
                    variant_id,
                    contract_id,
                    backend,
                    compiler_version,
                    variant_key,
                    Jsonb(options),
                    backend_contract_sha256,
                    Jsonb(backend_contract),
                ),
            ).fetchone()

    def create_publish_job(self, variant_id: str):
        job_id = str(uuid.uuid4())

        with self.pool.connection() as conn:
            return conn.execute(
                """
                INSERT INTO publish.publish_jobs(id, variant_id, status)
                VALUES (%s, %s, 'PENDING')
                RETURNING *
                """,
                (job_id, variant_id),
            ).fetchone()

    def mark_job_running(self, job_id: str) -> None:
        with self.pool.connection() as conn:
            conn.execute(
                """
                UPDATE publish.publish_jobs
                SET status = 'RUNNING', started_at = now()
                WHERE id = %s
                """,
                (job_id,),
            )

    def mark_job_ready(
        self,
        job_id: str,
        variant_id: str,
        *,
        artifact_ref: str,
        result: dict[str, Any],
    ) -> None:
        with self.pool.connection() as conn:
            conn.execute(
                """
                UPDATE publish.publish_jobs
                SET status = 'READY', artifact_ref = %s, result_json = %s,
                    error_message = NULL, finished_at = now()
                WHERE id = %s
                """,
                (artifact_ref, Jsonb(result), job_id),
            )

            conn.execute(
                """
                UPDATE publish.backend_variants
                SET status = 'PUBLISHED', published_ref = %s,
                    published_metadata = %s, last_error = NULL, updated_at = now()
                WHERE id = %s
                """,
                (artifact_ref, Jsonb(result), variant_id),
            )

    def mark_job_failed(self, job_id: str, variant_id: str, error_message: str) -> None:
        message = error_message[-8192:]

        with self.pool.connection() as conn:
            conn.execute(
                """
                UPDATE publish.publish_jobs
                SET status = 'FAILED', error_message = %s, finished_at = now()
                WHERE id = %s
                """,
                (message, job_id),
            )

            conn.execute(
                """
                UPDATE publish.backend_variants
                SET status = CASE
                        WHEN status = 'PUBLISHED' THEN 'PUBLISHED'
                        ELSE 'FAILED'
                    END,
                    last_error = %s,
                    updated_at = now()
                WHERE id = %s
                """,
                (message, variant_id),
            )

    @contextmanager
    def edge_publish_lock(
        self,
        *,
        user_id: int,
        target_os: str,
        target_arch: str,
        python_version: str,
    ) -> Iterator[Any]:
        key = f"edge-native:{user_id}:{target_os}:{target_arch}:{python_version}"

        with self.pool.connection() as conn:
            previous_autocommit = conn.autocommit
            conn.autocommit = True

            conn.execute(
                "SELECT pg_advisory_lock(hashtextextended(%s, 0))",
                (key,),
            )

            try:
                yield conn
            finally:
                conn.execute(
                    "SELECT pg_advisory_unlock(hashtextextended(%s, 0))",
                    (key,),
                )
                conn.autocommit = previous_autocommit

    def list_edge_deployments(
        self,
        *,
        user_id: int,
        target_os: str,
        target_arch: str,
        python_version: str,
        exclude_operator_id: str | None = None,
        conn: Any | None = None,
    ):
        clauses = [
            "user_id = %s",
            "target_os = %s",
            "target_arch = %s",
            "python_version = %s",
        ]
        params: list[Any] = [user_id, target_os, target_arch, python_version]

        if exclude_operator_id is not None:
            clauses.append("operator_id <> %s")
            params.append(exclude_operator_id)

        sql = f"""
            SELECT *
            FROM publish.edge_native_deployments
            WHERE {' AND '.join(clauses)}
            ORDER BY operator_id
        """

        if conn is not None:
            return conn.execute(sql, params).fetchall()

        with self.pool.connection() as pooled_conn:
            return pooled_conn.execute(sql, params).fetchall()

    def list_edge_deployments_for_user(self, *, user_id: int):
        with self.pool.connection() as conn:
            return conn.execute(
                """
                SELECT d.*, o.workspace, o.name, o.display_name, o.description
                FROM publish.edge_native_deployments d
                JOIN publish.operators o ON o.id = d.operator_id
                WHERE d.user_id = %s
                ORDER BY d.target_os, d.target_arch, d.python_version, o.display_name
                """,
                (user_id,),
            ).fetchall()

    def list_edge_bundles(self, *, user_id: int, limit: int = 50):
        limit = max(1, min(int(limit), 200))
        with self.pool.connection() as conn:
            return conn.execute(
                """
                SELECT *
                FROM publish.edge_dependency_bundles
                WHERE user_id = %s
                ORDER BY created_at DESC
                LIMIT %s
                """,
                (user_id, limit),
            ).fetchall()

    def get_edge_bundle(self, *, user_id: int, bundle_id: str):
        with self.pool.connection() as conn:
            return conn.execute(
                """
                SELECT *
                FROM publish.edge_dependency_bundles
                WHERE id = %s AND user_id = %s
                """,
                (bundle_id, user_id),
            ).fetchone()

    def list_edge_operations(self, *, user_id: int, limit: int = 100):
        limit = max(1, min(int(limit), 300))
        with self.pool.connection() as conn:
            return conn.execute(
                """
                SELECT
                    j.id AS job_id,
                    j.status AS job_status,
                    j.artifact_ref,
                    j.error_message,
                    j.created_at,
                    j.started_at,
                    j.finished_at,
                    v.id AS variant_id,
                    v.status AS variant_status,
                    v.options_json,
                    v.published_metadata,
                    o.id AS operator_id,
                    o.workspace,
                    o.name,
                    o.display_name
                FROM publish.publish_jobs j
                JOIN publish.backend_variants v ON v.id = j.variant_id
                JOIN publish.virtual_contract_versions c ON c.id = v.contract_id
                JOIN publish.operators o ON o.id = c.operator_id
                WHERE o.user_id = %s
                  AND v.backend = 'nifi_native'
                ORDER BY j.created_at DESC
                LIMIT %s
                """,
                (user_id, limit),
            ).fetchall()

    def next_edge_bundle_revision(
        self,
        *,
        user_id: int,
        target_os: str,
        target_arch: str,
        python_version: str,
        conn: Any | None = None,
    ) -> int:
        sql = """
                SELECT COALESCE(MAX(revision), 0) + 1 AS next_revision
                FROM publish.edge_dependency_bundles
                WHERE user_id = %s
                  AND target_os = %s
                  AND target_arch = %s
                  AND python_version = %s
                """
        params = (user_id, target_os, target_arch, python_version)

        if conn is not None:
            row = conn.execute(sql, params).fetchone()
            return int(row["next_revision"])

        with self.pool.connection() as pooled_conn:
            row = pooled_conn.execute(
                sql,
                params,
            ).fetchone()

        return int(row["next_revision"])

    def commit_edge_publish(
        self,
        *,
        user_id: int,
        operator_id: str,
        variant_id: str,
        target_os: str,
        target_arch: str,
        python_version: str,
        uv_python_platform: str,
        package_name: str,
        native_artifact_file: str,
        candidate_requirements: list[str],
        bundle_id: str,
        job_id: str,
        published_result: dict[str, Any],
        bundle_revision: int,
        bundle_requirements: list[str],
        requirements_lock: str,
        lock_sha256: str,
        bundle_artifact_ref: str,
        bundle_artifact_sha256: str,
        manifest: dict[str, Any],
        members: list[dict[str, Any]],
        conn: Any | None = None,
    ) -> str:
        deployment_id = str(uuid.uuid4())

        def commit(active_conn) -> None:
            active_conn.execute(
                """
                INSERT INTO publish.edge_native_deployments(
                    id, user_id, operator_id, variant_id,
                    target_os, target_arch, python_version, uv_python_platform,
                    package_name, artifact_file, requirements_json
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (
                    user_id, operator_id, target_os, target_arch, python_version
                )
                DO UPDATE SET
                    variant_id = EXCLUDED.variant_id,
                    uv_python_platform = EXCLUDED.uv_python_platform,
                    package_name = EXCLUDED.package_name,
                    artifact_file = EXCLUDED.artifact_file,
                    requirements_json = EXCLUDED.requirements_json,
                    updated_at = now()
                """,
                (
                    deployment_id,
                    user_id,
                    operator_id,
                    variant_id,
                    target_os,
                    target_arch,
                    python_version,
                    uv_python_platform,
                    package_name,
                    native_artifact_file,
                    Jsonb(candidate_requirements),
                ),
            )

            active_conn.execute(
                """
                INSERT INTO publish.edge_dependency_bundles(
                    id, user_id, target_os, target_arch, python_version,
                    uv_python_platform, revision, requirements_json,
                    requirements_lock, lock_sha256, artifact_ref,
                    artifact_sha256, manifest_json
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    bundle_id,
                    user_id,
                    target_os,
                    target_arch,
                    python_version,
                    uv_python_platform,
                    bundle_revision,
                    Jsonb(bundle_requirements),
                    requirements_lock,
                    lock_sha256,
                    bundle_artifact_ref,
                    bundle_artifact_sha256,
                    Jsonb(manifest),
                ),
            )

            for member in members:
                active_conn.execute(
                    """
                    INSERT INTO publish.edge_bundle_members(
                        bundle_id, operator_id, variant_id,
                        package_name, artifact_file, requirements_json
                    )
                    VALUES (%s, %s, %s, %s, %s, %s)
                    """,
                    (
                        bundle_id,
                        member["operator_id"],
                        member["variant_id"],
                        member["package_name"],
                        member["artifact_file"],
                        Jsonb(list(member.get("requirements") or [])),
                    ),
                )

            active_conn.execute(
                """
                UPDATE publish.publish_jobs
                SET status = 'READY',
                    artifact_ref = %s,
                    result_json = %s,
                    error_message = NULL,
                    finished_at = now()
                WHERE id = %s
                """,
                (
                    bundle_artifact_ref,
                    Jsonb(published_result),
                    job_id,
                ),
            )

            active_conn.execute(
                """
                UPDATE publish.backend_variants
                SET status = 'PUBLISHED',
                    published_ref = %s,
                    published_metadata = %s,
                    last_error = NULL,
                    updated_at = now()
                WHERE id = %s
                """,
                (
                    bundle_artifact_ref,
                    Jsonb(published_result),
                    variant_id,
                ),
            )

        if conn is not None:
            with conn.transaction():
                commit(conn)
        else:
            with self.pool.connection() as pooled_conn:
                with pooled_conn.transaction():
                    commit(pooled_conn)

        return bundle_id
