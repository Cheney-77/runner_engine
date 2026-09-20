from __future__ import annotations

import hashlib
import os
import re
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from packaging.requirements import InvalidRequirement, Requirement

from .backends.models import NativeTargetPlatform


_PIN = re.compile(r"^\s*([A-Za-z0-9_.-]+)==([^\s;\\]+)")


class EdgeDependencyResolutionError(RuntimeError):
    def __init__(self, message: str, *, stderr: str = "", stdout: str = ""):
        super().__init__(message)
        self.stderr = stderr[-8192:]
        self.stdout = stdout[-4096:]


@dataclass(frozen=True)
class EdgeDependencyResolution:
    direct_requirements: tuple[str, ...]
    lock_text: str

    @property
    def lock_sha256(self) -> str:
        return hashlib.sha256(self.lock_text.encode("utf-8")).hexdigest()

    @property
    def package_count(self) -> int:
        count = 0
        for line in self.lock_text.splitlines():
            if not line.strip() or line.lstrip().startswith(("#", "--")) or line[:1].isspace():
                continue
            if _PIN.match(line):
                count += 1
        return count


def normalize_requirements(values: Iterable[str]) -> tuple[str, ...]:
    result: set[str] = set()

    for raw in values:
        value = raw.strip()
        if not value:
            continue
        if "\n" in value or "\r" in value or value.startswith("-"):
            raise ValueError(f"unsupported edge requirement: {raw!r}")

        try:
            requirement = Requirement(value)
        except InvalidRequirement as exc:
            raise ValueError(f"invalid edge requirement: {raw!r}") from exc

        if requirement.url is not None:
            raise ValueError("URL/VCS/path dependencies are not supported for edge native publishing")

        result.add(str(requirement))

    return tuple(sorted(result, key=str.lower))


class EdgeDependencyResolver:
    def __init__(
        self,
        *,
        uv_default_index: str | None = None,
        require_binary: bool = True,
    ):
        self.uv_default_index = uv_default_index
        self.require_binary = require_binary

    def _env(self) -> dict[str, str]:
        env = os.environ.copy()
        if self.uv_default_index:
            env["UV_DEFAULT_INDEX"] = self.uv_default_index
        return env

    def resolve(
        self,
        requirements: Iterable[str],
        *,
        target: NativeTargetPlatform,
    ) -> EdgeDependencyResolution:
        normalized = normalize_requirements(requirements)

        if not normalized:
            return EdgeDependencyResolution(direct_requirements=(), lock_text="")

        with tempfile.TemporaryDirectory(prefix="mpr-edge-resolve-") as tmp:
            root = Path(tmp)
            source = root / "requirements.in"
            output = root / "requirements.lock"
            source.write_text("\n".join(normalized) + "\n", encoding="utf-8")

            command = [
                "uv", "pip", "compile", str(source),
                "-o", str(output),
                "--generate-hashes",
                "--no-header",
                "--no-annotate",
                "--no-config",
                "--python-version", target.python_version,
                "--python-platform", target.uv_python_platform,
            ]
            if self.require_binary:
                command.append("--no-build")

            result = subprocess.run(
                command,
                cwd=root,
                env=self._env(),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )

            if result.returncode != 0:
                raise EdgeDependencyResolutionError(
                    "uv could not resolve a single edge dependency environment",
                    stderr=result.stderr,
                    stdout=result.stdout,
                )

            return EdgeDependencyResolution(
                direct_requirements=normalized,
                lock_text=output.read_text(encoding="utf-8"),
            )

    def materialize(
        self,
        resolution: EdgeDependencyResolution,
        *,
        target: NativeTargetPlatform,
        destination: Path,
    ) -> None:
        destination.mkdir(parents=True, exist_ok=True)
        if not resolution.direct_requirements:
            return

        with tempfile.TemporaryDirectory(prefix="mpr-edge-materialize-") as tmp:
            root = Path(tmp)
            lock_file = root / "requirements.lock"
            lock_file.write_text(resolution.lock_text, encoding="utf-8")

            command = [
                "uv", "pip", "install",
                "-r", str(lock_file),
                "--target", str(destination),
                "--no-config",
                "--require-hashes",
                "--python-version", target.python_version,
                "--python-platform", target.uv_python_platform,
            ]
            if self.require_binary:
                command.append("--no-build")

            result = subprocess.run(
                command,
                cwd=root,
                env=self._env(),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )

            if result.returncode != 0:
                raise EdgeDependencyResolutionError(
                    "uv resolved the environment but could not materialize target-platform dependencies",
                    stderr=result.stderr,
                    stdout=result.stdout,
                )
