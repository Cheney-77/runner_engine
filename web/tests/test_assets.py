"""Static checks: frontend IDs and removal of old Build API routes."""
from __future__ import annotations

import re
import unittest
from html.parser import HTMLParser
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class HTMLIds(HTMLParser):
    def __init__(self):
        super().__init__()
        self.ids = set()
        self.scripts = []

    def handle_starttag(self, tag, attrs):
        values = dict(attrs)
        if "id" in values:
            self.ids.add(values["id"])
        if tag == "script":
            self.scripts.append(values.get("src"))


class TestAssets(unittest.TestCase):
    def test_frontend_wiring_and_order(self):
        page = (ROOT / "app/static/index.html").read_text(encoding="utf-8")
        js = (ROOT / "app/static/ui.js").read_text(encoding="utf-8")
        parser = HTMLIds()
        parser.feed(page)
        referenced = set(re.findall(r'\$\("([A-Za-z][A-Za-z0-9]*)"\)', js))
        missing = referenced - parser.ids
        self.assertEqual(missing, set(), f"UI refers to missing controls: {missing}")
        self.assertEqual(
            parser.scripts, [
                "/static/schema.js",
                "/static/form.js",
                "/static/binding.js",
                "/static/ui.js",
            ],
        )
        for index in range(5):
            self.assertIn(f"step{index}", parser.ids)
        for essential_control in (
            "callableSearch", "showUnsupported", "contractProgress",
            "asideReviewBtn", "runnerProfile", "advancedOptions",
            "publishDialog", "confirmPublishBtn", "cancelPublishBtn",
            "globalProgress", "noticeText", "workspacePreview",
        ):
            self.assertIn(essential_control, parser.ids)
        copy_targets = re.findall(r'data-copy-target="([^"]+)"', page)
        self.assertTrue(copy_targets)
        self.assertEqual(
            set(copy_targets) - parser.ids, set(),
            "A copy button targets a missing response/ID element",
        )

    def test_publish_only_and_no_fake_api(self):
        bff = (ROOT / "app/main.py").read_text(encoding="utf-8")
        gateway = (ROOT / "app/gateway.py").read_text(encoding="utf-8")
        configs = (ROOT / ".env.example").read_text(encoding="utf-8")
        self.assertNotIn("MPR_BUILD_SERVICE_URL", configs)
        self.assertNotIn("MPR_BUILD_SERVICE_URL", gateway)
        self.assertNotIn("/runtime-environments/", bff)
        self.assertNotIn("/v1/operator-contracts/draft", bff)
        self.assertNotIn('"/v1/operators/publish"', bff)
        self.assertNotIn("BuildServiceClient(", bff)
        self.assertIn('"/v1/authoring/analyze"', bff)
        self.assertIn('"/v1/operators"', bff)


if __name__ == "__main__":
    unittest.main()
