from __future__ import annotations

import unittest
from uuid import uuid4

import httpx

from admin.main import make_app
from admin.retry_client import RetryClient, RetryRejected

ENV = str(uuid4())


class BuildInventory:
    def __init__(self):
        self.state="FAILED"
        self.checked=[]

    def retry_preflight(self, key):
        self.checked.append(key)
        if key!="failed-env" or self.state!="FAILED":
            raise ValueError("Retry requires FAILED status")
        return {"id":ENV,"envKey":key,"status":"FAILED","activeJobs":0}

    def report(self):
        return {"environments":{"status":"ok","rows":[]}}


class AuditStore:
    dsn="mock"

    def __init__(self):
        self.actions=[]

    def overview(self):
        return {"status":"ok","notes":[],"requests":[],"audit":[]}

    def record_action(self,actor,action,kind,identifier):
        self.actions.append((actor,action,kind,identifier))
        return {"recorded":True}


class TestBuildRetry(unittest.IsolatedAsyncioTestCase):
    async def test_retry_only_calls_build_service_once_through_approved_endpoint(self):
        calls=[]
        def handle(request):
            calls.append((request.method,str(request.url)))
            self.assertEqual(request.method,"POST")
            self.assertEqual(request.url.path,
                "/v1/runtime-environments/failed-env/retry")
            return httpx.Response(200,json={"envKey":"failed-env","status":"BUILDING"})
        client=RetryClient("http://build.test",transport=httpx.MockTransport(handle))
        inv=BuildInventory()
        store=AuditStore()
        app=make_app(inv,store,audit_actor="anonymous-console",
                     public_origin="http://admin.test",retry_client=client)
        headers={
            "Origin":"http://admin.test","X-Admin-Request":"1",
        }
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://admin.test",
        ) as http:
            result=await http.post(
                "/admin/api/environments/failed-env/retry",
                json={},headers=headers,
            )
            self.assertEqual(result.status_code,200,result.text)
            self.assertTrue(result.json()["accepted"])
            self.assertEqual(len(calls),1)
            self.assertEqual([r[1] for r in store.actions],
                             ["build.retry.attempt","build.retry.accepted"])
            inv.state="READY"
            blocked=await http.post(
                "/admin/api/environments/failed-env/retry",
                json={},headers=headers,
            )
            self.assertEqual(blocked.status_code,409)
            self.assertEqual(len(calls),1)
            self.assertEqual(len(store.actions),2)

    async def test_without_optional_build_service_retry_is_disabled(self):
        app=make_app(BuildInventory(),AuditStore())
        headers={
            "Origin":"http://admin.test","X-Admin-Request":"1",
        }
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://admin.test",
        ) as client:
            data=await client.get("/admin/api/overview",headers=headers)
            self.assertFalse(data.json()["capabilities"]["buildRetry"])
            denied=await client.post(
                "/admin/api/environments/failed-env/retry",
                json={},headers=headers,
            )
            self.assertEqual(denied.status_code,503)

    def test_rejects_non_origin_build_url(self):
        for candidate in (
            "file:///var/run/docker.sock",
            "http://user:pass@build.test",
            "http://build.test/a/path",
        ):
            with self.assertRaises(ValueError):
                RetryClient(candidate)


if __name__=="__main__":
    unittest.main()
