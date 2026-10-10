---
kind: external_dependency
name: OpenSandbox 隔离沙箱后端
slug: opensandbox
category: external_dependency
category_hints:
    - vendor_identity
    - framework_behavior
scope:
    - '**'
source_files:
    - infra/opensandbox/README.md
    - runner_engine/sandbox/opensandbox.py
---

### OpenSandbox
- 角色：runner_engine 的 sandbox 后端之一，按 `config/policies.json` 选择的 isolation class（如 `gvisor`、`kata`）把 worker 进程投递到对应的 OpenSandbox control-plane/runtime group。
- 集成点：`runner_engine/sandbox/opensandbox.py` 通过 HTTP API 调用 OpenSandbox，每个 create 请求附带 CPU/memory 配额与 deny-by-default 网络策略，并请求 `secureAccess`；返回的安全头由 runner 原样转发给 worker。
- 行为要点：v3.3 不支持在单个 worker 请求中动态切换 gVisor/Kata——必须按隔离类部署独立的 OpenSandbox 实例组，并在 Runner 配置中以 url/api_key 映射选择集群。