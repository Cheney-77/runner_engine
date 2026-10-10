"""No-DB integration tests for Native Bundle reuse and per-device invalidation."""

from __future__ import annotations

import ast
import hashlib
import logging
import tempfile
import threading
import unittest
import uuid
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]


def load_service(context):
    path = ROOT / "publish_service/service.py"
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    service = next(item for item in tree.body if isinstance(item, ast.ClassDef) and item.name == "PublishService")
    module = ast.fix_missing_locations(ast.Module(body=[
        ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0), service,
    ], type_ignores=[]))
    exec(compile(module, str(path), "exec"), context)
    return context["PublishService"]


def hashes(path):
    data = path.read_bytes()
    return hashlib.sha256(data).hexdigest(), hashlib.md5(data).hexdigest()


def directory_name(token_pair, edge_name):
    import json
    text = json.dumps([token_pair, edge_name], ensure_ascii=False, separators=(",", ":"))
    return "edge-" + hashlib.sha256(text.encode("utf-8")).hexdigest()[:20]


class FakeStore:
    def __init__(self):
        self.deployments = {}
        self.bundles = {}
        self.lock = threading.Lock()
        self.publish_commits = []
        self.reused_jobs = []
        self.revision_queries = 0
        self.locks = []

    @staticmethod
    def key(device):
        return tuple(str(device[key]) for key in (
            "user_id", "target_os", "target_arch", "python_version", "token_pair", "edge_name"))

    @contextmanager
    def edge_publish_lock(self, **device):
        with self.lock:
            self.locks.append(self.key(device))
            yield self

    def list_edge_deployments(self, *, conn, **device):
        return list(self.deployments.get(self.key(device), {}).values())

    def get_latest_edge_bundle(self, *, conn, **device):
        records = self.bundles.get(self.key(device), [])
        return records[-1] if records else None

    def list_edge_bundle_members(self, *, bundle_id, conn):
        for records in self.bundles.values():
            for record in records:
                if record["id"] == bundle_id:
                    return record["members"]
        raise AssertionError(f"bundle not found: {bundle_id}")

    def next_edge_bundle_revision(self, *, conn, **device):
        self.revision_queries += 1
        return len(self.bundles.get(self.key(device), [])) + 1

    def mark_job_ready(self, job_id, variant_id, *, artifact_ref, result, conn):
        self.reused_jobs.append((job_id, variant_id, artifact_ref, result))

    def commit_edge_publish(self, **kwargs):
        self.publish_commits.append(kwargs)
        device = {key: kwargs[key] for key in (
            "user_id", "target_os", "target_arch", "python_version", "token_pair", "edge_name")}
        key = self.key(device)
        self.deployments.setdefault(key, {})[kwargs["operator_id"]] = {
            "operator_id": kwargs["operator_id"],
            "variant_id": kwargs["variant_id"],
            "package_name": kwargs["package_name"],
            "artifact_file": kwargs["native_artifact_file"],
            "requirements_json": kwargs["candidate_requirements"],
        }
        self.bundles.setdefault(key, []).append({
            "id": kwargs["bundle_id"], "user_id": kwargs["user_id"],
            "target_os": kwargs["target_os"], "target_arch": kwargs["target_arch"],
            "python_version": kwargs["python_version"], "uv_python_platform": kwargs["uv_python_platform"],
            "token_pair": kwargs["token_pair"], "edge_name": kwargs["edge_name"],
            "revision": kwargs["bundle_revision"], "artifact_ref": kwargs["bundle_artifact_ref"],
            "artifact_sha256": kwargs["bundle_artifact_sha256"],
            "lock_sha256": kwargs["lock_sha256"], "manifest_json": kwargs["manifest"],
            "members": [dict(item, requirements_json=item["requirements"]) for item in kwargs["members"]],
        })


class NativeBundleReuseTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="test-edge-bundle-reuse-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.writes = 0
        self.bundles_built = 0
        self.resolved = 0
        self.store = FakeStore()

        def native_write(root, *, contract, plan, source_path):
            self.writes += 1
            root.mkdir(parents=True, exist_ok=True)
            result = root / f"{contract.package_name}-{contract.variant_key[:12]}.zip"
            result.write_bytes(contract.variant_key.encode())
            return result

        def bundle_build(output_root, *, user_id, target, token_pair, edge_name,
                         revision, members, resolution, resolver):
            self.bundles_built += 1
            output = output_root / f"user-{user_id}" / f"{target.os}-{target.arch}-py{target.python_version}"
            output /= directory_name(token_pair, edge_name)
            output.mkdir(parents=True, exist_ok=True)
            file = output / f"bundle-r{revision:06d}.zip"
            file.write_bytes(repr((revision, members)).encode())
            manifest = {"dependencyPackageCount": resolution.package_count,
                        "processors": [{"operatorId": m["operator_id"]} for m in members]}
            return file, *hashes(file), manifest

        self.service_cls = load_service({
            "Path": Path, "uuid": uuid, "logger": logging.getLogger(__name__),
            "PublishError": RuntimeError, "EdgeDependencyResolutionError": type("DepErr", (Exception,), {}),
            "EdgeDependencyConflictError": RuntimeError,
            "edge_directory_name": directory_name, "_file_hashes": hashes,
            "write_native_package": native_write, "build_edge_bundle": bundle_build,
        })
        self.service = object.__new__(self.service_cls)
        self.service.settings = SimpleNamespace(native_artifact_root=self.root / "native",
                                                edge_bundle_root=self.root / "bundles")
        self.service.store = self.store
        def resolve(requirements, *, target):
            self.resolved += 1
            return SimpleNamespace(direct_requirements=requirements, lock_text="locked", lock_sha256="lock-sha",
                                   package_count=len(set(requirements)))
        self.service.edge_resolver = SimpleNamespace(resolve=resolve)
        self.target = SimpleNamespace(os="linux", arch="aarch64", python_version="3.12",
                                      uv_python_platform="aarch64-unknown-linux-gnu")

    def publish(self, operator="operator-1", variant="variant-1", device="machine-A",
                target=None, force=False, requirements=("numpy==2",), user_id="alice"):
        target = target or self.target
        child = SimpleNamespace(target_platform=target,
                                backend_options={"edgeIdentity": {"tokenPair": "pair-01", "edgeName": device}},
                                package_name=f"pkg_{operator}", class_name="Processor", processor_type="NiFiType",
                                deployment_mode="full", requirements=requirements, variant_key=variant)
        return self.service._publish_native(operator, f"job-{uuid.uuid4()}", {"id": variant}, child, {}, self.root,
                                            user_id=user_id, force_rebuild=force)[0]

    def test_repeat_uses_same_bundle_without_resolve_or_repackage(self):
        first = self.publish()
        repeated = self.publish()
        self.assertFalse(first["reusedExistingBundle"])
        self.assertTrue(repeated["reusedExistingBundle"])
        self.assertEqual(first["edgeBundle"]["bundleId"], repeated["edgeBundle"]["bundleId"])
        self.assertEqual(first["edgeBundle"]["revision"], repeated["edgeBundle"]["revision"])
        self.assertEqual(first["edgeBundle"]["artifactRef"], repeated["edgeBundle"]["artifactRef"])
        self.assertEqual((self.writes, self.bundles_built, self.resolved, self.store.revision_queries), (1, 1, 1, 1))
        self.assertEqual(len(self.store.publish_commits), 1)
        self.assertEqual(len(self.store.reused_jobs), 1)
        self.assertTrue(repeated["deploymentRequired"], "Cache reuse is not proof the device has installed the bundle")

    def test_variant_or_requirements_change_rebuilds(self):
        self.publish()
        second = self.publish(variant="variant-2")
        self.assertEqual(second["edgeBundle"]["revision"], 2)
        self.assertFalse(second["reusedExistingBundle"])
        third = self.publish(variant="variant-2", requirements=("numpy==3",))
        self.assertEqual(third["edgeBundle"]["revision"], 3)
        self.assertEqual(self.bundles_built, 3)

    def test_other_operator_update_return_latest_environment_bundle(self):
        self.publish(operator="a", variant="a-v1")
        other = self.publish(operator="b", variant="b-v1")
        reused_a = self.publish(operator="a", variant="a-v1")
        self.assertTrue(reused_a["reusedExistingBundle"])
        self.assertEqual(reused_a["edgeBundle"]["bundleId"], other["edgeBundle"]["bundleId"])
        self.assertEqual((self.writes, self.resolved, self.bundles_built), (2, 2, 2))
        changed_b = self.publish(operator="b", variant="b-v2")
        self.assertEqual(changed_b["edgeBundle"]["revision"], 3)
        self.assertEqual(self.publish(operator="a", variant="a-v1")["edgeBundle"]["revision"], 3)
        self.assertEqual(self.bundles_built, 3)

    def test_separate_device_and_platform_have_independent_revisions(self):
        self.publish()
        other_device = self.publish(device="machine-B")
        different_target = SimpleNamespace(os="windows", arch="x86_64", python_version="3.12",
                                           uv_python_platform="x86_64-pc-windows-msvc")
        other_platform = self.publish(target=different_target)
        self.assertEqual(other_device["edgeBundle"]["revision"], 1)
        self.assertEqual(other_platform["edgeBundle"]["revision"], 1)
        self.assertEqual(self.publish()["edgeBundle"]["revision"], 1)
        self.assertEqual(self.bundles_built, 3)
        self.assertEqual(len(set(self.store.locks)), 3)

    def test_same_machine_different_users_never_share_a_bundle(self):
        alice = self.publish(user_id="alice")
        bob = self.publish(user_id="bob")
        again = self.publish(user_id="alice")
        self.assertEqual(alice["edgeBundle"]["revision"], 1)
        self.assertEqual(bob["edgeBundle"]["revision"], 1)
        self.assertNotEqual(alice["edgeBundle"]["bundleId"], bob["edgeBundle"]["bundleId"])
        self.assertTrue(again["reusedExistingBundle"])
        self.assertEqual(again["edgeBundle"]["bundleId"], alice["edgeBundle"]["bundleId"])
        self.assertEqual(self.bundles_built, 2)

    def test_missing_and_corrupted_bundle_rebuild(self):
        initial = self.publish()
        file = Path(initial["edgeBundle"]["artifactFile"])
        file.write_bytes(b"tampered")
        after_tamper = self.publish()
        self.assertEqual(after_tamper["edgeBundle"]["revision"], 2)
        Path(after_tamper["edgeBundle"]["artifactFile"]).unlink()
        after_missing = self.publish()
        self.assertEqual(after_missing["edgeBundle"]["revision"], 3)
        self.assertEqual(self.bundles_built, 3)

    def test_missing_native_artifact_or_membership_mismatch_rebuilds(self):
        initial = self.publish()
        Path(initial["artifactFile"]).unlink()
        regenerated = self.publish()
        self.assertEqual(regenerated["edgeBundle"]["revision"], 2)
        scope = self.store.locks[-1]
        self.store.bundles[scope][-1]["members"][0]["requirements_json"] = ["broken==0"]
        next_result = self.publish()
        self.assertEqual(next_result["edgeBundle"]["revision"], 3)

    def test_force_rebuild_creates_new_revision_even_if_unchanged(self):
        self.publish()
        forced = self.publish(force=True)
        self.assertEqual(forced["edgeBundle"]["revision"], 2)
        self.assertFalse(forced["reusedExistingBundle"])
        self.assertTrue(self.publish()["reusedExistingBundle"])
        self.assertEqual(self.bundles_built, 2)

    def test_two_concurrent_publish_calls_share_one_bundle(self):
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: self.publish(), range(2)))
        self.assertEqual(results[0]["edgeBundle"]["bundleId"], results[1]["edgeBundle"]["bundleId"])
        self.assertEqual([item["reusedExistingBundle"] for item in results].count(True), 1)
        self.assertEqual(self.bundles_built, 1)
        self.assertEqual(self.store.revision_queries, 1)

    def test_publish_backend_force_flag_is_not_accidentally_truthy(self):
        self.service._compile_backend = lambda op, backend, options, *, user_id: (
            {"id": "var-1"}, SimpleNamespace(), None, None,
        )
        self.store.create_publish_job = lambda variant_id: {"id": "job-1"}
        self.store.mark_job_running = lambda job_id: None
        seen = []

        def native(*args, **kwargs):
            self.assertEqual(kwargs["user_id"], "alice")
            seen.append(kwargs.get("force_rebuild"))
            return {"reusedExistingBundle": False}, "edge-native-bundle:bundle.zip"

        self.service._publish_native = native
        for flag in (None, False, True, "true"):
            options = {} if flag is None else {"forceRebuild": flag}
            response = self.service.publish_backend("operator-1", "nifi_native", options, user_id="alice")
            self.assertEqual(response["status"], "READY")
        self.assertEqual(seen, [False, False, True, False])

    def test_broken_artifact_ref_falls_back_to_build(self):
        self.publish()
        self.store.bundles[self.store.locks[-1]][-1]["artifact_ref"] = "edge-native-bundle:../path.zip"
        repaired = self.publish()
        self.assertFalse(repaired["reusedExistingBundle"])
        self.assertEqual(repaired["edgeBundle"]["revision"], 2)


class SqlConsistencyTests(unittest.TestCase):
    def test_six_fields_scope_and_atomic_reuse_job(self):
        source = (ROOT / "publish_service/store.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        store = next(item for item in tree.body if isinstance(item, ast.ClassDef) and item.name == "PublishStore")
        names = {item.name: item for item in store.body if isinstance(item, ast.FunctionDef)}
        get_sql = ast.get_source_segment(source, names["get_latest_edge_bundle"])
        self.assertIn("token_pair = %s AND edge_name = %s", get_sql)
        self.assertIn("ORDER BY revision DESC LIMIT 1", get_sql)
        self.assertIn("conn.execute(sql, params).fetchone()", get_sql)
        self.assertIn("FROM publish.edge_bundle_members WHERE bundle_id = %s",
                      ast.get_source_segment(source, names["list_edge_bundle_members"]))
        mark_sql = ast.get_source_segment(source, names["mark_job_ready"])
        self.assertIn("with conn.transaction():", mark_sql)
        self.assertIn("update(conn)", mark_sql)


if __name__ == "__main__":
    unittest.main()
