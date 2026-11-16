"""The legacy ring buffer belongs to one configured source, never every camera."""
import re
from urllib.parse import urlsplit
from uuid import UUID


def normalize_source(value):
    if value == 'camera-real-01':
        return 'cam-ezviz'
    match = re.fullmatch(r'(?:camera-0*|cam)(\d+)', value or '')
    return f'cam{int(match.group(1))}' if match else value


def configured_source(rtsp_url, explicit=None):
    return normalize_source(explicit or urlsplit(rtsp_url).path.strip('/').split('/')[-1])


def should_capture(data, source):
    try:
        UUID(data.get('event_id', ''))
    except (ValueError, TypeError, AttributeError):
        return False
    if not source or normalize_source(data.get('camera_id')) != source:
        return False
    if data.get('source_type') in ('audio','simulation') or str(data.get('is_demo','')).lower() == 'true':
        return False
    if str(data.get('event_type','')).startswith('semantic_') or data.get('skill_code') == 'demo_simulation':
        return False
    return data.get('risk_level') in ('high','medium','low') and data.get('status','PENDING') == 'PENDING'
