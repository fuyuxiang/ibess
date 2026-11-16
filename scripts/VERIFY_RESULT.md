# GPU 推理基线验证结果

验证日期：2026-09-20
环境：Windows 11 + Docker Desktop 4.69 + WSL2
GPU：NVIDIA GeForce RTX 5060 Laptop 8GB（驱动 573.24 / CUDA 12.8）
镜像：`poc-inference-base:latest`（基于 pytorch/pytorch:2.8.0-cuda12.8-cudnn9-runtime）

## 验证结论：三项全部通过 ✅

### 1. torch GPU ✅
| 项目 | 结果 |
| --- | --- |
| PyTorch 版本 | 2.8.0+cu128 |
| CUDA 可用 | True |
| GPU 名称 | NVIDIA GeForce RTX 5060 Laptop GPU |
| CUDA 版本 | 12.8 |
| 显存 | 8.0 GB |
| GPU 张量计算 | OK |

### 2. YOLO 推理 ✅
| 项目 | 结果 |
| --- | --- |
| 模型 | yolov8n.pt |
| 测试图 | dev_assets/bus.jpg |
| 推理耗时 | 1.97 s |
| 检测目标 | 6 个（bus 0.87、person 0.87/0.85/0.83/0.26、stop sign 0.26） |

### 3. FunASR 转写 ✅
| 项目 | 结果 |
| --- | --- |
| 模型 | paraformer-zh + fsmn-vad + ct-punc |
| 测试音频 | dev_assets/asr_zh.wav |
| 转写耗时 | 0.9 s |
| 转写文本 | 欢迎大家来体验达摩院推出的语音识别模型。 |
| RTF | 0.147（实时率，满足流式要求） |

## 关键环境决策（已验证）

- **cu128 基线正确**：PyTorch 2.8.0+cu128 同时覆盖 RTX 5060（sm_120）与 RTX 4090（sm_86）
- **pip 未破坏 CUDA torch**：清华源安装 ultralytics/funasr 后 torch 仍为 2.8.0+cu128
- **模型已离线化**：FunASR 三模型（2.1GB）缓存于 `POC/models_cache`，YOLO 权重在 `dev_assets/yolov8n.pt`

## 模型权重清单（内网部署需分发）

| 模型 | 来源 | 大小 |
| --- | --- | --- |
| yolov8n.pt | Ultralytics | 6.5 MB |
| paraformer-zh（ASR） | ModelScope iic | ~1 GB |
| fsmn-vad（语音检测） | ModelScope iic | ~80 MB |
| ct-punc（标点） | ModelScope iic | ~1 GB |

分发/复用：见 `scripts/fetch_models.sh`

## 下一步

环境基线已打通，具备开始最小垂直切片的条件：1 路视频接入 → YOLO 检测 → 事件 → 告警 → 前端展示。
