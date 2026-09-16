"""Where meetings live on disk, in either layout.

Two layouts are supported:

* **flat** (upstream's, used when ``meetings_dir`` is empty): notes in
  ``notes_dir/*.md``, transcripts in ``transcripts_dir``, audio in
  ``recordings_dir``.
* **folders** (``meetings_dir`` set): one directory per meeting, grouped by
  category::

      <meetings_dir>/<Category>/<YYYY-MM-DD-HHMMSS-slug>/<same>.md
                                                         <same>.txt
                                                         <same>.wav

Everything that needs a meeting's files goes through here, so the rest of the
app never has to know which layout is in use.
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

from .logger import get_logger

logger = get_logger(__name__)

UNCATEGORISED = "Uncategorised"


def uses_folders(config) -> bool:
    return bool(getattr(config, "meetings_dir", ""))


def meetings_root(config) -> Path:
    return Path(config.meetings_dir).expanduser()


def slugify(title: str) -> str:
    """The filename-safe form of a title (the rules create_note always used)."""
    slug = re.sub(r"[^\w\s-]", "", title.lower())
    return re.sub(r"[-\s]+", "-", slug)[:50]


def meeting_basename(now: datetime, title: str) -> str:
    return f"{now.strftime('%Y-%m-%d-%H%M%S')}-{slugify(title)}"


def category_dir_name(category: Optional[str]) -> str:
    return category or UNCATEGORISED


def validate_category_name(name: object) -> Optional[str]:
    """Return an error message, or None when ``name`` is usable as a folder."""
    if not isinstance(name, str) or not name.strip():
        return "categories must be non-empty text"
    if name != name.strip():
        return f"category {name!r} has leading or trailing spaces"
    if name in (".", "..") or name.startswith(".") or "/" in name or "\0" in name:
        return f"category {name!r} cannot be used as a folder name"
    if name == UNCATEGORISED:
        return f"{UNCATEGORISED!r} is reserved for meetings with no category"
    return None


# --------------------------------------------------------------------------
# Frontmatter
# --------------------------------------------------------------------------


def read_frontmatter(path: Path) -> dict[str, str]:
    """Scalar ``key: value`` pairs from a note's leading ``---`` block."""
    try:
        content = Path(path).read_text(encoding="utf-8")
    except OSError:
        return {}
    if not content.startswith("---"):
        return {}
    parts = content.split("---", 2)
    if len(parts) < 3:
        return {}
    values = {}
    for line in parts[1].splitlines():
        if ":" in line and not line.startswith((" ", "\t")):
            key, value = line.split(":", 1)
            values[key.strip()] = value.strip().strip('"')
    return values


def set_frontmatter_value(path: Path, key: str, value: Optional[str]) -> None:
    """Set (or, with ``None``, remove) one quoted frontmatter value in place."""
    path = Path(path)
    content = path.read_text(encoding="utf-8")
    if not content.startswith("---"):
        raise ValueError(f"{path.name} has no frontmatter")
    _, frontmatter, body = content.split("---", 2)
    lines = frontmatter.split("\n")
    rendered = None if value is None else f'{key}: "{value}"'
    out, found = [], False
    for line in lines:
        if line.startswith(f"{key}:"):
            found = True
            if rendered is not None:
                out.append(rendered)
            continue
        out.append(line)
    if not found and rendered is not None:
        # Insert after the title line, before the trailing empty element.
        at = next((i + 1 for i, line in enumerate(out) if line.startswith("title:")), len(out) - 1)
        out.insert(at, rendered)
    tmp = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    tmp.write_text("---" + "\n".join(out) + "---" + body, encoding="utf-8")
    tmp.replace(path)


# --------------------------------------------------------------------------
# Listing and lookup
# --------------------------------------------------------------------------


def list_notes(config) -> list[Path]:
    """Every meeting note, newest first."""
    if uses_folders(config):
        root = meetings_root(config)
        if not root.is_dir():
            return []
        # Fixed depth: <category>/<meeting>/<meeting>.md. Anything dot-prefixed
        # is a staged delete or a temp file, never a meeting.
        notes = [
            note
            for note in root.glob("*/*/*.md")
            if not any(part.startswith(".") for part in note.relative_to(root).parts)
        ]
    else:
        notes_dir = Path(config.notes_dir).expanduser()
        notes = list(notes_dir.glob("*.md")) if notes_dir.is_dir() else []
    return sorted(notes, key=lambda p: p.stat().st_mtime, reverse=True)


def _contained(base: Path, name: str) -> Optional[Path]:
    """``base / name`` only if it stays inside ``base`` (frontmatter is editable)."""
    if not name:
        return None
    base = base.resolve()
    candidate = (base / name).resolve()
    return candidate if candidate.is_relative_to(base) else None


def transcript_path_for(note: Path, config) -> Optional[Path]:
    name = read_frontmatter(note).get("transcript_file", "")
    base = Path(note).parent if uses_folders(config) else Path(config.transcripts_dir).expanduser()
    return _contained(base, name)


def recording_path_for(note: Path, config) -> Optional[Path]:
    name = read_frontmatter(note).get("recording_file", "")
    base = Path(note).parent if uses_folders(config) else Path(config.recordings_dir).expanduser()
    return _contained(base, name)


def category_of(note: Path, config) -> Optional[str]:
    """The category a note is filed under; the folder is the truth in that layout."""
    if uses_folders(config):
        name = Path(note).parent.parent.name
        return None if name == UNCATEGORISED else name
    return read_frontmatter(note).get("category") or None


# --------------------------------------------------------------------------
# Moving and deleting
# --------------------------------------------------------------------------


def move_meeting(note: Path, category: Optional[str], config) -> Path:
    """Refile a meeting's folder under another category. Returns the new note path."""
    if not uses_folders(config):
        raise RuntimeError("Moving meetings needs the folder layout (set meetings_dir)")
    note = Path(note)
    folder = note.parent
    target_parent = meetings_root(config) / category_dir_name(category)
    target = target_parent / folder.name
    if target == folder:
        return note
    if target.exists():
        raise FileExistsError(f"{target_parent.name}/{folder.name} already exists")
    target_parent.mkdir(parents=True, exist_ok=True)
    folder.rename(target)
    new_note = target / note.name
    set_frontmatter_value(new_note, "category", category)
    _remove_if_empty(folder.parent)
    logger.info(f"Moved meeting {folder.name} to {category_dir_name(category)}")
    return new_note


def delete_meeting(note: Path, config) -> None:
    """Remove a meeting: its whole folder, or note + transcript in the flat layout."""
    note = Path(note)
    if uses_folders(config):
        folder = note.parent
        staged = folder.with_name(f".{folder.name}.deleting-{os.getpid()}")
        folder.rename(staged)
        shutil.rmtree(staged)
        _remove_if_empty(folder.parent)
        return

    transcript = transcript_path_for(note, config)
    staged_note = note.with_name(f".{note.name}.deleting-{os.getpid()}")
    staged_transcript = None
    note.replace(staged_note)
    try:
        if transcript and transcript.exists():
            staged_transcript = transcript.with_name(f".{transcript.name}.deleting-{os.getpid()}")
            transcript.replace(staged_transcript)
    except Exception:
        staged_note.replace(note)
        raise
    for staged in (staged_note, staged_transcript):
        if staged is not None:
            try:
                staged.unlink()
            except OSError:
                logger.warning(f"Could not purge staged deleted file: {staged}")


def _remove_if_empty(directory: Path) -> None:
    try:
        directory.rmdir()
    except OSError:
        pass


# --------------------------------------------------------------------------
# Migration from the flat layout
# --------------------------------------------------------------------------


@dataclass
class MigrationStep:
    note: Path
    target: Path
    transcript: Optional[Path]
    recording: Optional[Path]
    problem: str = ""


# Recording filenames are the capture start time: 2026-09-16-070450.wav
_WAV_STAMP = re.compile(r"^(\d{4}-\d{2}-\d{2}-\d{6})\.wav$")
# A note is written when processing finishes, so its time is the recording
# start + duration + transcription/summary time. Allow for that much slack.
_MATCH_SLACK = timedelta(minutes=3)


def _match_recording(meta: dict, recordings_dir: Path) -> tuple[Optional[Path], str]:
    try:
        written = datetime.strptime(f"{meta['date']} {meta['time']}", "%Y-%m-%d %H:%M")
        duration = timedelta(seconds=int(meta.get("duration_seconds", "0")))
    except (KeyError, ValueError):
        return None, "no date/time to match a recording by"
    if not recordings_dir.is_dir():
        return None, ""
    matches = []
    for wav in recordings_dir.glob("*.wav"):
        stamp = _WAV_STAMP.match(wav.name)
        if not stamp:
            continue
        started = datetime.strptime(stamp.group(1), "%Y-%m-%d-%H%M%S")
        ended = started + duration
        # Note times are truncated to the minute, hence the extra minute.
        if ended - timedelta(minutes=1) <= written <= ended + _MATCH_SLACK:
            matches.append(wav)
    if len(matches) > 1:
        return None, f"ambiguous recordings: {', '.join(sorted(m.name for m in matches))}"
    return (matches[0] if matches else None), ""


def plan_migration(config) -> list[MigrationStep]:
    root = meetings_root(config) / UNCATEGORISED
    transcripts_dir = Path(config.transcripts_dir).expanduser()
    recordings_dir = Path(config.recordings_dir).expanduser()
    notes_dir = Path(config.notes_dir).expanduser()
    steps = []
    for note in sorted(notes_dir.glob("*.md")) if notes_dir.is_dir() else []:
        meta = read_frontmatter(note)
        target = root / note.stem
        transcript = _contained(transcripts_dir, meta.get("transcript_file", ""))
        if transcript is not None and not transcript.exists():
            transcript = None
        recording = _contained(recordings_dir, meta.get("recording_file", ""))
        problem = ""
        if recording is None or not recording.exists():
            recording, problem = _match_recording(meta, recordings_dir)
        if target.exists():
            problem = f"{target} already exists"
        steps.append(MigrationStep(note, target, transcript, recording, problem))
    return steps


def apply_migration(steps: list[MigrationStep]) -> int:
    moved = 0
    for step in steps:
        if step.problem and step.target.exists():
            continue
        step.target.mkdir(parents=True)
        base = step.target.name
        if step.transcript:
            shutil.move(step.transcript, step.target / f"{base}.txt")
        if step.recording:
            shutil.move(step.recording, step.target / f"{base}.wav")
        new_note = step.target / f"{base}.md"
        shutil.move(step.note, new_note)
        set_frontmatter_value(new_note, "transcript_file", f"{base}.txt" if step.transcript else None)
        set_frontmatter_value(new_note, "recording_file", f"{base}.wav" if step.recording else "")
        moved += 1
    return moved


def migrate_main(argv: Optional[list[str]] = None) -> int:
    """``omascribe-migrate-folders``: move flat-layout meetings into folders."""
    from .config import load_config

    parser = argparse.ArgumentParser(
        prog="omascribe-migrate-folders",
        description="Move meetings from notes_dir/transcripts_dir/recordings_dir into "
        f"meetings_dir/{UNCATEGORISED}/<meeting>/. Dry run unless --apply.",
    )
    parser.add_argument("--apply", action="store_true", help="actually move files")
    args = parser.parse_args(argv)

    config = load_config()
    if not uses_folders(config):
        print("meetings_dir is not set in the config; nothing to migrate into.")
        return 1

    steps = plan_migration(config)
    if not steps:
        print("No flat-layout notes found.")
        return 0
    for step in steps:
        print(f"{step.note.name}")
        print(f"  -> {step.target}/")
        print(f"     transcript: {step.transcript.name if step.transcript else '(none)'}")
        print(f"     recording:  {step.recording.name if step.recording else '(none)'}")
        if step.problem:
            print(f"     note: {step.problem}")
    if not args.apply:
        print("\nDry run. Re-run with --apply to move these.")
        return 0
    moved = apply_migration(steps)
    print(f"\nMoved {moved} meeting(s).")
    return 0
