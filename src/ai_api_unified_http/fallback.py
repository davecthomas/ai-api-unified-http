# src/ai_api_unified_http/fallback.py

"""
Which model served a request, when a fallback chain is in play.

A pooled completions client with a chain (`COMPLETIONS_FALLBACKS`) may answer
a request on a different engine and model than the caller asked for. The
response has to say which one did, and cost has to be priced at it.

The library stamps the serving route on conversation and structured-output
results. For a plain text prompt it offers only `last_route` on the client,
and that is per-client state: the service shares one client across concurrent
requests, so `last_route` can describe someone else's call by the time it is
read. What is per-request is the library's `served_by_fallback` log record,
emitted inside the call that moved. A logging filter copies it into a
collector held in a ContextVar, which each request sets for itself.

A ContextVar is what makes this safe under concurrency. Every request runs in
its own task with its own context, and Starlette carries that context onto
the worker threads it uses for a stream, so a record lands only in the
collector of the request whose call logged it. The collector is a mutable
object rather than a value, so a write on a worker thread is visible to the
route that set it.

No record means the requested primary served the call.
"""

import logging
import os
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Final

from ai_api_unified.completions.ai_fallback_completions import (
    FALLBACK_EVENT_SERVED,
)

# The library's settings for the chain and the reasons that move a request.
# Names match the library's so one .env serves both (docs/requirements.md).
COMPLETIONS_FALLBACKS_ENV: Final[str] = "COMPLETIONS_FALLBACKS"
COMPLETIONS_FALLBACK_ON_ENV: Final[str] = "COMPLETIONS_FALLBACK_ON"

# Logger the library's fallback wrapper writes its events to.
FALLBACK_LOGGER: Final[str] = "ai_api_unified.completions.ai_fallback_completions"

# Label the library uses for a candidate with no model named.
_DEFAULT_MODEL_LABEL: Final[str] = "<default>"


@dataclass
class ServedRoute:
    """Where a request was answered.

    Attributes:
        engine: Engine token that served the call.
        model: Model that served it, or None for that engine's default.
        served_by_fallback: True when a fallback answered for a failed primary.
    """

    engine: str
    model: str | None
    served_by_fallback: bool = False


@dataclass
class _RouteCollector:
    """Holds the fallback that served the current request, if one did."""

    served: ServedRoute | None = field(default=None)


_collector: ContextVar[_RouteCollector | None] = ContextVar(
    "ai_api_unified_http_fallback_route", default=None
)


def _parse_label(label: str) -> tuple[str, str | None]:
    """Split the library's `engine:model` label on its first colon."""
    engine, _, model = label.partition(":")
    if not model or model == _DEFAULT_MODEL_LABEL:
        return engine, None
    return engine, model


class _ServedByFallbackFilter(logging.Filter):
    """Copy the library's served-by-fallback record into the request's collector.

    A filter rather than a handler so it sees every record on the logger
    whatever the handler setup, and it never drops one: it only reads.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        if getattr(record, "ai_fallback_event", None) != FALLBACK_EVENT_SERVED:
            return True
        collector: _RouteCollector | None = _collector.get()
        label: object = getattr(record, "fallback_to", None)
        if collector is not None and isinstance(label, str):
            engine, model = _parse_label(label)
            collector.served = ServedRoute(
                engine=engine, model=model, served_by_fallback=True
            )
        return True


_FILTER_MARKER: Final[str] = "_ai_api_unified_http_route_filter"


def install_route_filter() -> None:
    """Attach the filter to the library's fallback logger, once per process."""
    fallback_logger: logging.Logger = logging.getLogger(FALLBACK_LOGGER)
    if any(getattr(f, _FILTER_MARKER, False) for f in fallback_logger.filters):
        return
    # A logger drops a record below its level before any filter runs, so a
    # LOG_LEVEL of ERROR would hide the WARNING this filter depends on.
    if not fallback_logger.isEnabledFor(logging.WARNING):
        fallback_logger.setLevel(logging.WARNING)
    route_filter = _ServedByFallbackFilter()
    setattr(route_filter, _FILTER_MARKER, True)
    fallback_logger.addFilter(route_filter)


def start_route_tracking() -> _RouteCollector:
    """Give the current request its own collector.

    Returns:
        _RouteCollector: Pass it to `served_route` after the call.
    """
    install_route_filter()
    collector = _RouteCollector()
    _collector.set(collector)
    return collector


def served_route(
    collector: _RouteCollector, engine: str, model: str | None
) -> ServedRoute:
    """Return where the request was served.

    Args:
        collector: The collector `start_route_tracking` returned.
        engine: The requested engine, reported when the primary served.
        model: The requested model.

    Returns:
        ServedRoute: The fallback that served, or the requested primary.
    """
    if collector.served is not None:
        return collector.served
    return ServedRoute(engine=engine, model=model)


class FallbackMisconfiguredError(RuntimeError):
    """Raised at startup when the fallback settings cannot be used."""


def verify_fallback_config() -> list[str]:
    """Check the fallback settings at startup and log the chain.

    The library validates the chain only when it builds a client, which here
    is the first request per engine and model. A typo would then surface as
    failed requests, and an engine that is never requested would hide it
    until an outage. Checking at startup refuses the deployment instead.

    Returns:
        list[str]: The chain as `engine:model` labels; empty when unset.

    Raises:
        FallbackMisconfiguredError: When a candidate names an unknown engine
            or `COMPLETIONS_FALLBACK_ON` names an unknown reason.
    """
    # Imported here so the module stays importable for its request-path
    # helpers without pulling the registry in.
    from ai_api_unified.ai_provider_exceptions import AiProviderConfigurationError
    from ai_api_unified.ai_provider_registry import (
        AI_PROVIDER_CAPABILITY_COMPLETIONS,
        get_ai_provider_spec,
    )
    from ai_api_unified.completions.ai_fallback_completions import (
        DEFAULT_FALLBACK_REASONS,
        parse_fallback_candidates,
        parse_fallback_reasons,
    )

    logger: logging.Logger = logging.getLogger(__name__)
    candidates = parse_fallback_candidates(
        os.environ.get(COMPLETIONS_FALLBACKS_ENV, "")
    )
    for candidate in candidates:
        try:
            get_ai_provider_spec(AI_PROVIDER_CAPABILITY_COMPLETIONS, candidate.engine)
        except AiProviderConfigurationError as error:
            raise FallbackMisconfiguredError(
                f"{COMPLETIONS_FALLBACKS_ENV} names {candidate.engine!r}, which "
                f"is not a completions engine: {error}"
            ) from error
    try:
        raw_reasons: str = os.environ.get(COMPLETIONS_FALLBACK_ON_ENV, "").strip()
        reasons = (
            parse_fallback_reasons(raw_reasons)
            if raw_reasons
            else DEFAULT_FALLBACK_REASONS
        )
    except ValueError as error:
        raise FallbackMisconfiguredError(
            f"{COMPLETIONS_FALLBACK_ON_ENV}: {error}"
        ) from error

    labels: list[str] = [candidate.label for candidate in candidates]
    if labels:
        logger.info(
            "model fallback: %s, on %s",
            " -> ".join(labels),
            ",".join(sorted(reason.value for reason in reasons)),
        )
    else:
        logger.info("model fallback: off (%s unset)", COMPLETIONS_FALLBACKS_ENV)
    return labels
