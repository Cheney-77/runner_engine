"""Bounded, read-only aggregate database adapter.

Table names are discovered via system catalog; no user-defined raw SQL and
no business records are returned. Supports SQLite and optional PostgreSQL.
"""
from __future__ import annotations

import logging
import re
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote, unquote, urlsplit

from .settings import IDENTIFIER, SERVICES, Settings

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Metric:
    key: str
    label: str
    aliases: tuple[str, ...]
    statuses: bool = True
    backend_breakdown: bool = False


METRICS: dict[str, tuple[Metric, ...]] = {
    "publish": (
        Metric("operators", "Operators", ("operators", "operator"), False),
        Metric("contracts", "Contract versions", (
            "operator_contracts", "virtual_contracts", "contracts",
            "operator_contract_versions", "virtual_operator_contracts",
        ), False),
        Metric("variants", "Backend variants", (
            "backend_variants", "operator_variants", "variants",
        ), True, True),
        Metric("jobs", "Publish jobs", (
            "publish_jobs", "publishing_jobs", "publish_job",
        ), True, True),
    ),
    "build": (
        Metric("environments", "Runtime environments", (
            "runtime_environments", "runtime_environment",
            "build_environments",
        )),
        Metric("jobs", "Build jobs", (
            "build_jobs", "environment_build_jobs", "runtime_build_jobs",
        )),
    ),
    "runner": (
        Metric("releases", "Registered releases", (
            "releases", "operator_releases", "release",
        ), False),
        Metric("executions", "Executions", (
            "executions", "execution_runs", "run_records",
            "execution_records",
        )),
        Metric("jobs", "Runner jobs", (
            "runner_jobs", "execution_jobs", "runner_tasks",
        )),
    ),
}
TIME_NAMES = ("created_at", "createdAt", "created_on", "createdOn")
STATUS_NAMES = ("status", "state", "phase")
GROUP_NAMES = ("backend", "backend_type", "backendType")


def quote_identifier(name: str) -> str:
    if not IDENTIFIER.fullmatch(name):
        raise ValueError("SQL identifier is invalid")
    return '"' + name + '"'


def normalize_error(exc: Exception) -> str:
    # Connection error text may contain a password, SQL or DB URL. Do not
    # return or log it in an HTTP response.
    if isinstance(exc, (sqlite3.Error, OSError)):
        return "Database could not be opened or queried; check read-only path and permissions."
    return "Database connection/query failed; check credentials, schema and read-only permissions."


def parse_sqlite_path(url: str) -> Path:
    parts = urlsplit(url)
    if parts.scheme != "sqlite" or parts.netloc not in ("",):
        raise ValueError("SQLite URL must be sqlite:////absolute/path/file.db")
    if parts.query or parts.fragment:
        raise ValueError("SQLite URL query and fragment are not supported")
    path = Path(unquote(parts.path))
    if not path.is_absolute():
        raise ValueError("SQLite database path must be absolute")
    return path.resolve(strict=True)


class DBReader:
    def __init__(self, service: str, url: str, schema: str = "public", mapping=None):
        if service not in SERVICES:
            raise ValueError("Unknown service")
        self.service = service
        self.url = url
        self.schema = schema
        self.mapping = mapping or {}
        if not isinstance(self.mapping, dict):
            raise ValueError("Metric mapping must be a dict")

    @property
    def dialect(self) -> str:
        scheme = urlsplit(self.url).scheme
        if scheme == "sqlite":
            return "sqlite"
        if scheme in ("postgresql", "postgres"):
            return "postgresql"
        raise ValueError("Only SQLite and PostgreSQL URLs are supported")

    @contextmanager
    def connect(self):
        if self.dialect == "sqlite":
            path = parse_sqlite_path(self.url)
            # Opens the existing file ONLY, never creates or writes a DB.
            uri = "file:" + quote(str(path), safe="/") + "?mode=ro"
            conn = sqlite3.connect(uri, uri=True, timeout=2)
            try:
                conn.execute("PRAGMA query_only=ON")
                conn.execute("PRAGMA busy_timeout=2000")
                conn.execute("PRAGMA trusted_schema=OFF")
                yield conn
            finally:
                conn.close()
        else:
            try:
                import psycopg
            except ImportError as exc:
                raise RuntimeError(
                    "PostgreSQL requires: pip install -r observe/requirements-postgres.txt"
                ) from exc
            parsed = urlsplit(self.url)
            if not parsed.hostname or parsed.scheme not in ("postgres", "postgresql"):
                raise ValueError("Invalid PostgreSQL URL")
            conn = psycopg.connect(
                self.url,
                autocommit=True,
                connect_timeout=3,
                options="-c default_transaction_read_only=on -c statement_timeout=3000",
            )
            try:
                # Explicitly mark this session read-only too. The configured
                # PostgreSQL role must ALSO have SELECT-only permissions.
                conn.execute("SET SESSION CHARACTERISTICS AS TRANSACTION READ ONLY")
                yield conn
            finally:
                conn.close()

    def describe(self, conn) -> dict[str, list[str]]:
        if self.dialect == "sqlite":
            rows = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name NOT LIKE 'sqlite_%' ORDER BY name LIMIT 150"
            ).fetchall()
            catalog = {}
            for (name,) in rows:
                if not IDENTIFIER.fullmatch(name):
                    continue
                cols = conn.execute(
                    f"PRAGMA table_info({quote_identifier(name)})"
                ).fetchall()
                catalog[name] = [
                    c[1] for c in cols
                    if isinstance(c[1], str) and IDENTIFIER.fullmatch(c[1])
                ]
            return catalog

        table_rows = conn.execute(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema = %s AND table_type = 'BASE TABLE' "
            "ORDER BY table_name LIMIT 150",
            (self.schema,),
        ).fetchall()
        catalog = {}
        for (name,) in table_rows:
            if not IDENTIFIER.fullmatch(name):
                continue
            col_rows = conn.execute(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_schema = %s AND table_name = %s "
                "ORDER BY ordinal_position",
                (self.schema, name),
            ).fetchall()
            catalog[name] = [
                row[0] for row in col_rows
                if IDENTIFIER.fullmatch(row[0])
            ]
        return catalog

    def _table(self, table: str) -> str:
        if self.dialect == "sqlite":
            return quote_identifier(table)
        return f"{quote_identifier(self.schema)}.{quote_identifier(table)}"

    @staticmethod
    def _column(spec: dict, config_name: str, columns: list[str], candidates):
        requested = spec.get(config_name)
        if requested is not None:
            if requested not in columns:
                raise ValueError(f"Configured column {config_name} was not found")
            return requested
        return next((name for name in candidates if name in columns), None)

    def _match(self, metric: Metric, catalog: dict):
        user = self.mapping.get(metric.key)
        if user is not None:
            spec = {"table": user} if isinstance(user, str) else dict(user)
            table = spec.get("table")
            if table not in catalog:
                return "missing", None, {}, "Configured table was not found"
            return "explicit", table, spec, ""
        matches = [candidate for candidate in metric.aliases if candidate in catalog]
        if len(matches) == 1:
            return "auto", matches[0], {}, ""
        if len(matches) > 1:
            return "ambiguous", None, {}, "More than one possible table; configure a mapping."
        return "missing", None, {}, "No matching table; set OBS_TABLE_MAP_FILE."

    def _one(self, conn, metric: Metric, catalog: dict) -> dict:
        quality, table, spec, reason = self._match(metric, catalog)
        result = {
            "key": metric.key,
            "label": metric.label,
            "quality": quality,
            "table": table,
            "count": None,
            "recent24h": None,
            "latestAt": None,
            "byStatus": [],
            "byBackend": [],
            "note": reason,
        }
        if table is None:
            return result

        cols = catalog[table]
        qualified = self._table(table)
        try:
            result["count"] = int(conn.execute(
                f"SELECT COUNT(*) FROM {qualified}"
            ).fetchone()[0])

            status = self._column(
                spec, "statusColumn", cols,
                STATUS_NAMES if metric.statuses else (),
            )
            backend = self._column(
                spec, "groupColumn", cols,
                GROUP_NAMES if metric.backend_breakdown else (),
            )
            created = self._column(spec, "timeColumn", cols, TIME_NAMES)
            if status:
                result["byStatus"] = self._groups(conn, qualified, status, result["count"])
            if backend:
                result["byBackend"] = self._groups(conn, qualified, backend, result["count"])
            if created:
                col = quote_identifier(created)
                cutoff = datetime.now(timezone.utc) - timedelta(hours=24)
                if self.dialect == "sqlite":
                    # ISO-8601 UTC text is the convention of this optional
                    # measure. If format differs, leave it unreported rather
                    # than presenting an incorrectly computed number.
                    sample = conn.execute(
                        f"SELECT {col} FROM {qualified} "
                        f"WHERE {col} IS NOT NULL LIMIT 1"
                    ).fetchone()
                    if sample and not self._iso_timestamp(sample[0]):
                        result["note"] = "Time column is not ISO-8601; recent count omitted."
                        return result
                    parameter = cutoff.isoformat()
                    recent_sql = f"SELECT COUNT(*) FROM {qualified} WHERE {col} >= ?"
                    recent_params = (parameter,)
                else:
                    parameter = cutoff
                    recent_sql = f"SELECT COUNT(*) FROM {qualified} WHERE {col} >= %s"
                    recent_params = (parameter,)
                result["recent24h"] = int(conn.execute(
                    recent_sql, recent_params
                ).fetchone()[0])
                latest = conn.execute(f"SELECT MAX({col}) FROM {qualified}").fetchone()[0]
                result["latestAt"] = (
                    latest.isoformat() if hasattr(latest, "isoformat")
                    else str(latest)[:60] if latest is not None else None
                )
            return result
        except (ValueError, sqlite3.Error, RuntimeError, TypeError, Exception) as exc:
            # A metric failing must not hide other independent service metrics.
            log.warning("Aggregate query failed for %s.%s (%s)",
                        self.service, metric.key, type(exc).__name__)
            result.update(
                quality="error", count=None, recent24h=None, latestAt=None,
                byStatus=[], byBackend=[],
                note="Metric query failed. Verify mapped table/columns and data types.",
            )
            return result

    @staticmethod
    def _iso_timestamp(value) -> bool:
        if isinstance(value, datetime):
            return True
        if not isinstance(value, str) or not re.match(
            r"^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:", value
        ):
            return False
        try:
            datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return False
        return True

    @staticmethod
    def _groups(conn, table: str, column: str, total: int) -> list[dict]:
        col = quote_identifier(column)
        rows = conn.execute(
            f"SELECT CAST({col} AS TEXT) AS label, COUNT(*) AS amount "
            f"FROM {table} GROUP BY {col} "
            f"ORDER BY amount DESC LIMIT 9"
        ).fetchall()
        parsed = [
            {"label": str(label)[:64] if label is not None else "(null)",
             "count": int(amount)}
            for label, amount in rows[:8]
        ]
        covered = sum(item["count"] for item in parsed)
        if covered < total:
            parsed.append({"label": "Other", "count": total - covered})
        return parsed

    def report(self) -> dict:
        if not self.url:
            return {
                "service": self.service, "status": "unconfigured",
                "message": "Set this service's OBS_*_DB_URL.",
                "metrics": self._empty_metrics(),
                "tables": [], "dialect": None,
            }
        try:
            with self.connect() as conn:
                catalog = self.describe(conn)
                metrics = [self._one(conn, m, catalog) for m in METRICS[self.service]]
                return {
                    "service": self.service,
                    "status": "ok",
                    "message": "",
                    "metrics": metrics,
                    "tables": [
                        {"name": table, "columns": cols[:60]}
                        for table, cols in list(catalog.items())[:100]
                    ],
                    "dialect": self.dialect,
                }
        except Exception as exc:
            log.warning("Database unavailable for %s (%s)",
                        self.service, type(exc).__name__)
            return {
                "service": self.service, "status": "error",
                "message": normalize_error(exc),
                "metrics": self._empty_metrics(),
                "tables": [], "dialect": None,
            }

    def _empty_metrics(self):
        return [
            {
                "key": metric.key, "label": metric.label,
                "quality": "unconfigured", "table": None,
                "count": None, "recent24h": None, "latestAt": None,
                "byStatus": [], "byBackend": [], "note": "",
            } for metric in METRICS[self.service]
        ]


def readers(settings: Settings) -> dict[str, DBReader]:
    return {
        service: DBReader(
            service,
            settings.db_urls.get(service, ""),
            settings.db_schemas.get(service, "public"),
            settings.table_map.get(service, {}),
        )
        for service in SERVICES
    }
