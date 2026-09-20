# Managed Python v3.3 — Operator Console · Binding UX Update

**基于本次对话贴出的 Publish Service 源码和 endpoint 定义；本版主要升级前端交互。**
这是可启动的前端 + FastAPI BFF，不是静态原型，后端路由仍然只连接 Publish Service。
端到端浏览器测试使用**模拟 Publish 服务**，尚未连接你的真实部署；本工程内的
UI 截图也基于模拟样例数据，不能将其当成实际联调记录。

## 本版交互改进

这版参考 Chrome 设置页的清爽任务型交互，并非 Chrome 官方界面或精确复制：

- 顶部五步进度条与当前 Workspace/Callable 上下文：每步都有明确的当前位置；
  只有已完成的步骤可以回看，不能跳过前置数据。
- Workspace 输入时展示完整路径预览，首次 Analyze 自动选择真实 Python Root，
  后续候选从 Analyze 结果读取；改变项目或 Root 后清除旧状态。
- Callable 提供搜索、默认只显示可发布项、按需查看不支持原因、
  推荐入口标签及右侧的参数详情预览。
- Contract 采用分组卡片、侧边填写进度和持续可见的“查看契约摘要”按钮；
  没有构造参数时隐藏无关章节，必填绑定不会被意外关闭。
- 用户核对的是易读的参数绑定摘要；原始 JSON 和覆盖请求移至“高级信息”。
- Runner Profile 有简单输入框，并与真实 `options.profile` JSON 双向同步；
  其他 options 仍在高级设置中由真实 API 决定，Web 不假定 Native 字段。
- 发布使用网页内确认对话框，显示 Operator ID 和 backend；取消不会发送请求。
  Publish/Compile 直接展示状态、错误和摘要，NiFi Native 继续明确显示
  `deploymentRequired` 的含义；提供 ID / 响应复制按钮。
- 适配约 390px 手机屏幕，支持键盘焦点、操作中加载反馈；无需外部字体、
  图标 CDN、Node 或前端编译步骤。

**本版没有修改 Virtual Contract 的字段语义。** 你尚未贴出的
`OperatorSelection`、`BindingSelection`、`OutputSelection` 内部字段仍从
Publish Service `/openapi.json` 读取，不能匹配的 Analyze 建议只展示，不进行猜测转换。

## 本次更新：契约配置与输出编码

第三步的构造参数和调用参数都改用**数据来源卡片**，根据 Analyze 提供的
`allowedSources` 展示允许的选择：

| 契约中的值 | 页面名称 | 用途 |
|---|---|---|
| `input.payload` | 消息内容 | 从输入消息正文读取 |
| `input.metadata` | 消息属性 | 从输入消息的属性读取 |
| `operator.parameter` | 算子参数 | 从算子配置参数读取 |
| `constant` | 固定值 | 直接使用设置的值 |

选中来源后，界面才显示相关字段，例如属性名称、参数名称、内容路径或固定值。
固定值支持文本、数字、布尔值、对象/数组及空值；切换类型或来源时保留各自未提交的填写状态，
**最终请求只包含当前来源选中的字段**，不混入上一来源的陈旧数据。
例如确认“固定值”数字 42 后，提交的 `value` 是 JSON 数字 `42`，不会变成字符串 `"42"`。

上述标签和展示分组属于前端显示层。JSON 字段名只能来自
Publish Service 当前 `/openapi.json` 中的 `BindingSelection`：
若 Pydantic 模型使用未知字段名、不暴露来源字段，前端会退回 Schema 表单，
不会自造后端 API 字段。尚不明确、通用的可选字段可通过“更多设置”打开；
业务有效性仍由真实 Publish Service 校验。

同时修正了通用表单的枚举默认行为：**不再自动选择第一个 codec**。
输出区域会根据 Callable 声明的返回类型提供提示；明确选择 `bytes` 时说明
它只支持字节或文本，而对象/列表需要匹配的格式（如实际模型支持的 JSON）。
此提示不会擅自改写用户的契约或已有发布版本。

如果在 NiFi 工作流中看到 `/opt/runner/app/runner_engine/worker/child.py`
和 `/opt/runner/releases/.../__dsc_entry__.py`，这是 Runner 执行链路的堆栈，
并非单凭 NiFi 环境就能证明正在使用 `nifi_native`。
详细诊断步骤见项目根目录的 **`RUNTIME_DIAGNOSIS.md`**。

## 1. 职责边界

```text
Browser (HTML / CSS / JavaScript)
  |
  v
Web BFF (only MPR_PUBLISH_SERVICE_URL)
  |
  v
Publish Service
  |-- Analyze -> scan_project()
  |-- Create -> verify sourceRevision, immutable snapshot, save contract
  |-- Compile -> compile backend contract / upsert variant
  `-- Publish
      |-- runner -> internal BuildServiceClient.resolve() -> release
      `-- nifi_native -> write package -> deploymentRequired=true
```

Web **没有** `MPR_BUILD_SERVICE_URL`，没有 Runtime Environment 面板，
也没有 Build Service 的 resolve/get/retry 路由。
Runner 的环境解析是 Publish Service 内部 `_publish_runner()` 的责任，
不是浏览器或 BFF 的责任。

## 2. 安装启动

建议 Python 3.11+：

```bash
unzip managed-python-v33-chrome-console-v2.zip
cd managed-python-v33-chrome-console-v2

python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt

export MPR_PUBLISH_SERVICE_URL=http://127.0.0.1:9090
python -m app.main --host 127.0.0.1 --port 9095
```

然后打开：

```text
http://127.0.0.1:9095/
```

检查服务：

```bash
curl -i http://127.0.0.1:9090/health
curl -i http://127.0.0.1:9090/openapi.json
```

如 Publish Service 的端口不同，修改 `MPR_PUBLISH_SERVICE_URL`。
若 BFF 位于容器内，`127.0.0.1` 是容器自身：
需要使用从该容器能访问到的 Publish Service 地址。

可选配置：

```bash
export MPR_HTTP_TIMEOUT_SECONDS=60
export MPR_LONG_TIMEOUT_SECONDS=1900
# 仅当实际 Publish Service 已配置 Bearer Token 时：
# export MPR_PUBLISH_BEARER_TOKEN=...
```

服务只绑定本地回环接口，**仅供本地或受控内网测试**：
没有完整的登录、RBAC、多租户隔离或生产安全网关，不能直接暴露公网。

不需要 Node、npm、前端构建工具或额外数据库。

## 3. Workspace 定位：对应当前部署 /data/user-workspace

你提供的部署环境已经配置：

```text
PUBLISH_WORKSPACE_ROOT=/data/user-workspace
```

（此变量名取自你给的 `_workspace()` 错误提示；实际部署时，以你
PublishSettings.workspace_root 的有效配置为准。）

例如：

```text
/data/user-workspace/
  order-risk/
    requirements.txt
    src/
      order_risk.py
```

Web 只填写：

```text
Workspace: order-risk
```

请求：

```json
{"workspace": "order-risk"}
```

Publish Service 内部由 `_workspace()`：

1. 拒绝空 Workspace 或绝对路径。
2. `(workspace_root / workspace).resolve()` 解析路径并检查结果仍在根目录内。
3. 检查目标为现有目录。

浏览器**不从用户电脑选择目录**，也不访问 `/data/user-workspace` 文件系统。
页面中显示的 `/data/user-workspace` 是按这次部署信息给出的说明标签；
若部署根目录变化，需要同步调整这个标签，最终目录解析仍以 Publish Service
的实际配置为准。你未提供“列出 workspace” API，所以这里是相对路径输入框，
不会伪造项目列表。

Python Root：`_python_root_candidates` 只考虑 `src` 与 `.`，
且目录下要包含 `*.py`。有有效 `src` 时优先推荐它，
否则推荐第一个有效候选。页面先让后端自动选择，再使用 Analyze
响应的 `pythonRootCandidates` 填充选项。
**改变 Python Root 会清除旧 Analyze 结果，必须重新分析**，避免旧 revision
被继续使用。

## 4. 真实的五步交互

### Step 1 — 选择 Workspace / Analyze

```http
POST /v1/authoring/analyze
Content-Type: application/json

{"workspace":"order-risk"}
```

用户明确选择候选 Python Root 后，则发送：

```json
{"workspace":"order-risk","python_root":"src"}
```

页面读取你给定的真实响应字段：

```text
workspace
sourceRevision
pythonRoot
pythonRootCandidates
recommendedCallableId
callables
warnings
```

保留 `sourceRevision` 供创建契约时使用；扫描警告和原始 JSON 仍可查看。

### Step 2 — 选择 Callable

每个 Callable 用以下真实字段展示：

```text
id, kind, displayName, file, line,
supported, unsupportedReasons,
constructorParameters, parameters, returnAnnotation, score
```

不允许选择 `supported=false` 的入口，显示 unsupportedReasons。
优先预选 `recommendedCallableId`，但用户可自由选择其他 supported Callable。

### Step 3 — 配置契约

显示 `constructorParameters` 和 `parameters`，
使用参数的 `name` 作为绑定字典 key，提示：

```text
required, kind, annotation, hasLiteralDefault,
defaultValue / defaultExpression, allowedSources, suggestions
```

必填参数的绑定默认启用且不可关闭；可选参数可根据需要启用。可查看 Analyze 建议；建议的字段如果能
**与运行中的 BindingSelection Schema 匹配**，可以点“应用此建议”。
不能匹配的建议只展示，**不臆造转换规则**。

OperatorSelection、BindingSelection 和 OutputSelection 的内部字段，
**本次没有贴出源码**。因此页面从真正的：

```http
GET /openapi.json
```

读取 `POST /v1/operators` 的 request schema，自动生成结构化表单：
普通字段、枚举、布尔、嵌套对象、列表、可选字段都能根据 Schema 展示；
无法推断的 Any/动态 map 提供该子字段的 JSON 编辑器。

如果 OpenAPI 被禁用或这些模型无法提取，Step 3 会显示具体错误，并提供
“重新读取 Publish OpenAPI”按钮，不会猜测字段继续创建。

OpenAPI 模板不是后端业务校验的替代品。字段约束、Binding 的业务有效性
仍由 `compile_virtual_contract()` 和 Publish Service 判定，
422/400 错误会原样返回。

### Step 4 — 确认并创建

自动组装 `CreateVirtualContractRequest`：

```json
{
  "workspace": "order-risk",
  "source_revision": "<Analyze 实际返回值>",
  "python_root": "src",
  "operator": {},
  "callable_id": "<用户选择的 supported Callable.id>",
  "constructor_bindings": {},
  "argument_bindings": {},
  "output": {}
}
```

以上 `{}` **只是顶层结构说明**，不是可直接提交的真实业务契约；
实际 `operator`、各参数 binding 和 `output` 值来自 Step 3 的
OpenAPI 表单，字段别名（如 sourceRevision）以运行时 OpenAPI 为准。

必须勾选人工确认，才允许发送：

```http
POST /v1/operators
```

高级模式可查看并手工覆盖完整 JSON，仅用于排障，不是必经流程。

根据你提供的 `create_virtual_contract()`：

1. 重新扫描工作区，校验 `source_revision`。
2. `selection_to_virtual_contract()`。
3. 在可变工作区验证/编译。
4. `source_store.put_workspace()` 保存不可变快照。
5. 从 immutable source 再次扫描和编译。
6. `store.save_virtual_contract()` 保存算子及契约。

真实创建响应**使用 `operatorId`（驼峰）**，同时包含：
`contractId`、`contractVersion`、`contractSha256`、`sourceRevision`、
`sourceRef`、`virtualContract` 和 `derived`。
页面用这个真实 `operatorId` 继续。

当服务返回 `SOURCE_CHANGED`，页面清除旧 Analyze / 契约状态，要求重新分析，
不会绕过 revision 校验。

### Step 5 — Compile / Publish

从 Publish `/health.backends` 获取 Backend（源码已确认 runner/nifi_native）。

```http
POST /v1/operators/{operator_id}/backends/{backend}/compile
Content-Type: application/json

{"options": {}}
```

```http
POST /v1/operators/{operator_id}/backends/{backend}/publish
Content-Type: application/json

{"options": {}}
```

`BackendRequest.options` **确定是 `dict[str, Any]`**，
所以编辑器只允许 JSON object；不会像旧版接受数字/列表/null。

Runner：
- `_compile_backend()` 只生成 variant；profile 缺省使用服务端 default_profile。
- `_publish_runner()` 读取 snapshot 的 requirements，调用 Publish Service
  内部 `BuildServiceClient.resolve()`。
- 若 found=false、状态 FAILED、非 READY 或 image 缺失，
  以原始服务错误为准。
- 成功响应可显示 `releaseId`、`runtimeImage`、`envKey` 等。

NiFi Native：
- `publish` 内部 `write_native_package()` 生成 artifact。
- 响应中的 `deploymentRequired: true` 表示**还需要单独部署**到
  NiFi Python extension source directory。
- 页面显示 `artifactFile` 和 `deploymentHint`，不宣称已安装。

`/health.compiledPlanArtifact = false` 时不会推测有 compiled-plan.json。
发布 API 会内部重新 compile 并创建 Job，因此页面不会把先点 Compile
设成不可跳过的发布前置步骤。

## 5. BFF 路由表

| Web BFF | Publish Service |
|---|---|
| `GET /api/health` | `GET /health` |
| `GET /api/openapi` | `GET /openapi.json`（FastAPI 自省） |
| `POST /api/authoring/analyze` | `POST /v1/authoring/analyze` |
| `POST /api/operators` | `POST /v1/operators` |
| `GET /api/operators/{operator_id}` | `GET /v1/operators/{operator_id}` |
| `POST /api/operators/{operator_id}/backends/{backend}/compile` | 同路径 Publish Service |
| `POST /api/operators/{operator_id}/backends/{backend}/publish` | 同路径 Publish Service |

BFF 原样转发请求 JSON 并保留上游 status / Content-Type / body。
仅网关连接错误由 BFF 返回 502，超时由 BFF 返回 504。
未知 URL / 路径片段不会变成可任意访问上游的通用代理。

未使用任何旧版不存在的：

```text
/v1/operator-contracts/draft
/v1/operators/publish
/v1/runtime-environments/resolve
```

## 6. 运行测试

Python HTTP 集成测试（**使用 mock upstream，不连接实际 Publish Service**）：

```bash
python -m unittest discover -s tests -p 'test_*.py' -v
```

JavaScript 语法检查（如果本地装了 Node）：

```bash
node --check app/static/schema.js
node --check app/static/form.js
node --check app/static/binding.js
node --check app/static/ui.js
node tests/schema_test.cjs
```

可选真实 Chromium 前端端到端测试：

```bash
python -m pip install playwright
python -m playwright install chromium
python tests/browser_smoke.py
```

如需输出本地可查看的示意截图：

```bash
MPR_SCREENSHOT_DIR=./preview python tests/browser_smoke.py
```

此浏览器测试还覆盖 Callable 搜索、构造参数和调用参数的来源切换、
固定值的 JSON 类型切换、只提交当前来源的字段、输出 bytes 编码提示、
Runner Profile 与 JSON 同步、发布对话框取消/确认、390px 手机布局。

此浏览器测试会执行完整的
Analyze → Callable → 参数绑定 → 预览确认 → Create → Runner Compile →
Native Publish，同时验证 SOURCE_CHANGED 分支。
为了可离线运行，浏览器的 fetch() 被测试替身转发至真实 FastAPI BFF，
BFF 再调用仅供测试的 Mock Publish Service。
**这不是对你的真实部署进行联调。**

## 7. 已知边界与建议提供的剩余信息

你已提供足够的核心数据流和请求顶层模型，可以运行这版 Wizard。
如果要验证你部署中所有 BindingSelection 字段的适用条件，
尤其当字段命名并非 metadata_key / parameter_name 等常见形式时，
最好补充以下模型（不阻塞本版通过实际 OpenAPI 工作）：

```python
class OperatorSelection(...): ...
class BindingSelection(...): ...
class OutputSelection(...): ...
```

以及真实 Analyze 响应和一份可创建成功的请求/响应（脱敏即可）。
这些可以帮助进一步优化 Suggestion 自动应用、参数来源专用控件、
输出路由和默认值；在拿到之前，本工程不声称已实现未知的绑定语义。

当前没有实现：Workspace 列表、源码上传、发布历史列表、Job 查询轮询、
Native 自动部署、登录/授权。这些均需要额外的后端 API/安全设计，
不会在前端伪造。
