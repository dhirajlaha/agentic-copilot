"""Run routes. Starting a run only queues it (202); a worker executes it. Clients poll
GET /runs/{id} for status and the step trace."""

import uuid

from fastapi import APIRouter, Response, status

from app.api.deps import CurrentUser, RunServiceDep
from app.db.models import AgentRun
from app.schemas.run import RunDetailOut, RunOut

router = APIRouter(tags=["runs"])


@router.post(
    "/tickets/{ticket_id}/run", response_model=RunOut, status_code=status.HTTP_202_ACCEPTED
)
def start_run(
    ticket_id: uuid.UUID, response: Response, user: CurrentUser, service: RunServiceDep
) -> AgentRun:
    run = service.start(user, ticket_id)
    response.headers["Location"] = f"/runs/{run.id}"
    return run


@router.get("/runs/{run_id}", response_model=RunDetailOut)
def get_run(run_id: uuid.UUID, user: CurrentUser, service: RunServiceDep) -> AgentRun:
    return service.get(user, run_id)
