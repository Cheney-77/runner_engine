from __future__ import annotations

from typing import Any

from .model import Codec, ParameterInfo


_CONTENT_NAMES = {
    "content", "payload", "body", "data", "input", "record", "order", "request", "document",
}
_METADATA_NAMES = {
    "filename": "filename",
    "file_name": "filename",
    "mime_type": "mime.type",
    "mimetype": "mime.type",
    "path": "path",
    "uuid": "uuid",
}


def codec_for(parameter: ParameterInfo, *, payload: bool = False) -> str:
    annotation = (parameter.annotation or "").replace(" ", "").lower()
    name = parameter.name.lower()

    if "bytes" in annotation or "bytearray" in annotation:
        return Codec.BYTES.value
    if "dict" in annotation or "list" in annotation or "json" in name or name in {"record", "order", "document"}:
        return Codec.JSON.value
    if "str" in annotation:
        return Codec.TEXT.value
    return Codec.BYTES.value if payload else Codec.TEXT.value


def parameter_suggestions(
    parameter: ParameterInfo,
    *,
    index: int,
    constructor: bool = False,
) -> list[dict[str, Any]]:
    suggestions: list[dict[str, Any]] = []
    name = parameter.name.lower()

    if not constructor and (name in _CONTENT_NAMES or index == 0):
        suggestions.append({
            "source": "input.payload",
            "codec": codec_for(parameter, payload=True),
            "confidence": 0.95 if name in _CONTENT_NAMES else 0.60,
        })

    if not constructor and name in _METADATA_NAMES:
        suggestions.append({
            "source": "input.metadata",
            "codec": "text",
            "metadataKey": _METADATA_NAMES[name],
            "confidence": 0.90,
        })

    parameter_suggestion = {
        "source": "operator.parameter",
        "codec": codec_for(parameter),
        "parameterKey": parameter.name,
        "parameterDisplayName": parameter.name.replace("_", " ").title(),
        "parameterRequired": parameter.required,
        "parameterHasDefault": parameter.has_default,
        "confidence": 0.70 if constructor or parameter.has_default else 0.50,
    }
    if parameter.has_default:
        parameter_suggestion["parameterDefault"] = parameter.default_value

    suggestions.append(parameter_suggestion)
    return sorted(suggestions, key=lambda item: -item["confidence"])
