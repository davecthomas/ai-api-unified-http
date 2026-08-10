# src/ai_api_unified_http/clients.py

"""
Process-wide provider client pool.

`AIFactory.get_*` builds a new client on every call, and each construction
re-reads `.env`, re-parses the middleware YAML, and — for Gemini — makes a
`models.get` network round trip. Calling the factory per request would put a
config parse and a network hop in front of every completion, so the service
builds each client once per `(engine, model)` and reuses it for the life of
the process. See docs/technical-design.md, "Client pool", for the safety
argument: engine instances hold no per-conversation state, the provider SDK
clients are thread-safe, and the library's mutable instance attributes are
idempotent lazy caches.

Construction happens outside the lock. Two requests racing on the same cold
key can therefore both build a client, and the loser's copy is discarded.
That trade is deliberate: holding the lock across construction would serialize
every cold start behind one Gemini network call, and a duplicate build is
harmless because clients carry no state worth preserving.
"""

import threading
from typing import Any, Final

from ai_api_unified import AIBaseCompletions, AIBaseEmbeddings, AIFactory

from .errors import ProviderNotConfiguredError, missing_variable_from

# Key is (engine, model); model is None when the caller accepts the engine
# default, which is a distinct pool entry from any named model.
PoolKey = tuple[str, str | None]

_completions_pool: Final[dict[PoolKey, AIBaseCompletions]] = {}
_embeddings_pool: Final[dict[PoolKey, AIBaseEmbeddings]] = {}
# Image and video clients share a pool: both are keyed by model rather than
# by an engine token, and neither has a typed base exported for annotation.
_media_pool: Final[dict[PoolKey, Any]] = {}
_pool_lock: Final[threading.Lock] = threading.Lock()


def get_completions_client(engine: str, model: str | None = None) -> AIBaseCompletions:
    """Return the pooled completions client for this engine and model.

    Args:
        engine: Completions engine token, e.g. "openai", "claude", "google-gemini".
        model: Model name, or None to accept the engine's configured default.

    Returns:
        AIBaseCompletions: The pooled client, built on first use for this key.

    Raises:
        AiProviderError: Propagated from the library when the engine is unknown
            or its credentials are missing. Surfacing at first use per key,
            rather than per request, is a documented consequence of pooling.
    """
    key: PoolKey = (engine, model)
    cached: AIBaseCompletions | None = _completions_pool.get(key)
    if cached is not None:
        return cached

    built: AIBaseCompletions = _build(
        AIFactory.get_ai_completions_client,
        engine,
        model_name=model,
        completions_engine=engine,
    )
    with _pool_lock:
        # setdefault keeps whichever client won the race; both are equivalent.
        client: AIBaseCompletions = _completions_pool.setdefault(key, built)
    return client


def get_embeddings_client(engine: str, model: str | None = None) -> AIBaseEmbeddings:
    """Return the pooled embeddings client for this engine and model.

    Args:
        engine: Embeddings engine token, e.g. "openai", "google-gemini", "voyage".
        model: Model name, or None to accept the engine's configured default.

    Returns:
        AIBaseEmbeddings: The pooled client, built on first use for this key.

    Raises:
        AiProviderError: Propagated from the library when the engine is unknown
            or its credentials are missing.
    """
    key: PoolKey = (engine, model)
    cached: AIBaseEmbeddings | None = _embeddings_pool.get(key)
    if cached is not None:
        return cached

    built: AIBaseEmbeddings = _build(
        AIFactory.get_ai_embedding_client,
        engine,
        embedding_engine=engine,
        model_name=model,
    )
    with _pool_lock:
        client: AIBaseEmbeddings = _embeddings_pool.setdefault(key, built)
    return client


def get_images_client(model: str | None = None) -> Any:
    """Return the pooled images client for this model.

    Keyed on the model alone, because the factory selects the provider from the
    model rather than taking an engine token the way completions does.

    Args:
        model: Image model name, or None for the configured default.

    Returns:
        Any: The pooled client, built on first use.
    """
    key: PoolKey = ("images", model)
    cached: Any = _media_pool.get(key)
    if cached is not None:
        return cached

    built: Any = _build(AIFactory.get_ai_images_client, "images", image_model=model)
    with _pool_lock:
        return _media_pool.setdefault(key, built)


def get_video_client(engine: str | None = None, model: str | None = None) -> Any:
    """Return the pooled video client for this engine and model.

    Args:
        engine: Video engine token, or None for the configured default.
        model: Video model name, or None for the configured default.

    Returns:
        Any: The pooled client, built on first use.
    """
    key: PoolKey = (f"video:{engine or 'default'}", model)
    cached: Any = _media_pool.get(key)
    if cached is not None:
        return cached

    built: Any = _build(
        AIFactory.get_ai_video_client,
        engine or "video",
        model_name=model,
        video_engine=engine,
    )
    with _pool_lock:
        return _media_pool.setdefault(key, built)


def _build(factory: Any, engine: str, **kwargs: Any) -> Any:
    """Construct a client, translating a missing-credential failure.

    Provider SDKs report an absent key as a plain `ValueError`, which is
    outside the library's exception hierarchy and so would escape every
    handler and surface as a bare 500. Translating it here keeps the
    classification at the point where the engine is known.

    Args:
        factory: The AIFactory constructor to call.
        engine: Engine token, for the error message.
        **kwargs: Constructor arguments.

    Returns:
        Any: The constructed client.

    Raises:
        ProviderNotConfiguredError: When the environment lacks a credential
            the provider requires.
    """
    try:
        return factory(**kwargs)
    except (ValueError, KeyError) as error:
        raise ProviderNotConfiguredError(
            str(error), engine=engine, missing_variable=missing_variable_from(error)
        ) from error


def pool_sizes() -> dict[str, int]:
    """Return current pool occupancy, for tests and operational checks.

    Returns:
        dict[str, int]: Entry counts keyed by "completions" and "embeddings".
    """
    return {
        "completions": len(_completions_pool),
        "embeddings": len(_embeddings_pool),
        "media": len(_media_pool),
    }


def reset_pools() -> None:
    """Drop every pooled client.

    Exists for test isolation. Nothing in the request path calls this: a
    running service reuses its clients for the life of the process.
    """
    with _pool_lock:
        _completions_pool.clear()
        _embeddings_pool.clear()
        _media_pool.clear()
