# src/ai_api_unified_http/paths.py

"""
Paths served without an API key.

The set lives apart from the middleware that read it because the dependency
between those two runs one way. The authenticator has to reach the limiter, so
a request it rejects is still counted, and the limiter has to know which paths
carry no key. Holding the set in either module would make that pair circular.
"""

from typing import Final

# /healthz answers load balancers that hold no credential. Cloud Run reserves
# that path at its own frontend and never forwards it, so /health is served
# too. The OpenAPI documents carry no secrets and are what the TypeScript
# client is generated from, so gating them would break codegen for every
# consumer that does not already have a key.
PUBLIC_PATHS: Final[frozenset[str]] = frozenset(
    {
        "/healthz",
        "/health",
        "/openapi.json",
        "/docs",
        "/docs/oauth2-redirect",
        "/redoc",
    }
)
