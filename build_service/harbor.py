from __future__ import annotations

import base64
import json
import os
import ssl
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass


class HarborError(RuntimeError):
    pass


class HarborConflict(HarborError):
    pass


@dataclass(frozen=True)
class HarborArtifactRef:
    registry: str
    project: str
    repository: str
    digest: str


def parse_image_ref(image_ref: str) -> HarborArtifactRef:
    if "@" not in image_ref:
        raise ValueError("image_ref must be pinned by digest")

    repository_ref, digest = image_ref.rsplit("@", 1)
    if not digest.startswith("sha256:") or len(digest) != 71:
        raise ValueError("image_ref must contain a sha256 digest")

    parts = repository_ref.split("/")
    if len(parts) < 3:
        raise ValueError(
            "image_ref must look like registry/project/repository@sha256:..."
        )

    registry = parts[0]
    project = parts[1]
    repository = "/".join(parts[2:])
    if not registry or not project or not repository:
        raise ValueError("image_ref contains an empty registry/project/repository component")

    return HarborArtifactRef(
        registry=registry,
        project=project,
        repository=repository,
        digest=digest,
    )


class HarborClient:
    """Minimal Harbor v2 API client owned by Build Service."""

    def __init__(
        self,
        base_url: str,
        *,
        username: str,
        password: str,
        expected_registry: str = "",
        ca_file: str | None = None,
        insecure: bool = False,
        timeout_seconds: float = 20.0,
    ):
        if not base_url:
            raise ValueError("Harbor base_url is required")
        if not username or not password:
            raise ValueError("Harbor credentials are required")

        self.base_url = base_url.rstrip("/")
        self.username = username
        self.password = password
        self.expected_registry = expected_registry.strip()
        self.timeout_seconds = timeout_seconds

        if insecure:
            self.ssl_context = ssl._create_unverified_context()
        else:
            self.ssl_context = ssl.create_default_context(cafile=ca_file)

    @classmethod
    def from_env(cls) -> "HarborClient | None":
        base_url = os.environ.get("BUILD_HARBOR_URL", "").strip()
        username = os.environ.get("BUILD_HARBOR_USERNAME", "").strip()
        password = os.environ.get("BUILD_HARBOR_PASSWORD", "")

        if not base_url and not username and not password:
            return None
        if not base_url or not username or not password:
            raise RuntimeError(
                "BUILD_HARBOR_URL, BUILD_HARBOR_USERNAME and BUILD_HARBOR_PASSWORD "
                "must be configured together"
            )

        return cls(
            base_url,
            username=username,
            password=password,
            expected_registry=os.environ.get("BUILD_HARBOR_REGISTRY", ""),
            ca_file=os.environ.get("BUILD_HARBOR_CA_FILE") or None,
            insecure=os.environ.get("BUILD_HARBOR_INSECURE", "").lower()
            in {"1", "true", "yes", "on"},
        )

    def _authorization(self) -> str:
        raw = f"{self.username}:{self.password}".encode("utf-8")
        return "Basic " + base64.b64encode(raw).decode("ascii")

    @staticmethod
    def _repository_path(repository: str) -> str:
        once = urllib.parse.quote(repository, safe="")
        return urllib.parse.quote(once, safe="")

    def _artifact_url(self, ref: HarborArtifactRef) -> str:
        project = urllib.parse.quote(ref.project, safe="")
        repository = self._repository_path(ref.repository)
        digest = urllib.parse.quote(ref.digest, safe="")
        return (
            f"{self.base_url}/api/v2.0/projects/{project}"
            f"/repositories/{repository}/artifacts/{digest}"
        )

    def _request(self, method: str, url: str) -> tuple[int, bytes]:
        request = urllib.request.Request(
            url,
            method=method,
            headers={
                "Accept": "application/json",
                "Authorization": self._authorization(),
            },
        )

        try:
            with urllib.request.urlopen(
                request,
                timeout=self.timeout_seconds,
                context=self.ssl_context,
            ) as response:
                return response.status, response.read(4 * 1024 * 1024)
        except urllib.error.HTTPError as exc:
            body = exc.read(8192)
            if exc.code == 404:
                return 404, body
            if exc.code == 409:
                raise HarborConflict(
                    "Harbor refused artifact deletion because it is still "
                    f"referenced or protected: {body.decode('utf-8', 'replace')}"
                ) from exc
            raise HarborError(
                f"Harbor API {method} failed with HTTP {exc.code}: "
                f"{body.decode('utf-8', 'replace')}"
            ) from exc
        except (urllib.error.URLError, TimeoutError) as exc:
            raise HarborError(f"Harbor API unavailable: {exc}") from exc

    def get_artifact(self, image_ref: str) -> dict | None:
        ref = parse_image_ref(image_ref)
        self._verify_registry(ref)
        status, body = self._request("GET", self._artifact_url(ref))
        if status == 404:
            return None

        try:
            value = json.loads(body or b"{}")
        except json.JSONDecodeError as exc:
            raise HarborError("Harbor returned invalid JSON") from exc

        if not isinstance(value, dict):
            raise HarborError("Harbor artifact response must be an object")
        return value

    def delete_artifact(self, image_ref: str) -> dict:
        ref = parse_image_ref(image_ref)
        self._verify_registry(ref)
        status, _ = self._request("DELETE", self._artifact_url(ref))
        return {
            "deleted": status != 404,
            "alreadyMissing": status == 404,
            "project": ref.project,
            "repository": ref.repository,
            "digest": ref.digest,
        }

    def _verify_registry(self, ref: HarborArtifactRef) -> None:
        if self.expected_registry and ref.registry != self.expected_registry:
            raise HarborError(
                f"image registry {ref.registry!r} does not match configured "
                f"BUILD_HARBOR_REGISTRY {self.expected_registry!r}"
            )
