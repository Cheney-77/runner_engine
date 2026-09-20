from __future__ import annotations

import asyncio
import json
import unittest
from datetime import datetime, timedelta, timezone

import httpx

from observe.sandbox import SandboxReader, endpoint_for_metrics
from observe.settings import Settings, parse_origins


class TestSandboxReader(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.calls = []
        self.now = datetime.now(timezone.utc)
        self.created = (self.now-timedelta(hours=3)).isoformat()
        self.expires = (self.now+timedelta(hours=2)).isoformat()

    def transport(self):
        def handler(request: httpx.Request) -> httpx.Response:
            self.calls.append((request.method, str(request.url), dict(request.headers)))
            path = request.url.path
            if path == "/v1/sandboxes":
                assert request.url.params["state"] == "Running"
                if request.url.params["page"] == "1":
                    return httpx.Response(200, json={
                        "items": [
                            {
                                "id": "sbx_1", "status": {"state": "Running"},
                                "createdAt": self.created,
                                "expiresAt": self.expires,
                                "image": {"uri": "python:3.12"},
                                "metadata": {"secret": "must-not-be-sent"},
                            },
                        ],
                        "pagination": {
                            "page": 1, "pageSize": 1, "totalItems": 2,
                            "totalPages": 2, "hasNextPage": True,
                        },
                    })
                return httpx.Response(200, json={
                    "items": [{
                        "id": "sbx_2", "status": {"state": "Running"},
                        "createdAt": self.created,
                        "image": {"uri": "python:3.12"},
                    }],
                    "pagination": {
                        "page": 2, "pageSize": 1, "totalItems": 2,
                        "totalPages": 2, "hasNextPage": False,
                    },
                })
            if "/endpoints/44772" in path:
                sid = path.split("/")[3]
                return httpx.Response(200, json={
                    "endpoint": f"http://metrics.local/sandboxes/{sid}/port/44772",
                    "headers": {"X-EXECD-ACCESS-TOKEN": "test-execd-access"},
                })
            if path.endswith("/metrics"):
                assert request.url.host == "metrics.local"
                assert request.headers.get("X-EXECD-ACCESS-TOKEN") == "test-execd-access"
                assert not request.headers.get("OPEN-SANDBOX-API-KEY")
                return httpx.Response(200, json={
                    "cpu_count": 2.0, "cpu_used_pct": 34.5,
                    "mem_total_mib": 512.0, "mem_used_mib": 123.2,
                    "timestamp": 1789720000000,
                })
            return httpx.Response(404)
        return httpx.MockTransport(handler)

    async def test_paginated_running_listing_and_metrics(self):
        config = Settings(
            sandbox_url="http://sandbox.local/v1",
            sandbox_api_key="test-lifecycle-key",
            sandbox_page_size=1,
            sandbox_max_items=2,
            execd_metrics=True,
            allowed_origins=frozenset(["http://metrics.local"]),
        )
        result = await SandboxReader(config, self.transport()).report()
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["runningTotal"], 2)
        self.assertEqual(result["pagesFetched"], 2)
        self.assertEqual(result["resourceMeasured"], 2)
        self.assertFalse(result["truncated"])
        row = result["sandboxes"][0]
        self.assertEqual(row["resources"]["cpuPct"], 34.5)
        self.assertEqual(row["resources"]["memUsedMiB"], 123.2)
        self.assertTrue(2*3600 < row["ageSeconds"] < 4*3600)
        self.assertNotIn("metadata", row)
        self.assertNotIn("test-execd-access", json.dumps(result))
        self.assertNotIn("test-lifecycle-key", json.dumps(result))
        self.assertTrue(all(method == "GET" for method, *_ in self.calls))

    async def test_unconfigured_never_returns_false_zero(self):
        result = await SandboxReader(Settings()).report()
        self.assertEqual(result["status"], "unconfigured")
        self.assertIsNone(result["runningTotal"])

    async def test_truncation_is_explicit(self):
        config = Settings(
            sandbox_url="http://sandbox.local/v1",
            sandbox_page_size=1, sandbox_max_items=1,
        )
        result = await SandboxReader(config, self.transport()).report()
        self.assertEqual(result["runningTotal"], 2)
        self.assertEqual(result["returned"], 1)
        self.assertTrue(result["truncated"])
        self.assertEqual(result["resourceMeasured"], 0)
        self.assertEqual(result["sandboxes"][0]["resourceStatus"], "disabled")

    async def test_metrics_origin_blocked_without_allowlist(self):
        config = Settings(
            sandbox_url="http://sandbox.local/v1",
            sandbox_page_size=1, sandbox_max_items=1,
            execd_metrics=True,
            allowed_origins=frozenset(["https://approved.local"]),
        )
        result = await SandboxReader(config, self.transport()).report()
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["resourceMeasured"], 0)
        self.assertEqual(result["sandboxes"][0]["resourceStatus"], "unavailable")
        self.assertFalse(any("/metrics" in url for _, url, _ in self.calls))

    def test_endpoint_enforces_scheme_host_and_credentials(self):
        origins = frozenset(["https://approved.local"])
        ok = endpoint_for_metrics(
            "https://approved.local/sandboxes/s1/port/44772", origins
        )
        self.assertEqual(ok, "https://approved.local/sandboxes/s1/port/44772/metrics")
        self.assertEqual(
            endpoint_for_metrics("approved.local/sandboxes/s1/port/44772", origins),
            ok,
        )
        for unsafe in (
            "http://169.254.169.254/latest/meta-data",
            "http://user:password@approved.local/sandbox",
            "https://evil.local/?x=abc",
            "file:///tmp/sensitive",
        ):
            with self.assertRaises(ValueError):
                endpoint_for_metrics(unsafe, origins)

    def test_origin_config_requires_exact_origins(self):
        self.assertEqual(
            parse_origins("http://127.0.0.1:8080,https://sandbox.example.com"),
            frozenset({"http://127.0.0.1:8080", "https://sandbox.example.com"}),
        )
        with self.assertRaises(ValueError):
            parse_origins("http://localhost:8080/private/path")


if __name__ == "__main__":
    unittest.main()
