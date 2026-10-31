import uuid
from datetime import datetime
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, model_validator


class TicketCreate(BaseModel):
    title: str = Field(min_length=1, max_length=500)
    description: str = ""
    repo_connection_id: uuid.UUID


class TicketUpdate(BaseModel):
    """Partial update. `status` is deliberately absent: only the orchestrator changes it."""

    title: str | None = Field(default=None, min_length=1, max_length=500)
    description: str | None = None

    @model_validator(mode="after")
    def _no_explicit_nulls(self) -> Self:
        # Omitting a field leaves it unchanged; sending null is an error (columns are NOT NULL).
        for field in self.model_fields_set:
            if getattr(self, field) is None:
                raise ValueError(f"{field} cannot be null")
        return self


class TicketOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    title: str
    description: str
    repo_connection_id: uuid.UUID
    status: str
    created_at: datetime
