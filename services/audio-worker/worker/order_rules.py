"""执行卡 7.4：S09 音频侧候选产生器 → 契约 Observation `audio_order_candidate`。

两类候选，都只是「可回锚的事实」，不是结论：
- threat_phrase：只读 0.2 的转写句段，命中词表时引用逐字原话（quote 必须是句段 text 的连续子串，代码核对）。
- loud_burst：短时 RMS 转 dBFS，相对滚动中位基线突升 ≥ rise_db 且持续 ≥ burst_min_ms；持续偏高（基线跟上）与短尖峰都不算。
说话人只保留 A/B 标签，不映射人员。声音大不是冲突：候选单独不生成任何线索（规则在 vision-worker 的 conflict_rules）。
"""
from __future__ import annotations

import math
import statistics
from collections import deque
from dataclasses import dataclass, field
from typing import Iterable, Optional

from worker.audio_health import SCOPE_FIELDS, SNAPSHOT_FIELDS, AudioChunkContext, rms_of

CONTRACT_VERSION = "2.0.0"
OBSERVATION_TYPE = "audio_order_candidate"
MODEL_NAME = "order_rules"
MODEL_VERSION = "7.4-v1"
# 工程初值，业务方定。「你等着」「小心点」在正常对话里常见（「你等着我查一下」「小心点台阶」），子串命中误召太多，不放默认表
DEFAULT_LEXICON = ("打你", "揍你", "揍他", "滚出去", "废物", "弄死", "打死", "威胁你")


@dataclass
class OrderConfig:
    lexicon: tuple = DEFAULT_LEXICON
    rise_db: float = 12.0            # 相对基线突升多少 dB 才算
    burst_min_ms: int = 400          # 突升要持续多久
    baseline_window_ms: int = 5000   # 滚动中位基线窗口
    silence_dbfs: float = -60.0      # 低于此值不参与基线
    max_burst_ms: int = 4000         # 超过此时长视为「持续偏高」，不再是突发

    def __post_init__(self) -> None:
        if self.rise_db <= 0 or self.burst_min_ms <= 0 or self.baseline_window_ms <= 0 or self.max_burst_ms <= self.burst_min_ms:
            raise ValueError("声强配置非法")
        if not self.lexicon or any(not isinstance(t, str) or not t.strip() for t in self.lexicon):
            raise ValueError("词表必须是非空字符串")


def dbfs(rms: float) -> float:
    return 20.0 * math.log10(rms) if rms > 0 else -120.0


class ThreatLexicon:
    def __init__(self, terms: Iterable[str]) -> None:
        self.terms = tuple(dict.fromkeys(t.strip() for t in terms if t and t.strip()))

    def match(self, text: str) -> list[str]:
        """返回命中的词（按出现顺序去重）；每个词都是 text 的连续子串。"""
        if not isinstance(text, str):
            return []
        hits = [t for t in self.terms if t in text]
        hits.sort(key=lambda t: text.index(t))
        return hits


class LoudnessDetector:
    """滚动中位基线；突发期间冻结基线（否则基线会被突发抬高）。"""

    def __init__(self, config: OrderConfig) -> None:
        self.config = config
        self._history: deque = deque()     # (end_ms, dbfs)
        self._burst_since: Optional[int] = None
        self._burst_peak: float = -120.0
        self._burst_baseline: float = -120.0
        self._burst_samples: list = []     # 突发期间的 (end_ms, dbfs)，冻结在外；判为持续偏高时整段并入基线
        self._last_ms: Optional[int] = None

    def reset(self) -> None:
        self._history.clear()
        self._burst_since = None
        self._burst_samples = []
        self._last_ms = None

    def baseline(self) -> Optional[float]:
        values = [v for _t, v in self._history if v > self.config.silence_dbfs]
        return statistics.median(values) if len(values) >= 3 else None

    def feed(self, start_ms: int, end_ms: int, level_dbfs: float) -> Optional[dict]:
        """返回结束的突发 {"start_ms","end_ms","peak_dbfs","baseline_dbfs","rise_db"}，否则 None。"""
        if self._last_ms is not None and start_ms < self._last_ms:
            raise ValueError("媒体时间倒退；新轮次请 reset")
        self._last_ms = end_ms
        base = self.baseline()
        in_burst = base is not None and level_dbfs - base >= self.config.rise_db
        result = None
        if self._burst_since is None:
            if in_burst:
                self._burst_since, self._burst_peak, self._burst_baseline = start_ms, level_dbfs, base
                self._burst_samples = [(end_ms, level_dbfs)]
            else:
                self._push(end_ms, level_dbfs)
        else:
            if in_burst and end_ms - self._burst_since <= self.config.max_burst_ms:
                self._burst_peak = max(self._burst_peak, level_dbfs)
                self._burst_samples.append((end_ms, level_dbfs))
            else:
                duration = start_ms - self._burst_since
                if not in_burst and self.config.burst_min_ms <= duration <= self.config.max_burst_ms:   # 仍然很响 = 持续偏高，不是突发
                    result = {"start_ms": self._burst_since, "end_ms": start_ms, "peak_dbfs": round(self._burst_peak, 1),
                              "baseline_dbfs": round(self._burst_baseline, 1), "rise_db": round(self._burst_peak - self._burst_baseline, 1)}
                # 突发不论怎么结束都把期间样本并入基线：持续偏高（含有停顿的提高音量）几轮后基线就跟上，不再每句都算突发；
                # 一次真突发只占 5 秒窗口的一小段，对中位基线影响很小
                for sample_end, sample_level in self._burst_samples:
                    self._push(sample_end, sample_level)
                self._burst_since, self._burst_samples = None, []
                self._push(end_ms, level_dbfs)
        return result

    def _push(self, end_ms: int, level_dbfs: float) -> None:
        self._history.append((end_ms, level_dbfs))
        while self._history and end_ms - self._history[0][0] > self.config.baseline_window_ms:
            self._history.popleft()


@dataclass
class AudioOrderMonitor:
    """一个音频源一个实例。on_segments 给转写句段；on_chunk 给采样块。"""
    config: OrderConfig = field(default_factory=OrderConfig)

    def __post_init__(self) -> None:
        self.lexicon = ThreatLexicon(self.config.lexicon)
        self.loudness = LoudnessDetector(self.config)
        self._media_run_id: Optional[str] = None
        self._seen_segments: set[str] = set()

    def reset(self, media_run_id: Optional[str] = None) -> None:
        self._media_run_id = media_run_id
        self.loudness.reset()
        self._seen_segments.clear()

    def _check_run(self, ctx: AudioChunkContext) -> None:
        if self._media_run_id is not None and ctx.media_run_id != self._media_run_id:
            self.reset(ctx.media_run_id)
        self._media_run_id = ctx.media_run_id

    def on_segments(self, ctx: AudioChunkContext, segments: list[dict]) -> list[dict]:
        """segments：0.2 的 {segment_id, speaker, start_ms, end_ms, text}（只读）。每个句段只评一次。"""
        self._check_run(ctx)
        out = []
        for segment in segments:
            if not isinstance(segment, dict) or not isinstance(segment.get("segment_id"), str):
                continue
            if segment["segment_id"] in self._seen_segments:
                continue
            self._seen_segments.add(segment["segment_id"])
            text = segment.get("text") or ""
            hits = self.lexicon.match(text)
            if not hits:
                continue
            # 带原句：下游可逐字核对 quotes 是 segment_text 的连续子串，不必回查转写库
            payload = {"kind": "threat_phrase", "segment_ids": [segment["segment_id"]], "speaker": segment.get("speaker"),
                       "quotes": hits, "matched_terms": hits, "segment_text": text, "text_length": len(text)}
            start, end = int(segment.get("start_ms", ctx.start_ms)), int(segment.get("end_ms", ctx.start_ms))
            out.append(self._observation(ctx, f"threat:{segment['segment_id']}", payload, start, max(start, end)))
        return out

    def on_chunk(self, ctx: AudioChunkContext, samples, *, chunk_ms: int) -> list[dict]:
        self._check_run(ctx)
        level = dbfs(rms_of(samples))
        burst = self.loudness.feed(ctx.start_ms, ctx.start_ms + int(chunk_ms), level)
        if burst is None:
            return []
        payload = {"kind": "loud_burst", "segment_ids": [], "speaker": None, "quotes": [], "matched_terms": [],
                   "rms_dbfs": burst["peak_dbfs"], "baseline_dbfs": burst["baseline_dbfs"], "rise_db": burst["rise_db"],
                   "duration_ms": burst["end_ms"] - burst["start_ms"]}
        return [self._observation(ctx, f"burst:{burst['start_ms']}", payload, burst["start_ms"], burst["end_ms"])]

    def _observation(self, ctx: AudioChunkContext, subject: str, payload: dict, start_ms: int, end_ms: int) -> dict:
        bound = all(getattr(ctx, k) is not None for k in SCOPE_FIELDS + SNAPSHOT_FIELDS)
        flags = [] if bound else ["binding_missing"]
        observation_id = f"{ctx.source_id}:{ctx.media_run_id}:{OBSERVATION_TYPE}:{subject}"
        scope = {k: getattr(ctx, k) for k in SCOPE_FIELDS}
        evidence = {"contract_version": CONTRACT_VERSION, **scope, "source_id": ctx.source_id, "evidence_id": f"{observation_id}:audio",
                    "media_run_id": ctx.media_run_id, "asset_id": ctx.asset_id, "evidence_type": "audio", "start_ms": start_ms,
                    "end_ms": end_ms, "sha256": None, "status": "pending"}
        return {
            "contract_version": CONTRACT_VERSION, **scope, "source_id": ctx.source_id,
            "stage_snapshot_id": ctx.stage_snapshot_id if bound else None, "phase_id": ctx.phase_id if bound else None,
            "phase_version": ctx.phase_version if bound else None, "observation_id": observation_id,
            "binding_status": "bound" if bound else "unbound", "occurred_at": ctx.occurred_at, "media_run_id": ctx.media_run_id,
            "asset_id": ctx.asset_id, "start_ms": start_ms, "end_ms": end_ms, "type": OBSERVATION_TYPE, "payload": payload,
            "model_name": MODEL_NAME, "model_version": MODEL_VERSION, "quality_flags": flags, "evidence_refs": [evidence],
        }
