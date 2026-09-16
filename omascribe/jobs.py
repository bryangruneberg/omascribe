"""A persistent queue for turning stopped recordings into notes.

Stopping a recording writes a small job file; a single background thread
works through them. Everything a retry needs is on disk:

* ``transcript_id`` is saved the moment AssemblyAI issues it, so a retry or a
  restart polls the existing transcript instead of uploading the meeting again;
* the finished transcript is cached beside the job, so a failure after
  transcription never pays for transcription twice;
* the note is written for the time the recording stopped, into a
  deterministic folder, so repeating that step overwrites rather than
  duplicates.

Transient failures (network, timeouts, 429/5xx) back off and retry; anything
else, or the fifth transient failure, marks the job failed and keeps it --
with its audio -- until it is retried or discarded.
"""

from __future__ import annotations

import fcntl
import json
import os
import tempfile
import threading
import time
from dataclasses import asdict, dataclass, fields
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional

from .logger import get_logger

logger = get_logger(__name__)

MAX_ATTEMPTS = 5
BACKOFF_SECONDS = (60, 300, 900, 3600)  # after attempt 1, 2, 3, 4+
IDLE_WAKE_SECONDS = 30


def jobs_dir() -> Path:
    state_home = os.environ.get("XDG_STATE_HOME")
    base = Path(state_home) if state_home else Path.home() / ".local" / "state"
    path = base / "omascribe" / "jobs"
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    return path


@dataclass
class Job:
    id: str
    audio_path: str
    title: Optional[str] = None
    category: Optional[str] = None
    user_notes: str = ""
    created_at: str = ""          # ISO time the recording stopped
    stage: str = "transcribe"     # transcribe -> note
    status: str = "pending"       # pending | running | failed
    attempts: int = 0
    next_attempt_at: float = 0.0  # epoch seconds; 0 = now
    last_error: str = ""
    transcript_id: str = ""

    @property
    def path(self) -> Path:
        return jobs_dir() / f"{self.id}.json"

    @property
    def result_path(self) -> Path:
        return jobs_dir() / f"{self.id}.transcript.json"

    @property
    def stopped_at(self) -> datetime:
        try:
            return datetime.fromisoformat(self.created_at)
        except ValueError:
            return datetime.now()

    @property
    def label(self) -> str:
        return self.title or f"Meeting {self.stopped_at.strftime('%Y-%m-%d %H:%M')}"


# --------------------------------------------------------------------------
# Store
# --------------------------------------------------------------------------


def _write_json(path: Path, payload: dict) -> None:
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}-")
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def save(job: Job) -> None:
    _write_json(job.path, asdict(job))


def load(path: Path) -> Optional[Job]:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        logger.warning(f"Unreadable job file skipped: {path}")
        return None
    known = {f.name for f in fields(Job)}
    return Job(**{k: v for k, v in data.items() if k in known})


def get(job_id: str) -> Optional[Job]:
    path = jobs_dir() / f"{job_id}.json"
    return load(path) if path.exists() else None


def list_jobs() -> list[Job]:
    jobs = [
        job
        for path in jobs_dir().glob("*.json")
        if not path.name.startswith(".") and not path.name.endswith(".transcript.json")
        for job in [load(path)]
        if job is not None
    ]
    return sorted(jobs, key=lambda j: (j.created_at, j.id))


def delete(job: Job) -> None:
    for path in (job.path, job.result_path):
        try:
            path.unlink()
        except FileNotFoundError:
            pass


def counts() -> dict:
    tally = {"queued": 0, "running": 0, "failed": 0}
    for job in list_jobs():
        key = {"pending": "queued"}.get(job.status, job.status)
        if key in tally:
            tally[key] += 1
    return tally


def enqueue(audio_path: str, title: Optional[str] = None, category: Optional[str] = None,
            user_notes: str = "", stopped_at: Optional[datetime] = None) -> Job:
    base = Path(audio_path).stem
    job_id, n = base, 2
    while (jobs_dir() / f"{job_id}.json").exists():
        job_id, n = f"{base}-{n}", n + 1
    job = Job(
        id=job_id,
        audio_path=str(audio_path),
        title=title or None,
        category=category or None,
        user_notes=user_notes or "",
        created_at=(stopped_at or datetime.now()).isoformat(timespec="seconds"),
    )
    save(job)
    logger.info(f"Job {job.id} queued for {audio_path}")
    return job


def retry_now(job_id: str) -> Optional[Job]:
    job = get(job_id)
    if job is None:
        return None
    job.status, job.attempts, job.next_attempt_at = "pending", 0, 0.0
    save(job)
    return job


# --------------------------------------------------------------------------
# Failure classification
# --------------------------------------------------------------------------


def is_transient(exc: BaseException) -> bool:
    """Could the same step succeed if tried again later?"""
    from .transcriber import AssemblyAIError

    if isinstance(exc, AssemblyAIError):
        if exc.transient is not None:
            return exc.transient
        code = exc.status_code or 0
        return code in (408, 429) or code >= 500
    # Missing audio or unreadable files will not fix themselves; check these
    # before OSError, which they subclass.
    if isinstance(exc, (FileNotFoundError, PermissionError, IsADirectoryError)):
        return False
    try:
        import requests
        if isinstance(exc, requests.exceptions.HTTPError):
            code = exc.response.status_code if exc.response is not None else 0
            return code in (408, 429) or code >= 500
        if isinstance(exc, (requests.exceptions.ConnectionError, requests.exceptions.Timeout)):
            return True
    except ImportError:
        pass
    # requests' network errors derive from OSError too; so do socket errors.
    return isinstance(exc, (OSError, TimeoutError))


def backoff_seconds(attempts: int) -> int:
    return BACKOFF_SECONDS[min(max(attempts, 1), len(BACKOFF_SECONDS)) - 1]


# --------------------------------------------------------------------------
# Runner
# --------------------------------------------------------------------------


class JobRunner:
    """Processes the queue on one background thread.

    Holds an exclusive lock on the jobs directory for its lifetime, so a
    second omascribe instance runs no runner and a job is never processed
    twice. Callbacks are invoked on the runner thread; the app marshals them.
    """

    def __init__(
        self,
        config_provider: Callable[[], object],
        build_transcriber: Callable[[object], object],
        build_note_maker: Callable[[object], object],
        on_change: Optional[Callable[[Optional[Job]], None]] = None,
        on_done: Optional[Callable[[Job, str, Optional[str]], None]] = None,
        on_gave_up: Optional[Callable[[Job], None]] = None,
        clock: Callable[[], float] = time.time,
    ):
        self.config_provider = config_provider
        self.build_transcriber = build_transcriber
        self.build_note_maker = build_note_maker
        self.on_change = on_change or (lambda job: None)
        self.on_done = on_done or (lambda job, note, err: None)
        self.on_gave_up = on_gave_up or (lambda job: None)
        self.clock = clock
        self.current: Optional[Job] = None
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._lock_file = None

    # ---- lifecycle -------------------------------------------------------

    def acquire(self) -> bool:
        if self._lock_file is not None:
            return True
        handle = open(jobs_dir() / ".lock", "a")
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            handle.close()
            logger.info("Another omascribe instance is processing the queue; not starting a runner")
            return False
        self._lock_file = handle
        self.recover()
        return True

    def recover(self) -> None:
        """Jobs left 'running' belonged to a process that is gone."""
        for job in list_jobs():
            if job.status == "running":
                job.status = "pending"
                save(job)
                logger.info(f"Job {job.id} was interrupted; resuming from stage {job.stage}")

    def start(self) -> bool:
        if not self.acquire():
            return False
        self._thread = threading.Thread(target=self._loop, name="omascribe-jobs", daemon=True)
        self._thread.start()
        return True

    def stop(self, timeout: float = 2.0) -> None:
        self._stop.set()
        self._wake.set()
        if self._thread is not None:
            self._thread.join(timeout)
        if self._lock_file is not None:
            try:
                fcntl.flock(self._lock_file, fcntl.LOCK_UN)
            finally:
                self._lock_file.close()
                self._lock_file = None

    def wake(self) -> None:
        self._wake.set()

    @property
    def busy(self) -> bool:
        return self.current is not None

    # ---- scheduling ------------------------------------------------------

    def next_due(self) -> tuple[Optional[Job], Optional[float]]:
        """The job to run now, else the seconds until the next one is due."""
        now = self.clock()
        wait = None
        for job in list_jobs():
            if job.status != "pending":
                continue
            if job.next_attempt_at <= now:
                return job, None
            delta = job.next_attempt_at - now
            wait = delta if wait is None else min(wait, delta)
        return None, wait

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                job, wait = self.next_due()
            except Exception as exc:  # a broken jobs dir must not kill the thread
                logger.error(f"Could not scan jobs: {exc}", exc_info=True)
                job, wait = None, IDLE_WAKE_SECONDS
            if job is None:
                self._wake.wait(timeout=min(wait if wait is not None else IDLE_WAKE_SECONDS, IDLE_WAKE_SECONDS))
                self._wake.clear()
                continue
            self.run_one(job)

    # ---- one job ---------------------------------------------------------

    def run_one(self, job: Job) -> None:
        job.status = "running"
        save(job)
        self.current = job
        self._notify(self.on_change, job)
        try:
            result = self._transcribe(job)
            note_path, ai_error = self._write_note(job, result)
        except Exception as exc:
            self._failed(job, exc)
        else:
            delete(job)
            logger.info(f"Job {job.id} done: {note_path}")
            self.current = None
            self._notify(self.on_done, job, note_path, ai_error)
        finally:
            self.current = None
            self._notify(self.on_change, None)

    def _transcribe(self, job: Job):
        from .transcriber import TranscriptResult

        if job.result_path.exists():
            logger.info(f"Job {job.id}: using cached transcript")
            return TranscriptResult.from_dict(json.loads(job.result_path.read_text(encoding="utf-8")))

        config = self.config_provider()
        transcriber = self.build_transcriber(config)
        if hasattr(transcriber, "submit") and hasattr(transcriber, "wait"):
            if not job.transcript_id:
                job.transcript_id = transcriber.submit(job.audio_path)
                save(job)  # checkpoint before the long poll
                self._notify(self.on_change, job)
            else:
                logger.info(f"Job {job.id}: resuming transcript {job.transcript_id}")
            result = transcriber.wait(job.transcript_id)
        else:
            if hasattr(transcriber, "load_model"):
                transcriber.load_model()
            result = transcriber.transcribe(job.audio_path)

        _write_json(job.result_path, result.to_dict())
        job.stage = "note"
        save(job)
        self._notify(self.on_change, job)
        return result

    def _write_note(self, job: Job, result) -> tuple[str, Optional[str]]:
        from .transcriber import format_segments

        maker = self.build_note_maker(self.config_provider())
        duration = result.duration or (result.segments[-1].end if result.segments else 0)
        note_path, _transcript_path, ai_error = maker.create_note(
            transcript_text=result.text,
            formatted_transcript=format_segments(result.segments),
            duration=duration,
            title=job.title,
            user_notes=job.user_notes,
            summary_input=result.speaker_text(),
            category=job.category,
            recording_path=job.audio_path,
            when=job.stopped_at,
        )
        return note_path, ai_error

    def _failed(self, job: Job, exc: Exception) -> None:
        job.attempts += 1
        job.last_error = f"{type(exc).__name__}: {exc}"
        if getattr(exc, "remote_failed", False):
            job.transcript_id = ""  # that transcript is spent; a retry re-submits
        transient = is_transient(exc)
        if transient and job.attempts < MAX_ATTEMPTS:
            delay = backoff_seconds(job.attempts)
            job.status = "pending"
            job.next_attempt_at = self.clock() + delay
            logger.warning(f"Job {job.id} attempt {job.attempts} failed ({job.last_error}); retrying in {delay}s")
            save(job)
        else:
            job.status = "failed"
            job.next_attempt_at = 0.0
            why = "gave up after retries" if transient else "permanent failure"
            logger.error(f"Job {job.id} failed ({why}): {job.last_error}", exc_info=exc)
            save(job)
            self._notify(self.on_gave_up, job)
        self.current = None

    def _notify(self, callback, *args) -> None:
        try:
            callback(*args)
        except Exception as exc:
            logger.debug(f"Job callback failed: {exc}")
