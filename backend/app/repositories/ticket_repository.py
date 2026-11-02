from collections.abc import Sequence
from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.db.enums import TicketStatus
from app.db.models import Ticket


class TicketRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def add(self, ticket: Ticket) -> Ticket:
        self._session.add(ticket)
        self._session.flush()
        return ticket

    def get_owned(self, ticket_id: UUID, user_id: UUID) -> Ticket | None:
        return self._session.scalar(
            select(Ticket).where(Ticket.id == ticket_id, Ticket.user_id == user_id)
        )

    def list_owned(
        self,
        user_id: UUID,
        *,
        repo_connection_id: UUID | None,
        status: TicketStatus | None,
        limit: int,
        offset: int,
    ) -> Sequence[Ticket]:
        query = select(Ticket).where(Ticket.user_id == user_id)
        if repo_connection_id is not None:
            query = query.where(Ticket.repo_connection_id == repo_connection_id)
        if status is not None:
            query = query.where(Ticket.status == status.value)
        # `id` breaks ties so pages are stable when timestamps collide.
        query = query.order_by(Ticket.created_at.desc(), Ticket.id).limit(limit).offset(offset)
        return self._session.scalars(query).all()

    def delete(self, ticket: Ticket) -> None:
        self._session.delete(ticket)

    def get(self, ticket_id: UUID) -> Ticket | None:
        return self._session.get(Ticket, ticket_id)

    def set_status(self, ticket_ids: list[UUID], status: TicketStatus) -> None:
        if ticket_ids:
            self._session.execute(
                update(Ticket).where(Ticket.id.in_(ticket_ids)).values(status=status)
            )
