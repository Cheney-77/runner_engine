---
kind: error_handling
name: RunnerError 分层错误体系与 gRPC/HTTP 双通道映射
category: error_handling
scope:
    - '**'
source_files:
    - runner_engine/errors.py
    - runner_engine/gateway.py
    - runner_engine/service.py
    - runner_engine/quota.py
    - runner_engine/lifecycle_runtime.py
    - runner_engine/lifecycle_service.py
    - build_service/lifecycle_routes.py
    - web/admin/main.py
    - web/console/observe_app.py
    - web/observe/main.py
---

## 1. 采用的系统与方法

- **核心类型**：`runner_engine/errors.py` 定义了一个以 `RunnerError(RuntimeError)` 为根的自定义异常层次，子类包括 `CatalogError`、`LeaseError`、`QuotaError`、`SandboxError`。每个 `RunnerError` 构造时携带三个字段：
  - `code: str` —— 稳定的机器可读错误码（如 `TOO_MANY_ATTRIBUTES`、`UNKNOWN_SECURITY_POLICY`、`UNAUTHENTICATED`）。
  - `message: str` —— 人类可读消息。
  - `retryable: bool = False` —— 指示调用方是否应重试该错误。
- **gRPC 层**：`runner_engine/gateway.py` 中的 `Servicer._abort_runner_error` 将 `RunnerError.code` 与 `retryable` 标志映射到 `grpc.StatusCode`：
  - `UNAUTHENTICATED` → `UNAUTHENTICATED`
  - 以 `FORBIDDEN` 结尾或 `CANCEL_FORBIDDEN` → `PERMISSION_DENIED`
  - `RELEASE_NOT_FOUND`、`LEASE_UNKNOWN` → `NOT_FOUND`
  - `retryable=True` → `UNAVAILABLE`
  - 其余 → `FAILED_PRECONDITION`
  所有 gRPC 方法在捕获 `RunnerError` 后统一走 `_abort_runner_error`，不再直接 `raise`。
- **HTTP 层**：`build_service/lifecycle_routes.py` 等 FastAPI 路由使用 `fastapi.HTTPException(status_code=..., detail=...)` 表达业务错误；未捕获的 Python 异常由 FastAPI 默认处理。
- **Web 中间件**：`web/admin/main.py`、`web/console/observe_app.py`、`web/observe/main.py` 通过 `@app.middleware("http")` 注册请求级中间件，用于日志/指标等横切关注点，而非集中式全局错误处理器。
- **无全局 panic/recover**：Python 侧未发现 `try/except Exception` 作为进程级兜底；仅在 `service.py` 的后台清理线程中用 `except Exception: logger.exception(...)` 吞掉维护任务异常，避免守护线程退出。

## 2. 关键文件与包

- `runner_engine/errors.py` —— 唯一集中定义的领域错误类型。
- `runner_engine/gateway.py` —— gRPC Servicer，承担认证、鉴权与 RunnerError→gRPC StatusCode 映射。
- `runner_engine/service.py` —— `RunnerService.invoke` 是错误传播的核心枢纽：校验参数、执行用户算子、记录结果、回收 worker，并在最后两层分别捕获 `RunnerError` 和 `Exception`，把基础设施异常包装成 `RUNNER_INFRASTRUCTURE_ERROR`（`retryable=True`）。
- `runner_engine/quota.py` —— 配额超限抛出 `QuotaError(..., retryable=True)`，体现“配额不足可重试”的业务语义。
- `runner_engine/lifecycle_runtime.py`、`runner_engine/lifecycle_service.py` —— 生命周期相关逻辑也通过 `RunnerError` 上报失败。
- `build_service/lifecycle_routes.py` —— FastAPI 路由层，使用 `HTTPException` 暴露 401/404/409 等 HTTP 语义。
- `web/admin/main.py`、`web/console/observe_app.py`、`web/observe/main.py` —— Web 应用中间件挂载点。

## 3. 架构与约定

- **分层抛错**：业务/资源层（catalog、lease、quota、sandbox）抛出具体子类 `*Error`；服务层（`RunnerService`）对输入校验、并发冲突、用户算子失败等场景抛出带稳定 `code` 的 `RunnerError`；gRPC 网关是唯一把 `RunnerError` 转换为外部协议错误的地方。
- **错误码驱动**：`RunnerError.code` 是跨层契约——下游依赖它做决策（例如 `reusable_errors` 集合决定 worker 是否可复用；gateway 决定 gRPC status）。新增错误码需同时考虑：(1) 是否加入 `reusable_errors`；(2) 是否在 `_abort_runner_error` 中有显式映射；(3) 调用方是否消费 `error_code` 字段。
- **可重试性标记**：`retryable=True` 表示错误是瞬态的（超时、取消、配额耗尽、基础设施故障），调用方可安全重试；`False` 表示需要修正输入或策略后再试。
- **Worker 回收约定**：`RunnerService.invoke` 在 `RunnerError` 分支与通用 `Exception` 分支都尝试 `pool.invalidate(worker)`，并把未完成运行标记为 `ABANDONED`，保证异常路径不泄漏 worker。
- **gRPC 强制 mTLS**：`serve()` 文档注释明确“Plaintext mode is deliberately not implemented”，且 `_peer_identity` 缺失身份时直接抛 `UNAUTHENTICATED`，使认证失败成为不可恢复的 `RunnerError`。

## 4. 约定与约束（描述性观察 + 可验证规则）

- **观察到的约定**：
  - 领域层抛 `CatalogError` / `LeaseError` / `QuotaError` / `SandboxError`，这些子类目前仅继承 `RunnerError`，未添加额外字段，但语义上按子系统分类。
  - 业务校验失败（属性数量/大小、内容大小、幂等键缺失、并发 invocation_id）统一抛 `RunnerError(code="...", message=...)`，不带 `retryable`（即默认为 `False`）。
  - 配额类错误显式设置 `retryable=True`，表明客户端应退避重试。
  - gRPC 服务端不在业务代码中直接调用 `context.abort`，而是通过 `_abort_runner_error` 统一转换，避免在每个 RPC 方法重复映射逻辑。
  - FastAPI 路由层使用 `HTTPException` 表达 HTTP 语义（401 unauthorized、404 not found、409 conflict），与 gRPC 层的 `RunnerError` 体系分离。
  - Web 应用通过 `@app.middleware("http")` 挂载横切逻辑，而不是集中式 exception handler。
- **可验证的规则**：
  - 所有被 gRPC 暴露的方法（`AcquireLease`、`RenewLease`、`Invoke`、`Cancel`、`ReleaseLease`、`Health`）若可能抛出 `RunnerError`，均包裹在 `try/except RunnerError` 并调用 `_abort_runner_error`（见 `gateway.py` 第 102–177 行）。
  - `RunnerService.invoke` 末尾有两个 catch 块：先 `except RunnerError`，再 `except Exception`，后者统一包装为 `RUNNER_INFRASTRUCTURE_ERROR` 且 `retryable=True`（第 364–405 行）。
  - `reusable_errors` 白名单（第 334–349 行）决定哪些错误码对应的 worker 可放回连接池，新增错误码若期望复用 worker 需加入此集合。
  - `RunnerError.__init__` 签名固定为 `(code: str, message: str, *, retryable: bool = False)`，任何新增错误子类都应遵循该构造方式（定义处可见）。
  - gRPC 状态码映射在 `_abort_runner_error` 中硬编码匹配 `UNAUTHENTICATED`、`FORBIDDEN` 后缀、`RELEASE_NOT_FOUND`、`LEASE_UNKNOWN` 以及 `retryable` 标志，新增错误码若需特殊 HTTP/gRPC 语义需在此扩展。