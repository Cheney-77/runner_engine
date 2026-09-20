ARG PYTHON_IMAGE=python:3.12-slim
FROM ${PYTHON_IMAGE}

ARG RUNNER_UID=10001
ARG RUNNER_GID=10001
ARG RUNNER_USER=runner

RUN groupadd -g ${RUNNER_GID} ${RUNNER_USER} \
 && useradd -u ${RUNNER_UID} -g ${RUNNER_GID} -M -s /usr/sbin/nologin ${RUNNER_USER} \
 && mkdir -p /opt/runner/releases \
 && chown root:root /opt/runner /opt/runner/releases \
 && chmod 0755 /opt/runner /opt/runner/releases

WORKDIR /opt/runner/app
COPY runner_engine ./runner_engine
COPY pyproject.toml ./

ENV PYTHONPATH=/opt/runner/app
ENTRYPOINT ["python", "-m", "runner_engine.worker.agent"]