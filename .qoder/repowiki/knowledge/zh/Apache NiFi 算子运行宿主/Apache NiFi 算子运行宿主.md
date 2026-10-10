---
kind: external_dependency
name: Apache NiFi 算子运行宿主
slug: apache-nifi
category: external_dependency
category_hints:
    - vendor_identity
    - client_constraint
scope:
    - '**'
source_files:
    - nifi/README.md
    - nifi/managed-python-processors/src/main/java/com/dsc/nifi/ManagedPythonTransform.java
    - nifi/managed-python-processors/src/main/java/com/dsc/nifi/GrpcManagedPythonRuntimeService.java
    - proto/runner.proto
---

### Apache NiFi
- 角色：托管 Python 算子的 Java 宿主；本仓库提供 `ManagedPythonTransform` Processor 与 `GrpcManagedPythonRuntimeService` Controller Service，作为 NiFi 2.0 / Java 21 的 NAR 产物。
- 约束：NiFi 侧不持有 Python 环境/pip 逻辑，也不走明文 gRPC；安全策略绑定在已发布的 release 上而非 processor property。构建需对齐平台实际使用的 NiFi 发行版版本。
- verify exact API/params against official NiFi Processor/Controller Service SPI docs.