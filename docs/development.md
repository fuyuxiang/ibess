# 开发、测试与交付

标准 Python 环境为 3.11 或 3.12，生产 CPU 镜像使用 3.12，GPU 推理基线沿用 PyTorch 2.8 / CUDA 12.8。Node 使用 20，前端用 `npm ci` 安装锁文件。

首次准备：

```bash
make bootstrap
cd web-console
npm ci
cd ..
.venv/bin/python scripts/fetch_face_models.py
```

Windows 可用 Python venv 的 `Scripts/python.exe` 执行对应命令，无需安装 Make。公共包需要显式安装：`python -m pip install --no-deps -e packages/ibess-common`。单独在某个服务目录运行测试时，使用同一个虚拟环境，并将该服务根目录放入 `PYTHONPATH`。

统一入口为 `tools/check.py`，Make 只是快捷方式：

| 命令 | 内容 |
| --- | --- |
| `make lint` | 全仓库 Python 关键错误检查 |
| `make test` | 契约、架构约束、六类消费者、部署脚本及恢复逻辑测试 |
| `make integration` | PostgreSQL / Redis 集成、全部 API 测试类、音频队列及离线任务 |
| `make web` | 前端单元测试、TypeScript 和生产构建 |
| `make compose` | 基础、Linux、可选服务三种编排解析 |
| `make check` | 执行以上全部检查 |

`make test` 不连接业务数据库。数据库集成必须显式指定专用连接，检查器不会读取 `.env` 中的生产连接或模型密钥。可以使用随代码提供的临时依赖：

```bash
docker compose -f deploy/test/docker-compose.yml up -d --wait
export IBESS_TEST_DATABASE_URL='postgresql+psycopg://ibess_test:local-test-only@127.0.0.1:55432/ibess_test'
export IBESS_TEST_REDIS_URL='redis://127.0.0.1:56379/15'
make check
docker compose -f deploy/test/docker-compose.yml down
```

测试数据库使用随机 schema，结束后删除；阶段控制测试使用独立 Redis 流。由于历史测试复用应用模块和生命周期，统一检查器为每个服务和每个 API 测试类创建独立 Python 进程，避免全局缓存及模型注册相互污染。每个测试类只运行其直接定义的用例，基类用例在基类自己的检查任务中执行，避免子类预置状态与父类测试冲突。音频队列测试仅为建表 fixture 引入 API 模型。结果写入已忽略的 `tmp/reports/quality.json`；可用 `--report` 指定其他报告路径。

CPU 检查不会伪造 GPU 或现场验收结果。真实说话人模型测试须显式启用 `IBESS_REAL_AUDIO_TESTS=1`，并在推理环境准备测试代码声明的授权音频；未启用时会列为跳过。真实视觉接口调用单独运行 `scripts/verify_visual_model.py`；它发送合成空室画面，不能作为交头接耳、资料递接的现场准确率结果。

Python 直接依赖保留在服务的 `requirements.txt`；公共约束在 `requirements/constraints.txt`；部署安装对应 `requirements.lock`。GPU 基线也使用独立锁文件。升级依赖时使用固定版本 `uv==0.9.28` 执行 `pip compile --universal --python-version 3.11`，同时更新受影响服务和开发锁文件，在新虚拟环境跑完整检查。不要手工编辑锁文件中的解析结果。

所有 Python 应用镜像统一从仓库根构建，例如：

```bash
docker build -f deploy/inference-base/Dockerfile -t poc-inference-base:latest .
docker compose build
```

既有启动脚本继续兼容，标准启动入口为 `make up`。代码分发继续使用 `scripts/package_source.py`，不包含 `.env`、模型、原始媒体、报告和虚拟环境。

GitHub Actions 的 `quality.yml` 使用同一检查入口、专用 PostgreSQL/Redis 和固定依赖，另外构建四类 CPU 镜像。GPU 镜像及真实机位性能仍需在具备对应硬件和素材的环境验证。
