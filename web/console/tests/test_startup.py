from __future__ import annotations

import sys
import unittest
from unittest.mock import patch

import console.main as entry
from console.settings import ConsoleSettings
from observe.settings import Settings as ObservationSettings


class TestNoLoginStartup(unittest.TestCase):
    def setUp(self):
        self.config = ConsoleSettings(
            observation=ObservationSettings(
                db_urls={"publish": "", "build": "", "runner": ""},
                basic_user="", basic_password="",
            )
        )

    def test_loopback_no_web_credentials_needed(self):
        with patch.object(entry.app, "settings", self.config), \
             patch.object(sys, "argv", ["console.main", "--host", "127.0.0.1"]), \
             patch("uvicorn.run") as launch:
            entry.main()
        launch.assert_called_once()
        self.assertEqual(launch.call_args.kwargs["host"], "127.0.0.1")

    def test_remote_must_explicitly_accept_no_login(self):
        with patch.object(entry.app, "settings", self.config), \
             patch.object(sys, "argv", ["console.main", "--host", "0.0.0.0"]):
            with self.assertRaises(SystemExit):
                entry.main()

        allowed = ConsoleSettings(
            observation=self.config.observation,
            allow_remote_no_auth=True,
            public_origin="https://console.internal.example",
        )
        with patch.object(entry.app, "settings", allowed), \
             patch.object(sys, "argv", ["console.main", "--host", "0.0.0.0"]), \
             patch("uvicorn.run") as launch:
            entry.main()
        launch.assert_called_once()
        self.assertEqual(launch.call_args.kwargs["host"], "0.0.0.0")

    def test_remote_requires_explicit_browser_origin(self):
        missing = ConsoleSettings(
            observation=self.config.observation, allow_remote_no_auth=True,
        )
        with patch.object(entry.app, "settings", missing), \
             patch.object(sys, "argv", ["console.main", "--host", "0.0.0.0"]):
            with self.assertRaises(SystemExit):
                entry.main()


if __name__=="__main__":
    unittest.main()
