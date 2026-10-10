"""The operations API exposes device identity without a separate device registry."""

from __future__ import annotations

import ast
import unittest
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]


class OperationIdentityTests(unittest.TestCase):
    def test_operations_response_has_device_fields_and_legacy_fallback(self):
        source = ast.parse((ROOT / "publish_service/service.py").read_text(encoding="utf-8"))
        service = next(node for node in source.body if isinstance(node, ast.ClassDef) and node.name == "PublishService")
        method = next(node for node in service.body if isinstance(node, ast.FunctionDef) and node.name == "edge_operations")
        function = ast.FunctionDef(name="edge_operations", args=method.args, body=method.body,
                                   decorator_list=[], returns=method.returns, type_comment=None)
        module = ast.fix_missing_locations(ast.Module(body=[
            ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0), function,
        ], type_ignores=[]))
        namespace = {}
        exec(compile(module, str(ROOT / "publish_service/service.py"), "exec"), namespace)

        base = dict(job_id="job", job_status="READY", variant_id="variant", variant_status="PUBLISHED",
                    operator_id="operator", workspace="work", name="name", display_name="Name",
                    artifact_ref=None, published_metadata=None, error_message=None, created_at=None,
                    started_at=None, finished_at=None)
        rows = [
            {**base, "options_json": {"edgeIdentity": {"tokenPair": "pair-A", "edgeName": "edge-A"}}},
            {**base, "options_json": {"targetPlatform": {"os": "linux", "arch": "x86_64"}}},
        ]
        fake = SimpleNamespace(store=SimpleNamespace(
            list_edge_operations=lambda **kwargs: rows))
        data = namespace["edge_operations"](fake, user_id="alice", limit=2)
        self.assertEqual(data["operations"][0]["edgeIdentity"], {"tokenPair": "pair-A", "edgeName": "edge-A"})
        self.assertIsNone(data["operations"][1]["edgeIdentity"])
        self.assertEqual(data["userId"], "alice")
        self.assertEqual(len(data["operations"]), 2)


if __name__ == "__main__":
    unittest.main()
