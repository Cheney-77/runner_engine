---
kind: configuration_system
name: 配置系统：环境变量 + JSON 策略文件 + argparse 三源装配
category: configuration_system
scope:
    - '**'
source_files:
    - runner_engine/app.py
    - runner_engine/policy.py
    - runner_engine/config/policies.json
    - runner_engine/config/opensandbox.json
    - runner_engine/config/acl.json
    - build_service/app.py
    - publish_service/app.py
    - web/observe/settings.py
    - web/console/settings.py
    - web/.env.example
    - web/console/.env.example
    - web/observe/.env.example
---

## 1. 采用的方式
仓库没有统一的配置框架（如 pydantic-settings、dynaconf），而是采用**进程入口解析命令行参数 → 回退到 `os.environ` → 加载 JSON 配置文件**的三段式装配模式。运行时策略与连接信息通过独立的 JSON 文件提供，其余所有可运行参数（监听地址、数据库 URL、TLS 证书、配额、超时等）均通过环境变量注入。

## 2. 关键文件
- `runner_engine/app.py` — Runner gRPC 服务入口，使用 `argparse` 解析 `--listen/--catalog/--db-url/--policies/--acl/--clusters/--owner-id/--tls-cert/--tls-key/--client-ca`，并校验必填项。
- `runner_engine/policy.py` — 从 `config/policies.json` 加载为 `SecurityPolicy` 模型。
- `runner_engine/config/opensandbox.json` — OpenSandbox 沙箱集群（gvisor/kata）端点与 API Key。
- `runner_engine/config/acl.json` — gRPC 访问控制（identity→tenant→project 映射）。
- `runner_engine/config/policies.json` — 安全策略模板（standard/hardened，含 cpu/memory/max_timeout_ms/network_allow/reuse_sandbox）。
- `build_service/app.py` — Build Service FastAPI 应用，通过 `_service_from_env()` 从 `BUILD_*` 环境变量装配。
- `publish_service/app.py` — Publish Service FastAPI 应用，通过 `_service_from_env()` 从 `PUBLISH_*` 环境变量装配。
- `web/observe/settings.py` — Observability Settings dataclass，集中读取 `OBS_*` 环境变量并提供 `environment()` 工厂。
- `web/console/settings.py` — ConsoleSettings dataclass，复用 ObserveSettings，集中读取 `CONSOLE_*` / `ADMIN_*` / `OBS_*` 环境变量。
- `web/.env.example` / `web/console/.env.example` / `web/observe/.env.example` — 各子应用的 `.env` 示例。

## 3. 架构与约定
### 3.1 三源优先级
每个服务的启动逻辑遵循同一模式：
1. `argparse` 定义 CLI 参数，默认值来自 `os.environ.get("VAR", default)`。
2. 对关键参数执行显式非空校验（Runner 用 `SystemExit(f"--{name} is required")`；Build/Publish 在 `_service_from_env()` 中 `raise RuntimeError(f"{name} is required")`）。
3. 将装配好的 settings/dataclass 注入到 FastAPI `app.state` 或构造函数。

### 3.2 命名空间约定
| 组件 | 环境变量前缀 | 说明 |
|---|---|---|
| Runner gRPC | `RUNNER_` | 监听、DB、ACL、Clusters、Policies、TLS、Admin、Quota、Lease、Idempotency、Retention、Reaper、Worker Idle/TTL/Renew/Max Releases |
| Build Service | `BUILD_` | DB URL、Base Image、Registry Repo、Python Version、Platform、UV Python Platform、Policy Version、Default Index、Allow Superset Reuse、Listen Host/Port、Log Level、Admin Token |
| Publish Service | `PUBLISH_` | Workspace/Catalog/Source/Native Artifact/Edge Bundle Root、DB URL、Build Service URL、Default Profile/User ID、Edge Python Version/UV Default Index/Require Binary、Build Timeout、Max Source Bytes、Listen Host/Port、Log Level、Admin Token |
| Web (BFF) | `MPR_` | Publish Service URL、HTTP Timeout、Long Timeout、Bearer Token |
| Console/Observe | `OBS_` / `CONSOLE_` / `ADMIN_` | 三个后端 DB URL+Schema、Sandbox URL/API Key/Page Size/Max Items、Execd Metrics/Origins/Sample Limit、Basic Auth、Console Public Origin/Remote No Auth、Admin DB URL、Build Service Bearer |

### 3.3 JSON 策略文件
- `config/policies.json`：键名即策略名称（如 `standard`、`hardened`），由 `load_policies()` 反序列化为 `SecurityPolicy(name, sandbox_cluster, cpu, memory, max_timeout_ms, network_allow, reuse_sandbox)`。
- `config/opensandbox.json`：键名即集群标识（`gvisor`、`kata`），包含 `url` 与 `api_key`。
- `config/acl.json`：`identities.<identity>.<tenant> = [projects]`，`*` 表示通配项目。

### 3.4 类型与边界校验
- `web/observe/settings.py` 中的 `lifecycle_base()` 强制 Sandbox URL 必须是 `http(s)`、带 hostname、不含用户名/密码/query/fragment，且路径只能是根或 `/v1`。
- `parse_origins()` 要求 `OBS_EXECD_ALLOWED_ORIGINS` 每项都是精确的 `scheme://hostname[:port]`，无 wildcard。
- `valid_schema()` 要求 PostgreSQL schema 符合 SQL identifier 正则 `^[A-Za-z_][A-Za-z0-9_]*$`。
- `read_mapping()` 限制表映射只允许 `publish/build/runner` 三个 service 键，metric key 必须为 SQL identifier，value 可以是字符串表名或含 `table/statusColumn/groupColumn/timeColumn` 的对象。
- `web/console/settings.py` 的 `bounded_int()` 对 `OBS_SANDBOX_PAGE_SIZE`(1..100)、`OBS_SANDBOX_MAX_ITEMS`(1..500)、`OBS_EXECD_SAMPLE_LIMIT`(1..200) 做范围校验，越界抛 `ValueError`。
- `CONSOLE_PUBLIC_ORIGIN` 仅允许 `http(s)` scheme、无 user/password/path/query/fragment，外部 HTTP origin 需显式设置 `CONSOLE_ALLOW_REMOTE_NO_AUTH=1`。

### 3.5 可选功能开关
- Admin HTTP 仅在 `RUNNER_ADMIN_TOKEN` / `PUBLISH_ADMIN_TOKEN` / `BUILD_ADMIN_TOKEN` 非空时启用。
- `OBS_TABLE_MAP_FILE` 在 Console 模式下被刻意忽略（注释写明 `deliberately not reading OBS_TABLE_MAP_FILE`），真实表名由内嵌 DDL 提供。

## 4. 约定与约束
- **所有服务的关键配置走环境变量**：CLI 参数只是 `os.environ` 的薄包装，生产部署应通过容器/编排层注入 `RUNNER_*` / `BUILD_*` / `PUBLISH_*` / `OBS_*` / `CONSOLE_*` / `ADMIN_*` / `MPR_*` 变量。
- **JSON 配置文件用于静态策略数据**（policies、clusters、acl），路径通过 `--policies` / `--clusters` / `--acl` 覆盖，默认位于 `./config/`。
- **必填字段在启动期失败**：Runner 对 `db-url`、`owner-id`、`tls-cert`、`tls-key`、`client-ca` 做 `SystemExit` 检查；Build/Publish 对 `*_DATABASE_URL`、`*_ROOT` 等做 `RuntimeError` 检查。
- **URL/Origin/Schema 在加载期严格校验**：任何非法值直接抛 `ValueError`，阻止服务启动。
- **Web 层不读 `.env` 文件**：`.env.example` 仅为文档，实际由 shell/env 注入；Console 明确声明不读取 `OBS_TABLE_MAP_FILE`。
- **日志级别统一通过 `*_LOG_LEVEL` 环境变量** 控制，格式为 `%(asctime)s %(levelname)s %(name)s - %(message)s`。