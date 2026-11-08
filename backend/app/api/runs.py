"""Run routes. Starting a run only queues it (202); a worker executes it. Clients poll
GET /runs/{id} for status and the step trace, and publish a finished run's patch as a draft
pull request with POST /runs/{id}/publish."""

import uuid

from fastapi import APIRouter, Response, status

from app.api.deps import CurrentUser, PublishServiceDep, RunServiceDep
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


@router.post("/runs/{run_id}/publish", response_model=RunOut)
def publish_run(run_id: uuid.UUID, user: CurrentUser, service: PublishServiceDep) -> AgentRun:
    """Publish the run's patch as a DRAFT pull request in the ticket's repository.

    An explicit action: it writes a branch and opens a pull request. Safe to repeat: a run that
    was already published returns its existing pull request.
    """
    return service.publish(user, run_id)
