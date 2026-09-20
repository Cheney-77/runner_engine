from __future__ import annotations

import json
import urllib.error
import urllib.request


def call(
    endpoint: str,
    token: str,
    path: str,
    *,
    payload: dict | None = None,
    timeout_s: float = 30.0,
    extra_headers: dict[str, str] | None = None,
) -> dict:
    url = endpoint.rstrip("/") + path
    body = None if payload is None else json.dumps(payload, separators=(",", ":")).encode("utf-8")
    # Use a runner-specific header so OpenSandbox secure-access proxy headers
    # can safely include their own Authorization header.
    headers = {"X-Runner-Agent-Token": token}
    if body is not None:
        headers["Content-Type"] = "application/json"
    if extra_headers:
        headers.update(extra_headers)
    request = urllib.request.Request(
        url,
        data=body,
        headers=headers,
        method="GET" if payload is None else "POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout_s) as response:
            raw = response.read(32 * 1024 * 1024)
            return json.loads(raw or b"{}")
    except urllib.error.HTTPError as exc:
        detail = exc.read(4096).decode("utf-8", "replace")
        raise RuntimeError(f"worker HTTP {exc.code}: {detail}") from exc
