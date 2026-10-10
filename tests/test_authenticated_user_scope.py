"""API and service identity-scope regression tests without PostgreSQL/NiFi."""
from __future__ import annotations

import ast
import logging
import os
import tempfile
import unittest
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse
from fastapi.testclient import TestClient
from pydantic import BaseModel, Field

ROOT = Path(__file__).resolve().parents[1]


def source_tree(filename: str) -> ast.Module:
    path = ROOT / "publish_service" / filename
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def get_method(filename: str, class_name: str, method: str):
    tree = source_tree(filename)
    cls = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == class_name)
    return next(node for node in cls.body if isinstance(node, ast.FunctionDef) and node.name == method)


def load_function(filename: str, class_name: str, method: str, globals_: dict[str, Any]):
    method_node = get_method(filename, class_name, method)
    node = ast.fix_missing_locations(ast.Module(body=[
        ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0),
        ast.FunctionDef(name=method_node.name, args=method_node.args, body=method_node.body,
                        decorator_list=[], returns=method_node.returns, type_comment=None),
    ], type_ignores=[]))
    exec(compile(node, filename, "exec"), globals_)
    return globals_[method]


class FakeService:
    def __init__(self, path: Path):
        self.path = path
        self.path.write_bytes(b"bundle")
        self.last_user = None
        self.calls = []

    def edge_platforms(self):
        return {"platforms": []}

    def edge_deployments(self, *, user_id):
        return {"userId": user_id, "deployments": []}

    def edge_bundles(self, *, user_id, limit):
        return {"userId": user_id, "bundles": [], "limit": limit}

    def edge_operations(self, *, user_id, limit):
        return {"userId": user_id, "operations": [], "limit": limit}

    def edge_bundle_file(self, bundle_id, *, user_id):
        if user_id != "alice" or bundle_id != "alice-bundle":
            raise FakePublishError("bundle not found")
        return self.path

    def get_operator_list(self, run_type, user_id):
        return {"userId": user_id, "operators": []}

    def get_operator(self, operator_id, *, user_id):
        if user_id != "alice" or operator_id != "alice-operator":
            raise FakePublishError("operator not found")
        return {"userId": user_id}

    def compile_backend(self, operator_id, backend, options, *, user_id):
        self.calls.append(("compile", user_id, operator_id))
        if user_id != "alice" and operator_id == "alice-operator":
            raise FakePublishError("operator not found")
        return {"userId": user_id}

    def publish_backend(self, operator_id, backend, options, *, user_id):
        self.calls.append(("publish", user_id, operator_id))
        if user_id != "alice" and operator_id == "alice-operator":
            raise FakePublishError("operator not found")
        return {"userId": user_id}

    def create_virtual_contract(self, payload, *, user_id):
        return {"userId": user_id}

    def analyze(self, workspace, *, python_root=None):
        return {"workspace": workspace}


class FakePublishError(RuntimeError):
    pass


class UserScopeHttpTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.service = FakeService(Path(self.temp.name) / "bundle.zip")

        def get_current_user(request: Request):
            token = request.headers.get("Authorization")
            if token == "Bearer token-alice":
                return SimpleNamespace(user_id="alice")
            if token == "Bearer token-bob":
                return {"user_id": "bob"}
            if token == "Bearer token-bad-path":
                return {"user_id": "../bob"}
            raise HTTPException(status_code=401, detail="Invalid token")

        src = source_tree("app.py")
        needed = [node for node in src.body if isinstance(node, ast.FunctionDef)
                  and node.name in {"_user_id", "create_app"}]
        module = ast.fix_missing_locations(ast.Module(body=[
            ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0),
            *needed,
        ], type_ignores=[]))
        class BackendRequest(BaseModel):
            options: dict[str, Any] = Field(default_factory=dict)

        ctx = dict(
            Any=Any, FastAPI=FastAPI, Depends=Depends, HTTPException=HTTPException,
            BackendRequest=BackendRequest,
            Request=Request, FileResponse=FileResponse, Path=Path,
            logger=logging.getLogger("test"), os=os, get_current_user=get_current_user,
            PublishError=FakePublishError,
            EdgeDependencyConflictError=type("EdgeDependencyConflictError", (FakePublishError,), {}),
            asynccontextmanager=asynccontextmanager,
            install_publish_lifecycle_routes=lambda application: None,
        )
        exec(compile(module, "app.py", "exec"), ctx)
        self.client = TestClient(ctx["create_app"](service=self.service))
        self.client.__enter__()
        self.addCleanup(self.client.__exit__, None, None, None)

    def request(self, method, path, *, token=None, **kwargs):
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        return self.client.request(method, path, headers=headers, **kwargs)

    def test_missing_invalid_and_bogus_identity_are_rejected(self):
        for token in (None, "wrong-token", "token-bad-path"):
            for path in ("/v1/operators?run_type=edge", "/v1/edge/bundles", "/v1/edge/deployments",
                         "/v1/edge/operations", "/v1/edge/platforms", "/v1/operators/alice-operator"):
                response = self.request("GET", path, token=token)
                self.assertEqual(response.status_code, 401, (token, path, response.text))
        self.assertEqual(self.request("GET", "/health").status_code, 200)

    def test_user_specific_resource_scope(self):
        paths = ["/v1/edge/deployments", "/v1/edge/bundles", "/v1/edge/operations", "/v1/operators?run_type=edge"]
        for user, token in (("alice", "token-alice"), ("bob", "token-bob")):
            for path in paths:
                response = self.request("GET", path, token=token)
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.json()["userId"], user)
        self.assertEqual(self.request("GET", "/v1/operators/alice-operator", token="token-alice").status_code, 200)
        self.assertEqual(self.request("GET", "/v1/operators/alice-operator", token="token-bob").status_code, 404)
        self.assertEqual(self.request("GET", "/v1/edge/bundles/alice-bundle/download", token="token-alice").status_code, 200)
        self.assertEqual(self.request("GET", "/v1/edge/bundles/alice-bundle/download", token="token-bob").status_code, 404)

    def test_compile_publish_forward_authenticated_identity(self):
        route = "/v1/operators/alice-operator/backends/nifi_native/"
        for suffix in ("compile", "publish"):
            self.assertEqual(self.request("POST", route + suffix, token="token-alice", json={"options": {}}).status_code, 200)
            self.assertEqual(self.request("POST", route + suffix, token="token-bob", json={"options": {}}).status_code, 400 if suffix == "compile" else 409)
        self.assertEqual(self.service.calls, [
            ("compile", "alice", "alice-operator"), ("compile", "bob", "alice-operator"),
            ("publish", "alice", "alice-operator"), ("publish", "bob", "alice-operator"),
        ])

    def test_caller_supplied_user_id_is_ignored(self):
        response = self.request("GET", "/v1/operators?run_type=edge&user_id=alice", token="token-bob")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["userId"], "bob")


class ServiceOwnerEnforcementTests(unittest.TestCase):
    def test_all_store_methods_require_user_id_keyword(self):
        for method in ("save_virtual_contract", "get_operator", "get_latest_contract", "get_contract"):
            node = get_method("store.py", "PublishStore", method)
            keyword_args = [item.arg for item in node.args.kwonlyargs]
            self.assertIn("user_id", keyword_args, method)
            index = keyword_args.index("user_id")
            self.assertIsNone(node.args.kw_defaults[index], method)

    def test_service_no_default_identity_or_request_owner(self):
        text = "\n".join((ROOT / "publish_service" / name).read_text(encoding="utf-8")
                         for name in ("app.py", "service.py", "model.py", "store.py", "lifecycle_service.py", "schema.py"))
        self.assertNotIn("default_user_id", text)
        self.assertNotIn("PUBLISH_DEFAULT_USER_ID", text)
        self.assertNotIn("user_id: int = 1", text)
        self.assertNotIn("DEFAULT '1'", text)
        model = source_tree("model.py")
        cls = next(n for n in model.body if isinstance(n, ast.ClassDef) and n.name == "CreateVirtualContractRequest")
        self.assertFalse(any(isinstance(n, ast.AnnAssign) and isinstance(n.target, ast.Name) and n.target.id == "user_id" for n in cls.body))

    def test_native_bundle_must_take_explicit_user(self):
        for name in ("edge_deployments", "edge_bundles", "edge_operations", "edge_bundle_file",
                     "create_virtual_contract", "get_operator", "_load_current_parent", "_compile_backend",
                     "compile_backend", "publish_backend", "_publish_native"):
            node = get_method("service.py", "PublishService", name)
            keys = [k.arg for k in node.args.kwonlyargs]
            self.assertIn("user_id", keys, name)
            self.assertIsNone(node.args.kw_defaults[keys.index("user_id")], name)
        child = get_method("lifecycle_service.py", "LifecyclePublishService", "publish_backend")
        self.assertIn("user_id", [k.arg for k in child.args.kwonlyargs])

    def test_latest_contract_checks_owner_before_loading_source(self):
        observed = []
        class FakeStore:
            def get_latest_contract(self, operator_id, *, user_id):
                observed.append((operator_id, user_id))
                return None
        method = load_function("service.py", "PublishService", "_load_current_parent", {
            "PublishError": FakePublishError,
        })
        fake = SimpleNamespace(store=FakeStore())
        with self.assertRaisesRegex(FakePublishError, "operator or contract not found"):
            method(fake, "operator-alice", user_id="bob")
        self.assertEqual(observed, [("operator-alice", "bob")])
        with self.assertRaises(TypeError):
            method(fake, "operator-alice")

    def test_schema_drops_old_defaults(self):
        schema = (ROOT / "publish_service/schema.py").read_text(encoding="utf-8")
        self.assertIn("ALTER COLUMN user_id DROP DEFAULT", schema)
        migration = (ROOT / "sql/20261008_remove_default_publish_user.sql").read_text(encoding="utf-8")
        self.assertEqual(migration.count("ALTER COLUMN user_id DROP DEFAULT"), 2)


if __name__ == "__main__":
    unittest.main()
