"""可配置场景算子执行器。算子代码固定发布，参数和启停状态来自配置。"""
from dataclasses import dataclass
from typing import Any, Callable
from service.supervision_integration import normalize_source
import math


@dataclass(frozen=True)
class OperatorConfig:
    code: str
    version: str
    enabled: bool
    input_types: tuple[str, ...]
    output_event_type: str
    risk_level: str
    parameters: dict[str, Any]
    session_id: str | None = None
    camera_id: str | None = None
    zone_id: str | None = None


@dataclass(frozen=True)
class OperatorResult:
    event_type: str
    risk_level: str
    confidence: float | None
    reason: str
    detail: dict[str, Any]


def _scope_matches(config: OperatorConfig, data: dict[str, Any]) -> bool:
    return all(
        expected is None or (normalize_source(data.get(key)) == normalize_source(expected) if key == 'source_id' else data.get(key) == expected)
        for key, expected in (
            ("session_id", config.session_id),
            ("source_id", config.camera_id),
            ("zone_id", config.zone_id),
        )
    )


def _crowd_gathering(config: OperatorConfig, data: dict[str, Any], payload: dict[str, Any]) -> OperatorResult | None:
    if data.get('phase_id') in ('SIGNIN', 'DISCIPLINE', 'CLARIFY', 'REPORT'):
        return None  # scheduled collective activity is not abnormal gathering
    threshold = int(config.parameters.get("count_threshold", 3))
    boxes = [t['bbox'] for t in payload.get('person_tracks', [])] or payload.get('person_boxes', [])
    if len(boxes) < threshold:
        return None  # a count alone cannot prove spatial gathering
    distance = float(config.parameters.get('distance_threshold', .18))
    centers = [((b[0]+b[2])/2, (b[1]+b[3])/2) for b in boxes]
    # Compact groups must fit a common neighbourhood; distributed seats do not form a crowd.
    count = max((sum(math.dist(a, b) <= distance for b in centers) for a in centers), default=0)
    if count < threshold:
        return None
    return OperatorResult(
        event_type=config.output_event_type,
        risk_level=config.risk_level,
        confidence=payload.get("max_conf"),
        reason=f"检测到 {count} 人聚集，达到阈值 {threshold} 人",
        detail={"count": count, "threshold": threshold, 'distance_threshold': distance},
    )


def _seat_absence(config: OperatorConfig, data: dict[str, Any], payload: dict[str, Any]) -> OperatorResult | None:
    duration = float(payload.get("absence_seconds", 0))
    threshold = float(config.parameters.get("absence_seconds", 60))
    if duration < threshold:
        return None
    return OperatorResult(
        event_type=config.output_event_type,
        risk_level=config.risk_level,
        confidence=payload.get("confidence"),
        reason=f"工位离席 {duration:.0f} 秒，超过阈值 {threshold:.0f} 秒",
        detail={"absence_seconds": duration, "threshold": threshold},
    )


def _zone_intrusion(config: OperatorConfig, data: dict[str, Any], payload: dict[str, Any]) -> OperatorResult | None:
    if not payload.get("inside"):
        return None
    if 'person_count' in payload and int(payload['person_count']) < int(config.parameters.get('person_threshold',1)):
        return None
    return OperatorResult(
        event_type=config.output_event_type,
        risk_level=config.risk_level,
        confidence=payload.get("confidence"),
        reason=f"人员进入限制区域 {payload.get('zone_name', data.get('zone_id', '未命名区域'))}",
        detail={"zone_id": data.get("zone_id"), "zone_name": payload.get("zone_name")},
    )


def _phone_call(config: OperatorConfig, data: dict[str, Any], payload: dict[str, Any]) -> OperatorResult | None:
    """兼容旧输入：明确贴耳姿态并通过人机位置校验；生产动作规则使用连续复核。"""
    phones = payload.get("phone_boxes") or []
    persons = payload.get("person_boxes") or []
    postures = payload.get("phone_postures") or []
    if not phones:
        return None
    for index, phone in enumerate(phones):
        posture = postures[index] if index < len(postures) else ""
        # 未确认的姿态保留为手机候选，不能仅按几何位置定性通话。
        if posture != "ear":
            continue
        center_x = (phone[0] + phone[2]) / 2
        center_y = (phone[1] + phone[3]) / 2
        for person in persons:
            height = person[3] - person[1]
            if person[1] <= center_y <= person[1] + height * 0.45 and person[0] <= center_x <= person[2]:
                label = "贴耳" if posture == "ear" else ("胸前" if posture == "chest" else "贴耳/胸前")
                return OperatorResult(
                    event_type=config.output_event_type,
                    risk_level=config.risk_level,
                    confidence=float(payload.get("phone_conf_max") or 0),
                    reason=f"检测到人员手持手机{label}通话姿态" + ("（大模型复核）" if posture else ""),
                    detail={"phone_count": len(phones), "phone_conf": payload.get("phone_conf_max"), "posture": posture},
                )
    return None


def _phone_capture(config: OperatorConfig, data: dict[str, Any], payload: dict[str, Any]) -> OperatorResult | None:
    """兼容旧输入：要求明确对桌面拍摄姿态；生产规则另校验连续帧及拍摄目标。"""
    phones = payload.get("phone_boxes") or []
    persons = payload.get("person_boxes") or []
    postures = payload.get("phone_postures") or []
    if not phones or not persons:
        return None
    for index, phone in enumerate(phones):
        posture = postures[index] if index < len(postures) else ""
        center_y = (phone[1] + phone[3]) / 2
        # 大模型明确说是贴耳/胸前时，不再按"画面下部"误判为桌拍
        if posture in ("ear", "chest"):
            continue
        if posture == "desk":
            return OperatorResult(
                event_type=config.output_event_type,
                risk_level=config.risk_level,
                confidence=float(payload.get("phone_conf_max") or 0),
                reason="检测到手机在文件/桌面区域举持，疑似拍摄受控资料" + ("（大模型复核：对桌面）" if posture == "desk" else ""),
                detail={"phone_count": len(phones), "phone_center_y": round(center_y, 3), "posture": posture},
            )
    return None


def _phone_unstored(config: OperatorConfig, data: dict[str, Any], payload: dict[str, Any]) -> OperatorResult | None:
    """S01 疑似携带通讯设备未寄存：入口机位检出手机且画面有人（寄存流程应无手机入区）。"""
    if not payload.get("phone_boxes") or not payload.get("person_boxes"):
        return None
    return OperatorResult(
        event_type=config.output_event_type,
        risk_level=config.risk_level,
        confidence=float(payload.get("phone_conf_max") or 0),
        reason="入口机位检出人员携带手机（疑似未寄存）",
        detail={"phone_count": len(payload.get("phone_boxes") or []),
                "phone_conf": payload.get("phone_conf_max")},
    )


def _phone_detected(config: OperatorConfig, data: dict[str, Any], payload: dict[str, Any]) -> OperatorResult | None:
    """保留基础设备线索；单帧姿态不能吞掉尚未完成连续复核的手机候选。"""
    phones = payload.get("phone_boxes") or []
    persons = payload.get("person_boxes") or []
    postures = payload.get("phone_postures") or []
    if not phones:
        return None
    label = {"ear": "贴耳", "chest": "胸前", "desk": "对桌面", "hand": "手持"}.get(postures[0] if postures else "", "姿态未判定")
    return OperatorResult(
        event_type=config.output_event_type,
        risk_level=config.risk_level,
        confidence=float(payload.get("phone_conf_max") or 0),
        reason=f"检出人员携带/使用手机（{label}，置信度 {float(payload.get('phone_conf_max') or 0):.2f}）",
        detail={"phone_count": len(phones), "posture": postures[0] if postures else ""},
    )


HANDLERS: dict[str, Callable[[OperatorConfig, dict[str, Any], dict[str, Any]], OperatorResult | None]] = {
    "crowd_gathering": _crowd_gathering,
    "seat_absence": _seat_absence,
    "zone_intrusion": _zone_intrusion,
    "phone_call": _phone_call,
    "phone_capture": _phone_capture,
    "phone_unstored": _phone_unstored,
    "phone_detected": _phone_detected,
}


def evaluate_operator(config: OperatorConfig, data: dict[str, Any], payload: dict[str, Any]) -> OperatorResult | None:
    if not config.enabled or data.get("type") not in config.input_types or not _scope_matches(config, data):
        return None
    handler = HANDLERS.get(config.code)
    return handler(config, data, payload) if handler else None


def validate_parameters(code, parameters):
    from service.rule_parameters import PARAMETERS
    fields = {f['key']: (int if f['type']=='integer' else (int,float), f['min'], f['max']) for f in PARAMETERS.get(code, [])}
    if not isinstance(parameters,dict) or set(parameters)-set(fields):
        raise ValueError('规则含不支持的参数')
    for key,value in parameters.items():
        kind,low,high = fields[key]
        if isinstance(value,bool) or not isinstance(value,kind) or not math.isfinite(value) or not low <= value <= high:
            raise ValueError('规则参数类型或范围无效')


def evaluate_candidates(config, data, payload):
    """工位与区域细分仍使用同一算子配置、作用域和参数。"""
    from service.core_behaviors import CORE_SCENES, engine
    if config.code in CORE_SCENES or (data.get('type')=='behavior_detected' and config.code in ('phone_call','phone_capture')):
        return engine.evaluate(config, data, payload)
    if config.code.startswith('phone_') and payload.get('person_tracks'):
        output=[]
        for index,phone in enumerate(payload.get('phone_boxes') or []):
            x,y=(phone[0]+phone[2])/2,(phone[1]+phone[3])/2
            holders=[t for t in payload['person_tracks'] if t['bbox'][0]<=x<=t['bbox'][2] and t['bbox'][1]<=y<=t['bbox'][3]]
            # 重叠人物无法唯一归属时不合并轨迹，也不推断身份。
            holder=holders[0] if len(holders)==1 else None
            values={**payload,'phone_boxes':[phone],'phone_postures':(payload.get('phone_postures') or [])[index:index+1],
                'person_boxes':[holder['bbox']] if holder else payload.get('person_boxes',[])}
            result=evaluate_operator(config,data,values)
            if result:
                output.append((holder['track_id'] if holder else None,result))
        return output
    seats = payload.get('seats') or []
    if data.get('type') == 'person_detected' and seats and config.code in ('seat_absence','zone_intrusion'):
        output = []
        for seat in seats:
            restricted = seat.get('type') == 'restricted'
            if restricted != (config.code == 'zone_intrusion'):
                continue
            subject = str(seat.get('zone_id') or seat.get('name') or '')
            candidate = {**data, 'zone_id':subject}
            values = {**payload, 'absence_seconds':float(seat.get('absent_sec',0)),
                      'inside':restricted, 'person_count':int(seat.get('person_count',0) or 0), 'zone_name':seat.get('name')}
            result = evaluate_operator(config,candidate,values)
            if result:
                output.append((subject,result))
        return output
    result = evaluate_operator(config,data,payload)
    return [(data.get('zone_id') or ('room-empty' if config.code=='seat_absence' else 'room-crowd' if config.code=='crowd_gathering' else None),result)] if result else []


# Core rules are evaluated as candidate sets, with the same configuration lifecycle.
from service.core_behaviors import CORE_SCENES
HANDLERS.update({code: lambda *_: None for code in CORE_SCENES})
