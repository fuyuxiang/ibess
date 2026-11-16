# 数据库迁移与归属

所有生产 DDL 由 `services/api/migrations` 管理。事件、媒体等消费者不得执行 `create_all`、`drop_all` 或兼容改表；启动时检查需要的表和当前版本，不满足条件则明确失败。

当前版本链：

| 版本 | 内容 |
| --- | --- |
| `0001_baseline` | 用冻结的 PostgreSQL DDL 创建缺少的表，并先创建被外键引用的唯一索引 |
| `0002_legacy_columns` | 为已有表补齐允许安全追加的列和索引，保留原有业务数据和规则配置 |
| `0003_scope` | 固定摄像头别名归一化和按发生时间解析业务作用域的数据库函数 |

基线存放于 `snapshots/0001_schema.json`，迁移不依赖未来版本的 ORM 动态生成旧结构。当前表清单、DDL 负责人及可写服务记录在 `ownership.json`。API 的 `app/schema.py` 显式注册模型，避免依赖路由导入顺序决定哪些表会被创建。

升级在同一事务中执行，并使用 PostgreSQL advisory lock 防止多个 API 实例并发迁移。遇到无法安全追加的必填字段会回滚，不能只写版本号跳过错误。测试覆盖空数据库、已有事件/证据/自定义规则、并发升级、重复执行和失败回滚。

部署前先通过既有备份流程保存数据库和证据卷。使用本次代码重新构建 API 后，可显式执行：

```bash
docker compose run --rm --no-deps api python -m app.migrations upgrade
docker compose run --rm --no-deps api python -m app.migrations check
```

API 启动仍会调用同一版本迁移入口，以兼容既有启动脚本；Compose 中消费者等待 API 就绪。旧 `deploy/integration/migrate_a_tables.py` 保留 dry-run 计划能力，`--target api --apply` 委托给统一迁移，`--target event --apply` 仅检查结构。

版本基线不会删除表、字段或数据。当前拒绝破坏性降级；回退应恢复已验证的备份，或者另写保留数据的前向修正迁移。不要对已有业务库直接 `alembic stamp head`。非空历史库若存在重复键或更早的未知结构，应先在备份副本演练并补充明确的数据迁移。

后续修改表结构时：新增 Alembic revision，更新公共 `SCHEMA_HEAD`、归属清单与兼容测试，确保先迁移 API 再启动消费者。模型定义只能描述目标结构，不能承担升级已有数据库的职责。
