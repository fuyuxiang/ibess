> 当前版本统一采用仓库根构建上下文、共享包和 API 版本迁移。旧的独立建表流程已委托或改为检查；请先阅读 [开发与交付](../../docs/development.md) 和 [数据库迁移](../../docs/architecture/database.md)。下文保留可选部署组件的操作说明。

# 负责人 A 的部署说明（执行卡 10.2；5.01 更新）

5.01 起 A 的能力已经接进 B 的服务代码（vision-worker、event-service、api 各加一处调用，前端加标签）。**B 的启动方式照旧**，
本目录只放可选项与部署机上的辅助脚本。B 的 docker-compose.yml、start-demo.sh、start_poc.ps1 不改。

## 文件

| 文件 | 作用 |
|---|---|
| `recreate_vision_workers.sh` | 让 poc-vw-cam1～9 用上新代码：读现有容器配置，只换镜像后重建；默认只打印；`--apply` 时先把旧容器改名备份，新的起来才删旧的，失败自动恢复 |
| `docker-compose.a.yml` | 可选叠加：api 只读挂素材根（4.1）；`stage-media-controller`（4.2，profile `media`）；`offline-worker`（9.4 视觉离线质检，profile `offline`） |
| `start_a.ps1` | 在 B 启动之后按需执行上面三项（PowerShell 7） |
| `a.env.example` | A 的开关与环境项（追加到 `.env`） |
| `media-controller.Dockerfile` | 阶段媒体控制器镜像 |
| `api-a.Dockerfile` | 可选：B 的 api 镜像 + ffprobe（4.1 探测时长用）。叠加文件默认不用它；需要时手动构建并在 `.env` 外自行指定 |
| `migrate_a_tables.py` | 建 A 的独立表：`stage_assets` / `stage_media_bindings` / `offline_qa_jobs` / `source_zone_configs`。默认 dry-run，加 `--apply` 执行。**`source_zone_configs` 必须靠它建**：`zone_config_models` 没被 `app/models` 导入，api 启动的 create_all 覆盖不到；缺这张表时 A 的区域能力会在启动时警告一次然后静默降级，S03/S05/S08/S09 全都不判 |
| `restore_poc.ps1`、`../../scripts/backup_poc.ps1` | 备份与恢复 |
| `test_integration_deploy.py` | 静态校验（本机可跑） |

## 部署机上的顺序（新代码上线）

1. 先备份：`pwsh -File scripts/backup_poc.ps1`。
2. 拉取 `ly` 分支代码。A 的开关缺省全开；没配 `VISUAL_MODEL_BASE_URL` 时 7.1 自动跳过。
   - A 的开关与视觉模型地址是视觉 worker 的环境变量，B 的 compose 不会把 `.env` 里的这些项传给 poc-vw 容器，要用步骤 4 的 `--set` 带进去。
   - `.env` 只需追加可选服务用的项：`POC_STAGE_ASSETS_DIR`、`MEDIA_*`、`OFFLINE_*`（见 `a.env.example` 后半）。
3. 重建 B 的服务镜像并重启（这些服务由 compose 管理）：
   `docker compose build api event-service web-console` 然后 `docker compose up -d api event-service web-console`。
4. 建 A 的表：必须在步骤 3 之后执行（脚本要 import api 镜像里的 A 模型文件，旧镜像没有这些模块会报 `ModuleNotFoundError`）。
   先看计划：`docker compose -f docker-compose.yml -f deploy/integration/docker-compose.a.yml exec api python /integration/migrate_a_tables.py --target api`
   确认无误再加 `--apply`。若输出里 `source_zone_configs` 是「将新建」，说明这台机器此前一直缺这张表，A 的区域类规则从未生效过。
   叠加文件已把 `deploy/integration` 只读挂到 api 的 `/integration`，不用手动 docker cp。
5. 重建 9 路视觉 worker：先 `bash deploy/integration/recreate_vision_workers.sh --build` 只打印命令，核对镜像、挂载、环境变量无误后，再加 `--apply` 执行。
   - 这些容器是部署机上预先建好的，start-demo.sh 只执行 `docker start`；不重建的话跑的还是旧代码。
   - 脚本只换镜像，启动命令、环境变量、挂载、网络、GPU、shm、重启策略都照抄；有 tmpfs 等特殊挂载或多个网络时会提示、需手动处理。
   - 已关掉 Git Bash 的路径改写（`MSYS_NO_PATHCONV=1`），参数原样传给 docker。
   - 脚本照抄的是容器原有的环境变量，不读 `.env`。要改 A 的开关，在这一步用 `--set A_xxx=0`（可重复）带进去。
6. 按 B 的方式启动：`bash start-demo.sh`。
7. 可选：`pwsh -File deploy/integration/start_a.ps1 -AssetsMount -Media -Offline -Build`。

## 要看到 A 的规则事件，还需要在页面上配两样

- **区域**：在 B 的「区域配置」页签，给对应摄像头画工位区和限制区。没画区时，A 的区域事实和工位级离岗都不出。
- **阶段绑定**：在 B 的阶段控制台，给对应摄像头建场次绑定并保存阶段配置。没有阶段快照的摄像头算「未绑定」，A 的 S03 / S05 / S08 / S09 规则按设计不判，日志里会提示一次。画面质量检测和 `inside` 字段不受这一条影响。

## 回退

- 只关 A 的扩展：给视觉 worker 加 `A_EXT_ENABLED=0` 重建（脚本步骤 4），行为回到 B 原样；也可以只关某一项（见 `a.env.example`）。
- 代码回退：回退 5.01 对 B 五个文件的改动（清单见变更包验证.md），再删掉 A 新增的文件。
- 停 A 的可选服务：`docker compose -f docker-compose.yml -f deploy/integration/docker-compose.a.yml --profile media --profile offline stop stage-media-controller offline-worker`。
- 数据库回到备份：`pwsh -File deploy/integration/restore_poc.ps1 -BackupDir <备份目录>`，默认恢复到新库，现库不动。

## 限制

- 本机没有 Docker，`docker compose config`、镜像构建、真实启动都只能在部署机上做。
- 阶段媒体控制器要回 ACK，须在 B 的 `config/phase-consumers.json` 登记 media-controller 消费者（部署人员操作）。
- 视觉模型选型未定，见 `deploy/visual-model/README.md`。
