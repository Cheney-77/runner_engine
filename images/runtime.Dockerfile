#ARG RUNNER_BASE
#
## ===== Stage 1: 依赖构建阶段 =====
## 允许不可信依赖安装发生在这里
#FROM ${RUNNER_BASE} AS dependency-builder
#USER root
#COPY requirements.lock /build/requirements.lock
#RUN python -m pip install \
#    --no-cache-dir \
#    --target /opt/python-deps \
#    -r /build/requirements.lock -i http://pip3.inovance.local/repository/group-pypi/simple --trusted-host pip3.inovance.local
#
## ===== Stage 2: 最终镜像，从干净可信的 Base 重新开始 =====
#FROM ${RUNNER_BASE}
#USER root
#COPY --from=dependency-builder \
#    /opt/python-deps \
#    /opt/python-deps
#ENV PYTHONPATH=/opt/python-deps:/opt/runner/app

ARG RUNNER_BASE

# ======================================================
# Stage 1
# User dependency build
# ======================================================

FROM ${RUNNER_BASE} AS deps

USER root

RUN mkdir -p /opt/python-deps

COPY requirements.lock \
    /tmp/requirements.lock

RUN python -m pip install \
    --no-cache-dir \
    --target /opt/python-deps \
    -r /tmp/requirements.lock -i http://pip3.inovance.local/repository/group-pypi/simple --trusted-host pip3.inovance.local


# ======================================================
# Stage 2
# Clean trusted base
# ======================================================

FROM ${RUNNER_BASE}

USER root

COPY --from=deps \
    --chown=root:root \
    /opt/python-deps \
    /opt/python-deps

RUN chmod -R a+rX /opt/python-deps \
 && chmod -R go-w /opt/python-deps

ENV RUNNER_DEPENDENCY_ROOT=/opt/python-deps

# Important:
# Trusted Agent itself should NOT depend on user dependency PYTHONPATH.
ENV PYTHONPATH=/opt/runner/app
