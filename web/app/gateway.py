"""A deliberately narrow HTTP gateway. It cannot call Build Service."""
from __future__ import annotations

import os
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

import httpx

NO_BODY = object()


@dataclass(frozen=True)
class Settings:
    publish_url: str = "http://127.0.0.1:9090"
    http_timeout: float = 60
    long_timeout: float = 1900
    bearer_token: str = ""

    @classmethod
    def from_environment(cls) -> Settings:
        return cls(
            publish_url=os.getenv(
                "MPR_PUBLISH_SERVICE_URL", "http://127.0.0.1:9090"
            ).rstrip("/"),
            http_timeout=float(os.getenv("MPR_HTTP_TIMEOUT_SECONDS", "60")),
            long_timeout=float(os.getenv("MPR_LONG_TIMEOUT_SECONDS", "1900")),
            bearer_token=os.getenv("MPR_PUBLISH_BEARER_TOKEN", ""),
        )


def segment(value: str, label: str) -> str:
    """An ID/backend is ONE URL segment, never a path or a URL."""
    if (
        not value
        or len(value) > 256
        or value in {".", ".."}
        or any(c in value for c in "/\\%?#\r\n\x00")
    ):
        raise ValueError(f"Invalid {label}")
    return quote(value, safe="")


class PublishGateway:
    def __init__(
        self,
        settings: Settings | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        self.settings = settings or Settings.from_environment()
        # Dependency injection for tests; production uses real network transport.
        self.transport = transport

    def _headers(self, accept: str) -> dict[str, str]:
        headers = {"Accept": accept}
        if self.settings.bearer_token:
            headers["Authorization"] = (
                f"Bearer {self.settings.bearer_token}"
            )
        return headers

    async def request(
        self,
        method: str,
        path: str,
        *,
        body: Any = NO_BODY,
        long_running: bool = False,
    ) -> httpx.Response:
        if not path.startswith("/") or path.startswith("//"):
            raise ValueError("Invalid upstream route")

        kwargs: dict[str, Any] = {}
        if body is not NO_BODY:
            kwargs["json"] = body

        timeout = (
            self.settings.long_timeout
            if long_running
            else self.settings.http_timeout
        )
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(timeout, connect=10.0),
            follow_redirects=False,
            trust_env=False,
            transport=self.transport,
        ) as client:
            return await client.request(
                method,
                self.settings.publish_url + path,
                headers=self._headers("application/json"),
                **kwargs,
            )

    @asynccontextmanager
    async def stream(
        self,
        method: str,
        path: str,
        *,
        accept: str = "text/event-stream",
    ):
        if not path.startswith("/") or path.startswith("//"):
            raise ValueError("Invalid upstream route")

        client = httpx.AsyncClient(
            timeout=httpx.Timeout(None, connect=10.0),
            follow_redirects=False,
            trust_env=False,
            transport=self.transport,
        )
        response = None
        try:
            upstream_request = client.build_request(
                method,
                self.settings.publish_url + path,
                headers=self._headers(accept),
            )
            response = await client.send(
                upstream_request,
                stream=True,
            )
            yield response
        finally:
            if response is not None:
                await response.aclose()
            await client.aclose()
