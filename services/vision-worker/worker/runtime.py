"""Application behavior with explicit runtime dependencies; no startup side effects."""

from __future__ import annotations


def main(
    context,
):
    from worker.person_tracking import IouTracker, tracking_detections

    rtsp = context.os.getenv("RTSP_URL", "")
    redis_url = context.os.getenv("REDIS_URL", "redis://localhost:6379/0")
    model_path = context.os.getenv("YOLO_MODEL", "/assets/yolov8n.pt")
    conf = float(context.os.getenv("YOLO_CONF", "0.25"))
    imgsz = int(
        context.os.getenv("DETECT_IMGSZ", context.os.getenv("INFERENCE_SIZE", "480"))
    )
    fps_target = float(context.os.getenv("VIDEO_FPS", "5"))
    source_id = context.os.getenv("CAMERA_ID", "camera-001")
    session_id = context.os.getenv("SESSION_ID", "session-001")

    if not rtsp:
        context.log.error("未配置 RTSP_URL")
        return

    r = context.redis.from_url(redis_url)
    minio_client, minio_bucket = context.init_minio()
    det = context.PersonDetector(
        model_path, conf=conf, imgsz=imgsz, device=context.os.getenv("DEVICE", "cpu")
    )
    context.log.info("加载模型完成: %s (conf=%.2f, imgsz=%d)", model_path, conf, imgsz)
    zones = context.load_zones(source_id)
    seat_absent: dict[str, float] = {}
    tracking_run = context.uuid.uuid4().hex
    person_tracker = IouTracker(tracking_run, max_missed_ms=2000)
    a_ext = None
    try:
        from worker.a_extensions import AExtensions

        # 扩展场景显式接线启用；避免与现有基础算子同时默认上报同类线索。
        a_ext = AExtensions.from_env(
            source_id,
            env={
                **context.os.environ,
                "A_EXT_ENABLED": context.os.getenv("A_EXT_ENABLED", "1"),
            },
        )
    except Exception:
        context.log.warning("扩展场景初始化失败，继续基础采集", exc_info=True)
    if zones:
        context.log.info("工位区已加载：%s", ", ".join(z["name"] for z in zones))

    from worker.core_behaviors import CorePipeline

    core = CorePipeline.from_env(source_id, context.os.environ)

    interval = 1.0 / fps_target
    latest_capture = context.os.getenv("LATEST_FRAME_CAPTURE", "0") == "1"
    from worker.frame_capture import LatestFrameReader

    cap = LatestFrameReader(rtsp) if latest_capture else context.cv2.VideoCapture(rtsp)
    if not cap.isOpened():
        context.log.error("无法打开视频源: %s", rtsp)
        return

    last_frame_time = context.time.time()
    offline_reported = False
    black_started = None
    black_reported = False
    last_keyframe_time = 0.0
    last_phone_scan = 0.0
    stage_grab = stage_read = stage_det = 0.0
    stage_n = 0

    context.log.info("开始处理视频源: %s, %d FPS", rtsp, fps_target)
    while True:
        now = context.time.time()
        if latest_capture and cap.captured_at is not None:
            last_frame_time = max(last_frame_time, cap.captured_at)

        # V08 断流检测：3 秒无有效帧，状态变化时发布一次
        if now - last_frame_time > context.OFFLINE_SECONDS and not offline_reported:
            context.log.warning(
                "视频流中断（%s 秒无帧），上报 video_offline", context.OFFLINE_SECONDS
            )
            context.publish(
                r, "video_offline", source_id, session_id, {"reason": "no_frame"}
            )
            offline_reported = True

        t_stage = context.time.time()
        for _ in range(context.FRAME_DROP):
            if not cap.grab():
                break
        t_grab = context.time.time() - t_stage
        t_stage = context.time.time()
        ok, frame = cap.read()
        t_read = context.time.time() - t_stage
        if not ok:
            if latest_capture:
                continue  # producer reconnects; keep the offline timer and media run intact
            cap.release()
            context.time.sleep(2)
            cap = context.cv2.VideoCapture(rtsp)
            continue
        last_frame_time = (
            cap.last_captured_at if latest_capture else context.time.time()
        )
        captured_at = last_frame_time
        if offline_reported:
            context.log.info("视频流恢复，上报 video_online")
            context.publish(r, "video_online", source_id, session_id, {})
            offline_reported = False

        # V07 黑屏/遮挡：灰度方差低于阈值持续 1 秒，状态变化时发布一次
        gray = context.cv2.cvtColor(frame, context.cv2.COLOR_BGR2GRAY)
        variance = float(gray.var())
        if variance < context.BLACK_VAR_THRESHOLD:
            if black_started is None:
                black_started = now
                context.log.info("画面异常（方差 %.1f）开始计时", variance)
            if now - black_started > context.BLACK_SECONDS and not black_reported:
                context.publish(
                    r, "camera_blacked", source_id, session_id, {"variance": variance}
                )
                black_reported = True
        else:
            if black_reported:
                context.log.info("画面恢复，上报 camera_normal")
                context.publish(r, "camera_normal", source_id, session_id, {})
            black_started = None
            black_reported = False

        t0 = context.time.time()
        dets, phones = det.detect(frame)
        if (
            context.PHONE_SCAN
            and dets
            and now - last_phone_scan >= context.PHONE_SCAN_INTERVAL
        ):
            last_phone_scan = context.time.time()
            phones = context.merge_phones(
                phones, context.scan_person_phones(det, frame, dets, source_id)
            )
            if phones:
                context.log.info(
                    "检出手机：%d 个，最高置信度 %.2f",
                    len(phones),
                    max(p["conf"] for p in phones),
                )
        # 5.1 离席判定需要"无人"信号：恒发 person_detected（count=0 也发）；
        # 无人时也定期保存关键帧，供离席证据引用。事件量由 event-service 分级冷却控制。
        now = context.time.time()
        frame_key = None
        if now - last_keyframe_time >= context.KEYFRAME_INTERVAL:
            frame_key = context.save_keyframe(
                minio_client, minio_bucket, frame, source_id
            )
            last_keyframe_time = now
        max_conf = max((d.get("conf", 0) for d in dets), default=0)
        # 手机框与单帧姿态仅提供候选；通话/拍摄规则使用连续画面复核结果。
        height, width = frame.shape[:2]

        def _norm(box):
            return [
                round(box[0] / width, 4),
                round(box[1] / height, 4),
                round(box[2] / width, 4),
                round(box[3] / height, 4),
            ]

        seats = []
        if zones:
            boxes = [d["bbox"] for d in dets]
            for z in zones:
                in_zone = [
                    b
                    for b in boxes
                    if context.point_in_polygon(
                        (b[0] + b[2]) / 2 / width,
                        (b[1] + b[3]) / 2 / height,
                        z["polygon"],
                    )
                ]
                if in_zone:
                    seat_absent.pop(z["name"], None)
                    seats.append(
                        {
                            "zone_id": z.get("zone_id", z["name"]),
                            "name": z["name"],
                            "type": z["type"],
                            "absent_sec": 0.0,
                            "person_count": len(in_zone),
                        }
                    )
                else:
                    start = seat_absent.setdefault(z["name"], now)
                    seats.append(
                        {
                            "zone_id": z.get("zone_id", z["name"]),
                            "name": z["name"],
                            "type": z["type"],
                            "absent_sec": round(now - start, 1),
                            "person_count": 0,
                        }
                    )
        # 聚集判定要用"有效人数"：画面里远处路过的人框很小，算进去会把 3 人误判成 5 人
        qualified = [
            d
            for d in dets
            if ((d["bbox"][2] - d["bbox"][0]) * (d["bbox"][3] - d["bbox"][1]))
            / (width * height)
            >= 0.012
        ]
        tracks = person_tracker.update(
            tracking_detections(dets, width, height),
            int(context.time.monotonic() * 1000),
        )
        payload = {
            "count": len(dets),
            "count_qualified": len(qualified),
            "media_run_id": tracking_run,
            "person_tracks": [t.to_dict() for t in tracks],
            "max_conf": max_conf,
            "frame_key": frame_key,
            "seats": seats,
            "phone_count": len(phones),
            "phone_conf_max": max((p.get("conf", 0) for p in phones), default=0),
            "phone_boxes": [_norm(p["bbox"]) for p in phones[:5]],
            "phone_postures": [p.get("posture", "") for p in phones[:5]],
            "person_boxes": [_norm(d["bbox"]) for d in dets[:5]],
        }
        a_events = []
        if a_ext is not None:
            try:
                extra, a_events = a_ext.on_frame(frame, dets, phones)
                payload.update(
                    {key: value for key, value in extra.items() if key not in payload}
                )
            except Exception:
                context.log.warning("扩展场景处理失败，继续基础采集", exc_info=True)
        context.publish(
            r, "person_detected", source_id, session_id, payload, captured_at
        )
        if core is not None:
            try:
                for observation in core.on_frame(
                    frame,
                    payload["person_tracks"],
                    tracking_run,
                    captured_at,
                    frame_key,
                    degraded=variance < context.BLACK_VAR_THRESHOLD,
                ):
                    if observation.get("capture_jpeg"):
                        capture = context.cv2.imdecode(
                            context.np.frombuffer(
                                observation["capture_jpeg"], dtype=context.np.uint8
                            ),
                            context.cv2.IMREAD_COLOR,
                        )
                        observation["payload"]["frame_key"] = context.save_keyframe(
                            minio_client, minio_bucket, capture, source_id
                        )
                    context.publish(
                        r,
                        "behavior_detected",
                        source_id,
                        observation["session_id"],
                        observation["payload"],
                        observation["occurred_at"],
                    )
            except Exception:
                context.log.warning("核心行为识别本帧失败", exc_info=True)
        for a_event in a_events:
            context.publish(r, "a_scene_event", source_id, session_id, a_event)

        dt = context.time.time() - t0
        stage_grab += t_grab
        stage_read += t_read
        stage_det += dt
        stage_n += 1
        if stage_n % 30 == 0:
            context.log.info(
                "耗时统计（%d 轮平均）：grab %.3fs | read %.3fs | 检测+发布 %.3fs | 循环周期 %.3fs",
                stage_n,
                stage_grab / stage_n,
                stage_read / stage_n,
                stage_det / stage_n,
                (stage_grab + stage_read + stage_det) / stage_n,
            )
            stage_grab = stage_read = stage_det = 0.0
        if dt < interval:
            context.time.sleep(interval - dt)
