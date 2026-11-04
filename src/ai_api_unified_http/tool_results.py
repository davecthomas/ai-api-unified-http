# src/ai_api_unified_http/tool_results.py

"""
Tool results in one shape for every engine.

A caller answers a tool call with

    {"type": "tool_result", "tool_call_id": "<ToolCall.id>",
     "result": {...}, "is_error": false}

and the service builds the engine's own message from it with the library's
`build_tool_result_message`, on the client that serves the turn. A fallback
client is also given the history before the result, and builds it in the
shape of the engine that history came from. So the caller needs no
per-engine code, and a turn a fallback served gets its results in the
fallback engine's shape.

A neutral result has a `type` and no `role`. The library reads any message of
that form as an OpenAI Responses item, so every neutral result is converted
before the history reaches it.

Consecutive neutral results answer one assistant turn. Where the engine takes
results as a user message holding a list (Claude and Bedrock `content`, Gemini
`parts`), they are merged into one message: Gemini wants every function
response of a turn together, and Bedrock wants user and assistant turns to
alternate. OpenAI takes one message or item per result, so its results stay
separate.

A result already in an engine's own shape passes through untouched.
"""

from typing import Any, Final

from ai_api_unified import AiFallbackCompletions

NEUTRAL_TYPE: Final[str] = "tool_result"
_FIELDS: Final[frozenset[str]] = frozenset(
    {"type", "tool_call_id", "result", "is_error"}
)
# The list-holding keys of a user message that can take several results.
_MERGEABLE_KEYS: Final[tuple[str, ...]] = ("content", "parts")


class InvalidToolResultError(ValueError):
    """Raised when a neutral tool result is malformed. Maps to 400."""


def is_neutral_tool_result(message: object) -> bool:
    """Return whether a history entry is a neutral tool result."""
    return (
        isinstance(message, dict)
        and message.get("type") == NEUTRAL_TYPE
        and "role" not in message
    )


def _fields(message: dict[str, Any], index: int) -> tuple[str, dict[str, Any], bool]:
    """Read and check a neutral result's fields.

    Raises:
        InvalidToolResultError: When a field is missing, mistyped, or unknown.
    """
    where: str = f"messages[{index}] (a tool_result)"
    unknown: set[str] = set(message) - _FIELDS
    if unknown:
        raise InvalidToolResultError(
            f"{where} has unknown fields {sorted(unknown)}. A tool result takes "
            f"type, tool_call_id, result, and is_error."
        )
    tool_call_id: Any = message.get("tool_call_id")
    if not isinstance(tool_call_id, str) or not tool_call_id:
        raise InvalidToolResultError(
            f"{where} needs tool_call_id: the id of the tool call it answers."
        )
    result: Any = message.get("result")
    if not isinstance(result, dict):
        raise InvalidToolResultError(
            f"{where} needs result as a JSON object, e.g. "
            '{"output": "<the tool\'s text>"}.'
        )
    is_error: Any = message.get("is_error", False)
    if not isinstance(is_error, bool):
        raise InvalidToolResultError(f"{where} needs is_error as true or false.")
    return tool_call_id, result, is_error


def validate_tool_results(messages: list[dict[str, Any]]) -> None:
    """Check every neutral result before the client pool is touched.

    Raises:
        InvalidToolResultError: On the first malformed result.
    """
    for index, message in enumerate(messages):
        if is_neutral_tool_result(message):
            _fields(message, index)


def _build(
    client: Any,
    history: list[dict[str, Any]],
    tool_call_id: str,
    result: dict[str, Any],
    is_error: bool,
) -> dict[str, Any]:
    """Build one result in the shape of the engine that serves it."""
    if isinstance(client, AiFallbackCompletions):
        # The chain picks the engine whose shape the history is in.
        return client.build_tool_result_message(
            tool_call_id=tool_call_id,
            result=result,
            is_error=is_error,
            messages=list(history),
        )
    return client.build_tool_result_message(
        tool_call_id=tool_call_id, result=result, is_error=is_error
    )


def _merged(previous: dict[str, Any], built: dict[str, Any]) -> dict[str, Any] | None:
    """One user message holding both results, or None when the shape has none."""
    for key in _MERGEABLE_KEYS:
        if (
            previous.keys() == built.keys() == {"role", key}
            and previous["role"] == built["role"]
            and isinstance(previous[key], list)
            and isinstance(built[key], list)
        ):
            return {"role": previous["role"], key: [*previous[key], *built[key]]}
    return None


def shape_tool_results(
    client: Any, messages: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Replace every neutral result with the serving engine's own message.

    Args:
        client: The pooled client that will run the turn.
        messages: History with conversation tokens already expanded.

    Returns:
        list[dict[str, Any]]: The history with neutral results converted in
            place, and each run of them merged where the engine allows.

    Raises:
        InvalidToolResultError: When a neutral result is malformed.
        AiProviderCapabilityUnsupportedError: When the model takes no tools.
    """
    shaped: list[dict[str, Any]] = []
    # True while the last entry came from a neutral result in the current run.
    in_run: bool = False
    for index, message in enumerate(messages):
        if not is_neutral_tool_result(message):
            shaped.append(message)
            in_run = False
            continue
        tool_call_id, result, is_error = _fields(message, index)
        built: dict[str, Any] = _build(client, shaped, tool_call_id, result, is_error)
        merged: dict[str, Any] | None = _merged(shaped[-1], built) if in_run else None
        if merged is not None:
            shaped[-1] = merged
        else:
            shaped.append(built)
        in_run = True
    return shaped
