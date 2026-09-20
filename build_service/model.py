from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass

from packaging.utils import canonicalize_name

_PIN = re.compile(r"^\s*([A-Za-z0-9_.-]+)==([^\s;\\]+)")


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def parse_locked_packages(lock_text: str) -> dict[str, str]:
    packages: dict[str, str] = {}

    for line in lock_text.splitlines():
        if not line.strip() or line.lstrip().startswith(("#", "--")):
            continue
        if line[:1].isspace():
            continue

        match = _PIN.match(line)
        if not match:
            raise ValueError(f"lock line is not an exact pin: {line!r}")

        name = canonicalize_name(match.group(1))
        version = match.group(2)
        previous = packages.get(name)
        if previous is not None and previous != version:
            raise ValueError(f"{name} has conflicting versions")
        packages[name] = version

    return dict(sorted(packages.items()))


@dataclass(frozen=True)
class RuntimeBuildSpec:
    base_image: str
    python_version: str
    platform: str
    requirements_lock: str
    build_policy_version: str = "1"

    @property
    def lock_sha256(self) -> str:
        return _sha256(self.requirements_lock.encode("utf-8"))

    @property
    def packages(self) -> dict[str, str]:
        return parse_locked_packages(self.requirements_lock)

    @property
    def env_key(self) -> str:
        if "@sha256:" not in self.base_image:
            raise ValueError("base_image must be pinned as repo@sha256:<digest>")

        identity = {
            "schema": 1,
            "base_image": self.base_image,
            "python_version": self.python_version,
            "platform": self.platform,
            "requirements_lock_sha256": self.lock_sha256,
            "build_policy_version": self.build_policy_version,
        }
        canonical = json.dumps(identity, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        return _sha256(canonical)
