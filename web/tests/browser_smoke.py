"""OPTIONAL real Chromium test, fully offline against a mocked Publish upstream.

Install: pip install playwright && playwright install chromium
Run: python tests/browser_smoke.py

The browser is given inline HTML/JS and an intercepted fetch() because some
CI environments disallow external/localhost browser navigation. Requests
still go through the REAL FastAPI BFF via httpx.ASGITransport, then to a
test-only mock Publish Service via MockTransport.
"""
from __future__ import annotations

import asyncio
import copy
import json
import os
import re
import sys
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.main import app  # noqa: E402
from tests.test_bff import (
    TestPublishOnlyBFF, MOCK_CREATE_BODY, MOCK_OPENAPI, MOCK_ANALYZE,
)  # noqa: E402


async def scenario(page, *, source_changed: bool = False, mobile: bool = False):
    await page.locator("#workspace").fill("demo")
    await page.locator("#analyzeBtn").click()
    await page.locator("#analyzeSummary").wait_for(state="visible")
    assert await page.locator("#revision").inner_text() == "revision-001"
    await page.locator("#toCallablesBtn").click()
    assert await page.locator(".callable-option").count() == 1
    await page.locator("#callableSearch").fill("does-not-exist")
    assert await page.locator(".callable-option").count() == 0
    await page.locator("#callableSearch").fill("process")
    assert await page.locator(".callable-option").count() == 1
    await page.locator("#toBindingsBtn").click()
    await page.locator("#operatorEditor input[type=text]").fill("demo_processor")
    assert "0/1" in await page.locator("#contractProgress").inner_text()
    await page.locator('#argumentBindings .source-choice input[value="input.payload"]').check()
    assert "1/1" in await page.locator("#contractProgress").inner_text()
    if os.getenv("MPR_SCREENSHOT_DIR") and not source_changed and not mobile:
        await page.evaluate("window.scrollTo({top: 0, behavior: 'instant'})")
        await page.wait_for_timeout(120)
        await page.screenshot(
            path=str(Path(os.environ["MPR_SCREENSHOT_DIR"]) / "contract.png"),
            full_page=True,
        )
    await page.locator(
        "#reviewBtn" if mobile else "#asideReviewBtn"
    ).click()
    await page.locator("#step3").wait_for(state="visible")
    request_body = json.loads(await page.locator("#requestPreview").text_content())
    assert request_body == MOCK_CREATE_BODY, (
        "Wizard failed to generate correct CreateVirtualContractRequest:",
        request_body,
    )
    await page.locator("#confirmReview").check()
    await page.locator("#createBtn").click()

    if source_changed:
        await page.locator("#step0").wait_for(state="visible")
        assert await page.locator("#notice").is_visible()
        assert "代码已发生变化" in await page.locator("#notice").inner_text()
        assert not await page.locator("#analyzeSummary").is_visible()
        return "source revision conflict resets wizard"

    await page.locator("#step4").wait_for(state="visible")
    assert await page.locator("#createdOperatorId").inner_text() == "op_123"
    assert await page.locator("#quickProfileField").is_visible()
    await page.locator("#runnerProfile").fill("standard")
    assert json.loads(await page.locator("#options").input_value()) == {
        "profile": "standard"
    }
    await page.locator("#compileBtn").click()
    await page.locator("#compilePanel").wait_for(state="visible")
    compiled = json.loads(await page.locator("#compileResponse").text_content())
    assert compiled["backend"] == "runner"
    assert compiled["received"] == {"options": {"profile": "standard"}}

    await page.locator("#backend").select_option("nifi_native")
    assert not await page.locator("#quickProfileField").is_visible()
    assert await page.locator("#options").input_value() == "{}"
    await page.locator("#publishBtn").click()
    await page.locator("#publishDialog").wait_for(state="visible")
    assert await page.locator("#publishConfirmBackend").inner_text() == "nifi_native"
    await page.locator("#cancelPublishBtn").click()
    assert not await page.locator("#publishPanel").is_visible()
    await page.locator("#publishBtn").click()
    await page.locator("#confirmPublishBtn").click()
    await page.locator("#publishPanel").wait_for(state="visible")
    published = json.loads(await page.locator("#publishResponse").text_content())
    assert published["backend"] == "nifi_native"
    assert published["result"]["deploymentRequired"] is True
    assert "尚未部署" in await page.locator("#nativeNotice").inner_text()
    assert await page.evaluate(
        "document.documentElement.scrollWidth <= window.innerWidth + 1"
    ), "Full page unexpectedly overflows horizontally"
    if os.getenv("MPR_SCREENSHOT_DIR") and not mobile:
        await page.evaluate("window.scrollTo({top: 0, behavior: 'instant'})")
        await page.wait_for_timeout(120)
        await page.screenshot(
            path=str(Path(os.environ["MPR_SCREENSHOT_DIR"]) / "release.png"),
            full_page=True,
        )
    return "analyze → select → bind → review → create → compile → native publish"


async def output_codec_scenario(page):
    """Check warnings reflect current output selection; never change codec."""
    await page.locator("#workspace").fill("demo")
    await page.locator("#analyzeBtn").click()
    await page.locator("#toCallablesBtn").click()
    await page.locator("#toBindingsBtn").click()
    await page.locator("#operatorEditor input[type=text]").fill("output_test")
    await page.locator(
        '#argumentBindings .source-choice input[value="input.payload"]'
    ).check()

    # Optional output.payload is enabled explicitly by the user.
    await page.locator("#outputEditor .schema-field > label.inline-check input").first.check()
    codec = page.locator("#outputEditor select")
    assert await codec.count() == 1
    assert await codec.input_value() == "", "codec must not default to enum[0]"
    assert not await page.locator("#outputTypeWarning").is_visible()
    await codec.select_option(value='"bytes"')
    assert await page.locator("#outputTypeWarning").is_visible()
    assert "原始字节" in await page.locator("#outputTypeWarning").inner_text()
    if os.getenv("MPR_SCREENSHOT_DIR"):
        await page.locator("#outputPanel").screenshot(
            path=str(Path(os.environ["MPR_SCREENSHOT_DIR"]) / "output-codec-card.png"),
        )
        await page.evaluate("window.scrollTo({top: 0, behavior: 'instant'})")
        await page.wait_for_timeout(120)
        await page.screenshot(
            path=str(Path(os.environ["MPR_SCREENSHOT_DIR"]) / "output-codec-warning.png"),
            full_page=True,
        )

    await codec.select_option(value='"json"')
    assert not await page.locator("#outputTypeWarning").is_visible()
    await page.locator("#asideReviewBtn").click()
    await page.locator("#step3").wait_for(state="visible")
    body = json.loads(await page.locator("#requestPreview").text_content())
    assert body["output"]["payload"]["codec"] == "json"
    assert not await page.locator("#reviewOutputWarning").is_visible()
    return "bytes output warning appears for declared dict return and clears on codec change"


async def source_switch_scenario(page):
    """Synthetic OpenAPI fixture proving both parameter groups switch fields.

    The property names in this fixture are test-only examples, not claims
    about the user's unprovided BindingSelection implementation.
    """
    await page.locator("#workspace").fill("demo")
    await page.locator("#analyzeBtn").click()
    await page.locator("#toCallablesBtn").click()
    await page.locator("#toBindingsBtn").click()
    await page.locator("#operatorEditor input[type=text]").fill("switch_test")

    ctor = page.locator("#constructorBindings")
    assert await ctor.locator('.source-choice input[value="input.payload"]').count() == 0
    await ctor.locator('.source-choice input[value="operator.parameter"]').check()
    await ctor.locator(".source-main-fields input[type=text]").fill("threshold")
    await ctor.locator('.source-choice input[value="constant"]').check()
    assert await ctor.locator(".source-main-fields input[type=text]").count() == 1
    assert await ctor.locator(".source-main-fields select").count() == 1
    await ctor.locator(".quick-type select").select_option("number")
    await ctor.locator('.source-main-fields input[type=number]').fill("5")
    await ctor.locator('.source-choice input[value="operator.parameter"]').check()
    assert await ctor.locator(".source-main-fields input[type=text]").input_value() == "threshold"
    await ctor.locator('.source-choice input[value="constant"]').check()
    assert await ctor.locator('.source-main-fields input[type=number]').input_value() == "5"

    argument = page.locator("#argumentBindings")
    await argument.locator('.source-choice input[value="input.metadata"]').check()
    assert await argument.locator(".source-main-fields input[type=text]").count() == 1
    assert await argument.locator(".source-main-fields input[type=text]").is_visible()
    await argument.locator(".source-main-fields input[type=text]").fill("trace-id")
    if os.getenv("MPR_SCREENSHOT_DIR"):
        await argument.screenshot(
            path=str(Path(os.environ["MPR_SCREENSHOT_DIR"]) / "binding-metadata-card.png"),
        )
        await page.evaluate("window.scrollTo({top: 0, behavior: 'instant'})")
        await page.wait_for_timeout(120)
        await page.screenshot(
            path=str(Path(os.environ["MPR_SCREENSHOT_DIR"]) / "binding-metadata.png"),
            full_page=True,
        )
    await argument.locator('.source-choice input[value="constant"]').check()
    assert await argument.locator(".source-main-fields input[type=text]").count() == 1
    await argument.locator('.quick-type select').select_option("number")
    await argument.locator(".source-main-fields input[type=number]").fill("42")
    await argument.locator(".quick-type select").select_option("string")
    await argument.locator(".source-main-fields input[type=text]").fill("hello")
    await argument.locator(".quick-type select").select_option("number")
    assert await argument.locator(".source-main-fields input[type=number]").input_value() == "42"
    await argument.locator(".quick-type select").select_option("string")
    assert await argument.locator(".source-main-fields input[type=text]").input_value() == "hello"
    await argument.locator(".quick-type select").select_option("number")
    assert await argument.locator(".source-main-fields").locator("text=trace-id").count() == 0
    await argument.locator('.source-choice input[value="input.metadata"]').check()
    assert await argument.locator(".source-main-fields input[type=text]").input_value() == "trace-id"
    await argument.locator('.source-choice input[value="constant"]').check()
    assert await argument.locator(".source-main-fields input[type=number]").input_value() == "42"
    if os.getenv("MPR_SCREENSHOT_DIR"):
        await argument.screenshot(
            path=str(Path(os.environ["MPR_SCREENSHOT_DIR"]) / "binding-constant-card.png"),
        )
        await page.evaluate("window.scrollTo({top: 0, behavior: 'instant'})")
        await page.wait_for_timeout(120)
        await page.screenshot(
            path=str(Path(os.environ["MPR_SCREENSHOT_DIR"]) / "binding-constant.png"),
            full_page=True,
        )

    assert "2/2" in await page.locator("#contractProgress").inner_text()
    await page.locator("#asideReviewBtn").click()
    await page.locator("#step3").wait_for(state="visible")
    request = json.loads(await page.locator("#requestPreview").text_content())
    assert request["constructor_bindings"]["threshold"] == {
        "source": "constant", "value": 5
    }, request
    assert request["argument_bindings"]["payload"] == {
        "source": "constant", "value": 42
    }, request
    assert "metadata_key" not in request["argument_bindings"]["payload"]
    assert "parameter_name" not in request["constructor_bindings"]["threshold"]
    return "source switching updates fields and drops stale keys; values restore on switch"


async def main():
    try:
        from playwright.async_api import async_playwright
    except ImportError as exc:
        raise RuntimeError(
            "Optional dependency missing: pip install playwright; playwright install chromium"
        ) from exc

    fixture = TestPublishOnlyBFF(
        methodName="test_full_publish_endpoints_and_no_build_access"
    )
    await fixture.asyncSetUp()
    errors = []
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://bff.test",
        ) as bff:
            async with async_playwright() as playwright:
                try:
                    browser = await playwright.chromium.launch(headless=True)
                except Exception:
                    # Useful for container CI that preinstalls system Chromium.
                    browser = await playwright.chromium.launch(
                        headless=True,
                        executable_path="/usr/bin/chromium",
                        args=["--no-sandbox"],
                    )
                try:
                    async def load_page(width=1440):
                        page = await browser.new_page(viewport={
                            "width": width,
                            "height": 900 if width > 600 else 844,
                        })
                        page.on("pageerror", lambda error: errors.append(str(error)))
                        page.on(
                            "dialog",
                            lambda dialog: asyncio.create_task(dialog.accept()),
                        )

                        async def mocked_fetch(method, path, body):
                            response = await bff.request(
                                method,
                                path,
                                json=body if body is not None else None,
                            )
                            return {
                                "status": response.status_code,
                                "body": response.text,
                            }

                        await page.expose_function("mockFetch", mocked_fetch)
                        html = (ROOT / "app/static/index.html").read_text(
                            encoding="utf-8"
                        )
                        html = re.sub(
                            r'<script defer src="/static/[^"]+"></script>',
                            "",
                            html,
                        ).replace(
                            '<link rel="stylesheet" href="/static/styles.css">',
                            "",
                        )
                        await page.set_content(html)
                        await page.add_style_tag(content=(
                            ROOT / "app/static/styles.css"
                        ).read_text(encoding="utf-8"))
                        await page.evaluate("""() => {
                          window.fetch = async (path, opts = {}) => {
                            const body = opts.body === undefined ?
                              null : JSON.parse(opts.body);
                            const result = await window.mockFetch(
                              opts.method || "GET", path, body
                            );
                            return new Response(result.body, {
                              status: result.status,
                              headers: {"content-type": "application/json"}
                            });
                          };
                        }""")
                        for js in ("schema.js", "form.js", "binding.js", "ui.js"):
                            await page.add_script_tag(content=(
                                ROOT / "app/static" / js
                            ).read_text(encoding="utf-8"))
                        await page.wait_for_function(
                            "document.getElementById('serviceStatus')" +
                            ".textContent.includes('3.3.0')"
                        )
                        return page

                    preview = os.getenv("MPR_SCREENSHOT_DIR")
                    if preview:
                        Path(preview).mkdir(parents=True, exist_ok=True)
                    page = await load_page()
                    try:
                        if preview:
                            await page.screenshot(
                                path=str(Path(preview) / "workspace.png"),
                                full_page=True,
                            )
                        good = await scenario(page)
                        print("PASS:", good)
                    finally:
                        await page.close()

                    fixture.source_changed = True
                    page = await load_page()
                    try:
                        conflict = await scenario(page, source_changed=True)
                        print("PASS:", conflict)
                    finally:
                        await page.close()

                    fixture.source_changed = False
                    mobile_page = await load_page(width=390)
                    try:
                        assert await mobile_page.evaluate(
                            "document.documentElement.scrollWidth <= window.innerWidth + 1"
                        ), "Workspace page has mobile horizontal overflow"
                        if preview:
                            await mobile_page.screenshot(
                                path=str(Path(preview) / "mobile-workspace.png"),
                                full_page=True,
                            )
                        await scenario(mobile_page, mobile=True)
                        print("PASS: 390px mobile wizard flow and no page-wide horizontal overflow")
                    finally:
                        await mobile_page.close()

                    previous_binding_schema = copy.deepcopy(
                        MOCK_OPENAPI["components"]["schemas"]["BindingSelection"]
                    )
                    previous_constructors = copy.deepcopy(
                        MOCK_ANALYZE["callables"][0]["constructorParameters"]
                    )
                    try:
                        MOCK_OPENAPI["components"]["schemas"]["BindingSelection"] = {
                            "type": "object",
                            "required": ["source"],
                            "properties": {
                                "source": {"type": "string"},
                                "metadata_key": {"type": "string"},
                                "parameter_name": {"type": "string"},
                                "value": {},
                                "payload_path": {"type": "string"},
                                "codec": {"type": "string"},
                            },
                        }
                        MOCK_ANALYZE["callables"][0]["constructorParameters"] = [{
                            "name": "threshold",
                            "kind": "positional",
                            "annotation": "int",
                            "required": True,
                            "hasLiteralDefault": False,
                            "defaultValue": None,
                            "defaultExpression": None,
                            "allowedSources": ["operator.parameter", "constant"],
                            "suggestions": [],
                        }]
                        dynamic_page = await load_page()
                        try:
                            dynamic = await source_switch_scenario(dynamic_page)
                            print("PASS:", dynamic)
                        finally:
                            await dynamic_page.close()
                    finally:
                        MOCK_OPENAPI["components"]["schemas"]["BindingSelection"] = (
                            previous_binding_schema
                        )
                        MOCK_ANALYZE["callables"][0]["constructorParameters"] = (
                            previous_constructors
                        )

                    previous_output = copy.deepcopy(
                        MOCK_OPENAPI["components"]["schemas"]["OutputSelection"]
                    )
                    previous_annotation = MOCK_ANALYZE["callables"][0]["returnAnnotation"]
                    try:
                        MOCK_OPENAPI["components"]["schemas"]["OutputSelection"] = {
                            "type": "object",
                            "properties": {
                                "payload": {
                                    "type": "object",
                                    "required": ["codec"],
                                    "properties": {
                                        "codec": {"enum": ["bytes", "json"]},
                                    },
                                }
                            },
                        }
                        MOCK_ANALYZE["callables"][0]["returnAnnotation"] = "dict[str, int]"
                        output_page = await load_page()
                        try:
                            result = await output_codec_scenario(output_page)
                            print("PASS:", result)
                        finally:
                            await output_page.close()
                    finally:
                        MOCK_OPENAPI["components"]["schemas"]["OutputSelection"] = previous_output
                        MOCK_ANALYZE["callables"][0]["returnAnnotation"] = previous_annotation

                    assert not errors, f"JavaScript page errors: {errors}"
                    assert all(
                        "/runtime-environments" not in key[1]
                        for key, _ in fixture.calls
                    ), "BFF contacted a Build endpoint"
                    print("PASS: no browser JavaScript errors")
                    print("PASS: no direct Build Service endpoints")
                finally:
                    await browser.close()
    finally:
        await fixture.asyncTearDown()


if __name__ == "__main__":
    asyncio.run(main())
