"""Application behavior with explicit runtime dependencies; no startup side effects."""

from __future__ import annotations


async def consume(
    context,
):
    r = context.redis.from_url(context.REDIS_URL)
    context.log.info(
        "event-service 启动，流: %s，分级冷却: 默认%.0fs person=%.0fs crowd=%.0fs",
        context.STREAM,
        context.DEFAULT_COOLDOWN_SECONDS,
        context.COOLDOWN_BY_TYPE["person_detected"],
        context.COOLDOWN_BY_TYPE["crowd_gathering"],
    )
    last_emit = {}
    last_id = "$"  # 只消费启动后的新消息，避免重放历史
    control_id = "$"
    context.refresh_operator_cache(force=True)

    while True:
        try:
            entries = await r.xread(
                {context.STREAM: last_id, "phase-control": control_id},
                block=1000,
                count=100,
            )
            for stream, items in entries:
                stream_name = stream.decode() if isinstance(stream, bytes) else stream
                for msg_id, data in items:
                    if stream_name == "phase-control":
                        control_id = msg_id
                        await context.handle_phase_control(r, context._decode(data))
                        continue
                    last_id = msg_id
                    data = context._decode(data)
                    obs_type = data.get("type")
                    now = context.time.time()
                    # 冷却下沉到 handle_observation 内部：状态更新与算子评估每条观察都执行，
                    # 只有事件创建被冷却拦截（5.1 离席状态需要密集采样）。
                    await context.handle_observation(r, data, last_emit)
        except context.asyncio.CancelledError:
            break
        except Exception as e:
            context.log.warning("消费异常: %s", e)
            await context.asyncio.sleep(1)


async def auto_close_pending(
    context,
):
    """按类型分级自动关闭：态势类 10 分钟、线索类 60 分钟、违规类 2 小时。

    不做这一步，处置队列会被人员检测和聚集事件淹没（实测一小时 400+ 条），
    首页"待人工处置"永远是个吓人的数字。
    """
    context.log.info("自动关闭任务启动：%s", context.AUTO_CLOSE_MINUTES)
    while True:
        await context.asyncio.sleep(300)
        closed = 0
        session = context.Session()
        try:
            for event_type, minutes in context.AUTO_CLOSE_MINUTES.items():
                result = session.execute(
                    context.text(
                        "update events set status='CLOSED', updated_at=now() "
                        "where status='PENDING' and event_type=:event_type "
                        "and occurred_at < now() - (:minutes * interval '1 minute')"
                    ),
                    {"event_type": event_type, "minutes": minutes},
                )
                closed += result.rowcount or 0
            session.commit()
        except Exception as exc:
            session.rollback()
            context.log.warning("自动关闭失败：%s", exc)
        finally:
            session.close()
        if closed:
            context.log.info("自动关闭过期待处置事件 %d 条", closed)


async def cleanup_old_data(
    context,
):
    """仅清理超期基础观察；监管事件、语义关联、归档与证据持续保留。"""
    retention_days = float(
        context.os.getenv(
            "OBSERVATION_RETENTION_DAYS", context.os.getenv("RETENTION_DAYS", "7")
        )
    )
    context.log.info("数据清理任务启动，保留 %s 天", retention_days)
    while True:
        try:
            cutoff = context.datetime.utcnow() - context.timedelta(days=retention_days)
            session = context.Session()
            try:
                ids = [
                    r[0]
                    for r in session.query(context.Event.event_id)
                    .filter(
                        context.Event.created_at < cutoff,
                        context.Event.risk_level == "info",
                    )
                    .filter(
                        context.text(
                            "NOT EXISTS (SELECT 1 FROM supervision_cases c WHERE c.event_id=events.event_id)"
                        )
                    )
                    .all()
                ]
                if ids:
                    session.query(context.Evidence).filter(
                        context.Evidence.event_id.in_(ids)
                    ).delete(synchronize_session=False)
                    session.query(context.Decision).filter(
                        context.Decision.event_id.in_(ids)
                    ).delete(synchronize_session=False)
                    deleted = (
                        session.query(context.Event)
                        .filter(context.Event.event_id.in_(ids))
                        .delete(synchronize_session=False)
                    )
                    session.commit()
                    context.log.info(
                        "清理超期数据完成：%d 条事件及关联证据/处置", deleted
                    )
                else:
                    context.log.info("无超期数据需清理")
            finally:
                session.close()
        except Exception as e:
            context.log.warning("数据清理失败: %s", e)
        await context.asyncio.sleep(86400)


async def main(
    context,
):
    context.initialize_database()
    context.load_phase_cache_from_db()
    await context.asyncio.gather(
        context.consume(), context.cleanup_old_data(), context.auto_close_pending()
    )
