from __future__ import annotations

import hashlib
import json
from typing import Any


def normalize_edge_identity(value: Any) -> dict[str, str]:
    """Normalize the caller-selected edge machine; this service keeps no device registry."""
    if not isinstance(value, dict):
        raise ValueError("nifi_native requires options.edgeIdentity with tokenPair and edgeName")

    result = {}
    for field, alias in (("tokenPair", "token_pair"), ("edgeName", "edge_name")):
        raw = value.get(field, value.get(alias))
        if not isinstance(raw, str):
            raise ValueError(f"edgeIdentity.{field} must be a non-empty string")
        text = raw.strip()
        if not text or len(text) > 128 or any(ord(char) < 32 or ord(char) == 127 for char in text):
            raise ValueError(f"edgeIdentity.{field} must contain 1-128 visible characters")
        result[field] = text
    return result


def edge_directory_name(token_pair: str, edge_name: str) -> str:
    """Stable, path-safe directory for the pair; neither identifier becomes a path segment."""
    identity = json.dumps([token_pair, edge_name], ensure_ascii=False, separators=(",", ":"))
    return "edge-" + hashlib.sha256(identity.encode("utf-8")).hexdigest()[:20]
