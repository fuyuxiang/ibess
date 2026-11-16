"""Additive camera-rule registration. Existing user parameters and switches are preserved."""
from datetime import datetime
from uuid import uuid4
from sqlalchemy.orm import Session
from app.models import Operator
from app.core.rule_parameters import PARAMETERS

CORE_RULES = {
    'whispering': ('疑似交头接耳', '独立评审阶段，连续画面确认两人近距交流姿态，需人工核查。'),
    'workstation_visit': ('疑似串岗', '人员离开原工位后持续停留于他人工位。'),
    'document_transfer': ('疑似资料递接', '连续画面可见纸张或文件在两名人员之间递接。'),
    'frequent_absence': ('频繁离席', '同一工位在时间窗口内多次由占用变为空置，排除登记的离岗许可。'),
    'joint_absence': ('多人同时离席', '多个已占用工位在共同时间窗口内离席，不能单凭工位画面认定离室。'),
    'unauthorized_entry': ('疑似未授权人员进入', '入口越线后，本地人脸识别匹配到已登记人员，但不在本场有效授权名单。'),
}
BASE_RULES = {
    'crowd_gathering': ('空间聚集', '人员达到人数和空间距离条件并持续存在。'),
    'seat_absence': ('连续离席', '已配置工位持续空置。'),
    'zone_intrusion': ('限制区越界', '限制区域内出现人员，默认覆盖单人进入。'),
    'phone_call': ('疑似通话', '连续画面可见手机贴耳通话姿态。'),
    'phone_capture': ('疑似拍摄', '手机明确对准文件或屏幕，连续画面复核。'),
    'phone_detected': ('检出手机', '检出手机但具体动作未确认时保留基础线索。'),
    'phone_unstored': ('入口手机携带', '入口携带手机线索；寄存情况需人工核实。'),
}


def ensure_camera_rules(engine):
    from app.supervision_models import OperatorAudit
    with Session(engine) as db:
        if engine.dialect.name == 'postgresql':
            from sqlalchemy import text
            db.execute(text('select pg_advisory_xact_lock(81234172)'))
        for code, (name, description) in {**BASE_RULES, **CORE_RULES}.items():
            existing = db.query(Operator).filter_by(code=code).first()
            if existing:
                if code in ('phone_call','phone_capture') and 'behavior_detected' not in (existing.input_types or []):
                    before = {'input_types': list(existing.input_types or [])}
                    existing.input_types = list(existing.input_types or [])+['behavior_detected']
                    db.add(OperatorAudit(operator_id=existing.operator_id, actor='system', action='upgrade', before=before,
                                         after={'input_types': existing.input_types}, at=datetime.utcnow()))
                continue
            row = Operator(operator_id=str(uuid4()), code=code, name=name, description=description,
                           category='vision', version='camera-v2', enabled=True,
                           input_types=(['behavior_detected'] if code in CORE_RULES else ['person_detected','behavior_detected'] if code in ('phone_call','phone_capture') else ['person_detected']),
                           output_event_type=code, risk_level='medium',
                           parameters={p['key']: p['default'] for p in PARAMETERS[code]})
            db.add(row)
            db.add(OperatorAudit(operator_id=row.operator_id, actor='system', action='register', before={},
                                 after={'code': code, 'version': 'camera-v2'}, at=datetime.utcnow()))
        # Upgrade the legacy shipped threshold; custom values/versions are untouched.
        zone = db.query(Operator).filter_by(code='zone_intrusion').first()
        if zone and zone.version in ('v1', '1.0.0') and zone.parameters == {'person_threshold': 2}:
            before = dict(zone.parameters)
            zone.parameters, zone.version = {'person_threshold': 1}, 'camera-v2'
            db.add(OperatorAudit(operator_id=zone.operator_id, actor='system', action='upgrade', before=before,
                                 after=zone.parameters, at=datetime.utcnow()))
        db.commit()
