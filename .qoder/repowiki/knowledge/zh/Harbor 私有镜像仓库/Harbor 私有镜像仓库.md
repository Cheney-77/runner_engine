---
kind: external_dependency
name: Harbor 私有镜像仓库
slug: harbor
category: external_dependency
category_hints:
    - vendor_identity
scope:
    - '**'
source_files:
    - build_service/harbor.py
---

### Harbor
- 集成点：`build_service/harbor.py` 封装对 Harbor 的认证与镜像上传/查询操作。
- 方向：镜像以“超集复用 + 重试”的方式管理，避免每次发布重复构建完整环境。