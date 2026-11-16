"""Continuous camera facts, bounded asynchronous interaction review and entrance recognition.

Frames carry capture time, source-local track IDs and frozen business scope. Model output
cannot supply identities, authorizations or timestamps. Missing capabilities are reported.
"""
from collections import deque
from pathlib import Path
import json
import hashlib
import logging
import math
import queue
import threading
import time

import cv2

from worker.zone_geometry import bbox_polygon_overlap_ratio, bbox_center, segment_side, segments_intersect
from worker.visual_client import VisualModelConfig, OpenAICompatibleVisionClient, encode_jpeg, image_part

log = logging.getLogger('vision.core')


def authorized(auths, kinds, room_id, seat_ids):
    """Only trusted room/seat facts apply; track labels are never treated as identities."""
    return any(a['kind'] in kinds and ((room_id and a['scope'].get('room_id') == room_id) or
               (a['scope'].get('seat_id') is not None and a['scope'].get('seat_id') in seat_ids)) for a in auths)

PROMPT = (Path(__file__).resolve().parents[1]/'prompts/core-interactions-v2.txt').read_text(encoding='utf-8')
PROMPT_VERSION = 'core-v2:'+hashlib.sha256(PROMPT.encode()).hexdigest()[:12]


def validate_review(text, samples):
    result = json.loads(text)
    if not isinstance(result, dict) or set(result) != {'facts'} or not isinstance(result['facts'], list) or len(result['facts']) > 20:
        raise ValueError('invalid interaction result')
    facts = []
    for fact in result['facts']:
        required = {'kind', 'track_ids', 'visibility', 'evidence_frames', 'object_visible', 'confidence'}
        if not isinstance(fact, dict) or not required.issubset(fact) or set(fact)-required-{'target_visible'}:
            raise ValueError('invalid fact fields')
        if fact['kind'] not in ('whispering', 'document_transfer', 'phone_call', 'phone_capture') or fact['visibility'] not in ('clear', 'unclear'):
            raise ValueError('invalid action')
        ids, frames, conf = fact['track_ids'], fact['evidence_frames'], fact['confidence']
        count = 1 if fact['kind'].startswith('phone_') else 2
        if not isinstance(ids, list) or len(ids) != count or any(not isinstance(x, str) for x in ids) or len(set(ids)) != count:
            raise ValueError('invalid participants')
        if not isinstance(frames, list) or len(set(frames)) < 2 or any(type(i) is not int or not 0 <= i < len(samples) for i in frames):
            raise ValueError('invalid evidence frames')
        if type(fact['object_visible']) is not bool or isinstance(conf, bool) or not isinstance(conf, (float, int)) or not math.isfinite(conf) or not 0 <= conf <= 1:
            raise ValueError('invalid confidence')
        if any(not set(ids).issubset({t['track_id'] for t in samples[i]['tracks']}) for i in frames):
            raise ValueError('participants not visible in cited frames')
        if fact['visibility'] != 'clear' or conf < 0.7:
            continue
        if fact['kind'] == 'document_transfer' and not fact['object_visible']:
            continue
        if fact['kind'].startswith('phone_') and not fact['object_visible']:
            continue
        if fact['kind'] == 'phone_capture' and fact.get('target_visible') is not True:
            continue
        first, last = min(frames), max(frames)
        facts.append({**fact, 'duration_seconds': samples[last]['at']-samples[first]['at'],
                      'started_at': samples[first]['at'], 'ended_at': samples[last]['at']})
    return facts


class InteractionReview:
    def __init__(self, client):
        self.client = client
        self.inbox, self.outbox = queue.Queue(maxsize=1), queue.Queue(maxsize=4)
        self.status = 'ready' if client else 'model_not_configured'
        if client:
            threading.Thread(target=self._loop, daemon=True, name='core-interactions').start()

    def submit(self, samples, scope):
        if not self.client or self.inbox.full():
            return
        self.inbox.put_nowait((list(samples), dict(scope)))

    def _loop(self):
        while True:
            samples, scope = self.inbox.get()
            try:
                content = []
                for index, sample in enumerate(samples):
                    content.append({'type': 'text', 'text': json.dumps({'frame_index': index, 'capture_time': sample['at'], 'tracks': sample['tracks']})})
                    content.append(image_part(sample['jpeg']))
                answer = self.client.complete([{'role': 'system', 'content': PROMPT}, {'role': 'user', 'content': content}], max_tokens=1200)
                facts = validate_review(answer, samples)
                self.status = 'ready'
                if not self.outbox.full():
                    self.outbox.put_nowait((samples[-1]['at'], scope, facts, samples[-1].get('frame_key'), samples))
            except Exception as exc:
                self.status = 'review_failed'
                log.warning('连续画面复核失败：%s', type(exc).__name__)
            finally:
                self.inbox.task_done()


class BehaviorObserver:
    def __init__(self):
        self.reset()

    def reset(self):
        self.homes, self.home_pending, self.sides, self.seat_states, self.track_seen = {}, {}, {}, {}, {}
        self.generation, self.last_at = None, None

    def observe(self, at, tracks, zones, lines, generation, permitted=()):
        if generation != self.generation or (self.last_at is not None and at-self.last_at > 5):
            self.reset()
            self.generation = generation
        self.last_at = at
        seats, visits, crossings = [], [], []
        live = {t['track_id'] for t in tracks}
        self.track_seen.update({tid: at for tid in live})
        self.track_seen = {tid:last for tid,last in self.track_seen.items() if at-last <= 2}
        self.homes = {k: v for k, v in self.homes.items() if k in self.track_seen}
        self.home_pending = {k: v for k, v in self.home_pending.items() if k in live}
        self.sides = {k: v for k, v in self.sides.items() if k[1] in self.track_seen}
        for zone in zones:
            if zone.get('kind', zone.get('type')) != 'seat':
                continue
            sid = str(zone.get('seat_id') or zone.get('zone_id') or zone['name'])
            occupants = [t for t in tracks if bbox_polygon_overlap_ratio(tuple(t['bbox']), tuple(map(tuple, zone['polygon']))) >= .3]
            occupied = bool(occupants)
            state = self.seat_states.setdefault(sid, {'value': occupied, 'pending': occupied, 'at': at})
            if state['pending'] != occupied:
                state.update(pending=occupied, at=at)
            if at-state['at'] >= 1:
                state['value'] = occupied
            seats.append({'seat_id': sid, 'zone_id': zone.get('zone_id', sid), 'occupied': state['value'], 'authorized': sid in permitted})
            for track in occupants:
                tid = track['track_id']
                if tid not in self.homes:
                    prior = self.home_pending.get(tid)
                    if prior is None or prior[0] != sid:
                        self.home_pending[tid] = (sid, at)
                    elif at-prior[1] >= 2:
                        self.homes[tid] = sid
                elif self.homes[tid] != sid:
                    home = next((z for z in zones if str(z.get('seat_id') or z.get('zone_id') or z.get('name'))==self.homes[tid]), None)
                    if home and bbox_polygon_overlap_ratio(tuple(track['bbox']), tuple(map(tuple, home['polygon']))) >= .3:
                        continue  # overlapping drawings alone do not prove the person left their seat
                    visits.append({'track_id': tid, 'home_seat_id': self.homes[tid], 'seat_id': sid,
                                   'zone_id': zone.get('zone_id', sid), 'authorized': False})
        for track in tracks:
            center = bbox_center(tuple(track['bbox']))
            for line in lines:
                start, end = tuple(line['start']), tuple(line['end'])
                side = segment_side(center, start, end)
                if not side:
                    continue
                key = (line['line_id'], track['track_id'])
                previous = self.sides.get(key)
                self.sides[key] = (side, center)
                if previous and previous[0] != side and segments_intersect(previous[1], center, start, end):
                    direction = 'in' if side > 0 else 'out'
                    if line.get('direction', 'both') in ('both', direction):
                        crossings.append({**track, 'direction': direction, 'line_id': line['line_id']})
        return seats, visits, crossings


class CorePipeline:
    def __init__(self, source, context, *, client=None, identity=None, interval=4):
        self.source, self.context, self.identity = source, context, identity
        self.observer, self.review = BehaviorObserver(), InteractionReview(client)
        self.frames = deque(maxlen=8)
        self.interval, self.last_sample, self.last_review = interval, 0, 0
        self.generation = None

    @classmethod
    def from_env(cls, source, env):
        if env.get('CORE_BEHAVIORS_ENABLED', '1') != '1':
            return None
        from worker.a_context import DbContextSource, CachedContext
        from worker.entrance_identity import EntranceIdentity
        # Explicit vision configuration; an existing VLM endpoint can be reused.
        vision_env = {**env, 'VISUAL_MODEL_BASE_URL': env.get('VISUAL_MODEL_BASE_URL') or env.get('VLM_BASE_URL', ''),
                      'VISUAL_MODEL_NAME': env.get('VISUAL_MODEL_NAME') or env.get('VLM_MODEL', ''),
                      'VISUAL_MODEL_API_KEY': env.get('VISUAL_MODEL_API_KEY') or env.get('VLM_API_KEY', '')}
        config = VisualModelConfig.from_env(vision_env)
        context = CachedContext(DbContextSource(env.get('DATABASE_URL', '')), source, refresh_seconds=3, background=True)
        return cls(source, context, client=OpenAICompatibleVisionClient(config) if config else None,
                   identity=EntranceIdentity(env.get('DATABASE_URL', ''), env.get('FACE_MODEL_DIR', '/face-models')),
                   interval=float(env.get('CORE_REVIEW_INTERVAL', '4')))

    def on_frame(self, frame, tracks, media_run, at, frame_key=None, degraded=False):
        rc = self.context.get()
        scope = dict(rc.scope)
        zones = (rc.zone_config or {}).get('zones', [])
        lines = (rc.zone_config or {}).get('entry_lines', [])
        fresh = bool(rc.loaded_at and time.time()-rc.loaded_at < 15)
        bound = bool(scope.get('stage_snapshot_id') and fresh)
        generation = (scope.get('session_id'), media_run, scope.get('phase_id'), scope.get('phase_version'), (rc.zone_config or {}).get('config_version'), rc.identity_verification_enabled)
        if generation != self.generation or degraded or not bound:
            self.frames.clear()
            self.observer.reset()
            self.generation = generation
        capability = {'spatial': 'ready' if zones and bound else 'zones_or_binding_missing',
                      'interactions': self.review.status if bound else 'binding_missing',
                      'entrance': ('not_enabled' if not rc.identity_verification_enabled else
                          getattr(self.identity, 'status', 'model_not_configured') if lines and bound else 'entry_line_or_binding_missing')}
        if degraded:
            capability = dict.fromkeys(capability, 'camera_degraded')
        base = {'bound': bound, 'quality': 'degraded' if degraded else 'clear', 'media_run_id': media_run,
                'phase_id': scope.get('phase_id'), 'phase_version': scope.get('phase_version'),
                'model_name': 'camera-core', 'model_version': PROMPT_VERSION,
                'analysis_model': getattr(getattr(self.review.client,'config',None),'model',None),
                'capabilities': capability, 'frame_key': frame_key, 'seats': [], 'visits': [], 'entries': [], 'crossings': [], 'facts': []}
        output = []
        if bound and not degraded:
            permitted = set()
            moment = datetime_utc(at)
            active_auths = [a for a in rc.authorizations if a['valid_from'] <= moment and (not a.get('valid_until') or moment <= a['valid_until'])]
            for zone in zones:
                sid = str(zone.get('seat_id') or zone.get('zone_id') or zone.get('name'))
                if authorized(active_auths, ('leave_approved','break'), scope['room_id'], {sid}):
                    permitted.add(sid)
            seats, visits, crossings = self.observer.observe(at, tracks, zones, lines, generation, permitted)
            for visit in visits:
                visit['authorized'] = authorized(active_auths, ('contact_allowed',), scope['room_id'], {visit['home_seat_id'], visit['seat_id']})
            base.update(seats=seats, visits=visits, crossings=[{'track_id':c['track_id'],'direction':c['direction'],'line_id':c['line_id'],
                'authorized':authorized(active_auths, ('leave_approved','break'),scope['room_id'], {self.observer.homes.get(c['track_id'])})} for c in crossings])
            if self.identity and rc.identity_verification_enabled:
                entries = self.identity.check(frame, [c for c in crossings if c['direction']=='in'], {**scope, 'occurred_at': at})
                for entry in entries:
                    jpeg = entry.pop('_capture_jpeg',None)
                    if entry.get('status')=='matched' and entry.get('authorized') is False:
                        output.append({'occurred_at':entry['occurred_at'], 'session_id':scope['session_id'], 'capture_jpeg':jpeg,
                            'payload':{**base,'seats':[],'visits':[],'crossings':[],'entries':[entry],'identity_only':True,'frame_key':None}})
                capability['entrance'] = self.identity.status if lines else 'entry_line_missing'
            if self.frames and at-self.frames[-1]['at'] > 3:
                self.frames.clear()
            if len(tracks) >= 1 and at-self.last_sample >= .5:
                self.frames.append({'at': at, 'jpeg': encode_jpeg(frame, max_side=1600), 'tracks': tracks, 'frame_key': frame_key})
                self.last_sample = at
            if tracks and len(self.frames) >= 3 and self.frames[-1]['at']-self.frames[0]['at'] >= 3 and at-self.last_review >= self.interval:
                self.review.submit(self.frames, {**scope, 'generation': generation, 'base': dict(base),
                                                'authorizations': list(rc.authorizations), 'homes': dict(self.observer.homes),
                                                'seat_zones': {z.get('seat_id'):z.get('zone_id') for z in zones if z.get('kind')=='seat'}})
                self.last_review = at
        while not self.review.outbox.empty():
            captured, frozen, facts, key, samples = self.review.outbox.get_nowait()
            if frozen['generation'] != generation or at-captured > 30 or not bound or degraded:
                continue
            for fact in facts:
                kinds = {'whispering':('contact_allowed',), 'document_transfer':('document_access',),
                         'phone_call':('device_allowed',), 'phone_capture':('photo_allowed',)}.get(fact['kind'], ())
                home_seats = {frozen.get('homes',{}).get(t) for t in fact.get('track_ids', [])}-{None}
                fact['zone_ids'] = [frozen.get('seat_zones',{}).get(s) for s in home_seats if frozen.get('seat_zones',{}).get(s)]
                start = datetime_utc(fact.get('started_at',captured-fact.get('duration_seconds',0)))
                end = datetime_utc(fact.get('ended_at',captured))
                whole_interval = [a for a in frozen.get('authorizations',[]) if a['valid_from']<=start and (not a.get('valid_until') or end<=a['valid_until'])]
                fact['authorized'] = authorized(whole_interval, kinds, frozen.get('room_id'), home_seats)
            groups = {}
            for fact in facts:
                groups.setdefault(fact.get('ended_at',captured), []).append(fact)
            if not groups:
                groups[captured] = []
            for ended, group in sorted(groups.items()):
                sample = next((s for s in samples if s['at']==ended), samples[-1])
                output.append({'occurred_at': ended, 'session_id': frozen['session_id'],
                               'capture_jpeg': sample['jpeg'] if group else None,
                               'payload': {**frozen['base'], 'seats': [], 'visits': [], 'entries': [], 'crossings': [], 'facts': group,
                                           'frame_key': sample.get('frame_key',key), 'review_only': True}})
        output.append({'occurred_at': at, 'session_id': scope.get('session_id'), 'payload': base})
        return output


def datetime_utc(epoch):
    from datetime import datetime, timezone
    return datetime.fromtimestamp(epoch, timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.%f')[:-3]+'Z'
