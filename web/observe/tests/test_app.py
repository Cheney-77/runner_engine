from __future__ import annotations

import base64
import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

import httpx

from observe.main import make_app
from observe.settings import Settings
from observe.database import readers
from observe.sandbox import SandboxReader


class TestIndependentApp(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db_path = Path(self.tmp.name) / "publish.db"
        connection = sqlite3.connect(self.db_path)
        connection.execute("CREATE TABLE publish_jobs (id INTEGER, status TEXT, backend TEXT)")
        connection.execute("INSERT INTO publish_jobs VALUES (1, 'READY', 'runner')")
        connection.commit()
        connection.close()
        self.config = Settings(
            db_urls={
                "publish": "sqlite:////" + str(self.db_path).lstrip("/"),
                "build": "", "runner": "",
            },
        )
        self.app = make_app(self.config)
        self.client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.app),
            base_url="http://test.local",
        )

    async def asyncTearDown(self):
        await self.client.aclose()

    async def test_page_assets_and_api_are_isolated(self):
        page = await self.client.get("/observe/")
        self.assertEqual(page.status_code, 200)
        self.assertIn("数据库指标", page.text)
        self.assertIn("/observe/static/dashboard.js", page.text)
        assets = await self.client.get("/observe/static/dashboard.js")
        self.assertEqual(assets.status_code, 200)
        self.assertIn("Sandbox", assets.text)
        obs = await self.client.get("/observe/api/overview")
        self.assertEqual(obs.status_code, 200)
        data = obs.json()
        metric = next(
            x for x in data["databases"]["publish"]["metrics"]
            if x["key"] == "jobs"
        )
        self.assertEqual(metric["count"], 1)
        self.assertEqual(data["databases"]["build"]["status"], "unconfigured")
        self.assertIsNone(data["sandboxes"]["runningTotal"])
        self.assertIn("no-store", obs.headers["cache-control"])
        self.assertEqual(obs.headers["x-frame-options"], "DENY")
        original = await self.client.get("/api/operators")
        self.assertEqual(original.status_code, 404)
        redirects = await self.client.get("/", follow_redirects=False)
        self.assertEqual(redirects.headers["location"], "/observe/")

    async def test_basic_auth_protects_everything(self):
        config = Settings(basic_user="viewer", basic_password="secret")
        app = make_app(config)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test.local",
        ) as client:
            for path in (
                "/observe/", "/observe/api/overview",
                "/observe/api/databases", "/observe/static/dashboard.js",
            ):
                denied = await client.get(path)
                self.assertEqual(denied.status_code, 401)
                self.assertIn("Basic realm=", denied.headers["www-authenticate"])
            credentials = base64.b64encode(b"viewer:secret").decode()
            page = await client.get(
                "/observe/",
                headers={"Authorization": f"Basic {credentials}"},
            )
            self.assertEqual(page.status_code, 200)
            bad = await client.get("/observe/", headers={
                "Authorization": "Basic " + base64.b64encode(
                    b"viewer:wrong"
                ).decode()
            })
            self.assertEqual(bad.status_code, 401)


if __name__ == "__main__":
    unittest.main()
