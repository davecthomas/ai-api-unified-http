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
from fastapi.middleware.cors import CORSMiddleware
from fastapi.testclient import TestClient

from ai_api_unified_http.errors import (
    EXCEPTION_HANDLERS,
    ErrorEnvelopeMiddleware,
    ProviderNotConfiguredError,
    missing_variable_from,
)


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
    "not_configured": ProviderNotConfiguredError(
        "OPENAI_API_KEY environment variable must be set.",
        engine="openai",
        missing_variable="OPENAI_API_KEY",
    ),
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


class TestProviderNotConfigured:
    """A missing credential is the deployment's problem, not the caller's."""

    def test_missing_variable_is_lifted_from_the_message(self) -> None:
        # Provider SDKs report this as a plain ValueError naming the variable;
        # the name is the only actionable part.
        error = ValueError("ANTHROPIC_API_KEY environment variable must be set.")
        assert missing_variable_from(error) == "ANTHROPIC_API_KEY"

    def test_a_message_with_no_variable_yields_none(self) -> None:
        assert missing_variable_from(ValueError("something went wrong")) is None

    def test_maps_to_503_and_names_the_fix(self, client: TestClient) -> None:
        # 503, not 4xx: the caller did nothing wrong and retrying the same
        # request will not help until an operator sets the variable.
        response = client.get("/boom/not_configured")
        assert response.status_code == 503
        body = response.json()
        assert body["error"] == "provider_not_configured"
        assert "OPENAI_API_KEY" in body["detail"]
        assert body["engine"] == "openai"


class TestErrorEnvelope:
    """Nothing may leave as a bare 500 with a traceback."""

    @pytest.fixture
    def enveloped(self) -> TestClient:
        app = FastAPI()
        for exception_type, handler in EXCEPTION_HANDLERS:
            app.add_exception_handler(exception_type, handler)
        app.add_middleware(ErrorEnvelopeMiddleware)

        @app.get("/kaboom")
        def kaboom() -> None:
            raise ValueError("something the service does not classify")

        @app.get("/fine")
        def fine() -> dict:
            return {"ok": True}

        return TestClient(app, raise_server_exceptions=False)

    def test_an_unclassified_error_becomes_the_service_error_shape(
        self, enveloped: TestClient
    ) -> None:
        response = enveloped.get("/kaboom")
        assert response.status_code == 500
        body = response.json()
        assert set(body) == {"error", "detail", "engine", "provider_status"}
        assert body["error"] == "internal_error"

    def test_the_traceback_never_reaches_the_caller(
        self, enveloped: TestClient
    ) -> None:
        # The body carries a request id instead, which is also on the log line
        # holding the traceback.
        body = enveloped.get("/kaboom").json()
        assert "Traceback" not in body["detail"]
        assert "ValueError" not in body["detail"]
        assert "request_id" in body["detail"]

    def test_successful_requests_pass_through_untouched(
        self, enveloped: TestClient
    ) -> None:
        assert enveloped.get("/fine").json() == {"ok": True}


def test_an_unclassified_error_still_carries_cors_headers() -> None:
    # This is the whole reason the envelope is middleware rather than an
    # exception handler. Starlette's server-error handler sits outside the CORS
    # middleware, so a bare 500 reaches a browser with no allow-origin header
    # and the caller sees an opaque network failure instead of the reason.
    app = FastAPI()
    app.add_middleware(ErrorEnvelopeMiddleware)
    app.add_middleware(
        CORSMiddleware, allow_origins=["http://localhost:3000"], allow_methods=["GET"]
    )

    @app.get("/kaboom")
    def kaboom() -> None:
        raise RuntimeError("unclassified")

    client = TestClient(app, raise_server_exceptions=False)
    response = client.get("/kaboom", headers={"Origin": "http://localhost:3000"})

    assert response.status_code == 500
    assert response.headers["access-control-allow-origin"] == "http://localhost:3000"
    assert response.json()["error"] == "internal_error"


class TestUnwrappedProviderFailures:
    """Some library paths let the provider SDK's own exception through.

    A bad batch id surfaces as `anthropic.BadRequestError`, which matches no
    registered handler, so it reached the caller as a 500 for what is plainly
    their own 400. Found by driving the batch endpoints from the browser
    console against a running service.
    """

    def test_a_provider_400_is_not_a_500(self) -> None:
        import httpx

        from ai_api_unified_http.errors import provider_status_of

        request = httpx.Request("GET", "https://example.invalid")
        response = httpx.Response(400, request=request, json={"error": "bad id"})

        class FakeSdkError(Exception):
            def __init__(self) -> None:
                super().__init__("Message Batch id must have `msgbatch_` prefix.")
                self.status_code = 400
                self.response = response

        assert provider_status_of(FakeSdkError()) == 400

    def test_an_ordinary_bug_still_becomes_a_500(self) -> None:
        # The detection has to be specific enough that a genuine defect is not
        # quietly reported to the caller as a provider problem.
        from ai_api_unified_http.errors import provider_status_of

        assert provider_status_of(ValueError("a real bug")) is None
        assert provider_status_of(KeyError("missing")) is None

    def test_a_status_without_a_response_is_not_a_provider_failure(self) -> None:
        from ai_api_unified_http.errors import provider_status_of

        class Coincidence(Exception):
            status_code = 400

        assert provider_status_of(Coincidence()) is None

    def test_a_non_integer_status_is_ignored(self) -> None:
        from ai_api_unified_http.errors import provider_status_of

        class Odd(Exception):
            status_code = "400"
            response = object()

        assert provider_status_of(Odd()) is None

    @pytest.mark.parametrize(
        "provider_status,expected",
        [(400, 400), (404, 400), (429, 429), (401, 502), (403, 502), (500, 502)],
    )
    def test_classification_matches_the_wrapped_path(
        self, provider_status: int, expected: int
    ) -> None:
        # An unwrapped failure must classify identically to the same failure
        # arriving wrapped, or the status a caller sees would depend on which
        # library path happened to raise it.
        from ai_api_unified_http.errors import _classify_provider_status

        assert _classify_provider_status(provider_status)[0] == expected
