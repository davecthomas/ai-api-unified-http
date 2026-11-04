# src/ai_api_unified_http/run_grants.py

"""
Run grants: model access for one agent run, with a model, a spend limit and
an expiry.

A harness runs an agent (pi) in a sandbox, and every model call the agent
makes comes here. Giving the sandbox an API key would let the agent call any
model, spend without limit, and keep that power after the run ends. A run
grant narrows that. The caller that starts the run asks for a grant, gets back
a **grant key**, and hands only the key to the sandbox. The key opens
`POST /v1/conversations/turn` and nothing else.

A grant is a record, kept in Redis:

- `run_id`: the run it belongs to, chosen by the issuer
- `issuer`: the label of the API key that asked for it
- `engine` and `model`: every turn made with the key runs here, whatever the
  request body names
- `spend_limit_usd`: once the run's spend reaches it, turns are refused
- `expires_at`: after this the key stops working, and Redis drops the record
- attribution (`caller_id`, `session_id`, `workflow_id`, `tags`) stamped on
  every cost event the run produces, in place of the request headers

The grant key is a random string with the `rg_` prefix and carries no data.
Redis stores only its SHA-256, so a copy of the keyspace holds no key that
works. One grant can have several keys: a caller retrying its request for a
live grant gets a new key for the same grant, and the old key keeps working.
Revoking the grant ends all of them.

Redis, because the service runs as several processes and instances. A grant
issued by one must be readable by the others. The store is optional: without
`HTTP_REDIS_URL` the service runs as before, and the grant endpoints answer
503.

Spend is counted in whole picodollars (10^-12 USD) with `INCRBY`, so the
running total is exact. A turn's cost is known only after the provider
answers, so the check happens before each turn: a run can pass its limit by
the cost of the turns already in flight when it crossed, never by more.
"""

import hashlib
import json
import logging
import math
import os
import secrets
import time
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from decimal import ROUND_CEILING, Decimal
from functools import wraps
from typing import Any, Final, ParamSpec, TypeVar

from redis.asyncio import Redis
from redis.exceptions import RedisError

REDIS_URL_ENV: Final[str] = "HTTP_REDIS_URL"
REDIS_PREFIX_ENV: Final[str] = "HTTP_REDIS_PREFIX"
DEFAULT_REDIS_PREFIX: Final[str] = "ai-api-unified-http:"

# What a grant key starts with, so the authenticator can tell one from an API
# key without a lookup.
GRANT_KEY_PREFIX: Final[str] = "rg_"

# The only path a grant key opens.
GRANT_KEY_PATH: Final[str] = "/v1/conversations/turn"

MAX_TTL_SECONDS: Final[int] = 24 * 60 * 60
_REDIS_TIMEOUT_SECONDS: Final[float] = 2.0
_PICODOLLARS_PER_USD: Final[int] = 10**12

logger: Final[logging.Logger] = logging.getLogger(__name__)

_P = ParamSpec("_P")
_R = TypeVar("_R")


class RunGrantStoreUnavailableError(RuntimeError):
    """Redis could not be reached, or answered with an error. Maps to 503."""


class RunGrantConflictError(ValueError):
    """A live grant for the run id has different terms or another issuer. Maps to 409."""


class RunGrantSpendLimitError(RuntimeError):
    """The run has spent its limit, so the turn is refused. Maps to 402."""

    def __init__(self, run_id: str, spent: str, limit: str) -> None:
        super().__init__(
            f"Run {run_id!r} has spent ${spent} of its ${limit} limit, so no "
            f"further turns are allowed on this grant."
        )
        self.run_id: str = run_id


@dataclass(frozen=True)
class RunGrant:
    """The record behind a grant key."""

    run_id: str
    issuer: str
    engine: str
    model: str
    spend_limit_usd: Decimal
    expires_at: int
    caller_id: str | None = None
    session_id: str | None = None
    workflow_id: str | None = None
    tags: dict[str, str] = field(default_factory=dict)

    def same_terms(self, other: "RunGrant") -> bool:
        """Whether a second request for this run asks for what the grant already allows."""
        return (self.issuer, self.engine, self.model, self.spend_limit_usd) == (
            other.issuer,
            other.engine,
            other.model,
            other.spend_limit_usd,
        )

    def expired(self, now: float | None = None) -> bool:
        return self.expires_at <= (time.time() if now is None else now)

    def expires_at_iso(self) -> str:
        return datetime.fromtimestamp(self.expires_at, UTC).isoformat()

    def spend_limit_picodollars(self) -> int:
        return usd_to_picodollars(self.spend_limit_usd)

    def to_json(self) -> str:
        record: dict[str, Any] = asdict(self)
        record["spend_limit_usd"] = str(self.spend_limit_usd)
        return json.dumps(record, separators=(",", ":"))

    @classmethod
    def from_json(cls, raw: str) -> "RunGrant":
        record: dict[str, Any] = json.loads(raw)
        record["spend_limit_usd"] = Decimal(record["spend_limit_usd"])
        return cls(**record)


def usd_to_picodollars(usd: Decimal) -> int:
    """Whole picodollars, rounded up, so a cost is never undercounted."""
    return int((usd * _PICODOLLARS_PER_USD).to_integral_value(rounding=ROUND_CEILING))


def picodollars_to_usd(picodollars: int) -> str:
    """A decimal string, as every other money value in the API is."""
    if picodollars == 0:
        return "0"
    return format(Decimal(picodollars).scaleb(-12).normalize(), "f")


def new_grant_key() -> str:
    return f"{GRANT_KEY_PREFIX}{secrets.token_urlsafe(32)}"


def looks_like_grant_key(presented: str) -> bool:
    return presented.startswith(GRANT_KEY_PREFIX)


def _digest(grant_key: str) -> str:
    return hashlib.sha256(grant_key.encode("utf-8", "surrogateescape")).hexdigest()


def _store_call(
    method: Callable[_P, Awaitable[_R]],
) -> Callable[_P, Awaitable[_R]]:
    """Turn any Redis failure into RunGrantStoreUnavailableError."""

    @wraps(method)
    async def wrapper(*args: _P.args, **kwargs: _P.kwargs) -> _R:
        try:
            return await method(*args, **kwargs)
        except RedisError as error:
            logger.error("run grant store failed in %s: %s", method.__name__, error)
            raise RunGrantStoreUnavailableError(
                f"The run grant store is unavailable: {type(error).__name__}."
            ) from error

    return wrapper


class RunGrantStore:
    """Grants, their keys and their spend, in one Redis key prefix.

    Keys, all expiring at the grant's `expires_at`:

    - `<prefix>grant:<run_id>`: the grant record, as JSON
    - `<prefix>grant-key:<sha256 of key>`: the run id the key belongs to
    - `<prefix>grant-keys:<run_id>`: the set of the grant's key digests
    - `<prefix>grant-spend:<run_id>`: spend so far, in picodollars
    """

    def __init__(self, redis: Redis, prefix: str = DEFAULT_REDIS_PREFIX) -> None:
        self._redis: Redis = redis
        self._prefix: str = prefix

    def _grant(self, run_id: str) -> str:
        return f"{self._prefix}grant:{run_id}"

    def _key(self, digest: str) -> str:
        return f"{self._prefix}grant-key:{digest}"

    def _keys(self, run_id: str) -> str:
        return f"{self._prefix}grant-keys:{run_id}"

    def _spend(self, run_id: str) -> str:
        return f"{self._prefix}grant-spend:{run_id}"

    @_store_call
    async def ping(self) -> None:
        await self._redis.ping()

    @_store_call
    async def issue(self, requested: RunGrant) -> tuple[RunGrant, str, bool]:
        """Create the grant, or add a key to a live grant with the same terms.

        Returns:
            tuple[RunGrant, str, bool]: The grant in force, a new grant key
                for it, and whether this call created the grant.

        Raises:
            RunGrantConflictError: When a live grant for the run id has another
                issuer, engine, model or spend limit.
        """
        grant: RunGrant = requested
        created: bool = bool(
            await self._redis.set(
                self._grant(grant.run_id),
                grant.to_json(),
                nx=True,
                exat=grant.expires_at,
            )
        )
        if not created:
            existing: RunGrant | None = await self.get(grant.run_id)
            if existing is None:
                # Expired or revoked between the two calls. One more try is
                # enough: a second miss means something else is writing it.
                created = bool(
                    await self._redis.set(
                        self._grant(grant.run_id),
                        grant.to_json(),
                        nx=True,
                        exat=grant.expires_at,
                    )
                )
                existing = None if created else await self.get(grant.run_id)
            if not created:
                if existing is None or not existing.same_terms(requested):
                    raise RunGrantConflictError(
                        f"Run {requested.run_id!r} already has a grant with other "
                        f"terms or another issuer. Use a new run_id, or revoke "
                        f"the grant first."
                    )
                grant = existing

        grant_key: str = new_grant_key()
        digest: str = _digest(grant_key)
        async with self._redis.pipeline(transaction=True) as pipe:
            pipe.set(self._key(digest), grant.run_id, exat=grant.expires_at)
            pipe.sadd(self._keys(grant.run_id), digest)
            pipe.expireat(self._keys(grant.run_id), grant.expires_at)
            await pipe.execute()
        return grant, grant_key, created

    @_store_call
    async def get(self, run_id: str) -> RunGrant | None:
        """The live grant for a run, or None when there is none or it expired."""
        raw: str | bytes | None = await self._redis.get(self._grant(run_id))
        if raw is None:
            return None
        grant: RunGrant = RunGrant.from_json(
            raw.decode("utf-8") if isinstance(raw, bytes) else raw
        )
        return None if grant.expired() else grant

    @_store_call
    async def find(self, grant_key: str) -> RunGrant | None:
        """The live grant a key belongs to, or None for an unknown, revoked or expired key."""
        run_id: str | bytes | None = await self._redis.get(
            self._key(_digest(grant_key))
        )
        if run_id is None:
            return None
        return await self.get(
            run_id.decode("utf-8") if isinstance(run_id, bytes) else run_id
        )

    @_store_call
    async def spent_picodollars(self, run_id: str) -> int:
        raw: str | bytes | None = await self._redis.get(self._spend(run_id))
        return int(raw) if raw is not None else 0

    @_store_call
    async def add_spend(self, grant: RunGrant, picodollars: int) -> int:
        """Add a turn's cost and return the run's new total."""
        async with self._redis.pipeline(transaction=True) as pipe:
            pipe.incrby(self._spend(grant.run_id), picodollars)
            pipe.expireat(self._spend(grant.run_id), grant.expires_at)
            total, _ = await pipe.execute()
        return int(total)

    @_store_call
    async def revoke(self, run_id: str) -> tuple[bool, int]:
        """End the grant and every key it has.

        Returns:
            tuple[bool, int]: Whether a grant was live, and what the run spent.
        """
        digests: set[str | bytes] = await self._redis.smembers(self._keys(run_id))
        spent: int = await self.spent_picodollars(run_id)
        async with self._redis.pipeline(transaction=True) as pipe:
            pipe.delete(self._grant(run_id))
            for digest in digests:
                text: str = (
                    digest.decode("utf-8") if isinstance(digest, bytes) else digest
                )
                pipe.delete(self._key(text))
            pipe.delete(self._keys(run_id))
            pipe.delete(self._spend(run_id))
            deleted: list[int] = await pipe.execute()
        return bool(deleted[0]), spent


def grant_ttl_end(ttl_seconds: int, now: float | None = None) -> int:
    """The epoch second a grant issued now with this lifetime expires."""
    return math.ceil((time.time() if now is None else now) + ttl_seconds)


def store_from_env() -> RunGrantStore | None:
    """A store for HTTP_REDIS_URL, or None when run grants are not configured."""
    url: str = os.environ.get(REDIS_URL_ENV, "").strip()
    if not url:
        return None
    prefix: str = os.environ.get(REDIS_PREFIX_ENV, DEFAULT_REDIS_PREFIX)
    redis: Redis = Redis.from_url(
        url,
        decode_responses=True,
        socket_timeout=_REDIS_TIMEOUT_SECONDS,
        socket_connect_timeout=_REDIS_TIMEOUT_SECONDS,
    )
    return RunGrantStore(redis, prefix)


_UNSET: Final[object] = object()


def run_grant_store(app: Any) -> RunGrantStore | None:
    """The app's store, built from the environment on first use.

    Kept on `app.state`, so each app has its own and a test can put a store
    there before the first request.
    """
    store: Any = getattr(app.state, "run_grant_store", _UNSET)
    if store is _UNSET:
        store = store_from_env()
        app.state.run_grant_store = store
    return store
