"""Application behavior with explicit runtime dependencies; no startup side effects."""

from __future__ import annotations


def refresh_operator_cache(context, force: bool = False):
    """热刷新算子配置；读取失败时保留上一份有效配置。"""
    pass  # mutable state belongs to the injected context
    now = context.time.time()
    if not force and now - context.operator_cache_at < context.OPERATOR_REFRESH_SECONDS:
        return context.operator_cache
    session = context.Session()
    try:
        configs: dict[str, context.OperatorConfig] = {}
        receipt = {}
        for operator in session.query(context.Operator).all():
            entry = {
                "updated_at": operator.updated_at.isoformat()
                if operator.updated_at
                else None,
                "enabled": operator.enabled,
                "error": None,
            }
            receipt[operator.operator_id] = entry
            if not operator.enabled:
                continue
            from service.operators import HANDLERS

            if operator.code not in HANDLERS:
                entry["error"] = "当前消费者未部署该规则"
                continue
            bindings = (
                session.query(context.OperatorBinding)
                .filter(
                    context.OperatorBinding.operator_id == operator.operator_id,
                    context.OperatorBinding.enabled == True,
                )
                .all()
            )
            targets = bindings or [None]
            for binding in targets:
                params = dict(operator.parameters or {})
                if binding:
                    params.update(binding.parameters or {})
                try:
                    context.validate_parameters(operator.code, params)
                except (TypeError, ValueError):
                    entry["error"] = "规则参数无效，该作用域未应用"
                    continue
                key = f"{operator.code}:{binding.binding_id if binding else 'global'}"
                configs[key] = context.OperatorConfig(
                    code=operator.code,
                    version=operator.version,
                    enabled=operator.enabled,
                    input_types=tuple(operator.input_types or []),
                    output_event_type=operator.output_event_type,
                    risk_level=operator.risk_level,
                    parameters=params,
                    session_id=binding.session_id if binding else None,
                    camera_id=binding.camera_id if binding else None,
                    zone_id=binding.zone_id if binding else None,
                )
        session.execute(
            context.text("""INSERT INTO supervision_operator_receipts(consumer_id,configuration,applied_at)
            VALUES ('event-service',CAST(:configuration AS jsonb),:at)
            ON CONFLICT(consumer_id) DO UPDATE SET configuration=EXCLUDED.configuration,applied_at=EXCLUDED.applied_at"""),
            {
                "configuration": context.json.dumps(receipt),
                "at": context.datetime.utcnow(),
            },
        )
        session.commit()
        context.operator_cache = configs
        context.operator_cache_at = now
        return context.operator_cache
    except Exception as exc:
        context.log.warning("算子配置刷新失败，继续使用旧配置: %s", exc)
        return context.operator_cache
    finally:
        session.close()


def load_phase_cache_from_db(
    context,
):
    """启动时从库里读一次当前场次的阶段场景，避免依赖控制消息（消息丢了会变成"不过滤"）。"""
    session = context.Session()
    try:
        row = (
            session.execute(
                context.text(
                    "select s.session_id, p.enabled_scenes from eval_sessions s "
                    "join phase_snapshots p on p.session_id = s.session_id "
                    "where s.status in ('active','running') "
                    "order by s.updated_at desc, p.effective_at desc limit 1"
                )
            )
            .mappings()
            .first()
        )
        if not row:
            context.log.warning("阶段缓存初始化失败：没有可用场次，暂不按阶段过滤")
            return
        scenes = set(row["enabled_scenes"] or [])
        context.phase_scene_cache[row["session_id"]] = scenes
        # 演示 worker 固定上报 session-001，同步一份
        context.phase_scene_cache["session-001"] = scenes
        context.log.info(
            "阶段缓存初始化：session=%s scenes=%s", row["session_id"], sorted(scenes)
        )
    except Exception as exc:
        context.log.warning("阶段缓存初始化失败：%s", exc)
    finally:
        session.close()


def active_session_id(
    context,
) -> str | None:
    """当前运行中的场次。演示 worker 一律上报 session-001，这里换成真实场次 UUID，
    否则阶段看板、监管档案按场次统计永远是 0。缓存 15 秒。"""
    now = context.time.time()
    if now - context._SESSION_CACHE["at"] < 15:
        return context._SESSION_CACHE["value"]
    session = context.Session()
    try:
        row = session.execute(
            context.text(
                "select session_id from eval_sessions where status = 'running' "
                "order by start_at desc nulls last, id desc limit 1"
            )
        ).scalar()
        context._SESSION_CACHE.update(at=now, value=row)
        return row
    except Exception as exc:  # noqa: BLE001
        context.log.warning("读取运行场次失败: %s", type(exc).__name__)
        return context._SESSION_CACHE["value"]
    finally:
        session.close()


def current_phase_id(
    context,
) -> str | None:
    """当前生效阶段：取 effective_at 已到的最近一条快照，缓存 30 秒。

    事件与阶段原本各用一套 session（检测是 session-001，阶段配置是场次 UUID），
    靠 session 关联不上，这里直接按"最新生效快照"归属，单场次演示场景下是准确的。
    """
    now = context.time.time()
    if now - context._PHASE_CACHE["at"] < 30:
        return context._PHASE_CACHE["value"]
    session = context.Session()
    try:
        row = session.execute(
            context.text(
                "select phase_id from phase_snapshots "
                "where effective_at <= now() order by effective_at desc limit 1"
            )
        ).scalar()
        context._PHASE_CACHE.update(at=now, value=row)
        return row
    except Exception as exc:  # noqa: BLE001
        context.log.warning("读取当前阶段失败: %s", type(exc).__name__)
        return context._PHASE_CACHE["value"]
    finally:
        session.close()
