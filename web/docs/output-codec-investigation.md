# Runner 输出编码异常：调查记录与定位步骤

本页只依据本次对话中提供的运行堆栈和 `PublishService` 主流程。
**没有读取到真实 `__dsc_entry__.py` 源码、`OutputSelection`/`BindingSelection` 模型或该算子的实际返回值**，因此不能直接判定是配置问题还是生成器 Bug。

## 1. 已由堆栈确定的事实

```text
runner_engine/worker/child.py: execute()
  → target(content=content, attributes=attributes, parameters=parameters)
releases/.../__dsc_entry__.py: process(), line 164
  → _encode_payload(payload_value, output_spec["payload"]["codec"])
__dsc_entry__.py: _encode_payload(), line 122
  → TypeError("bytes output codec expects bytes, bytearray or str")
```

可以确认：

1. Runner worker 已进入发布产物的 `process()`，异常发生在输出编码阶段。
2. 此次调用传入 `_encode_payload()` 的 codec 是 `bytes`。
3. 当时被编码的 `payload_value` 不是该分支接受的 bytes、bytearray 或 str。
4. 堆栈路径表明这是 **Runner release 的 Python 入口**；即使由 NiFi 触发，也不能仅凭此堆栈归因到 `nifi_native` package 的生成器。

堆栈**没有**提供 `payload_value` 的实际 Python 类型、原始函数返回值、
`VirtualOperatorContract.output`、中间编译结果及生成器源码，所以尚不能定位责任。

## 2. 三处数据对照：一眼分清配置和生成器

| 观测 | 可能的原因 | 下一步 |
|---|---|---|
| 业务函数返回 dict/list/int/None；保存的契约和 `output_spec` 都选 `bytes` | 输出 codec 与真实值类型不匹配；也可能业务返回类型偏离预期 | 选择该版本真正支持的合适 codec，或让函数按原约定返回 bytes/str |
| 保存的 Virtual Contract 选择了 JSON 或其他格式，但生成入口实际 `output_spec["payload"]["codec"] == "bytes"` | selection→contract→compiler→release writer 中可能有转换/默认值问题 | 检查每层输出配置的变化；不要只修改 Web 文案 |
| 业务函数原始返回值是 bytes/str，生成 `payload_value` 却是 dict/list/tuple/None | `process()` 对返回值的解包或输出字段提取可能有问题 | 比较业务函数真实返回值与编码前 `payload_value` 的类型 |
| `returnAnnotation` 写 dict，但运行值有时是 str、有时是 dict | 运行时返回类型不稳定；注解不是运行时证明 | 以异常那一次的实际类型为准 |

**不要直接将 `_encode_payload()` 的 `bytes` 分支改成
`str(value).encode("utf-8")`。** 字典将被转成 Python 表示形式，而非标准 JSON；
这会隐藏原有的契约错误和下游格式问题。只有在明确产品协议后才能引入自动序列化。

## 3. 推荐用最少的信息定位

### A. 检查实际保存的契约

调用已有接口（没有新增 Web API）：

```http
GET /v1/operators/{operator_id}
```

重点看响应中的：

```text
currentContract.virtualContract.output
currentContract.sourceRevision
currentContract.sourceRef
backendVariants
```

也可查看**当次 Create 响应**的 `virtualContract` 和 `derived`。
如果后端变体 API 响应包含 `backendContract`，也对比其中输出相关字段。
不同层的字段名必须以实际 JSON 为准，本工程不会虚构字段。

### B. 在可复现的开发环境中记录类型，不记录敏感业务内容

在生成器模板相应位置，或仅在隔离调试副本的
`__dsc_entry__.py` 第 164 行调用前，临时加入：

```python
logger.warning(
    "output codec=%s payload_value_type=%s is_none=%s",
    output_spec["payload"]["codec"],
    type(payload_value).__name__,
    payload_value is None,
)
```

如果无法引入 logger，可在隔离测试中用 `print`，但**不要打印
`payload_value` 的值本身**：可能包含真实数据或用户敏感信息。
记录类型后，进一步在业务 Callable 返回后、输出映射前分别记录类型，
确定究竟是函数输出本身，还是入口的解包逻辑改变了它。

**不要直接修改线上按内容寻址的 release 文件**：
那可能影响发布完整性和审计。优先修改代码生成模板并在测试环境重新编译发布，
或者使用单独拷贝复现。

### C. 看看该版本支持什么 codec

检查实际 `OutputSelection` 模型、生成器 `_encode_payload()` 的全部分支。
如果确实存在 `json` 且接受 dict/list，可在前端选择 JSON 输出；
如果没有此 codec，就不能假设修改 UI 或 contract 能修复。

编译/发布之后也要检查最新 release 是否真的更新。
如果仍运行旧的 release ID，新契约不会自动修复旧入口。

## 4. 建议补充的最小源码与样本

只需脱敏后的这些片段，不需要整仓库：

1. `OutputSelection` 的完整定义（含引用到的子模型），以及 `selection_to_virtual_contract()` 如何使用 `output`。
2. 生成 `__dsc_entry__.py` 的模板/函数中 `_encode_payload` 与 `process` 的实际实现，
   尤其是 `payload_value` 如何从业务返回值得到、`output_spec` 如何生成（约第 100–170 行）。
3. 失败算子的 `currentContract.virtualContract.output` 和当时对应的
   `output_spec`（或者生成入口中的相关常量，均可脱敏）。
4. 业务 Callable 的**返回类型及返回形态**，例如“dict，键包含 payload/attributes”；
   不需要提供真实数据内容。如果能说明异常时 `type(payload_value).__name__` 更有用。

拿到这几项后，可以按契约、代码生成、执行数据三条链逐个定位，
并写针对性单元测试，而不是在 `bytes` 分支上盲目加容错。

## 5. 本版 Web 做了什么、没做什么

当 OutputSelection 的表单中能够识别出 `payload.codec == "bytes"`
（或相应输出字段）时，提示它只直接支持 bytes/bytearray/str；
若 Callable 返回注解声明为 dict/list 等，提示可能不匹配。

**只提醒，不自动改 codec。** 返回注解可能缺失或与运行时实际类型不同；
最终是否有效仍由真实编译器和 Runner 校验。

这次没有修改 Publish Service、生成器或任何 `__dsc_entry__.py`，
因此 UI 优化**本身不会修复已有 release 的运行错误**。
