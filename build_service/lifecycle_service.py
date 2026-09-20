from __future__ import annotations

from psycopg.types.json import Jsonb

from .harbor import HarborClient
from .lifecycle import BuildLifecycle
from .service import BuildService, BuildSettings
from .store import BuildStore


class LifecycleBuildStore(BuildStore):
    """BuildStore with an orthogonal lifecycle state."""

    def _init_schema(self) -> None:
        super()._init_schema()

        statements = (
            """
            ALTER TABLE build.runtime_environments
            ADD COLUMN IF NOT EXISTS lifecycle_state TEXT NOT NULL DEFAULT 'ACTIVE'
            """,
            """
            ALTER TABLE build.runtime_environments
            ADD COLUMN IF NOT EXISTS last_used_at TIMESTAMPTZ
            """,
            """
            ALTER TABLE build.runtime_environments
            ADD COLUMN IF NOT EXISTS retired_at TIMESTAMPTZ
            """,
            """
            ALTER TABLE build.runtime_environments
            ADD COLUMN IF NOT EXISTS artifact_deleted_at TIMESTAMPTZ
            """,
            """
            ALTER TABLE build.runtime_environments
            ADD COLUMN IF NOT EXISTS cleanup_error TEXT
            """,
            """
            CREATE INDEX IF NOT EXISTS ix_build_runtime_lifecycle
            ON build.runtime_environments(
                lifecycle_state,
                status,
                updated_at DESC
            )
            """,
        )

        with self.pool.connection() as conn:
            for statement in statements:
                conn.execute(statement)

    def get_for_request(self, request_env_key: str):
        with self.pool.connection() as conn:
            return conn.execute(
                """
                SELECT env.*, alias.request_env_key, alias.resolution_kind
                FROM build.runtime_env_aliases alias
                JOIN build.runtime_environments env ON env.id = alias.env_id
                WHERE alias.request_env_key = %s
                  AND env.lifecycle_state = 'ACTIVE'
                """,
                (request_env_key,),
            ).fetchone()

    def find_smallest_superset(self, spec):
        with self.pool.connection() as conn:
            return conn.execute(
                """
                SELECT *
                FROM build.runtime_environments
                WHERE status = 'READY'
                  AND lifecycle_state = 'ACTIVE'
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

    def get_or_create_exact(self, spec):
        row = super().get_or_create_exact(spec)
        if row is None:
            raise RuntimeError("BuildStore failed to create/load exact environment")

        lifecycle = row.get("lifecycle_state", "ACTIVE")
        if lifecycle == "RETIRING":
            raise ValueError(
                "exact runtime environment is being retired; retry after cleanup"
            )

        if lifecycle == "RETIRED":
            reactivated = self.reactivate_retired(row["id"])
            if reactivated is None:
                raise ValueError("retired runtime environment could not reactivate")
            return reactivated

        return row

    def claim_build(self, env_id: str) -> str | None:
        import uuid

        job_id = str(uuid.uuid4())
        with self.pool.connection() as conn:
            row = conn.execute(
                """
                UPDATE build.runtime_environments
                SET status = 'BUILDING',
                    last_error = NULL,
                    updated_at = now()
                WHERE id = %s
                  AND status = 'PENDING'
                  AND lifecycle_state = 'ACTIVE'
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

    def bind_request(self, request_env_key: str, env_id: str, resolution_kind: str) -> None:
        with self.pool.connection() as conn:
            row = conn.execute(
                """
                SELECT lifecycle_state
                FROM build.runtime_environments
                WHERE id = %s
                """,
                (env_id,),
            ).fetchone()

            if row is None:
                raise KeyError(env_id)
            if row["lifecycle_state"] != "ACTIVE":
                raise ValueError(
                    f"runtime environment lifecycle is {row['lifecycle_state']}"
                )

            conn.execute(
                """
                INSERT INTO build.runtime_env_aliases(
                    request_env_key,
                    env_id,
                    resolution_kind
                )
                VALUES (%s, %s, %s)
                ON CONFLICT (request_env_key) DO NOTHING
                """,
                (request_env_key, env_id, resolution_kind),
            )

    def reactivate_retired(self, env_id: str):
        with self.pool.connection() as conn:
            return conn.execute(
                """
                UPDATE build.runtime_environments
                SET lifecycle_state = 'ACTIVE',
                    status = 'PENDING',
                    image_ref = NULL,
                    retired_at = NULL,
                    artifact_deleted_at = NULL,
                    cleanup_error = NULL,
                    updated_at = now()
                WHERE id = %s
                  AND lifecycle_state = 'RETIRED'
                RETURNING *
                """,
                (env_id,),
            ).fetchone()

    def count_aliases(self, env_id: str) -> int:
        with self.pool.connection() as conn:
            row = conn.execute(
                """
                SELECT COUNT(*) AS count
                FROM build.runtime_env_aliases
                WHERE env_id = %s
                """,
                (env_id,),
            ).fetchone()
        return int(row["count"])

    def count_active_build_jobs(self, env_id: str) -> int:
        with self.pool.connection() as conn:
            row = conn.execute(
                """
                SELECT COUNT(*) AS count
                FROM build.build_jobs
                WHERE env_id = %s
                  AND status IN ('BUILDING', 'VERIFYING')
                """,
                (env_id,),
            ).fetchone()
        return int(row["count"])

    def begin_retirement(self, env_id: str, *, reason: str):
        del reason
        with self.pool.connection() as conn:
            return conn.execute(
                """
                UPDATE build.runtime_environments env
                SET lifecycle_state = 'RETIRING',
                    cleanup_error = NULL,
                    updated_at = now()
                WHERE env.id = %s
                  AND env.lifecycle_state = 'ACTIVE'
                  AND env.status NOT IN ('PENDING', 'BUILDING', 'VERIFYING')
                  AND NOT EXISTS (
                      SELECT 1
                      FROM build.build_jobs jobs
                      WHERE jobs.env_id = env.id
                        AND jobs.status IN ('BUILDING', 'VERIFYING')
                  )
                RETURNING *
                """,
                (env_id,),
            ).fetchone()

    def cancel_retirement(self, env_id: str):
        with self.pool.connection() as conn:
            return conn.execute(
                """
                UPDATE build.runtime_environments
                SET lifecycle_state = 'ACTIVE',
                    cleanup_error = NULL,
                    updated_at = now()
                WHERE id = %s
                  AND lifecycle_state = 'RETIRING'
                RETURNING *
                """,
                (env_id,),
            ).fetchone()

    def finalize_retirement(self, env_id: str):
        with self.pool.connection() as conn:
            conn.execute(
                """
                DELETE FROM build.runtime_env_aliases
                WHERE env_id = %s
                """,
                (env_id,),
            )

            return conn.execute(
                """
                UPDATE build.runtime_environments
                SET lifecycle_state = 'RETIRED',
                    retired_at = COALESCE(retired_at, now()),
                    artifact_deleted_at = COALESCE(artifact_deleted_at, now()),
                    cleanup_error = NULL,
                    updated_at = now()
                WHERE id = %s
                  AND lifecycle_state = 'RETIRING'
                RETURNING *
                """,
                (env_id,),
            ).fetchone()

    def record_cleanup_error(self, env_id: str, message: str) -> None:
        with self.pool.connection() as conn:
            conn.execute(
                """
                UPDATE build.runtime_environments
                SET cleanup_error = %s,
                    updated_at = now()
                WHERE id = %s
                  AND lifecycle_state = 'RETIRING'
                """,
                (message[-8192:], env_id),
            )


class LifecycleBuildService(BuildService):
    def __init__(
        self,
        store: LifecycleBuildStore,
        settings: BuildSettings,
        *,
        harbor: HarborClient | None = None,
    ):
        super().__init__(store, settings)
        self.lifecycle = BuildLifecycle(store, harbor)

    def cleanup_plan(self, environment_id: str) -> dict:
        return self.lifecycle.plan(environment_id).as_dict()

    def retire_environment(self, environment_id: str, *, reason: str) -> dict:
        return self.lifecycle.begin_retirement(environment_id, reason=reason)

    def cancel_environment_retirement(self, environment_id: str) -> dict:
        return self.lifecycle.cancel_retirement(environment_id)

    def delete_environment_artifact(
        self,
        environment_id: str,
        *,
        expected_image_ref: str,
    ) -> dict:
        return self.lifecycle.delete_artifact(
            environment_id,
            expected_image_ref=expected_image_ref,
        )
