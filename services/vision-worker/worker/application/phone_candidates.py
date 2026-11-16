"""Application behavior with explicit runtime dependencies; no startup side effects."""

from __future__ import annotations


def _phone_vote(context, source_id: str, person_key: str, now: float) -> bool:
    """同一位置在时间窗内命中次数达到阈值才算确认，压掉单帧抖动；确认后清零，避免连续重复报。"""
    key = (source_id, person_key)
    slot = [
        t
        for t in context._PHONE_TRACKS.get(key, [])
        if now - t <= context.PHONE_VOTE_WINDOW
    ]
    slot.append(now)
    if len(slot) >= context.PHONE_VOTE_MIN_HITS:
        context._PHONE_TRACKS.pop(key, None)
        return True
    context._PHONE_TRACKS[key] = slot
    return False


def _vlm_judge(context, crop) -> tuple[bool, str]:
    """把裁剪图交给视觉大模型复核，返回 (是否认定为手机, 模型原话)。"""
    if not (context.VLM_BASE_URL and context.VLM_API_KEY):
        return True, "未配置大模型"
    ok, buffer = context.cv2.imencode(
        ".jpg", crop, [int(context.cv2.IMWRITE_JPEG_QUALITY), 85]
    )
    if not ok:
        return True, "编码失败"
    import base64 as _b64

    uri = "data:image/jpeg;base64," + _b64.b64encode(buffer.tobytes()).decode()
    body = {
        "model": context.VLM_MODEL,
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": context.VLM_QUESTION},
                    {"type": "image_url", "image_url": {"url": uri}},
                ],
            }
        ],
        # M3.1-Flash 是推理模型，token 太少会把额度全花在 reasoning_content 上、content 为空
        "max_tokens": 600,
        "temperature": 0,
    }
    request = context.urllib.request.Request(
        context.VLM_BASE_URL.rstrip("/") + "/chat/completions",
        data=context.json.dumps(body).encode(),
        headers={
            "Content-Type": "application/json",
            "Authorization": "Bearer " + context.VLM_API_KEY,
        },
    )
    try:
        with context.urllib.request.urlopen(
            request, timeout=context.VLM_TIMEOUT
        ) as response:
            payload = context.json.loads(response.read().decode())
        message = payload["choices"][0]["message"]
        text = str(message.get("content") or "").strip()
        if not text:
            context.log.warning("大模型复核无结论（content 为空），按通过处理")
            return True, "复核无结论"
    except context.urllib.error.HTTPError as exc:  # 带状态码，便于定位是额度还是参数
        detail = exc.read().decode(errors="ignore")[:160]
        context.log.warning("大模型复核失败：HTTP %s %s", exc.code, detail)
        return True, "复核不可用"
    except Exception as exc:  # 网络问题不能把检出一起否掉
        context.log.warning("大模型复核失败：%s", type(exc).__name__)
        return True, "复核不可用"
    head = text.lstrip()
    rejected = head.startswith(("无", "没有", "看不到", "不是"))
    return (not rejected), text[:80]


def _posture_of(context, text: str) -> str:
    """从模型回答里抽姿态，供算子决定是"通话"还是"桌拍"。"""
    t = text or ""
    if any(k in t for k in ("贴耳", "耳边", "耳旁", "接听")):
        return "ear"
    if any(k in t for k in ("胸前", "胸口", "胸前举")):
        return "chest"
    if any(k in t for k in ("对准文件拍摄", "拍照", "拍摄", "俯拍")):
        return "desk"
    if any(k in t for k in ("手持", "拿着", "手中", "握着")):
        return "hand"
    return ""


def _vlm_loop(
    context,
):
    while True:
        job = context._VLM_QUEUE.get()
        try:
            accepted, verdict = context._vlm_judge(job["crop"])
            context.log.info(
                "大模型复核 conf=%.2f -> %s | %s",
                job["conf"],
                "通过" if accepted else "否决",
                verdict,
            )
            if accepted:
                phone = dict(job["phone"])
                phone["posture"] = context._posture_of(verdict)
                phone["review"] = verdict[:60]
                with context._VLM_LOCK:
                    context._VLM_READY.append(
                        {"phone": phone, "person_key": job["person_key"]}
                    )
        except Exception as exc:
            context.log.warning("大模型复核线程异常：%s", exc)
        finally:
            context._VLM_INFLIGHT.pop(job["key"], None)
            context._VLM_QUEUE.task_done()


def _ensure_vlm_thread(
    context,
):
    pass  # mutable state belongs to the injected context
    if context._VLM_STARTED or not (
        context.VLM_REVIEW and context.VLM_BASE_URL and context.VLM_API_KEY
    ):
        return
    context._VLM_STARTED = True
    context.threading.Thread(
        target=context._vlm_loop, name="vlm-review", daemon=True
    ).start()
    context.log.info("大模型复核线程已启动（模型 %s）", context.VLM_MODEL)


def _drain_ready(context, source_id: str, now: float) -> list[dict]:
    """把复核通过、且通过时序投票的候选转成正式手机框。"""
    with context._VLM_LOCK:
        ready = context._VLM_READY[:]
        context._VLM_READY.clear()
    out = []
    for item in ready:
        if not context._phone_vote(source_id, item["person_key"], now):
            context.log.info(
                "手机候选复核通过，等待二次确认 conf=%.2f", item["phone"]["conf"]
            )
            continue
        context.log.info("手机确认 conf=%.2f", item["phone"]["conf"])
        out.append(item["phone"])
    return out


def scan_person_phones(context, detector, frame, persons, source_id: str = ""):
    """按人裁剪后检测手机：主循环不阻塞，大模型复核走后台线程，下一轮把结果并入。"""
    context._ensure_vlm_thread()
    found = context._drain_ready(source_id, context.time.time())
    height, width = frame.shape[:2]
    boxes = sorted(
        (p["bbox"] for p in persons),
        key=lambda b: (b[2] - b[0]) * (b[3] - b[1]),
        reverse=True,
    )[: context.PHONE_SCAN_MAX_PERSONS]
    for x1, y1, x2, y2 in boxes:
        w, h = x2 - x1, y2 - y1
        if w < 16 or h < 24:
            continue
        person_key = f"{int((x1 + x2) / 2) // 96}-{int((y1 + y2) / 2) // 96}"
        cx1 = int(max(0, x1 - 0.20 * w))
        cx2 = int(min(width, x2 + 0.20 * w))
        cy1 = int(max(0, y1 - 0.15 * h))
        cy2 = int(min(height, y2 + 0.10 * h))
        if cx2 - cx1 < 32 or cy2 - cy1 < 40:
            continue
        crop = frame[cy1:cy2, cx1:cx2]
        for phone in detector.scan_phones(
            crop, imgsz=context.PHONE_SCAN_IMGSZ, conf=context.PHONE_SCAN_CONF
        ):
            conf = phone["conf"]
            px1, py1, px2, py2 = phone["bbox"]
            record = {
                "class": "cell phone",
                "conf": conf,
                "bbox": [px1 + cx1, py1 + cy1, px2 + cx1, py2 + cy1],
            }
            if context.VLM_REVIEW and context.VLM_BASE_URL and context.VLM_API_KEY:
                key = f"{person_key}-{int(px1)}-{int(py1)}"
                now = context.time.time()
                if (
                    key in context._VLM_INFLIGHT
                    and now - context._VLM_INFLIGHT[key] < 20
                ):
                    continue
                if context._VLM_QUEUE.full():
                    continue
                context._VLM_INFLIGHT[key] = now
                context._VLM_QUEUE.put(
                    {
                        "key": key,
                        "crop": crop.copy(),
                        "conf": conf,
                        "phone": record,
                        "person_key": person_key,
                    }
                )
                continue
            if not context._phone_vote(source_id, person_key, context.time.time()):
                context.log.info("手机候选等待二次确认 conf=%.2f", conf)
                continue
            context.log.info("手机确认 conf=%.2f", conf)
            found.append(record)
    return found


def merge_phones(context, base, extra, min_gap=12.0):
    """整帧检测与裁剪扫描的结果合并，中心点太近的视为同一个手机。"""
    merged = list(base)
    for item in extra:
        cx = (item["bbox"][0] + item["bbox"][2]) / 2
        cy = (item["bbox"][1] + item["bbox"][3]) / 2
        duplicate = False
        for kept in merged:
            kx = (kept["bbox"][0] + kept["bbox"][2]) / 2
            ky = (kept["bbox"][1] + kept["bbox"][3]) / 2
            if abs(cx - kx) < min_gap and abs(cy - ky) < min_gap:
                duplicate = True
                if item["conf"] > kept["conf"]:
                    kept.update(item)
                break
        if not duplicate:
            merged.append(item)
    return merged
