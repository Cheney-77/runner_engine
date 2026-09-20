from runner_engine.publisher import OperatorPublisher


def test_publish_is_deterministic(published):
    project, catalog, first = published
    second = OperatorPublisher(catalog).publish(
        project,
        runtime_image="example.invalid/python@sha256:" + "a" * 64,
    )
    assert first.id == second.id
    assert first.artifact_sha256 == second.artifact_sha256


def test_mutable_runtime_rejected(tmp_path):
    from runner_engine.catalog import Catalog
    project = tmp_path / "p"
    project.mkdir()
    (project / "operator.yaml").write_text("entrypoint: main:process")
    (project / "main.py").write_text("def process(content, attributes, parameters): return content")
    try:
        OperatorPublisher(Catalog(tmp_path / "c")).publish(project, runtime_image="python:3.12")
    except ValueError as exc:
        assert "sha256" in str(exc)
    else:
        raise AssertionError("mutable image tag should be rejected")
