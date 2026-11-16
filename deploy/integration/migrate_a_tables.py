"""A 侧增量建表（执行卡 10.2）：只建 A 新增的独立表，不删、不改任何已有表；默认 dry-run。

在对应服务的容器里运行（工作目录 /app，模型包可导入）：
  docker compose -f docker-compose.yml -f deploy/integration/docker-compose.a.yml exec api python /integration/migrate_a_tables.py --target api [--apply]
  docker compose ... run --rm -v ./deploy/integration:/integration:ro event-service python /integration/migrate_a_tables.py --target event [--apply]
兼容入口：API 写入委托给版本化 Alembic 迁移；event 仅验证结构，不再建表。
"""
from __future__ import annotations

import argparse
import importlib
import os
import sys

TARGETS = {
    "api": {
        "base": "app.models",
        # business_models 只用于把 business_projects / business_rooms 注册进 metadata：
        # source_zone_configs 的外键指向它们，不注册的话 create_all 解析外键会报 NoReferencedTableError。
        # 这两个表由 B 负责，本脚本只读它们的定义，不会建也不会改。
        "modules": ("app.business_models", "app.asset_models", "app.stage_media_binding_models",
                    "app.offline_visual_models", "app.zone_config_models"),
        "tables": ("stage_assets", "stage_media_bindings", "offline_qa_jobs", "source_zone_configs"),
        "requires": ("eval_sessions", "business_projects", "business_rooms"),
    },
    "event": {
        "base": "service.models",
        "modules": ("service.clue_models",),
        "tables": ("risk_clues", "clue_processing", "observation_receipts"),
        "requires": (),
    },
}


def plan(engine, target: str) -> tuple[list[str], list[str], list[str]]:
    """返回 (已存在, 将新建, 缺失的依赖表)。"""
    from sqlalchemy import inspect
    existing = set(inspect(engine).get_table_names())
    spec = TARGETS[target]
    present = [t for t in spec["tables"] if t in existing]
    create = [t for t in spec["tables"] if t not in existing]
    missing = [t for t in spec["requires"] if t not in existing]
    return present, create, missing


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="A 侧增量建表（只建不删，默认 dry-run）")
    parser.add_argument("--target", choices=sorted(TARGETS), required=True)
    parser.add_argument("--apply", action="store_true", help="真的建表；不加只打印计划")
    parser.add_argument("--database-url", default=os.environ.get("DATABASE_URL"))
    args = parser.parse_args(argv)
    if not args.database_url:
        print("缺 DATABASE_URL（环境项或 --database-url）")
        return 2
    sys.path.insert(0, os.getcwd())
    from sqlalchemy import create_engine
    engine = create_engine(args.database_url, pool_pre_ping=True)
    try:
        present, create, missing = plan(engine, args.target)
        for table in present:
            print(f"{table}: 已存在")
        for table in create:
            print(f"{table}: 将新建（由 API 版本迁移统一执行）")
        if missing:
            print(f"缺少依赖表：{', '.join(missing)}")
        if not args.apply:
            print("dry-run：未写库；统一迁移入口为 python -m app.migrations upgrade")
            return 0
        if args.target == "api":
            if engine.dialect.name != "postgresql":
                print("统一迁移仅支持 PostgreSQL；SQLite 只用于隔离测试和 dry-run")
                return 2
            from app.migrations import upgrade_schema, verify_schema
            upgrade_schema(engine)
            verify_schema(engine)
        else:
            from ibess_db.schema import require_schema
            require_schema(engine, TARGETS["event"]["tables"])
        print("统一迁移检查完成；消费者不执行建表")
        return 0
    except RuntimeError as exc:
        print(str(exc))
        return 2
    finally:
        engine.dispose()


if __name__ == "__main__":
    sys.exit(main())
