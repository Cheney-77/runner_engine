"""OpenSandbox lifecycle collector, optionally querying approved execd metrics.

Only GET calls; no sandbox execution, creation, stopping or Docker socket.
The official lifecycle API returns current state, createdAt and expiresAt.
Resource usage is obtained separately from execd GET /metrics, if approved.
"""
from __future__ import annotations

import asyncio
import logging
import math
import re
from datetime import datetime, timezone
from urllib.parse import quote, urlsplit, urlunsplit

import httpx

from .settings import Settings

log = logging.getLogger(__name__)
SANDBOX_ID = re.compile(r"^[A-Za-z0-9_-]{1,200}$")
HEADER_NAME = re.compile(r"^[A-Za-z0-9-]{1,64}$")
FORBIDDEN_HEADERS = {
    "host", "cookie", "connection", "content-length",
    "transfer-encoding", "proxy-authorization", "proxy-connection",
    "forwarded", "x-forwarded-host", "x-forwarded-for",
    "x-forwarded-proto",
}
VALID_STATES = {"Running", "Paused", "Pending", "Pausing", "Resuming", "Stopping"}


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def parse_date(value) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return result.astimezone(timezone.utc) if result.tzinfo else None
    except ValueError:
        return None


def number(value, minimum=0, maximum=1e12) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (float, int)):
        return None
    f = float(value)
    if not math.isfinite(f) or not minimum <= f <= maximum:
        return None
    return f


def endpoint_for_metrics(raw: str, origins: frozenset[str]) -> str:
    if not raw or not isinstance(raw, str):
        raise ValueError("Sandbox metrics endpoint not supplied")
    parsed = urlsplit(raw)
    if not parsed.scheme and not parsed.netloc:
        # The OpenSandbox Endpoint schema has examples without a scheme.
        # Infer only when an explicitly approved origin matches its host:port
        # uniquely; otherwise do NOT guess the protocol.
        hostless = urlsplit("//" + raw)
        matching = [
            urlsplit(origin) for origin in origins
            if urlsplit(origin).netloc.lower() == hostless.netloc.lower()
        ]
        if len(matching) != 1:
            raise ValueError("Scheme-less metrics endpoint has no unique approved origin")
        parsed = urlsplit(matching[0].scheme + "://" + raw)
    if (
        parsed.scheme not in ("http", "https")
        or not parsed.hostname
        or parsed.username or parsed.password or parsed.fragment
    ):
        raise ValueError("Sandbox metrics endpoint must be a valid http(s) URL")
    origin = f"{parsed.scheme}://{parsed.netloc.lower()}"
    if origin not in origins:
        raise ValueError("Sandbox metrics endpoint origin is not approved")
    if parsed.path.endswith("/metrics"):
        path = parsed.path
    else:
        path = parsed.path.rstrip("/") + "/metrics"
    return urlunsplit((parsed.scheme, parsed.netloc, path, parsed.query, ""))


class SandboxReader:
    def __init__(
        self,
        settings: Settings,
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        self.settings = settings
        self.transport = transport

    def _headers(self):
        headers = {"Accept": "application/json"}
        if self.settings.sandbox_api_key:
            headers["OPEN-SANDBOX-API-KEY"] = self.settings.sandbox_api_key
        return headers

    async def _get(self, client: httpx.AsyncClient, url: str,
                   *, params=None, headers=None) -> dict:
        response = await client.get(url, params=params, headers=headers or self._headers())
        response.raise_for_status()
        data = response.json()
        if not isinstance(data, dict):
            raise ValueError("OpenSandbox returned an invalid JSON object")
        return data

    def _record(self, item: dict, timestamp: datetime) -> dict | None:
        sid = item.get("id")
        if not isinstance(sid, str) or not SANDBOX_ID.fullmatch(sid):
            return None
        state = item.get("status")
        status = state.get("state") if isinstance(state, dict) else None
        created = parse_date(item.get("createdAt"))
        expires = parse_date(item.get("expiresAt"))
        age = max(0, round((timestamp-created).total_seconds())) if created else None
        ttl = round((expires-timestamp).total_seconds()) if expires else None
        image = item.get("image")
        image_ref = image.get("uri") if isinstance(image, dict) else None
        return {
            "id": sid, "state": status or "Unknown",
            "createdAt": item.get("createdAt") if created else None,
            "ageSeconds": age,
            "expiresAt": item.get("expiresAt") if expires else None,
            "ttlSeconds": ttl,
            "image": str(image_ref)[:180] if image_ref else None,
            "snapshotId": str(item.get("snapshotId"))[:128]
            if item.get("snapshotId") else None,
            "resources": None,
            "resourceStatus": "disabled",
            "resourceNote": "Enable execd metrics and approve the endpoint origin.",
        }

    async def _metrics(self, client: httpx.AsyncClient, sid: str) -> tuple[dict | None, str, str]:
        try:
            endpoint_url = (
                self.settings.sandbox_url
                + f"/sandboxes/{quote(sid, safe='')}/endpoints/44772"
            )
            endpoint = await self._get(
                client, endpoint_url,
                params={"use_server_proxy": "true"},
            )
            url = endpoint_for_metrics(
                endpoint.get("endpoint"), self.settings.allowed_origins
            )
            headers = {"Accept": "application/json"}
            provided = endpoint.get("headers") or {}
            if not isinstance(provided, dict):
                raise ValueError("Endpoint headers have invalid format")
            for key, value in provided.items():
                if (
                    not isinstance(key, str) or not HEADER_NAME.fullmatch(key)
                    or key.lower() in FORBIDDEN_HEADERS
                    or not isinstance(value, str)
                    or len(value) > 4096 or "\r" in value or "\n" in value
                ):
                    raise ValueError("Endpoint returned an unsafe header")
                headers[key] = value
            # The server-proxied URL may itself require Lifecycle auth.
            # Forward the key ONLY to the same exact Lifecycle origin and
            # a matching server-proxy route for this sandbox/execd port.
            target = urlsplit(url)
            lifecycle = urlsplit(self.settings.sandbox_url)
            if (
                self.settings.sandbox_api_key
                and f"{target.scheme}://{target.netloc.lower()}"
                    == f"{lifecycle.scheme}://{lifecycle.netloc.lower()}"
                and f"/sandboxes/{sid}/port/44772" in target.path
            ):
                headers["OPEN-SANDBOX-API-KEY"] = self.settings.sandbox_api_key
            raw = await self._get(client, url, headers=headers)
            metrics = {
                "cpuPct": number(raw.get("cpu_used_pct"), maximum=100000),
                "cpuCores": number(raw.get("cpu_count"), maximum=100000),
                "memUsedMiB": number(raw.get("mem_used_mib")),
                "memTotalMiB": number(raw.get("mem_total_mib")),
                "sampledAt": raw.get("timestamp")
                if isinstance(raw.get("timestamp"), (float, int)) else None,
                "source": "opensandbox-execd",
            }
            if metrics["cpuPct"] is None or metrics["memUsedMiB"] is None:
                raise ValueError("Incomplete OpenSandbox execd metrics")
            return metrics, "ok", ""
        except (httpx.HTTPError, ValueError, TypeError) as exc:
            # Never leak endpoint URL or response bodies (could contain
            # auth tokens) to browser or logs.
            log.info("Sandbox resource metric unavailable for a sandbox (%s)",
                     type(exc).__name__)
            return None, "unavailable", (
                "Resource data unavailable. Check approved origin, endpoint "
                "credentials, proxy support and execd /metrics."
            )

    async def report(self) -> dict:
        if not self.settings.sandbox_url:
            return self._empty("unconfigured", "Set OBS_SANDBOX_URL.")
        timestamp = utcnow()
        try:
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(8, connect=3),
                trust_env=False, follow_redirects=False,
                transport=self.transport,
            ) as client:
                results: list[dict] = []
                total = None
                pages = 0
                page_size = min(
                    self.settings.sandbox_page_size,
                    self.settings.sandbox_max_items,
                )
                while len(results) < self.settings.sandbox_max_items:
                    pages += 1
                    data = await self._get(
                        client, self.settings.sandbox_url + "/sandboxes",
                        params=[
                            ("state", "Running"),
                            ("page", pages),
                            ("pageSize", page_size),
                        ],
                    )
                    pagination = data.get("pagination")
                    items = data.get("items")
                    if not isinstance(pagination, dict) or not isinstance(items, list):
                        raise ValueError("Unsupported OpenSandbox list response shape")
                    listed_total = pagination.get("totalItems")
                    if isinstance(listed_total, int) and listed_total >= 0:
                        total = listed_total
                    available = self.settings.sandbox_max_items - len(results)
                    results.extend(
                        item for item in items[:available]
                        if isinstance(item, dict)
                    )
                    if (
                        not pagination.get("hasNextPage")
                        or not items or pages >= 20
                    ):
                        break

                records = [
                    record for item in results
                    if (record := self._record(item, timestamp)) is not None
                ]
                limited = (total is not None and total > len(records))
                # A screenshot of the running sandboxes is still available
                # when per-sandbox metrics cannot be configured or collected.
                requested = 0
                if self.settings.execd_metrics and self.settings.allowed_origins:
                    sem = asyncio.Semaphore(6)
                    sampled = records[:self.settings.execd_sample_limit]
                    requested = len(sampled)
                    for record in records:
                        record["resourceStatus"] = "not_sampled"
                        record["resourceNote"] = "Resource sampling limit reached."
                    for record in sampled:
                        record["resourceStatus"] = "pending"
                        record["resourceNote"] = "Awaiting execd metrics."

                    async def fill(record: dict):
                        async with sem:
                            metrics, status, note = await self._metrics(
                                client, record["id"]
                            )
                            record["resources"] = metrics
                            record["resourceStatus"] = status
                            record["resourceNote"] = note

                    try:
                        await asyncio.wait_for(
                            asyncio.gather(*(fill(record) for record in sampled)),
                            timeout=18,
                        )
                    except asyncio.TimeoutError:
                        for record in sampled:
                            if record["resourceStatus"] == "pending":
                                record["resourceStatus"] = "unavailable"
                                record["resourceNote"] = (
                                    "Resource sampling exceeded time budget."
                                )
                elif self.settings.execd_metrics:
                    for record in records:
                        record["resourceStatus"] = "unconfigured"
                        record["resourceNote"] = (
                            "Set OBS_EXECD_ALLOWED_ORIGINS to enable resource readings."
                        )
                return {
                    "status": "ok",
                    "message": "",
                    "sampledAt": timestamp.isoformat(),
                    "runningTotal": total,
                    "returned": len(records),
                    "truncated": limited,
                    "pagesFetched": pages,
                    "metricsEnabled": (
                        self.settings.execd_metrics
                        and bool(self.settings.allowed_origins)
                    ),
                    "resourceMeasured": sum(
                        r["resourceStatus"] == "ok" for r in records
                    ),
                    "resourceRequested": requested,
                    "samplingLimit": self.settings.execd_sample_limit,
                    "sandboxes": records,
                }
        except (httpx.HTTPError, ValueError, TypeError) as exc:
            log.warning("Sandbox list failed (%s)", type(exc).__name__)
            return self._empty(
                "error",
                "OpenSandbox is unavailable. Check lifecycle URL, API key and network.",
            )

    @staticmethod
    def _empty(status: str, message: str):
        return {
            "status": status, "message": message, "sampledAt": None,
            "runningTotal": None, "returned": 0,
            "truncated": False, "pagesFetched": 0,
            "metricsEnabled": False, "resourceMeasured": 0,
            "resourceRequested": 0, "samplingLimit": None,
            "sandboxes": [],
        }
