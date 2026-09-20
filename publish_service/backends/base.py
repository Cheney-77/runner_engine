from __future__ import annotations

import hashlib
import json
from typing import Any

from operator_authoring.compiler import canonical_json


def backend_variant_key(
    *,
    contract_sha256: str,
    backend: str,
    compiler_version: str,
    options: dict[str, Any],
) -> str:
    payload = {
        "contract_sha256": contract_sha256,
        "backend": backend,
        "compiler_version": compiler_version,
        "options": options,
    }
    return hashlib.sha256(canonical_json(payload)).hexdigest()


def backend_contract_sha256(contract) -> str:
    return hashlib.sha256(canonical_json(contract.model_dump(mode="json"))).hexdigest()
