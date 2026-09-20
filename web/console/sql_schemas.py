"""SQL identifier routing for the three explicitly supplied PostgreSQL DDLs.

No table mapping / dynamic SQL from the browser. Only schema identifiers can
be overridden; table names and the complete SQL remain fixed in code.
"""
from __future__ import annotations

import re

DEFAULT_SCHEMAS = {"publish": "publish", "build": "build", "runner": "runner"}
IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
QUALIFIED = re.compile(r"(?<![A-Za-z0-9_])(?P<service>publish|build|runner)\.(?=[A-Za-z_])")


def valid_schema(value: str) -> str:
    if not isinstance(value, str) or not IDENTIFIER.fullmatch(value):
        raise ValueError("PostgreSQL schema must be a simple SQL identifier")
    return value


def configured_schemas(overrides: dict[str, str] | None = None) -> dict[str, str]:
    result = DEFAULT_SCHEMAS.copy()
    if overrides:
        if set(overrides) - set(result):
            raise ValueError("Unknown PostgreSQL service")
        for service, schema in overrides.items():
            result[service] = valid_schema(schema)
    return result


def physical_table(qualified: str, schemas: dict[str, str]) -> str:
    service, dot, table = qualified.partition(".")
    if not dot or service not in DEFAULT_SCHEMAS or not IDENTIFIER.fullmatch(table):
        raise ValueError("Unexpected table in fixed DDL")
    schema = valid_schema(schemas.get(service, DEFAULT_SCHEMAS[service]))
    # Preserve existing PostgreSQL syntax for the user-supplied default DDL.
    # For overrides quote validated identifiers (handles uppercase names).
    prefix = service if schema == service else '"' + schema + '"'
    return f"{prefix}.{table}"


def render_sql(statement: str, schemas: dict[str, str]) -> str:
    """Translate only hard-coded logical schema qualifiers in trusted SQL."""
    def replace(match: re.Match):
        service = match.group("service")
        schema = valid_schema(schemas.get(service, DEFAULT_SCHEMAS[service]))
        return (service if schema == service else '"' + schema + '"') + "."
    return QUALIFIED.sub(replace, statement)
