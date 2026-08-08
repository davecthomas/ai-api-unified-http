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

from ai_api_unified.middleware import MiddlewareConfig

# The library's default cost topic, used when its middleware profile does not
# override `emit_cost_topic`.
DEFAULT_COST_TOPIC: Final[str] = "ai_api_unified.observability.cost"

# Escape hatch for a deployment whose topic the service cannot resolve. The
# resolved library setting wins when both are present, because it is the one
# the library actually publishes on.
COST_TOPIC_ENV: Final[str] = "HTTP_COST_TOPIC"

# The middleware profile this service ships. The library reads it from
# AI_MIDDLEWARE_CONFIG_PATH; the service defaults that variable to this file so
# a stock deployment starts with cost emission and PII redaction already on.
MIDDLEWARE_CONFIG_PATH_ENV: Final[str] = "AI_MIDDLEWARE_CONFIG_PATH"
DEFAULT_MIDDLEWARE_CONFIG_PATH: Final[str] = "config/middleware.yaml"

# Bootstrap sink. Expected to become a metrics pipeline, which is a handler
# swap rather than a change to how capture works.
DEFAULT_COST_LOG_PATH: Final[str] = "cost-events.jsonl"
COST_LOG_PATH_ENV: Final[str] = "HTTP_COST_LOG_PATH"

_HANDLER_MARKER: Final[str] = "ai_api_unified_http_cost_sink"

logger: Final[logging.Logger] = logging.getLogger(__name__)


class CostEventNotCapturedError(RuntimeError):
    """Raised at startup when cost events would not be recorded.

    Covers both halves of the failure: nothing listening on the topic, and the
    library not emitting on it in the first place.
    """


def apply_default_middleware_config() -> None:
    """Point the library at this service's middleware profile when unset.

    Called before anything reads the library's configuration. A deployment
    that supplies its own profile keeps it; the default exists so a stock
    install starts with cost emission and PII redaction already on.
    """
    if os.environ.get(MIDDLEWARE_CONFIG_PATH_ENV):
        return
    default: Path = Path(DEFAULT_MIDDLEWARE_CONFIG_PATH)
    if not default.exists():
        logger.warning(
            "%s is unset and %s is missing; the library will run with its "
            "built-in middleware defaults, which do not emit cost events",
            MIDDLEWARE_CONFIG_PATH_ENV,
            default,
        )
        return
    os.environ[MIDDLEWARE_CONFIG_PATH_ENV] = str(default)
    logger.info("middleware profile -> %s", default)


def _resolved_observability_settings() -> Any | None:
    """Return the library's resolved observability settings, or None when off."""
    return MiddlewareConfig().get_observability_settings()


# Attribute names the logging module puts on every record. Taken from an
# instance, because LogRecord.__dict__ on the class holds methods rather than
# the per-record attributes, and filtering against it lets `pathname`,
# `lineno`, `thread`, and the rest through into the sink.
_LOG_RECORD_ATTRIBUTES: Final[frozenset[str]] = frozenset(
    logging.LogRecord("", 0, "", 0, "", None, None).__dict__.keys()
) | {"message", "asctime", "taskName"}


def _structured_fields(record: logging.LogRecord) -> dict[str, Any]:
    """Extract the library's cost fields from a log record.

    The library logs the event as `"%s %s"` with args `(event_name, payload)`,
    so the structured data lives in the args tuple rather than on the record.
    Both shapes are read: the args payload, and any `extra=` attributes, so a
    change on either side still lands in the sink.

    Args:
        record: The cost event record.

    Returns:
        dict[str, Any]: Event name plus every structured field found.
    """
    fields: dict[str, Any] = {}

    args: Any = record.args
    if isinstance(args, (tuple, list)):
        for item in args:
            if isinstance(item, dict):
                fields.update(item)
            elif isinstance(item, str) and "event" not in fields:
                fields["event"] = item
    elif isinstance(args, dict):
        fields.update(args)

    # Anything attached via extra=, which is where a future library version
    # might put fields instead.
    for key, value in record.__dict__.items():
        if key not in _LOG_RECORD_ATTRIBUTES and key not in fields:
            fields[key] = value

    if "event" not in fields:
        fields["event"] = record.getMessage()
    return fields


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
            }
            payload.update(_structured_fields(record))
            with self.path.open("a", encoding="utf-8") as sink:
                sink.write(json.dumps(payload, default=str) + "\n")
        except Exception:  # noqa: BLE001 - logging must never raise into callers
            self.handleError(record)


def cost_topic() -> str:
    """Return the logger name the library emits cost events on.

    Resolved from the library's own middleware profile first, so the service
    listens where the library actually publishes. An explicit `HTTP_COST_TOPIC`
    is the fallback for a deployment whose profile the service cannot read.
    """
    settings = _resolved_observability_settings()
    if settings is not None and settings.emit_cost_topic:
        return str(settings.emit_cost_topic)
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
    """Fail when cost events would not be recorded.

    Two independent things must hold, and checking only one of them is how
    this service shipped a startup gate that passed while recording nothing:

    1. The library must be emitting cost events. `emit_cost` defaults to
       **false**, so a deployment with no middleware profile publishes nothing
       and a handler waits on a silent topic.
    2. Something must be listening on the topic it publishes to.

    Raises:
        CostEventNotCapturedError: When either half fails.
    """
    settings = _resolved_observability_settings()
    if settings is None:
        raise CostEventNotCapturedError(
            f"The library's observability middleware is disabled, so no cost "
            f"events are emitted at all. Point {MIDDLEWARE_CONFIG_PATH_ENV} at "
            f"a profile with an enabled 'observability' entry; this service "
            f"ships one at {DEFAULT_MIDDLEWARE_CONFIG_PATH}."
        )
    if not settings.emit_cost:
        raise CostEventNotCapturedError(
            f"The library's observability middleware is enabled but "
            f"emit_cost is false, which is its default, so no cost events are "
            f"produced. Set 'emit_cost: true' under the observability entry in "
            f"the profile at "
            f"{os.environ.get(MIDDLEWARE_CONFIG_PATH_ENV, DEFAULT_MIDDLEWARE_CONFIG_PATH)}."
        )

    topic: str = cost_topic()
    topic_logger: logging.Logger = logging.getLogger(topic)
    if topic_logger.handlers:
        return
    raise CostEventNotCapturedError(
        f"No handler is attached to the cost topic {topic!r}, so every cost "
        f"event this service produces would be dropped. Set {COST_LOG_PATH_ENV} "
        f"to a writable path."
    )
