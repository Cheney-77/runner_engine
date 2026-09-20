from __future__ import annotations

import json
from uuid import uuid4

from .store import uuid_text


class CleanupExecutionStore:
    def __init__(self, admin_store):
        self.store = admin_store

    def get_cleanup_request(self, request_id: str):
        request_id = uuid_text(request_id)
        with self.store.connection() as conn:
            row = conn.execute(
                """
                SELECT id::text, environment_id::text, env_key, status,
                       reason, preflight_snapshot, created_by, created_at
                FROM console_ops.cleanup_requests
                WHERE id = %s::uuid
                """,
                (request_id,),
            ).fetchone()

        if row is None:
            return None

        snapshot = row[5] if isinstance(row[5], dict) else {}
        environment = snapshot.get("environment") or {}
        return {
            "id": row[0],
            "environmentId": row[1],
            "envKey": row[2],
            "status": row[3],
            "reason": row[4],
            "imageRef": environment.get("imageRef"),
            "createdBy": row[6],
            "createdAt": row[7].isoformat() if hasattr(row[7], "isoformat") else row[7],
        }

    def start_cleanup_execution(self, actor, request_id, environment_id) -> str:
        request_id = uuid_text(request_id)
        environment_id = uuid_text(environment_id)
        execution_id = str(uuid4())

        with self.store.connection() as conn:
            conn.execute(
                """
                INSERT INTO console_ops.cleanup_executions(
                    id, request_id, environment_id, status, started_by
                )
                VALUES (%s::uuid, %s::uuid, %s::uuid, 'RUNNING', %s)
                """,
                (execution_id, request_id, environment_id, actor),
            )
            self.store.audit(
                conn,
                actor,
                "cleanup.execute.start",
                "build_environment",
                environment_id,
            )

        return execution_id

    def record_cleanup_step(self, execution_id, step_name, status, detail) -> None:
        execution_id = uuid_text(execution_id)
        allowed = {"RUNNING", "SUCCEEDED", "BLOCKED", "WAITING", "FAILED"}
        if status not in allowed:
            raise ValueError("invalid cleanup step status")

        with self.store.connection() as conn:
            conn.execute(
                """
                INSERT INTO console_ops.cleanup_execution_steps(
                    id, execution_id, step_name, status, detail_json
                )
                VALUES (%s::uuid, %s::uuid, %s, %s, %s::jsonb)
                """,
                (
                    str(uuid4()),
                    execution_id,
                    step_name[:120],
                    status,
                    json.dumps(detail or {}, ensure_ascii=False, default=str),
                ),
            )

    def finish_cleanup_execution(self, actor, execution_id, status, message) -> None:
        execution_id = uuid_text(execution_id)
        allowed = {"BLOCKED", "WAITING", "SUCCEEDED", "FAILED", "PARTIAL"}
        if status not in allowed:
            raise ValueError("invalid cleanup execution status")

        with self.store.connection() as conn:
            row = conn.execute(
                """
                UPDATE console_ops.cleanup_executions
                SET status = %s,
                    message = %s,
                    finished_at = CASE
                        WHEN %s IN ('BLOCKED', 'SUCCEEDED', 'FAILED', 'PARTIAL')
                            THEN now()
                        ELSE finished_at
                    END,
                    updated_at = now()
                WHERE id = %s::uuid
                RETURNING environment_id::text
                """,
                (status, str(message)[:2000], status, execution_id),
            ).fetchone()

            if row is None:
                raise ValueError("cleanup execution not found")

            self.store.audit(
                conn,
                actor,
                f"cleanup.execute.{status.lower()}",
                "build_environment",
                row[0],
            )

    def list_recent(self, limit: int = 100) -> list[dict]:
        limit = max(1, min(int(limit), 300))
        with self.store.connection() as conn:
            rows = conn.execute(
                """
                SELECT id::text, request_id::text, environment_id::text,
                       status, message, started_by, started_at, finished_at, updated_at
                FROM console_ops.cleanup_executions
                ORDER BY started_at DESC
                LIMIT %s
                """,
                (limit,),
            ).fetchall()

        return [
            {
                "id": row[0],
                "requestId": row[1],
                "environmentId": row[2],
                "status": row[3],
                "message": row[4],
                "startedBy": row[5],
                "startedAt": row[6].isoformat() if row[6] else None,
                "finishedAt": row[7].isoformat() if row[7] else None,
                "updatedAt": row[8].isoformat() if row[8] else None,
            }
            for row in rows
        ]
