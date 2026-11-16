"""声学事件 API（7.4 S09 最小路径）：录音高能量突发段分析。

离线声学分析与实时采集解耦（POC 无实时麦克风链路）；结果供人工听审参考，
不直接生成处置事件——S09 线索仍走语义通道（07 素材已命中 suspicious）。
"""
import os
from pathlib import Path
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.acoustic import analyze_wav
from app.audio_models import AudioRecording
from app.db import get_session

router = APIRouter()
AUDIO_DIR = Path(os.getenv("AUDIO_DIR", "/audio-data"))


@router.post("/audio/recordings/{recording_id}/acoustic-analysis")
def acoustic_analysis(recording_id: UUID, session: Session = Depends(get_session)):
    recording = session.get(AudioRecording, str(recording_id))
    if recording is None:
        raise HTTPException(404, "录音不存在")
    if not recording.original_path:
        raise HTTPException(422, "录音缺少原始文件")
    path = AUDIO_DIR / recording.original_path
    if not path.is_file():
        raise HTTPException(503, "录音原始文件不可用（音频卷未挂载或文件缺失）")
    try:
        result = analyze_wav(path)
    except ValueError as exc:
        raise HTTPException(422, f"音频格式不支持：{exc}") from None
    return {"recording_id": recording.recording_id, "title": recording.title, **result,
            "note": "声学突发段为喊叫/争吵的强度特征，需结合语义研判与原音人工听审确认。"}
