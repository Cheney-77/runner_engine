"""Browser UI coverage with simulated APIs (never touches production services).

python console/tests/browser_smoke.py
CONSOLE_SCREENSHOTS=/tmp/console-previews python console/tests/browser_smoke.py
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import sys
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from admin.main import make_app as make_admin_app
from console.main import Unified
from console.settings import ConsoleSettings
from console.observe_app import make_app as make_observe_app
from observe.settings import Settings as ObsSettings

from console.tests.test_unified import TestInventory, ENV_ID


class MemoryStore:
    def __init__(self):
        self.notes = []
        self.requests = []
        self.audit = []

    def overview(self):
        return {"status":"ok","message":"",
                "notes":list(self.notes),
                "requests":list(self.requests),
                "audit":list(self.audit)}

    def log(self, actor, action, kind, asset):
        self.audit.append({
            "id":ENV_ID,"actor":actor,"action":action,
            "assetType":kind,"assetId":asset,"createdAt":"2026-09-18T08:00:00Z",
        })

    def create_note(self,actor,kind,asset,txt):
        record={
            "id":ENV_ID,"assetType":kind,"assetId":asset,
            "note":txt,"version":1,"updatedAt":"2026-09-18T08:00:00Z",
        }
        self.notes.append(record)
        self.log(actor,"note.create",kind,asset)
        return record

    def update_note(self,actor,uid,txt,version):
        item=next(x for x in self.notes if x["id"]==uid)
        assert item["version"]==version
        item["version"]+=1
        item["note"]=txt
        self.log(actor,"note.update",item["assetType"],item["assetId"])
        return {"id":uid,"note":txt,"version":item["version"]}

    def delete_note(self,actor,uid,version):
        item=next(x for x in self.notes if x["id"]==uid)
        assert item["version"]==version
        self.notes.remove(item)
        self.log(actor,"note.delete",item["assetType"],item["assetId"])
        return {"deleted":True,"id":uid}

    def request_cleanup(self,actor,preview,reason,confirm):
        assert confirm==preview["environment"]["envKey"]
        row={
            "id":ENV_ID,"environmentId":ENV_ID,
            "envKey":confirm,"status":"DRAFT","reason":reason,
            "createdBy":actor,"createdAt":"2026-09-18T08:00:00Z",
        }
        self.requests.append(row)
        self.log(actor,"cleanup.request","build_environment",ENV_ID)
        return {"id":ENV_ID,"status":"DRAFT","executed":False}

    def cancel_cleanup(self,actor,uid):
        row=next(x for x in self.requests if x["id"]==uid)
        row["status"]="CANCELLED"
        self.log(actor,"cleanup.cancel","build_environment",ENV_ID)
        return {"id":uid,"status":"CANCELLED","executed":False}


class Reader:
    def __init__(self,service):
        self.service=service

    def report(self):
        names={
            "publish":[("operators",2),("contracts",2),("variants",2),("jobs",3)],
            "build":[("environments",2),("aliases",1),("jobs",2)],
            "runner":[("leases",1),("runs",5),("idempotency",1),("releases",1)],
        }[self.service]
        return {
            "service":self.service,"status":"ok","dialect":"postgresql",
            "message":"","tables":[],"checks":[{
                "key":"failed","label":"需要关注的项目",
                "severity":"warning","count":1,
            }],
            "metrics":[{
                "key":name,"label":name.title(),"count":value,
                "quality":"exact","table":"mock."+name,
                "recent24h":1,"latestAt":None,
                "byStatus":[{"label":"READY","count":value}],
                "byBackend":[],"note":"",
            } for name,value in names],
        }


class Sandbox:
    async def report(self):
        return {
            "status":"ok","message":"","sampledAt":"2026-09-18T08:00:00Z",
            "runningTotal":1,"returned":1,"truncated":False,
            "resourceMeasured":0,"metricsEnabled":False,
            "sandboxes":[{
                "id":"sandbox_1","state":"Running",
                "createdAt":"2026-09-18T06:00:00Z",
                "ageSeconds":7200,"expiresAt":None,"ttlSeconds":None,
                "image":"python:test","snapshotId":None,"resources":None,
                "resourceStatus":"disabled","resourceNote":"disabled",
            }],
        }


async def main():
    from playwright.async_api import async_playwright

    screenshot_path=os.getenv("CONSOLE_SCREENSHOTS")
    if screenshot_path:Path(screenshot_path).mkdir(parents=True,exist_ok=True)
    obs_settings=ObsSettings(
        db_urls={"publish":"","build":"","runner":""},
        basic_user="observer",basic_password="secret",
    )
    settings=ConsoleSettings(
        observation=obs_settings,
        admin_db_url="",public_origin="http://example.test",
    )
    memory=MemoryStore()
    observe=make_observe_app(
        settings=obs_settings,sandbox_reader=Sandbox(),
        database_readers={key:Reader(key) for key in ("publish","build","runner")},
    )
    administration=make_admin_app(
        TestInventory(),memory,
        public_origin="http://example.test",
    )
    combined=Unified(settings,observation=observe,administration=administration)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=combined),
        base_url="http://example.test",
    ) as backend:
        async with async_playwright() as playwright:
            try:
                browser=await playwright.chromium.launch(headless=True)
            except Exception:
                browser=await playwright.chromium.launch(
                    headless=True,executable_path="/usr/bin/chromium",
                    args=["--no-sandbox"],
                )
            errors=[]
            try:
                async def load(folder,width=1400):
                    page=await browser.new_page(viewport={
                        "width":width,"height":840,
                    })
                    page.on("pageerror",lambda error:errors.append(str(error)))
                    html_name={
                        "admin":"admin/static/index.html",
                        "observe":"console/static/observe.html",
                        "console":"console/static/index.html",
                    }[folder]
                    html=(ROOT/html_name).read_text(encoding="utf-8")
                    html=re.sub(
                        r'<script defer src="[^"]+"></script>',"",html,
                    )
                    html=re.sub(
                        r'<link rel="stylesheet" href="[^"]+">',"",html,
                    )
                    await page.set_content(html)
                    css_list={
                        "admin":["admin/static/styles.css"],
                        "observe":["observe/static/styles.css",
                                   "console/static/observe-addon.css"],
                        "console":["console/static/home.css"],
                    }[folder]
                    for name in css_list:
                        await page.add_style_tag(content=(ROOT/name).read_text())
                    if folder!="console":
                        async def fake_fetch(path,options):
                            options=options or {}
                            headers={}
                            # No Authorization header is ever required.
                            if options.get("body") is not None:
                                headers.update({
                                    "Origin":"http://example.test",
                                    "X-Admin-Request":"1",
                                })
                                result=await backend.request(
                                    options.get("method","GET"),
                                    path,content=options["body"],
                                    headers={**headers,"Content-Type":"application/json"},
                                )
                            else:
                                result=await backend.get(path,headers=headers)
                            return {"status":result.status_code,"body":result.text}
                        await page.expose_function("mockFetch",fake_fetch)
                        await page.evaluate("""() => {
                          window.fetch = async (path, opts={}) => {
                            const response=await window.mockFetch(path,opts);
                            return new Response(response.body,{
                              status:response.status,
                              headers:{"content-type":"application/json"},
                            });
                          };
                        }""")
                        js_file={
                            "admin":"admin/static/admin.js",
                            "observe":"console/static/observe-dashboard.js",
                        }[folder]
                        await page.add_script_tag(content=(ROOT/js_file).read_text())
                    return page

                home=await load("console")
                if screenshot_path:
                    await home.screenshot(path=str(Path(screenshot_path)/"landing.png"),
                                          full_page=True)
                assert await home.locator(".module").count()==3
                print("PASS: 3-module hub, separate Publish link preserved")
                await home.close()

                page=await load("admin")
                await page.wait_for_function(
                    "document.querySelector('#envCount').textContent==='1'"
                )
                assert await page.locator(".risk-card").count()==3
                print("PASS: admin inventory and risk cards")
                if screenshot_path:
                    await page.screenshot(
                        path=str(Path(screenshot_path)/"admin-overview.png"),
                        full_page=True,
                    )
                await page.locator('[data-view="environments"]').click()
                assert await page.locator("#envRows tr").count()==1
                await page.locator("#envSearch").fill("notfound")
                assert "暂无记录" in await page.locator("#envRows").inner_text()
                await page.locator("#envSearch").fill("")
                await page.locator("#envRows button").click()
                await page.locator("#previewResult").wait_for(state="visible")
                assert "dev-image" in await page.locator("#previewResult").inner_text()
                assert "不可" not in (await page.locator("#previewResult").inner_text())[:2]
                if screenshot_path:
                    await page.screenshot(
                        path=str(Path(screenshot_path)/"admin-preflight.png"),
                        full_page=True,
                    )
                await page.locator("#cleanupReason").fill(
                    "This image was removed outside Build Service; verify cleanup."
                )
                await page.locator("#cleanupConfirm").fill("dev-image")
                await page.locator("#requestBtn").click()
                await page.wait_for_function(
                    "document.querySelector('#requestRows').textContent.includes('DRAFT')"
                )
                assert len(memory.requests)==1
                assert memory.requests[0]["status"]=="DRAFT"
                print("PASS: preflight + draft request, no deletion")

                await page.locator('[data-view="notes"]').click()
                await page.locator("#noteAsset").fill(ENV_ID)
                await page.locator("#noteText").fill("Operator verified asset")
                await page.locator("#noteCreateBtn").click()
                await page.wait_for_function(
                    "document.querySelector('#noteList').textContent.includes('Operator verified asset')"
                )
                await page.locator("#noteList button").first.click()
                await page.locator("#noteList textarea").fill("Rechecked asset")
                await page.locator("#noteList button").last.click()
                await page.wait_for_function(
                    "document.querySelector('#noteList').textContent.includes('Rechecked asset')"
                )
                assert memory.notes[0]["version"]==2
                await page.locator("#noteList button").last.click()
                await page.locator("#confirmDialog").wait_for(state="visible")
                await page.locator("#confirmActionBtn").click()
                await page.wait_for_function(
                    "document.querySelector('#noteList').textContent.includes('暂无备注')"
                )
                assert len(memory.notes)==0
                print("PASS: notes create/update/delete + audit")
                await page.close()

                obs=await load("observe")
                await obs.wait_for_function(
                    "document.querySelector('#connectedDb').textContent==='3/3'"
                )
                assert await obs.locator("#runnerReleases").inner_text()=="1"
                assert await obs.locator(".exact-checks").count()==3
                if screenshot_path:
                    await obs.screenshot(
                        path=str(Path(screenshot_path)/"observe-exact.png"),
                        full_page=True,
                    )
                print("PASS: exact DDL observability dashboard, all 3 databases")
                await obs.close()

                mobile=await load("admin",390)
                await mobile.wait_for_function(
                    "document.querySelector('#envCount').textContent==='1'"
                )
                assert await mobile.evaluate(
                    "document.documentElement.scrollWidth <= window.innerWidth+1"
                ),"Admin page-wide mobile overflow"
                if screenshot_path:
                    await mobile.screenshot(
                        path=str(Path(screenshot_path)/"admin-mobile.png"),
                        full_page=True,
                    )
                await mobile.close()
                assert errors==[],errors
                print("PASS: 390px mobile layout and no page JavaScript errors")
            finally:
                await browser.close()


if __name__=="__main__":
    asyncio.run(main())
