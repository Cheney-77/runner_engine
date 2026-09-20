# Managed Python v3.3 · 免登录统一控制台

**本版只修改上版新增的 `console/` 与 `admin/`，原始发布模块
`app/` 的 HTML、CSS、JavaScript、BFF、API 路由与旧测试全部不变。**
原独立观测模块 `observe/` 也保留原文件；统一入口使用新的
`console.observe_app` 和固定 PostgreSQL Schema 查询。

## 1. 启动方式

在工程根目录：

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r console/requirements.txt

# 现有 Publish Service URL，和老版本一致：
export MPR_PUBLISH_SERVICE_URL="http://127.0.0.1:9090"

# 每个服务单独填写 PostgreSQL 数据库连接：
export OBS_PUBLISH_DB_URL="postgresql://observer:PASSWORD@PG_HOST:5432/publish_db"
export OBS_BUILD_DB_URL="postgresql://observer:PASSWORD@PG_HOST:5432/build_db"
export OBS_RUNNER_DB_URL="postgresql://observer:PASSWORD@PG_HOST:5432/runner_db"

# 每个数据库内自己的 PostgreSQL SCHEMA：
export OBS_PUBLISH_DB_SCHEMA="publish"
export OBS_BUILD_DB_SCHEMA="build"
export OBS_RUNNER_DB_SCHEMA="runner"

export OBS_SANDBOX_URL="http://SANDBOX_HOST:8080/v1"
export OBS_SANDBOX_API_KEY="REPLACE_IF_REQUIRED"

# 同一进程使用原 Web 端口，先停止原来占用此端口的进程：
python -m console.main --host 127.0.0.1 --port 9095
```

这三个数据库的主机、端口、数据库名可相同也可不同。
例如**共用同一个数据库，但三个 Schema 不同**：

```bash
export OBS_PUBLISH_DB_URL="postgresql://observer:PASSWORD@pg:5432/platform"
export OBS_BUILD_DB_URL="postgresql://observer:PASSWORD@pg:5432/platform"
export OBS_RUNNER_DB_URL="postgresql://observer:PASSWORD@pg:5432/platform"

export OBS_PUBLISH_DB_SCHEMA="publish_service"
export OBS_BUILD_DB_SCHEMA="build_service"
export OBS_RUNNER_DB_SCHEMA="runner_engine"
```

以上仅是配置示例，不代表你的真实 Schema 名称。

**数据库 URL 用于选定数据库；`OBS_*_DB_SCHEMA` 用于选定该数据库
内部的 Schema。两者不是同一个东西。**
所有观测 SQL、资产列表、清理预检、查询业务资产是否存在、
失败环境重试前检查均使用各自 Schema。
覆盖值仅支持安全 SQL identifier：字母、数字、下划线，不能以数字开头。
表名与字段名仍固定为你提供的真实 DDL，不支持、也不读取
`OBS_TABLE_MAP_FILE`。默认 Schema 为 `publish`、`build`、`runner`。

如果 PG 密码包含 `@`、`#`、`/` 等 URL 特殊字符，需进行 URL 百分号编码。
创建独立的只读 PG 角色，仅为所需数据库和 Schema 授权 USAGE/SELECT。
例子：`console/sql/readonly_grants.example.sql`。
这些连接不需要、也不能使用 DBA/superuser 账号。

## 2. 一个 Web 进程，四个入口

| URL | 模块 |
|---|---|
| `/` | 原版算子发布页面、现有 `/api/*`、`/static/*` 全保持原状 |
| `/observe/` | 固定 PostgreSQL Schema + OpenSandbox 观测 |
| `/admin/` | 构建环境、Runner 资产、发布记录、清理预检和管理记录 |
| `/console/` | 三模块入口导航 |

**全部 Web 页面免登录**。访问 `/observe/` 和 `/admin/`
不会再出现 HTTP Basic 登录弹框，即使运行环境里遗留了
`OBS_BASIC_USER`、`OBS_BASIC_PASSWORD`、`ADMIN_USER`、
`ADMIN_PASSWORD`，统一启动入口也会忽略它们。可以删除这些旧变量。

登录被移除后，管理页不再知道究竟是哪位人类用户在操作：
新记录的审计 actor 固定为 `console-operator`，**不能视为个人身份审计**。
如果需要区分操作人，应以后通过受控网关传递可信身份，而不是
从浏览器表单中随意填写操作者。

### 从其他电脑访问：显式开放，必须有网络访问限制

默认绑定 `127.0.0.1`，只能本机访问。若部署在服务器上，
必须自行保证只有可信内网/VPN/受控反向代理能访问 9095 端口；
**不要把免登录管理端口直接暴露公网**。

在已实施防火墙 / VPN / 反向代理访问控制之后：

```bash
export CONSOLE_ALLOW_REMOTE_NO_AUTH=1
export CONSOLE_PUBLIC_ORIGIN="https://your-internal-console.example.com"

python -m console.main --host 0.0.0.0 --port 9095
```

`CONSOLE_PUBLIC_ORIGIN` 必须是浏览器实际访问的完整 Origin
（协议 + 域名/IP + 可选端口，不含路径和尾斜杠）。
若仅在有防火墙的受控私网测试，通过 HTTP 访问，也可以在
`CONSOLE_ALLOW_REMOTE_NO_AUTH=1` 的前提下将它设为例如：

```bash
export CONSOLE_PUBLIC_ORIGIN="http://192.168.10.50:9095"
```

**该变量只是管理写请求的同源校验，不是认证或访问控制。**
本系统保留 JSON Content-Type、Origin 及 `X-Admin-Request`
检查，防止普通跨站表单误操作；这些措施不能阻挡有网络访问权限
的人主动调用 API。防火墙、受控网关和只读数据库权限仍是必需的。

容器内 `--host 127.0.0.1` 可能不能从宿主机的端口映射访问；
若选择 `0.0.0.0`，请同时配置明确的访问控制和上述两个变量。

## 3. PostgreSQL 观测口径

完全使用你给出的三组 Schema DDL：

```text
Publish： operators / virtual_contract_versions / backend_variants / publish_jobs
Build：   runtime_environments / runtime_env_aliases / build_jobs
Runner：  leases / runs / idempotency_keys
```

统计数量、最近 24 小时记录、适用的状态/Backend 分布，以及：
READY 但 `image_ref IS NULL`、FAILED / 长时间 BUILDING 环境、
失败发布、过期租约、长期 RUNNING / ABANDONED Runs、过期幂等记录等。

Runner DDL **没有 releases 表**。
“Referenced release IDs”是统计 `leases.release_id` 的不同取值，
不是实际 Catalog 文件或镜像制品数量。

未连接、SQL 失败与不存在记录会区别显示：未知为 `—`，
实际 COUNT 为 0 才显示 0。成功连接并不保证权限完整；
缺少所需表/列的权限会在对应指标中提示查询失败。

OpenSandbox 继续使用：
`OBS_SANDBOX_URL`、`OBS_SANDBOX_API_KEY`；
CPU/内存须另外设置：

```bash
export OBS_EXECD_METRICS=1
export OBS_EXECD_ALLOWED_ORIGINS="https://approved-execd-gateway.example.com"
export OBS_EXECD_SAMPLE_LIMIT=30
```

必须确认实际 execd Endpoint origin 后才能填写允许列表；
未采集的资源指标显示未知，不会伪装成零。

## 4. 管理模块功能与数据库职责

管理页面可以读取三套业务 PG 数据，做环境清理预检；
但**不会直接 UPDATE/DELETE 三个业务 Schema，也不会删除
物理 Docker Image、Registry 镜像或 Runner Release**。

清理预检检查 Alias、活动 Build Job、已发布作业中的关联信息
和 Runner 有效租约。清理申请只保存 DRAFT 记录，明确返回
`executed: false`；预检结果不是“可安全删除”的授权结论。

如需真实保存资产备注、清理申请以及审计事件，
管理员应先在**单独的管理数据库**执行：

```bash
psql "postgresql://DBA@PG_HOST:5432/admin_db" \
  -v ON_ERROR_STOP=1 -f admin/sql/001_admin_schema.sql
```

然后配置仅具备 `console_ops` Schema 所需权限的角色：

```bash
export ADMIN_DB_URL="postgresql://console_role:PASSWORD@PG_HOST:5432/admin_db"
```

`console_ops` 是第四个**管理元数据** Schema，和你提供的三套业务
Schema 分离；原三套业务表 DDL 不变。
如果不配置 `ADMIN_DB_URL`，业务观测与预检仍可用，
管理记录的写入功能显示不可用。

可选的 Build 重试使用**已有 Build Service API**，
而不是直接修改 `build.runtime_environments.status`：

```bash
export ADMIN_BUILD_SERVICE_URL="http://BUILD_HOST:PORT"
# 仅 Build API 确实需要 Bearer 时配置：
export ADMIN_BUILD_BEARER_TOKEN="..."
```

只对 FAILED 且没有进行中的 Build Job 的环境开放重试；
必须先写操作意图审计。没有真实 Build 服务时该按钮禁用。

## 5. 测试与兼容性

```bash
python -m unittest discover -s console/tests -p 'test_*.py' -v
python -m unittest discover -s admin/tests -p 'test_*.py' -v
python -m unittest discover -s tests -p 'test_*.py' -v
python -m unittest discover -s observe/tests -p 'test_*.py' -v
node --check console/static/observe-dashboard.js
node --check admin/static/admin.js

# 可选 Chromium 端到端（模拟 PostgreSQL / OpenSandbox）：
python console/tests/browser_smoke.py
```

测试特别覆盖免登录直接访问、写操作的 Origin 校验、
3 套 Schema 各自独立的 SQL 拼接与非法 identifier 拒绝、
原发布路由兼容以及原始文件 SHA256 不变。

浏览器 E2E 使用模拟业务接口，并非对你的实际 PostgreSQL、
Build Service、Runner Catalog 或镜像仓库进行联调。
真正上线前应以最小权限账号测试三个实际 Schema 的连接和读权限。

旧版 `tests/binding_test.cjs` 依然有原始测试与模块接口不匹配的
已知问题；为保证发布源代码不变，本版不修改这个旧文件。
