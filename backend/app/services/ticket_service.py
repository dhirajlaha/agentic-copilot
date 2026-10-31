from collections.abc import Mapping, Sequence
from typing import Any
from uuid import UUID

from sqlalchemy.orm import Session

from app.core.exceptions import NotFoundError
from app.db.enums import TicketStatus
from app.db.models import Ticket, User
from app.repositories.repo_connection_repository import RepoConnectionRepository
from app.repositories.ticket_repository import TicketRepository

# The only fields a client may change. Status is owned by the orchestrator.
UPDATABLE_FIELDS = frozenset({"title", "description"})


class TicketService:
    """Ticket use cases. Everything is scoped to the acting user (multi-tenancy); a
    ticket or repo owned by someone else is reported as not found."""

    def __init__(
        self,
        session: Session,
        tickets: TicketRepository,
        repo_connections: RepoConnectionRepository,
    ) -> None:
        self._session = session
        self._tickets = tickets
        self._repo_connections = repo_connections

    def create(
        self, user: User, *, title: str, description: str, repo_connection_id: UUID
    ) -> Ticket:
        repo = self._repo_connections.get_owned(repo_connection_id, user.id)
        if repo is None:
            raise NotFoundError("Repo connection")
        ticket = self._tickets.add(
            Ticket(
                user_id=user.id,
                repo_connection_id=repo.id,
                title=title,
                description=description,
            )
        )
        self._session.commit()
        return ticket

    def get(self, user: User, ticket_id: UUID) -> Ticket:
        ticket = self._tickets.get_owned(ticket_id, user.id)
        if ticket is None:
            raise NotFoundError("Ticket")
        return ticket

    def list(
        self,
        user: User,
        *,
        repo_connection_id: UUID | None,
        status: TicketStatus | None,
        limit: int,
        offset: int,
    ) -> Sequence[Ticket]:
        return self._tickets.list_owned(
            user.id,
            repo_connection_id=repo_connection_id,
            status=status,
            limit=limit,
            offset=offset,
        )

    def update(self, user: User, ticket_id: UUID, changes: Mapping[str, Any]) -> Ticket:
        unknown = set(changes) - UPDATABLE_FIELDS
        if unknown:
            raise ValueError(f"Fields not updatable: {sorted(unknown)}")
        ticket = self.get(user, ticket_id)
        for field, value in changes.items():
            setattr(ticket, field, value)
        self._session.commit()
        return ticket

    def delete(self, user: User, ticket_id: UUID) -> None:
        self._tickets.delete(self.get(user, ticket_id))
        self._session.commit()
