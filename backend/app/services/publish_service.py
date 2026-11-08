"""Publishing a run's patch as a draft pull request.

An explicit action by the ticket's owner (never automatic): it writes to their repository.
The patch is re-applied to the exact base commit it was checked against, committed as one
commit on a new branch, and opened as a DRAFT pull request. The default branch is never
touched. Publishing the same run twice returns the same pull request.
"""

import logging
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy.orm import Session

from app.agents.patching import apply_edits
from app.agents.schemas import Patch
from app.core.exceptions import AppError, ConflictError, NotFoundError
from app.db.enums import PublicationStatus, RunStatus
from app.db.models import AgentRun, User
from app.integrations.github_app import InstallationTokenSource
from app.integrations.github_files import GitHubFileReader
from app.integrations.github_publish import FileWrite, PullRequestPublisher
from app.repositories.run_repository import AgentRunRepository
from app.services import pr_text

logger = logging.getLogger(__name__)

# A claim older than this belongs to an attempt that crashed and may be taken over.
STALE_CLAIM = timedelta(minutes=10)


def _now() -> datetime:
    return datetime.now(UTC)


def latest_patch(run: AgentRun) -> tuple[Patch | None, list[tuple[str, str]]]:
    """The Coder's patch and the Planner's sub-tasks, from the run's recorded steps."""
    patch: Patch | None = None
    subtasks: list[tuple[str, str]] = []
    for step in run.steps:  # ordered by creation time
        if step.agent_name == "coder" and step.output:
            patch = Patch.model_validate(step.output)
        elif step.agent_name == "planner" and step.output:
            subtasks = [(s["title"], s["details"]) for s in step.output.get("subtasks", [])]
    return patch, subtasks


class PublishService:
    def __init__(
        self,
        *,
        session: Session,
        runs: AgentRunRepository,
        tokens: InstallationTokenSource,
        files: GitHubFileReader,
        publisher: PullRequestPublisher,
        clock: Callable[[], datetime] = _now,
    ) -> None:
        self._session = session
        self._runs = runs
        self._tokens = tokens
        self._files = files
        self._publisher = publisher
        self._clock = clock

    def publish(self, user: User, run_id: UUID) -> AgentRun:
        run = self._runs.get_owned_with_steps(run_id, user.id)
        if run is None:
            raise NotFoundError("Run")
        if run.publication_status == PublicationStatus.PUBLISHED:
            return run  # already done: idempotent
        if run.status != RunStatus.COMPLETED:
            raise ConflictError("Only a completed run can be published")
        patch, subtasks = latest_patch(run)
        if patch is None or not patch.changes:
            raise ConflictError("This run produced no changes to publish")

        now = self._clock()
        if not self._runs.claim_publication(run_id, now, now - STALE_CLAIM):
            self._session.rollback()
            raise ConflictError("This run is already being published")
        self._session.commit()

        try:
            result = self._publish(run, patch, subtasks)
        except Exception as exc:
            self._session.rollback()
            message = str(exc) if isinstance(exc, AppError) else "Publishing failed unexpectedly."
            self._runs.mark_publication_failed(run_id, message)
            self._session.commit()
            if not isinstance(exc, AppError):
                logger.exception("publishing crashed", extra={"run_id": str(run_id)})
            raise
        self._runs.mark_published(
            run_id,
            branch=result.branch,
            number=result.number,
            url=result.url,
            now=self._clock(),
        )
        self._session.commit()
        logger.info("run published", extra={"run_id": str(run_id), "pr": result.number})
        return self._runs.get_owned_with_steps(run_id, user.id) or run

    def _publish(self, run: AgentRun, patch: Patch, subtasks: list[tuple[str, str]]):
        ticket = run.ticket
        repo = ticket.repo_connection
        repo_name = repo.repo_full_name
        token = self._tokens.installation_token(repo.github_installation.installation_id)

        writes: list[FileWrite] = []
        for change in patch.changes:
            if change.action == "create":
                writes.append(FileWrite(change.path, change.content))
            elif change.action == "delete":
                writes.append(FileWrite(change.path, None))
            else:
                base = self._files.read_text(repo_name, patch.base_commit_sha, change.path, token)
                if base is None:
                    raise AppError(f"{change.path} no longer exists at the base commit")
                try:
                    writes.append(FileWrite(change.path, apply_edits(base, change.edits)))
                except ValueError as exc:
                    raise AppError(f"The patch no longer applies to {change.path}: {exc}") from exc

        return self._publisher.publish(
            repo_full_name=repo_name,
            token=token,
            base_commit_sha=patch.base_commit_sha,
            branch=pr_text.branch_name(ticket.title, run.id),
            files=writes,
            commit_message=pr_text.commit_message(ticket.title, patch.summary, run.id),
            pr_title=pr_text.pr_title(ticket.title),
            pr_body=pr_text.pr_body(
                ticket_title=ticket.title, patch=patch, subtasks=subtasks, run_id=run.id
            ),
        )
