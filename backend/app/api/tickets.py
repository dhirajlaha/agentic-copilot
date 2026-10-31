"""Ticket routes. Thin: validate input, call TicketService, shape the response."""

import uuid
from collections.abc import Sequence
from typing import Annotated

from fastapi import APIRouter, Query, Response, status

from app.api.deps import CurrentUser, TicketServiceDep
from app.db.enums import TicketStatus
from app.db.models import Ticket
from app.schemas.ticket import TicketCreate, TicketOut, TicketUpdate

router = APIRouter(prefix="/tickets", tags=["tickets"])


@router.post("", response_model=TicketOut, status_code=status.HTTP_201_CREATED)
def create_ticket(body: TicketCreate, user: CurrentUser, service: TicketServiceDep) -> Ticket:
    return service.create(
        user,
        title=body.title,
        description=body.description,
        repo_connection_id=body.repo_connection_id,
    )


@router.get("", response_model=list[TicketOut])
def list_tickets(
    user: CurrentUser,
    service: TicketServiceDep,
    repo_connection_id: uuid.UUID | None = None,
    status: TicketStatus | None = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> Sequence[Ticket]:
    return service.list(
        user,
        repo_connection_id=repo_connection_id,
        status=status,
        limit=limit,
        offset=offset,
    )


@router.get("/{ticket_id}", response_model=TicketOut)
def get_ticket(ticket_id: uuid.UUID, user: CurrentUser, service: TicketServiceDep) -> Ticket:
    return service.get(user, ticket_id)


@router.patch("/{ticket_id}", response_model=TicketOut)
def update_ticket(
    ticket_id: uuid.UUID,
    body: TicketUpdate,
    user: CurrentUser,
    service: TicketServiceDep,
) -> Ticket:
    return service.update(user, ticket_id, body.model_dump(exclude_unset=True))


@router.delete("/{ticket_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_ticket(ticket_id: uuid.UUID, user: CurrentUser, service: TicketServiceDep) -> Response:
    service.delete(user, ticket_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
