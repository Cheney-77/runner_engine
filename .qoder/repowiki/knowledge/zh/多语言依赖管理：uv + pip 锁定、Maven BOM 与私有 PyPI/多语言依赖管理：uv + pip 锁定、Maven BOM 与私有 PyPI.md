---
kind: dependency_management
name: 多语言依赖管理：uv + pip 锁定、Maven BOM 与私有 PyPI
category: dependency_management
scope:
    - '**'
source_files:
    - pyproject.toml
    - requirements.lock
    - images/requirements.lock
    - web/requirements.txt
    - web/console/requirements.txt
    - web/observe/requirements.txt
    - web/observe/requirements-postgres.txt
    - images/runner.Dockerfile
    - images/runtime.Dockerfile
    - nifi/pom.xml
    - nifi/managed-python-processors/pom.xml
    - scripts/generate_proto.py
---

## 1. 使用的系统/方案

仓库同时维护三类依赖，分别由不同工具链管理：

- **Python 应用（runner_engine / build_service / publish_service）**：使用 `pyproject.toml`（PEP 621）声明运行时依赖与可选依赖组（`grpc`、`dev`、`postgres`、`build`、`publish`），构建后端为 `setuptools.build_meta`，要求 Python ≥3.11。通过 `uv pip compile` 生成 `requirements.lock` 作为可复现的锁文件。
- **Web 子应用（web/console、web/observe）**：各自维护独立的 `requirements.txt`，并通过 `-r ../requirements.txt` 继承根 Web 层依赖；Postgres 变体通过 `requirements-postgres.txt` 以 `-r requirements.txt` 叠加 `psycopg[binary]>=3.2,<4`。
- **NiFi Java 适配器**：使用 Maven 多模块项目，父 POM `nifi/pom.xml` 集中声明 `nifi.version=2.10.0`、`grpc.version=1.66.0`、`protobuf.version=4.28.2`、`maven.compiler.release=21`，子模块 `managed-python-processors` 通过 `${...}` 引用这些属性。

没有发现 Poetry、PDM、conda、pipenv 或 `go.mod`/`package.json` 等其它包管理器。

## 2. 关键文件

| 文件 | 作用 |
|---|---|
| `pyproject.toml` | Python 包元数据、依赖与可选依赖组、entry points（`mpr-runner`、`mpr-build-service`、`mpr-publish-service` 等） |
| `requirements.lock` | `uv pip compile` 生成的 Python 依赖锁定快照（numpy/pandas 示例） |
| `images/requirements.lock` | 镜像构建阶段使用的用户依赖锁定快照 |
| `web/requirements.txt` | Web 层基础依赖（fastapi、uvicorn、httpx） |
| `web/console/requirements.txt` | 通过 `-r` 继承 web 层并叠加 observe 的 Postgres 依赖 |
| `web/observe/requirements.txt` / `requirements-postgres.txt` | observe 应用的可选依赖拆分 |
| `images/runner.Dockerfile` | Runner 镜像：复制源码 + `pyproject.toml`，ENTRYPOINT 为 `python -m runner_engine.worker.agent` |
| `images/runtime.Dockerfile` | 运行时镜像：在独立 stage 中用 `pip install -r requirements.lock -i http://pip3.inovance.local/repository/group-pypi/simple --trusted-host pip3.inovance.local` 预装依赖到 `/opt/python-deps`，再 COPY 进最终镜像，设置 `RUNNER_DEPENDENCY_ROOT=/opt/python-deps` |
| `nifi/pom.xml` | Maven 父 POM，集中版本属性与模块列表 |
| `nifi/managed-python-processors/pom.xml` | NiFi Processor 模块，依赖 nifi-api、grpc-*、protobuf-java，并通过 protobuf-maven-plugin 从 `../../proto` 编译 gRPC 代码 |
| `scripts/generate_proto.py` | 通过 `grpc_tools.protoc` 生成 Python gRPC 存根（见 `runner_engine/generated/`） |

## 3. 架构与约定

- **分层依赖隔离**：Runner 自身仅依赖 `PyYAML>=6.0`；所有用户 Python 依赖（如 pandas/numpy）通过 `runtime.Dockerfile` 在独立 stage 中安装到 `/opt/python-deps`，并以 `RUNNER_DEPENDENCY_ROOT` 暴露给运行时加载。Dockerfile 注释明确说明“不可信依赖安装发生在这里”，可信 Agent 不将其加入 PYTHONPATH。
- **私有 PyPI**：镜像构建阶段通过 `-i http://pip3.inovance.local/repository/group-pypi/simple --trusted-host pip3.inovance.local` 指向内部私有源，而非官方 PyPI。
- **可选依赖按角色拆分**：`pyproject.toml` 将 grpc 开发工具、postgres 驱动、fastapi/uvicorn/pydantic 构建依赖、minio 发布依赖拆成独立 extras，避免生产镜像引入无关依赖。
- **Java 依赖集中化**：Maven 父 POM 通过 `<properties>` 统一管理 NiFi、gRPC、Protobuf 版本，子模块只引用变量，保证跨模块一致。
- **Proto 代码双向生成**：Python 侧通过 `scripts/generate_proto.py`（基于 `grpcio-tools`）从 `proto/runner.proto` 生成 `runner_engine/generated/`；Java 侧通过 `protobuf-maven-plugin` 在同一份 `.proto` 上生成 Java gRPC 代码。

## 4. 约定与约束

- **Python 版本约束**：`pyproject.toml` 声明 `requires-python = ">=3.11"`，Runner 镜像 base 使用 `python:3.12-slim`。
- **可复现构建**：`requirements.lock` 由 `uv pip compile requirements.txt -o requirements.lock` 自动生成（文件头注释保留该命令），用于固定 numpy==2.5.3、pandas==3.0.5 等传递依赖。
- **Web 依赖继承**：`web/console/requirements.txt` 和 `web/observe/requirements-postgres.txt` 统一通过 `-r` 相对路径继承基线依赖，禁止直接重复声明 fastapi/uvicorn/httpx。
- **Java 版本集中管理**：`nifi/pom.xml` 的 `<properties>` 是单一事实源，子模块不得硬编码 nifi/grpc/protobuf 版本号。
- **NiFi 依赖 scope**：`nifi-api`、`nifi-utils` 声明为 `<scope>provided</scope>`，表明它们由运行时的 NiFi 容器提供，不应打入打包产物。
- **gRPC 版本对齐**：Python 端 `grpcio>=1.65` 与 Java 端 `grpc.version=1.66.0` 保持相近大版本，但二者未通过共享配置强制同步——更新时需人工对齐。
- **无 vendored 第三方库**：未发现 `vendor/`、`third_party/` 或内嵌的二进制依赖；所有第三方库均通过包管理器拉取。
- **无 CI 中的自动升级规则**：仓库中未发现 Dependabot、Renovate 或自动化 PR 脚本，依赖升级需手动编辑 manifest 并重新生成 lock 文件。