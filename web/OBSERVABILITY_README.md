# Managed Python 独立观测控制台

本功能是**新增工程代码**：`observe/`。原有 `app/`、`tests/`、根目录
`requirements.txt`、`.env.example` 与其他既有文件全部原样保留。

```text
managed-python-v33-with-observability/
├── app/                       ← 原 Publish Web，未修改
├── tests/                     ← 原发布测试，未修改
├── requirements.txt           ← 原发布依赖，未修改
├── README.md                  ← 原发布说明，未修改
├── observe/                   ← 本次新增：独立观测前端 + 只读 API
│   ├── main.py                ← 单独启动的 FastAPI
│   ├── database.py            ← 三个独立数据库的只读聚合适配器
│   ├── sandbox.py             ← OpenSandbox 生命周期 / execd 采集
│   ├── settings.py            ← 观测专属配置
│   ├── .env.example
│   ├── config/table-map.example.json
│   ├── static/
│   │   ├── index.html         ← 完全独立的观测页面
│   │   ├── styles.css
│   │   └── dashboard.js
│   └── tests/                 ← 单元、HTTP、浏览器测试
└── OBSERVABILITY_README.md    ← 本文件
```

## 1. 启动（与发布页面互不依赖）

在根目录执行：

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r observe/requirements.txt

# 可先不配置数据源启动，页面将显示「未配置」，不会有伪造数据。
python -m observe.main --host 127.0.0.1 --port 9195
```

访问：`http://127.0.0.1:9195/observe/`

原发布页面照旧按原 README 启动，例如 `http://127.0.0.1:9095/`。
新观测服务不 import 原发布模块，不调用其 Build/Publish 操作，
原发布 HTTP 路由没有被修改。

若观测服务需要在其他主机访问，请在 HTTPS 反向代理后部署，
并配置 `OBS_BASIC_USER`、`OBS_BASIC_PASSWORD`。命令行在绑定非回环
地址时会强制检查用户名/密码已配置。HTTP Basic 本身不提供 TLS，
生产应配合网关认证、访问控制和 HTTPS。

注意：`observe/.env.example` 是文档模板，不会被服务自动加载。
请在你的部署系统中设置环境变量，或显式加载可信的环境文件。
**不要把包含密码的 `.env` 提交至 Git。**

## 2. 三个数据库：独立配置

每个服务指向它自己的**数据库**，不是 HTTP 服务的 API URL：

```bash
export OBS_PUBLISH_DB_URL="sqlite:////data/publish/publish.db"
export OBS_BUILD_DB_URL="sqlite:////data/build/build.db"
export OBS_RUNNER_DB_URL="sqlite:////data/runner/runner.db"
```

以上路径**只是 SQLite URL 格式示例，不代表你的服务实际使用这些文件**；
务必换成实际部署配置。SQLite 需要绝对文件路径，使用 `mode=ro`
和 `PRAGMA query_only=ON`，不会创建数据库文件。

如使用 PostgreSQL：

```bash
python -m pip install -r observe/requirements-postgres.txt
export OBS_PUBLISH_DB_URL="postgresql://readonly_user:YOUR_PASSWORD@DB_HOST:5432/publish_db"
export OBS_PUBLISH_DB_SCHEMA="public"
```

Build / Runner 同理配置各自数据库 URL 和 Schema。
推荐为观测控制台创建专门的 SELECT-only 用户，
仅授权所需业务表。程序额外设置只读事务和查询超时，
但它不能代替真正的数据库权限隔离。

本版支持 SQLite 和 PostgreSQL；如果三个服务里有 MySQL、
其他存储系统或多个实例分片，需要新增专用 DB Reader，
而不是把其连接字符串误填为 SQLite。

### 提供哪些统计？

当前以**已经确认存在的逻辑实体**为指标名，尚不假定真实 SQL 表名：

| 服务 | 指标 | 可计算细分 |
|---|---|---|
| Publish | Operators / Contract versions | 总记录数；存在可靠创建时间时的最近 24h |
| Publish | Backend variants | 总数、状态分布、backend 分布、最近 24h |
| Publish | Publish jobs | 总数、状态分布、backend 分布、最近 24h |
| Build | Runtime environments | 总数、状态分布、最近 24h |
| Build | Build jobs | 总数、状态分布、最近 24h |
| Runner | Registered releases | 总数、最近 24h |
| Runner | Executions / Runner jobs | 总数、状态分布、最近 24h |

只有匹配到真实表后才展示 COUNT；没有找到表或未连接时显示「—」，
不是零。状态、backend 和时间细分也必须能从真实列识别，
不支持时明确隐藏该维度。页面的数据库结构折叠区域仅展示
表名和字段名，**不会读取或返回业务数据行、错误详情、源码或凭证**。

**自动识别只匹配少量常见的完整表名**，不是根据猜测执行 SQL。
例如 Publish Service `save_virtual_contract()` 等业务方法让我们
知道存在逻辑上的 Operator、Contract、Variant、Job，但仅凭
`service.py` 无法知道具体 SQL 表名。

如果未识别，请在数据库卡片中展开「查看数据库结构（表名及字段）」，
将 `observe/config/table-map.example.json` 复制到受控路径并
替换其中所有 `YOUR_...` 占位符：

```bash
cp observe/config/table-map.example.json /etc/observability-map.json
# 编辑真实表名、字段名后：
export OBS_TABLE_MAP_FILE=/etc/observability-map.json
```

映射值可以是表名字符串，也可以是对象：

```json
{
  "publish": {
    "jobs": {
      "table": "ACTUAL_PUBLISH_JOB_TABLE",
      "statusColumn": "ACTUAL_STATUS_COLUMN",
      "groupColumn": "ACTUAL_BACKEND_COLUMN",
      "timeColumn": "ACTUAL_CREATED_AT_COLUMN"
    }
  }
}
```

`timeColumn` 等细分可省略。映射字段通过真实数据库元数据校验，
不接受用户提交 SQL；前端没有 SQL 编辑器。

**时间口径：**最近 24h 是 `created_at` / 配置时间列中落入
当前 UTC 时间减 24h 的记录数，仅在该字段类型/格式适合时展示。
不同 DB 时区及 timestamp 字符串格式须在部署时确认。
当前指标是“当前快照”，不是完整的历史趋势或调用链。
如要统计构建平均时长、缓存命中率、发布成功率随时间变化，
需要确认真实字段、事件时间和留存数据，再新增专门视图，
不能直接拿不同生命周期的总行数推导。

## 3. OpenSandbox：生命周期指标

官方 Lifecycle API：

```http
GET /v1/sandboxes?state=Running&page=1&pageSize=50
```

返回 `items` 和 `pagination`，每个 Sandbox 提供 `id`、
`status.state`、`createdAt` 和可选 `expiresAt`。
页面分别展示 Running 总数（以分页的 `totalItems` 为准）、
本次已读取个数、存续时长（当前时间减 createdAt）、剩余 TTL。
如果数量超过设置的上限，明确提示列表截断，不把局部当全量。

配置：

```bash
export OBS_SANDBOX_URL=http://YOUR_OPEN_SANDBOX_HOST:8080/v1
export OBS_SANDBOX_API_KEY=YOUR_OPEN_SANDBOX_API_KEY
export OBS_SANDBOX_PAGE_SIZE=50
export OBS_SANDBOX_MAX_ITEMS=200
# 此处只设列出多少个实例，不代表全部都要采样。
```

`OBS_SANDBOX_API_KEY` 只由后端发往 OpenSandbox，不出现在浏览器
HTML 或响应中。程序只执行 GET，并且只返回 ID、状态、镜像、
创建与到期时间等显示需要的字段，不向浏览器返回全部 metadata。

**存续时长不是 CPU 实际工作时间**：paused/resumed 期间或排队历史
是否包含在其中需要额外生命周期记录；本页仅用 `createdAt` 计算。

## 4. OpenSandbox：CPU / 内存的实时用量

官方 Lifecycle API 提供存续信息，但不提供每个 Sandbox 的当前
CPU/内存消耗；OpenSandbox execd 定义了：

```http
GET /metrics
```

响应中有：

```text
cpu_count
cpu_used_pct
mem_total_mib
mem_used_mib
timestamp
```

本工程在用户显式开启采集后，先向 Lifecycle API 调用：

```http
GET /v1/sandboxes/<id>/endpoints/44772?use_server_proxy=true
```

只对**管理员明确允许的 endpoint origin** 发 `GET /metrics`。
在返回的 endpoint URL 上只追加 `/metrics`，不执行任意命令。
Endpoint 可能带需要的 headers：它们只会发送到获准的目标，
不会返回浏览器。如果 server-proxy 与 Lifecycle 同源且
路径明确为对应 Sandbox 的 44772 端口，则可向该同源代理
发送 Lifecycle API key。其他目标绝不会自动收到 Lifecycle key。

```bash
export OBS_EXECD_METRICS=1
export OBS_EXECD_SAMPLE_LIMIT=30
# 下面必须换成 endpoint 实际返回的、管理员认可的 scheme+host+port。
export OBS_EXECD_ALLOWED_ORIGINS=http://YOUR_APPROVED_GATEWAY:8080
```

多个源使用英文逗号分隔。不能设置 `*`，不能包含路径、
用户名、密码、通配符。未配置 origin 时**不尝试**获取资源数据。

资源采集失败时显示「采样失败」和空值，不显示 0。内存汇总只累加
**成功采样**的实例；如有部分失败，页面会注明覆盖不完整。
一轮资源请求并发数上限为 6；默认最多采样 30 个 Sandbox，
本轮采样耗时限制约 18 秒，未采样/超时单独标注。
可用 `OBS_EXECD_SAMPLE_LIMIT` 调整采样上限（1–200）。
短时间密集监测可增加服务负载，建议先用 20/60 秒刷新间隔。

**口径提醒：**execd 返回的是其系统指标接口的读数。
是否严格按 container cgroup 隔离计量，应结合你的 OpenSandbox
运行时（Docker/Kubernetes）确认；本版不声称这些数值等同于
Docker stats 或 Pod metrics。它也不监控网络 IO、GPU、磁盘写入
或多日历史曲线；这些需要单独采集源或 OTLP/Prometheus 接入。

官方协议：
- https://open-sandbox.ai/api/
- https://github.com/opensandbox-group/OpenSandbox/blob/main/specs/sandbox-lifecycle.yml
- https://github.com/opensandbox-group/OpenSandbox/blob/main/specs/execd-api.yaml

## 5. 安全与边界

- 无写接口，无 DELETE/POST Sandbox 操作，无任意 SQL、任意 Shell、Docker Socket。
- 数据库 SELECT-only 凭据放后端环境变量，推荐独立只读数据库账号。
- Sandbox API Key 和 execd Endpoint Token 不发给浏览器。
- 只允许预配置的 execd Endpoint Origin，禁止自动重定向和环境代理，
  降低被伪造 endpoint 诱导访问内网其他服务的风险。
- 在任何数据库或 Sandbox 未配置/失败时返回明确状态和空指标，
  不会自动生成示例监控数据。
- 页面采用 20 秒默认自动刷新，可调整为 60 秒或关闭；无落盘历史数据。
- 适合私有网观测。生产还建议增加审计、RBAC、限流、服务发现、
  Prometheus / OpenTelemetry 与持久化指标存储。

## 6. 测试

```bash
python -m unittest discover -s observe/tests -p 'test_*.py' -v
node --check observe/static/dashboard.js

# 可选 Chromium UI 测试
python -m pip install playwright
python -m playwright install chromium
python observe/tests/browser_smoke.py
```

浏览器测试使用**真实的本地 SQLite 测试数据库**和**明确的
OpenSandbox HTTP 模拟数据**，不会连接你生产服务，不会创建
或终止任何 Sandbox。可选生成测试截图：

```bash
OBS_SCREENSHOT_DIR=./observe-preview python observe/tests/browser_smoke.py
```

`test_baseline_files_are_byte_identical` 在原发布 ZIP 存在时，
对比全部 20 个原始文件的 SHA256，确认没有修改旧发布工程。

**既有测试的已知问题：**原 ZIP 自带的 `tests/binding_test.cjs`
调用了旧 `binding.js` 未暴露的 `sourceChoices()`。
它在**未修改的原版 ZIP**中也会失败；本次严格遵守“不修改发布代码”
的要求，不调整旧文件，也不把这个既有失败计入观测功能测试通过项。
