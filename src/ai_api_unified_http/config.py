# src/ai_api_unified_http/config.py

"""
Environment file loading for the service's own settings.

The split is easy to get wrong, so it is worth stating exactly.

The **library** reads `.env` for its settings on its own: `EnvSettings` is a
pydantic-settings model declared with `env_file=".env"`, so provider keys and
engine defaults reach it without help. It populates that model, though — not
`os.environ`.

The service's own variables are not in that model. `HTTP_API_KEYS`,
`HTTP_COST_LOG_PATH`, `HTTP_CORS_ORIGINS`, and `LOG_LEVEL` are read straight
from `os.environ`, so a `.env` holding them is inert until something loads it.
Put `HTTP_API_KEYS` in `.env` without this module and the service starts with
no keys configured at all.

Real environment variables always win over the file. A deployment injects
configuration through its own environment, and a stale `.env` left in an image
must never quietly override it.
"""

import logging
import os
from pathlib import Path
from typing import Final

from dotenv import dotenv_values

# Override the file location when the service runs from somewhere other than
# the repo root.
ENV_FILE_ENV: Final[str] = "HTTP_ENV_FILE"
DEFAULT_ENV_FILE: Final[str] = ".env"

logger: Final[logging.Logger] = logging.getLogger(__name__)


def env_file_path() -> Path:
    """Return the configured env file path."""
    return Path(os.environ.get(ENV_FILE_ENV, DEFAULT_ENV_FILE))


def load_env_file() -> int:
    """Load the env file into `os.environ`, without overriding what is set.

    Returns:
        int: How many variables were applied. Zero when the file is absent,
            which is normal for a deployment that injects its own environment.
    """
    path: Path = env_file_path()
    if not path.is_file():
        logger.info("no env file at %s; using the process environment as-is", path)
        return 0

    applied: int = 0
    for key, value in dotenv_values(path).items():
        if value is None:
            continue
        # A real environment variable is deliberate; a file value is a default.
        if key in os.environ:
            continue
        os.environ[key] = value
        applied += 1

    logger.info("loaded %s variables from %s", applied, path)
    return applied
