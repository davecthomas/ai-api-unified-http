# src/ai_api_unified_http/routes_admin.py
"""
Operator endpoints, behind an admin key rather than a client key.

The library's response cache (3.23.0) lives inside the process that owns it,
so the only place it can be inspected is from inside that process. These
routes are that place for a deployment of this service: they call the
library's own inspection API and return what it returns, metadata only,
never a prompt or an answer.

Two facts shape what the numbers mean:

- **They are per worker.** The default store is in memory, and each worker
  process has its own, so the entries and counters describe the worker
  that happened to answer this request. A deployment that wants one view
  across workers hands the library a shared store (see the library README,
  "Response caching"); the listing then reads that store.
- **Clearing is a write.** `DELETE` removes entries from whichever store the
  answering worker uses. With the in-memory default that is one worker's
  cache; with a shared store it is everyone's.
"""

from typing import Final

from ai_api_unified import (
    response_cache_clear,
    response_cache_entries,
    response_cache_stats,
)
from fastapi import APIRouter, Query
from starlette.concurrency import run_in_threadpool

from .schemas import (
    CacheClearResponse,
    CacheEntry,
    CacheResponse,
    CacheStats,
    ErrorResponse,
)

ADMIN_PREFIX: Final[str] = "/v1/admin"

router: APIRouter = APIRouter(prefix=ADMIN_PREFIX, tags=["admin"])

_ADMIN_RESPONSES: Final[dict] = {
    401: {"model": ErrorResponse},
    403: {"model": ErrorResponse},
}


@router.get(
    "/cache",
    responses=_ADMIN_RESPONSES,
    summary="Response cache: counters and live entries (this worker)",
)
async def get_cache(
    limit: int = Query(default=100, ge=1, le=1000, description="Entries to list."),
) -> CacheResponse:
    """Report the response cache as the answering worker sees it.

    Args:
        limit: Most entries to list, most recently used first.

    Returns:
        CacheResponse: Whether the cache is on, the store, hit and miss
            counters with the cost they saved, and the live entries as
            metadata.
    """
    stats: dict = await run_in_threadpool(response_cache_stats)
    entries: list[dict] = await run_in_threadpool(response_cache_entries, limit)
    return CacheResponse(
        stats=CacheStats.model_validate(stats),
        entries=[CacheEntry.model_validate(entry) for entry in entries],
    )


@router.delete(
    "/cache",
    responses=_ADMIN_RESPONSES,
    summary="Clear the response cache (this worker's store)",
)
async def clear_cache(
    operation: str | None = Query(
        default=None,
        description=(
            "Clear only this operation's entries: completions, "
            "structured_output, conversation, embeddings, or classification."
        ),
    ),
) -> CacheClearResponse:
    """Remove live entries from the response cache.

    Args:
        operation: Restrict the clear to one operation; all when omitted.

    Returns:
        CacheClearResponse: How many entries were removed. Counters are kept.
    """
    removed: int = await run_in_threadpool(response_cache_clear, operation)
    return CacheClearResponse(removed=removed, operation=operation)
