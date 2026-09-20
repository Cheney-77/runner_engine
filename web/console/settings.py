"""Unified configuration, deliberately not reading OBS_TABLE_MAP_FILE."""
from __future__ import annotations

import os
from dataclasses import dataclass
from urllib.parse import urlsplit

from observe.settings import (
    Settings as ObserveSettings, lifecycle_base, parse_origins,
)

from .metrics import SPECS
from .sql_schemas import DEFAULT_SCHEMAS, configured_schemas


def bounded_int(name, default, lo, hi):
    value = int(os.getenv(name, default))
    if value < lo or value > hi:
        raise ValueError(f"{name} must be {lo}..{hi}")
    return value


@dataclass(frozen=True)
class ConsoleSettings:
    observation: ObserveSettings
    admin_db_url: str = ""
    public_origin: str = ""
    allow_remote_no_auth: bool = False
    audit_actor: str = "console-operator"
    build_service_url: str = ""
    build_bearer_token: str = ""

    @classmethod
    def environment(cls):
        urls = {name: os.getenv(f"OBS_{name.upper()}_DB_URL", "").strip()
                for name in SPECS}
        observation = ObserveSettings(
            db_urls=urls,
            db_schemas=configured_schemas({
                service: os.getenv(
                    f"OBS_{service.upper()}_DB_SCHEMA",
                    DEFAULT_SCHEMAS[service],
                ).strip()
                for service in SPECS
            }),
            table_map={},  # Intentionally ignores OBS_TABLE_MAP_FILE, even if set.
            sandbox_url=lifecycle_base(os.getenv("OBS_SANDBOX_URL", "").strip()),
            sandbox_api_key=os.getenv("OBS_SANDBOX_API_KEY", ""),
            sandbox_page_size=bounded_int("OBS_SANDBOX_PAGE_SIZE", 50, 1, 100),
            sandbox_max_items=bounded_int("OBS_SANDBOX_MAX_ITEMS", 200, 1, 500),
            execd_sample_limit=bounded_int("OBS_EXECD_SAMPLE_LIMIT", 30, 1, 200),
            execd_metrics=os.getenv("OBS_EXECD_METRICS", "0") == "1",
            allowed_origins=parse_origins(
                os.getenv("OBS_EXECD_ALLOWED_ORIGINS", "")
            ),
            basic_user="",
            basic_password="",
        )
        # This unified entry never enables UI login, even if legacy
        # OBS_BASIC_* / ADMIN_* variables remain in the process environment.
        remote = os.getenv("CONSOLE_ALLOW_REMOTE_NO_AUTH", "0") == "1"
        public_origin = os.getenv("CONSOLE_PUBLIC_ORIGIN", "").strip().rstrip("/")
        if public_origin:
            parsed = urlsplit(public_origin)
            if (
                not parsed.hostname or parsed.username or parsed.password
                or parsed.path not in ("", "/") or parsed.query or parsed.fragment
                or parsed.scheme not in ("http", "https")
                or (parsed.scheme == "http" and parsed.hostname not in
                    ("127.0.0.1", "localhost", "::1") and not remote)
            ):
                raise ValueError(
                    "CONSOLE_PUBLIC_ORIGIN must be an exact http(s) origin; "
                    "external HTTP requires explicit CONSOLE_ALLOW_REMOTE_NO_AUTH=1"
                )
        return cls(
            observation=observation,
            admin_db_url=os.getenv("ADMIN_DB_URL", "").strip(),
            public_origin=public_origin,
            allow_remote_no_auth=remote,
            audit_actor="console-operator",
            build_service_url=os.getenv("ADMIN_BUILD_SERVICE_URL", "").strip().rstrip("/"),
            build_bearer_token=os.getenv("ADMIN_BUILD_BEARER_TOKEN", ""),
        )
