from __future__ import annotations

import hashlib
from typing import Any

from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool


SCHEMA = (
    "CREATE SCHEMA IF NOT EXISTS runner",
    """
    CREATE TABLE IF NOT EXISTS runner.runtime_image_lifecycle (
        runtime_id VARCHAR(64) PRIMARY KEY,
        image_ref TEXT UNIQUE NOT NULL,
        state TEXT NOT NULL CHECK (state IN ('RETIRING', 'RETIRED')),
        reason TEXT NOT NULL DEFAULT '',
        cleanup_error TEXT,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        retired_at TIMESTAMPTZ
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS ix_runner_runtime_image_lifecycle_state
    ON runner.runtime_image_lifecycle(state, updated_at DESC)
    """,
)


def runtime_id_for_image(image_ref: str) -> str:
    return hashlib.sha256(image_ref.encode("utf-8")).hexdigest()


class RuntimeLifecycleStore:
    def __init__(self, dsn: str):
        if not dsn:
            raise ValueError("Runner lifecycle store requires RUNNER_DB_URL")

        self.pool = ConnectionPool(
            conninfo=dsn,
            min_size=1,
            max_size=4,
            kwargs={"row_factory": dict_row},
            open=True,
        )
        self._init_schema()

    def _init_schema(self) -> None:
        with self.pool.connection() as conn:
            for statement in SCHEMA:
                conn.execute(statement)

    def close(self) -> None:
        self.pool.close()

    def get_by_runtime_id(self, runtime_id: str):
        with self.pool.connection() as conn:
            return conn.execute(
                """
                SELECT *
                FROM runner.runtime_image_lifecycle
                WHERE runtime_id = %s
                """,
                (runtime_id,),
            ).fetchone()

    def get_by_image(self, image_ref: str):
        with self.pool.connection() as conn:
            return conn.execute(
                """
                SELECT *
                FROM runner.runtime_image_lifecycle
                WHERE image_ref = %s
                """,
                (image_ref,),
            ).fetchone()

    def is_blocked_runtime(self, runtime_id: str) -> bool:
        return self.get_by_runtime_id(runtime_id) is not None

    def begin_retirement(self, image_ref: str, *, reason: str):
        runtime_id = runtime_id_for_image(image_ref)

        with self.pool.connection() as conn:
            return conn.execute(
                """
                INSERT INTO runner.runtime_image_lifecycle(
                    runtime_id,
                    image_ref,
                    state,
                    reason
                )
                VALUES (%s, %s, 'RETIRING', %s)
                ON CONFLICT (runtime_id) DO UPDATE
                SET image_ref = EXCLUDED.image_ref,
                    reason = EXCLUDED.reason,
                    state = CASE
                        WHEN runner.runtime_image_lifecycle.state = 'RETIRED'
                            THEN 'RETIRED'
                        ELSE 'RETIRING'
                    END,
                    cleanup_error = NULL,
                    updated_at = now()
                RETURNING *
                """,
                (runtime_id, image_ref, reason[:1000]),
            ).fetchone()

    def cancel_retirement(self, image_ref: str) -> bool:
        with self.pool.connection() as conn:
            row = conn.execute(
                """
                DELETE FROM runner.runtime_image_lifecycle
                WHERE image_ref = %s
                  AND state = 'RETIRING'
                RETURNING runtime_id
                """,
                (image_ref,),
            ).fetchone()
        return row is not None

    def finalize_retirement(self, image_ref: str):
        with self.pool.connection() as conn:
            return conn.execute(
                """
                UPDATE runner.runtime_image_lifecycle
                SET state = 'RETIRED',
                    retired_at = COALESCE(retired_at, now()),
                    cleanup_error = NULL,
                    updated_at = now()
                WHERE image_ref = %s
                RETURNING *
                """,
                (image_ref,),
            ).fetchone()

    def list_recent(self, limit: int = 100) -> list[dict[str, Any]]:
        limit = max(1, min(int(limit), 500))
        with self.pool.connection() as conn:
            rows = conn.execute(
                """
                SELECT *
                FROM runner.runtime_image_lifecycle
                ORDER BY updated_at DESC
                LIMIT %s
                """,
                (limit,),
            ).fetchall()
        return [dict(row) for row in rows]
