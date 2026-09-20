from __future__ import annotations

import json
from dataclasses import dataclass
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


class BuildServiceClientError(RuntimeError):
    pass


@dataclass(frozen=True)
class BuildServiceClient:
    base_url: str
    timeout_seconds: int = 1800

    def resolve(self, requirements: list[str]) -> dict:
        url = self.base_url.rstrip("/") + "/v1/runtime-environments/resolve"
        body = json.dumps({"requirements": requirements}, separators=(",", ":")).encode("utf-8")
        request = Request(url, data=body, method="POST", headers={"Content-Type": "application/json"})

        try:
            with urlopen(request, timeout=self.timeout_seconds) as response:
                return json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise BuildServiceClientError(f"Build Service returned HTTP {exc.code}: {detail}") from exc
        except URLError as exc:
            raise BuildServiceClientError(f"Build Service is unavailable: {exc}") from exc
