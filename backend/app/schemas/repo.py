import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class RepoConnect(BaseModel):
    installation_id: uuid.UUID  # our id for the installation link, from GET /repos/available
    full_name: str = Field(min_length=3, max_length=140)  # "owner/repo"


class RepoOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    repo_full_name: str
    connected_at: datetime


class AvailableRepoOut(BaseModel):
    full_name: str
    private: bool


class AvailableInstallationOut(BaseModel):
    id: uuid.UUID
    account_login: str
    account_type: str
    reachable: bool
    repos: list[AvailableRepoOut]
