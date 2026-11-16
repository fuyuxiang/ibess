"""Application behavior with explicit runtime dependencies; no startup side effects."""

from __future__ import annotations


def publish(context, r, obs_type, source_id, session_id, detail, occurred_at=None):
    from ibess_contracts.messages import observation_message

    obs = observation_message(
        obs_type,
        source_id,
        session_id,
        detail,
        context.time.time() if occurred_at is None else occurred_at,
    )

    try:
        r.xadd(context.STREAM, obs)
        r.xtrim(context.STREAM, maxlen=context.STREAM_MAXLEN)
        context.log.info("观察已发布: %s", obs_type)
    except Exception as e:
        context.log.warning("Redis 发布失败: %s", e)


def load_zones(context, camera_id: str) -> list[dict]:
    try:
        with context._zone_engine.begin() as conn:
            rows = (
                conn.execute(
                    context.text(
                        "select zone_id, name, type, polygon from zones where camera_id = :cid and polygon is not null"
                    ),
                    {"cid": camera_id},
                )
                .mappings()
                .all()
            )
        return [
            {
                "zone_id": r["zone_id"],
                "name": r["name"],
                "type": r["type"] or "seat",
                "polygon": r["polygon"],
            }
            for r in rows
        ]
    except Exception as exc:  # noqa: BLE001
        context.log.warning("区域配置读取失败：%s", type(exc).__name__)
        return []
