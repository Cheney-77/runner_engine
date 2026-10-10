---
kind: external_dependency
name: MinIO 源码与工件存储
slug: minio
category: external_dependency
category_hints:
    - vendor_identity
scope:
    - '**'
source_files:
    - publish_service/utils/minio_tools.py
    - publish_service/source_store.py
    - pyproject.toml
---

### MinIO
- 角色：`publish_service` 用于下载用户源码（workspace）及暂存编译/发布产物的对象存储后端，依赖声明为 `minio>=7.2`。
- 集成点：`publish_service/utils/minio_tools.py` 提供 MinIO 客户端封装；`source_store.py` 负责 workspace 快照的不可变存储。