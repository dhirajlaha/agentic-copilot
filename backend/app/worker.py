"""The run worker: a separate process that executes queued runs.

    uv run python -m app.worker

Run as many as you like; Postgres row locking guarantees each run is claimed once.
Stops gracefully on Ctrl+C / SIGTERM: it finishes the current run, then exits.
"""

import logging
import signal
import threading

import httpx

from app.agents.orchestrator import get_orchestrator
from app.core.config import get_worker_settings
from app.core.logging import configure_logging
from app.db.session import get_session_factory
from app.integrations.github_repos import GitHubRepoFiles
from app.services.run_executor import RunExecutor

logger = logging.getLogger("app.worker")


def main() -> None:
    configure_logging()
    settings = get_worker_settings()
    stop = threading.Event()

    def request_stop(signum: int, _frame: object) -> None:
        logger.info(
            "received %s, finishing the current run then stopping", signal.Signals(signum).name
        )
        stop.set()

    signal.signal(signal.SIGINT, request_stop)
    signal.signal(signal.SIGTERM, request_stop)

    # Fail fast on missing configuration (database, LLM) before taking any work.
    graph = get_orchestrator()
    with httpx.Client(timeout=httpx.Timeout(15.0), follow_redirects=False) as http:
        executor = RunExecutor(
            session_factory=get_session_factory(),
            graph=graph,
            repo_files=GitHubRepoFiles(http),
            lease_seconds=settings.run_lease_seconds,
        )
        logger.info("worker started (poll every %ss)", settings.worker_poll_interval_seconds)
        while not stop.is_set():
            executor.reap_expired()
            if executor.run_next() is None:
                stop.wait(settings.worker_poll_interval_seconds)
    logger.info("worker stopped")


if __name__ == "__main__":
    main()
