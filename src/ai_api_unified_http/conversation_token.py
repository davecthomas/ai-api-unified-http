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

Versions:

- `v1` holds the turn's `raw_content`. Replay wraps it as the content of the
  caller's assistant message, which is the Anthropic and Bedrock shape only.
  OpenAI's `raw_content` is already a whole message and Gemini's is a parts
  list, so a v1 token from those engines never replayed. Still accepted, so a
  Claude conversation in flight across an upgrade continues.
- `v2` holds the messages the serving engine's own `extend_messages_with_turn`
  appends for the turn, in that engine's wire shape, and replay splices them
  in place of the caller's assistant message. A fallback client reads that
  shape to send the next turn back to the engine that produced it, for the
  shapes it recognizes (see docs/technical-design.md, "Model fallback").
"""

import base64
import binascii
import json
import re
from typing import Any, Final

# Bumped when the encoding changes. New tokens are written at TOKEN_VERSION;
# ACCEPTED_VERSIONS are the ones replay still understands.
TOKEN_VERSION: Final[str] = "v2"
_RAW_CONTENT_VERSION: Final[str] = "v1"
ACCEPTED_VERSIONS: Final[frozenset[str]] = frozenset(
    {_RAW_CONTENT_VERSION, TOKEN_VERSION}
)
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


def _pack(version: str, value: Any) -> str:
    payload: bytes = json.dumps(value, separators=(",", ":")).encode("utf-8")
    body: str = base64.urlsafe_b64encode(payload).decode("ascii")
    return f"{version}{_SEPARATOR}{body}"


def encode_conversation_token(raw_content: Any) -> str | None:
    """Pack provider content into a v1 token.

    Used when the serving engine cannot shape its own turn (a model without
    tool-use support, whose `extend_messages_with_turn` refuses). Replay then
    wraps the content in the caller's assistant message.

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
    return _pack(_RAW_CONTENT_VERSION, raw_content)


def encode_turn_messages(messages: list[dict[str, Any]]) -> str | None:
    """Pack a turn's messages, in the serving engine's shape, into a token.

    Args:
        messages: What the engine's `extend_messages_with_turn` appended to
            an empty history for this turn.

    Returns:
        str | None: The token, or None when the turn produced no messages.
    """
    if not messages:
        return None
    return _pack(TOKEN_VERSION, {"messages": messages})


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
    if version not in ACCEPTED_VERSIONS:
        raise InvalidConversationTokenError(
            f"Conversation token version {version!r} is not accepted by this "
            f"service version (expected one of {sorted(ACCEPTED_VERSIONS)}). "
            f"Start a new conversation."
        )
    try:
        payload: bytes = base64.urlsafe_b64decode(body.encode("ascii"))
        return json.loads(payload.decode("utf-8"))
    except (binascii.Error, UnicodeDecodeError, json.JSONDecodeError, ValueError) as e:
        raise InvalidConversationTokenError(
            f"Conversation token could not be decoded: {e}. Echo the token "
            f"back exactly as it was received."
        ) from e


def replay_messages(message: dict[str, Any]) -> list[dict[str, Any]]:
    """Return the history entries a tokened assistant message stands for.

    Args:
        message: The caller's assistant message whose content is a token.

    Returns:
        list[dict[str, Any]]: For a v2 token, the engine-shaped messages it
            carries. For v1, the caller's message with the content decoded.

    Raises:
        InvalidConversationTokenError: When the token cannot be decoded.
    """
    token: str = message["content"]
    decoded: Any = decode_conversation_token(token)
    if token.startswith(f"{_RAW_CONTENT_VERSION}{_SEPARATOR}"):
        return [{**message, "content": decoded}]
    messages: Any = decoded.get("messages") if isinstance(decoded, dict) else None
    if not isinstance(messages, list) or not all(isinstance(m, dict) for m in messages):
        raise InvalidConversationTokenError(
            "Conversation token does not carry a turn. Echo the token back "
            "exactly as it was received."
        )
    return messages
