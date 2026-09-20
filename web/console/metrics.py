"""PostgreSQL metrics derived ONLY from the provided 3 service DDLs.

No table discovery, guesses, SQL templates from environment, or OBS_TABLE_MAP_FILE.
All SQL table/column identifiers below are code constants. Aggregate rows only.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import logging

from .sql_schemas import configured_schemas, physical_table

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Spec:
    key: str
    label: str
    table: str
    time: str | None = None
    status: str | None = None
    backend: str | None = None
    count_expression: str = "COUNT(*)"
    note: str = ""


SPECS: dict[str, tuple[Spec, ...]] = {
    "publish": (
        Spec("operators", "Operators", "publish.operators", "created_at"),
        Spec("contracts", "Contract versions", "publish.virtual_contract_versions", "created_at"),
        Spec("variants", "Backend variants", "publish.backend_variants", "created_at",
             "status", "backend"),
        Spec("jobs", "Publish jobs", "publish.publish_jobs", "created_at", "status"),
    ),
    "build": (
        Spec("environments", "Runtime environments", "build.runtime_environments",
             "created_at", "status"),
        Spec("aliases", "Environment aliases", "build.runtime_env_aliases",
             "created_at", "resolution_kind"),
        Spec("jobs", "Build jobs", "build.build_jobs", "created_at", "status"),
    ),
    "runner": (
        Spec("leases", "Leases", "runner.leases", "created_at"),
        Spec("runs", "Runs", "runner.runs", "created_at", "state"),
        Spec("idempotency", "Idempotency keys", "runner.idempotency_keys",
             "created_at", "state"),
        Spec("releases", "Referenced release IDs", "runner.leases",
             count_expression=(
                 "COUNT(DISTINCT release_id)"
             ),
             note="Unique IDs in runner.leases; NOT the count of actual Release artifacts."),
    ),
}

# Exact column names from the supplied DDL. For display only: never accept
# identifiers from browser input or configuration.
KNOWN_COLUMNS = {
    "publish.operators": (
        "id workspace name display_name description created_at updated_at"
    ).split(),
    "publish.virtual_contract_versions": (
        "id operator_id version source_revision source_ref contract_sha256 "
        "contract_json created_at"
    ).split(),
    "publish.backend_variants": (
        "id contract_id backend compiler_version variant_key options_json "
        "backend_contract_sha256 backend_contract_json status published_ref "
        "published_metadata last_error created_at updated_at"
    ).split(),
    "publish.publish_jobs": (
        "id variant_id status artifact_ref result_json error_message "
        "created_at started_at finished_at"
    ).split(),
    "build.runtime_environments": (
        "id env_key base_image python_version platform build_policy_version "
        "lock_sha256 requirements_lock packages package_count image_ref status "
        "last_error created_at updated_at"
    ).split(),
    "build.runtime_env_aliases": (
        "request_env_key env_id resolution_kind created_at"
    ).split(),
    "build.build_jobs": (
        "id env_id status error_message created_at started_at finished_at"
    ).split(),
    "runner.leases": (
        "id tenant_id project_id processor_id release_id expires_at created_at updated_at"
    ).split(),
    "runner.runs": (
        "run_id tenant_id project_id processor_id release_id lease_id "
        "invocation_id idempotency_key request_fingerprint state outcome_class "
        "status relationship retryable error_code error_message worker_id duration_ms "
        "replayable idempotency_expires_at started_at completed_at created_at updated_at"
    ).split(),
    "runner.idempotency_keys": (
        "tenant_id release_id idempotency_key request_fingerprint state current_run_id "
        "lease_id invocation_id replayable result_json expires_at replay_count "
        "last_replayed_at created_at updated_at"
    ).split(),
}

# Metrics that explain operational risk, grounded in explicit columns and
# state CHECK constraints supplied by the user.
CHECKS: dict[str, tuple[tuple[str, str, str, str, str], ...]] = {
    "publish": (
        ("publish_failed", "Failed publish jobs", "high",
         "publish.publish_jobs", "status = 'FAILED'"),
        ("publish_running_stale", "Publish jobs RUNNING > 1h", "warning",
         "publish.publish_jobs",
         "status = 'RUNNING' AND started_at < now() - interval '1 hour'"),
        ("variants_compiled_only", "Variants not yet published", "info",
         "publish.backend_variants", "status = 'COMPILED'"),
        ("variants_failed", "Failed backend variants", "high",
         "publish.backend_variants", "status = 'FAILED'"),
    ),
    "build": (
        ("ready_missing_image", "READY environments without image_ref", "high",
         "build.runtime_environments", "status = 'READY' AND image_ref IS NULL"),
        ("failed_environments", "FAILED runtime environments", "high",
         "build.runtime_environments", "status = 'FAILED'"),
        ("build_stuck", "BUILDING / VERIFYING > 1h", "warning",
         "build.runtime_environments",
         "status IN ('BUILDING','VERIFYING') AND updated_at < now() - interval '1 hour'"),
        ("build_failed_jobs", "Failed build jobs", "high",
         "build.build_jobs", "status = 'FAILED'"),
        ("superset_aliases", "Superset aliases", "info",
         "build.runtime_env_aliases", "resolution_kind = 'superset'"),
    ),
    "runner": (
        ("active_leases", "Unexpired leases", "info",
         "runner.leases", "expires_at > now()"),
        ("expired_leases", "Expired leases awaiting cleanup", "warning",
         "runner.leases", "expires_at <= now()"),
        ("runs_stale", "RUNNING runs > 1h", "warning",
         "runner.runs",
         "state = 'RUNNING' AND started_at < now() - interval '1 hour'"),
        ("abandoned_runs", "Abandoned runs", "warning",
         "runner.runs", "state = 'ABANDONED'"),
        ("expired_idempotency", "Expired idempotency keys", "warning",
         "runner.idempotency_keys",
         "expires_at IS NOT NULL AND expires_at <= now()"),
        ("replayed_keys", "Keys with replay_count > 0", "info",
         "runner.idempotency_keys", "replay_count > 0"),
    ),
}


class ExactPGReader:
    def __init__(self, service: str, dsn: str, connect=None, schemas=None):
        if service not in SPECS:
            raise ValueError("Unknown service")
        self.service = service
        self.dsn = dsn
        self.connect_factory = connect
        self.schemas = configured_schemas(schemas)

    def _connection(self):
        if self.connect_factory:
            return self.connect_factory()
        try:
            import psycopg
        except ImportError as exc:
            raise RuntimeError("Install psycopg: pip install -r console/requirements.txt") from exc
        # Uses only SELECT, enforces read-only at the PostgreSQL session level.
        return psycopg.connect(
            self.dsn, connect_timeout=3, autocommit=True,
            options="-c default_transaction_read_only=on -c statement_timeout=5000",
        )

    @staticmethod
    def _fetch(conn, sql: str):
        return conn.execute(sql).fetchall()

    def _metric(self, conn, spec: Spec):
        table = physical_table(spec.table, self.schemas)
        record = {
            "key": spec.key, "label": spec.label, "table": table,
            "quality": "exact", "count": None, "recent24h": None,
            "latestAt": None, "byStatus": [], "byBackend": [],
            "note": spec.note,
        }
        try:
            record["count"] = int(
                self._fetch(conn, f"SELECT {spec.count_expression} FROM {table}")[0][0]
            )
            if spec.time:
                rows = self._fetch(conn,
                    f"SELECT COUNT(*) FILTER (WHERE {spec.time} >= now() - interval '24 hours'), "
                    f"MAX({spec.time}) FROM {table}")
                record["recent24h"] = int(rows[0][0])
                instant = rows[0][1]
                record["latestAt"] = instant.isoformat() if instant else None
            for col, field in ((spec.status, "byStatus"), (spec.backend, "byBackend")):
                if col:
                    rows = self._fetch(conn,
                        f"SELECT CAST({col} AS TEXT), COUNT(*) FROM {table} "
                        f"GROUP BY {col} ORDER BY COUNT(*) DESC LIMIT 9")
                    parts = [{"label": (str(n) if n is not None else "(null)")[:64],
                              "count": int(count)} for n, count in rows[:8]]
                    overflow = record["count"] - sum(part["count"] for part in parts)
                    if overflow > 0:
                        parts.append({"label": "Other", "count": overflow})
                    record[field] = parts
        except Exception as exc:
            log.warning("Metric %s.%s unavailable: %s",
                        self.service, spec.key, type(exc).__name__)
            record.update(quality="error", count=None, recent24h=None,
                          latestAt=None, byStatus=[], byBackend=[],
                          note="Query failed; check this exact table, column and SELECT permission.")
        return record

    def report(self):
        blank = {
            "service": self.service, "status": "unconfigured",
            "dialect": "postgresql", "metrics": [], "checks": [],
            "tables": [], "message": "",
        }
        if not self.dsn:
            blank["message"] = "Configure OBS_" + self.service.upper() + "_DB_URL."
            blank["metrics"] = self._empty_metrics()
            return blank
        try:
            with self._connection() as conn:
                metrics = [self._metric(conn, spec) for spec in SPECS[self.service]]
                checks = []
                for key, label, severity, table, where in CHECKS[self.service]:
                    try:
                        count = int(self._fetch(
                            conn, f"SELECT COUNT(*) FROM {physical_table(table, self.schemas)} WHERE {where}"
                        )[0][0])
                        checks.append({"key": key, "label": label,
                                       "severity": severity, "count": count})
                    except Exception as exc:
                        log.warning("Check %s failed: %s", key, type(exc).__name__)
                        checks.append({"key": key, "label": label,
                                       "severity": severity, "count": None})
                blank.update(
                    status="ok" if any(m["quality"]=="exact" for m in metrics)
                    else "error",
                    metrics=metrics, checks=checks,
                    message="" if any(m["quality"]=="exact" for m in metrics)
                    else "No metrics accessible; verify schema and grants.",
                    # Explicit DDL tables, not schema enumeration:
                    tables=[{"name": physical_table(spec.table, self.schemas),
                             "columns": KNOWN_COLUMNS[spec.table]}
                            for spec in SPECS[self.service]
                            if spec.key != "releases"],
                )
                return blank
        except Exception as exc:
            log.warning("PostgreSQL %s connection failed: %s",
                        self.service, type(exc).__name__)
            blank.update(status="error",
                         message="PostgreSQL unavailable; check DSN, credentials, network and SELECT grants.",
                         metrics=self._empty_metrics())
            return blank

    def _empty_metrics(self):
        return [
            {"key": spec.key, "label": spec.label,
             "table": physical_table(spec.table, self.schemas),
             "quality": "unconfigured", "count": None, "recent24h": None,
             "latestAt": None, "byStatus": [], "byBackend": [],
             "note": spec.note}
            for spec in SPECS[self.service]
        ]


def exact_readers(db_urls: dict[str, str], *, schemas=None, connect_factories=None):
    connect_factories = connect_factories or {}
    schemas = configured_schemas(schemas)
    return {
        service: ExactPGReader(
            service, db_urls.get(service, ""),
            connect=connect_factories.get(service),
            schemas=schemas,
        )
        for service in SPECS
    }
