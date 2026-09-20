from pathlib import Path
import pytest

from runner_engine.catalog import Catalog
from runner_engine.publisher import OperatorPublisher


@pytest.fixture
def published(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    (project / "operator.yaml").write_text(
        """
name: test
entrypoint: main:process
input_attributes: [filename]
output_attributes: [mime.type]
""".strip(),
        encoding="utf-8",
    )
    (project / "main.py").write_text(
        """
def process(content, attributes, parameters):
    return {
        "content": content.upper(),
        "attributes": {"mime.type": "text/plain", "secret.out": "drop-me"},
    }
""".strip(),
        encoding="utf-8",
    )
    catalog = Catalog(tmp_path / "catalog")
    release = OperatorPublisher(catalog).publish(
        project,
        runtime_image="example.invalid/python@sha256:" + "a" * 64,
    )
    return project, catalog, release
