# 视觉复核模型（执行卡 7.1）：配置清单与可运行性检查

状态：**受限**（2026-10-19，T1.4 可运行性检查）。本机没有 NVIDIA GPU、没有多模态模型服务、没有配置；`check_runnability.py` 在本机输出 `restricted`。真实检查在有 GPU 的机器上跑（D-17），由负责人 A 与 B 共同安排 GPU 负载窗口（与 ASR / 文本模型争用显存，风险 K-10）。

## 1. 模型选型（待用户定，本目录不选）

只评估 **OpenAI 兼容接口的多模态模型**；图片发给纯文本模型不会有视觉能力（开发计划 4.4）。候选与许可证需要在选型时逐个核实（下表是待核实的候选名单，不是结论）：

| 候选 | 形态 | 许可证（待核实） | 出域 | 备注 |
|---|---|---|---|---|
| Qwen2.5-VL（7B / 3B） | 本地 vLLM | 需核实（Qwen 系列按版本不同） | 否 | 中文友好；8 GB 显存跑 7B 需量化，与 ASR 并存未验证（K-10） |
| InternVL 系列 | 本地 vLLM | 需核实 | 否 | |
| MiniMax 多模态接口 | 云端 | 服务协议 | **是** | 与 B 的文本链路同一家（K-14 同类出域问题） |
| 其他云端多模态 | 云端 | 服务协议 | **是** | |

选型要定：模型 ID、权重版本 / 摘要、量化方式、上下文长度、vLLM 版本、每张图片 token 开销、单次延迟、与 ASR 并存的显存占用。定稿后填 `model-manifest.json`（按 `model-manifest.example.json`）。

## 2. 服务端配置（环境项，只写键名）

| 键 | 含义 |
|---|---|
| `VISUAL_MODEL_BASE_URL` | OpenAI 兼容服务地址（本地 vLLM 如 `http://visual-model:8001/v1`，只在 Docker 内网开放） |
| `VISUAL_MODEL_NAME` | 模型 ID |
| `VISUAL_MODEL_API_KEY` / `VISUAL_MODEL_API_KEY_FILE` | 凭据（文件优先挂只读；不进仓库、不进日志） |
| `VISUAL_MODEL_TIMEOUT` | 单次超时秒（缺省 60） |
| `VISUAL_MODEL_MAX_RETRIES` | 网络 / 5xx 重试次数（缺省 2） |
| `VISUAL_PROMPT_NAME` / `VISUAL_PROMPT_DIR` | 提示词名与目录（缺省 `visual-review`、`services/vision-worker/prompts`） |
| `CANDIDATE_MODEL` | 候选检测的 YOLO 权重路径（自己的实例，不改共享 detector） |

缺任一必填项时 `VisualModelConfig.from_env` 返回 None，复核器对每个候选记 `failed(MODEL_NOT_CONFIGURED)`，页面应显示「视觉复核未配置」，不显示 normal。

## 3. 本地 vLLM 启动模板

见 `vllm-compose.example.yml`（不接入正式 compose；10.2 按选型定稿后合入）。要点：只映射到 Docker 内网、`--max-model-len` 与并发按显存定、`--limit-mm-per-prompt image=6`（每帧两张图 × 3 帧）。

## 4. 可运行性检查

```powershell
D:/anaconda3/envs/ai_agent/python.exe deploy/visual-model/check_runnability.py            # 本机：restricted
python deploy/visual-model/check_runnability.py --output tmp/reports/visual-model-runnability.json   # 另一台机器
```

检查项：`nvidia-smi` 可用且有 GPU；`VISUAL_MODEL_*` 配置齐；`GET {base_url}/models` 返回含模型 ID；（可选）一次最小多模态请求的延迟。任一项缺失 → `status=restricted` 并列出缺项与补采条件；全部通过 → `status=ok`。脚本不下载模型、不改任何配置。

## 5. 机位与素材条件（验收范围要写明）

单路全景下手机约 28 像素、缩放后约 10 像素，基本检不出（37 张卡报告 7.1 节【调研】）。S01 / S02 / S08 的手机、资料细节需要**近景机位**；没有近景时这些场景的验收结论只能是「看不清 / 证据不足」，不是「正常」。

## 6. 本目录文件

- `README.md`（本文件）
- `model-manifest.example.json`：模型清单模板（ID、来源、许可证、权重摘要、量化、vLLM 版本、采样设置）
- `check_runnability.py`：可运行性检查脚本
- `vllm-compose.example.yml`：本地 vLLM 服务模板
