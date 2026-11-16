"""Build bounded semantic windows from complete ASR segments."""

from copy import deepcopy


class WindowBuildError(ValueError):
    """The transcript cannot produce a trustworthy semantic window."""


def _validate_segments(segments, duration_ms):
    if isinstance(duration_ms, bool) or not isinstance(duration_ms, int) or duration_ms <= 0:
        raise WindowBuildError("invalid recording duration")
    if not isinstance(segments, list):
        raise WindowBuildError("segments must be a list")

    seen = set()
    normalized = []
    for segment in segments:
        if not isinstance(segment, dict):
            raise WindowBuildError("segment must be an object")
        required = {"segment_id", "speaker", "start_ms", "end_ms", "text"}
        if set(segment) != required:
            raise WindowBuildError("segment fields are incomplete")
        segment_id = segment["segment_id"]
        start_ms, end_ms = segment["start_ms"], segment["end_ms"]
        if not isinstance(segment_id, str) or not segment_id.strip() or segment_id in seen:
            raise WindowBuildError("segment id must be unique")
        if (isinstance(start_ms, bool) or isinstance(end_ms, bool)
                or not isinstance(start_ms, int) or not isinstance(end_ms, int)
                or start_ms < 0 or end_ms <= start_ms or end_ms > duration_ms):
            raise WindowBuildError("segment time is outside recording")
        if not isinstance(segment["text"], str) or not segment["text"].strip():
            raise WindowBuildError("segment text is empty")
        seen.add(segment_id)
        normalized.append(deepcopy(segment))
    return sorted(normalized, key=lambda item: (item["start_ms"], item["end_ms"], item["segment_id"]))


def _bounded_range(target, duration_ms, neighbor_ms, max_window_ms):
    target_start, target_end = target["start_ms"], target["end_ms"]
    target_length = target_end - target_start
    if target_length > max_window_ms:
        raise WindowBuildError("target segment exceeds maximum semantic window")

    before = min(neighbor_ms, target_start)
    after = min(neighbor_ms, duration_ms - target_end)
    available = max_window_ms - target_length
    if before + after > available:
        # Keep the target intact and trim the farthest context first.
        before = min(before, available // 2)
        after = min(after, available - before)
        if before + after < available:
            before = min(min(neighbor_ms, target_start), available - after)
    return target_start - before, target_end + after


def build_windows(segments, metadata, *, duration_ms, neighbor_ms=15000, max_window_ms=60000):
    """Return one request per target segment with complete neighboring segments."""
    if isinstance(neighbor_ms, bool) or not isinstance(neighbor_ms, int) or neighbor_ms < 0:
        raise WindowBuildError("invalid neighbor window")
    if isinstance(max_window_ms, bool) or not isinstance(max_window_ms, int) or max_window_ms <= 0:
        raise WindowBuildError("invalid maximum window")
    if not isinstance(metadata, dict):
        raise WindowBuildError("metadata must be an object")

    normalized = _validate_segments(segments, duration_ms)
    if not normalized:
        return []
    output = []
    for target in normalized:
        start_ms, end_ms = _bounded_range(target, duration_ms, neighbor_ms, max_window_ms)
        complete = [
            deepcopy(segment) for segment in normalized
            if segment["start_ms"] >= start_ms and segment["end_ms"] <= end_ms
        ]
        request = deepcopy(metadata)
        request["target_segment_ids"] = [target["segment_id"]]
        request["segments"] = complete
        request["window_start_ms"] = start_ms
        request["window_end_ms"] = end_ms
        output.append(request)
    return output
