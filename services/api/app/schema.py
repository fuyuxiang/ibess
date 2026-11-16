"""Explicit model registry for migrations and isolated tests; never imports the application."""

from importlib import import_module

MODEL_MODULES = (
    "app.models",
    "app.business_models",
    "app.business_facts_models",
    "app.business_fact_link_models",
    "app.business_authz_models",
    "app.score_models",
    "app.video_evidence_models",
    "app.media_binding_models",
    "app.offline_qa_models",
    "app.auth_models",
    "app.face_models",
    "app.audio_models",
    "app.phase_models",
    "app.phase_control_models",
    "app.semantic_models",
    "app.supervision_models",
    "app.zone_config_models",
    "app.asset_models",
    "app.stage_media_binding_models",
    "app.offline_visual_models",
)


def load_metadata():
    from ibess_db.base import Base

    import_module("ibess_db.clues")
    for module in MODEL_MODULES:
        import_module(module)
    return Base.metadata
