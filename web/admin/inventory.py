"""Read-only operational inventory and cleanup preflight over supplied DDL.

Writes to the three service databases are intentionally not implemented:
their owning services must expose coordinated lifecycle APIs first.
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime
import logging
from uuid import UUID

from console.sql_schemas import configured_schemas, render_sql

log = logging.getLogger(__name__)


def stamp(value):
    return value.isoformat() if isinstance(value, datetime) else value


def safe_text(value, cap=160):
    return str(value)[:cap] if value is not None else None


class Inventory:
    def __init__(self, urls: dict[str, str], connections=None, schemas=None):
        self.urls = urls
        self.connections = connections or {}
        self.schemas = configured_schemas(schemas)

    @contextmanager
    def connect(self, service: str):
        dsn = self.urls.get(service, "")
        if not dsn and service not in self.connections:
            raise RuntimeError(f"{service} database not configured")
        if service in self.connections:
            with self.connections[service]() as conn:
                yield conn
            return
        import psycopg
        with psycopg.connect(
            dsn, autocommit=True, connect_timeout=3,
            options="-c default_transaction_read_only=on -c statement_timeout=5000",
        ) as conn:
            yield conn

    def _execute(self, conn, sql, args=()):
        return conn.execute(render_sql(sql, self.schemas), args)

    def _list(self, conn, query, cols, args=()):
        rows = self._execute(conn, query, args).fetchall()
        return [dict(zip(cols, (stamp(x) for x in row))) for row in rows]

    def report(self):
        result = {}
        query_map = {
            "environments": (
                "build",
                """SELECT id::text, env_key, status, image_ref,
                          python_version, platform, package_count, created_at, updated_at
                   FROM build.runtime_environments
                   ORDER BY updated_at DESC LIMIT 100""",
                ("id","envKey","status","imageRef","pythonVersion",
                 "platform","packageCount","createdAt","updatedAt"),
            ),
            "buildJobs": (
                "build",
                """SELECT id::text, env_id::text, status, created_at,
                          started_at, finished_at
                   FROM build.build_jobs ORDER BY created_at DESC LIMIT 70""",
                ("id","envId","status","createdAt","startedAt","finishedAt"),
            ),
            "leases": (
                "runner",
                """SELECT id::text, tenant_id, project_id, processor_id,
                          release_id, expires_at, created_at
                   FROM runner.leases ORDER BY expires_at ASC LIMIT 70""",
                ("id","tenantId","projectId","processorId","releaseId",
                 "expiresAt","createdAt"),
            ),
            "runs": (
                "runner",
                """SELECT run_id::text, tenant_id, processor_id, release_id, state,
                          outcome_class, status, duration_ms, started_at, completed_at
                   FROM runner.runs ORDER BY started_at DESC LIMIT 70""",
                ("id","tenantId","processorId","releaseId","state","outcomeClass",
                 "status","durationMs","startedAt","completedAt"),
            ),
            "idempotency": (
                "runner",
                """SELECT tenant_id, release_id, state, replay_count,
                          expires_at, created_at, updated_at
                   FROM runner.idempotency_keys
                   ORDER BY created_at DESC LIMIT 70""",
                ("tenantId","releaseId","state","replayCount",
                 "expiresAt","createdAt","updatedAt"),
            ),
            "variants": (
                "publish",
                """SELECT id::text, contract_id::text, backend, status,
                          published_ref, created_at, updated_at
                   FROM publish.backend_variants
                   ORDER BY updated_at DESC LIMIT 70""",
                ("id","contractId","backend","status","publishedRef",
                 "createdAt","updatedAt"),
            ),
            "publishJobs": (
                "publish",
                """SELECT id::text, variant_id::text, status, artifact_ref,
                          created_at, started_at, finished_at
                   FROM publish.publish_jobs
                   ORDER BY created_at DESC LIMIT 70""",
                ("id","variantId","status","artifactRef",
                 "createdAt","startedAt","finishedAt"),
            ),
        }
        by_service: dict[str, list[tuple]] = {}
        for name, (service, query, cols) in query_map.items():
            by_service.setdefault(service, []).append((name, query, cols))
        for service, items in by_service.items():
            try:
                with self.connect(service) as conn:
                    for name, query, cols in items:
                        try:
                            result[name] = {
                                "status": "ok",
                                "rows": self._list(conn, query, cols),
                                "limitedTo": 100 if name=="environments" else 70,
                            }
                        except Exception as exc:
                            log.warning("Inventory query %s failed (%s)",
                                        name, type(exc).__name__)
                            result[name] = {
                                "status": "error", "rows": [],
                                "message": "Inventory query failed; check schema and SELECT grants.",
                            }
            except Exception as exc:
                log.warning("Inventory source %s unavailable (%s)",
                            service, type(exc).__name__)
                for name, _, _ in items:
                    result[name] = {
                        "status": "unconfigured" if not self.urls.get(service) else "error",
                        "rows": [],
                        "message": "Database not configured or unavailable.",
                    }
        return result

    def asset_exists(self, kind: str, raw_id: str) -> bool:
        """Validate UUID and find only a real record in the owning service."""
        definitions = {
            "build_environment": ("build",
                "SELECT 1 FROM build.runtime_environments WHERE id=%s::uuid"),
            "runner_lease": ("runner",
                "SELECT 1 FROM runner.leases WHERE id=%s::uuid"),
            "runner_run": ("runner",
                "SELECT 1 FROM runner.runs WHERE run_id=%s::uuid"),
            "publish_variant": ("publish",
                "SELECT 1 FROM publish.backend_variants WHERE id=%s::uuid"),
        }
        if kind not in definitions:
            raise ValueError("Unsupported asset type")
        try:
            asset_id=str(UUID(raw_id))
        except (ValueError,TypeError,AttributeError) as exc:
            raise ValueError("Asset identifier must be a UUID") from exc
        service,sql=definitions[kind]
        try:
            with self.connect(service) as conn:
                return self._execute(conn, sql,(asset_id,)).fetchone() is not None
        except Exception as exc:
            log.warning("Asset existence check failed (%s)",type(exc).__name__)
            raise RuntimeError("Asset owner DB unavailable; note cannot be created") from exc

    def retry_preflight(self, env_key: str):
        """Read-only guard before Build Service's authoritative retry endpoint."""
        import re
        if not isinstance(env_key, str) or not re.fullmatch(r"[A-Za-z0-9_.-]{1,64}",env_key):
            raise ValueError("Invalid env_key")
        try:
            with self.connect("build") as conn:
                row=self._execute(conn, 
                    """SELECT id::text, env_key, status
                       FROM build.runtime_environments WHERE env_key = %s""",
                    (env_key,),
                ).fetchone()
                if row is None:
                    raise ValueError("Environment not found")
                active=self._execute(conn, 
                    """SELECT COUNT(*) FROM build.build_jobs WHERE env_id=%s::uuid
                       AND status IN ('BUILDING','VERIFYING')""",
                    (row[0],),
                ).fetchone()[0]
                if row[2] != "FAILED" or active:
                    raise ValueError(
                        "Retry requires FAILED status and no active build job."
                    )
                return {"id":row[0],"envKey":row[1],"status":row[2],
                        "activeJobs":int(active)}
        except ValueError:
            raise
        except Exception as exc:
            log.warning("Build retry preflight unavailable (%s)",type(exc).__name__)
            raise RuntimeError("Build retry preflight unavailable") from exc

    def cleanup_preview(self, raw_id: str):
        """Asset references are examined, but cannot establish physical safety.

        This preflight is a snapshot. Even zero active leases does NOT prove
        a Docker image or registry blob is safe to remove.
        """
        try:
            env_id = str(UUID(raw_id))
        except (ValueError, AttributeError, TypeError) as exc:
            raise ValueError("Invalid environment UUID") from exc

        preview = {
            "environmentId": env_id, "found": False,
            "status": "unavailable", "environment": None,
            "aliasCount": None, "activeBuildJobs": None,
            "publishedReferences": None, "activeLeaseReferences": None,
            "releaseIds": [],
            "blockingReasons": [],
            "warnings": [],
            "canExecute": False,
            "action": "request_only",
        }
        try:
            with self.connect("build") as conn:
                row = self._execute(conn, 
                    """SELECT id::text, env_key, status, image_ref,
                              package_count, updated_at
                       FROM build.runtime_environments WHERE id = %s""",
                    (env_id,),
                ).fetchone()
                if not row:
                    preview.update(status="not_found")
                    return preview
                preview.update(
                    found=True, status="ready",
                    environment={
                        "id": row[0], "envKey": row[1], "status": row[2],
                        "imageRef": safe_text(row[3], 320),
                        "packageCount": row[4], "updatedAt": stamp(row[5]),
                    },
                )
                preview["aliasCount"] = int(self._execute(conn, 
                    "SELECT COUNT(*) FROM build.runtime_env_aliases WHERE env_id = %s",
                    (env_id,),
                ).fetchone()[0])
                preview["activeBuildJobs"] = int(self._execute(conn, 
                    """SELECT COUNT(*) FROM build.build_jobs
                       WHERE env_id = %s AND status IN ('BUILDING','VERIFYING')""",
                    (env_id,),
                ).fetchone()[0])
                if row[2] in ("PENDING","BUILDING","VERIFYING"):
                    preview["blockingReasons"].append("Environment is being prepared.")
                if preview["activeBuildJobs"]:
                    preview["blockingReasons"].append("Build jobs are still active.")
                if preview["aliasCount"]:
                    preview["warnings"].append(
                        "Aliases point to this environment; cleanup must invalidate aliases via Build Service."
                    )
                if row[2] == "READY" and row[3] is None:
                    preview["warnings"].append("READY environment has no image_ref.")
                if row[3]:
                    preview["warnings"].append(
                        "Physical image existence, Docker hosts and registry references remain unverified."
                    )
                key = row[1]
                image = row[3]

            # Publishing metadata may point to the same runtime environment.
            release_ids = []
            try:
                with self.connect("publish") as conn:
                    rows = self._execute(conn, 
                        """SELECT DISTINCT result_json->>'releaseId'
                           FROM publish.publish_jobs
                           WHERE status = 'READY'
                             AND result_json IS NOT NULL
                             AND (
                                result_json->>'envKey' = %s
                                OR result_json->>'runtimeEnvKey' = %s
                                OR (%s IS NOT NULL AND result_json->>'runtimeImage' = %s)
                             )
                           LIMIT 101""",
                        (key, key, image, image),
                    ).fetchall()
                    release_ids = [str(r[0]) for r in rows if r[0]]
                    preview["publishedReferences"] = len(release_ids)
                    if any(not r[0] for r in rows):
                        preview["blockingReasons"].append(
                            "A matched Publish job has no releaseId; references cannot be verified."
                        )
                    if len(rows) >= 101:
                        preview["blockingReasons"].append("Publish references exceed query limit.")
            except Exception as exc:
                log.warning("Publish cross-check failed (%s)", type(exc).__name__)
                preview["blockingReasons"].append(
                    "Cannot verify Publish references; do not delete."
                )
            preview["releaseIds"] = release_ids[:30]
            try:
                with self.connect("runner") as conn:
                    if release_ids:
                        active = self._execute(conn, 
                            """SELECT COUNT(*) FROM runner.leases
                               WHERE expires_at > now()
                                 AND release_id = ANY(%s)""",
                            (release_ids,),
                        ).fetchone()[0]
                    else:
                        active = 0
                    preview["activeLeaseReferences"] = int(active)
                    if active:
                        preview["blockingReasons"].append("Active Runner leases reference published releases.")
            except Exception as exc:
                log.warning("Runner cross-check failed (%s)", type(exc).__name__)
                preview["activeLeaseReferences"] = None
                preview["blockingReasons"].append(
                    "Cannot verify Runner leases; do not delete."
                )
            preview["warnings"].append(
                "Runner Catalog, physical image usage, concurrent jobs and remote registry are not verifiable from these tables."
            )
            return preview
        except Exception as exc:
            log.warning("Build environment preflight failed (%s)", type(exc).__name__)
            preview["blockingReasons"].append(
                "Build database unavailable; cleanup must not proceed."
            )
            return preview
