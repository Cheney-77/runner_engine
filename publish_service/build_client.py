from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Any, Callable, Iterable
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen


class BuildServiceClientError(RuntimeError):
    pass


ProgressCallback = Callable[
    [str, str, dict[str, Any] | None],
    None,
]


@dataclass(frozen=True)
class BuildServiceClient:
    base_url: str
    timeout_seconds: int = 1800
    poll_interval_seconds: float = 1.0

    @staticmethod
    def _emit(
        progress: ProgressCallback | None,
        stage: str,
        message: str,
        detail: dict[str, Any] | None = None,
    ) -> None:
        if progress is not None:
            progress(stage, message, detail)

    @staticmethod
    def _read_sse(lines: Iterable[bytes]):
        event_name = "message"
        data_lines: list[str] = []

        for raw in lines:
            line = raw.decode("utf-8", errors="replace").rstrip("\r\n")

            if not line:
                if data_lines:
                    yield event_name, "\n".join(data_lines)
                event_name = "message"
                data_lines = []
                continue

            if line.startswith(":"):
                continue
            if line.startswith("event:"):
                event_name = line[6:].strip() or "message"
                continue
            if line.startswith("data:"):
                data_lines.append(line[5:].lstrip())

        if data_lines:
            yield event_name, "\n".join(data_lines)

    def _json_request(
        self,
        method: str,
        path: str,
        *,
        body: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        url = self.base_url.rstrip("/") + path
        data = None
        headers = {"Accept": "application/json"}

        if body is not None:
            data = json.dumps(body, separators=(",", ":")).encode("utf-8")
            headers["Content-Type"] = "application/json"

        request = Request(url, data=data, method=method, headers=headers)
        try:
            with urlopen(request, timeout=self.timeout_seconds) as response:
                return json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise BuildServiceClientError(
                f"Build Service returned HTTP {exc.code}: {detail}"
            ) from exc
        except URLError as exc:
            raise BuildServiceClientError(
                f"Build Service is unavailable: {exc}"
            ) from exc

    def _resolve_stream(
        self,
        requirements: list[str],
        progress: ProgressCallback,
    ) -> dict[str, Any] | None:
        url = (
            self.base_url.rstrip("/")
            + "/v1/runtime-environments/resolve/events"
        )
        body = json.dumps(
            {"requirements": requirements},
            separators=(",", ":"),
        ).encode("utf-8")
        request = Request(
            url,
            data=body,
            method="POST",
            headers={
                "Content-Type": "application/json",
                "Accept": "text/event-stream",
            },
        )

        try:
            with urlopen(request, timeout=self.timeout_seconds) as response:
                for event_name, raw_data in self._read_sse(response):
                    if not raw_data:
                        continue

                    try:
                        payload = json.loads(raw_data)
                    except json.JSONDecodeError as exc:
                        raise BuildServiceClientError(
                            "Build Service returned malformed SSE JSON"
                        ) from exc

                    if event_name == "progress":
                        stage = str(payload.get("stage") or "BUILDING_RUNTIME")
                        message = str(
                            payload.get("message")
                            or "Build Service is preparing the runtime"
                        )
                        detail = payload.get("detail")
                        progress(
                            stage,
                            message,
                            detail if isinstance(detail, dict) else None,
                        )
                        continue

                    if event_name == "result":
                        if not isinstance(payload, dict):
                            raise BuildServiceClientError(
                                "Build Service result event is not an object"
                            )
                        return payload

                    if event_name == "error":
                        message = (
                            payload.get("message")
                            if isinstance(payload, dict)
                            else None
                        )
                        raise BuildServiceClientError(
                            str(message or "Build Service progress stream failed")
                        )

                raise BuildServiceClientError(
                    "Build Service progress stream ended without a result"
                )
        except HTTPError as exc:
            if exc.code in {404, 405}:
                return None
            detail = exc.read().decode("utf-8", errors="replace")
            raise BuildServiceClientError(
                f"Build Service returned HTTP {exc.code}: {detail}"
            ) from exc
        except URLError as exc:
            raise BuildServiceClientError(
                f"Build Service is unavailable: {exc}"
            ) from exc

    def _wait_until_terminal(
        self,
        runtime: dict[str, Any],
        progress: ProgressCallback | None,
    ) -> dict[str, Any]:
        if runtime.get("status") in {"READY", "FAILED"}:
            return runtime

        env_key = runtime.get("envKey")
        if not env_key:
            return runtime

        deadline = time.monotonic() + self.timeout_seconds
        last_status = None

        while time.monotonic() < deadline:
            current = self._json_request(
                "GET",
                f"/v1/runtime-environments/{quote(str(env_key), safe='')}",
            )
            status = current.get("status")

            if status != last_status:
                if status == "PENDING":
                    self._emit(
                        progress,
                        "WAITING_RUNTIME",
                        "运行时环境已创建，等待构建任务开始",
                        {"envKey": env_key},
                    )
                elif status == "BUILDING":
                    self._emit(
                        progress,
                        "BUILDING_RUNTIME",
                        "正在构建并推送 Runner Runtime 镜像",
                        {"envKey": env_key},
                    )
                elif status == "VERIFYING":
                    self._emit(
                        progress,
                        "VERIFYING_RUNTIME",
                        "正在验证 Runner Runtime 镜像中的依赖",
                        {"envKey": env_key},
                    )
                elif status == "READY":
                    self._emit(
                        progress,
                        "RUNTIME_READY",
                        "Runner Runtime 环境已就绪",
                        {"envKey": env_key},
                    )
                elif status == "FAILED":
                    self._emit(
                        progress,
                        "RUNTIME_FAILED",
                        "Runner Runtime 环境构建失败",
                        {
                            "envKey": env_key,
                            "error": current.get("error"),
                        },
                    )
                last_status = status

            if status in {"READY", "FAILED"}:
                return current

            time.sleep(max(0.1, self.poll_interval_seconds))

        raise BuildServiceClientError(
            f"Timed out waiting for Build Service runtime env_key={env_key}"
        )

    def resolve(
        self,
        requirements: list[str],
        *,
        progress: ProgressCallback | None = None,
    ) -> dict[str, Any]:
        if progress is None:
            runtime = self._json_request(
                "POST",
                "/v1/runtime-environments/resolve",
                body={"requirements": requirements},
            )
            return self._wait_until_terminal(runtime, None)

        runtime = self._resolve_stream(requirements, progress)
        if runtime is None:
            self._emit(
                progress,
                "RESOLVING_RUNTIME",
                "Build Service 未提供进度流，已降级为普通 Resolve",
                {"degraded": True},
            )
            runtime = self._json_request(
                "POST",
                "/v1/runtime-environments/resolve",
                body={"requirements": requirements},
            )

        return self._wait_until_terminal(runtime, progress)
