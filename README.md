# 智能评标监管系统

面向招投标评标现场的异常行为分析预警与监管处置系统。沿用项目现有的视频、音频、目标检测、语音转写及语义分析能力，前台以“发现—确认—处置—留痕”组织工作：集中核查事件证据，登记处置责任与反馈，经人工复核后归档。技术故障与专家行为预警分别展示，机器线索不代替人工认定。

实际画面、模型与录像状态以部署服务回执为准。Compose 项目名、容器和镜像使用 `poc`；现有持久化卷保留历史名称，避免误删业务数据。部署前执行 `docker compose config --quiet` 检查配置。

### 现有环境迁移

当前Compose使用external卷复用迁移前的数据库、Redis、MinIO和音频数据：`demo_pgdata`、`demo_redisdata`、`demo_miniodata`、`demo_audio-data`。这些是现有卷标识，保留数据时不要直接改名或删除。

全新服务器可先创建`poc_pgdata`、`poc_redisdata`、`poc_miniodata`、`poc_audio-data`，再在服务器的`.env`设置`POC_PG_VOLUME_NAME`、`POC_REDIS_VOLUME_NAME`、`POC_MINIO_VOLUME_NAME`、`POC_AUDIO_VOLUME_NAME`指向对应新卷；迁移现有服务器则必须同时迁移数据。

## 目录结构

```
ibess/
├── docker-compose.yml        # 单机部署编排（多路视频）
├── .env.example              # 环境变量模板
├── deploy/
│   └── inference-base/       # cu128 GPU 推理基线镜像（ultralytics + funasr）
├── packages/ibess-common/     # 公共协议、持久化映射及依赖容器
├── requirements/             # 公共依赖约束和开发锁文件
├── tests/                    # 契约及架构边界测试
├── tools/check.py            # 本地与 CI 统一检查入口
├── docs/                     # 架构、协议、迁移及开发说明
├── services/
│   ├── api/                  # FastAPI：场次、本体对象、事件、处置、证据、系统状态
│   ├── media-service/        # 视频接入、循环缓存、HLS、证据片段截取
│   ├── vision-worker/        # YOLO 检测 + ByteTrack 跟踪 + 区域规则 + 画面质量
│   ├── audio-worker/         # VAD + 流式 ASR + 敏感词 + 持续讲话规则
│   ├── semantic-worker/      # 语义任务及结果校验
│   ├── offline-worker/       # 离线质检和复用规则
│   └── event-service/        # 观察 → 业务事件，连续确认、去重、状态机
├── web-console/              # React + Vite + Ant Design 监评控制台
├── scripts/                  # 初始化、演示数据、测试脚本
└── config/                   # 可提交的公共配置
```

## 技术栈

| 能力域 | 选型 |
| --- | --- |
| 前端 | React、TypeScript、Vite、Ant Design、hls.js |
| 业务 API | Python 3.12、FastAPI、Pydantic、SQLAlchemy、Alembic |
| 实时推送 | SSE |
| 媒体 | FFmpeg、MediaMTX |
| 视觉 | Ultralytics YOLO、ByteTrack、OpenCV |
| 语音 | FunASR Paraformer、FSMN-VAD、CT-Punc |
| 实时任务 | Redis Streams |
| 存储 | PostgreSQL 16、MinIO |

## 快速开始

```bash
python3 scripts/init_env.py  # 创建权限为 600 的 .env，并随机生成基础设施口令
# 按实际环境补充 RTSP_URL、大模型密钥和部署参数
docker compose config --quiet
docker compose up -d   # 启动全部服务
```

- 监评控制台：http://localhost:3000
- API 文档：http://localhost:8000/docs
- MinIO 控制台：http://localhost:9001

`.env` 只保存在本机；模板中的口令留空，Compose 遇到未配置的数据库或对象存储口令会直接报错。若复用已有数据卷，请在 `.env` 填入该环境现有的凭据；初始化脚本不会重置数据库、对象存储或网页账号的密码，也不会覆盖已有 `.env`。

SSH 目标通过 `.env` 的 `DEPLOY_SSH_HOST`、`DEPLOY_SSH_USER`、`DEPLOY_REMOTE_DIR` 配置；特殊 Node 路径使用 `POC_NODE_BIN`，截图输出路径使用 `POC_SCREENSHOT_DIR`。授权媒体放在仓库外，通过 `POC_VIDEO_DIR` 指定；前端默认使用不含个人信息的 SVG 示意图。

分发源码使用 `python3 scripts/package_source.py --output deploy/linux/out/code.tar.gz`。代码包会排除本机配置、凭据、原始媒体、运行报告、备份和模型；服务器需要单独配置 `.env`，不能用代码包传递凭据。原始现场素材、历史文档和运行报告已从代码目录清除；公开来源的测试图片和各服务自身的测试代码保留。工具生成的报告写入已忽略的 `tmp/reports/`。

## 工程组织与统一检查

项目结构、依赖边界及渐进整理范围见 [架构说明](docs/architecture/README.md)。数据库改表统一进入 API 的 Alembic 版本迁移；消费者只检查结构。公共事件、证据映射及规则参数通过 `packages/ibess-common` 复用，旧导入路径继续兼容。

开发先执行 `make bootstrap`，然后在 `web-console` 执行 `npm ci`。`make test` 运行无需业务数据库的检查；`make check` 还需要显式的专用 PostgreSQL/Redis 测试连接。完整命令、临时依赖启动方式和跳过项说明见 [开发与交付](docs/development.md)。CI 使用相同入口。

Python 应用与 GPU 基线镜像统一以仓库根为构建上下文，使用服务内锁文件安装依赖。部署升级和历史库兼容策略见 [数据库迁移](docs/architecture/database.md)。

## 当前状态

前端验证在 `web-console` 目录执行：

```bash
npm ci
npm test
npm run build
```

现用入口包括监管工作台、现场监测、事件中心、监管档案及专项工具；旧地址 `/manual`、`/evidence`、`/history`、`/show` 继续兼容。区域编辑组件及相关后端扩展保留，尚未接入主界面。

- [x] 环境基线：cu128 GPU 推理容器验证
- [x] 最小垂直切片：1 路视频 → YOLO 检测 → 事件 → SSE 告警 → 前端展示
- [x] 视频场景：V08 断流、V07 遮挡/黑屏（已完成）
- [x] 音频链路：A01 实时转写、A02 敏感词命中（已完成，文件模拟源）
- [x] 音频链路：A03 持续讲话、A04 音频中断（已完成，文件模拟源）
- [x] 事件处置闭环：事件详情、人工确认/误报/升级/关闭、处置记录、历史查询筛选（已完成）
- [x] 证据留存：检测关键帧自动保存 MinIO，事件关联证据，回放地址可访问（已完成）
- [x] 摄像头规则链路：手机、区域、交头接耳、串岗、资料递接、频繁/共同离席、入口授权（代码与链路测试完成，现场样本待验收）
- [ ] 联调调优与演示

监管事件录像另存于 `poc_supervision_evidence` 持久化卷，与实时录像轮转隔离；备份使用 `scripts/backup_poc.ps1`，恢复前核对备份清单与哈希。

## 算子化场景能力

系统支持通过 `/api/operators` 管理场景算子。算子代码随镜像发布，算子的启停、参数和场次/摄像头/区域绑定通过 API 管理，消费者正常运行时约 3 秒刷新配置；以页面应用回执确认是否重新读取，不能仅凭保存成功判断生效。启动后可在本机 `/docs` 查看 API 文档。


## 摄像头核心行为规则（camera-v2）

新增六项规则：疑似交头接耳、疑似串岗、疑似资料递接、频繁离席、多人同时离席、疑似未授权人员进入。
API 启动会增量注册规则；保留已有开关与自定义参数。原通话/拍摄算子追加连续画面观察输入。
空间聚集同时检查距离与人数；限制区默认覆盖单人。手机几何位置本身不再定性为通话或拍摄。

部署与配置：

1. 执行 `python3 scripts/fetch_face_models.py`，按既有清单下载并校验 YuNet/SFace 模型。模型挂载到视觉容器 `/face-models`，人脸特征只在本地比对。
2. 配置支持图片输入的 `VISUAL_MODEL_BASE_URL`、`VISUAL_MODEL_NAME`、`VISUAL_MODEL_API_KEY`；留空时 Compose 复用现有语义模型地址、密钥和 `VLM_MODEL`。必须确认所选服务支持多图输入；无配置或调用失败时，规则页显示受限状态。
3. 在场次中绑定项目、评标室和视频源；在区域配置中画出每个工位及入口线。入口线正侧为进入方向；身份规则只在实际越线后核验。辅助视频通道也支持独立区域配置。
4. 按阶段启用 S03（离席）、S04（入口权限）、S05（交流和串岗）、S08（资料和拍摄）。新模板已补齐对应场景；已有场次配置不会被启动过程重写。交头接耳、串岗、资料递接默认仅在初步/详细独立评审阶段输出。
5. `docker compose config --quiet` 后重建 API、事件服务及使用中的视觉服务。核对规则页“消费者应用”和各机位“识别来源”；配置已读取不表示模型或画面已经就绪。

`CORE_BEHAVIORS_ENABLED=1`、`A_EXT_ENABLED=1` 为默认值。基础工位离席及新动作规则已接管相同场景，因此 Compose 默认关闭旧的 `A_S03_SEAT_ENABLED` / `A_S05_ENABLED` 重复线索；资料携离和画面质量扩展仍启用。

频繁/共同离席按工位占用变化判断，不将初始空座、故障期间或获准离岗计入；入口画面的连续跨线轨迹可另行形成疑似共同离室依据。短时 IoU 轨迹不跨摄像头关联身份。交头接耳和资料递接使用有界异步队列分析连续画面；模型只能引用输入轨迹及帧编号，不能生成姓名、授权或时间。模型未配置时不会伪造识别结果。

例外沿用业务事实与阶段快照：离岗许可自动读取；其他许可可登记为 `clarification` 事实，在 `payload.authorization_kind` 指定 `contact_allowed`、`document_access`、`photo_allowed`、`device_allowed` 或 `break`，并设置带时区的 `valid_until`。事实绑定工位则仅作用于该工位，无工位/人员则作用于本室；阶段 `exceptions` 必须引用该不可变事实的 `fact_id` 与 `fact_version=1`。仅人员许可在未建立视频身份关联时不自动套用。

新规则复用既有事件处置、SSE 推送、关键帧及多来源录像保全。Compose 默认开启 `LATEST_FRAME_CAPTURE=1`，专用读帧线程持续消化 RTSP 缓冲，推理线程读取最新帧。延迟复核保留采集端收到引用帧的 UTC 时间，截图使用被复核的原始帧；证据窗口按行为起止及前后 45 秒定位，文件保全后按 SHA256 校验并支持鉴权 Range 回放。缺失录像会显示缺口，不拿其他时刻的录像代替。

可重复验证：`services/event-service/tests/test_core_behaviors.py`、`services/vision-worker/tests/test_core_behaviors.py` 和 `services/api/tests/test_camera_rules.py`。API 集成测试需显式指定专用 `WORKFLOW_TEST_DATABASE_URL`，使用隔离 schema，覆盖六项告警、真实入口模型、真实 MP4 保全和原片轮转后的回放。模型准确率仍须用现场机位的正负样本验收。

真实视觉接口验证可运行 `python3 scripts/verify_visual_model.py`：读取本机 `.env`，发送八帧不含人员的合成空评标室画面，校验正式提示词及结构化返回；报告保存到 `tmp/reports/visual-model-smoke.json`，不包含密钥。MiniMax M3 系列使用 `reasoning_effort=low` 与足够的 `max_completion_tokens`，避免思考预算耗尽导致正文为空，参数依据见 [MiniMax 官方接口说明](https://platform.minimax.io/docs/api-reference/text-openai-api)。
