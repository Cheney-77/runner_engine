# Runner 输出编码异常：排查说明

## 已知现象

在 NiFi 流程中观察到的堆栈包含：

```text
/opt/runner/app/runner_engine/worker/child.py
/opt/runner/releases/<release-id>/__dsc_entry__.py
output_content = _encode_payload(payload_value, output_spec["payload"]["codec"])
TypeError: bytes output codec expects bytes, bytearray or str
```

这段堆栈说明错误发生在 **Runner 发布版本的输出编码** 阶段，
不是 Build Service 环境 resolve 的 HTTP 调用阶段。NiFi 可以触发
Runner 执行，不能仅据出现 NiFi 就将执行路径判断为 `nifi_native`。

可从异常确定：传给 `_encode_payload()` 的 `payload_value` 不属于
`bytes`、`bytearray`、`str` 三种支持类型，而运行时使用的 codec 是
`bytes`。这 **不能单独证明**是配置错误还是代码生成错误。
当前没有拿到你的源函数、真实保存的 Virtual Contract 和生成器实现，
因此不应擅自修改 `__dsc_entry__.py` 或声称已修复运行时。

## 1. 先看实际保存的契约

使用发布时返回的 `operatorId`：

```bash
curl -sS http://127.0.0.1:9090/v1/operators/<operator-id> \
  | python -m json.tool
```

将默认地址和 `<operator-id>` 换成你的实际值。
重点检查：

```text
currentContract.virtualContract.output
backendVariants[].backendContract（如已有）
```

根据你贴出的 `get_operator()` 源码，
这两个位置分别来自持久化 Parent Contract 和已保存的 Backend Variant。
**不要凭前端当前输入框推断旧 release 当时使用的配置**：
旧 release 可能对应不同 contractVersion、variantKey 或编译选项。

## 2. 对照真实运行产物

请在报错的同一个 release 中检查 `__dsc_entry__.py`：

```text
输出配置 output_spec 如何赋值？
output_spec["payload"]["codec"] 的实际值是什么？
payload_value 是如何从函数返回值／输出映射得到的？
_encode_payload() 的 bytes/json 分支各支持什么类型？
```

保留现场，不建议直接在 release 目录手改生成文件；
手改既无法确定问题来源，也无法保证下次发布不会再次复现。

## 3. 决策依据

| 观察结果 | 应优先检查 |
|---|---|
| 保存的契约就是 `bytes`，实际 `payload_value` 是 dict/list/int/None | 输出格式是否配置正确；必要时选择服务端**实际支持**的结构化编码格式 |
| 保存的契约是 `json`（或其他非 bytes），生成的 `output_spec` 却是 `bytes` | `compile_virtual_contract()`、Runner backend contract、`write_runner_release_files()` 和入口代码生成逻辑 |
| 契约是 `bytes`，业务函数明确返回 bytes/str，但 `payload_value` 变成其他类型 | 入口包装、输出映射或返回值选择的生成逻辑 |
| 业务函数实际返回 dict，且前端曾自动选中 bytes | 同时核查旧版前端的枚举默认值和已持久化契约 |

仅当 **当前 backend contract / output_spec 与期望契约不一致**时，
才有较强证据支持“代码生成问题”。反之，如果字节格式是用户明确选择
或旧版 UI 隐式选中并成功保存的值，而返回的确实是对象，那么
“类型与 codec 不匹配”是直接原因。

即使业务函数返回注解写了 `dict`，也只能作为线索；
实际分支返回类型与输出映射结果还需要运行记录确认。
调试日志只需要记录 `type(payload_value).__name__` 等类型信息，
不必记录可能包含敏感数据的完整内容。

## 4. 当前 Web 工程已处理什么

- 修复通用枚举表单：不再自动选择第一个 codec（尤其是 bytes）。
- 结构化返回类型时，输出配置区域提示检查格式。
- 明确选择 bytes 时，提示只能编码字节或文本，不会自动把 dict `str()` 化。
- 不更改 Publish Service、Runner Engine 或现有 immutable source / release。
- 不擅自将用户配置改成 json：只有实际 OpenAPI 暴露该选项时才可选择。

**仍需的最小信息**（可以脱敏）：

1. 这次报错的业务 Callable 返回值类型（`type(value).__name__`）及相关返回语句；
2. 对应 Operator 的 `currentContract.virtualContract.output`，
   最好同时给出该 release 所用的 contractVersion；
3. 生成 `output_spec` 和 `payload_value` 的 `__dsc_entry__.py` 相关代码，
   以及 `_encode_payload()` 实现（报错附近约 90–170 行）。

这三项可以进一步确认究竟是配置、旧版 UI 默认选择，
还是契约编译／入口生成逻辑的问题。
