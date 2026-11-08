import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict


class RunOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    ticket_id: uuid.UUID
    status: str
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
    error: str | None
    # Set once the patch has been published as a draft pull request.
    publication_status: str | None
    branch_name: str | None
    pull_request_number: int | None
    pull_request_url: str | None
    published_at: datetime | None
    publish_error: str | None


class StepOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    agent_name: str
    input: dict[str, Any] | None
    output: dict[str, Any] | None
    tokens_used: int | None
    latency_ms: int | None
    created_at: datetime


class RunDetailOut(RunOut):
    steps: list[StepOut]
