#!/usr/bin/env python3
"""Write the OpenAPI document to a file.

The spec comes from the app object rather than a running server, so
regenerating the client needs no port, no provider keys, and no startup
configuration. That is what lets CI check the committed client against the
spec on every pull request.
"""

import json
import sys
from pathlib import Path
from typing import Any

from ai_api_unified_http.app import create_app

# FastAPI labels auto-generated responses with Python's own HTTP reason
# phrases, and Python 3.13 renamed 422 from "Unprocessable Entity" to
# "Unprocessable Content". The spec would therefore differ by interpreter
# version, and the CI drift check would fail for any contributor whose Python
# minor differs from the one that last regenerated. Pinning the phrase keeps
# the output identical across every version this project supports.
_STABLE_REASON_PHRASES: dict[str, str] = {
    "Unprocessable Entity": "Unprocessable Content",
}


def _stabilize(node: Any) -> Any:
    """Replace version-dependent reason phrases throughout the document."""
    if isinstance(node, dict):
        return {
            key: (
                _STABLE_REASON_PHRASES.get(value, value)
                if key == "description" and isinstance(value, str)
                else _stabilize(value)
            )
            for key, value in node.items()
        }
    if isinstance(node, list):
        return [_stabilize(item) for item in node]
    return node


def main() -> int:
    """Dump the spec to the path given as the first argument."""
    destination = Path(sys.argv[1] if len(sys.argv) > 1 else "openapi.json")
    destination.parent.mkdir(parents=True, exist_ok=True)
    spec = _stabilize(create_app().openapi())
    # Sorted keys and a trailing newline keep the output byte-stable, so the
    # drift check compares content rather than dict ordering.
    destination.write_text(
        json.dumps(spec, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    print(f"wrote {destination} ({len(spec['paths'])} paths)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
