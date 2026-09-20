from __future__ import annotations

import uuid
from typing import Any

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

    def save_virtual_contract(self, workspace: str, contract: VirtualOperatorContract):
        digest = contract_sha256(contract)
        payload = contract.model_dump(mode="json")
        operator_id = str(uuid.uuid4())
        contract_id = str(uuid.uuid4())

        with self.pool.connection() as conn:
            conn.execute(
                """
                INSERT INTO publish.operators(id, workspace, name, display_name, description)
                VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT (workspace, name) DO UPDATE
                SET display_name = EXCLUDED.display_name,
                    description = EXCLUDED.description,
                    updated_at = now()
                """,
                (
                    operator_id,
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
                WHERE workspace = %s AND name = %s
                FOR UPDATE
                """,
                (workspace, contract.metadata.name),
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

    def get_operator(self, operator_id: str):
        with self.pool.connection() as conn:
            return conn.execute(
                "SELECT * FROM publish.operators WHERE id = %s",
                (operator_id,),
            ).fetchone()

    def get_latest_contract(self, operator_id: str):
        with self.pool.connection() as conn:
            return conn.execute(
                """
                SELECT *
                FROM publish.virtual_contract_versions
                WHERE operator_id = %s
                ORDER BY version DESC
                LIMIT 1
                """,
                (operator_id,),
            ).fetchone()

    def get_contract(self, contract_id: str):
        with self.pool.connection() as conn:
            return conn.execute(
                """
                SELECT c.*, o.workspace, o.name, o.display_name, o.description
                FROM publish.virtual_contract_versions c
                JOIN publish.operators o ON o.id = c.operator_id
                WHERE c.id = %s
                """,
                (contract_id,),
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
                SET status = CASE WHEN status = 'PUBLISHED' THEN 'PUBLISHED' ELSE 'FAILED' END,
                    last_error = %s, updated_at = now()
                WHERE id = %s
                """,
                (message, variant_id),
            )
