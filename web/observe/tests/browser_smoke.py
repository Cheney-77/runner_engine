"""Optional Chromium verification against SQLite test fixtures and HTTP mocks.

Run: python observe/tests/browser_smoke.py
Optional screenshots: OBS_SCREENSHOT_DIR=/tmp/observe-preview python observe/tests/browser_smoke.py
"""
from __future__ import annotations

import asyncio
import os
import re
import sqlite3
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from observe.main import make_app  # noqa: E402
from observe.sandbox import SandboxReader  # noqa: E402
from observe.settings import Settings  # noqa: E402


def prepare_sqlite(database: Path):
    conn = sqlite3.connect(database)
    for statement in (
        "CREATE TABLE operators (id INTEGER)",
        "CREATE TABLE operator_contracts (id INTEGER)",
        "CREATE TABLE backend_variants (id INTEGER, backend TEXT, status TEXT)",
        "CREATE TABLE publish_jobs (id INTEGER, status TEXT, backend TEXT)",
        "INSERT INTO operators VALUES (1)",
        "INSERT INTO operators VALUES (2)",
        "INSERT INTO operator_contracts VALUES (1)",
        "INSERT INTO backend_variants VALUES (1,'runner','READY')",
        "INSERT INTO publish_jobs VALUES (1,'READY','runner')",
        "INSERT INTO publish_jobs VALUES (2,'FAILED','nifi_native')",
        "INSERT INTO publish_jobs VALUES (3,'READY','runner')",
    ):
        conn.execute(statement)
    conn.commit()
    conn.close()


def sandbox_transport():
    timestamp = datetime.now(timezone.utc)
    def handler(request: httpx.Request):
        path = request.url.path
        if path == "/v1/sandboxes":
            items = []
            for index in (1, 2):
                items.append({
                    "id": f"sbx_{index}",
                    "status": {"state": "Running"},
                    "createdAt": (timestamp-timedelta(hours=index)).isoformat(),
                    "expiresAt": (timestamp+timedelta(hours=1)).isoformat(),
                    "image": {"uri": "python:3.12"},
                })
            return httpx.Response(200, json={
                "items": items,
                "pagination": {
                    "page": 1,"pageSize": 20,"totalItems": 2,
                    "totalPages": 1,"hasNextPage": False,
                },
            })
        if path.endswith("/endpoints/44772"):
            sid = path.split("/")[3]
            return httpx.Response(200, json={
                "endpoint": f"http://metrics.local/sandboxes/{sid}/port/44772",
                "headers": {"X-EXECD-ACCESS-TOKEN": "mock-only-token"},
            })
        if path.endswith("/metrics"):
            return httpx.Response(200, json={
                "cpu_count":2.0,"cpu_used_pct":25.0,
                "mem_total_mib":512.0,"mem_used_mib":128.0,
                "timestamp":int(timestamp.timestamp()*1000),
            })
        return httpx.Response(404)
    return httpx.MockTransport(handler)


async def main():
    from playwright.async_api import async_playwright

    shots=os.getenv("OBS_SCREENSHOT_DIR", "")
    if shots: Path(shots).mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp:
        db=Path(tmp)/"publish.db"
        prepare_sqlite(db)
        settings=Settings(
            db_urls={"publish":"sqlite:////"+str(db).lstrip("/"),
                     "build":"","runner":""},
            sandbox_url="http://sandbox.local/v1",
            sandbox_page_size=20,sandbox_max_items=20,
            execd_metrics=True,
            allowed_origins=frozenset({"http://metrics.local"}),
        )
        app=make_app(
            settings,
            sandbox_reader=SandboxReader(settings, sandbox_transport()),
        )
        html=(ROOT/"observe/static/index.html").read_text(encoding="utf-8")
        html=re.sub(r'<script defer src="/observe/static/[^"]+"></script>',"",html)
        html=html.replace(
            '<link rel="stylesheet" href="/observe/static/styles.css">',""
        )
        css=(ROOT/"observe/static/styles.css").read_text(encoding="utf-8")
        js=(ROOT/"observe/static/dashboard.js").read_text(encoding="utf-8")

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://observability.test",
        ) as client:
            async with async_playwright() as playwright:
                try:
                    browser=await playwright.chromium.launch(headless=True)
                except Exception:
                    browser=await playwright.chromium.launch(
                        executable_path="/usr/bin/chromium",
                        headless=True,args=["--no-sandbox"],
                    )
                errors=[]
                calls=[]
                try:
                    async def page_for(width: int):
                        page=await browser.new_page(viewport={
                            "width":width,"height":850,
                        })
                        page.on("pageerror",lambda error: errors.append(str(error)))
                        async def mocked_fetch(path,opts):
                            calls.append(path)
                            result=await client.get(path)
                            return {
                                "status":result.status_code,
                                "body":result.text,
                            }
                        await page.expose_function("mockFetch",mocked_fetch)
                        await page.set_content(html)
                        await page.add_style_tag(content=css)
                        await page.evaluate("""() => {
                          window.fetch = async (url, opts={}) => {
                            const data = await window.mockFetch(url,opts);
                            return new Response(data.body, {
                              status:data.status,
                              headers:{"content-type":"application/json"},
                            });
                          };
                        }""")
                        await page.add_script_tag(content=js)
                        await page.wait_for_function(
                            "document.querySelector('#connectedDb').textContent === '1/3'"
                        )
                        return page

                    page=await page_for(1440)
                    try:
                        assert await page.locator("#publishJobs").inner_text()=="3"
                        assert await page.locator("#buildEnvs").inner_text()=="—"
                        assert await page.locator("#runnerReleases").inner_text()=="—"
                        assert await page.locator(".service-card").count()==3
                        assert await page.locator(".service-card").first.locator(
                            ".metric-card"
                        ).count()==4
                        if shots:
                            await page.screenshot(
                                path=str(Path(shots)/"database-mock.png"),
                                full_page=True,
                            )
                        print("PASS: database UI shows real SQLite aggregates; missing DBs are not zero")

                        await page.locator("#navSandbox").click()
                        assert await page.locator("#sandboxRunning").inner_text()=="2"
                        assert await page.locator("#sandboxListed").inner_text()=="2"
                        assert await page.locator("#sandboxMeasured").inner_text()=="2"
                        assert await page.locator("#sandboxRows tr").count()==2
                        assert "256" in await page.locator("#sandboxMemory").inner_text()
                        if shots:
                            await page.screenshot(
                                path=str(Path(shots)/"sandbox-mock.png"),
                                full_page=True,
                            )
                        await page.locator("#sandboxSearch").fill("sbx_1")
                        assert await page.locator("#sandboxRows tr").count()==1
                        assert "sbx_1" in await page.locator("#sandboxRows").inner_text()
                        await page.locator("#sandboxSearch").fill("")
                        await page.locator("#sandboxSort").select_option("cpu")
                        assert await page.locator("#sandboxRows tr").count()==2

                        original_calls=len(calls)
                        await page.locator("#refreshBtn").click()
                        await page.wait_for_function(
                            "document.querySelector('#refreshBtn').disabled === false"
                        )
                        assert len(calls)>original_calls
                        print("PASS: OpenSandbox counts, execd resources, search, sorting and manual refresh")
                    finally:
                        await page.close()

                    mobile=await page_for(390)
                    try:
                        assert await mobile.evaluate(
                            "document.documentElement.scrollWidth <= window.innerWidth + 1"
                        ), "mobile page horizontally overflows"
                        await mobile.locator("#navSandbox").click()
                        assert await mobile.locator("#sandboxRunning").inner_text()=="2"
                        if shots:
                            await mobile.screenshot(
                                path=str(Path(shots)/"mobile-mock.png"),
                                full_page=True,
                            )
                        print("PASS: 390px mobile navigation, sandbox data, no page-wide overflow")
                    finally:
                        await mobile.close()
                    assert errors==[],errors
                    assert all(path=="/observe/api/overview" for path in calls),calls
                    print("PASS: no JavaScript errors; dashboard calls observability API only")
                finally:
                    await browser.close()


if __name__=="__main__":
    asyncio.run(main())
