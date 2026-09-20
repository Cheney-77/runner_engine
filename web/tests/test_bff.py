"""HTTP-level tests against a TEST-ONLY fake Publish Service.

No test claims that the real user deployment has been contacted.
Production uses the configured real Publish Service URL.
"""
from __future__ import annotations

import json
import unittest

import httpx

from app.gateway import PublishGateway, Settings
from app.main import app


# Known top-level CreateVirtualContractRequest fields from supplied Python
# source; nested model examples below are TEST FIXTURES, NOT user model claims.
MOCK_OPENAPI = {
    "openapi": "3.1.0",
    "paths": {
        "/v1/operators": {
            "post": {
                "requestBody": {
                    "content": {
                        "application/json": {
                            "schema": {
                                "$ref": "#/components/schemas/CreateVirtualContractRequest"
                            }
                        }
                    }
                }
            }
        }
    },
    "components": {
        "schemas": {
            "CreateVirtualContractRequest": {
                "type": "object",
                "required": [
                    "workspace", "source_revision", "python_root",
                    "operator", "callable_id",
                ],
                "properties": {
                    "workspace": {"type": "string"},
                    "source_revision": {"type": "string"},
                    "python_root": {"type": "string"},
                    "operator": {"$ref": "#/components/schemas/OperatorSelection"},
                    "callable_id": {"type": "string"},
                    "constructor_bindings": {
                        "type": "object",
                        "additionalProperties": {
                            "$ref": "#/components/schemas/BindingSelection"
                        },
                    },
                    "argument_bindings": {
                        "type": "object",
                        "additionalProperties": {
                            "$ref": "#/components/schemas/BindingSelection"
                        },
                    },
                    "output": {"$ref": "#/components/schemas/OutputSelection"},
                },
            },
            "OperatorSelection": {
                "type": "object",
                "properties": {"name": {"type": "string"}},
                "required": ["name"],
            },
            "BindingSelection": {
                "type": "object",
                "properties": {
                    "source": {"type": "string"},
                    "value": {"type": "string"},
                },
                "required": ["source"],
            },
            "OutputSelection": {
                "type": "object",
                "properties": {"format": {"type": "string"}},
            },
        }
    },
}

MOCK_ANALYZE = {
    "workspace": "demo",
    "sourceRevision": "revision-001",
    "pythonRoot": "src",
    "pythonRootCandidates": [
        {"path": "src", "recommended": True},
        {"path": ".", "recommended": False},
    ],
    "recommendedCallableId": "src/ops.py:process",
    "callables": [{
        "id": "src/ops.py:process",
        "kind": "function",
        "displayName": "process",
        "file": "src/ops.py",
        "line": 8,
        "supported": True,
        "unsupportedReasons": [],
        "constructorParameters": [],
        "parameters": [{
            "name": "payload",
            "kind": "positional",
            "annotation": "str",
            "required": True,
            "hasLiteralDefault": False,
            "defaultValue": None,
            "defaultExpression": None,
            "allowedSources": [
                "input.payload", "input.metadata",
                "operator.parameter", "constant",
            ],
            "suggestions": [],
        }],
        "returnAnnotation": "str",
        "score": 1.0,
    }],
    "warnings": [],
}
MOCK_CREATE_BODY = {
    "workspace": "demo",
    "source_revision": "revision-001",
    "python_root": "src",
    "operator": {"name": "demo_processor"},
    "callable_id": "src/ops.py:process",
    "constructor_bindings": {},
    "argument_bindings": {"payload": {"source": "input.payload"}},
    "output": {},
}


class TestPublishOnlyBFF(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.calls = []
        self.fail_connection = False
        self.source_changed = False
        self.previous = app.state.gateway

        def handle(request: httpx.Request) -> httpx.Response:
            if self.fail_connection:
                raise httpx.ConnectError("test-only refused", request=request)
            assert request.url.host == "publish.test", "BFF must NEVER call Build"
            body = json.loads(request.content) if request.content else None
            key = (request.method, request.url.path)
            self.calls.append((key, body))

            if key == ("GET", "/health"):
                return httpx.Response(200, json={
                    "ok": True,
                    "version": "3.3.0",
                    "api": "virtual-contract-v1",
                    "backends": ["runner", "nifi_native"],
                    "compiledPlanArtifact": False,
                })
            if key == ("GET", "/openapi.json"):
                return httpx.Response(200, json=MOCK_OPENAPI)
            if key == ("POST", "/v1/authoring/analyze"):
                return httpx.Response(200, json={
                    **MOCK_ANALYZE,
                    "received": body,
                })
            if key == ("POST", "/v1/operators"):
                if self.source_changed:
                    return httpx.Response(400, json={
                        "detail": "SOURCE_CHANGED: workspace changed after Analyze"
                    })
                return httpx.Response(200, json={
                    "created": True,
                    "operatorId": "op_123",
                    "contractId": "contract_123",
                    "contractVersion": 1,
                    "contractSha256": "sha",
                    "sourceRevision": "revision-001",
                    "sourceRef": "immutable-source",
                    "virtualContract": {},
                    "derived": {
                        "parameters": [],
                        "inputMetadata": {},
                        "outputMetadata": {},
                    },
                    "received": body,
                })
            if key == ("GET", "/v1/operators/op_123"):
                return httpx.Response(200, json={
                    "operatorId": "op_123",
                    "workspace": "demo",
                    "currentContract": {
                        "contractId": "contract_123",
                        "version": 1,
                        "sourceRevision": "revision-001",
                        "sourceRef": "immutable-source",
                        "virtualContract": {},
                    },
                    "backendVariants": [],
                })
            if key == ("POST", "/v1/operators/op_123/backends/runner/compile"):
                return httpx.Response(200, json={
                    "variantId": "runner_variant",
                    "backend": "runner",
                    "variantKey": "key",
                    "backendContractSha256": "abc",
                    "received": body,
                })
            if key == ("POST", "/v1/operators/op_123/backends/nifi_native/compile"):
                return httpx.Response(200, json={
                    "variantId": "native_variant",
                    "backend": "nifi_native",
                    "received": body,
                })
            if key == ("POST", "/v1/operators/op_123/backends/runner/publish"):
                if body == {"options": {"error": True}}:
                    return httpx.Response(409, json={
                        "detail": "RUNTIME_NOT_READY env_key=demo status=BUILDING"
                    })
                return httpx.Response(200, json={
                    "jobId": "job_runner",
                    "status": "READY",
                    "backend": "runner",
                    "artifactRef": "runner-release:release_1",
                    "result": {
                        "releaseId": "release_1",
                        "runtimeImage": "runtime:test",
                        "envKey": "env_test",
                    },
                })
            if key == ("POST", "/v1/operators/op_123/backends/nifi_native/publish"):
                return httpx.Response(200, json={
                    "jobId": "job_native",
                    "status": "READY",
                    "backend": "nifi_native",
                    "artifactRef": "nifi-native-package:sample.zip",
                    "result": {
                        "packageName": "sample",
                        "artifactFile": "/safe/test-only/sample.zip",
                        "deploymentRequired": True,
                        "deploymentHint": "Deploy to configured extension source directory.",
                    },
                })
            return httpx.Response(404, json={"detail": "operator not found"})

        app.state.gateway = PublishGateway(
            Settings(publish_url="http://publish.test"),
            transport=httpx.MockTransport(handle),
        )
        self.client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://bff.test",
        )

    async def asyncTearDown(self):
        await self.client.aclose()
        app.state.gateway = self.previous

    async def test_full_publish_endpoints_and_no_build_access(self):
        health = await self.client.get("/api/health")
        self.assertEqual(health.status_code, 200)
        self.assertEqual(health.json()["backends"], ["runner", "nifi_native"])
        self.assertIs(health.json()["compiledPlanArtifact"], False)

        openapi = await self.client.get("/api/openapi")
        self.assertEqual(openapi.status_code, 200)
        self.assertEqual(openapi.json(), MOCK_OPENAPI)

        analyzed = await self.client.post(
            "/api/authoring/analyze", json={"workspace": "demo"}
        )
        self.assertEqual(analyzed.status_code, 200)
        self.assertEqual(analyzed.json()["pythonRoot"], "src")
        self.assertEqual(
            analyzed.json()["received"], {"workspace": "demo"}
        )

        created = await self.client.post(
            "/api/operators", json=MOCK_CREATE_BODY
        )
        self.assertEqual(created.status_code, 200)
        self.assertEqual(created.json()["operatorId"], "op_123")
        self.assertEqual(created.json()["received"], MOCK_CREATE_BODY)

        got = await self.client.get("/api/operators/op_123")
        self.assertEqual(got.json()["currentContract"]["version"], 1)

        for backend in ("runner", "nifi_native"):
            compiled = await self.client.post(
                f"/api/operators/op_123/backends/{backend}/compile",
                json={"options": {"profile": "test-profile"}},
            )
            self.assertEqual(compiled.status_code, 200)
            self.assertEqual(
                compiled.json()["received"],
                {"options": {"profile": "test-profile"}},
            )

        for backend in ("runner", "nifi_native"):
            published = await self.client.post(
                f"/api/operators/op_123/backends/{backend}/publish",
                json={"options": {}},
            )
            self.assertEqual(published.status_code, 200)
            self.assertEqual(published.json()["backend"], backend)
            if backend == "nifi_native":
                self.assertTrue(
                    published.json()["result"]["deploymentRequired"]
                )

        self.assertEqual(
            {item[0][1] for item in self.calls},
            {
                "/health",
                "/openapi.json",
                "/v1/authoring/analyze",
                "/v1/operators",
                "/v1/operators/op_123",
                "/v1/operators/op_123/backends/runner/compile",
                "/v1/operators/op_123/backends/nifi_native/compile",
                "/v1/operators/op_123/backends/runner/publish",
                "/v1/operators/op_123/backends/nifi_native/publish",
            }
        )
        self.assertFalse(
            any("/runtime-environments" in path for (_, path), _ in self.calls)
        )

    async def test_upstream_source_changed_and_publish_conflict_are_preserved(self):
        self.source_changed = True
        failure = await self.client.post(
            "/api/operators", json=MOCK_CREATE_BODY
        )
        self.assertEqual(failure.status_code, 400)
        self.assertEqual(
            failure.json()["detail"],
            "SOURCE_CHANGED: workspace changed after Analyze",
        )
        self.source_changed = False
        conflict = await self.client.post(
            "/api/operators/op_123/backends/runner/publish",
            json={"options": {"error": True}},
        )
        self.assertEqual(conflict.status_code, 409)
        self.assertIn("RUNTIME_NOT_READY", conflict.json()["detail"])

        missing = await self.client.get("/api/operators/nonexistent")
        self.assertEqual(missing.status_code, 404)
        self.assertEqual(missing.json()["detail"], "operator not found")

    async def test_invalid_input_not_forwarded(self):
        invalid = await self.client.post(
            "/api/operators",
            content='{"broken"',
            headers={"Content-Type": "application/json"},
        )
        self.assertEqual(invalid.status_code, 400)
        array = await self.client.post("/api/operators", json=[1, 2])
        self.assertEqual(array.status_code, 422)
        traversal = await self.client.post(
            "/api/operators/op_123/backends/runner%25evil/compile",
            json={"options": {}},
        )
        self.assertEqual(traversal.status_code, 400)
        self.assertEqual(self.calls, [])
        legacy = await self.client.post(
            "/api/runtime-environments/resolve", json={"requirements": "x"}
        )
        self.assertEqual(legacy.status_code, 404)

    async def test_connection_failure_returns_502(self):
        self.fail_connection = True
        bad = await self.client.get("/api/health")
        self.assertEqual(bad.status_code, 502)
        self.assertIn("Publish Service unreachable", bad.json()["detail"])

    async def test_frontend_is_served(self):
        page = await self.client.get("/")
        self.assertEqual(page.status_code, 200)
        self.assertIn("选择工作区", page.text)
        self.assertIn("/static/schema.js", page.text)
        self.assertIn("/static/form.js", page.text)
        self.assertNotIn("Runtime Environment</button>", page.text)
        js = await self.client.get("/static/ui.js")
        self.assertEqual(js.status_code, 200)
        self.assertIn("sourceRevision", js.text)


if __name__ == "__main__":
    unittest.main()
