"""Audio-to-semantic scheduling hook; it does not modify ASR recordings."""


SCENES = ("S06", "S07")


def schedule_ready_recording(recording_id, *, recording_loader, segment_loader, metadata_builder,
                             window_builder, enqueue, scenes=SCENES):
    """Build semantic task inputs for a ready recording through injected adapters.

    The audio worker only reads the completed ASR output. Database writes for semantic
    tasks are performed by the API/semantic repository supplied through ``enqueue``.
    """
    recording = recording_loader(recording_id)
    if not recording or recording.get("status") != "ready":
        return []
    segments = segment_loader(recording_id)
    result = []
    for scene_id in scenes:
        metadata = metadata_builder(recording, scene_id)
        for request in window_builder(segments, metadata, recording["duration_ms"]):
            result.append(enqueue(request))
    return result
