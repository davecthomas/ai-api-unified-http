# src/ai_api_unified_http/fallback.py

"""
Which model served a request, when a fallback chain is in play.

A pooled completions client with a chain (`completions.fallbacks` in
models.json) may answer a request on a different engine and model than the
caller asked for: the requested model failed, or it is resting after a recent
failure and the library started the request on the next candidate without
asking it. The response has to say which one answered, and cost has to be
priced at it.

The library says so per call, which is what a service sharing one client
across concurrent requests needs; `client.last_route` describes whichever
call finished last. Conversation and structured results carry the serving
engine and model on the result. A buffered text prompt is sent with
`asend_prompt_with_result`, whose envelope carries the same. A stream has no
envelope, so the service opens the library's `request_cost_scope` around it:
the chain joins that scope, each provider call made for the stream lands on
it as a line item, and the report that closes the scope names the call whose
answer was returned.

Before library 3.7.0 the service read the chain's `served_by_fallback` log
record instead. That record is written only when a candidate fails during
the call, so a request that started on a fallback because the primary was
resting would have been reported as served by the primary.
"""

import logging
from dataclasses import dataclass
from typing import Any, Final

from ai_api_unified import AiFallbackCompletions, AIRequestCostReport
from ai_api_unified.util.env_settings import EnvSettings

# The library's settings keys for the chain and the reasons that move a
# request. Since library 3.2.0 both live in models.json, as
# `completions.fallbacks` and `completions.fallback_on`; these environment
# keys still win over the file, with a one-time deprecation warning from the
# library naming the field to move to.
COMPLETIONS_FALLBACKS_ENV: Final[str] = "COMPLETIONS_FALLBACKS"
COMPLETIONS_FALLBACK_ON_ENV: Final[str] = "COMPLETIONS_FALLBACK_ON"


@dataclass
class ServedRoute:
    """Where a request was answered.

    Attributes:
        engine: Engine token that served the call.
        model: Model that served it, or None for that engine's default.
        served_by_fallback: True when a candidate other than the requested
            primary answered, whether because the primary failed or because
            it was resting after a recent failure.
    """

    engine: str
    model: str | None
    served_by_fallback: bool = False


def served_route(
    client: Any,
    engine: str,
    model: str | None,
    served_engine: str | None,
    served_model: str | None,
) -> ServedRoute:
    """Decide which route served a call from what the library reported.

    Args:
        client: The pooled client the route called.
        engine: The requested engine.
        model: The requested model, or None for the engine default.
        served_engine: Engine token the library says answered, or None.
        served_model: Model the library says answered, or None.

    Returns:
        ServedRoute: The requested engine and model when the primary served,
            else the fallback that did. A plain client, one with no chain,
            can only have served the call itself, whatever it reports; so
            can a chain that reported nothing.
    """
    if not served_engine or not isinstance(client, AiFallbackCompletions):
        return ServedRoute(engine=engine, model=model)
    # The chain's primary resolves the engine default, so the comparison is
    # against the resolved model rather than the request's None.
    primary_model: str | None = getattr(client.primary, "model_name", None)
    if served_engine == engine.strip().lower() and served_model == primary_model:
        return ServedRoute(engine=engine, model=model)
    return ServedRoute(
        engine=served_engine, model=served_model or None, served_by_fallback=True
    )


def served_route_of_result(
    client: Any, engine: str, model: str | None, result: Any
) -> ServedRoute:
    """Read which model served from a result envelope.

    `AIPromptResult`, `AITurnResult`, and `AIStructuredOutputResult` all
    carry `provider_engine` and `model_name`, which a chain fills with the
    candidate that answered. A test double that carries neither reads as
    served by the primary.
    """
    return served_route(
        client,
        engine,
        model,
        getattr(result, "provider_engine", None),
        getattr(result, "model_name", None),
    )


def served_route_of_report(
    client: Any, engine: str, model: str | None, report: AIRequestCostReport | None
) -> ServedRoute:
    """Read which model served from the request cost report of a stream.

    The report's `served_by_engine` and `served_by_model` come from the line
    item the library marked as served when the stream ended. A stream whose
    engine recorded no line item (a test double) reads as served by the
    primary.
    """
    if report is None:
        return ServedRoute(engine=engine, model=model)
    return served_route(
        client, engine, model, report.served_by_engine, report.served_by_model
    )


def configured_fallbacks() -> str:
    """Return the configured chain as the library's `engine:model,...` text.

    Read through the library's own settings resolver, so the value comes from
    the process environment, then `.env`, then `completions.fallbacks` in
    models.json, in that order. Reading `os.environ` directly, as the service
    did before models.json existed, would ignore the file and leave a
    deployment that moved its chain there with no fallback at all.

    Returns:
        str: The chain, or an empty string when none is configured.
    """
    value: object = EnvSettings().get_setting(COMPLETIONS_FALLBACKS_ENV, "")
    return str(value) if value is not None else ""


def configured_fallback_reasons() -> str:
    """Return the configured trigger reasons as the library's comma text.

    Resolved the same way as `configured_fallbacks`.

    Returns:
        str: The reasons, or an empty string for the library's default set.
    """
    value: object = EnvSettings().get_setting(COMPLETIONS_FALLBACK_ON_ENV, "")
    return str(value).strip() if value is not None else ""


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
            or the configured reasons include an unknown one.
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
    candidates = parse_fallback_candidates(configured_fallbacks())
    for candidate in candidates:
        try:
            get_ai_provider_spec(AI_PROVIDER_CAPABILITY_COMPLETIONS, candidate.engine)
        except AiProviderConfigurationError as error:
            raise FallbackMisconfiguredError(
                f"the fallback chain (completions.fallbacks in models.json, or "
                f"{COMPLETIONS_FALLBACKS_ENV}) names {candidate.engine!r}, which "
                f"is not a completions engine: {error}"
            ) from error
    try:
        raw_reasons: str = configured_fallback_reasons()
        reasons = (
            parse_fallback_reasons(raw_reasons)
            if raw_reasons
            else DEFAULT_FALLBACK_REASONS
        )
    except ValueError as error:
        raise FallbackMisconfiguredError(
            f"completions.fallback_on (or {COMPLETIONS_FALLBACK_ON_ENV}): {error}"
        ) from error

    labels: list[str] = [candidate.label for candidate in candidates]
    if labels:
        logger.info(
            "model fallback: %s, on %s",
            " -> ".join(labels),
            ",".join(sorted(reason.value for reason in reasons)),
        )
    else:
        logger.info(
            "model fallback: off (completions.fallbacks in models.json is empty "
            "and %s is unset)",
            COMPLETIONS_FALLBACKS_ENV,
        )
    return labels
