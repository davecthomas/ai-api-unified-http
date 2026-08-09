# src/ai_api_unified_http/caller_context.py

"""
Per-request caller attribution for cost events.

Without this, every cost event the service produces carries the library's
default caller, so a deployment serving many end users sees one undifferentiated
spend total. An API key identifies the calling *application*, which is the wrong
grain: one web app serving a thousand users holds one key.

Callers supply identifiers as request headers, which the service normalizes and
puts into the library's observability context for the duration of the request.
The library then stamps them onto every cost and observability event it emits.

Two properties are worth being precise about.

**These identifiers are attribution, not authorization.** The service cannot
verify that `X-Caller-Id: user-42` really is user 42; the calling application
asserts it. That is sound for splitting a bill between a trusted caller's own
users, and useless as an access control. Authorization remains the API key's
job.

**The value reaches logs and cost records.** It is therefore length-bounded and
stripped of control characters, so a caller cannot forge log lines or smuggle a
newline into a cost event. Callers should send an opaque, stable id rather than
an email address or a name, because whatever they send lands in the cost sink.
"""

import logging
import re
from typing import Final

from ai_api_unified.middleware import (
    reset_observability_context,
    set_observability_context,
)
from fastapi import Request
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.responses import Response

CALLER_ID_HEADER: Final[str] = "X-Caller-Id"
SESSION_ID_HEADER: Final[str] = "X-Session-Id"
WORKFLOW_ID_HEADER: Final[str] = "X-Workflow-Id"

# Long enough for a UUID, a prefixed id, or a short opaque token. Anything
# longer is a payload rather than an identifier.
MAX_IDENTIFIER_LENGTH: Final[int] = 128

# Control characters, including the newlines and carriage returns that would
# otherwise let a caller inject a forged line into the log or the cost sink.
_CONTROL_CHARACTERS: Final[re.Pattern[str]] = re.compile(r"[\x00-\x1f\x7f]")

logger: Final[logging.Logger] = logging.getLogger(__name__)


def sanitize_identifier(value: str | None) -> str | None:
    """Make a caller-supplied identifier safe to write into a log record.

    Args:
        value: Raw header value, or None when the header is absent.

    Returns:
        str | None: The cleaned identifier, or None when nothing usable remains.
    """
    if value is None:
        return None
    cleaned: str = _CONTROL_CHARACTERS.sub("", value).strip()
    if not cleaned:
        return None
    if len(cleaned) > MAX_IDENTIFIER_LENGTH:
        cleaned = cleaned[:MAX_IDENTIFIER_LENGTH]
    return cleaned


def namespaced_caller(api_key_label: str, caller_id: str | None) -> str:
    """Combine the API key's label with the caller's own identifier.

    Two applications both numbering their users from one would otherwise
    collide in the cost record, and the resulting total would be attributed to
    whichever of them the reader assumed. Prefixing with the key label keeps
    each application's identifiers in their own space.

    Args:
        api_key_label: Label of the API key that authenticated the request.
        caller_id: The caller's identifier, or None when the header is absent.

    Returns:
        str: `label:caller` when an identifier was supplied, otherwise the
            label alone, which attributes the spend to the application.
    """
    if caller_id is None:
        return api_key_label
    return f"{api_key_label}:{caller_id}"


class CallerContextMiddleware(BaseHTTPMiddleware):
    """Attach caller identifiers to the library's observability context.

    Runs after authentication, because the namespace comes from the API key's
    label. The context is reset in a `finally`, since it lives in a contextvar
    that a worker thread would otherwise carry into the next request it serves.
    """

    async def dispatch(
        self, request: Request, call_next: RequestResponseEndpoint
    ) -> Response:
        """Set the context for this request, then restore it."""
        label: str = getattr(request.state, "api_key_label", "unauthenticated")
        caller_id: str | None = sanitize_identifier(
            request.headers.get(CALLER_ID_HEADER)
        )
        session_id: str | None = sanitize_identifier(
            request.headers.get(SESSION_ID_HEADER)
        )
        workflow_id: str | None = sanitize_identifier(
            request.headers.get(WORKFLOW_ID_HEADER)
        )

        # session_id and workflow_id are passed twice on purpose. The library's
        # cost event carries a fixed field set that includes caller_id and
        # neither of the other two, while tags are emitted on cost events as
        # tag_<name>. Passing them only as context fields would attribute spend
        # to a user but leave the session invisible in the record that bills.
        tags: dict[str, str] = {}
        if session_id is not None:
            tags["session_id"] = session_id
        if workflow_id is not None:
            tags["workflow_id"] = workflow_id

        token = set_observability_context(
            caller_id=namespaced_caller(label, caller_id),
            session_id=session_id,
            workflow_id=workflow_id,
            tags=tags or None,
        )
        try:
            return await call_next(request)
        finally:
            reset_observability_context(token)
