"""声学事件分析（7.4 S09 最小路径）：RMS 能量包络突发检测。

对录音原始 wav 计算帧级 RMS，基线取中位数（安静/正常说话主导），
显著高于基线且持续够长的片段判为"高能量突发"（喊叫/争吵特征）。
纯 numpy + wave，无额外依赖；不替代人工听审，与语义研判互补。
"""
import wave
from pathlib import Path

import numpy as np

FRAME_MS = 100
BURST_RATIO = 2.5      # 帧RMS超过基线的倍数（喊叫能量特征）
MIN_BURST_MS = 600     # 突发最短持续
MERGE_GAP_MS = 400     # 相邻突发合并间距


def _read_mono(path: Path) -> tuple[np.ndarray, int]:
    with wave.open(str(path), "rb") as handle:
        channels = handle.getnchannels()
        width = handle.getsampwidth()
        rate = handle.getframerate()
        frames = handle.readframes(handle.getnframes())
    if width == 2:
        data = np.frombuffer(frames, dtype=np.int16).astype(np.float32) / 32768.0
    elif width == 1:
        data = (np.frombuffer(frames, dtype=np.uint8).astype(np.float32) - 128.0) / 128.0
    else:
        raise ValueError(f"不支持的采样宽度: {width}")
    if channels > 1:
        data = data.reshape(-1, channels).mean(axis=1)
    return data, rate


def analyze_wav(path: Path) -> dict:
    """返回帧RMS包络摘要 + 突发段列表。"""
    data, rate = _read_mono(path)
    if data.size == 0:
        raise ValueError("音频为空")
    frame = max(1, int(rate * FRAME_MS / 1000))
    pad = (-data.size) % frame
    if pad:
        data = np.pad(data, (0, pad))
    frames = data.reshape(-1, frame)
    rms = np.sqrt(np.mean(frames.astype(np.float64) ** 2, axis=1))
    baseline = float(np.median(rms)) or 1e-6
    duration_ms = int(data.size / rate * 1000)
    hot = rms > baseline * BURST_RATIO

    bursts = []
    index = 0
    while index < len(hot):
        if not hot[index]:
            index += 1
            continue
        start = index
        while index < len(hot) and hot[index]:
            index += 1
        burst = {"start_ms": start * FRAME_MS, "end_ms": index * FRAME_MS,
                 "peak_rms": float(rms[start:index].max()),
                 "ratio": float(rms[start:index].max() / baseline)}
        if bursts and burst["start_ms"] - bursts[-1]["end_ms"] <= MERGE_GAP_MS:
            bursts[-1]["end_ms"] = burst["end_ms"]
            bursts[-1]["peak_rms"] = max(bursts[-1]["peak_rms"], burst["peak_rms"])
            bursts[-1]["ratio"] = max(bursts[-1]["ratio"], burst["ratio"])
        else:
            bursts.append(burst)
    bursts = [item for item in bursts if item["end_ms"] - item["start_ms"] >= MIN_BURST_MS]
    peak_index = int(np.argmax(rms)) if rms.size else 0
    # 包络曲线（降采样到约100点）供前端可视化：每秒聚合一次RMS
    step = max(1, int(len(rms) / 120))
    envelope = [{"t": round(index * FRAME_MS / 1000, 1),
                 "rms": round(float(rms[index:index + step].max() / baseline), 2)}
                for index in range(0, len(rms), step)]
    return {
        "duration_ms": duration_ms,
        "frame_ms": FRAME_MS,
        "baseline_rms": round(baseline, 6),
        "envelope": envelope,
        "bursts": [{"start_ms": item["start_ms"], "end_ms": item["end_ms"],
                    "duration_ms": item["end_ms"] - item["start_ms"],
                    "peak_rms": round(item["peak_rms"], 6), "ratio": round(item["ratio"], 2)} for item in bursts],
        "summary": {
            "burst_count": len(bursts),
            "loudest_at_ms": peak_index * FRAME_MS,
            "conclusion": "high_energy_burst" if len(bursts) >= 1 else "quiet",
        },
    }
