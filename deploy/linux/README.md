> 当前版本统一采用仓库根构建上下文、共享包和 API 版本迁移。旧的独立建表流程已委托或改为检查；请先阅读 [开发与交付](../../docs/development.md) 和 [数据库迁移](../../docs/architecture/database.md)。下文保留可选部署组件的操作说明。

`0006_preparation` 使现场机位与区域可按版本复用，既有场次保持原配置。场次在业务“场次管理”中创建并准备；消费者接入改用 `python3 scripts/configure_phase_consumer.py --session-id <场次UUID>`，详见[场次流程](../../docs/session-workflow.md#部署人员接入阶段消费者)。

当前数据库目标为 `0006_preparation`。更新时同步重建 API、前端与消费者镜像，先执行 API 迁移，再启动消费者。新场次的人员资料默认选填，人脸身份核查由管理员在开场前按需启用；存量已开始场次保留原模式及全部身份、授权历史。

# Linux 服务器部署手册

目标机：Ubuntu 22.04 LTS，Docker 28.0.4 + Compose v2.34.0，root 权限。
本文按"带演示数据迁移"的场景写；只要空库的话跳过第 4 步。

---

## 0 先决条件

| 项 | 要求 | 检查命令 |
| --- | --- | --- |
| 磁盘 | 可用 **≥ 60 GB**（镜像 ~33 G + 数据 ~12 G） | `df -h /` |
| 内存 | ≥ 16 GB（检测+语音+数据库同机） | `free -g` |
| 端口 | 3000 / 8000 / 5432 / 6379 / 9000 / 8554 / 8888 空闲 | `ss -ltnp \| grep -E ':(3000\|8000)'` |
| 内核 | 已重启过（`System restart required` 提示存在时先重启） | `uptime` |

> 服务器上如果看到 `*** System restart required ***`，**先重启**再装/跑容器，
> 否则 overlayfs、iptables 的模块可能没加载，容器起不来。

---

## 1 准备代码

```bash
mkdir -p /opt/ibess && cd /opt/ibess
tar -xzf /root/poc-transfer/code.tar.gz
```

目录结构应为 `docker-compose.yml`、`services/`、`web-console/`、`deploy/`。

---

## 2 环境变量

首次部署按下面的模板填写口令；复用旧数据卷时使用该环境现有凭据。代码包不包含 `.env`，应通过受控渠道单独配置。

```bash
cp deploy/linux/.env.example .env
vi .env      # 至少改 POSTGRES_PASSWORD、MINIO_ROOT_PASSWORD、PHASE_CONTROL_TOKEN_EVENT_SERVICE
chmod 600 .env
```

新安装推荐直接运行 `python3 scripts/init_env.py` 生成随机凭据及本机消费者摘要文件。已有安装保留原 `.env`；新场次的精确消费者范围接入见 [场次流程说明](../../docs/session-workflow.md#部署人员接入阶段消费者)。

`SEMANTIC_MODEL_API_KEY` 不填也能跑：语义复核会自动降级为规则兜底，其他功能不受影响。

本机发送文件前，在本机 `.env` 设置 `DEPLOY_SSH_HOST`、`DEPLOY_SSH_USER`、`DEPLOY_REMOTE_DIR`，并执行 `source scripts/load-env.sh && load_poc_env .env`。下面的传输命令从这些变量读取目标地址，不在文档中记录现场服务器信息。

---

## 3 镜像

两条路，选一条。

### 路线 A：本机打包，拷过去 load（推荐，不依赖服务器网络）

本机执行：

```bash
bash deploy/linux/save-images.sh          # 产出 deploy/linux/out/poc-images.tar（约 32 G）
scp deploy/linux/out/poc-images.tar ${DEPLOY_SSH_USER}@${DEPLOY_SSH_HOST}:${DEPLOY_REMOTE_DIR}/
```

服务器执行：

```bash
docker load -i /root/poc-transfer/poc-images.tar
docker images | grep poc-
```

### 路线 B：服务器直接构建（需要外网 + 能拉 pytorch 镜像）

```bash
cd /opt/ibess
bash deploy/linux/deploy.sh --build
```

Dockerfile 已默认使用清华 pip 源，可用
`--build-arg PIP_INDEX_URL=https://pypi.org/simple` 覆盖。

> 首次构建 20–40 分钟；`poc-vision-worker` 与 `poc-audio-worker` 各自约 14.6 G，
> 因为基础镜像是 CUDA 运行时。服务器没有 GPU 也能跑（CPU 推理），只是慢一些。

---

## 4 迁移演示数据（可选）

本机执行（默认导出近 3 天证据，约 12 G）：

```bash
bash deploy/linux/export-demo-data.sh
# 想多带几天：DAYS=7 bash deploy/linux/export-demo-data.sh
scp -r deploy/linux/out/* ${DEPLOY_SSH_USER}@${DEPLOY_SSH_HOST}:${DEPLOY_REMOTE_DIR}/
```

服务器执行：

```bash
cd /opt/ibess
bash deploy/linux/import-on-linux.sh /root/poc-transfer
```

脚本会：恢复数据库 → 导入对象存储 → 解包语音模型 → 启动全部服务。

> 只带数据库、不带证据也可以（`evidence/` 目录留空），
> 只是历史事件详情里的缩略图会裂，新产生的事件不受影响。

---

## 5 启动与验收

```bash
bash deploy/linux/bootstrap.sh      # 前置检查
bash deploy/linux/deploy.sh         # 启动
```

验收清单：

```bash
curl -s localhost:8000/health/ready        # 期望 200
docker compose -f docker-compose.yml -f deploy/linux/docker-compose.linux.yml ps
```

浏览器打开 `http://<服务器IP>:3000`：

1. 能用原账号登录（迁移数据的情况下），或用初始化流程建管理员
2. 监管工作台有数字、事件中心有列表
3. 现场监测能出画面（需要推流；没有真实摄像头时用 `--with-demo-cams` 起演示源）
4. 评标阶段看板能切阶段

---

## 6 常用运维

```bash
# 查看状态
docker compose -f docker-compose.yml -f deploy/linux/docker-compose.linux.yml ps

# 看某个服务日志
docker logs poc-api --tail 100 -f

# 停掉（保留数据）
docker compose -f docker-compose.yml -f deploy/linux/docker-compose.linux.yml down

# 停掉并删除数据卷（危险）
docker compose -f docker-compose.yml -f deploy/linux/docker-compose.linux.yml down -v

# 连演示摄像头一起起（本机演示用）
bash deploy/linux/deploy.sh --with-demo-cams
```

---

## 7 常见问题

**端口被占用**
`bootstrap.sh` 会逐条检查。Ubuntu 上 8888 常被 jupyter 占，5432 常被本机 postgres 占。

**容器起不来，日志显示 permission denied**
SELinux/AppArmor 环境给挂载目录加标签：`chcon -Rt svirt_sandbox_file_t /opt/ibess`

**服务器访问不了外网却想构建**
走路线 A。或者在国内先 `docker save` 基础镜像 `pytorch/pytorch:2.8.0-cuda12.8-cudnn9-runtime` 一起带过去。

**时间不对**
覆盖文件已统一 `TZ=Asia/Shanghai`；数据库里的时间存的是 UTC，页面按东八区显示，属正常。

**内存不够，worker 被杀**
`docker stats` 看到 vision-worker 吃满。把检测抽帧降到每秒 2 帧：
`.env` 里 `VIDEO_FPS=2`，或只启用必要机位。

---

## 附：数据体积参考（本机实测）

| 内容 | 体积 |
| --- | --- |
| 数据库 | 135 MB |
| 对象存储全量 | 47.0 GB（256,439 个对象） |
| 对象存储近 3 天 | 9.5 GB |
| 对象存储近 2 天 | 1.4 GB |
| 语音模型缓存 | 2.1 GB |
| 代码 + 素材 | 约 60 MB |
| 业务镜像合计 | 约 32.5 GB |
