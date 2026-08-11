# src/ai_api_unified_http/artifacts.py

"""
Storage for generated images and video, and the job records that track them.

Generation is the expensive step. An image costs cents and a video can cost
dollars, so losing the bytes to a dropped connection and regenerating is paying
twice for one artifact. Everything here exists so a transfer can fail and be
retried for free.

**Artifacts outlive the request that made them.** They cannot live in the
container: Cloud Run's filesystem is in-memory, so a large video would consume
the instance's own memory budget, and with several instances and no session
affinity a retry would usually reach one that never held the bytes. The store
is therefore a directory that outlives any single instance — a Cloud Storage
bucket mounted as a path in production, an ordinary directory locally. Nothing
here knows which it is, because a mounted bucket is reached with `open()` like
any other path.

**Callers are separated.** A path is derived from the API key's label as well
as the artifact id, and reads take the label of the caller asking. One caller
therefore cannot read another's artifacts by holding an id, and ids are random
rather than sequential so they cannot be walked.

Job records live beside artifacts for the same reason artifacts do: a video job
started on one instance is polled from another, so its progress has to be
somewhere both can see.
"""

import json
import logging
import os
import re
import secrets
import time
from collections.abc import Iterator
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Final

# Root of the artifact store. In a deployment this is where the bucket is
# mounted; locally it is any writable directory.
ARTIFACT_DIR_ENV: Final[str] = "HTTP_ARTIFACT_DIR"
DEFAULT_ARTIFACT_DIR: Final[str] = "artifacts"

# How long an artifact is served before it is treated as gone. This is a floor,
# not the deletion mechanism: the bucket's own lifecycle rule does the deleting,
# because a service that scales to zero cannot be relied on to run a sweeper.
ARTIFACT_TTL_ENV: Final[str] = "HTTP_ARTIFACT_TTL_SECONDS"
DEFAULT_ARTIFACT_TTL_SECONDS: Final[int] = 86_400

# Read size when streaming a stored artifact out. Large enough that a big video
# is not a million syscalls, small enough that a chunk is never a memory
# problem and progress advances visibly.
CHUNK_BYTES: Final[int] = 256 * 1024

# Ids are generated here, but they also arrive from callers in URLs, so the
# shape is enforced on the way in. Without this a caller could put `..` in a
# path segment and read outside the store.
_SAFE_ID: Final[re.Pattern[str]] = re.compile(r"^[A-Za-z0-9_-]{8,64}$")

# Labels may hold dots, since an operator naming a key after a hostname is
# reasonable, so the pattern alone would admit "." and ".." — a label that
# would place the whole store one directory up. The id pattern rejects those by
# its length floor; this one has to rule them out by name.
_SAFE_LABEL: Final[re.Pattern[str]] = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")
_RESERVED_LABELS: Final[frozenset[str]] = frozenset({".", ".."})

logger: Final[logging.Logger] = logging.getLogger(__name__)


class ArtifactNotFoundError(LookupError):
    """Raised when an id resolves to nothing the caller may read."""


class ArtifactStoreUnavailableError(RuntimeError):
    """Raised when the store's root cannot be written to."""


def artifact_root() -> Path:
    """Return the configured root directory for stored artifacts."""
    return Path(os.environ.get(ARTIFACT_DIR_ENV, DEFAULT_ARTIFACT_DIR))


def artifact_ttl_seconds() -> int:
    """Return how long an artifact is considered readable."""
    raw: str = os.environ.get(
        ARTIFACT_TTL_ENV, str(DEFAULT_ARTIFACT_TTL_SECONDS)
    ).strip()
    try:
        value: int = int(raw)
    except ValueError:
        logger.warning(
            "%s=%r is not an integer; falling back to %s",
            ARTIFACT_TTL_ENV,
            raw,
            DEFAULT_ARTIFACT_TTL_SECONDS,
        )
        return DEFAULT_ARTIFACT_TTL_SECONDS
    return max(value, 0)


def new_id() -> str:
    """Return a random, unguessable artifact or job id."""
    return secrets.token_urlsafe(24)


def _safe_segment(value: str, pattern: re.Pattern[str], kind: str) -> str:
    """Reject a path segment that could escape the store.

    Raises:
        ArtifactNotFoundError: When the value is not a plain segment. A refusal
            rather than an error, so probing for the difference between a
            malformed id and a missing one tells the caller nothing.
    """
    if not pattern.match(value):
        raise ArtifactNotFoundError(f"No such {kind}.")
    return value


def _caller_dir(caller: str) -> Path:
    """Return the directory holding one caller's artifacts."""
    if caller.strip(".") == "" or caller in _RESERVED_LABELS:
        raise ArtifactNotFoundError("No such caller.")
    return artifact_root() / _safe_segment(caller, _SAFE_LABEL, "caller")


@dataclass
class ArtifactRecord:
    """What is known about a stored artifact without reading its bytes."""

    artifact_id: str
    mime_type: str
    size_bytes: int
    created_at: float
    kind: str = "image"
    engine: str | None = None
    model: str | None = None


def _meta_path(caller: str, artifact_id: str) -> Path:
    return (
        _caller_dir(caller) / f"{_safe_segment(artifact_id, _SAFE_ID, 'artifact')}.json"
    )


def _blob_path(caller: str, artifact_id: str) -> Path:
    return (
        _caller_dir(caller) / f"{_safe_segment(artifact_id, _SAFE_ID, 'artifact')}.bin"
    )


def store_artifact(
    caller: str,
    data: bytes,
    *,
    mime_type: str,
    kind: str = "image",
    engine: str | None = None,
    model: str | None = None,
) -> ArtifactRecord:
    """Write bytes to the store and return what a caller needs to fetch them.

    Args:
        caller: API key label the artifact belongs to.
        data: The artifact's bytes.
        mime_type: Content type to serve it back as.
        kind: `image` or `video`, for the caller's own bookkeeping.
        engine: Engine that produced it, when known.
        model: Model that produced it, when known.

    Returns:
        ArtifactRecord: Identity and size, without the bytes.

    Raises:
        ArtifactStoreUnavailableError: When the store cannot be written.
    """
    directory: Path = _caller_dir(caller)
    artifact_id: str = new_id()
    record = ArtifactRecord(
        artifact_id=artifact_id,
        mime_type=mime_type,
        size_bytes=len(data),
        created_at=time.time(),
        kind=kind,
        engine=engine,
        model=model,
    )
    try:
        directory.mkdir(parents=True, exist_ok=True)
        _blob_path(caller, artifact_id).write_bytes(data)
        _meta_path(caller, artifact_id).write_text(json.dumps(asdict(record)))
    except OSError as error:
        raise ArtifactStoreUnavailableError(
            f"Could not write to the artifact store at {artifact_root()}: {error}. "
            f"Set {ARTIFACT_DIR_ENV} to a writable path, or mount a bucket there."
        ) from error
    return record


def read_record(caller: str, artifact_id: str) -> ArtifactRecord:
    """Return an artifact's metadata.

    Raises:
        ArtifactNotFoundError: When it does not exist, belongs to another
            caller, or has aged past the configured lifetime.
    """
    meta: Path = _meta_path(caller, artifact_id)
    blob: Path = _blob_path(caller, artifact_id)
    if not meta.is_file() or not blob.is_file():
        raise ArtifactNotFoundError("No such artifact.")
    try:
        record = ArtifactRecord(**json.loads(meta.read_text()))
    except (OSError, ValueError, TypeError) as error:
        raise ArtifactNotFoundError("No such artifact.") from error

    ttl: int = artifact_ttl_seconds()
    if ttl and (time.time() - record.created_at) > ttl:
        # The bucket's lifecycle rule is what actually deletes; this keeps the
        # service from serving something the rule is about to remove halfway
        # through a download.
        raise ArtifactNotFoundError("Artifact has expired.")
    return record


def read_range(caller: str, artifact_id: str, start: int, end: int) -> Iterator[bytes]:
    """Yield an artifact's bytes from `start` to `end` inclusive.

    Reads in chunks rather than whole, so a video never has to fit in the
    instance's memory to be served.

    Args:
        caller: API key label the artifact belongs to.
        artifact_id: The artifact to read.
        start: First byte offset, inclusive.
        end: Last byte offset, inclusive.

    Yields:
        bytes: Successive chunks of the requested range.
    """
    path: Path = _blob_path(caller, artifact_id)
    remaining: int = end - start + 1
    with path.open("rb") as handle:
        handle.seek(start)
        while remaining > 0:
            chunk: bytes = handle.read(min(CHUNK_BYTES, remaining))
            if not chunk:
                # The file shrank underneath us, which a lifecycle deletion can
                # do mid-read. Stopping short beats looping forever.
                return
            remaining -= len(chunk)
            yield chunk


@dataclass
class JobRecord:
    """A generation job's progress, shared across instances.

    Video generation runs longer than the request that starts it, and the
    instance polled for progress is often not the instance doing the work, so
    this is written to the same store the artifacts use rather than held in
    process memory.
    """

    job_id: str
    status: str = "queued"
    percent: float = 0.0
    estimated: bool = True
    detail: str | None = None
    artifact_ids: list[str] = field(default_factory=list)
    engine: str | None = None
    model: str | None = None
    error: str | None = None
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)


def _job_path(caller: str, job_id: str) -> Path:
    return _caller_dir(caller) / f"job-{_safe_segment(job_id, _SAFE_ID, 'job')}.json"


def write_job(caller: str, record: JobRecord) -> JobRecord:
    """Persist a job's current state, replacing whatever was there."""
    record.updated_at = time.time()
    directory: Path = _caller_dir(caller)
    try:
        directory.mkdir(parents=True, exist_ok=True)
        _job_path(caller, record.job_id).write_text(json.dumps(asdict(record)))
    except OSError as error:
        raise ArtifactStoreUnavailableError(
            f"Could not write to the artifact store at {artifact_root()}: {error}."
        ) from error
    return record


def read_job(caller: str, job_id: str) -> JobRecord:
    """Return a job's current state.

    Raises:
        ArtifactNotFoundError: When no such job belongs to this caller.
    """
    path: Path = _job_path(caller, job_id)
    if not path.is_file():
        raise ArtifactNotFoundError("No such job.")
    try:
        data: dict[str, Any] = json.loads(path.read_text())
        return JobRecord(**data)
    except (OSError, ValueError, TypeError) as error:
        raise ArtifactNotFoundError("No such job.") from error


TERMINAL_STATUSES: Final[frozenset[str]] = frozenset({"ready", "failed"})


def job_is_finished(record: JobRecord) -> bool:
    """Return whether a job has stopped changing."""
    return record.status in TERMINAL_STATUSES
