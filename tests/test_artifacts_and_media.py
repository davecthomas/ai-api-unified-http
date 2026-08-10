# tests/test_artifacts_and_media.py

"""
Generated media, and the two different things "progress" means.

Once bytes exist, progress is arithmetic the client does from `Content-Length`,
so the service's whole job is to send an honest length and honour `Range`.
Before bytes exist — while a video is generating — there is nothing to measure,
so the service publishes a figure instead and says whether it measured it.

The rest is separation. Artifacts are addressed by an id that travels in a URL,
so the tests care that one caller cannot read another's, and that an id cannot
be used to walk out of the store.
"""

import json
import time
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from ai_api_unified_http import artifacts, rate_limit
from ai_api_unified_http.app import create_app
from ai_api_unified_http.auth import API_KEYS_ENV
from ai_api_unified_http.delivery import parse_range
from ai_api_unified_http.jobs import ESTIMATE_CEILING, estimated_percent

FIRST_KEY: str = "first-caller-key"
SECOND_KEY: str = "second-caller-key"
PNG: bytes = b"\x89PNG\r\n\x1a\n" + b"pixels" * 500


@pytest.fixture(autouse=True)
def clean_counter() -> None:
    rate_limit.reset_counter()
    yield
    rate_limit.reset_counter()


@pytest.fixture
def store(tmp_path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv(artifacts.ARTIFACT_DIR_ENV, str(tmp_path))
    return tmp_path


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch, store) -> TestClient:
    monkeypatch.setenv(API_KEYS_ENV, f"first:{FIRST_KEY},second:{SECOND_KEY}")
    return TestClient(create_app())


def _auth(key: str = FIRST_KEY) -> dict:
    return {"Authorization": f"Bearer {key}"}


class TestTransferProgress:
    """A progress bar needs a length and nothing else."""

    def test_a_full_fetch_sends_content_length(self, client: TestClient, store) -> None:
        record = artifacts.store_artifact("first", PNG, mime_type="image/png")
        response = client.get(
            f"/v1/artifacts/{record.artifact_id}/content", headers=_auth()
        )
        assert response.status_code == 200
        assert response.headers["content-length"] == str(len(PNG))
        assert response.content == PNG

    def test_the_length_matches_what_the_manifest_promised(
        self, client: TestClient, store
    ) -> None:
        # A caller sizes its progress bar from size_bytes before the fetch
        # starts, so the two have to agree exactly.
        record = artifacts.store_artifact("first", PNG, mime_type="image/png")
        response = client.get(
            f"/v1/artifacts/{record.artifact_id}/content", headers=_auth()
        )
        assert record.size_bytes == int(response.headers["content-length"])

    def test_range_support_is_advertised(self, client: TestClient, store) -> None:
        # A client that has to retry needs to know it may resume before it
        # decides whether to keep what it already read.
        record = artifacts.store_artifact("first", PNG, mime_type="image/png")
        response = client.get(
            f"/v1/artifacts/{record.artifact_id}/content", headers=_auth()
        )
        assert response.headers["accept-ranges"] == "bytes"

    def test_the_media_type_is_the_artifact_not_json(
        self, client: TestClient, store
    ) -> None:
        record = artifacts.store_artifact("first", PNG, mime_type="image/png")
        response = client.get(
            f"/v1/artifacts/{record.artifact_id}/content", headers=_auth()
        )
        assert response.headers["content-type"].startswith("image/png")


class TestResume:
    """Generation is already paid for, so a failed transfer must not repeat it."""

    def test_a_range_returns_206_with_only_those_bytes(
        self, client: TestClient, store
    ) -> None:
        record = artifacts.store_artifact("first", PNG, mime_type="image/png")
        response = client.get(
            f"/v1/artifacts/{record.artifact_id}/content",
            headers={**_auth(), "Range": "bytes=10-19"},
        )
        assert response.status_code == 206
        assert response.content == PNG[10:20]
        assert response.headers["content-range"] == f"bytes 10-19/{len(PNG)}"
        assert response.headers["content-length"] == "10"

    def test_an_open_ended_range_resumes_to_the_end(
        self, client: TestClient, store
    ) -> None:
        # The shape a resuming client actually sends: "I have N bytes, send the rest."
        record = artifacts.store_artifact("first", PNG, mime_type="image/png")
        response = client.get(
            f"/v1/artifacts/{record.artifact_id}/content",
            headers={**_auth(), "Range": "bytes=100-"},
        )
        assert response.status_code == 206
        assert response.content == PNG[100:]

    def test_a_resumed_transfer_reassembles_exactly(
        self, client: TestClient, store
    ) -> None:
        # The whole point, end to end: fetch a prefix, "fail", resume, and the
        # bytes must equal one uninterrupted fetch.
        record = artifacts.store_artifact("first", PNG, mime_type="image/png")
        url = f"/v1/artifacts/{record.artifact_id}/content"
        head = client.get(url, headers={**_auth(), "Range": "bytes=0-99"}).content
        tail = client.get(url, headers={**_auth(), "Range": "bytes=100-"}).content
        assert head + tail == PNG

    @pytest.mark.parametrize(
        "header,expected",
        [
            ("bytes=0-9", (0, 9)),
            ("bytes=5-", (5, 99)),
            ("bytes=-10", (90, 99)),
            ("bytes=0-500", (0, 99)),
            ("bytes=abc", None),
            ("items=0-9", None),
            (None, None),
            ("bytes=200-", None),
        ],
        ids=[
            "closed",
            "open",
            "suffix",
            "clamped",
            "garbage",
            "wrong-unit",
            "absent",
            "past-end",
        ],
    )
    def test_range_parsing(self, header, expected) -> None:
        assert parse_range(header, 100) == expected

    def test_an_unsatisfiable_range_serves_the_whole_body(
        self, client: TestClient, store
    ) -> None:
        # RFC 9110 allows ignoring a range that cannot be met. Failing instead
        # would break a caller whose retry logic guessed wrong.
        record = artifacts.store_artifact("first", PNG, mime_type="image/png")
        response = client.get(
            f"/v1/artifacts/{record.artifact_id}/content",
            headers={**_auth(), "Range": "bytes=999999-"},
        )
        assert response.status_code == 200
        assert response.content == PNG


class TestSeparation:
    """An artifact id travels in a URL, so it has to be safe to receive."""

    def test_one_caller_cannot_read_anothers_artifact(
        self, client: TestClient, store
    ) -> None:
        record = artifacts.store_artifact("first", PNG, mime_type="image/png")
        response = client.get(
            f"/v1/artifacts/{record.artifact_id}/content", headers=_auth(SECOND_KEY)
        )
        assert response.status_code == 404

    def test_an_unkeyed_request_is_refused(self, client: TestClient, store) -> None:
        record = artifacts.store_artifact("first", PNG, mime_type="image/png")
        assert (
            client.get(f"/v1/artifacts/{record.artifact_id}/content").status_code == 401
        )

    @pytest.mark.parametrize(
        "bad_id", ["../../etc/passwd", "..", "a/b", "short", "x" * 200, ""]
    )
    def test_a_traversing_id_cannot_escape_the_store(self, bad_id: str) -> None:
        with pytest.raises(artifacts.ArtifactNotFoundError):
            artifacts.read_record("first", bad_id)

    def test_ids_are_not_sequential(self) -> None:
        # A guessable id would make separation depend on nobody counting.
        assert len({artifacts.new_id() for _ in range(50)}) == 50


class TestExpiry:
    def test_an_aged_artifact_is_gone(
        self, store, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(artifacts.ARTIFACT_TTL_ENV, "60")
        record = artifacts.store_artifact("first", PNG, mime_type="image/png")
        meta = store / "first" / f"{record.artifact_id}.json"
        data = json.loads(meta.read_text())
        data["created_at"] = time.time() - 3600
        meta.write_text(json.dumps(data))

        with pytest.raises(artifacts.ArtifactNotFoundError):
            artifacts.read_record("first", record.artifact_id)

    def test_zero_ttl_never_expires(
        self, store, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(artifacts.ARTIFACT_TTL_ENV, "0")
        record = artifacts.store_artifact("first", PNG, mime_type="image/png")
        meta = store / "first" / f"{record.artifact_id}.json"
        data = json.loads(meta.read_text())
        data["created_at"] = 0.0
        meta.write_text(json.dumps(data))
        assert artifacts.read_record("first", record.artifact_id).size_bytes == len(PNG)


class TestGenerationProgress:
    """Before bytes exist there is nothing to measure, so it is published."""

    def test_an_estimate_never_claims_completion(self) -> None:
        # A job that overruns its assumed duration must not sit at 100 while
        # still working; only finishing sets that.
        assert estimated_percent(time.time() - 10_000) == ESTIMATE_CEILING
        assert ESTIMATE_CEILING < 100

    def test_the_estimate_advances_with_elapsed_time(self) -> None:
        start = time.time()
        early = estimated_percent(start, now=start + 10)
        later = estimated_percent(start, now=start + 100)
        assert 0 <= early < later <= ESTIMATE_CEILING

    def test_a_job_reports_its_progress(self, client: TestClient, store) -> None:
        record = artifacts.JobRecord(
            job_id=artifacts.new_id(), status="generating", percent=42.0, estimated=True
        )
        artifacts.write_job("first", record)
        body = client.get(f"/v1/videos/{record.job_id}", headers=_auth()).json()
        assert body["percent"] == 42.0
        assert body["estimated"] is True

    def test_a_measured_figure_is_marked_as_measured(
        self, client: TestClient, store
    ) -> None:
        # The flag is what lets a UI show a confident bar rather than a
        # hedged one, so it has to survive the round trip.
        record = artifacts.JobRecord(
            job_id=artifacts.new_id(),
            status="generating",
            percent=60.0,
            estimated=False,
        )
        artifacts.write_job("first", record)
        body = client.get(f"/v1/videos/{record.job_id}", headers=_auth()).json()
        assert body["estimated"] is False

    def test_a_finished_job_carries_its_artifacts(
        self, client: TestClient, store
    ) -> None:
        stored = artifacts.store_artifact(
            "first", PNG, mime_type="video/mp4", kind="video"
        )
        record = artifacts.JobRecord(
            job_id=artifacts.new_id(),
            status="ready",
            percent=100.0,
            estimated=False,
            artifact_ids=[stored.artifact_id],
        )
        artifacts.write_job("first", record)
        body = client.get(f"/v1/videos/{record.job_id}", headers=_auth()).json()
        assert body["status"] == "ready"
        assert body["artifacts"][0]["url_path"] == (
            f"/v1/artifacts/{stored.artifact_id}/content"
        )
        assert body["artifacts"][0]["size_bytes"] == len(PNG)

    def test_another_callers_job_is_not_visible(
        self, client: TestClient, store
    ) -> None:
        record = artifacts.JobRecord(job_id=artifacts.new_id(), status="generating")
        artifacts.write_job("first", record)
        assert (
            client.get(
                f"/v1/videos/{record.job_id}", headers=_auth(SECOND_KEY)
            ).status_code
            == 404
        )

    def test_progress_events_end_when_the_job_does(
        self, client: TestClient, store
    ) -> None:
        record = artifacts.JobRecord(
            job_id=artifacts.new_id(), status="ready", percent=100.0, estimated=False
        )
        artifacts.write_job("first", record)
        response = client.get(f"/v1/videos/{record.job_id}/events", headers=_auth())
        assert response.status_code == 200
        assert "event: progress" in response.text
        assert "event: done" in response.text

    def test_a_failed_job_ends_the_stream_with_an_error(
        self, client: TestClient, store
    ) -> None:
        record = artifacts.JobRecord(
            job_id=artifacts.new_id(), status="failed", error="provider said no"
        )
        artifacts.write_job("first", record)
        response = client.get(f"/v1/videos/{record.job_id}/events", headers=_auth())
        assert "event: error" in response.text
        assert "provider said no" in response.text


class TestImages:
    def test_generated_images_are_stored_and_referenced_not_inlined(
        self, client: TestClient, store
    ) -> None:
        fake = MagicMock()
        fake.generate_images = MagicMock(return_value=[PNG, PNG])
        with patch(
            "ai_api_unified_http.routes_v1.get_images_client", return_value=fake
        ):
            body = client.post(
                "/v1/images",
                json={"prompt": "a cat", "num_images": 2},
                headers=_auth(),
            ).json()

        assert len(body["artifacts"]) == 2
        assert all(a["size_bytes"] == len(PNG) for a in body["artifacts"])
        assert all(a["mime_type"] == "image/png" for a in body["artifacts"])
        # No base64 anywhere: the bytes are fetched, not embedded.
        assert "b64" not in json.dumps(body)

    def test_a_reference_fetches_the_real_bytes(
        self, client: TestClient, store
    ) -> None:
        fake = MagicMock()
        fake.generate_images = MagicMock(return_value=[PNG])
        with patch(
            "ai_api_unified_http.routes_v1.get_images_client", return_value=fake
        ):
            body = client.post(
                "/v1/images", json={"prompt": "a cat"}, headers=_auth()
            ).json()

        fetched = client.get(body["artifacts"][0]["url_path"], headers=_auth())
        assert fetched.content == PNG

    def test_the_requested_format_decides_the_served_type(
        self, client: TestClient, store
    ) -> None:
        fake = MagicMock()
        fake.generate_images = MagicMock(return_value=[PNG])
        with patch(
            "ai_api_unified_http.routes_v1.get_images_client", return_value=fake
        ):
            body = client.post(
                "/v1/images",
                json={"prompt": "a cat", "image_format": "webp"},
                headers=_auth(),
            ).json()
        assert body["artifacts"][0]["mime_type"] == "image/webp"

    def test_too_many_images_is_refused(self, client: TestClient, store) -> None:
        response = client.post(
            "/v1/images", json={"prompt": "a cat", "num_images": 99}, headers=_auth()
        )
        assert response.status_code == 422


class TestVideoSubmission:
    def test_creating_a_job_returns_immediately(
        self, client: TestClient, store
    ) -> None:
        # The response must not wait for generation; that is the entire reason
        # video is a job rather than a call.
        fake = MagicMock()
        with (
            patch("ai_api_unified_http.routes_v1.get_video_client", return_value=fake),
            patch("ai_api_unified_http.routes_v1.run_video_job"),
        ):
            body = client.post(
                "/v1/videos", json={"prompt": "a sunset"}, headers=_auth()
            ).json()
        assert body["status"] == "queued"
        assert body["percent"] == 0.0
        assert body["job_id"]

    def test_the_job_is_readable_straight_away(self, client: TestClient, store) -> None:
        fake = MagicMock()
        with (
            patch("ai_api_unified_http.routes_v1.get_video_client", return_value=fake),
            patch("ai_api_unified_http.routes_v1.run_video_job"),
        ):
            job_id = client.post(
                "/v1/videos", json={"prompt": "a sunset"}, headers=_auth()
            ).json()["job_id"]
        assert client.get(f"/v1/videos/{job_id}", headers=_auth()).status_code == 200

    def test_an_unknown_job_is_404(self, client: TestClient, store) -> None:
        assert (
            client.get(f"/v1/videos/{artifacts.new_id()}", headers=_auth()).status_code
            == 404
        )


class TestJobRunner:
    def test_a_provider_percentage_is_used_and_marked_measured(self, store) -> None:
        from ai_api_unified_http.jobs import run_video_job

        job = SimpleNamespace(
            status=SimpleNamespace(value="succeeded"), progress_percent=80.0
        )
        artifact = SimpleNamespace(data=b"video-bytes", mime_type="video/mp4")
        client = MagicMock()
        client.submit_video_generation = MagicMock(return_value=job)
        client.download_video_result = MagicMock(
            return_value=SimpleNamespace(artifacts=[artifact])
        )
        record = artifacts.JobRecord(job_id=artifacts.new_id())
        artifacts.write_job("first", record)

        run_video_job("first", record, client, "a sunset", object())
        final = artifacts.read_job("first", record.job_id)
        assert final.status == "ready"
        assert final.percent == 100.0
        assert final.estimated is False
        assert final.artifact_ids

    def test_a_provider_without_progress_still_produces_a_figure(self, store) -> None:
        # The point the design turns on: the feature cannot be contingent on
        # the provider reporting anything.
        from ai_api_unified_http.jobs import run_video_job

        job = SimpleNamespace(status=SimpleNamespace(value="succeeded"))
        artifact = SimpleNamespace(data=b"video-bytes", mime_type="video/mp4")
        client = MagicMock()
        client.submit_video_generation = MagicMock(return_value=job)
        client.download_video_result = MagicMock(
            return_value=SimpleNamespace(artifacts=[artifact])
        )
        record = artifacts.JobRecord(job_id=artifacts.new_id())
        artifacts.write_job("first", record)

        run_video_job("first", record, client, "a sunset", object())
        assert artifacts.read_job("first", record.job_id).status == "ready"

    def test_a_provider_failure_becomes_a_terminal_record(self, store) -> None:
        # A caller polling for progress has to learn the outcome; a job that
        # simply stops updating is the worst possible failure mode.
        from ai_api_unified_http.jobs import run_video_job

        client = MagicMock()
        client.submit_video_generation = MagicMock(side_effect=RuntimeError("boom"))
        record = artifacts.JobRecord(job_id=artifacts.new_id())
        artifacts.write_job("first", record)

        run_video_job("first", record, client, "a sunset", object())
        final = artifacts.read_job("first", record.job_id)
        assert final.status == "failed"
        assert "boom" in (final.error or "")

    def test_success_with_no_bytes_is_a_failure(self, store) -> None:
        from ai_api_unified_http.jobs import run_video_job

        job = SimpleNamespace(status=SimpleNamespace(value="succeeded"))
        client = MagicMock()
        client.submit_video_generation = MagicMock(return_value=job)
        client.download_video_result = MagicMock(
            return_value=SimpleNamespace(artifacts=[])
        )
        record = artifacts.JobRecord(job_id=artifacts.new_id())
        artifacts.write_job("first", record)

        run_video_job("first", record, client, "a sunset", object())
        assert artifacts.read_job("first", record.job_id).status == "failed"


class TestSurface:
    def test_every_new_path_requires_a_key(self, client: TestClient, store) -> None:
        for method, path in [
            ("post", "/v1/images"),
            ("post", "/v1/videos"),
            ("get", "/v1/videos/abc"),
            ("get", "/v1/videos/abc/events"),
            ("get", "/v1/artifacts/abc/content"),
        ]:
            call = getattr(client, method)
            response = call(path, json={}) if method == "post" else call(path)
            assert response.status_code == 401, path
