from __future__ import annotations

import os
import unittest
from contextlib import contextmanager
from datetime import datetime, timezone
from unittest.mock import patch

from console.metrics import CHECKS, SPECS, ExactPGReader
from console.settings import ConsoleSettings


class FakePG:
    """Synthetic rows, but all SQL shapes are asserted against DDL constants."""
    def __init__(self):
        self.statements = []

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def execute(self, statement):
        self.statements.append(statement)
        assert statement.strip().startswith("SELECT "), statement
        assert "OBS_TABLE_MAP_FILE" not in statement
        if "FILTER" in statement:
            self.rows = [(2, datetime(2026, 9, 18, tzinfo=timezone.utc))]
        elif "GROUP BY" in statement:
            self.rows = [("READY", 3), ("FAILED", 1)]
        elif "COUNT(DISTINCT" in statement:
            self.rows = [(2,)]
        elif "COUNT(*)" in statement:
            self.rows = [(4,)]
        else:
            raise AssertionError(statement)
        return self

    def fetchall(self):
        return self.rows


class TestExactMetrics(unittest.TestCase):
    def test_real_ddls_are_hardcoded_no_release_table_and_no_map(self):
        self.assertEqual(
            {s.table for s in SPECS["runner"]},
            {"runner.leases", "runner.runs", "runner.idempotency_keys"}
        )
        self.assertEqual(
            {s.table for s in SPECS["build"]},
            {"build.runtime_environments", "build.runtime_env_aliases",
             "build.build_jobs"}
        )
        self.assertEqual(
            {s.table for s in SPECS["publish"]},
            {"publish.operators", "publish.virtual_contract_versions",
             "publish.backend_variants", "publish.publish_jobs"}
        )
        for service in SPECS:
            conn = FakePG()
            report = ExactPGReader(
                service, "postgresql://fake/fixture", connect=lambda: conn
            ).report()
            self.assertEqual(report["status"], "ok")
            self.assertEqual(len(report["checks"]), len(CHECKS[service]))
            self.assertTrue(all(
                row["quality"] == "exact" for row in report["metrics"]
            ))
            self.assertTrue(all(statement.startswith("SELECT ")
                                for statement in conn.statements))
            self.assertNotIn("runner.releases", "\n".join(conn.statements))

    def test_no_table_map_file_even_if_poisoned(self):
        with patch.dict(os.environ, {
            "OBS_TABLE_MAP_FILE": "/DOES/NOT/EXIST/bad.json",
            "OBS_PUBLISH_DB_URL": "postgresql://observer@pg/platform",
        }):
            settings = ConsoleSettings.environment()
        self.assertEqual(settings.observation.table_map, {})
        self.assertEqual(
            settings.observation.db_schemas,
            {"publish": "publish", "build": "build", "runner": "runner"},
        )

    def test_each_service_uses_independent_schema_for_every_query(self):
        schemas = {
            "publish": "publish_metrics",
            "build": "build_metrics",
            "runner": "runner_metrics",
        }
        for service in SPECS:
            conn = FakePG()
            report = ExactPGReader(
                service, "postgresql://fixture",
                connect=lambda: conn, schemas=schemas,
            ).report()
            self.assertEqual(report["status"], "ok")
            expected = f'"{schemas[service]}".'
            self.assertTrue(conn.statements)
            for sql in conn.statements:
                self.assertIn(expected, sql)
                self.assertNotIn(f"FROM {service}.", sql)
            self.assertTrue(all(
                metric["table"].startswith(expected)
                for metric in report["metrics"]
            ))
            self.assertTrue(all(
                row["name"].startswith(expected)
                for row in report["tables"]
            ))

    def test_schema_environment_independent_and_identifier_validation(self):
        with patch.dict(os.environ, {
            "OBS_PUBLISH_DB_SCHEMA": "metrics_publish",
            "OBS_BUILD_DB_SCHEMA": "app_build",
            "OBS_RUNNER_DB_SCHEMA": "production_runner",
            "OBS_TABLE_MAP_FILE": "/INVALID/DONT/READ.json",
            "OBS_BASIC_USER": "old", "OBS_BASIC_PASSWORD": "old",
            "ADMIN_USER": "old", "ADMIN_PASSWORD": "old",
        }):
            setting = ConsoleSettings.environment()
        self.assertEqual(setting.observation.db_schemas, {
            "publish": "metrics_publish",
            "build": "app_build",
            "runner": "production_runner",
        })
        self.assertEqual(setting.observation.table_map, {})
        self.assertEqual(setting.observation.basic_user, "")
        self.assertEqual(setting.observation.basic_password, "")
        self.assertEqual(setting.audit_actor, "console-operator")
        for bad in ('unsafe;DROP TABLE t', 'public.foo', 'has spaces', '"quoted"'):
            with patch.dict(os.environ, {
                "OBS_PUBLISH_DB_SCHEMA": bad,
            }):
                with self.assertRaises(ValueError):
                    ConsoleSettings.environment()

    def test_unknown_metric_is_none_not_zero(self):
        report = ExactPGReader("build", "").report()
        self.assertEqual(report["status"], "unconfigured")
        self.assertTrue(all(item["count"] is None for item in report["metrics"]))


if __name__ == "__main__":
    unittest.main()
