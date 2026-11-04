# src/ai_api_unified_http/routes_run_grants.py

"""
Issue, read and revoke run grants (see run_grants.py).

These take an API key. A grant key never reaches them: the authenticator lets
a grant key open only the conversation endpoint. An API key sees and revokes
only the grants it issued; another key's grant answers 404, as a missing one
does.
"""

from decimal import Decimal
from typing import Any, Final

from fastapi import APIRouter, HTTPException, Request

from .caller_context import sanitize_identifier
from .clients import get_completions_client
from .run_grants import (
    RunGrant,
    RunGrantStore,
    grant_ttl_end,
    picodollars_to_usd,
    run_grant_store,
)
from .schemas import (
    ErrorResponse,
    RunGrantRequest,
    RunGrantResponse,
    RunGrantRevokeResponse,
)

RUN_GRANTS_PREFIX: Final[str] = "/v1/run-grants"

# The label a grant records as its issuer when authentication is off.
_NO_AUTH_ISSUER: Final[str] = "unauthenticated"

router: APIRouter = APIRouter(prefix=RUN_GRANTS_PREFIX, tags=["run-grants"])

_RESPONSES: Final[dict[int | str, dict[str, Any]]] = {
    400: {"model": ErrorResponse},
    401: {"model": ErrorResponse},
    404: {"model": ErrorResponse},
    409: {"model": ErrorResponse},
    503: {"model": ErrorResponse},
}


def _store(request: Request) -> RunGrantStore:
    store: RunGrantStore | None = run_grant_store(request.app)
    if store is None:
        raise HTTPException(
            status_code=503,
            detail=(
                "Run grants are not enabled on this deployment. Set "
                "HTTP_REDIS_URL to a Redis the service can reach."
            ),
        )
    return store


def _issuer(request: Request) -> str:
    return getattr(request.state, "api_key_label", None) or _NO_AUTH_ISSUER


def _pinned_model(engine: str, model: str | None) -> str:
    """The model the grant pins, refusing one the service cannot price.

    The model is resolved now, so the grant names it even when the request
    left it to the engine's default. A model with no token rates would make
    every turn's cost unknown, and the spend limit could never be reached.
    """
    client: Any = get_completions_client(engine, model)
    resolved: str | None = client.model_name
    pricing: Any = getattr(getattr(client, "capabilities", None), "pricing", None)
    if resolved is None or pricing is None or pricing.token_rates is None:
        raise HTTPException(
            status_code=400,
            detail=(
                f"{engine}:{resolved or model} has no token pricing on record, so "
                f"its spend cannot be counted against a limit. Pick a priced model."
            ),
        )
    return resolved


async def _response(
    store: RunGrantStore, grant: RunGrant, grant_key: str | None, created: bool
) -> RunGrantResponse:
    return RunGrantResponse(
        run_id=grant.run_id,
        grant_key=grant_key,
        created=created,
        engine=grant.engine,
        model=grant.model,
        spend_limit_usd=str(grant.spend_limit_usd),
        spent_usd=picodollars_to_usd(await store.spent_picodollars(grant.run_id)),
        expires_at=grant.expires_at_iso(),
    )


@router.post("", responses=_RESPONSES, summary="Issue a run grant and its key")
async def issue_run_grant(request: Request, body: RunGrantRequest) -> RunGrantResponse:
    """Issue a grant for one run, or a new key for a live grant with the same terms."""
    store: RunGrantStore = _store(request)
    limit: Decimal = Decimal(body.spend_limit_usd)
    if limit <= 0:
        raise HTTPException(status_code=400, detail="spend_limit_usd must be above 0.")
    tags: dict[str, str] = {}
    for name, value in body.tags.items():
        cleaned: str | None = sanitize_identifier(value)
        if cleaned is not None:
            tags[name] = cleaned
    requested = RunGrant(
        run_id=body.run_id,
        issuer=_issuer(request),
        engine=body.engine,
        model=_pinned_model(body.engine, body.model),
        spend_limit_usd=limit,
        expires_at=grant_ttl_end(body.ttl_seconds),
        caller_id=sanitize_identifier(body.caller_id),
        session_id=sanitize_identifier(body.session_id),
        workflow_id=sanitize_identifier(body.workflow_id),
        tags=tags,
    )
    grant, grant_key, created = await store.issue(requested)
    return await _response(store, grant, grant_key, created)


async def _own_grant(request: Request, store: RunGrantStore, run_id: str) -> RunGrant:
    grant: RunGrant | None = await store.get(run_id)
    if grant is None or grant.issuer != _issuer(request):
        raise HTTPException(
            status_code=404, detail=f"No live run grant for {run_id!r}."
        )
    return grant


@router.get("/{run_id}", responses=_RESPONSES, summary="A run grant and its spend")
async def get_run_grant(request: Request, run_id: str) -> RunGrantResponse:
    store: RunGrantStore = _store(request)
    grant: RunGrant = await _own_grant(request, store, run_id)
    return await _response(store, grant, None, False)


@router.delete(
    "/{run_id}", responses=_RESPONSES, summary="Revoke a run grant and all its keys"
)
async def revoke_run_grant(request: Request, run_id: str) -> RunGrantRevokeResponse:
    """End the grant. Revoking one that is gone answers 200 with revoked false.

    A caller revokes on every end of a run, retries included, so a second
    revoke must not fail.
    """
    store: RunGrantStore = _store(request)
    grant: RunGrant | None = await store.get(run_id)
    if grant is not None and grant.issuer != _issuer(request):
        raise HTTPException(
            status_code=404, detail=f"No live run grant for {run_id!r}."
        )
    if grant is None:
        return RunGrantRevokeResponse(run_id=run_id, revoked=False, spent_usd="0")
    revoked, spent = await store.revoke(run_id)
    return RunGrantRevokeResponse(
        run_id=run_id, revoked=revoked, spent_usd=picodollars_to_usd(spent)
    )
