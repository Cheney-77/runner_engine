from __future__ import annotations

import unittest
from datetime import datetime, timezone
from uuid import uuid4

from admin.inventory import Inventory
from admin.store import AdminStore, Conflict, StorageUnavailable

ENV = str(uuid4())
RELEASE = "release-abc"


class PreflightConnection:
    def __init__(self, service, queries):
        self.service = service
        self.queries = queries
        self.rows = []

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def execute(self, sql, args=()):
        self.queries.append((self.service, sql, args))
        assert sql.lstrip().startswith("SELECT "), sql
        if self.service == "build":
            if "FROM build.runtime_environments WHERE id" in sql:
                self.rows = [(ENV, "env-key-a", "READY", None, 5,
                              datetime.now(timezone.utc))]
            elif "FROM build.runtime_env_aliases" in sql:
                self.rows = [(2,)]
            elif "FROM build.build_jobs" in sql:
                self.rows = [(1,)]
            else:
                self.rows = []
        elif self.service == "publish":
            self.rows = [(RELEASE,)]
        elif self.service == "runner":
            self.rows = [(1,)]
        return self

    def fetchone(self):
        return self.rows[0] if self.rows else None

    def fetchall(self):
        return self.rows


class TestPreflight(unittest.TestCase):
    def setUp(self):
        self.queries = []
        self.inventory = Inventory({
            "build": "postgresql://fixture",
            "publish": "postgresql://fixture",
            "runner": "postgresql://fixture",
        }, connections={
            name: lambda n=name: PreflightConnection(n, self.queries)
            for name in ("build", "publish", "runner")
        })

    def test_active_job_and_runner_lease_block_cleanup(self):
        result = self.inventory.cleanup_preview(ENV)
        self.assertTrue(result["found"])
        self.assertEqual(result["environment"]["envKey"], "env-key-a")
        self.assertEqual(result["aliasCount"], 2)
        self.assertEqual(result["activeBuildJobs"], 1)
        self.assertEqual(result["activeLeaseReferences"], 1)
        self.assertIn(RELEASE, result["releaseIds"])
        self.assertFalse(result["canExecute"])
        self.assertEqual(result["action"], "request_only")
        self.assertGreaterEqual(len(result["blockingReasons"]), 2)
        self.assertTrue(all(sql.lstrip().startswith("SELECT ")
                            for _, sql, _ in self.queries))
        self.assertNotIn("DELETE ", "\n".join(sql for _,sql,_ in self.queries))

    def test_invalid_id_cannot_be_used_in_sql(self):
        with self.assertRaises(ValueError):
            self.inventory.cleanup_preview("abc'; DELETE FROM build.runtime_environments")
        self.assertEqual(self.queries, [])

    def test_missing_cross_service_db_fails_closed(self):
        missing = Inventory({"build": "postgresql://fixture"}, connections={
            "build": lambda: PreflightConnection("build", []),
        })
        result = missing.cleanup_preview(ENV)
        self.assertFalse(result["canExecute"])
        self.assertTrue(any("Cannot verify" in reason
                            for reason in result["blockingReasons"]))


class FakeWriteConnection:
    """Simulated admin-owned DB transactions; no access to service tables."""
    def __init__(self, store):
        self.db = store
        self.result = None

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def execute(self, sql, args=()):
        self.db.statements.append(sql)
        assert not any(
            f"{schema}." in sql
            for schema in ("publish", "runner", "build")
        ), sql
        if "INSERT INTO console_ops.asset_notes" in sql:
            uid, kind, aid, note, actor, _ = args
            if aid in self.db.notes:
                self.result = None
            else:
                self.db.notes[aid] = {
                    "id": uid, "kind": kind, "note": note,
                    "actor": actor, "version": 1,
                }
                self.result = (uid,)
        elif "UPDATE console_ops.asset_notes" in sql:
            note, actor, uid, version = args
            obj = next(
                (v for v in self.db.notes.values() if v["id"] == uid), None
            )
            if not obj or obj["version"] != version:
                self.result = None
            else:
                obj["note"] = note
                obj["version"] += 1
                self.result = (obj["kind"],
                               next(k for k,v in self.db.notes.items() if v is obj),
                               obj["version"])
        elif "DELETE FROM console_ops.asset_notes" in sql:
            uid, version = args
            obj = next(
                ((k,v) for k,v in self.db.notes.items()
                 if v["id"]==uid and v["version"]==version), None
            )
            if not obj:
                self.result = None
            else:
                del self.db.notes[obj[0]]
                self.result = (obj[1]["kind"],obj[0])
        elif "INSERT INTO console_ops.audit_events" in sql:
            self.db.audit.append(args)
        elif "INSERT INTO console_ops.cleanup_requests" in sql:
            self.db.requests[args[0]] = args
        elif "UPDATE console_ops.cleanup_requests" in sql:
            self.result = (ENV,) if args[0] in self.db.requests else None
        elif "SELECT" in sql:
            self.result = []
        else:
            raise AssertionError(sql)
        return self

    def fetchone(self):
        return self.result

    def fetchall(self):
        return self.result if isinstance(self.result, list) else []


class FakeMetadata:
    def __init__(self):
        self.notes = {}
        self.requests = {}
        self.audit = []
        self.statements = []

    def connect(self):
        return FakeWriteConnection(self)


class TestIndependentSchemaRouting(unittest.TestCase):
    def test_inventory_and_preflight_use_three_real_schemas(self):
        from console.sql_schemas import render_sql, configured_schemas
        overrides={
            "publish":"publish_domain",
            "build":"build_domain",
            "runner":"runner_domain",
        }
        original_sql=[]
        normalized_sql=[]
        class QueryAdapter(PreflightConnection):
            def execute(self, sql, args=()):
                original_sql.append((self.service,sql,args))
                translated=sql
                for logical,physical in overrides.items():
                    translated=translated.replace(
                        f'"{physical}".',f"{logical}."
                    )
                # The fixture returns data only for the actual user DDL.
                return super().execute(translated,args)
        source=Inventory(
            {name:"postgresql://fixture" for name in overrides},
            connections={
                name:(lambda service=name: QueryAdapter(service,normalized_sql))
                for name in overrides
            },
            schemas=overrides,
        )
        source.report()
        preview=source.cleanup_preview(ENV)
        self.assertTrue(preview["found"])
        self.assertEqual(preview["aliasCount"],2)
        self.assertEqual(preview["activeLeaseReferences"],1)
        self.assertTrue(original_sql)
        seen={name:0 for name in overrides}
        for service,statement,params in original_sql:
            self.assertIn(f'"{overrides[service]}".',statement)
            self.assertNotIn(f"FROM {service}.",statement)
            seen[service]+=1
        self.assertTrue(all(count>0 for count in seen.values()),seen)

    def test_schema_formatter_keeps_sql_parameters_not_replacing_values(self):
        from console.sql_schemas import render_sql, configured_schemas
        names=configured_schemas({"build":"build_custom"})
        template="SELECT * FROM build.runtime_environments WHERE env_key=%s"
        sql=render_sql(template,names)
        self.assertEqual(
            sql,'SELECT * FROM "build_custom".runtime_environments WHERE env_key=%s'
        )
        self.assertNotIn("env_key='",sql)


class TestAdminOwnedCRUD(unittest.TestCase):
    def setUp(self):
        self.database = FakeMetadata()
        self.store = AdminStore("", connect=self.database.connect)

    def test_note_create_update_conflict_delete_and_audit(self):
        created = self.store.create_note(
            "operator", "build_environment", ENV, "test image expired"
        )
        self.assertEqual(created["version"], 1)
        with self.assertRaises(Conflict):
            self.store.create_note("operator", "build_environment", ENV, "duplicate")
        changed = self.store.update_note(
            "operator", created["id"], "rechecked missing image", 1
        )
        self.assertEqual(changed["version"], 2)
        with self.assertRaises(Conflict):
            self.store.update_note("operator", created["id"], "stale write", 1)
        self.assertTrue(self.store.delete_note(
            "operator", created["id"], 2
        )["deleted"])
        self.assertEqual(len(self.database.audit), 3)
        self.assertTrue(all("console_ops." in statement
                            for statement in self.database.statements))

    def test_cleanup_request_is_draft_only(self):
        preview = {
            "found": True, "environmentId": ENV, "status": "ready",
            "environment": {"envKey": "env-key-a", "id": ENV},
            "blockingReasons": ["Active Runner lease"],
            "warnings": [],
            "canExecute": False,
        }
        with self.assertRaises(ValueError):
            self.store.request_cleanup("operator", preview,
                                       "stale image in registry", "wrong-name")
        result = self.store.request_cleanup(
            "operator", preview,
            "stale image in registry", "env-key-a",
        )
        self.assertEqual(result["status"], "DRAFT")
        self.assertFalse(result["executed"])
        self.assertEqual(self.store.cancel_cleanup(
            "operator", result["id"])["status"], "CANCELLED")
        self.assertEqual(len(self.database.audit), 2)


if __name__ == "__main__":
    unittest.main()
