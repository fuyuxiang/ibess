"""Redis wire envelopes; legacy unversioned producers remain readable during rollout."""

import json
import math
from ibess_contracts import CONTRACT_VERSION


def observation_message(kind, source, session, payload, captured_at):
    if not isinstance(payload, dict) or not math.isfinite(float(captured_at)):
        raise ValueError(
            "Observation requires structured payload and finite capture time"
        )
    return {
        "contract_version": CONTRACT_VERSION,
        "type": kind,
        "source_id": source,
        "session_id": session or "unbound",
        "occurred_at": captured_at,
        "payload": json.dumps(payload, ensure_ascii=False, allow_nan=False),
    }


def decode_message(data):
    decoded = {
        (k.decode() if isinstance(k, bytes) else k): (
            v.decode() if isinstance(v, bytes) else v
        )
        for k, v in data.items()
    }
    if decoded.get("contract_version", CONTRACT_VERSION) != CONTRACT_VERSION:
        raise ValueError("Unsupported message contract version")
    return decoded


def event_message(fields):
    """Version an existing alert envelope while preserving additive business fields."""
    required = (
        "event_id",
        "event_type",
        "camera_id",
        "session_id",
        "risk_level",
        "reason",
        "status",
        "occurred_at",
    )
    if any(not isinstance(fields.get(key), str) or not fields[key] for key in required):
        raise ValueError("Alert envelope is incomplete")
    return {**fields, "contract_version": CONTRACT_VERSION}
