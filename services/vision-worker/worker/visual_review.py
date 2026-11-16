"""执行卡 7.1：候选物品的多帧多模态复核 → 契约 Observation `visual_fact`。

请求 = 全景 + 候选 ROI 放大 + 前后多帧（≤ 3 帧）；响应必须是有限枚举并引用请求里的帧 / 区域；
非法响应允许一次结构化纠错，仍非法记 failed（quality_flags 含 model_failed），绝不回退 normal。
模型未配置、超时、上游错误同样是 failed 事实。看不清（unclear / too_small）是可见性事实（visibility_unclear），
由 7.2 / 7.3 的规则转成 insufficient。payload 只有可观察事实，没有结论。
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import numpy as np

from worker.object_candidates import Candidate
from worker.stage_health import SCOPE_FIELDS, FrameContext
from worker.visual_client import OpenAICompatibleVisionClient, Prompt, VisualModelError, crop_roi, encode_jpeg, image_part, load_prompt

CONTRACT_VERSION = "2.0.0"
OBSERVATION_TYPE = "visual_fact"
MAX_FRAMES = 3
ENUMS = {
    "object_kind": ("phone", "paper", "screen", "watch", "cup", "other", "none"),
    "visibility": ("clear", "partial", "unclear"),
    "holding": ("in_hand", "on_table", "at_ear", "none", "unknown"),
    "activity": ("reading", "writing", "operating_device", "photographing", "phone_call", "none", "unknown"),
}
REGIONS = ("panorama", "roi")
FACT_KEYS = ("object_kind", "visibility", "holding", "activity", "evidence", "notes")
NOTES_MAX = 200
DEFAULT_PROMPT_DIR = Path(__file__).resolve().parents[1] / "prompts"


class FactValidationError(ValueError):
    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(message)


@dataclass(frozen=True)
class ReviewFrame:
    frame_index: int
    start_ms: int
    panorama_jpeg: bytes
    roi_jpeg: bytes


@dataclass(frozen=True)
class ReviewRequest:
    request_id: str
    candidate: Candidate
    frames: tuple
    scene_hint: Optional[str]
    prompt: Prompt
    model: str

    @property
    def frame_indexes(self) -> tuple:
        return tuple(f.frame_index for f in self.frames)


def build_review_request(frames: list[tuple[int, np.ndarray]], candidate: Candidate, *, prompt: Prompt, model: str,
                         scene_hint: Optional[str] = None, max_frames: int = MAX_FRAMES, max_side: int = 1024) -> ReviewRequest:
    """frames：[(start_ms, bgr_frame)]，按时间排序；当前帧放中间，最多 max_frames 帧。"""
    if not frames:
        raise ValueError("至少一帧")
    if max_frames < 1:
        raise ValueError("max_frames 至少 1")
    ordered = sorted(frames, key=lambda f: f[0])
    current = min(range(len(ordered)), key=lambda i: abs(ordered[i][0] - candidate.start_ms))
    half = (max_frames - 1) // 2
    lo = max(0, current - half)
    hi = min(len(ordered), lo + max_frames)
    lo = max(0, hi - max_frames)
    picked = ordered[lo:hi]
    review_frames = []
    for index, (start_ms, frame) in enumerate(picked):
        review_frames.append(ReviewFrame(frame_index=index, start_ms=int(start_ms), panorama_jpeg=encode_jpeg(frame, max_side=max_side),
                                         roi_jpeg=encode_jpeg(crop_roi(frame, candidate.bbox), max_side=max_side)))
    digest = hashlib.sha256(f"{candidate.subject}|{candidate.start_ms}|{[f.start_ms for f in review_frames]}|{prompt.label}|{model}".encode("utf-8")).hexdigest()[:24]
    return ReviewRequest(request_id=f"review:{digest}", candidate=candidate, frames=tuple(review_frames), scene_hint=scene_hint,
                         prompt=prompt, model=model)


def build_messages(request: ReviewRequest, *, correction: Optional[str] = None) -> list[dict]:
    """system = 提示词；user = 文本（候选种类、帧与区域清单）+ 每帧 panorama、roi 两张图；纠错时追加一条。"""
    lines = [f"候选物品种类（检测器给出，可能误检）：{request.candidate.kind}。",
             f"帧序号：{list(request.frame_indexes)}；每帧两张图依次是 panorama、roi。"]
    if request.scene_hint:
        lines.append(f"关注场景：{request.scene_hint}（只影响你描述的侧重，不要据此下结论）。")
    content: list[dict] = [{"type": "text", "text": "\n".join(lines)}]
    for frame in request.frames:
        content.append({"type": "text", "text": f"frame_index={frame.frame_index} panorama:"})
        content.append(image_part(frame.panorama_jpeg))
        content.append({"type": "text", "text": f"frame_index={frame.frame_index} roi:"})
        content.append(image_part(frame.roi_jpeg))
    messages = [{"role": "system", "content": request.prompt.text}, {"role": "user", "content": content}]
    if correction:
        messages.append({"role": "user", "content": [{"type": "text", "text": correction}]})
    return messages


def validate_facts(request: ReviewRequest, text: str) -> dict:
    """解析并校验模型回答；任何不合法抛 FactValidationError(code)。"""
    if not isinstance(text, str) or not text.strip():
        raise FactValidationError("EMPTY_RESPONSE", "模型没有返回内容")
    body = text.strip()
    if body.startswith("```"):
        body = re.sub(r"^```(?:json)?\s*|\s*```$", "", body, flags=re.S)
    try:
        data = json.loads(body)
    except ValueError:
        raise FactValidationError("INVALID_JSON", "回答不是合法 JSON") from None
    if not isinstance(data, dict):
        raise FactValidationError("INVALID_JSON", "回答不是 JSON 对象")
    extra = set(data) - set(FACT_KEYS)
    missing = [k for k in FACT_KEYS if k not in data]
    if extra or missing:
        raise FactValidationError("SCHEMA_MISMATCH", f"缺字段 {missing} / 多字段 {sorted(extra)}")
    for key, allowed in ENUMS.items():
        if data[key] not in allowed:
            raise FactValidationError("ENUM_VIOLATION", f"{key}={data[key]!r} 不在枚举内")
    if data["visibility"] == "unclear" and (data["holding"] != "unknown" or data["activity"] != "unknown"):
        raise FactValidationError("ENUM_VIOLATION", "visibility=unclear 时 holding / activity 必须 unknown")
    evidence = data["evidence"]
    if not isinstance(evidence, list) or any(not isinstance(e, dict) for e in evidence):
        raise FactValidationError("SCHEMA_MISMATCH", "evidence 必须是对象列表")
    seen = set()
    for item in evidence:
        if set(item) != {"frame_index", "region"}:
            raise FactValidationError("SCHEMA_MISMATCH", "evidence 项只能有 frame_index 与 region")
        if not isinstance(item["frame_index"], int) or isinstance(item["frame_index"], bool) or item["frame_index"] not in request.frame_indexes:
            raise FactValidationError("INVALID_REFERENCE", f"引用了请求里没有的帧 {item['frame_index']!r}")
        if item["region"] not in REGIONS:
            raise FactValidationError("INVALID_REFERENCE", f"引用了请求里没有的区域 {item['region']!r}")
        key = (item["frame_index"], item["region"])
        if key in seen:
            raise FactValidationError("SCHEMA_MISMATCH", "evidence 重复")
        seen.add(key)
    if data["visibility"] != "unclear" and not evidence:
        raise FactValidationError("SCHEMA_MISMATCH", "可见时必须至少一项 evidence")
    notes = data["notes"]
    if not isinstance(notes, str) or len(notes) > NOTES_MAX:
        raise FactValidationError("SCHEMA_MISMATCH", "notes 必须是不超过 200 字的字符串")
    return {k: data[k] for k in FACT_KEYS}


CORRECTION_TEXT = ("上一次回答不符合要求（{reason}）。请只输出一个 JSON 对象，键顺序 object_kind、visibility、holding、activity、evidence、notes，"
                   "每个值只能取说明里给出的枚举，evidence 只能引用帧序号 {frames} 与区域 panorama / roi。")


@dataclass
class VisualReviewer:
    """review(request, ctx) -> Observation。client 为 None 表示模型未配置。"""
    client: Optional[OpenAICompatibleVisionClient]
    model_name: str = "visual-model"
    max_corrections: int = 1
    model_versions: dict = field(default_factory=dict)

    def review(self, request: ReviewRequest, ctx: FrameContext) -> dict:
        if self.client is None:
            return self._failed(request, ctx, "MODEL_NOT_CONFIGURED")
        correction: Optional[str] = None
        last_code = "UNKNOWN"
        for attempt in range(self.max_corrections + 1):
            try:
                text = self.client.complete(build_messages(request, correction=correction))
            except VisualModelError as exc:
                return self._failed(request, ctx, exc.code)
            try:
                facts = validate_facts(request, text)
            except FactValidationError as exc:
                last_code = exc.code
                correction = CORRECTION_TEXT.format(reason=str(exc), frames=list(request.frame_indexes))
                continue
            return self._observation(request, ctx, facts)
        return self._failed(request, ctx, last_code)

    # ---- 内部 ----
    def _model_version(self, request: ReviewRequest) -> str:
        return f"{request.model}@{request.prompt.label}"

    def _base(self, request: ReviewRequest, ctx: FrameContext, payload: dict, flags: list[str]) -> dict:
        bound = ctx.bound
        quality = list(dict.fromkeys(flags))
        if not bound and "binding_missing" not in quality:
            quality.append("binding_missing")
        frames = [f.start_ms for f in request.frames]
        start_ms, end_ms = min(frames), max(frames)
        observation_id = f"{ctx.source_id}:{ctx.media_run_id}:{OBSERVATION_TYPE}:{request.request_id}"
        scope = {k: getattr(ctx, k) for k in SCOPE_FIELDS}
        evidence = {"contract_version": CONTRACT_VERSION, **scope, "source_id": ctx.source_id, "evidence_id": f"{observation_id}:keyframe",
                    "media_run_id": ctx.media_run_id, "asset_id": ctx.asset_id, "evidence_type": "keyframe",
                    "start_ms": request.candidate.start_ms, "end_ms": request.candidate.start_ms, "sha256": None, "status": "pending"}
        return {
            "contract_version": CONTRACT_VERSION, **scope, "source_id": ctx.source_id,
            "stage_snapshot_id": ctx.stage_snapshot_id if bound else None, "phase_id": ctx.phase_id if bound else None,
            "phase_version": ctx.phase_version if bound else None, "observation_id": observation_id,
            "binding_status": "bound" if bound else "unbound", "occurred_at": ctx.occurred_at, "media_run_id": ctx.media_run_id,
            "asset_id": ctx.asset_id, "start_ms": start_ms, "end_ms": end_ms, "type": OBSERVATION_TYPE,
            "payload": {**payload, "candidate": request.candidate.to_dict(), "request_id": request.request_id,
                        "frames": [{"frame_index": f.frame_index, "start_ms": f.start_ms} for f in request.frames],
                        "prompt_version": request.prompt.label, "scene_hint": request.scene_hint},
            "model_name": self.model_name, "model_version": self._model_version(request), "quality_flags": quality, "evidence_refs": [evidence],
        }

    def _observation(self, request: ReviewRequest, ctx: FrameContext, facts: dict) -> dict:
        flags = []
        if facts["visibility"] == "unclear" or request.candidate.too_small:
            flags.append("visibility_unclear")
        if facts["visibility"] == "partial":
            flags.append("visibility_partial")
        if request.candidate.too_small:
            flags.append("candidate_too_small")
            # 候选太小时模型说的持握 / 动作不可信：事实里不保留肯定的持握与动作，只留物品种类与看不清
            facts = {**facts, "visibility": "unclear", "holding": "unknown", "activity": "unknown"}
        return self._base(request, ctx, {"status": "ok", "facts": facts}, flags)

    def _failed(self, request: ReviewRequest, ctx: FrameContext, code: str) -> dict:
        return self._base(request, ctx, {"status": "failed", "error_code": code, "facts": None}, ["model_failed"])


def reviewer_from_env(env: Optional[dict] = None, *, prompt_dir: Optional[Path] = None) -> tuple[VisualReviewer, Prompt]:
    """入口用：读环境项组装；模型未配置时 client=None（受限状态）。"""
    from worker.visual_client import VisualModelConfig
    import os
    env = os.environ if env is None else env
    prompt = load_prompt(Path(prompt_dir or env.get("VISUAL_PROMPT_DIR") or DEFAULT_PROMPT_DIR), env.get("VISUAL_PROMPT_NAME", "visual-review"))
    config = VisualModelConfig.from_env(env)
    client = OpenAICompatibleVisionClient(config) if config else None
    return VisualReviewer(client=client, model_name=config.model if config else "visual-model"), prompt
