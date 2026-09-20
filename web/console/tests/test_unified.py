from __future__ import annotations

import hashlib
import os
import unittest
import zipfile
from pathlib import Path

import httpx

from app.main import app as original_publish
from app.gateway import PublishGateway, Settings as PublishSettings
from admin.main import make_app as make_admin_app
from console.main import Unified
from console.settings import ConsoleSettings
from console.observe_app import make_app as make_observe_app
from observe.settings import Settings as ObservationSettings

BASE = Path(__file__).resolve().parents[2]
BASELINE = Path("/mnt/data/managed-python-v33-with-observability.zip")
ENV_ID = "3269af65-af8d-4a40-8d59-50e2c8f2b316"


class TestInventory:
    def asset_exists(self,kind,identifier):
        return kind=="build_environment" and identifier==ENV_ID

    def report(self):
        return {"environments": {"status": "ok", "rows": [{
            "id": ENV_ID, "envKey": "dev-image", "status": "READY",
            "imageRef": None, "packageCount": 3,
            "createdAt": None, "updatedAt": None,
        }]},
        "leases": {"status":"ok","rows":[]},
        "buildJobs": {"status":"ok","rows":[]},
        "runs": {"status":"ok","rows":[]},
        "idempotency": {"status":"ok","rows":[]},
        "variants": {"status":"ok","rows":[]},
        "publishJobs": {"status":"ok","rows":[]}}

    def cleanup_preview(self, env_id):
        if env_id != ENV_ID:
            raise ValueError("unknown")
        return {
            "found": True, "environmentId": ENV_ID, "status": "ready",
            "environment": {
                "id": ENV_ID,"envKey": "dev-image",
                "status":"READY","imageRef":None,
            },
            "aliasCount":0,"activeBuildJobs":0,
            "publishedReferences":0,"activeLeaseReferences":0,
            "blockingReasons":[],"warnings":["Physical inspection unavailable"],
            "canExecute":False,"action":"request_only",
        }


class TestAdminStore:
    def __init__(self):
        self.notes = []
        self.requests = []

    def overview(self):
        return {
            "status":"ok","notes":self.notes,"requests":self.requests,
            "audit":[],"message":"",
        }

    def create_note(self,actor,kind,aid,text):
        note = {
            "id":ENV_ID,"assetType":kind,"assetId":aid,
            "note":text,"version":1,
        }
        self.notes.append(note)
        return note

    def request_cleanup(self,actor,preview,reason,confirm):
        if confirm!=preview["environment"]["envKey"]:
            raise ValueError("Confirmation must match env_key")
        entry={
            "id":ENV_ID,"environmentId":ENV_ID,"envKey":"dev-image",
            "status":"DRAFT","reason":reason,"createdBy":actor,"createdAt":None,
        }
        self.requests.append(entry)
        return {"id": ENV_ID,"status":"DRAFT","executed":False}


class TestUnifiedApp(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.observation_settings = ObservationSettings(
            db_urls={"publish":"","build":"","runner":""},
            basic_user="observer", basic_password="observe-secret",
            # Even legacy credentials must NOT re-enable login in unified mode.
        )
        self.settings=ConsoleSettings(
            observation=self.observation_settings,
            admin_db_url="",
            public_origin="http://example.test",
        )
        self.existing_gateway=original_publish.state.gateway

        def upstream(request):
            if request.url.path=="/health":
                return httpx.Response(200,json={
                    "version":"3.3.0", "api":"virtual-contract-v1",
                    "backends":["runner","nifi_native"],
                })
            return httpx.Response(404,json={"detail":"not found"})

        original_publish.state.gateway=PublishGateway(
            PublishSettings(publish_url="http://publish.test"),
            transport=httpx.MockTransport(upstream),
        )
        observe=make_observe_app(settings=self.observation_settings)
        admin=make_admin_app(
            TestInventory(),TestAdminStore(),
            public_origin="http://example.test",
        )
        self.unified=Unified(
            self.settings,publishing=original_publish,
            observation=observe,administration=admin,
        )
        self.client=httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.unified),
            base_url="http://example.test",
        )
        self.write_headers={
            "Origin":"http://example.test","X-Admin-Request":"1",
        }

    async def asyncTearDown(self):
        await self.client.aclose()
        original_publish.state.gateway=self.existing_gateway

    async def test_all_three_same_origin_and_publish_original_urls(self):
        original=await self.client.get("/")
        self.assertEqual(original.status_code,200)
        self.assertIn("选择工作区",original.text)
        self.assertNotIn("运行资产管理",original.text)
        script=await self.client.get("/static/ui.js")
        self.assertEqual(script.status_code,200)
        self.assertEqual(script.content,(BASE/"app/static/ui.js").read_bytes())

        health=await self.client.get("/api/health")
        self.assertEqual(health.status_code,200)
        self.assertEqual(health.json()["version"],"3.3.0")

        hub=await self.client.get("/console/")
        self.assertEqual(hub.status_code,200)
        for ref in ('href="/"','href="/observe/"','href="/admin/"'):
            self.assertIn(ref,hub.text)

        # No login on dashboard, including when old Basic settings remain.
        obs=await self.client.get("/observe/")
        self.assertEqual(obs.status_code,200)
        self.assertIn("/console/static/observe-dashboard.js",obs.text)
        data=await self.client.get(
            "/observe/api/databases"
        )
        self.assertEqual(data.status_code,200)
        self.assertEqual(data.json()["services"]["runner"]["status"],"unconfigured")

        # Admin page, JS and observability CSS are directly reachable.
        admin_static=await self.client.get("/admin/static/admin.js")
        self.assertEqual(admin_static.status_code,200)
        observe_css=await self.client.get("/observe/static/styles.css")
        self.assertEqual(observe_css.status_code,200)
        admin=await self.client.get("/admin/")
        self.assertEqual(admin.status_code,200)
        self.assertIn("运行资产总览",admin.text)
        overview=await self.client.get(
            "/admin/api/overview",
        )
        self.assertEqual(overview.json()["capabilities"]["physicalImageDeletion"],False)
        self.assertEqual(overview.json()["inventory"]["environments"]["rows"][0]["envKey"],
                         "dev-image")

    async def test_admin_no_login_but_same_origin_and_draft_only(self):
        body={"assetType":"build_environment","assetId":ENV_ID,"note":"some note"}
        missing_origin=await self.client.post(
            "/admin/api/notes",json=body,
        )
        self.assertEqual(missing_origin.status_code,403)
        wrong_origin=await self.client.post(
            "/admin/api/notes",json=body,
            headers={"Origin":"https://evil.example","X-Admin-Request":"1"},
        )
        self.assertEqual(wrong_origin.status_code,403)
        headers=self.write_headers
        create=await self.client.post(
            "/admin/api/notes",json=body,headers=headers,
        )
        self.assertEqual(create.status_code,200)
        self.assertEqual(create.json()["assetId"],ENV_ID)

        preview=await self.client.get(
            f"/admin/api/environments/{ENV_ID}/preview",
        )
        self.assertFalse(preview.json()["canExecute"])
        request=await self.client.post(
            "/admin/api/cleanup-requests",
            json={
                "environmentId":ENV_ID,
                "reason":"Test image removed outside service; review requested.",
                "confirmation":"dev-image",
            },
            headers=headers,
        )
        self.assertEqual(request.status_code,200)
        self.assertEqual(request.json()["status"],"DRAFT")
        self.assertFalse(request.json()["executed"])
        delete_image=await self.client.request(
            "DELETE",f"/admin/api/images/{ENV_ID}",json={},headers=headers,
        )
        self.assertEqual(delete_image.status_code,404)

    def test_unified_passes_independent_schemas_to_both_modules(self):
        schema_settings = ObservationSettings(
            db_urls={"publish": "", "build": "", "runner": ""},
            db_schemas={
                "publish": "p_schema",
                "build": "b_schema",
                "runner": "r_schema",
            },
        )
        config=ConsoleSettings(observation=schema_settings)
        combined=Unified(config, publishing=original_publish)
        expected=schema_settings.db_schemas
        for service in ("publish", "build", "runner"):
            self.assertEqual(
                combined.observe.state.readers[service].schemas,
                expected,
            )
        self.assertEqual(combined.admin.state.inventory.schemas,expected)

    def test_all_original_file_bytes_unchanged(self):
        if not BASELINE.is_file():
            self.skipTest("Original baseline zip missing")
        with zipfile.ZipFile(BASELINE) as z:
            for name in z.namelist():
                if name.endswith("/"):continue
                rel=Path(name).relative_to("managed-python-v33-with-observability")
                actual=BASE/rel
                self.assertTrue(actual.is_file(),str(rel))
                self.assertEqual(
                    hashlib.sha256(actual.read_bytes()).digest(),
                    hashlib.sha256(z.read(name)).digest(),
                    str(rel),
                )


if __name__=="__main__":
    unittest.main()
