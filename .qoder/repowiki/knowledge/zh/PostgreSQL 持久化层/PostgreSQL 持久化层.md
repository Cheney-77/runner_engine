---
kind: external_dependency
name: PostgreSQL 持久化层
slug: postgresql
category: external_dependency
category_hints:
    - vendor_identity
scope:
    - '**'
source_files:
    - pyproject.toml
    - sql/publish_schema.sql
    - web/console/sql/readonly_grants.example.sql
---

### PostgreSQL
- 角色：runner_engine 与 publish_service 共享的租户/项目/租约/生命周期状态存储；web/console 与 web/observe 作为只读面板连接同一数据库。
- 集成点：依赖 `psycopg[binary]>=3.2` + `psycopg-pool>=3.2`；`sql/publish_schema.sql` 定义 `runner` schema；console 使用 `readonly_grants.example.sql` 授予只读权限。
- 注意：`state/runner.db` 是早期 SQLite 原型遗留，生产路径使用 PostgreSQL。