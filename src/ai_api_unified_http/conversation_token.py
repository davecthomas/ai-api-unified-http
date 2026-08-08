# src/ai_api_unified_http/conversation_token.py

"""
Opaque round-trip token for `AITurnResult.raw_content`.

The library needs the previous turn's provider content back verbatim to
continue a conversation, and the service holds no conversation state, so that
content has to travel through the client. It is engine-specific and its shape
changes with the provider and the library version, which makes it exactly the
kind of value a client must never read.

Wrapping it in an opaque token is what enforces that. Handing back raw JSON
would invite clients to parse it, and any field they came to depend on would
turn a provider's internal representation into this service's public contract.

The encoding is base64 of compact JSON, prefixed with a version tag. It is
deliberately **not** signed or encrypted:

- The content is the caller's own conversation, replayed back to them. There is
  nothing here they did not already send or receive.
- Signing would require key management and rotation for no confidentiality
  gain, and the service would still have to treat a decoded token as untrusted
  input, exactly as it does now.

What the version prefix buys is a clean failure. When the encoding changes, an
old token is rejected with a message telling the caller to start a new
conversation, rather than being fed to a provider as malformed content.
"""

import base64
import binascii
import json
import re
from typing import Any, Final

# Bumped when the encoding changes in a way old tokens cannot satisfy.
TOKEN_VERSION: Final[str] = "v1"
_SEPARATOR: Final[str] = "."

# Any version-shaped prefix counts as a token attempt, not just the current
# one. A token from a future or retired version has to be rejected with a
# clear message; letting it fall through would replay "v99.eyJ..." to a
# provider as literal assistant text.
TOKEN_PATTERN: Final[re.Pattern[str]] = re.compile(r"^v\d+\.")


def looks_like_conversation_token(value: object) -> bool:
    """Return whether a message content is an attempt at a conversation token."""
    return isinstance(value, str) and bool(TOKEN_PATTERN.match(value))


class InvalidConversationTokenError(ValueError):
    """Raised when a token cannot be decoded, or carries an unknown version."""


def encode_conversation_token(raw_content: Any) -> str | None:
    """Pack provider content into an opaque token.

    Args:
        raw_content: The library's `raw_content` for this turn. Verified
            JSON-safe across engines: each builds plain dicts and lists.

    Returns:
        str | None: The token, or None when the turn carried no content, so
            the field is absent from the response rather than holding an
            encoded null.
    """
    if raw_content is None:
        return None
    payload: bytes = json.dumps(raw_content, separators=(",", ":")).encode("utf-8")
    body: str = base64.urlsafe_b64encode(payload).decode("ascii")
    return f"{TOKEN_VERSION}{_SEPARATOR}{body}"


def decode_conversation_token(token: str) -> Any:
    """Unpack a token produced by `encode_conversation_token`.

    Args:
        token: The token the client echoed back.

    Returns:
        Any: The provider content, ready to replay to the library.

    Raises:
        InvalidConversationTokenError: When the token is malformed or was
            produced by an encoding this version no longer accepts. Both are
            caller-fixable by starting a new conversation, so both map to 400.
    """
    version, separator, body = token.partition(_SEPARATOR)
    if not separator:
        raise InvalidConversationTokenError(
            "Malformed conversation token: expected a version prefix. Tokens "
            "come from a previous turn's response and are echoed back "
            "unmodified."
        )
    if version != TOKEN_VERSION:
        raise InvalidConversationTokenError(
            f"Conversation token version {version!r} is no longer accepted by "
            f"this service version (expected {TOKEN_VERSION!r}). Start a new "
            f"conversation."
        )
    try:
        payload: bytes = base64.urlsafe_b64decode(body.encode("ascii"))
        return json.loads(payload.decode("utf-8"))
    except (binascii.Error, UnicodeDecodeError, json.JSONDecodeError, ValueError) as e:
        raise InvalidConversationTokenError(
            f"Conversation token could not be decoded: {e}. Echo the token "
            f"back exactly as it was received."
        ) from e
