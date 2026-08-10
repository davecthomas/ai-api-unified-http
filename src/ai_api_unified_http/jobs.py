# src/ai_api_unified_http/jobs.py

"""
Running a video generation to completion, and publishing progress while it does.

Video takes minutes, which is longer than a request should live, so the work is
started by one request and collected by later ones. That makes progress
something the service has to publish rather than something a caller can
measure: there are no bytes yet, so `Content-Length` — which is how progress
works once bytes exist — has nothing to describe.

**The estimate is the service's own.** A provider may report a percentage and
may not, and the same provider may report one for some models and not others.
Rather than making the feature contingent on that, the runner falls back to
elapsed time against an expected duration and marks the figure `estimated` so a
caller can render it honestly — a real bar when the number is measured, and a
bar that admits it is guessing otherwise. A UI that only ever gets an
indeterminate spinner is still a working UI; one that shows a confident 47%
derived from nothing is worse than no number.

Progress is written to the shared artifact store rather than held in memory,
because the instance polled for it is usually not the instance doing the work.
"""

import logging
import time
from typing import Any, Final

from .artifacts import (
    JobRecord,
    store_artifact,
    write_job,
)

logger: Final[logging.Logger] = logging.getLogger(__name__)

# How long a video generation is assumed to take when nothing better is known.
# Only used to shape the estimated curve; overrunning it parks the bar just
# below complete rather than letting it claim to be finished.
ASSUMED_DURATION_SECONDS: Final[float] = 180.0

# Ceiling for an estimated figure. A guess must never reach 100, because the
# only thing entitled to say a job is complete is the job completing.
ESTIMATE_CEILING: Final[float] = 95.0

# Gap between provider status polls.
POLL_INTERVAL_SECONDS: Final[float] = 5.0


def estimated_percent(started_at: float, now: float | None = None) -> float:
    """Return an elapsed-time estimate of completion, bounded below 100.

    Args:
        started_at: Unix time the job began.
        now: Current unix time; defaults to the clock.

    Returns:
        float: A percentage in [0, ESTIMATE_CEILING].
    """
    elapsed: float = max((now if now is not None else time.time()) - started_at, 0.0)
    fraction: float = min(elapsed / ASSUMED_DURATION_SECONDS, 1.0)
    return round(fraction * ESTIMATE_CEILING, 1)


def _reported_percent(job: Any) -> float | None:
    """Read a provider-reported percentage, when there is one.

    The attribute is absent on some providers and null on others, so this
    answers None for both rather than assuming the field exists.
    """
    value: Any = getattr(job, "progress_percent", None)
    if value is None:
        return None
    try:
        return max(0.0, min(float(value), 100.0))
    except (TypeError, ValueError):
        return None


def run_video_job(
    caller: str,
    record: JobRecord,
    client: Any,
    prompt: str,
    properties: Any,
) -> None:
    """Generate a video, publishing progress, then store the result.

    Runs on a worker thread for the whole generation. Every exit path writes a
    terminal record, so a caller polling for progress always learns the outcome
    instead of watching a job that stopped moving.

    Args:
        caller: API key label the job belongs to.
        record: The job record, already written as queued.
        client: Pooled video client.
        prompt: What to generate.
        properties: The library's video properties object.
    """
    started: float = time.time()
    record.status = "generating"
    write_job(caller, record)

    try:
        submitted: Any = client.submit_video_generation(prompt, properties)
        provider_job: Any = submitted

        while True:
            reported: float | None = _reported_percent(provider_job)
            record.percent = (
                reported if reported is not None else estimated_percent(started)
            )
            record.estimated = reported is None
            write_job(caller, record)

            status: str = str(
                getattr(getattr(provider_job, "status", None), "value", "")
            )
            if status in {"succeeded", "completed", "failed", "canceled", "cancelled"}:
                break

            time.sleep(POLL_INTERVAL_SECONDS)
            provider_job = client.get_video_generation_job(provider_job)

        if status in {"failed", "canceled", "cancelled"}:
            record.status = "failed"
            record.error = getattr(provider_job, "error_message", None) or (
                f"Provider reported the job as {status}."
            )
            write_job(caller, record)
            return

        result: Any = client.download_video_result(provider_job)
        for artifact in getattr(result, "artifacts", []) or []:
            data: bytes | None = getattr(artifact, "data", None) or getattr(
                artifact, "content", None
            )
            if not data:
                continue
            stored = store_artifact(
                caller,
                data,
                mime_type=getattr(artifact, "mime_type", None) or "video/mp4",
                kind="video",
                engine=record.engine,
                model=record.model,
            )
            record.artifact_ids.append(stored.artifact_id)

        if not record.artifact_ids:
            record.status = "failed"
            record.error = "The provider reported success but returned no video data."
        else:
            record.status = "ready"
            record.percent = 100.0
            record.estimated = False
        write_job(caller, record)

    except Exception as error:
        logger.exception("video job %s failed", record.job_id)
        record.status = "failed"
        record.error = str(error)
        write_job(caller, record)
