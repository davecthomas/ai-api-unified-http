# src/ai_api_unified_http/cost.py

"""
Cost-event capture.

The library emits one structured record per call on the logger named by its
`emit_cost_topic` setting, carrying the cost attribution for that call. The
service neither computes nor duplicates that accounting; its only obligation
is that those records reach somewhere durable, because a dropped cost event is
spend with no record of who incurred it.

Capture is therefore a logging-handler concern rather than a code path through
request handling: nothing is threaded through the routes, and adding an
endpoint cannot forget to account for its own cost.

Startup attaches the handler and then verifies the topic has one. A
deployment that cannot record spend refuses to start rather than serving
traffic that quietly loses cost events (docs/requirements.md, hard
requirement 3).
"""

import json
import logging
import os
from pathlib import Path
from typing import Any, Final

# The library's default cost topic. Deployments that retune the library's
# emit_cost_topic set HTTP_COST_TOPIC to match, or capture silently misses.
DEFAULT_COST_TOPIC: Final[str] = "ai_api_unified.observability.cost"
COST_TOPIC_ENV: Final[str] = "HTTP_COST_TOPIC"

# Bootstrap sink. Expected to become a metrics pipeline, which is a handler
# swap rather than a change to how capture works.
DEFAULT_COST_LOG_PATH: Final[str] = "cost-events.jsonl"
COST_LOG_PATH_ENV: Final[str] = "HTTP_COST_LOG_PATH"

_HANDLER_MARKER: Final[str] = "ai_api_unified_http_cost_sink"

logger: Final[logging.Logger] = logging.getLogger(__name__)


class CostEventNotCapturedError(RuntimeError):
    """Raised at startup when the cost topic has no handler attached."""


class JsonLinesCostHandler(logging.Handler):
    """Write each cost event to a JSON Lines file.

    One JSON object per line, so the file is appendable, greppable, and
    loadable by the metrics pipeline expected to replace it.
    """

    def __init__(self, path: Path) -> None:
        """Open the sink.

        Args:
            path: Destination file. Parent directories are created.
        """
        super().__init__()
        self.path: Path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # Marks this handler as ours so repeat attachment stays idempotent.
        self.name = _HANDLER_MARKER

    def emit(self, record: logging.LogRecord) -> None:
        """Append one cost event as a JSON object.

        A failing sink must not take down the request that produced the event,
        so write errors route through the logging error path.
        """
        try:
            payload: dict[str, Any] = {
                "timestamp": record.created,
                "level": record.levelname,
                "logger": record.name,
                "message": record.getMessage(),
            }
            # The library attaches its structured fields to the record; carry
            # every non-standard attribute through rather than naming them,
            # so new library fields survive without a change here.
            standard: frozenset[str] = frozenset(logging.LogRecord.__dict__.keys())
            for key, value in record.__dict__.items():
                if key not in standard and key not in payload:
                    payload[key] = value
            with self.path.open("a", encoding="utf-8") as sink:
                sink.write(json.dumps(payload, default=str) + "\n")
        except Exception:  # noqa: BLE001 - logging must never raise into callers
            self.handleError(record)


def cost_topic() -> str:
    """Return the logger name the library emits cost events on."""
    return os.environ.get(COST_TOPIC_ENV, DEFAULT_COST_TOPIC)


def attach_cost_handler() -> logging.Handler | None:
    """Attach the JSON Lines sink to the cost topic.

    Idempotent: a second call with a sink already attached returns the existing
    handler rather than doubling every event.

    Returns:
        logging.Handler | None: The attached handler, or the existing one.
    """
    topic_logger: logging.Logger = logging.getLogger(cost_topic())
    for existing in topic_logger.handlers:
        if getattr(existing, "name", None) == _HANDLER_MARKER:
            return existing

    path: Path = Path(os.environ.get(COST_LOG_PATH_ENV, DEFAULT_COST_LOG_PATH))
    handler: JsonLinesCostHandler = JsonLinesCostHandler(path)
    topic_logger.addHandler(handler)
    # Cost events are records the service must not drop, so the topic opts out
    # of whatever level the root logger is set to.
    topic_logger.setLevel(logging.INFO)
    logger.info("cost events -> %s (topic %s)", path, cost_topic())
    return handler


def verify_cost_capture() -> None:
    """Fail when nothing is listening on the cost topic.

    Raises:
        CostEventNotCapturedError: When the topic has no handler, so every
            cost event the service produces would be discarded.
    """
    topic: str = cost_topic()
    topic_logger: logging.Logger = logging.getLogger(topic)
    if topic_logger.handlers:
        return
    raise CostEventNotCapturedError(
        f"No handler is attached to the cost topic {topic!r}, so every cost "
        f"event this service produces would be dropped. Set {COST_LOG_PATH_ENV} "
        f"to a writable path, or {COST_TOPIC_ENV} if the library's "
        f"emit_cost_topic was retuned."
    )
