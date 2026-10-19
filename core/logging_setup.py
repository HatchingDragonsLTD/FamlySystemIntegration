"""Application logging for the entry points (the gunicorn web app and the workers).

Without this the root logger sits at WARNING, so every `logger.info(...)` in the
application is dropped: gunicorn only configures its OWN loggers. One line per
event on stderr (which systemd sends to journald), INFO by default, `LOG_LEVEL`
to override. Log lines carry ids and codes only, never personal data.
"""

import logging
import os
import sys

FORMAT = "%(asctime)s %(levelname)s %(name)s %(message)s"


def configure_logging(stream=None, level: str | int | None = None, force: bool = False) -> None:
    """Send application logs to `stream` (default stderr) at INFO.

    Idempotent: when the root logger already has a handler it only sets the level,
    unless `force` replaces the handlers.
    """
    wanted = level if level is not None else os.environ.get("LOG_LEVEL", "").strip() or "INFO"
    if isinstance(wanted, str):
        wanted = getattr(logging, wanted.upper(), logging.INFO)
    root = logging.getLogger()
    if force:
        for handler in list(root.handlers):
            root.removeHandler(handler)
    if not root.handlers:
        handler = logging.StreamHandler(stream or sys.stderr)
        handler.setFormatter(logging.Formatter(FORMAT))
        root.addHandler(handler)
    root.setLevel(wanted)
