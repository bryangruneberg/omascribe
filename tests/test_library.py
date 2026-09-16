"""Tests for the folder-per-meeting layout, categories and migration."""
from __future__ import annotations

from datetime import datetime

import pytest

from omascribe import library
from omascribe.ai_summarizer import BaseSummarizer
from omascribe.config import AppConfig, validate_config
from omascribe.note_maker import NoteMaker


def folder_config(tmp_path, **kw):
    return AppConfig(meetings_dir=str(tmp_path / "Meetings"), recordings_dir=str(tmp_path / "rec"), **kw)


def make_note(tmp_path, category="DGxC Customer", title="Sprint Planning", with_audio=True):
    cfg = folder_config(tmp_path, categories=["DGxC Customer", "Personal"])
    rec = tmp_path / "rec"
    rec.mkdir(exist_ok=True)
    wav = rec / "2026-09-16-070450.wav"
    if with_audio:
        wav.write_bytes(b"RIFF")
    maker = NoteMaker(ai_provider="none", meetings_dir=cfg.meetings_dir,
                      output_dir=str(tmp_path / "flat-n"), transcripts_dir=str(tmp_path / "flat-t"))
    note, transcript, _ = maker.create_note(
        transcript_text="hello there", formatted_transcript="**[00:00]** hello there", duration=60,
        title=title, category=category, recording_path=str(wav) if with_audio else None,
    )
    return cfg, library.Path(note), wav


def test_basename_matches_the_old_rules():
    assert library.meeting_basename(datetime(2026, 9, 16, 7, 35, 19), "Commercial - GK Setup!") == \
        "2026-09-16-073519-commercial-gk-setup"


def test_create_note_writes_one_folder_per_meeting(tmp_path):
    cfg, note, wav = make_note(tmp_path)
    folder = note.parent
    assert folder.parent.name == "DGxC Customer"
    assert folder.parent.parent == tmp_path / "Meetings"
    assert sorted(p.suffix for p in folder.iterdir()) == [".md", ".txt", ".wav"]
    assert not wav.exists(), "audio moves into the meeting folder"
    meta = library.read_frontmatter(note)
    assert meta["category"] == "DGxC Customer"
    assert meta["recording_file"] == f"{folder.name}.wav"
    assert library.transcript_path_for(note, cfg) == (folder / f"{folder.name}.txt").resolve()
    assert library.recording_path_for(note, cfg) == (folder / f"{folder.name}.wav").resolve()
    # the flat dirs are not created in the folder layout
    assert not (tmp_path / "flat-n").exists()


def test_uncategorised_meetings_have_no_category_line(tmp_path):
    cfg, note, _ = make_note(tmp_path, category=None)
    assert note.parent.parent.name == library.UNCATEGORISED
    assert "category" not in library.read_frontmatter(note)
    assert library.category_of(note, cfg) is None


def test_list_notes_fixed_depth_skips_staged_and_stray(tmp_path):
    cfg, note, _ = make_note(tmp_path)
    root = tmp_path / "Meetings"
    (root / "stray.md").write_text("x")
    (root / "Personal" / ".2026-old.deleting-1").mkdir(parents=True)
    (root / "Personal" / ".2026-old.deleting-1" / "a.md").write_text("x")
    (note.parent / "deeper").mkdir()
    (note.parent / "deeper" / "b.md").write_text("x")
    assert library.list_notes(cfg) == [note]


def test_transcript_lookup_refuses_escape(tmp_path):
    cfg, note, _ = make_note(tmp_path)
    library.set_frontmatter_value(note, "transcript_file", "../../../../etc/passwd")
    assert library.transcript_path_for(note, cfg) is None


def test_flat_layout_lookup_is_unchanged(tmp_path):
    cfg = AppConfig(notes_dir=str(tmp_path / "n"), transcripts_dir=str(tmp_path / "t"))
    maker = NoteMaker(ai_provider="none", output_dir=cfg.notes_dir, transcripts_dir=cfg.transcripts_dir)
    note, transcript, _ = maker.create_note(transcript_text="hi", formatted_transcript="hi", duration=1, title="T")
    assert library.list_notes(cfg) == [library.Path(note)]
    assert library.transcript_path_for(library.Path(note), cfg) == library.Path(transcript).resolve()
    library.delete_meeting(library.Path(note), cfg)
    assert list((tmp_path / "n").iterdir()) == [] and list((tmp_path / "t").iterdir()) == []


def test_move_meeting_and_collision(tmp_path):
    cfg, note, _ = make_note(tmp_path)
    moved = library.move_meeting(note, "Personal", cfg)
    assert moved.parent.parent.name == "Personal"
    assert library.read_frontmatter(moved)["category"] == "Personal"
    assert not (tmp_path / "Meetings" / "DGxC Customer").exists(), "empty category folder is tidied"
    assert library.transcript_path_for(moved, cfg).exists()

    back = library.move_meeting(moved, None, cfg)
    assert back.parent.parent.name == library.UNCATEGORISED
    assert "category" not in library.read_frontmatter(back)

    (tmp_path / "Meetings" / "Personal" / back.parent.name).mkdir(parents=True)
    with pytest.raises(FileExistsError):
        library.move_meeting(back, "Personal", cfg)
    assert back.exists()


def test_delete_removes_whole_folder(tmp_path):
    cfg, note, _ = make_note(tmp_path)
    library.delete_meeting(note, cfg)
    assert not note.parent.exists()
    assert library.list_notes(cfg) == []


def test_category_validation():
    assert validate_config(AppConfig(categories=["amazee.io", "DGxC Customer"]))[0]
    for bad in (["a/b"], [".hidden"], [""], ["dup", "dup"], [library.UNCATEGORISED], [" pad"], "notalist"):
        ok, err = validate_config(AppConfig(categories=bad))
        assert not ok and "categories" in err, bad


def test_prompt_carries_category_outside_transcript():
    prompt = BaseSummarizer()._build_prompt("Speaker A: hi", category="DGxC Customer")
    assert 'category "DGxC Customer"' in prompt
    assert prompt.index("DGxC Customer") < prompt.index("<transcript>")
    assert "category" not in BaseSummarizer()._build_prompt("x")


def test_summarizer_receives_category(tmp_path):
    seen = {}
    maker = NoteMaker(ai_provider="none", meetings_dir=str(tmp_path / "M"))
    maker.ai_provider = "deepinfra"
    maker.summarizer = type("S", (), {"summarize": lambda self, t, user_notes="", category="": seen.update(c=category) or (_ for _ in ()).throw(RuntimeError())})()
    maker.create_note(transcript_text="x", formatted_transcript="x", duration=1, title="T", category="Personal")
    assert seen["c"] == "Personal"


# ---------------------------------------------------------------- migration

def flat_note(notes, transcripts, stem, time, duration, transcript=True):
    notes.mkdir(parents=True, exist_ok=True)
    transcripts.mkdir(parents=True, exist_ok=True)
    (notes / f"{stem}.md").write_text(
        f'---\ntitle: "{stem}"\ndate: 2026-09-16\ntime: "{time}"\nduration_seconds: {duration}\n'
        f'tags: [meeting, auto-generated]\nrecording_file: ""\ntranscript_file: "{stem}.txt"\n---\n\n# body\n'
    )
    if transcript:
        (transcripts / f"{stem}.txt").write_text("transcript")


def test_migration_matches_recordings_by_time(tmp_path):
    notes, transcripts, rec = tmp_path / "notes", tmp_path / "transcripts", tmp_path / "recordings"
    cfg = AppConfig(meetings_dir=str(tmp_path), notes_dir=str(notes), transcripts_dir=str(transcripts),
                    recordings_dir=str(rec))
    flat_note(notes, transcripts, "2026-09-16-063010-test", "06:30", 101)
    flat_note(notes, transcripts, "2026-09-16-073519-commercial-gk-setup", "07:35", 1691)
    rec.mkdir()
    (rec / "2026-09-16-062814.wav").write_bytes(b"a")
    (rec / "2026-09-16-070450.wav").write_bytes(b"b")

    steps = library.plan_migration(cfg)
    assert {s.note.stem: s.recording.name for s in steps} == {
        "2026-09-16-063010-test": "2026-09-16-062814.wav",
        "2026-09-16-073519-commercial-gk-setup": "2026-09-16-070450.wav",
    }
    assert library.apply_migration(steps) == 2

    gk = tmp_path / library.UNCATEGORISED / "2026-09-16-073519-commercial-gk-setup"
    assert sorted(p.name for p in gk.iterdir()) == [f"{gk.name}.md", f"{gk.name}.txt", f"{gk.name}.wav"]
    note = gk / f"{gk.name}.md"
    assert library.recording_path_for(note, cfg).read_bytes() == b"b"
    assert library.transcript_path_for(note, cfg).read_text() == "transcript"
    assert list(rec.iterdir()) == [] and list(notes.iterdir()) == []
    assert len(library.list_notes(cfg)) == 2


def test_migration_leaves_ambiguous_audio_behind(tmp_path):
    notes, transcripts, rec = tmp_path / "notes", tmp_path / "transcripts", tmp_path / "recordings"
    cfg = AppConfig(meetings_dir=str(tmp_path), notes_dir=str(notes), transcripts_dir=str(transcripts),
                    recordings_dir=str(rec))
    flat_note(notes, transcripts, "2026-09-16-063010-test", "06:30", 101)
    rec.mkdir()
    (rec / "2026-09-16-062814.wav").write_bytes(b"a")
    (rec / "2026-09-16-062830.wav").write_bytes(b"b")
    [step] = library.plan_migration(cfg)
    assert step.recording is None and "ambiguous" in step.problem
    library.apply_migration([step])
    assert len(list(rec.iterdir())) == 2
    assert library.list_notes(cfg)[0].parent.name == "2026-09-16-063010-test"


def test_folder_layout_does_not_require_flat_dirs(tmp_path):
    """The flat notes/transcripts dirs are unused there; requiring them sent
    the app into safe mode (no AI, empty list) after migration removed them."""
    rec = tmp_path / "rec"
    rec.mkdir()
    cfg = AppConfig(meetings_dir=str(tmp_path / "M"), recordings_dir=str(rec),
                    notes_dir=str(tmp_path / "gone-notes"), transcripts_dir=str(tmp_path / "gone-t"))
    assert validate_config(cfg) == (True, None)
    cfg.meetings_dir = ""
    assert not validate_config(cfg)[0]
