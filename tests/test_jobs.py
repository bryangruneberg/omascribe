"""Tests for the persistent processing queue (omascribe/jobs.py)."""
from __future__ import annotations

import json
from datetime import datetime

import pytest
import requests

from omascribe import jobs
from omascribe.transcriber import AssemblyAIError, TranscriptResult, TranscriptSegment


@pytest.fixture(autouse=True)
def state_home(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    return tmp_path


RESULT = TranscriptResult(
    text="hello there",
    segments=[TranscriptSegment(0, 1.5, "hello there", "A")],
    language="en",
    duration=90.0,
)


class FakeTranscriber:
    def __init__(self, submit_errors=(), wait_errors=()):
        self.submits, self.waits = [], []
        self.submit_errors, self.wait_errors = list(submit_errors), list(wait_errors)

    def submit(self, audio_path):
        self.submits.append(audio_path)
        if self.submit_errors:
            raise self.submit_errors.pop(0)
        return f"tx-{len(self.submits)}"

    def wait(self, transcript_id):
        self.waits.append(transcript_id)
        if self.wait_errors:
            raise self.wait_errors.pop(0)
        return RESULT


class FakeNoteMaker:
    def __init__(self):
        self.calls = []

    def create_note(self, **kwargs):
        self.calls.append(kwargs)
        return "/notes/x.md", "/notes/x.txt", None


class Clock:
    def __init__(self):
        self.now = 1_000_000.0

    def __call__(self):
        return self.now


def make_runner(transcriber, maker=None, clock=None):
    events = {"changes": 0, "done": [], "gave_up": []}
    maker = maker or FakeNoteMaker()
    runner = jobs.JobRunner(
        config_provider=lambda: object(),
        build_transcriber=lambda cfg: transcriber,
        build_note_maker=lambda cfg: maker,
        on_change=lambda job: events.__setitem__("changes", events["changes"] + 1),
        on_done=lambda job, note, err: events["done"].append((job.id, note)),
        on_gave_up=lambda job: events["gave_up"].append(job.id),
        clock=clock or Clock(),
    )
    return runner, events, maker


def queued(tmp_path, **kw):
    audio = tmp_path / "2026-09-16-083001.wav"
    audio.write_bytes(b"RIFF")
    return jobs.enqueue(str(audio), title="Midweek Mayhem", category="DGxC Internal",
                        stopped_at=datetime(2026, 9, 16, 9, 4, 20), **kw)


# ---------------------------------------------------------------- store

def test_enqueue_roundtrip_and_counts(tmp_path):
    job = queued(tmp_path)
    assert job.id == "2026-09-16-083001"
    assert (job.path.stat().st_mode & 0o777) == 0o600
    again = jobs.get(job.id)
    assert again == job
    assert jobs.counts() == {"queued": 1, "running": 0, "failed": 0}
    second = queued(tmp_path)
    assert second.id == "2026-09-16-083001-2"


def test_delete_removes_cached_transcript(tmp_path):
    job = queued(tmp_path)
    job.result_path.write_text("{}")
    jobs.delete(job)
    assert not job.path.exists() and not job.result_path.exists()
    assert jobs.list_jobs() == []


def test_transcript_result_roundtrip():
    assert TranscriptResult.from_dict(json.loads(json.dumps(RESULT.to_dict()))) == RESULT


# ---------------------------------------------------------------- classify

@pytest.mark.parametrize("exc, transient", [
    (requests.exceptions.SSLError("UNEXPECTED_EOF_WHILE_READING"), True),
    (requests.exceptions.ConnectionError("reset"), True),
    (requests.exceptions.Timeout("slow"), True),
    (AssemblyAIError("503", status_code=503), True),
    (AssemblyAIError("429", status_code=429), True),
    (AssemblyAIError("upload failed after 3 attempts", transient=True), True),
    (AssemblyAIError("401", status_code=401), False),
    (AssemblyAIError("400", status_code=400), False),
    (AssemblyAIError("Audio file is empty", transient=False, remote_failed=True), False),
    (ValueError("AssemblyAI API key required"), False),
    (FileNotFoundError("gone"), False),
    (KeyError("upload_url"), False),
])
def test_is_transient(exc, transient):
    assert jobs.is_transient(exc) is transient


# ---------------------------------------------------------------- runner

def test_success_writes_note_for_the_stop_time_and_clears_the_job(tmp_path):
    job = queued(tmp_path)
    runner, events, maker = make_runner(FakeTranscriber())
    runner.run_one(jobs.get(job.id))
    assert events["done"] == [(job.id, "/notes/x.md")]
    assert jobs.list_jobs() == []
    call = maker.calls[0]
    assert call["title"] == "Midweek Mayhem" and call["category"] == "DGxC Internal"
    assert call["when"] == datetime(2026, 9, 16, 9, 4, 20)
    assert call["summary_input"] == "Speaker A: hello there"
    assert call["recording_path"].endswith("2026-09-16-083001.wav")


def test_transient_failure_backs_off(tmp_path):
    job = queued(tmp_path)
    clock = Clock()
    runner, events, _ = make_runner(
        FakeTranscriber(submit_errors=[requests.exceptions.SSLError("eof")]), clock=clock)
    runner.run_one(jobs.get(job.id))
    after = jobs.get(job.id)
    assert (after.status, after.attempts) == ("pending", 1)
    assert after.next_attempt_at == clock.now + 60
    assert "SSLError" in after.last_error
    assert events["gave_up"] == []
    assert runner.next_due() == (None, 60)


def test_gives_up_after_max_attempts_exactly_once(tmp_path):
    job = queued(tmp_path)
    errors = [requests.exceptions.ConnectionError("down")] * (jobs.MAX_ATTEMPTS + 1)
    clock = Clock()
    runner, events, _ = make_runner(FakeTranscriber(submit_errors=errors), clock=clock)
    for _ in range(jobs.MAX_ATTEMPTS):
        clock.now += 10_000
        due, _ = runner.next_due()
        runner.run_one(due)
    after = jobs.get(job.id)
    assert (after.status, after.attempts) == ("failed", jobs.MAX_ATTEMPTS)
    assert events["gave_up"] == [job.id]
    assert runner.next_due() == (None, None), "failed jobs are not picked up again"


def test_permanent_failure_fails_immediately(tmp_path):
    job = queued(tmp_path)
    runner, events, _ = make_runner(FakeTranscriber(submit_errors=[AssemblyAIError("bad key", status_code=401)]))
    runner.run_one(jobs.get(job.id))
    assert jobs.get(job.id).status == "failed"
    assert events["gave_up"] == [job.id]


def test_resume_polls_existing_transcript_without_uploading(tmp_path):
    job = queued(tmp_path)
    transcriber = FakeTranscriber(wait_errors=[requests.exceptions.Timeout("poll")])
    runner, _, _ = make_runner(transcriber)
    runner.run_one(jobs.get(job.id))
    assert jobs.get(job.id).transcript_id == "tx-1", "id checkpointed before the poll failed"

    jobs.retry_now(job.id)
    runner.run_one(jobs.get(job.id))
    assert transcriber.submits == [str(tmp_path / "2026-09-16-083001.wav")], "uploaded once"
    assert transcriber.waits == ["tx-1", "tx-1"]
    assert jobs.list_jobs() == []


def test_remote_error_clears_transcript_id(tmp_path):
    job = queued(tmp_path)
    transcriber = FakeTranscriber(wait_errors=[AssemblyAIError("empty audio", transient=False, remote_failed=True)])
    runner, _, _ = make_runner(transcriber)
    runner.run_one(jobs.get(job.id))
    after = jobs.get(job.id)
    assert after.status == "failed" and after.transcript_id == ""


def test_cached_transcript_skips_transcription(tmp_path):
    job = queued(tmp_path)

    class BrokenMaker(FakeNoteMaker):
        def create_note(self, **kwargs):
            raise OSError("disk full")

    transcriber = FakeTranscriber()
    runner, _, _ = make_runner(transcriber, maker=BrokenMaker())
    runner.run_one(jobs.get(job.id))
    after = jobs.get(job.id)
    assert after.stage == "note" and after.result_path.exists()

    runner2, events, _ = make_runner(transcriber)
    jobs.retry_now(job.id)
    runner2.run_one(jobs.get(job.id))
    assert len(transcriber.submits) == 1 and len(transcriber.waits) == 1
    assert events["done"]


def test_interrupted_running_job_resumes_and_lock_is_exclusive(tmp_path):
    job = queued(tmp_path)
    job.status = "running"
    jobs.save(job)
    first, _, _ = make_runner(FakeTranscriber())
    assert first.acquire()
    assert jobs.get(job.id).status == "pending"
    second, _, _ = make_runner(FakeTranscriber())
    assert not second.acquire(), "only one runner per jobs directory"
    first.stop()
    assert second.acquire()
    second.stop()


def test_thread_processes_a_woken_job(tmp_path):
    import time
    runner, events, _ = make_runner(FakeTranscriber(), clock=time.time)
    assert runner.start()
    try:
        job = queued(tmp_path)
        runner.wake()
        for _ in range(100):
            if events["done"]:
                break
            time.sleep(0.02)
        assert events["done"] == [(job.id, "/notes/x.md")]
    finally:
        runner.stop()


def test_panel_data_reports_jobs(tmp_path, capsys, monkeypatch):
    from omascribe import desktop
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    job = queued(tmp_path)
    job.status, job.last_error = "failed", "SSLError: eof"
    jobs.save(job)
    queued(tmp_path)
    desktop.panel_data()
    data = json.loads(capsys.readouterr().out)
    assert data["jobs"] == {"queued": 1, "running": 0, "failed": 1}
    assert data["failed_jobs"] == [{"title": "Midweek Mayhem", "error": "SSLError: eof"}]
