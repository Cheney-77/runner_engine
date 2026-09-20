"""Optional recovery through EXISTING Build Service retry endpoint only.

Unlike deleting a Docker image or editing build.runtime_environments directly,
this calls the owning Build Service's documented operation. Build still
enforces its own state transitions and idempotency/concurrency rules.
"""
from __future__ import annotations

from urllib.parse import quote, urlsplit
import httpx


class RetryUnavailable(Exception):
    pass


class RetryRejected(Exception):
    def __init__(self, status: int):
        self.status = status
        super().__init__("Build Service rejected retry; consult Build Service logs.")


class RetryClient:
    def __init__(self,url="",token="",transport=None):
        self.url=url.rstrip("/")
        self.token=token
        self.transport=transport
        if self.url:
            parts=urlsplit(self.url)
            if (parts.scheme not in ("http","https") or not parts.hostname
                or parts.username or parts.password or parts.path not in ("","/")
                or parts.query or parts.fragment):
                raise ValueError("ADMIN_BUILD_SERVICE_URL must be an http(s) origin")

    @property
    def enabled(self):
        return bool(self.url)

    async def retry(self, env_key):
        if not self.enabled:
            raise RetryUnavailable("Set ADMIN_BUILD_SERVICE_URL to enable Build retry")
        headers={"Accept":"application/json"}
        if self.token:
            headers["Authorization"]=f"Bearer {self.token}"
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(40,connect=5),
            trust_env=False,follow_redirects=False,
            transport=self.transport,
        ) as client:
            try:
                response=await client.post(
                    self.url + f"/v1/runtime-environments/{quote(env_key,safe='')}/retry",
                    headers=headers,
                )
            except httpx.HTTPError as exc:
                raise RetryUnavailable("Build Service retry endpoint unavailable") from exc
        if not 200 <= response.status_code < 300:
            raise RetryRejected(response.status_code)
        try:
            content=response.json()
        except ValueError:
            content={}
        if not isinstance(content,dict):
            content={}
        # Do not proxy arbitrary Build error/output bodies to a browser.
        return {
            "accepted":True,"httpStatus":response.status_code,
            "envKey":str(content.get("envKey",env_key))[:64],
            "status":str(content.get("status","accepted"))[:36],
        }
