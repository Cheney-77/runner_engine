from __future__ import annotations

import json
import os
import subprocess
import tempfile
from pathlib import Path
from typing import Iterable

from packaging.requirements import InvalidRequirement, Requirement

from .model import RuntimeBuildSpec

RUNTIME_DOCKERFILE = r"""
ARG RUNNER_BASE

# ======================================================
# Stage 1
# User dependency build
# ======================================================

FROM ${RUNNER_BASE} AS deps

USER root

RUN mkdir -p /opt/python-deps

COPY requirements.lock \
    /tmp/requirements.lock

RUN python -m pip install \
    --no-cache-dir \
    --target /opt/python-deps \
    -r /tmp/requirements.lock -i http://pip3.inovance.local/repository/group-pypi/simple --trusted-host pip3.inovance.local


# ======================================================
# Stage 2
# Clean trusted base
# ======================================================

FROM ${RUNNER_BASE}

USER root

COPY --from=deps \
    --chown=root:root \
    /opt/python-deps \
    /opt/python-deps

RUN chmod -R a+rX /opt/python-deps \
 && chmod -R go-w /opt/python-deps

ENV RUNNER_DEPENDENCY_ROOT=/opt/python-deps

# Important:
# Trusted Agent itself should NOT depend on user dependency PYTHONPATH.
ENV PYTHONPATH=/opt/runner/app
""".lstrip()


def normalize_requirements(values: Iterable[str]) -> list[str]:
    result: set[str] = set()

    for raw in values:
        value = raw.strip()
        if not value:
            continue
        if "\n" in value or "\r" in value or value.startswith("-"):
            raise ValueError(f"unsupported requirement: {raw!r}")

        try:
            requirement = Requirement(value)
        except InvalidRequirement as exc:
            raise ValueError(f"invalid requirement: {raw!r}") from exc

        if requirement.url is not None:
            raise ValueError("URL/VCS/path dependencies are not allowed")
        result.add(str(requirement))

    return sorted(result, key=str.lower)


def lock_requirements(
    requirements: Iterable[str],
    *,
    python_version: str,
    python_platform: str,
    uv_default_index: str | None = None,
) -> str:
    normalized = normalize_requirements(requirements)

    with tempfile.TemporaryDirectory(prefix="mpr-lock-") as tmp:
        root = Path(tmp)
        source = root / "requirements.in"
        output = root / "requirements.lock"
        source.write_text("\n".join(normalized) + ("\n" if normalized else ""), encoding="utf-8")

        command = [
            "uv",
            "pip",
            "compile",
            str(source),
            "-o",
            str(output),
            "--generate-hashes",
            "--no-header",
            "--no-annotate",
            "--python-version",
            python_version,
            "--python-platform",
            python_platform,
        ]
        env = os.environ.copy()
        if uv_default_index:
            env["UV_DEFAULT_INDEX"] = uv_default_index

        subprocess.run(command, check=True, cwd=root, env=env)
        return output.read_text(encoding="utf-8")


def build_runtime_image(
    spec: RuntimeBuildSpec,
    *,
    registry_repo: str,
    build_tag: str,
) -> str:
    with tempfile.TemporaryDirectory(prefix="mpr-image-") as tmp:
        root = Path(tmp)
        (root / "Dockerfile").write_text(RUNTIME_DOCKERFILE, encoding="utf-8")
        (root / "requirements.lock").write_text(spec.requirements_lock, encoding="utf-8")
        metadata_file = root / "metadata.json"
        tag = f"{registry_repo}:{build_tag}"

        subprocess.run(
            [
                "docker",
                "buildx",
                "build",
                "--platform",
                spec.platform,
                "--build-arg",
                f"RUNNER_BASE={spec.base_image}",
                "--tag",
                tag,
                "--push",
                "--metadata-file",
                str(metadata_file),
                str(root),
            ],
            check=True,
        )

        metadata = json.loads(metadata_file.read_text(encoding="utf-8"))
        digest = metadata.get("containerimage.digest")
        if not isinstance(digest, str) or not digest.startswith("sha256:"):
            raise RuntimeError("buildx did not return containerimage.digest")
        return f"{registry_repo}@{digest}"


def verify_runtime_image(image_ref: str, expected_packages: dict[str, str]) -> None:
    expected_json = json.dumps(expected_packages, sort_keys=True, separators=(",", ":"))
    script = r'''
import importlib.metadata as md
import json
import re
import sys


def normalize(name):
    return re.sub(r"[-_.]+", "-", name).lower()


expected = json.loads(sys.argv[1])
actual = {}
for dist in md.distributions(path=["/opt/python-deps"]):
    name = dist.metadata.get("Name")
    if name:
        actual[normalize(name)] = dist.version

bad = {name: version for name, version in expected.items() if actual.get(name) != version}
if bad:
    print(json.dumps({"missing_or_mismatched": bad, "actual": actual}))
    raise SystemExit(2)

print(json.dumps({"ok": True, "packages": len(actual)}))
'''

    subprocess.run(
        [
            "docker",
            "run",
            "--rm",
            "--pull=always",
            "--entrypoint",
            "python",
            image_ref,
            "-I",
            "-c",
            script,
            expected_json,
        ],
        check=True,
    )
