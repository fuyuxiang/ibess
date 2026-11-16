"""Validate untrusted model output and derive evidence time ranges."""

from copy import deepcopy


class SemanticResultError(ValueError):
    def __init__(self, code, message):
        super().__init__(f"{code}: {message}")
        self.code = code


def _fail(code, message):
    raise SemanticResultError(code, message)


def _insufficient(request, missing):
    return {
        "scene_id": request["scene_id"],
        "conclusion": "insufficient",
        "risk_level": "none",
        "reason": "业务上下文或有效证据不足，暂不形成风险建议。",
        "evidence": [],
        "missing_facts": list(dict.fromkeys(missing)),
        "recommendation": "collect_evidence",
    }


def validate_result(request, result):
    if not isinstance(result, dict):
        _fail("RESULT_FORMAT_INVALID", "模型结果必须是对象")
    required = {"scene_id", "conclusion", "risk_level", "reason", "evidence", "missing_facts", "recommendation"}
    if set(result) != required:
        _fail("RESULT_FORMAT_INVALID", "模型结果字段不完整或包含额外字段")
    if result["scene_id"] != request["scene_id"]:
        _fail("SEMANTIC_SCENE_MISMATCH", "模型场景与请求不一致")
    if result["conclusion"] not in {"normal", "suspected", "insufficient"}:
        _fail("RESULT_FORMAT_INVALID", "结论不受支持")
    if result["risk_level"] not in {"none", "low", "medium", "high"}:
        _fail("RESULT_FORMAT_INVALID", "风险级别不受支持")
    if result["recommendation"] not in {"no_action", "human_review", "collect_evidence"}:
        _fail("RESULT_FORMAT_INVALID", "处置建议不受支持")
    if not isinstance(result["reason"], str) or not result["reason"].strip():
        _fail("RESULT_FORMAT_INVALID", "原因不能为空")
    if not isinstance(result["evidence"], list) or not isinstance(result["missing_facts"], list):
        _fail("RESULT_FORMAT_INVALID", "证据和缺口必须是列表")
    if result["conclusion"] == "normal" and (result["risk_level"] != "none" or result["recommendation"] != "no_action"):
        _fail("RESULT_STATE_INVALID", "normal必须对应none和no_action")
    if result["conclusion"] == "suspected" and (result["risk_level"] == "none" or result["recommendation"] != "human_review"):
        _fail("RESULT_STATE_INVALID", "suspected必须进入人工复核")
    if result["conclusion"] == "insufficient" and (result["risk_level"] != "none" or result["recommendation"] != "collect_evidence"):
        _fail("RESULT_STATE_INVALID", "insufficient必须进入补证")
    if request.get("binding_status") == "unbound" and result["conclusion"] != "insufficient":
        _fail("UNBOUND_RISK", "未绑定输入只能返回证据不足")

    segments = {segment["segment_id"]: segment for segment in request["segments"]}
    evidence = []
    for item in result["evidence"]:
        if not isinstance(item, dict) or set(item) != {"segment_id", "quote"}:
            _fail("RESULT_FORMAT_INVALID", "证据字段不正确")
        segment = segments.get(item["segment_id"])
        if segment is None:
            _fail("SEGMENT_MISSING", "证据引用的句段不在窗口内")
        if not isinstance(item["quote"], str) or not item["quote"].strip() or item["quote"] not in segment["text"]:
            _fail("QUOTE_MISMATCH", "原话引用不匹配原始转写")
        evidence.append({
            "segment_id": item["segment_id"],
            "quote": item["quote"],
            "start_ms": segment["start_ms"],
            "end_ms": segment["end_ms"],
        })
    if result["conclusion"] == "suspected" and not evidence:
        _fail("EVIDENCE_REQUIRED", "疑似结果必须包含有效证据")
    if request["scene_id"] == "S07" and result["conclusion"] == "suspected":
        context = request["context"]
        if not (context.get("recipient_authorized") is False
                and context.get("recipient_fact_id")
                and context.get("confidentiality_policy_id")
                and context.get("information_visibility") == "confidential"):
            _fail("SEMANTIC_CONTEXT_MISSING", "S07疑似结果缺少接收对象或保密依据")

    normalized = deepcopy(result)
    normalized["evidence"] = evidence
    return normalized
