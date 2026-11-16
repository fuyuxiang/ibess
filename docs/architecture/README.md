# 项目结构与依赖边界

本项目采用单仓库、多进程服务部署。实时链路保持为：摄像头/音频 → 观察消息 → 事件服务 → PostgreSQL/Redis → API/SSE → 控制台；关键帧保存于 MinIO，监管录像单独保全并提供鉴权回放。

```text
ibess/
├── packages/ibess-common/       # 可安装的公共包
│   └── src/
│       ├── ibess_contracts/     # 消息、规则参数、JSON Schema、协议校验
│       ├── ibess_db/            # 公共 ORM 映射、Base、数据库版本检查
│       └── ibess_runtime/       # 无基础设施连接的依赖容器
├── services/
│   ├── api/
│   │   ├── app/routers/        # HTTP、认证依赖、请求及响应
│   │   ├── app/schemas/        # 请求模型
│   │   ├── app/application/    # 监管处置等事务用例
│   │   ├── app/core/           # 既有业务服务，按修改范围逐步归并
│   │   └── migrations/         # 唯一数据库迁移入口、冻结基线与表归属
│   ├── event-service/service/
│   │   ├── main.py            # 配置、状态和兼容入口的组装
│   │   ├── runtime.py         # 消费循环与定期维护任务
│   │   ├── application/       # 观察处理、持久化、配置刷新、阶段控制
│   │   ├── event_templates.py # 既有事件构造器
│   │   └── core_behaviors.py  # 时序行为规则
│   ├── vision-worker/worker/
│   │   ├── main.py            # 参数、依赖和兼容入口的组装
│   │   ├── runtime.py         # 视频采集、推理及发布流程
│   │   ├── application/       # 手机候选复核
│   │   └── infrastructure/    # 观察发布、区域读取、证据上传
│   └── ...                    # 音频、语义、媒体及离线质检服务
├── web-console/                # 原有路由、页面、组件、API 与状态管理
├── requirements/               # 公共依赖约束和开发环境锁文件
├── tests/contracts/            # 公共协议兼容与负例测试
├── tests/architecture/         # 依赖边界、表归属和镜像输入约束
├── tools/check.py              # 所有本地及 CI 检查的统一入口
├── deploy/                     # 生产覆盖、推理基线、可选服务、临时测试依赖
└── docs/                       # 架构决策、协议、迁移及验证说明
```

`main.py` 保留既有函数和导入路径，调用拆出的应用函数，并注入运行时依赖。`Namespace` 是对组装入口状态的实时视图，使现有配置热更新及依赖替换继续生效；应用模块本身不启动线程、不连接基础设施。新用例可以直接注入独立对象进行测试。

公共协议不允许导入 `app`、`worker`、`service` 或 ORM。公共持久化包只定义映射与版本检查；API、事件、媒体服务导入同一套事件和证据模型。旧 `models.py` 和 `rule_parameters.py` 作为兼容导出保留，不再复制定义。

目前仍共用 PostgreSQL，视觉服务读取阶段和授权快照，部分消费者更新其负责的数据。表结构变更统一由 API 的 Alembic 迁移执行；运行中的消费者只检查版本。数据库访问角色尚未拆成每服务独立账号，当前并非独立数据库的微服务架构。

参见：[数据库迁移与归属](database.md)、[共享数据库决策](decisions/0001-shared-database.md)、[开发与交付检查](../development.md)、[公共协议](../contracts/README.md)。
