# 最小垂直切片联调结果

日期：2026-09-21
环境：Windows 11 + Docker Desktop 4.69 + RTX 5060 8GB

## 结论：端到端切片链路验证通过 ✅

```
test_people.mp4 → FFmpeg推流 → mediamtx(RTSP/HLS) → vision-worker(YOLO)
    → Redis Streams(observations) → event-service → PostgreSQL(events)
    → api SSE → web-console(视频+实时告警)
```

## 各环节验证

| 环节 | 状态 | 验证依据 |
| --- | --- | --- |
| 视频源 | ✅ | test_people.mp4 循环推流到 mediamtx `cam1`，HLS 播放器页面可访问 |
| vision-worker | ✅ | 实时抽帧 → YOLO 检出 3~4 人 → 观察写入 Redis（每 200ms 一条） |
| event-service | ✅ | 消费观察 → 生成 Event → 写入 PostgreSQL（5 秒冷却去重） |
| api | ✅ | `GET /api/events` 返回事件；`GET /api/events/stream` SSE 实时推送 |
| web-console | ✅ | HLS 视频自动播放（readyState=4），SSE 告警实时显示（截图确认） |

## 运行中的服务

```
poc-postgres     (5432)  healthy
poc-redis        (6379)  healthy
poc-minio        (9000)  healthy
poc-mediamtx     (8554/8888/9997)
poc-publisher    (FFmpeg 推流容器)
poc-vision-worker  GPU YOLO 推理
poc-event-service   观察→事件
poc-api          (8000)  FastAPI + SSE
web-console dev   (3000)  Vite dev server
```

## 访问入口

- 前端控制台：http://localhost:3000
- API 文档：http://localhost:8000/docs
- HLS 播放：http://localhost:8888/cam1
- MinIO 控制台：http://localhost:9001

## 联调中发现并修复的问题

1. 服务模块导入路径（`from detector` → `from worker.detector` 等包结构）
2. api 的 `app/models/` 空目录与 `models.py` 冲突 → 删除空目录
3. SSE 长连接导致 Playwright `networkidle` 永不满足 → 改用 `domcontentloaded`
4. 视频自动播放被浏览器策略拦截 → video 加 `muted + autoPlay`

## 已知事项 / 下一步

- vision-worker 目前用 cv2.VideoCapture 拉流，生产建议换 FFmpeg 拉流（更稳）
- 事件为最简 `person_detected`，后续扩展 V01~V08 场景规则
- 视频截图见 `slice_screenshot.png`
