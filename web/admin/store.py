"""CRUD for console-owned notes & cleanup requests, with append-only audit.

This class NEVER mutates publish.*, build.* or runner.* and cannot delete
physical images. ADMIN_DB_URL needs privileges only in console_ops.
"""
from __future__ import annotations

import json
import logging
from contextlib import contextmanager
from datetime import datetime
from uuid import UUID, uuid4

log = logging.getLogger(__name__)
KINDS = frozenset(("build_environment", "runner_lease", "runner_run", "publish_variant"))


class Conflict(Exception):
    pass


class StorageUnavailable(Exception):
    pass


def uuid_text(value):
    try:
        return str(UUID(value))
    except (TypeError, ValueError, AttributeError) as exc:
        raise ValueError("Invalid UUID") from exc


def simple(value):
    return value.isoformat() if isinstance(value, datetime) else value


class AdminStore:
    def __init__(self, dsn: str, connect=None):
        self.dsn = dsn
        self.connect_factory = connect

    @contextmanager
    def connection(self):
        if not self.dsn and not self.connect_factory:
            raise StorageUnavailable("Set ADMIN_DB_URL and apply migration 001_admin_schema.sql")
        try:
            if self.connect_factory:
                connection = self.connect_factory()
            else:
                import psycopg
                connection = psycopg.connect(self.dsn, connect_timeout=3)
        except Exception as exc:
            log.warning("Console metadata connection failed (%s)", type(exc).__name__)
            raise StorageUnavailable(
                "Admin metadata database unavailable; check DSN and permissions."
            ) from exc
        # Exceptions from the calling CRUD operation must propagate unchanged:
        # psycopg's context manager will roll back a failing transaction.
        with connection as conn:
            yield conn

    @staticmethod
    def audit(conn, actor, action, kind, resource_id):
        conn.execute(
            """INSERT INTO console_ops.audit_events
               (id, actor, action, resource_type, resource_id)
               VALUES (%s::uuid, %s, %s, %s, %s)""",
            (str(uuid4()), actor, action, kind, str(resource_id)),
        )

    def record_action(self, actor, action, kind, resource_id):
        """Append audit intent before any external Build mutation."""
        if kind != "build_environment":
            raise ValueError("Unsupported audited asset type")
        if not isinstance(resource_id,str) or not 1<=len(resource_id)<=160:
            raise ValueError("Invalid resource ID")
        with self.connection() as conn:
            self.audit(conn,actor,action,kind,resource_id)
        return {"recorded":True}

    def overview(self):
        if not self.dsn and not self.connect_factory:
            return {
                "status": "unconfigured",
                "message": "Configure ADMIN_DB_URL and apply console_ops migration.",
                "notes": [], "requests": [], "audit": [],
            }
        with self.connection() as conn:
            notes = conn.execute(
                """SELECT id::text, asset_type, asset_id, note, version,
                          created_by, updated_by, updated_at
                   FROM console_ops.asset_notes ORDER BY updated_at DESC LIMIT 100"""
            ).fetchall()
            req = conn.execute(
                """SELECT id::text, environment_id::text, env_key, status,
                          reason, created_by, created_at
                   FROM console_ops.cleanup_requests
                   ORDER BY created_at DESC LIMIT 100"""
            ).fetchall()
            audits = conn.execute(
                """SELECT id::text, actor, action, resource_type, resource_id, occurred_at
                   FROM console_ops.audit_events ORDER BY occurred_at DESC LIMIT 80"""
            ).fetchall()
        return {
            "status": "ok", "message": "",
            "notes": [
                dict(zip(("id","assetType","assetId","note","version",
                          "createdBy","updatedBy","updatedAt"),
                         (simple(v) for v in row)))
                for row in notes
            ],
            "requests": [
                dict(zip(("id","environmentId","envKey","status","reason",
                          "createdBy","createdAt"), (simple(v) for v in row)))
                for row in req
            ],
            "audit": [
                dict(zip(("id","actor","action","assetType","assetId","createdAt"),
                         (simple(v) for v in row)))
                for row in audits
            ],
        }

    @staticmethod
    def validate_note(kind, asset_id, note):
        if kind not in KINDS:
            raise ValueError("Unsupported asset type")
        if not isinstance(asset_id, str) or not 1 <= len(asset_id) <= 160:
            raise ValueError("Asset ID must be 1..160 characters")
        if not isinstance(note, str) or not 1 <= len(note.strip()) <= 1000:
            raise ValueError("Note must be 1..1000 characters")
        return kind, asset_id, note.strip()

    def create_note(self, actor, kind, asset_id, note):
        kind, asset_id, note = self.validate_note(kind, asset_id, note)
        uid = str(uuid4())
        with self.connection() as conn:
            row = conn.execute(
                """INSERT INTO console_ops.asset_notes
                   (id, asset_type, asset_id, note, created_by, updated_by)
                   VALUES (%s::uuid, %s, %s, %s, %s, %s)
                   ON CONFLICT (asset_type, asset_id) DO NOTHING
                   RETURNING id::text""",
                (uid, kind, asset_id, note, actor, actor),
            ).fetchone()
            if not row:
                raise Conflict("A note for this asset already exists.")
            self.audit(conn, actor, "note.create", kind, asset_id)
        return {"id": uid, "assetType": kind, "assetId": asset_id,
                "note": note, "version": 1}

    def update_note(self, actor, uid, note, version):
        uid = uuid_text(uid)
        if not isinstance(note, str) or not 1 <= len(note.strip()) <= 1000:
            raise ValueError("Note must be 1..1000 characters")
        if not isinstance(version, int) or isinstance(version, bool) or version < 1:
            raise ValueError("Expected positive note version")
        with self.connection() as conn:
            row = conn.execute(
                """UPDATE console_ops.asset_notes
                   SET note=%s, updated_by=%s, updated_at=now(), version=version+1
                   WHERE id=%s::uuid AND version=%s
                   RETURNING asset_type, asset_id, version""",
                (note.strip(), actor, uid, version),
            ).fetchone()
            if not row:
                raise Conflict("Note changed or no longer exists. Refresh and try again.")
            self.audit(conn, actor, "note.update", row[0], row[1])
        return {"id": uid, "version": int(row[2]), "note": note.strip()}

    def delete_note(self, actor, uid, version):
        uid = uuid_text(uid)
        if not isinstance(version, int) or isinstance(version, bool) or version < 1:
            raise ValueError("Expected positive note version")
        with self.connection() as conn:
            row = conn.execute(
                """DELETE FROM console_ops.asset_notes WHERE id=%s::uuid AND version=%s
                   RETURNING asset_type, asset_id""",
                (uid, version),
            ).fetchone()
            if not row:
                raise Conflict("Note changed or no longer exists. Refresh and try again.")
            self.audit(conn, actor, "note.delete", row[0], row[1])
        return {"deleted": True, "id": uid}

    def request_cleanup(self, actor, preview, reason, confirmation):
        if not preview.get("found"):
            raise ValueError("Environment was not found")
        if not isinstance(reason, str) or not 10 <= len(reason.strip()) <= 1000:
            raise ValueError("Reason must be 10..1000 characters")
        environment = preview["environment"]
        if confirmation != environment["envKey"]:
            raise ValueError("Confirmation must exactly match env_key")
        uid = str(uuid4())
        # Keep only known-safe snapshot fields; never persist arbitrary
        # SQL, raw errors, credentials or execd token.
        snapshot = {
            key: preview.get(key) for key in (
                "environmentId", "status", "environment", "aliasCount",
                "activeBuildJobs", "publishedReferences",
                "activeLeaseReferences", "blockingReasons",
                "warnings", "canExecute",
            )
        }
        with self.connection() as conn:
            conn.execute(
                """INSERT INTO console_ops.cleanup_requests
                   (id, environment_id, env_key, status, reason,
                    preflight_snapshot, created_by)
                   VALUES (%s::uuid, %s::uuid, %s, 'DRAFT',
                           %s, %s::jsonb, %s)""",
                (uid, preview["environmentId"], environment["envKey"],
                 reason.strip(), json.dumps(snapshot, ensure_ascii=False), actor),
            )
            self.audit(conn, actor, "cleanup.request", "build_environment",
                       preview["environmentId"])
        return {
            "id": uid, "environmentId": preview["environmentId"],
            "status": "DRAFT", "executed": False,
            "message": "Request saved. No image or service record has been deleted.",
        }

    def cancel_cleanup(self, actor, uid):
        uid = uuid_text(uid)
        with self.connection() as conn:
            row = conn.execute(
                """UPDATE console_ops.cleanup_requests
                   SET status='CANCELLED', updated_at=now()
                   WHERE id=%s::uuid AND status='DRAFT'
                   RETURNING environment_id::text""",
                (uid,),
            ).fetchone()
            if not row:
                raise Conflict("Request not in DRAFT state or does not exist")
            self.audit(conn, actor, "cleanup.cancel", "build_environment", row[0])
        return {"id": uid, "status": "CANCELLED", "executed": False}
