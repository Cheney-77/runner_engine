"""Multi-edge regression tests; runnable with the Python standard library only."""

from __future__ import annotations

import ast
import hashlib
import importlib.util
import json
import shutil
import tempfile
import unittest
import uuid
import zipfile
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Iterator

ROOT = Path(__file__).resolve().parents[1]


def load_identity_module():
    location = ROOT / "publish_service/edge_identity.py"
    spec = importlib.util.spec_from_file_location("edge_identity_under_test", location)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


IDENTITY = load_identity_module()


def load_ast_definition(source: Path, name: str, context: dict):
    """Load a single definition to test code without requiring deployment-only packages."""
    tree = ast.parse(source.read_text(encoding="utf-8"), filename=str(source))
    definition = next(item for item in tree.body if getattr(item, "name", None) == name)
    module = ast.Module(
        body=[ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0), definition],
        type_ignores=[],
    )
    module = ast.fix_missing_locations(module)
    exec(compile(module, str(source), "exec"), context)
    return context[name]


class FakeConnection:
    def __init__(self):
        self.calls = []
        self.autocommit = False

    def execute(self, sql, params=None):
        self.calls.append((sql, tuple(params) if params is not None else ()))
        return self

    def fetchall(self):
        return []

    def fetchone(self):
        return {"next_revision": 7}

    @contextmanager
    def transaction(self):
        yield self


class FakePool:
    def __init__(self, conn):
        self.conn = conn

    @contextmanager
    def connection(self):
        yield self.conn


class MultiEdgeIdentityTests(unittest.TestCase):
    def test_identity_validation(self):
        raw = {"tokenPair": " pair-1 ", "edgeName": " edge-A "}
        self.assertEqual(IDENTITY.normalize_edge_identity(raw), {"tokenPair": "pair-1", "edgeName": "edge-A"})
        self.assertEqual(IDENTITY.normalize_edge_identity({"token_pair": "id", "edge_name": "edge"})["edgeName"], "edge")
        invalid = [
            None, [], {}, {"tokenPair": "x"}, {"tokenPair": "", "edgeName": "x"},
            {"tokenPair": "x", "edgeName": "\n"}, {"tokenPair": "x" * 129, "edgeName": "y"},
        ]
        for value in invalid:
            with self.subTest(value=value), self.assertRaises(ValueError):
                IDENTITY.normalize_edge_identity(value)

    def test_device_fingerprint(self):
        one = IDENTITY.edge_directory_name("pair-a", "edge-1")
        self.assertEqual(one, IDENTITY.edge_directory_name("pair-a", "edge-1"))
        self.assertNotEqual(one, IDENTITY.edge_directory_name("pair-b", "edge-1"))
        self.assertNotEqual(one, IDENTITY.edge_directory_name("pair-a", "edge-2"))
        self.assertTrue(one.startswith("edge-"))
        self.assertEqual(len(one), 25)
        self.assertNotIn("/", IDENTITY.edge_directory_name("a/../b", "边缘一号"))

    def test_variant_key_includes_device_identity(self):
        class Result(SimpleNamespace):
            def model_dump(self, **kwargs):
                return dict(self.__dict__)

        def key(**kwargs):
            return hashlib.sha256(json.dumps(kwargs, sort_keys=True, ensure_ascii=False).encode()).hexdigest()

        def compile_source(**kwargs):
            fake_target = Result(
                os="linux", arch="aarch64", python_version="3.12",
                uv_python_platform="aarch64-unknown-linux-gnu",
            )
            ctx = {
                "read_requirements": lambda path: [],
                "normalize_edge_identity": IDENTITY.normalize_edge_identity,
                "normalize_target_platform": lambda *args, **kw: fake_target,
                "backend_variant_key": key,
                "COMPILER_VERSION": "nifi-native-contract-v2",
                "NifiNativeBackendContract": Result,
            }
            compile_func = load_ast_definition(
                ROOT / "publish_service/backends/nifi_native.py", "compile_nifi_native_contract", ctx
            )
            return compile_func(
                contract_id="contract-1", contract_version=1,
                parent=Result(metadata=Result(name="demo")), plan=Result(contract_sha256="abcd", parameters=[]),
                source_path=Path("."), options={"className": "Demo", "packageName": "demo", **kwargs},
            )

        first = compile_source(edgeIdentity={"tokenPair": "pair-1", "edgeName": "A"})
        again = compile_source(edgeIdentity={"tokenPair": "pair-1", "edgeName": "A"})
        other = compile_source(edgeIdentity={"tokenPair": "pair-2", "edgeName": "A"})
        self.assertEqual(first.variant_key, again.variant_key)
        self.assertNotEqual(first.variant_key, other.variant_key)
        self.assertEqual(first.backend_options["edgeIdentity"], {"tokenPair": "pair-1", "edgeName": "A"})
        with self.assertRaises(ValueError):
            compile_source()

    def test_store_scope_revision_and_advisory_lock(self):
        ctx = {"contextmanager": contextmanager, "json": json, "uuid": uuid, "Jsonb": lambda x: x}
        Store = load_ast_definition(ROOT / "publish_service/store.py", "PublishStore", ctx)
        store = object.__new__(Store)
        conn = FakeConnection()
        store.pool = FakePool(conn)
        machine = {
            "user_id": "alice", "target_os": "linux", "target_arch": "aarch64",
            "python_version": "3.12", "token_pair": "pair-01", "edge_name": "edge-A",
        }

        store.list_edge_deployments(**machine, exclude_operator_id="operator-1", conn=conn)
        sql, params = conn.calls[-1]
        self.assertIn("token_pair = %s", sql)
        self.assertIn("edge_name = %s", sql)
        self.assertEqual(params, ("alice", "linux", "aarch64", "3.12", "pair-01", "edge-A", "operator-1"))

        self.assertEqual(store.next_edge_bundle_revision(**machine, conn=conn), 7)
        sql, params = conn.calls[-1]
        self.assertIn("token_pair = %s", sql)
        self.assertEqual(params[-2:], ("pair-01", "edge-A"))

        with store.edge_publish_lock(**machine) as active:
            self.assertIs(active, conn)
        lock, unlock = conn.calls[-2:]
        self.assertEqual(lock[1], unlock[1])
        self.assertIn("pair-01", lock[1][0])
        self.assertFalse(conn.autocommit)

        store.get_operator_list("edge", "alice")
        sql, params = conn.calls[-1]
        self.assertIn("DISTINCT ON (o.id, target_platform, edge_identity)", sql)
        self.assertEqual(params, ("alice",))

    def test_commit_sql_placeholder_count_and_device_keys(self):
        ctx = {"contextmanager": contextmanager, "json": json, "uuid": uuid, "Jsonb": lambda x: x}
        Store = load_ast_definition(ROOT / "publish_service/store.py", "PublishStore", ctx)
        store = object.__new__(Store)
        conn = FakeConnection()
        kwargs = dict(
            user_id="alice", operator_id="operator-1", variant_id="variant-1",
            target_os="linux", target_arch="aarch64", python_version="3.12",
            token_pair="pair-A", edge_name="edge-one", uv_python_platform="aarch64-unknown-linux-gnu",
            package_name="mypkg", native_artifact_file="/tmp/native.zip",
            candidate_requirements=["numpy==2"], bundle_id="bundle-1", job_id="job-1",
            published_result={}, bundle_revision=1, bundle_requirements=["numpy==2"],
            requirements_lock="lock", lock_sha256="abcd",
            bundle_artifact_ref="edge-native-bundle:artifact.zip",
            bundle_artifact_sha256="efgh", manifest={}, members=[], conn=conn,
        )
        self.assertEqual(store.commit_edge_publish(**kwargs), "bundle-1")
        deploy_sql, deploy_params = conn.calls[0]
        bundle_sql, bundle_params = conn.calls[1]
        self.assertEqual(deploy_sql.count("%s"), len(deploy_params))
        self.assertEqual(bundle_sql.count("%s"), len(bundle_params))
        self.assertIn("token_pair, edge_name", deploy_sql)
        self.assertIn("token_pair, edge_name", bundle_sql)
        self.assertEqual(deploy_params[7:9], ("pair-A", "edge-one"))
        self.assertEqual(bundle_params[5:7], ("pair-A", "edge-one"))

    def test_bundle_download_keeps_legacy_and_machine_paths(self):
        ctx = {"Path": Path, "Any": Any, "edge_directory_name": IDENTITY.edge_directory_name, "PublishError": RuntimeError}
        Service = load_ast_definition(ROOT / "publish_service/service.py", "PublishService", ctx)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            service = object.__new__(Service)
            service.settings = SimpleNamespace(edge_bundle_root=root)
            row = {
                "user_id": "alice", "target_os": "linux", "target_arch": "aarch64",
                "python_version": "3.12", "artifact_ref": "edge-native-bundle:result.zip",
                "token_pair": None, "edge_name": None,
            }
            service.store = SimpleNamespace(get_edge_bundle=lambda **kwargs: row)
            legacy = root / "user-alice" / "linux-aarch64-py3.12" / "result.zip"
            legacy.parent.mkdir(parents=True)
            legacy.write_bytes(b"legacy")
            self.assertEqual(service.edge_bundle_file("bundle-legacy", user_id="alice"), legacy.resolve())

            row.update(token_pair="pair-1", edge_name="device-A")
            modern = legacy.parent / IDENTITY.edge_directory_name("pair-1", "device-A") / "result.zip"
            modern.parent.mkdir(parents=True)
            modern.write_bytes(b"modern")
            self.assertEqual(service.edge_bundle_file("bundle-modern", user_id="alice"), modern.resolve())
            row["edge_name"] = "device-B"
            with self.assertRaises(RuntimeError):
                service.edge_bundle_file("bundle-wrong-machine", user_id="alice")

    def test_bundle_isolation_and_manifest(self):
        ctx = {
            "Path": Path, "hashlib": hashlib, "json": json, "shutil": shutil,
            "tempfile": tempfile, "zipfile": zipfile, "Any": Any,
            "edge_directory_name": IDENTITY.edge_directory_name,
            "canonical_json": lambda data: json.dumps(
                data, ensure_ascii=False, sort_keys=True, separators=(",", ":")
            ).encode(),
            "_file_hashes": lambda p: ("sha", "md5"),
        }
        build = load_ast_definition(ROOT / "publish_service/edge_bundle.py", "build_edge_bundle", ctx)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            native = root / "native.zip"
            native.write_bytes(b"fake native")
            member = {
                "operator_id": "op-a", "variant_id": "var-a", "package_name": "pkg",
                "artifact_file": str(native), "requirements": [],
            }
            target = SimpleNamespace(os="linux", arch="aarch64", python_version="3.12", uv_python_platform="aarch64-linux")
            resolution = SimpleNamespace(direct_requirements=[], lock_text="", lock_sha256="sha", package_count=0)
            resolver = SimpleNamespace(materialize=lambda *args, **kwargs: None)
            build_args = dict(
                output_root=root / "bundles", user_id="alice", target=target,
                revision=1, members=[member], resolution=resolution, resolver=resolver,
            )
            file_a, _, _, manifest_a = build(token_pair="p1", edge_name="A", **build_args)
            file_b, _, _, manifest_b = build(token_pair="p2", edge_name="B", **build_args)
            self.assertNotEqual(file_a.parent, file_b.parent)
            self.assertNotEqual(file_a.name, file_b.name)
            self.assertEqual(manifest_a["edgeIdentity"], {"tokenPair": "p1", "edgeName": "A"})
            self.assertEqual(manifest_b["edgeIdentity"], {"tokenPair": "p2", "edgeName": "B"})
            with zipfile.ZipFile(file_a) as archive:
                archived = json.loads(archive.read("edge-bundle/manifest.json"))
            self.assertEqual(archived["edgeIdentity"], {"tokenPair": "p1", "edgeName": "A"})


if __name__ == "__main__":
    unittest.main()
