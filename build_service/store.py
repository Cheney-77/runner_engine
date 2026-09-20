from __future__ import annotations

import uuid

from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool

from .model import RuntimeBuildSpec


BUILD_SCHEMA_STATEMENTS = (
    "CREATE SCHEMA IF NOT EXISTS build",
    """
    CREATE TABLE IF NOT EXISTS build.runtime_environments (
        id UUID PRIMARY KEY,
        env_key VARCHAR(64) UNIQUE NOT NULL,
        base_image TEXT NOT NULL,
        python_version TEXT NOT NULL,
        platform TEXT NOT NULL,
        build_policy_version TEXT NOT NULL,
        lock_sha256 VARCHAR(64) NOT NULL,
        requirements_lock TEXT NOT NULL,
        packages JSONB NOT NULL CHECK (jsonb_typeof(packages) = 'object'),
        package_count INTEGER NOT NULL CHECK (package_count >= 0),
        image_ref TEXT UNIQUE,
        status TEXT NOT NULL CHECK (status IN ('PENDING', 'BUILDING', 'VERIFYING', 'READY', 'FAILED')),
        last_error TEXT,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS ix_build_runtime_packages
        ON build.runtime_environments USING GIN (packages jsonb_path_ops)
    """,
    """
    CREATE INDEX IF NOT EXISTS ix_build_runtime_compat
        ON build.runtime_environments(base_image, python_version, platform, build_policy_version, status)
    """,
    """
    CREATE TABLE IF NOT EXISTS build.runtime_env_aliases (
        request_env_key VARCHAR(64) PRIMARY KEY,
        env_id UUID NOT NULL REFERENCES build.runtime_environments(id),
        resolution_kind TEXT NOT NULL CHECK (resolution_kind IN ('exact', 'superset')),
        created_at TIMESTAMPTZ NOT NULL DEFAULT now()
    )
    """,
    "CREATE INDEX IF NOT EXISTS ix_build_runtime_alias_env ON build.runtime_env_aliases(env_id)",
    """
    CREATE TABLE IF NOT EXISTS build.build_jobs (
        id UUID PRIMARY KEY,
        env_id UUID NOT NULL REFERENCES build.runtime_environments(id),
        status TEXT NOT NULL CHECK (status IN ('BUILDING', 'VERIFYING', 'READY', 'FAILED')),
        error_message TEXT,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        started_at TIMESTAMPTZ,
        finished_at TIMESTAMPTZ
    )
    """,
    "CREATE INDEX IF NOT EXISTS ix_build_jobs_env ON build.build_jobs(env_id, created_at DESC)",
)


class BuildStore:
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
            for statement in BUILD_SCHEMA_STATEMENTS:
                conn.execute(statement)

            # Migration for build schemas created by earlier v3.3 code.
            # package_count is stored explicitly because PostgreSQL does not provide
            # jsonb_object_length(). Count the top-level package keys once during migration.
            conn.execute(
                """
                ALTER TABLE build.runtime_environments
                ADD COLUMN IF NOT EXISTS package_count INTEGER
                """
            )
            conn.execute(
                """
                UPDATE build.runtime_environments
                SET package_count = (
                    SELECT count(*)::integer
                    FROM jsonb_object_keys(packages)
                )
                WHERE package_count IS NULL
                """
            )
            conn.execute(
                """
                ALTER TABLE build.runtime_environments
                ALTER COLUMN package_count SET NOT NULL
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS ix_build_runtime_superset_order
                ON build.runtime_environments(
                    base_image,
                    python_version,
                    platform,
                    build_policy_version,
                    status,
                    package_count,
                    created_at
                )
                """
            )

    def close(self) -> None:
        self.pool.close()

    def get(self, env_id: str):
        with self.pool.connection() as conn:
            return conn.execute(
                "SELECT * FROM build.runtime_environments WHERE id = %s",
                (env_id,),
            ).fetchone()

    def get_for_request(self, request_env_key: str):
        with self.pool.connection() as conn:
            return conn.execute(
                """
                SELECT env.*, alias.request_env_key, alias.resolution_kind
                FROM build.runtime_env_aliases alias
                JOIN build.runtime_environments env ON env.id = alias.env_id
                WHERE alias.request_env_key = %s
                """,
                (request_env_key,),
            ).fetchone()

    def find_smallest_superset(self, spec: RuntimeBuildSpec):
        with self.pool.connection() as conn:
            return conn.execute(
                """
                SELECT *
                FROM build.runtime_environments
                WHERE status = 'READY'
                  AND base_image = %s
                  AND python_version = %s
                  AND platform = %s
                  AND build_policy_version = %s
                  AND packages @> %s::jsonb
                ORDER BY package_count, created_at
                LIMIT 1
                """,
                (
                    spec.base_image,
                    spec.python_version,
                    spec.platform,
                    spec.build_policy_version,
                    Jsonb(spec.packages),
                ),
            ).fetchone()

    def get_or_create_exact(self, spec: RuntimeBuildSpec):
        env_id = str(uuid.uuid4())

        with self.pool.connection() as conn:
            row = conn.execute(
                """
                INSERT INTO build.runtime_environments(
                    id,
                    env_key,
                    base_image,
                    python_version,
                    platform,
                    build_policy_version,
                    lock_sha256,
                    requirements_lock,
                    packages,
                    package_count,
                    status
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'PENDING')
                ON CONFLICT (env_key) DO NOTHING
                RETURNING *
                """,
                (
                    env_id,
                    spec.env_key,
                    spec.base_image,
                    spec.python_version,
                    spec.platform,
                    spec.build_policy_version,
                    spec.lock_sha256,
                    spec.requirements_lock,
                    Jsonb(spec.packages),
                    len(spec.packages),
                ),
            ).fetchone()

            if row is not None:
                return row

            return conn.execute(
                "SELECT * FROM build.runtime_environments WHERE env_key = %s",
                (spec.env_key,),
            ).fetchone()

    def bind_request(self, request_env_key: str, env_id: str, resolution_kind: str) -> None:
        with self.pool.connection() as conn:
            conn.execute(
                """
                INSERT INTO build.runtime_env_aliases(request_env_key, env_id, resolution_kind)
                VALUES (%s, %s, %s)
                ON CONFLICT (request_env_key) DO NOTHING
                """,
                (request_env_key, env_id, resolution_kind),
            )

    def reset_failed(self, env_id: str):
        """Move a FAILED environment back to PENDING for an explicit retry.

        Previous build_jobs rows are intentionally preserved as history. claim_build()
        will create a fresh job row for the new attempt.
        """
        with self.pool.connection() as conn:
            return conn.execute(
                """
                UPDATE build.runtime_environments
                SET status = 'PENDING',
                    image_ref = NULL,
                    last_error = NULL,
                    updated_at = now()
                WHERE id = %s AND status = 'FAILED'
                RETURNING *
                """,
                (env_id,),
            ).fetchone()

    def claim_build(self, env_id: str) -> str | None:
        job_id = str(uuid.uuid4())

        with self.pool.connection() as conn:
            row = conn.execute(
                """
                UPDATE build.runtime_environments
                SET status = 'BUILDING', last_error = NULL, updated_at = now()
                WHERE id = %s AND status = 'PENDING'
                RETURNING id
                """,
                (env_id,),
            ).fetchone()

            if row is None:
                return None

            conn.execute(
                """
                INSERT INTO build.build_jobs(id, env_id, status, started_at)
                VALUES (%s, %s, 'BUILDING', now())
                """,
                (job_id, env_id),
            )

        return job_id

    def mark_verifying(self, env_id: str, job_id: str) -> None:
        with self.pool.connection() as conn:
            conn.execute(
                """
                UPDATE build.runtime_environments
                SET status = 'VERIFYING', updated_at = now()
                WHERE id = %s
                """,
                (env_id,),
            )
            conn.execute(
                """
                UPDATE build.build_jobs
                SET status = 'VERIFYING'
                WHERE id = %s
                """,
                (job_id,),
            )

    def mark_ready(self, env_id: str, job_id: str, image_ref: str) -> None:
        with self.pool.connection() as conn:
            conn.execute(
                """
                UPDATE build.runtime_environments
                SET status = 'READY', image_ref = %s, last_error = NULL, updated_at = now()
                WHERE id = %s
                """,
                (image_ref, env_id),
            )
            conn.execute(
                """
                UPDATE build.build_jobs
                SET status = 'READY', finished_at = now()
                WHERE id = %s
                """,
                (job_id,),
            )

    def mark_failed(self, env_id: str, job_id: str, error_message: str) -> None:
        message = error_message[-8192:]

        with self.pool.connection() as conn:
            conn.execute(
                """
                UPDATE build.runtime_environments
                SET status = 'FAILED', last_error = %s, updated_at = now()
                WHERE id = %s
                """,
                (message, env_id),
            )
            conn.execute(
                """
                UPDATE build.build_jobs
                SET status = 'FAILED', error_message = %s, finished_at = now()
                WHERE id = %s
                """,
                (message, job_id),
            )
