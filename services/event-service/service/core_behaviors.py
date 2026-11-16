"""Camera facts → configurable core rules. State is isolated by binding, phase and media run."""
from collections import deque
from datetime import datetime
import math

CORE_SCENES = {
    'whispering': 'S05', 'workstation_visit': 'S05', 'document_transfer': 'S08',
    'frequent_absence': 'S03', 'joint_absence': 'S03', 'unauthorized_entry': 'S04',
}
INDEPENDENT_PHASES = {'PRELIM', 'DETAIL', 'PRELIMINARY', 'DETAILED'}
REVIEW_RULES = ('whispering', 'document_transfer', 'phone_call', 'phone_capture')


class CoreRules:
    def __init__(self):
        self.states = {}
        self.generations = {}

    def evaluate(self, config, data, payload):
        from service.operators import OperatorResult, _scope_matches
        scope_data = {**data, 'zone_id': config.zone_id} if config.zone_id else data
        if not config.enabled or data.get('type') not in config.input_types or not _scope_matches(config, scope_data):
            return []
        if not payload.get('bound') or payload.get('quality') != 'clear':
            self._clear(data.get('source_id'))
            return []
        code, now = config.code, float(data['occurred_at'])
        if bool(payload.get('review_only')) != (code in REVIEW_RULES):
            return []
        phase = payload.get('phase_id')
        if code in ('whispering', 'workstation_visit', 'document_transfer') and phase not in INDEPENDENT_PHASES:
            return []
        generation = (data.get('session_id'), payload.get('media_run_id'), payload.get('phase_version'), phase)
        source = data.get('source_id')
        if self.generations.get(source) != generation:
            self._clear(source)
            self.generations[source] = generation
        key = (source, generation, config.code, config.session_id, config.camera_id, config.zone_id, config.version,
               tuple(sorted(config.parameters.items())))
        state = self.states.setdefault(key, {'last': None, 'seats': {}, 'visits': {}, 'exits': {}, 'results': []})
        if state['last'] is not None and now <= state['last']:
            # The same observation still describes the same condition; don't falsely close its span.
            return list(state['results']) if now == state['last'] else []
        if state['last'] is not None and now - state['last'] > 5:
            state['seats'].clear()
            state['visits'].clear()
            state['exits'].clear()
        state['last'] = now
        if len(self.states) > 2048:
            self.states = {key: state}
        found = []

        def emit(subject, reason, start, detail, confidence=None):
            if config.zone_id and detail.get('zone_id') != config.zone_id and config.zone_id not in detail.get('zone_ids', []):
                return
            if not math.isfinite(start) or not 0 <= now-start <= 86400:
                return
            found.append((subject, OperatorResult(config.output_event_type, config.risk_level, confidence,
                reason, {**detail, 'analysis_model':payload.get('analysis_model'), 'model_version':payload.get('model_version'),
                         'phase_version':payload.get('phase_version'), 'started_at': start,
                         'ended_at': detail.get('occurred_at') or now, 'duration_seconds': now-start})))

        p = config.parameters
        if code in REVIEW_RULES:
            for fact in payload.get('facts', []):
                if fact.get('kind') != code or fact.get('visibility') != 'clear':
                    continue
                participants = fact.get('track_ids', [])
                if len(set(participants)) != (1 if code.startswith('phone_') else 2) or not fact.get('evidence_frames'):
                    continue
                duration = float(fact.get('duration_seconds', 0))
                minimum = {'whispering':3,'document_transfer':.5,'phone_call':2,'phone_capture':1}[code]
                if duration < float(p.get('duration_seconds', minimum)):
                    continue
                if code == 'document_transfer' and not fact.get('object_visible'):
                    continue
                if code.startswith('phone_') and not fact.get('object_visible'):
                    continue
                if code == 'phone_capture' and fact.get('target_visible') is not True:
                    continue
                if fact.get('authorized'):
                    continue
                label = {'whispering':'疑似交头接耳，需人工复核原片', 'document_transfer':'疑似纸张或文件递接，需核对材料性质及授权',
                         'phone_call':'连续画面可见手机贴耳通话姿态，不能证明电话接通或对方身份',
                         'phone_capture':'连续画面可见手机对准文件或屏幕，疑似拍摄'}[code]
                emit(':'.join(sorted(participants)), label, now-duration, fact, fact.get('confidence'))
        elif code == 'workstation_visit':
            active = set()
            for visit in payload.get('visits', []):
                if visit.get('authorized') or visit.get('home_seat_id') == visit.get('seat_id'):
                    continue
                subject = f"{visit['track_id']}:{visit['seat_id']}"
                active.add(subject)
                start = state['visits'].setdefault(subject, now)
                if now-start >= float(p.get('dwell_seconds', 5)):
                    emit(subject, '人员离开原工位后在他人工位持续停留，疑似串岗', start, visit)
            state['visits'] = {k: v for k, v in state['visits'].items() if k in active}
        elif code in ('frequent_absence', 'joint_absence'):
            if code == 'joint_absence':
                window = float(p.get('departure_window_seconds', 10))
                state['exits'] = {k:v for k,v in state['exits'].items() if now-v <= window}
                for crossing in payload.get('crossings', []):
                    if crossing.get('authorized'):
                        state['exits'].pop(crossing['track_id'], None)
                        continue
                    if crossing.get('direction') == 'out':
                        state['exits'].setdefault(crossing['track_id'], now)
                    elif crossing.get('direction') == 'in':
                        state['exits'].pop(crossing['track_id'], None)
                if len(state['exits']) >= int(p.get('count_threshold', 2)):
                    emit('joint-group', '入口画面短时间内出现多条离室轨迹，疑似多人共同离室，需复核人数与许可',
                         min(state['exits'].values()), {'track_ids':sorted(state['exits']), 'basis':'exit_crossings'})
                    state['results'] = found
                    return found
            missing = []
            for seat in payload.get('seats', []):
                sid = seat['seat_id']
                item = state['seats'].setdefault(sid, {'occupied': None, 'since': now, 'departures': deque(), 'ever_occupied': False})
                if seat.get('occupied'):
                    item['ever_occupied'] = True
                if seat.get('authorized'):
                    item.update(occupied=seat.get('occupied'), since=now)
                    item['departures'].clear()
                    continue
                occupied = bool(seat.get('occupied'))
                if item['occupied'] is not None and occupied != item['occupied']:
                    item['since'] = now
                    if not occupied and item['ever_occupied']:
                        item['departures'].append(now)
                item['occupied'] = occupied
                window = float(p.get('window_seconds', 600))
                while item['departures'] and now-item['departures'][0] > window:
                    item['departures'].popleft()
                if not occupied and item['ever_occupied']:
                    missing.append((sid, item['since'], seat))
                if code == 'frequent_absence' and len(item['departures']) >= int(p.get('count_threshold', 3)):
                    emit(sid, f"工位在 {window:.0f} 秒内离席 {len(item['departures'])} 次", item['departures'][0],
                         {**seat, 'departure_count': len(item['departures']), 'basis': 'seat_transitions'})
            if code == 'joint_absence':
                window = float(p.get('departure_window_seconds', 10))
                hold = float(p.get('absence_seconds', 5))
                # A common departure window, not just several long-empty seats.
                for _, start, _ in missing:
                    group = [(sid, at, seat) for sid, at, seat in missing if start <= at <= start+window and now-at >= hold]
                    if len(group) >= int(p.get('count_threshold', 2)):
                        ids = sorted(sid for sid, _, _ in group)
                        emit('joint-group', f"{len(ids)} 个工位短时间内同时离席，需核查是否获准集体离开",
                             min(at for _, at, _ in group), {'seat_ids': ids, 'zone_ids': [s.get('zone_id') for _, _, s in group], 'basis': 'seat_vacancy'})
                        break
        elif code == 'unauthorized_entry':
            for entry in payload.get('entries', []):
                # Unmatched/blurred faces are a capability gap, never proof of unauthorized entry.
                if entry.get('status') != 'matched' or entry.get('authorized') is not False:
                    continue
                if not entry.get('person_id') or not entry.get('authorization_snapshot_id'):
                    continue
                emit(entry['track_id'], '已识别人员进入本评标室，但不在当前有效授权名单内', float(entry.get('occurred_at') or now),
                     entry, entry.get('similarity'))
        state['results'] = found
        return found

    def _clear(self, source):
        self.states = {k: v for k, v in self.states.items() if k[0] != source}


engine = CoreRules()
