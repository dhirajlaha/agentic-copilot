import uuid

from pydantic import BaseModel, ConfigDict


class UserOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    github_login: str
    email: str | None
    avatar_url: str | None
