# tests/test_errors.py

"""
Provider failures must be classified by whose fault they are.

The rule under test: a caller who can fix the request gets a 4xx; a provider
that rate-limited or rejected the service's own credentials produces a 5xx,
because callers never hold provider keys and can do nothing about either.
"""

import pytest
from ai_api_unified import (
    AiProviderCapabilityUnsupportedError,
    AiProviderRequestError,
    StructuredResponseTokenLimitError,
)
from ai_api_unified.ai_provider_exceptions import (
    AiProviderConfigurationError,
    AiProviderDependencyUnavailableError,
    AiProviderError,
)
from fastapi import FastAPI
from fastapi.testclient import TestClient

from ai_api_unified_http.errors import EXCEPTION_HANDLERS


@pytest.fixture(scope="module")
def client() -> TestClient:
    """An app whose only job is to raise whatever the test asks for."""
    app = FastAPI()
    for exception_type, handler in EXCEPTION_HANDLERS:
        app.add_exception_handler(exception_type, handler)

    @app.get("/boom/{kind}")
    def boom(kind: str) -> None:
        raise _EXCEPTIONS[kind]

    # raise_server_exceptions=False so unhandled errors surface as responses
    # rather than propagating into the test, which is what a real client sees.
    return TestClient(app, raise_server_exceptions=False)


_EXCEPTIONS: dict[str, Exception] = {
    "rate_limited": AiProviderRequestError(
        "slow down", status_code=429, provider_engine="claude"
    ),
    "provider_500": AiProviderRequestError(
        "upstream exploded", status_code=500, provider_engine="openai"
    ),
    "provider_401": AiProviderRequestError(
        "bad key", status_code=401, provider_engine="openai"
    ),
    "provider_403": AiProviderRequestError(
        "forbidden", status_code=403, provider_engine="openai"
    ),
    "provider_400": AiProviderRequestError(
        "unknown model", status_code=400, provider_engine="google-gemini"
    ),
    "no_status": AiProviderRequestError("connection reset", provider_engine="claude"),
    "config": AiProviderConfigurationError("model retired"),
    "capability": AiProviderCapabilityUnsupportedError("engine cannot stream"),
    "dependency": AiProviderDependencyUnavailableError("boto3 not installed"),
    "token_limit": StructuredResponseTokenLimitError(
        message="response truncated",
        provider_name="openai",
        model_name="gpt-5.4-mini",
        max_response_tokens=16,
        minimum_supported_tokens=256,
    ),
    "base": AiProviderError("something else"),
}


@pytest.mark.parametrize(
    "kind,expected_status,expected_error",
    [
        ("rate_limited", 429, "provider_rate_limited"),
        ("provider_500", 502, "provider_error"),
        ("provider_400", 400, "provider_rejected_request"),
        ("no_status", 502, "provider_unavailable"),
        ("config", 400, "invalid_request"),
        ("capability", 400, "invalid_request"),
        ("dependency", 503, "provider_dependency_unavailable"),
        ("token_limit", 422, "structured_response_token_limit"),
        ("base", 502, "provider_error"),
    ],
)
def test_status_mapping(
    client: TestClient, kind: str, expected_status: int, expected_error: str
) -> None:
    response = client.get(f"/boom/{kind}")
    assert response.status_code == expected_status
    assert response.json()["error"] == expected_error


@pytest.mark.parametrize("kind", ["provider_401", "provider_403"])
def test_provider_auth_failure_is_never_the_callers_fault(
    client: TestClient, kind: str
) -> None:
    # Provider credentials live only in the service environment, so a 401 from
    # the provider is a service misconfiguration. Returning 401 to the caller
    # would tell them to fix an API key they do not hold.
    response = client.get(f"/boom/{kind}")
    assert response.status_code == 502
    assert response.json()["error"] == "provider_auth_failed"


def test_provider_status_is_reported_without_becoming_the_response_status(
    client: TestClient,
) -> None:
    response = client.get("/boom/provider_500")
    body = response.json()
    assert response.status_code == 502
    assert body["provider_status"] == 500
    assert body["engine"] == "openai"


def test_token_limit_detail_names_the_minimum_that_would_work(
    client: TestClient,
) -> None:
    # The library knows the floor for this model, so the caller should not
    # have to bisect max_response_tokens to find it.
    body = client.get("/boom/token_limit").json()
    assert "256" in body["detail"]
    assert body["engine"] == "openai"


def test_error_body_shape_is_uniform(client: TestClient) -> None:
    for kind in _EXCEPTIONS:
        body = client.get(f"/boom/{kind}").json()
        assert set(body) == {"error", "detail", "engine", "provider_status"}
        assert isinstance(body["error"], str) and body["error"]
        assert isinstance(body["detail"], str) and body["detail"]


def test_subclasses_are_registered_before_their_base() -> None:
    # AiProviderRequestError and the rest all inherit AiProviderError. If the
    # base were registered first, every failure would collapse to one 502.
    registered = [exception_type for exception_type, _ in EXCEPTION_HANDLERS]
    assert registered[-1] is AiProviderError
    for exception_type in registered[:-1]:
        if issubclass(exception_type, AiProviderError):
            assert registered.index(exception_type) < registered.index(AiProviderError)
