from datetime import UTC, datetime, timedelta

import pytest

from app.agents.schemas import AppliedChange, EditOut, Patch
from app.core.exceptions import AppError, ConflictError, NotFoundError, RepositoryAccessError
from app.db.enums import PublicationStatus, RunStatus
from app.db.models import AgentRun, AgentStep
from app.repositories.run_repository import AgentRunRepository
from app.services.publish_service import STALE_CLAIM, PublishService
from tests.factories import create_repo, create_ticket, create_user
from tests.fakes import FakeFileReader, FakeGitHubApp, FakePublisher

NOW = datetime(2026, 11, 1, 12, 0, tzinfo=UTC)
BASE = "c" * 40
LOGIN = "def parse(email):\n    return email\n"


def change(path="src/login.py", action="modify", **kw) -> AppliedChange:
    values = {
        "path": path,
        "action": action,
        "reason": "Whitespace broke parsing.",
        "edits": [EditOut(search="    return email\n", replace="    return email.strip()\n")]
        if action == "modify"
        else [],
        "content": "x = 1\n" if action == "create" else "",
        "diff": "--- a\n",
        "lines_added": 1,
        "lines_removed": 1,
    }
    return AppliedChange(**{**values, **kw})


def make_patch(*changes: AppliedChange) -> Patch:
    changes = changes or (change(),)
    return Patch(
        base_commit_sha=BASE,
        summary="Strip whitespace.",
        changes=list(changes),
        open_questions=[],
        attempts=1,
        files_changed=len(changes),
        lines_added=len(changes),
        lines_removed=len(changes),
    )


@pytest.fixture
def world(session):
    user = create_user(session, "alice")
    repo = create_repo(session, user, "octo/demo")
    ticket = create_ticket(session, user, repo, title="Fix login bug")
    return user, ticket


def add_run(session, ticket, patch="default", status=RunStatus.COMPLETED, **values) -> AgentRun:
    run = AgentRun(ticket_id=ticket.id, status=status, **values)
    session.add(run)
    session.flush()
    session.add(
        AgentStep(
            run_id=run.id,
            agent_name="planner",
            input={},
            output={"subtasks": [{"title": "Fix parser", "details": "Strip it"}]},
        )
    )
    if patch is not None:
        patch = make_patch() if patch == "default" else patch
        session.add(
            AgentStep(
                run_id=run.id, agent_name="coder", input={}, output=patch.model_dump(mode="json")
            )
        )
    session.commit()
    return run


def service(session, publisher=None, files=None, tokens=None, clock=lambda: NOW):
    return PublishService(
        session=session,
        runs=AgentRunRepository(session),
        tokens=tokens or FakeGitHubApp(),
        files=files or FakeFileReader({"src/login.py": LOGIN}),
        publisher=publisher or FakePublisher(),
        clock=clock,
    )


def reload(session, run):
    session.expire_all()
    return session.get(AgentRun, run.id)


# ---- success ------------------------------------------------------------------------


def test_publishing_commits_the_patch_reapplied_to_the_base_commit(session, world):
    user, ticket = world
    run = add_run(session, ticket)
    publisher, files, app = (
        FakePublisher(),
        FakeFileReader({"src/login.py": LOGIN}),
        FakeGitHubApp(),
    )

    result = service(session, publisher, files, app).publish(user, run.id)

    (call,) = publisher.calls
    installation_id = ticket.repo_connection.github_installation.installation_id
    assert call["repo_full_name"] == "octo/demo"
    assert call["token"] == f"token-for-{installation_id}"  # minted for this installation
    assert call["base_commit_sha"] == BASE
    assert call["branch"] == f"copilot/fix-login-bug-{str(run.id)[:8]}"
    (write,) = call["files"]
    assert write.path == "src/login.py"
    assert write.content == "def parse(email):\n    return email.strip()\n"
    assert files.reads == [("octo/demo", BASE, "src/login.py", call["token"])]  # at the base
    assert call["pr_title"] == "Fix login bug"
    assert "Strip whitespace." in call["pr_body"] and "Fix parser" in call["pr_body"]
    assert call["commit_message"].startswith("Fix login bug\n\nStrip whitespace.")

    assert result.publication_status == PublicationStatus.PUBLISHED
    assert result.pull_request_url == "https://github.com/octo/demo/pull/41"
    assert result.pull_request_number == 41 and result.published_at == NOW
    assert result.branch_name == call["branch"] and result.publish_error is None


def test_created_and_deleted_files_are_written_without_reading_the_repository(session, world):
    user, ticket = world
    patch = make_patch(change("src/new.py", "create"), change("src/old.py", "delete", edits=[]))
    run = add_run(session, ticket, patch)
    publisher, files = FakePublisher(), FakeFileReader()

    service(session, publisher, files).publish(user, run.id)

    writes = {w.path: w.content for w in publisher.calls[0]["files"]}
    assert writes == {"src/new.py": "x = 1\n", "src/old.py": None}
    assert files.reads == []  # nothing to read: no file is being modified


def test_publishing_twice_returns_the_same_pull_request(session, world):
    user, ticket = world
    run = add_run(session, ticket)
    publisher = FakePublisher()
    svc = service(session, publisher)

    first = svc.publish(user, run.id)
    second = svc.publish(user, run.id)

    assert len(publisher.calls) == 1
    assert second.pull_request_url == first.pull_request_url


# ---- who and when -------------------------------------------------------------------


def test_another_users_run_is_not_found(session, world):
    _, ticket = world
    run = add_run(session, ticket)
    stranger = create_user(session, "mallory")
    publisher = FakePublisher()
    with pytest.raises(NotFoundError):
        service(session, publisher).publish(stranger, run.id)
    assert publisher.calls == []


@pytest.mark.parametrize("status", [RunStatus.PENDING, RunStatus.RUNNING, RunStatus.FAILED])
def test_only_completed_runs_can_be_published(session, world, status):
    user, ticket = world
    run = add_run(session, ticket, status=status)
    with pytest.raises(ConflictError, match="completed"):
        service(session).publish(user, run.id)


def test_a_run_without_changes_cannot_be_published(session, world):
    user, ticket = world
    empty = make_patch()
    empty.changes.clear()
    for patch in (empty, None):
        run = add_run(session, ticket, patch, status=RunStatus.COMPLETED)
        with pytest.raises(ConflictError, match="no changes"):
            service(session).publish(user, run.id)


# ---- failure and retry --------------------------------------------------------------


def test_a_failed_publish_is_recorded_with_its_reason_and_can_be_retried(session, world):
    user, ticket = world
    run = add_run(session, ticket)
    failing = FakePublisher(error=RepositoryAccessError("GitHub refused to let the app write"))

    with pytest.raises(RepositoryAccessError):
        service(session, failing).publish(user, run.id)

    failed = reload(session, run)
    assert failed.publication_status == PublicationStatus.FAILED
    assert failed.publish_error == "GitHub refused to let the app write"
    assert failed.pull_request_url is None

    result = service(session, FakePublisher()).publish(user, run.id)  # permissions fixed: retry
    assert result.publication_status == PublicationStatus.PUBLISHED
    assert result.publish_error is None


def test_unexpected_errors_are_not_leaked_into_the_stored_error(session, world):
    user, ticket = world
    run = add_run(session, ticket)
    broken = FakePublisher(error=RuntimeError("token ghs_secret in traceback"))

    with pytest.raises(RuntimeError):
        service(session, broken).publish(user, run.id)

    error = reload(session, run).publish_error
    assert error == "Publishing failed unexpectedly." and "ghs_secret" not in error


def test_a_patch_that_no_longer_applies_is_a_clear_failure_not_a_bad_commit(session, world):
    user, ticket = world
    run = add_run(session, ticket)
    publisher = FakePublisher()
    changed = FakeFileReader({"src/login.py": "def parse(email):\n    return None\n"})

    with pytest.raises(AppError, match=r"no longer applies to src/login\.py"):
        service(session, publisher, changed).publish(user, run.id)

    assert publisher.calls == []
    assert reload(session, run).publication_status == PublicationStatus.FAILED


def test_a_file_missing_at_the_base_commit_is_a_clear_failure(session, world):
    user, ticket = world
    run = add_run(session, ticket)
    with pytest.raises(AppError, match="no longer exists at the base commit"):
        service(session, files=FakeFileReader({})).publish(user, run.id)


def test_token_failures_are_recorded(session, world):
    user, ticket = world
    run = add_run(session, ticket)
    app = FakeGitHubApp()
    app.error = RepositoryAccessError("GitHub no longer lets this app access the repository")
    with pytest.raises(RepositoryAccessError):
        service(session, tokens=app).publish(user, run.id)
    assert "no longer lets this app" in reload(session, run).publish_error


# ---- two clicks ---------------------------------------------------------------------


def test_a_publish_already_in_progress_is_refused(session, world):
    user, ticket = world
    run = add_run(
        session, ticket,
        publication_status=PublicationStatus.PUBLISHING,
        publish_started_at=NOW - timedelta(seconds=30),
    )  # fmt: skip
    publisher = FakePublisher()
    with pytest.raises(ConflictError, match="already being published"):
        service(session, publisher).publish(user, run.id)
    assert publisher.calls == []


def test_a_crashed_attempt_is_taken_over_after_the_stale_window(session, world):
    user, ticket = world
    run = add_run(
        session, ticket,
        publication_status=PublicationStatus.PUBLISHING,
        publish_started_at=NOW - STALE_CLAIM - timedelta(seconds=1),
    )  # fmt: skip
    result = service(session).publish(user, run.id)
    assert result.publication_status == PublicationStatus.PUBLISHED


def test_only_one_claim_wins(session, world):
    _, ticket = world
    run = add_run(session, ticket)
    runs = AgentRunRepository(session)
    assert runs.claim_publication(run.id, NOW, NOW - STALE_CLAIM) is True
    assert runs.claim_publication(run.id, NOW, NOW - STALE_CLAIM) is False
