"""整理模型句段，毫秒时间相对当前录音；匿名标签仅在一条录音内有效。"""
import math
from numbers import Integral


def speaker_name(index):
    name = ''
    while True:
        name = chr(65 + index % 26) + name
        index = index // 26 - 1
        if index < 0:
            return name


def normalize_segments(result, duration_ms):
    valid = []
    for item in result.get('sentence_info', []) or []:
        try:
            start, end = float(item['start']), float(item['end'])
            text = str(item.get('text') or '').strip()
            if not math.isfinite(start) or not math.isfinite(end) or start < 0 or start >= duration_ms or end <= start or not text:
                continue
            if end > duration_ms + 250:
                continue
            start, end = round(start), min(round(end), duration_ms)
            if end <= start:
                continue
            speaker = item.get('spk')
            if not isinstance(speaker, Integral) or isinstance(speaker, bool) or speaker < 0:
                speaker = None
            valid.append({'start_ms':start, 'end_ms':end, 'raw_speaker':speaker, 'text':text})
        except (KeyError, TypeError, ValueError):
            continue
    valid.sort(key=lambda row: (row['start_ms'], row['end_ms']))
    labels = {}
    for row in valid:
        raw = row.pop('raw_speaker')
        if raw is not None and raw not in labels:
            labels[raw] = speaker_name(len(labels))
        row['speaker'] = labels.get(raw)
    return valid
