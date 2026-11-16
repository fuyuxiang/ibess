from datetime import datetime
from uuid import uuid4

"""HTTP input for a supervision decision or feedback operation."""
from pydantic import BaseModel, ConfigDict, Field


class OperationIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: str
    note: str = Field(min_length=1, max_length=4000)
    expected_version: int = Field(ge=0)
    expected_status: str
    request_id: str = Field(
        default_factory=lambda: str(uuid4()), min_length=1, max_length=64
    )
    assignee: str | None = Field(None, max_length=128)
    due_at: datetime | None = None
    resolution: str | None = None
    acknowledge_missing_evidence: bool = False
    evidence_note: str | None = Field(None, max_length=2000)
