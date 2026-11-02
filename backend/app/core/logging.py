"""Process-wide logging setup, shared by the API and the worker."""

import logging
import os


def configure_logging() -> None:
    """Plain, timestamped log lines on stderr. Level from LOG_LEVEL (default INFO).

    Never log secrets: tokens, API keys, cookies and authorization codes are kept out of
    log messages throughout the codebase.
    """
    level = os.environ.get("LOG_LEVEL", "INFO").upper()
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    # The HTTP client libraries log every request at INFO; keep them quieter.
    for noisy in ("httpx", "httpx2", "httpcore"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
