"""Only observability configuration. Nothing imports or mutates app/."""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

SERVICES = ("publish", "build", "runner")
IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def valid_schema(value: str) -> str:
    if not IDENTIFIER.fullmatch(value):
        raise ValueError("Database schema must be an SQL identifier")
    return value


def lifecycle_base(value: str) -> str:
    if not value:
        return ""
    parsed = urlsplit(value)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise ValueError("OBS_SANDBOX_URL must be an http(s) URL with hostname")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("OBS_SANDBOX_URL must not contain credentials, query, or fragment")
    path = parsed.path.rstrip("/")
    if path not in ("", "/v1"):
        raise ValueError("OBS_SANDBOX_URL must end at the server root or /v1")
    return value.rstrip("/") if path == "/v1" else value.rstrip("/") + "/v1"


def parse_origins(value: str) -> frozenset[str]:
    origins: set[str] = set()
    for raw in value.split(","):
        part = raw.strip()
        if not part:
            continue
        url = urlsplit(part)
        if (
            url.scheme not in ("http", "https") or not url.hostname
            or url.username or url.password or url.path not in ("", "/")
            or url.query or url.fragment
        ):
            raise ValueError("OBS_EXECD_ALLOWED_ORIGINS entries must be exact http(s) origins")
        origins.add(f"{url.scheme}://{url.netloc.lower()}")
    return frozenset(origins)


def read_mapping(path: str) -> dict:
    if not path:
        return {}
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("Table mapping must be a JSON object")
    if set(data) - set(SERVICES):
        raise ValueError("Table mapping has an unknown service name")
    for service, mapping in data.items():
        if not isinstance(mapping, dict):
            raise ValueError(f"Mapping for {service} must be a JSON object")
        for metric, value in mapping.items():
            if not IDENTIFIER.fullmatch(metric):
                raise ValueError(f"Invalid metric key for {service}")
            if isinstance(value, str):
                valid_schema(value)
            elif isinstance(value, dict):
                if not isinstance(value.get("table"), str):
                    raise ValueError(f"Mapping {service}.{metric} needs a table")
                for col in ("table", "statusColumn", "groupColumn", "timeColumn"):
                    if col in value and value[col] is not None:
                        valid_schema(str(value[col]))
            else:
                raise ValueError(f"Mapping {service}.{metric} must be a table or mapping")
    return data


@dataclass(frozen=True)
class Settings:
    db_urls: dict[str, str] = field(default_factory=dict)
    db_schemas: dict[str, str] = field(default_factory=dict)
    table_map: dict = field(default_factory=dict)
    sandbox_url: str = ""
    sandbox_api_key: str = ""
    sandbox_page_size: int = 50
    sandbox_max_items: int = 200
    execd_sample_limit: int = 30
    execd_metrics: bool = False
    allowed_origins: frozenset[str] = field(default_factory=frozenset)
    basic_user: str = ""
    basic_password: str = ""

    @classmethod
    def environment(cls) -> Settings:
        page_size = int(os.getenv("OBS_SANDBOX_PAGE_SIZE", "50"))
        max_items = int(os.getenv("OBS_SANDBOX_MAX_ITEMS", "200"))
        if not 1 <= page_size <= 100:
            raise ValueError("OBS_SANDBOX_PAGE_SIZE must be 1..100")
        if not 1 <= max_items <= 500:
            raise ValueError("OBS_SANDBOX_MAX_ITEMS must be 1..500")
        sample_limit = int(os.getenv("OBS_EXECD_SAMPLE_LIMIT", "30"))
        if not 1 <= sample_limit <= 200:
            raise ValueError("OBS_EXECD_SAMPLE_LIMIT must be 1..200")
        return cls(
            db_urls={
                key: os.getenv(f"OBS_{key.upper()}_DB_URL", "").strip()
                for key in SERVICES
            },
            db_schemas={
                key: valid_schema(os.getenv(
                    f"OBS_{key.upper()}_DB_SCHEMA", "public"
                )) for key in SERVICES
            },
            table_map=read_mapping(os.getenv("OBS_TABLE_MAP_FILE", "")),
            sandbox_url=lifecycle_base(os.getenv("OBS_SANDBOX_URL", "").strip()),
            sandbox_api_key=os.getenv("OBS_SANDBOX_API_KEY", ""),
            sandbox_page_size=page_size,
            sandbox_max_items=max_items,
            execd_sample_limit=sample_limit,
            execd_metrics=os.getenv("OBS_EXECD_METRICS", "0") == "1",
            allowed_origins=parse_origins(
                os.getenv("OBS_EXECD_ALLOWED_ORIGINS", "")
            ),
            basic_user=os.getenv("OBS_BASIC_USER", ""),
            basic_password=os.getenv("OBS_BASIC_PASSWORD", ""),
        )
