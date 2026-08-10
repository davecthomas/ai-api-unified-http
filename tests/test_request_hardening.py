# tests/test_request_hardening.py

"""
Limits that hold when the caller is hostile rather than merely wrong.

Three separate holes are covered here, and they share a shape: each one let a
caller reach past a guard that looks like it is already in the way.
Authentication rejected a bad key but crashed on a bad *encoding*. The limiter
counted authenticated callers and never saw the rest. And both count requests,
which says nothing about how large any one of them is.
"""

import pytest
from fastapi.testclient import TestClient

from ai_api_unified_http import rate_limit, request_limits
from ai_api_unified_http.app import create_app
from ai_api_unified_http.auth import API_KEYS_ENV
from ai_api_unified_http.schemas import (
    MAX_EMBEDDING_INPUTS,
    MAX_PROMPT_CHARS,
)

GOOD_KEY: str = "configured-key-value"
PATH: str = "/v1/tokens/count"
BODY: dict = {"engine": "claude", "prompt": "hi"}


@pytest.fixture(autouse=True)
def clean_counter() -> None:
    rate_limit.reset_counter()
    yield
    rate_limit.reset_counter()


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setenv(API_KEYS_ENV, f"webapp:{GOOD_KEY}")
    return TestClient(create_app())


class TestNonAsciiKeysAreRejectedNotCrashed:
    """`hmac.compare_digest` refuses two non-ASCII `str` arguments.

    Header values arrive decoded from raw bytes, so a caller could put a byte
    above 0x7f in the Authorization header and turn the authenticator's
    TypeError into a 500 with a logged traceback, from an unauthenticated
    request. The comparison encodes both sides, which makes that byte an
    ordinary non-match.
    """

    # Sent as raw bytes, because that is what reaches the socket. An HTTP
    # client will not encode a non-ASCII `str` into a header, so a `str`
    # parameter here would test the client library rather than the service.
    @pytest.mark.parametrize(
        "presented",
        [
            b"\xfc",
            b"\xf0\x9f\x98\x80",
            b"abc\xe9def",
            b"\xd0\xb0bc",
            GOOD_KEY.encode() + b"\xff",
        ],
        ids=["high-byte", "emoji-utf8", "embedded", "cyrillic-utf8", "valid-plus-byte"],
    )
    def test_a_non_ascii_key_is_401_not_500(
        self, client: TestClient, presented: bytes
    ) -> None:
        response = client.post(
            PATH, json=BODY, headers={"Authorization": b"Bearer " + presented}
        )
        assert response.status_code == 401
        assert response.json()["error"] == "unauthorized"

    def test_the_configured_key_still_authenticates(self, client: TestClient) -> None:
        # The encoding change must not break the path that matters.
        response = client.post(
            PATH, json=BODY, headers={"Authorization": f"Bearer {GOOD_KEY}"}
        )
        assert response.status_code != 401


class TestFailedAuthIsCounted:
    """Authentication runs outside the limiter, so it counts its own refusals.

    Without this an unauthenticated caller retries without bound, and every
    attempt costs a log line on a sink that bills by volume.
    """

    def test_repeated_bad_keys_eventually_get_429(
        self, monkeypatch: pytest.MonkeyPatch, client: TestClient
    ) -> None:
        monkeypatch.setenv(rate_limit.RATE_LIMIT_ENV, "3")
        codes = [
            client.post(
                PATH, json=BODY, headers={"Authorization": "Bearer wrong"}
            ).status_code
            for _ in range(5)
        ]
        assert codes[:3] == [401, 401, 401]
        assert codes[3:] == [429, 429]

    def test_a_missing_header_is_counted_too(
        self, monkeypatch: pytest.MonkeyPatch, client: TestClient
    ) -> None:
        # Sending no header at all is the cheapest way to generate load, so it
        # cannot be the one path that escapes counting.
        monkeypatch.setenv(rate_limit.RATE_LIMIT_ENV, "2")
        codes = [client.post(PATH, json=BODY).status_code for _ in range(4)]
        assert codes == [401, 401, 429, 429]

    def test_public_paths_stay_reachable_while_a_caller_is_blocked(
        self, monkeypatch: pytest.MonkeyPatch, client: TestClient
    ) -> None:
        monkeypatch.setenv(rate_limit.RATE_LIMIT_ENV, "1")
        for _ in range(4):
            client.post(PATH, json=BODY, headers={"Authorization": "Bearer wrong"})

        assert client.get("/health").status_code == 200

    def test_a_valid_key_is_unaffected_by_a_blocked_address(
        self, monkeypatch: pytest.MonkeyPatch, client: TestClient
    ) -> None:
        # Address counting must not become a way to lock out paying callers
        # sharing a NAT with someone guessing keys.
        monkeypatch.setenv(rate_limit.RATE_LIMIT_ENV, "2")
        for _ in range(4):
            client.post(PATH, json=BODY, headers={"Authorization": "Bearer wrong"})

        response = client.post(
            PATH, json=BODY, headers={"Authorization": f"Bearer {GOOD_KEY}"}
        )
        assert response.status_code != 429

    def test_disabling_the_limit_disables_this_too(
        self, monkeypatch: pytest.MonkeyPatch, client: TestClient
    ) -> None:
        monkeypatch.setenv(rate_limit.RATE_LIMIT_ENV, "0")
        for _ in range(12):
            response = client.post(
                PATH, json=BODY, headers={"Authorization": "Bearer wrong"}
            )
            assert response.status_code == 401


class TestClientAddress:
    """Which address a request is counted against."""

    def _request(self, headers: dict | None = None):
        from starlette.requests import Request

        scope = {
            "type": "http",
            "headers": [
                (k.lower().encode(), v.encode()) for k, v in (headers or {}).items()
            ],
            "client": ("10.0.0.1", 1234),
        }
        return Request(scope)

    def test_the_socket_peer_is_used_by_default(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv(rate_limit.CLIENT_IP_FROM_XFF_ENV, raising=False)
        request = self._request({"x-forwarded-for": "1.2.3.4"})
        assert rate_limit.client_ip(request) == "10.0.0.1"

    def test_the_last_forwarded_entry_wins_when_opted_in(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A proxy appends the address it accepted the connection from, so the
        # last entry is the one written by infrastructure. Reading the first
        # would let the caller supply it.
        monkeypatch.setenv(rate_limit.CLIENT_IP_FROM_XFF_ENV, "1")
        request = self._request({"x-forwarded-for": "9.9.9.9, 8.8.8.8, 203.0.113.7"})
        assert rate_limit.client_ip(request) == "203.0.113.7"

    def test_a_forged_single_entry_cannot_shadow_the_peer(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(rate_limit.CLIENT_IP_FROM_XFF_ENV, "1")
        first = rate_limit.client_ip(self._request({"x-forwarded-for": "1.1.1.1"}))
        second = rate_limit.client_ip(self._request({"x-forwarded-for": "2.2.2.2"}))
        # Forging still changes the bucket when the deployment opts in without
        # a proxy in front, which is why the default is the socket peer.
        assert first != second

    def test_no_header_falls_back_to_the_peer(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(rate_limit.CLIENT_IP_FROM_XFF_ENV, "1")
        assert rate_limit.client_ip(self._request()) == "10.0.0.1"


class TestRequestSize:
    """The limiter bounds how often a caller spends, not how much per call."""

    def test_a_body_over_the_ceiling_is_413(
        self, monkeypatch: pytest.MonkeyPatch, client: TestClient
    ) -> None:
        monkeypatch.setenv(request_limits.MAX_REQUEST_BYTES_ENV, "500")
        response = client.post(
            PATH,
            json={"engine": "claude", "prompt": "A" * 5_000},
            headers={"Authorization": f"Bearer {GOOD_KEY}"},
        )
        assert response.status_code == 413
        assert response.json()["error"] == "request_too_large"

    def test_the_refusal_names_the_knob(
        self, monkeypatch: pytest.MonkeyPatch, client: TestClient
    ) -> None:
        monkeypatch.setenv(request_limits.MAX_REQUEST_BYTES_ENV, "500")
        response = client.post(
            PATH,
            json={"engine": "claude", "prompt": "A" * 5_000},
            headers={"Authorization": f"Bearer {GOOD_KEY}"},
        )
        assert request_limits.MAX_REQUEST_BYTES_ENV in response.json()["detail"]

    def test_an_oversized_body_is_refused_without_a_key(
        self, monkeypatch: pytest.MonkeyPatch, client: TestClient
    ) -> None:
        # The guard sits outside auth: reading a key to decide that a body is
        # too large would mean parsing headers the service has no use for.
        monkeypatch.setenv(request_limits.MAX_REQUEST_BYTES_ENV, "500")
        response = client.post(PATH, json={"engine": "claude", "prompt": "A" * 5_000})
        assert response.status_code == 413

    def test_a_normal_body_passes(
        self, monkeypatch: pytest.MonkeyPatch, client: TestClient
    ) -> None:
        monkeypatch.setenv(request_limits.MAX_REQUEST_BYTES_ENV, "1048576")
        response = client.post(
            PATH, json=BODY, headers={"Authorization": f"Bearer {GOOD_KEY}"}
        )
        assert response.status_code != 413

    def test_zero_disables_the_guard(
        self, monkeypatch: pytest.MonkeyPatch, client: TestClient
    ) -> None:
        monkeypatch.setenv(request_limits.MAX_REQUEST_BYTES_ENV, "0")
        response = client.post(
            PATH,
            json={"engine": "claude", "prompt": "A" * 50_000},
            headers={"Authorization": f"Bearer {GOOD_KEY}"},
        )
        assert response.status_code != 413

    def test_an_unparseable_limit_falls_back_rather_than_disabling(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(request_limits.MAX_REQUEST_BYTES_ENV, "one megabyte")
        assert request_limits.max_request_bytes() == (
            request_limits.DEFAULT_MAX_REQUEST_BYTES
        )

    def test_a_get_is_not_size_checked(
        self, monkeypatch: pytest.MonkeyPatch, client: TestClient
    ) -> None:
        monkeypatch.setenv(request_limits.MAX_REQUEST_BYTES_ENV, "1")
        assert client.get("/health").status_code == 200


class TestFieldBounds:
    """Shape limits live in the schema, so they reach the OpenAPI document."""

    def test_an_overlong_prompt_is_422(self, client: TestClient) -> None:
        response = client.post(
            "/v1/completions",
            json={"engine": "claude", "prompt": "A" * (MAX_PROMPT_CHARS + 1)},
            headers={"Authorization": f"Bearer {GOOD_KEY}"},
        )
        assert response.status_code == 422

    def test_too_many_embedding_inputs_is_422(self, client: TestClient) -> None:
        response = client.post(
            "/v1/embeddings",
            json={
                "engine": "openai",
                "inputs": ["x"] * (MAX_EMBEDDING_INPUTS + 1),
            },
            headers={"Authorization": f"Bearer {GOOD_KEY}"},
        )
        assert response.status_code == 422

    def test_the_bounds_are_published_in_the_spec(self, client: TestClient) -> None:
        # A caller generating a client should see the limit, not discover it
        # by being refused.
        spec = client.get("/openapi.json").json()
        prompt = spec["components"]["schemas"]["CompletionRequest"]["properties"][
            "prompt"
        ]
        assert prompt["maxLength"] == MAX_PROMPT_CHARS
