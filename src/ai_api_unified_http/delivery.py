# src/ai_api_unified_http/delivery.py

"""
Serving a stored artifact's bytes, resumably.

A client draws a progress bar from `Content-Length` and the bytes it has read
so far. That is the whole mechanism — no side channel, no framing, no event
protocol — so the one obligation here is to always send an honest length and
never buffer the body to produce it.

`Range` is what makes a failed transfer cheap. Generation has already been paid
for by the time these bytes exist, and a drop at 90% of a large video would
otherwise mean sending the first 90% again. A caller that kept what it received
asks for the rest and gets a 206.

The bytes are streamed from the store in chunks, so serving a video never
requires holding one in the instance's memory.
"""

import logging
import re
from typing import Final

from fastapi import Request
from fastapi.responses import JSONResponse, StreamingResponse
from starlette.concurrency import iterate_in_threadpool
from starlette.responses import Response

from .artifacts import ArtifactRecord, read_range
from .schemas import ErrorResponse

# "bytes=0-1023", "bytes=1024-", "bytes=-512". Anything else is ignored rather
# than refused: RFC 9110 permits serving the whole body when a range cannot be
# satisfied in the form given.
_RANGE: Final[re.Pattern[str]] = re.compile(r"^bytes=(\d*)-(\d*)$")

logger: Final[logging.Logger] = logging.getLogger(__name__)


def parse_range(header: str | None, size: int) -> tuple[int, int] | None:
    """Resolve a Range header into inclusive byte offsets.

    Args:
        header: Raw `Range` header value, or None.
        size: Total size of the artifact in bytes.

    Returns:
        tuple[int, int] | None: First and last byte to send, or None to send
            the whole artifact. A range that cannot be satisfied also returns
            None, which serves the whole body rather than failing the request.
    """
    if not header or size == 0:
        return None
    match = _RANGE.match(header.strip())
    if match is None:
        return None

    raw_start, raw_end = match.group(1), match.group(2)
    if raw_start == "" and raw_end == "":
        return None
    if raw_start == "":
        # "bytes=-N": the final N bytes.
        length = min(int(raw_end), size)
        return size - length, size - 1
    start = int(raw_start)
    if start >= size:
        return None
    end = int(raw_end) if raw_end else size - 1
    return start, min(end, size - 1)


def artifact_response(
    request: Request, caller: str, record: ArtifactRecord
) -> Response:
    """Stream a stored artifact, honouring Range and always sending a length.

    Args:
        request: The incoming request, read for its `Range` header.
        caller: API key label the artifact belongs to.
        record: The artifact's metadata.

    Returns:
        Response: 200 with the whole artifact, or 206 with the requested range.
    """
    size: int = record.size_bytes
    span = parse_range(request.headers.get("range"), size)
    start, end = span if span else (0, max(size - 1, 0))
    length: int = (end - start + 1) if size else 0

    headers: dict[str, str] = {
        # Without this a client cannot tell how far along it is, which is the
        # entire basis of a progress bar.
        "Content-Length": str(length),
        # Advertised even on a full response, so a client that has to retry
        # knows it may resume rather than start again.
        "Accept-Ranges": "bytes",
        "Cache-Control": "private, max-age=3600",
        "X-Artifact-Id": record.artifact_id,
    }
    if span is not None:
        headers["Content-Range"] = f"bytes {start}-{end}/{size}"

    # Reading the file blocks, so each chunk is pulled on a worker thread
    # rather than on the event loop, the same treatment the SSE bridge gives
    # the library's synchronous generator.
    chunks = iterate_in_threadpool(read_range(caller, record.artifact_id, start, end))
    return StreamingResponse(
        chunks,
        status_code=206 if span is not None else 200,
        media_type=record.mime_type,
        headers=headers,
    )


def not_found(detail: str) -> JSONResponse:
    """Build the 404 body in the service-wide error shape."""
    body = ErrorResponse(error="artifact_not_found", detail=detail)
    return JSONResponse(status_code=404, content=body.model_dump())
