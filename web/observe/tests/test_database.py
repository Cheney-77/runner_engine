from __future__ import annotations

import hashlib
import json
import sqlite3
import tempfile
import unittest
import zipfile
from datetime import datetime, timezone
from pathlib import Path

from observe.database import DBReader
from observe.settings import read_mapping

ROOT = Path(__file__).resolve().parents[2]


def make_database(path: Path, statements: list[str]):
    conn = sqlite3.connect(path)
    try:
        for command in statements:
            conn.execute(command)
        conn.commit()
    finally:
        conn.close()
    return "sqlite:////" + str(path).lstrip("/")


class TestDatabaseReader(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "publish.db"
        self.url = make_database(self.path, [
            'CREATE TABLE operators (id INTEGER PRIMARY KEY, created_at TEXT)',
            'CREATE TABLE operator_contracts (id INTEGER PRIMARY KEY)',
            'CREATE TABLE backend_variants (id INTEGER PRIMARY KEY, status TEXT, backend TEXT)',
            'CREATE TABLE publish_jobs (id INTEGER PRIMARY KEY, status TEXT, backend TEXT, created_at TEXT)',
            "INSERT INTO operators VALUES (1, '2026-09-16T10:00:00+00:00')",
            "INSERT INTO operators VALUES (2, '2026-09-16T11:00:00+00:00')",
            "INSERT INTO operator_contracts VALUES (100)",
            "INSERT INTO backend_variants VALUES (1,'READY','runner')",
            "INSERT INTO backend_variants VALUES (2,'FAILED','nifi_native')",
            "INSERT INTO publish_jobs VALUES (1,'READY','runner','2026-09-18T02:00:00+00:00')",
            "INSERT INTO publish_jobs VALUES (2,'FAILED','nifi_native','2026-09-18T02:05:00+00:00')",
        ])

    def test_real_readonly_counts_and_status(self):
        report = DBReader("publish", self.url).report()
        self.assertEqual(report["status"], "ok")
        metrics = {m["key"]: m for m in report["metrics"]}
        self.assertEqual(metrics["operators"]["count"], 2)
        self.assertEqual(metrics["contracts"]["count"], 1)
        self.assertEqual(metrics["variants"]["count"], 2)
        self.assertEqual(metrics["jobs"]["count"], 2)
        self.assertEqual(
            {item["label"]: item["count"] for item in metrics["jobs"]["byStatus"]},
            {"FAILED": 1, "READY": 1},
        )
        self.assertEqual(
            {item["label"]: item["count"] for item in metrics["jobs"]["byBackend"]},
            {"runner": 1, "nifi_native": 1},
        )
        self.assertNotIn("sqlite_sequence", {t["name"] for t in report["tables"]})
        after = sqlite3.connect(self.path)
        self.assertEqual(after.execute("SELECT COUNT(*) FROM publish_jobs").fetchone()[0], 2)
        after.close()

    def test_unmapped_and_ambiguous_never_look_like_zero(self):
        extra = sqlite3.connect(self.path)
        extra.execute("CREATE TABLE operator (id INTEGER)")
        extra.commit()
        extra.close()
        report = DBReader("publish", self.url).report()
        metric = next(x for x in report["metrics"] if x["key"] == "operators")
        self.assertEqual(metric["quality"], "ambiguous")
        self.assertIsNone(metric["count"])

    def test_explicit_table_column_mapping(self):
        url = make_database(Path(self.temp.name) / "custom.db", [
            'CREATE TABLE custom_runtime (id INTEGER, phase TEXT, made_at TEXT)',
            "INSERT INTO custom_runtime VALUES (1,'READY','2026-09-18T03:00:00+00:00')",
            "INSERT INTO custom_runtime VALUES (2,'FAILED','2026-09-18T03:00:00+00:00')",
        ])
        reader = DBReader("build", url, mapping={
            "environments": {
                "table": "custom_runtime",
                "statusColumn": "phase",
                "timeColumn": "made_at",
            },
        })
        data = reader.report()
        row = next(x for x in data["metrics"] if x["key"] == "environments")
        self.assertEqual(row["quality"], "explicit")
        self.assertEqual(row["count"], 2)
        self.assertEqual(len(row["byStatus"]), 2)

    def test_unconfigured_and_missing_file(self):
        self.assertEqual(DBReader("runner", "").report()["status"], "unconfigured")
        missing = DBReader("runner", "sqlite:////no/such/observability-file.db").report()
        self.assertEqual(missing["status"], "error")
        self.assertIsNone(missing["metrics"][0]["count"])
        self.assertNotIn("sqlite:////", missing["message"])

    def test_query_only_blocks_writes(self):
        db = DBReader("publish", self.url)
        with db.connect() as conn:
            with self.assertRaises(sqlite3.OperationalError):
                conn.execute("INSERT INTO operators (id) VALUES (9999)")
        conn = sqlite3.connect(self.path)
        self.assertEqual(conn.execute(
            "SELECT COUNT(*) FROM operators WHERE id=9999"
        ).fetchone()[0], 0)
        conn.close()

    def test_mapping_validation_blocks_sql_identifier_injection(self):
        config = Path(self.temp.name) / "mapping.json"
        config.write_text(json.dumps({
            "build": {"environments": {
                "table": 'runtime_environments"; DROP TABLE x;--'
            }}
        }), encoding="utf-8")
        with self.assertRaises(ValueError):
            read_mapping(str(config))

    def test_baseline_files_are_byte_identical(self):
        archive = Path("/mnt/data/managed-python-v33-chrome-console-v2.zip")
        if not archive.exists():
            self.skipTest("Original distributed archive not mounted")
        with zipfile.ZipFile(archive) as zipfile_reader:
            original = {
                str(Path(name).relative_to("managed-python-v33-chrome-console-v2")):
                hashlib.sha256(zipfile_reader.read(name)).hexdigest()
                for name in zipfile_reader.namelist()
                if not name.endswith("/")
            }
        for name, digest in original.items():
            path = ROOT / name
            self.assertTrue(path.is_file(), name)
            self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), digest, name)


if __name__ == "__main__":
    unittest.main()
