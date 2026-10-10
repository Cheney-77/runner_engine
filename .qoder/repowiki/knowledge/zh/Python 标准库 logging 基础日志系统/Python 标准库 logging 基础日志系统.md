---
kind: logging_system
name: Python 标准库 logging 基础日志系统
category: logging_system
scope:
    - '**'
source_files:
    - build_service/app.py
    - publish_service/app.py
    - runner_engine/app.py
    - web/console/main.py
---

## 1. 使用的框架/工具

仓库未引入第三方日志框架（如 `loguru`、`structlog`、`python-json-logger`），全部使用 Python 标准库 `logging`。每个 FastAPI 服务进程在启动时通过本地 `_configure_logging()` 函数对根 logger 进行一次性初始化，将输出定向到 `sys.stdout`。

## 2. 关键文件

- `build_service/app.py`：构建服务的日志配置入口，环境变量 `BUILD_LOG_LEVEL`。
- `publish_service/app.py`：发布服务的日志配置入口，环境变量 `PUBLISH_LOG_LEVEL`。
- `runner_engine/app.py`：Runner 引擎主进程，直接使用模块级 `logger = logging.getLogger(__name__)`，无独立 `_configure_logging`；日志级别由 uvicorn 默认控制。
- `web/console/main.py`：统一控制台聚合进程，仅在非回环地址暴露且禁用 Web login 时通过 `logging.warning(...)` 发出安全告警。

其余业务模块（`service.py`、`lifecycle_service.py`、`admin_http.py`、`worker/*`、`web/admin/*`、`web/observe/*`）仅 `import logging` 并通过 `logger.info/warning/error/exception` 调用，不持有自己的 formatter/handler。

## 3. 架构与约定

### 3.1 每服务一份 root logger 初始化

`build_service/app.py` 的 `_configure_logging` 实现如下模式：

```python
def _configure_logging(level_name: str) -> None:
    level = getattr(logging, level_name.upper(), logging.INFO)
    formatter = logging.Formatter(
        "%(asctime)s %(levelname)s %(name)s - %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    root = logging.getLogger()
    root.setLevel(level)
    if not root.handlers:
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(formatter)
        root.addHandler(handler)
```

`publish_service/app.py` 采用完全相同的结构，只是省略了自定义 formatter 中的 name 字段（格式为 `"%(asctime)s %(levelname)s - %(message)s"`）。两个服务都只在 `root.handlers` 为空时才添加 StreamHandler，避免重复挂载。

### 3.2 日志级别来源

| 服务 | 环境变量 | CLI 参数 | 默认值 |
|---|---|---|---|
| Build Service | `BUILD_LOG_LEVEL` | `--log-level` | `INFO` |
| Publish Service | `PUBLISH_LOG_LEVEL` | `--log-level` | `INFO` |
| Runner Engine | 无专用变量 | 由 uvicorn 默认控制 | uvicorn 默认（通常 INFO） |
| Unified Console | 无专用变量 | 无 | uvicorn 默认 |

CLI 参数与环境变量共存，例如 build/publish 的 `main()` 中：

```python
parser.add_argument("--log-level", default=os.environ.get("..._LOG_LEVEL", "INFO"))
...
_configure_logging(args.log_level)
uvicorn.run(app, ..., log_level=args.log_level.lower())
```

注意：`args.log_level.lower()` 传给 uvicorn，而 `_configure_logging` 内部会再次 `.upper()` 做 lookup，因此大小写不敏感。

### 3.3 输出格式

Build Service 输出行形如：

```
2024-xx-xx xx:xx:xx INFO build_service - Runtime environment resolve failed
```

Publish Service 缺少 `%(name)s`，输出形如：

```
2024-xx-xx xx:xx:xx - Creating virtual operator contract failed
```

所有日志均写入 `sys.stdout`，没有文件 sink、没有 JSON 结构化字段、没有请求 ID/correlation id 注入。

### 3.4 命名空间约定

各模块通过 `logger = logging.getLogger(__name__)` 获取子 logger，继承 root 的 handler。示例命名空间包括 `build_service`、`publish_service`、`runner_engine`、`web.admin.*`、`web.observe.*` 等。部分模块（如 `runner_engine/service.py`、`publish_service/service.py`）直接 `import logging` 后使用 `logging.info(...)` 而非模块 logger，属于混用模式。

### 3.5 结构化字段

日志消息本身是普通字符串，通过格式化参数嵌入上下文，例如：

```python
logger.info("MinIO folder downloaded bucket=%s prefix=%s workspace=%s files=%s bytes=%s", ...)
logger.exception("Runtime environment retry failed env_key=%s", env_key)
```

没有统一的 structured dict 或 JSON 序列化层；下游收集器若需要结构化字段需自行解析。

## 4. 约定与约束

- **约定**：每个 FastAPI 服务在模块顶层调用 `_configure_logging(os.environ.get("*_LOG_LEVEL", "INFO"))`，随后 `logger = logging.getLogger(__name__)`，并在 `create_app` 的 lifespan 中记录启动/停止信息。
- **约定**：HTTP 异常路径使用 `logger.exception(...)` 记录堆栈，再向上抛出；业务异常先转为 HTTPException，再由外层 `except Exception` 捕获并记录。
- **约定**：Runner Engine 主进程没有独立的 `_configure_logging`，依赖 uvicorn 默认的 root logger 设置；其 `logger.info/warning` 仍通过标准库生效。
- **约束**：日志输出目标固定为 `sys.stdout`，没有可插拔 sink（文件、远程、JSON、OpenTelemetry 集成均未实现）。
- **约束**：日志级别只支持 Python 标准库内置级别（DEBUG/INFO/WARNING/ERROR/CRITICAL），通过 `getattr(logging, level_name.upper(), logging.INFO)` 容错处理未知级别。
- **约束**：Build Service 和 Publish Service 的 `_configure_logging` 仅在 `not root.handlers` 时添加 handler，保证多次导入不会重复挂载 StreamHandler。
- **约束**：Unified Console (`web/console/main.py`) 是唯一一个在非服务入口处直接调用 `logging.warning(...)` 的文件，用于在禁用 Web login 且暴露非回环地址时发出安全警告。
- **约束**：未发现任何测试中对 logger 的断言或 mock，日志行为未被单元测试覆盖。

## 5. 总结

该仓库的日志系统是轻量级的标准库 `logging` 方案：每个服务进程独立初始化 root logger，输出到 stdout，通过环境变量 + CLI 参数控制级别，无结构化字段、无外部 sink、无 trace correlation。它足以满足单机/容器化部署下的 stdout 收集场景，但尚未提供面向云原生（JSON、trace-id、采样、分级路由）的能力。