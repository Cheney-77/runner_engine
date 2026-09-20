from __future__ import annotations

import json
from pathlib import Path

from .model import SecurityPolicy


def load_policies(path: str | Path) -> dict[str, SecurityPolicy]:
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    policies: dict[str, SecurityPolicy] = {}

    for name, item in raw.items():
        policies[name] = SecurityPolicy(
            name=name,
            sandbox_cluster=item["sandbox_cluster"],
            cpu=str(item.get("cpu", "1")),
            memory=str(item.get("memory", "512Mi")),
            max_timeout_ms=int(item.get("max_timeout_ms", 60_000)),
            network_allow=tuple(item.get("network_allow", [])),
            reuse_sandbox=bool(item.get("reuse_sandbox", True)),
        )

    return policies
