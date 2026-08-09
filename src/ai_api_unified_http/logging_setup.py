# src/ai_api_unified_http/logging_setup.py

"""
Service logging configuration.

Without this the service configures no logging at all, so every `logger.info`
in the codebase — the cost sink's destination, the middleware profile in use,
the auth-disabled warning — goes nowhere, and only uvicorn's own access log
appears. Operators reading a container log would see requests arriving and
nothing about what the service decided.

`LOG_LEVEL` sets the level, matching the variable name `env_template` already
documented for the library's observability output.

Two loggers are deliberately exempt from the configured level:

- The cost topic stays at INFO regardless, because a deployment running at
  WARNING must not thereby drop every cost event. `cost.py` owns that.
- Library provider chatter stays at WARNING unless the level is explicitly
  DEBUG, since the SDKs log per-request detail that buries the service's own
  lines.
"""

import logging
import os
import sys
from typing import Final

LOG_LEVEL_ENV: Final[str] = "LOG_LEVEL"
DEFAULT_LOG_LEVEL: Final[str] = "INFO"

# Third-party loggers that are noisy at INFO. Raised to WARNING unless the
# operator asked for DEBUG, in which case they asked for the noise too.
_NOISY_LOGGERS: Final[tuple[str, ...]] = (
    "httpx",
    "httpcore",
    "anthropic",
    "openai",
    "google_genai",
    "urllib3",
)

_LOG_FORMAT: Final[str] = "%(asctime)s %(levelname)-8s %(name)s: %(message)s"


def resolve_log_level() -> int:
    """Return the configured level, falling back to INFO on an unknown name.

    Returns:
        int: A logging level constant. An unparseable `LOG_LEVEL` falls back
            rather than raising, because a typo in one variable should not
            stop the service from starting.
    """
    raw: str = os.environ.get(LOG_LEVEL_ENV, DEFAULT_LOG_LEVEL).strip().upper()
    resolved: int | str = logging.getLevelName(raw or DEFAULT_LOG_LEVEL)
    if isinstance(resolved, int):
        return resolved
    return logging.INFO


def configure_logging() -> int:
    """Install a stream handler on the root logger and quiet the noisy ones.

    Idempotent: a second call replaces nothing, so reload-driven restarts do
    not stack handlers and double every line.

    Returns:
        int: The level that was applied.
    """
    level: int = resolve_log_level()
    root: logging.Logger = logging.getLogger()

    already_ours: bool = any(
        getattr(h, "name", None) == __name__ for h in root.handlers
    )
    # uvicorn installs a root handler of its own before the app starts. Adding
    # a second one prints every line twice, so the existing handler is adopted
    # and only the level is applied. Raising the level is the part that
    # matters; the format is uvicorn's problem when uvicorn is hosting.
    if not already_ours and not root.handlers:
        handler: logging.StreamHandler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(logging.Formatter(_LOG_FORMAT))
        handler.name = __name__
        root.addHandler(handler)
    root.setLevel(level)

    if level > logging.DEBUG:
        for name in _NOISY_LOGGERS:
            logging.getLogger(name).setLevel(logging.WARNING)

    return level
