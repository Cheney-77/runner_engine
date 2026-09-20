from __future__ import annotations

from contextlib import contextmanager
from typing import Any, Iterator


class PublishRuntimeLifecycle:
    """
    Durable gate for Runner runtime environments.

    Runner publication and runtime retirement share one PostgreSQL advisory
    lock, closing the publish-vs-delete race.
    """

    def __init__(self, store):
        self.store = store
        self._init_schema()

    def _init_schema(self) -> None:
        statements = (
            """
            CREATE TABLE IF NOT EXISTS publish.runner_runtime_lifecycle (
                runtime_env_key TEXT PRIMARY KEY,
                image_ref TEXT NOT NULL,
                state TEXT NOT NULL CHECK (state IN ('RETIRING', 'RETIRED')),
                reason TEXT NOT NULL DEFAULT '',
                created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                retired_at TIMESTAMPTZ
            )
            """,
            """
            CREATE INDEX IF NOT EXISTS ix_publish_runner_runtime_lifecycle_state
            ON publish.runner_runtime_lifecycle(state, updated_at DESC)
            """,
        )

        with self.store.pool.connection() as conn:
            for statement in statements:
                conn.execute(statement)

    @staticmethod
    def _lock_key(runtime_env_key: str) -> str:
        return f"publish-runner-runtime:{runtime_env_key}"

    @contextmanager
    def guard(self, runtime_env_key: str) -> Iterator[None]:
        if not runtime_env_key:
            raise ValueError("runtime_env_key is required")

        key = self._lock_key(runtime_env_key)
        with self.store.pool.connection() as conn:
            previous_autocommit = conn.autocommit
            conn.autocommit = True
            conn.execute(
                "SELECT pg_advisory_lock(hashtextextended(%s, 0))",
                (key,),
            )
            try:
                yield
            finally:
                conn.execute(
                    "SELECT pg_advisory_unlock(hashtextextended(%s, 0))",
                    (key,),
                )
                conn.autocommit = previous_autocommit

    def get(self, runtime_env_key: str):
        with self.store.pool.connection() as conn:
            return conn.execute(
                """
                SELECT *
                FROM publish.runner_runtime_lifecycle
                WHERE runtime_env_key = %s
                """,
                (runtime_env_key,),
            ).fetchone()

    def assert_active(self, runtime_env_key: str, image_ref: str) -> None:
        row = self.get(runtime_env_key)
        if row is None:
            return

        if row["image_ref"] != image_ref:
            raise RuntimeError(
                "runtime environment is lifecycle-blocked with another image_ref"
            )

        raise RuntimeError(
            f"runtime environment {runtime_env_key} is {row['state']} and cannot "
            "be used by a new Runner publication"
        )

    def current_runner_references(
        self,
        *,
        runtime_env_key: str,
        image_ref: str,
    ) -> list[dict[str, Any]]:
        with self.store.pool.connection() as conn:
            rows = conn.execute(
                """
                WITH latest_contracts AS (
                    SELECT operator_id, MAX(version) AS version
                    FROM publish.virtual_contract_versions
                    GROUP BY operator_id
                )
                SELECT
                    o.id AS operator_id,
                    o.name,
                    o.display_name,
                    c.id AS contract_id,
                    v.id AS variant_id,
                    v.published_ref,
                    v.published_metadata
                FROM publish.backend_variants v
                JOIN publish.virtual_contract_versions c
                  ON c.id = v.contract_id
                JOIN latest_contracts latest
                  ON latest.operator_id = c.operator_id
                 AND latest.version = c.version
                JOIN publish.operators o
                  ON o.id = c.operator_id
                WHERE v.backend = 'runner'
                  AND v.status = 'PUBLISHED'
                  AND v.published_metadata IS NOT NULL
                  AND (
                    v.published_metadata->>'runtimeEnvKey' = %s
                    OR v.published_metadata->>'envKey' = %s
                    OR v.published_metadata->>'runtimeImage' = %s
                  )
                ORDER BY o.display_name, v.updated_at DESC
                """,
                (runtime_env_key, runtime_env_key, image_ref),
            ).fetchall()

        return [
            {
                "operatorId": str(row["operator_id"]),
                "name": row["name"],
                "displayName": row["display_name"],
                "contractId": str(row["contract_id"]),
                "variantId": str(row["variant_id"]),
                "publishedRef": row["published_ref"],
                "publishedMetadata": row["published_metadata"],
            }
            for row in rows
        ]

    def begin_retirement(
        self,
        *,
        runtime_env_key: str,
        image_ref: str,
        reason: str,
    ) -> dict[str, Any]:
        if not runtime_env_key or not image_ref:
            raise ValueError("runtime_env_key and image_ref are required")

        with self.guard(runtime_env_key):
            with self.store.pool.connection() as conn:
                row = conn.execute(
                    """
                    INSERT INTO publish.runner_runtime_lifecycle(
                        runtime_env_key,
                        image_ref,
                        state,
                        reason
                    )
                    VALUES (%s, %s, 'RETIRING', %s)
                    ON CONFLICT (runtime_env_key) DO UPDATE
                    SET image_ref = EXCLUDED.image_ref,
                        reason = EXCLUDED.reason,
                        state = CASE
                            WHEN publish.runner_runtime_lifecycle.state = 'RETIRED'
                                THEN 'RETIRED'
                            ELSE 'RETIRING'
                        END,
                        updated_at = now()
                    RETURNING *
                    """,
                    (runtime_env_key, image_ref, reason[:1000]),
                ).fetchone()

            references = self.current_runner_references(
                runtime_env_key=runtime_env_key,
                image_ref=image_ref,
            )

            return {
                "runtimeEnvKey": runtime_env_key,
                "imageRef": image_ref,
                "lifecycleState": row["state"],
                "publishedReferenceCount": len(references),
                "publishedReferences": references,
                "safeForRetirement": not references and row["state"] == "RETIRING",
            }

    def cancel_retirement(self, *, runtime_env_key: str) -> dict[str, Any]:
        with self.guard(runtime_env_key):
            with self.store.pool.connection() as conn:
                row = conn.execute(
                    """
                    DELETE FROM publish.runner_runtime_lifecycle
                    WHERE runtime_env_key = %s
                      AND state = 'RETIRING'
                    RETURNING *
                    """,
                    (runtime_env_key,),
                ).fetchone()

            if row is None:
                current = self.get(runtime_env_key)
                if current is None:
                    return {
                        "cancelled": True,
                        "alreadyActive": True,
                        "runtimeEnvKey": runtime_env_key,
                    }
                raise ValueError(
                    f"runtime environment lifecycle is {current['state']}; "
                    "it cannot be reactivated"
                )

            return {
                "cancelled": True,
                "runtimeEnvKey": runtime_env_key,
                "imageRef": row["image_ref"],
            }

    def finalize_retirement(self, *, runtime_env_key: str) -> dict[str, Any]:
        with self.guard(runtime_env_key):
            with self.store.pool.connection() as conn:
                row = conn.execute(
                    """
                    UPDATE publish.runner_runtime_lifecycle
                    SET state = 'RETIRED',
                        retired_at = COALESCE(retired_at, now()),
                        updated_at = now()
                    WHERE runtime_env_key = %s
                    RETURNING *
                    """,
                    (runtime_env_key,),
                ).fetchone()

            if row is None:
                raise KeyError(runtime_env_key)

            return {
                "runtimeEnvKey": runtime_env_key,
                "imageRef": row["image_ref"],
                "lifecycleState": row["state"],
            }
