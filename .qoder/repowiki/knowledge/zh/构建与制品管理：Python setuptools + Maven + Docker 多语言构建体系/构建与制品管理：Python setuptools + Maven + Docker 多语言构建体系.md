---
kind: build_system
name: 构建与制品管理：Python setuptools + Maven + Docker 多语言构建体系
category: build_system
scope:
    - '**'
source_files:
    - pyproject.toml
    - requirements.lock
    - images/runner.Dockerfile
    - images/runtime.Dockerfile
    - nifi/pom.xml
    - nifi/managed-python-processors/pom.xml
    - scripts/generate_proto.py
---

## 1. 使用的构建系统

仓库采用**多语言、分层式**的构建方案，没有统一的 Makefile 或 CI 流水线文件（仓库根目录无 `Makefile`、无 `.github/workflows`、无 Jenkinsfile）：

- **Python 侧**：使用 `pyproject.toml` + `setuptools.build_meta`（要求 `setuptools>=70`），通过 `[project.scripts]` 暴露 5 个 CLI 入口：`mpr-publish`、`mpr-agent`、`mpr-runner`、`mpr-build-service`、`mpr-publish-service`。
- **Java/NiFi 侧**：使用 Maven 父 POM（`nifi/pom.xml`，artifactId=`managed-python-nifi-parent`，version=`3.3.0`），聚合 `managed-python-processors` 与 `managed-python-nar` 两个子模块；编译目标 Java 21（`maven.compiler.release=21`），NiFi 版本锁定为 `2.10.0`，gRPC/protobuf 版本在父 POM 中集中声明。
- **容器镜像**：Dockerfile 位于 `images/runner.Dockerfile` 与 `images/runtime.Dockerfile`，分别用于 Runner 进程镜像和用户依赖镜像。
- **Proto 生成**：Python gRPC 代码由 `scripts/generate_proto.py` 生成；Java gRPC 代码由 Maven 插件 `protobuf-maven-plugin:0.6.1` 从 `proto/runner.proto` 自动生成。

## 2. 关键文件

- `pyproject.toml`：Python 包元数据、可选依赖分组（`grpc`/`dev`/`postgres`/`build`/`publish`）、可执行脚本入口、pytest 配置。
- `requirements.lock`：顶层 Python 依赖锁定文件（与 `uv` 配合使用，见 `build`/`publish` optional-dependencies 中的 `uv>=0.8`）。
- `images/runner.Dockerfile`：Runner Agent 镜像，基于 `python:3.12-slim`，以非 root 用户 `runner`（UID/GID=10001）运行，`ENTRYPOINT` 为 `python -m runner_engine.worker.agent`。
- `images/runtime.Dockerfile`：用户依赖镜像，采用两阶段构建：Stage 1 (`deps`) 从 `RUNNER_BASE` 安装 `requirements.lock` 到 `/opt/python-deps`，Stage 2 仅复制已安装的依赖并设置 `RUNNER_DEPENDENCY_ROOT=/opt/python-deps`，同时显式将 `PYTHONPATH=/opt/runner/app`，确保可信 Agent 不依赖用户依赖路径。
- `nifi/pom.xml`：Maven 父工程，集中声明 NiFi 2.10.0、gRPC 1.66.0、protobuf 4.28.2、Java 21 等版本属性。
- `nifi/managed-python-processors/pom.xml`：NiFi Processor 模块，通过 `os-maven-plugin` 自动选择 `protoc` 与 `protoc-gen-grpc-java` 的二进制分类器。
- `scripts/generate_proto.py`：Python 侧 Proto 代码生成脚本。

## 3. 架构与约定

- **版本单一来源**：Python 包版本 `3.3.0` 定义于 `pyproject.toml`；NiFi Java 包版本 `3.3.0` 定义于 `nifi/pom.xml`。两者独立维护，但语义上保持对齐。
- **依赖锁定优先**：Python 侧使用 `requirements.lock`（而非 `requirements.txt`）作为唯一依赖源，Dockerfile 直接 `COPY requirements.lock` 并通过 `pip install -r /tmp/requirements.lock` 安装，保证可复现。
- **安全隔离镜像**：`runtime.Dockerfile` 的两阶段构建刻意把不可信的用户依赖安装放在 Stage 1，最终镜像只包含已编译好的 wheel，且 `PYTHONPATH` 被硬编码为 `/opt/runner/app`，注释明确说明“Trusted Agent itself should NOT depend on user dependency PYTHONPATH”。
- **产物分发方式**：Python 通过 `setuptools` 打包为 wheel/sdist；NiFi 通过 Maven 打包为 NAR/JAR；运行时以 Docker 镜像形式发布，镜像标签策略未在仓库内体现（可能由外部 CI 决定）。
- **Web 子应用**：`web/` 下的 admin、console、observe、app 是独立的 FastAPI 应用，各自有 `requirements.txt`（如 `web/console/requirements.txt`、`web/observe/requirements.txt`），与顶层 `pyproject.toml` 解耦，由上层编排组合部署。

## 4. 约定与约束

- Python 包必须满足 `requires-python = ">=3.11"`（`pyproject.toml` 声明）。
- Runner 镜像必须以非 root 用户 `runner`（UID/GID=10001）运行，且工作目录固定为 `/opt/runner/app`（`runner.Dockerfile` 强制）。
- 用户依赖镜像必须设置环境变量 `RUNNER_DEPENDENCY_ROOT=/opt/python-deps`，Agent 侧据此加载用户依赖（`runtime.Dockerfile` 设定，Runner 侧消费该约定）。
- NiFi 侧 Java 源码必须使用 Java 21 兼容 API（`maven.compiler.release=21`，由 `nifi/pom.xml` 的 `maven-compiler-plugin` 强制执行）。
- gRPC 接口定义位于 `proto/runner.proto`，Python 与 Java 两侧分别通过 `scripts/generate_proto.py` 与 `protobuf-maven-plugin` 同步生成，新增接口需同时更新两侧生成步骤。
- 仓库未包含任何 CI/CD 配置文件（无 GitHub Actions、Jenkinsfile、Makefile），构建与发布流程不在本仓库内实现，而是由外部系统驱动。