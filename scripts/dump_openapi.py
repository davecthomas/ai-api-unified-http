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

from ai_api_unified_http.app import create_app


def main() -> int:
    """Dump the spec to the path given as the first argument."""
    destination = Path(sys.argv[1] if len(sys.argv) > 1 else "openapi.json")
    destination.parent.mkdir(parents=True, exist_ok=True)
    spec = create_app().openapi()
    # Sorted keys and a trailing newline keep the output byte-stable, so the
    # drift check compares content rather than dict ordering.
    destination.write_text(
        json.dumps(spec, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    print(f"wrote {destination} ({len(spec['paths'])} paths)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
