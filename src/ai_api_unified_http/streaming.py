# src/ai_api_unified_http/streaming.py

"""
Server-sent events bridge for the library's synchronous stream.

`send_prompt_streaming` returns a sync `Iterator[str]`, so each `next()` call
blocks. Iterating it directly inside an async route would block the event loop
for the whole generation, stalling every other request on the worker. Starlette's
`iterate_in_threadpool` moves each step onto a worker thread instead, which
costs one thread per active stream for the stream's lifetime. Concurrency is
therefore capped at threadpool size times worker count until the library grows
an async streaming surface.

Errors are the awkward part of streaming. Once the first byte is written the
status line is already sent, so a mid-stream failure cannot become a 502 the
way a buffered call would. It is delivered as a terminal `error` event instead,
and clients must treat that as a failed call even though the response began
with a 200.
"""

import asyncio
import json
import logging
import time
from collections.abc import AsyncIterator, Iterator
from typing import Any, Final

from starlette.concurrency import iterate_in_threadpool, run_in_threadpool

logger: Final[logging.Logger] = logging.getLogger(__name__)

# SSE event names. `chunk` carries generated text, `done` closes a healthy
# stream, and `error` closes a failed one.
EVENT_CHUNK: Final[str] = "chunk"
EVENT_DONE: Final[str] = "done"
EVENT_ERROR: Final[str] = "error"

SSE_MEDIA_TYPE: Final[str] = "text/event-stream"

# Proxies that buffer responses defeat streaming, and nginx in particular
# needs to be told explicitly.
SSE_HEADERS: Final[dict[str, str]] = {
    "Cache-Control": "no-cache",
    "Connection": "keep-alive",
    "X-Accel-Buffering": "no",
}


def format_sse(event: str, data: dict[str, Any]) -> str:
    """Render one server-sent event.

    Args:
        event: Event name the client dispatches on.
        data: JSON-serializable payload.

    Returns:
        str: The wire-format event, terminated by the blank line SSE requires.
    """
    payload: str = json.dumps(data, separators=(",", ":"))
    return f"event: {event}\ndata: {payload}\n\n"


async def sse_from_sync_iterator(
    chunks: Iterator[str], engine: str, model: str | None
) -> AsyncIterator[str]:
    """Bridge the library's blocking text iterator onto an SSE byte stream.

    Args:
        chunks: The library's synchronous chunk iterator.
        engine: Engine token, echoed in the terminal event for attribution.
        model: Model name, echoed in the terminal event.

    Yields:
        str: SSE frames — zero or more `chunk` events, then exactly one
            terminal `done` or `error` event.
    """
    chunk_count: int = 0
    try:
        async for chunk in iterate_in_threadpool(chunks):
            if not chunk:
                # Providers emit empty keep-alive chunks; forwarding them
                # would show as spurious empty updates in a browser client.
                continue
            chunk_count += 1
            yield format_sse(EVENT_CHUNK, {"text": chunk})
    except Exception as error:  # noqa: BLE001 - the status line is already sent
        # A buffered call would map this to a 502. Here the 200 is committed,
        # so the failure has to travel in-band as the stream's last event.
        logger.warning(
            "stream failed after %s chunks: %s: %s",
            chunk_count,
            type(error).__name__,
            error,
        )
        yield format_sse(
            EVENT_ERROR,
            {
                "error": "stream_failed",
                "detail": str(error),
                "engine": engine,
                "chunks_delivered": chunk_count,
            },
        )
        return

    yield format_sse(
        EVENT_DONE, {"engine": engine, "model": model, "chunks": chunk_count}
    )


# --- Job progress ------------------------------------------------------------

EVENT_PROGRESS: Final[str] = "progress"

# Gap between reads of a job record while streaming its progress.
PROGRESS_POLL_SECONDS: Final[float] = 1.0

# Ceiling on how long one progress stream is held open. Each stream holds a
# connection for its lifetime, and Cloud Run counts connections against an
# instance's concurrency, so a job that stopped updating would otherwise
# occupy a slot indefinitely. A caller following a longer job polls
# GET /v1/videos/{id} and reconnects.
PROGRESS_MAX_SECONDS: Final[float] = 600.0


async def sse_job_progress(caller: str, job_id: str) -> AsyncIterator[str]:
    """Emit a job's progress as it changes, then a terminal event.

    Only changes are sent. A job sitting at the same percent for a minute
    produces one event, not sixty, so a client re-renders when there is
    something to re-render.

    Args:
        caller: API key label the job belongs to.
        job_id: The job to follow.

    Yields:
        str: Server-sent event frames.
    """
    # Imported here because artifacts imports nothing from this module and the
    # reverse import at module scope would be circular.
    from .artifacts import ArtifactNotFoundError, job_is_finished, read_job

    started: float = time.monotonic()
    last: tuple[str, float] | None = None

    while True:
        try:
            record = await run_in_threadpool(read_job, caller, job_id)
        except ArtifactNotFoundError:
            yield _frame(EVENT_ERROR, {"error": "job_not_found"})
            return

        current: tuple[str, float] = (record.status, record.percent)
        if current != last:
            yield _frame(
                EVENT_PROGRESS,
                {
                    "job_id": record.job_id,
                    "status": record.status,
                    "percent": record.percent,
                    "estimated": record.estimated,
                },
            )
            last = current

        if job_is_finished(record):
            if record.status == "failed":
                yield _frame(
                    EVENT_ERROR, {"error": record.error or "generation failed"}
                )
            else:
                yield _frame(
                    EVENT_DONE,
                    {"job_id": record.job_id, "artifact_ids": record.artifact_ids},
                )
            return

        if time.monotonic() - started > PROGRESS_MAX_SECONDS:
            yield _frame(EVENT_ERROR, {"error": "progress stream timed out"})
            return

        await asyncio.sleep(PROGRESS_POLL_SECONDS)


def _frame(event: str, payload: dict[str, Any]) -> str:
    """Format one server-sent event."""
    return f"event: {event}\ndata: {json.dumps(payload)}\n\n"
