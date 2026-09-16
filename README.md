# Omascribe

A keyboard-driven TUI for recording, transcribing, and summarising meetings on Linux.

Built specifically for [Omarchy Quattro](https://omarchy.org/) — integrates natively with the Quickshell bar, desktop notifications, and `SUPER+M` keybinding.

![TUI screenshot](docs/screenshot.png)

## Features

- **Record** — mic + system audio (PipeWire/PulseAudio)
- **Transcribe** — local Whisper (CPU, privacy-first), or AssemblyAI in the cloud with speaker labels
- **Summarise** — cloud LLM (OpenAI, Anthropic, OpenRouter) or local Ollama
- **Write notes** — add your own context during recording for better AI summaries
- **Keyboard-driven** — Lazygit-inspired layout, no mouse required
- **Omarchy-native** — bar status, notifications, app menu, and `SUPER+M` out of the box

## Quick Start

```bash
git clone https://github.com/jamespember/omascribe.git
cd omascribe
./setup.sh
```

On Omarchy Quattro this adds:

- `SUPER + M` — launch or focus
- Apps menu entry
- **Omascribe control panel** — bar widget with live recording status, quick actions, and recent meetings
- Desktop notifications for recording events

The control panel plugin lives at `integrations/omarchy/omascribe-control/` and
is installed by `./setup.sh` into `~/.config/omarchy/plugins/`.

## Usage

```
omascribe
```

| Key | Action |
|-----|--------|
| `r` | Start recording |
| `s` | Stop and process |
| `x` | Cancel recording |
| `o` | Open in editor |
| `e` | Edit title |
| `t` | View transcript |
| `T` | Manage tags |
| `d` | Delete (discard, on a queued job) |
| `R` | Retry a queued or failed job |
| `m` | Move a meeting to another category (folder layout) |
| `,` | Settings |
| `A` | Audio test |
| `q` | Quit |
| `j/k` or `↑↓` | Navigate |
| `/` | Search |
| `1` / `2` | Focus Meetings / Note pane |

During recording, write notes in the text area — they're fed to the AI as extra context.

### Processing queue

Stopping a recording queues it and returns immediately — you can start the
next meeting while the last one is still uploading. Queued jobs live in
`~/.local/state/omascribe/jobs/` and survive failures and restarts:

- network errors, timeouts and HTTP 408/429/5xx retry with backoff (1, 5, 15,
  then 60 minutes; 5 attempts); anything else fails straight away;
- a cloud transcription resumes from its saved transcript id instead of
  uploading again, and a finished transcript is cached so writing the note
  can be retried on its own;
- closing the app mid-job is safe — the job resumes on next launch.

Queued and failed recordings appear at the top of the meetings list.
Select one and press `R` to retry now, or `d` to discard the job (the audio
file is always kept). A job that gives up sends a desktop notification, and
the Omarchy bar widget shows a warning glyph with the failures listed in its
panel, even while the TUI is closed.

## AI Setup

Cloud (fast, recommended):
```bash
./setup_cloud.sh
# or press `,` in the app and pick a provider
```

Local (free, private, slower):
```bash
curl -fsSL https://ollama.com/install.sh | sh
ollama pull llama3.2:3b
```

Or skip AI entirely — set `ai_provider: none` in settings for transcription-only.

Claude through an OpenAI-compatible endpoint — `ai_model: haiku | sonnet | opus`:

| `ai_provider` | Key | Notes |
|---|---|---|
| `assemblyai` | `ASSEMBLYAI_API_KEY` | AssemblyAI's LLM Gateway; the same key as cloud transcription below. Model access is enabled per account. |
| `deepinfra` | `DEEPINFRA_API_KEY` | DeepInfra's OpenAI-compatible API. |

Both are small subclasses of `OpenAICompatibleSummarizer` (a base URL, an env
var and a tier → model-id table), so another OpenAI-compatible host is a few
lines.

## Cloud transcription (optional)

Local Whisper is the default. For faster transcription with **speaker
labels** (`Speaker A:` / `Speaker B:`, which also lets the summary name who
owns each action item), switch to [AssemblyAI](https://www.assemblyai.com/):

```yaml
transcriber: assemblyai       # whisper (default) | assemblyai
```

Set `ASSEMBLYAI_API_KEY` in the environment (or `assemblyai_api_key` in the
config, or Settings → AI → Transcription). The recording is uploaded as 16 kHz
mono FLAC — lossless for speech recognition and about a tenth of the WAV's
size — with retries if the connection drops. Audio leaves your machine in this
mode; use Whisper for meetings that must not.

Whisper is an install extra, so a cloud-only install needs no torch:

```bash
pip install -e ".[assemblyai]"     # cloud transcription only
pip install -e ".[all]"            # everything, including Whisper (what setup.sh installs)
```

## Output

Notes are saved as markdown in `notes/`:

```markdown
---
title: "Sprint Planning"
date: 2026-08-18
duration_seconds: 1860
word_count: 4230
tags: [meeting, auto-generated]
---

# Sprint Planning

**Date:** August 18, 2026 at 2:30 PM  
**Duration:** 31 minutes  
**Words:** 4,230

## AI Summary
...

### Action Items
- Sarah to send preview link by tomorrow morning
```

Full transcripts with timestamps are saved separately in `transcripts/`.

### Categories and one folder per meeting (optional)

Set `meetings_dir` and a list of `categories` to keep each meeting's note,
transcript and audio together, grouped by category:

```yaml
meetings_dir: ~/Documents/Meetings
categories: [Work, Clients, Personal]
```

```
~/Documents/Meetings/<Category>/<YYYY-MM-DD-HHMMSS-title>/<same>.md   note
                                                         <same>.txt  transcript
                                                         <same>.wav  audio
~/Documents/Meetings/Uncategorised/...                               no category
```

A **Category** dropdown appears under the meeting title while recording; the
category is shown and searchable in the meetings list and given to the AI as
context. `m` moves a saved meeting to another category. With `meetings_dir`
empty (the default) the flat `notes/` + `transcripts/` layout is unchanged.
`omascribe-migrate-folders` moves existing flat notes (dry run by default,
`--apply` to act), matching recordings by timestamp.

## Audio

**Recording modes:** `combined` (mic + system, default), `mic`, `system`

**Device selection:** Pick specific mic and output devices in Settings → Audio, or use system default.

**Audio Test** (`A` from main view) records a 5-second clip and diagnoses whether your meeting app's audio is actually hitting the captured sink. Catches common traps like Zoom routing to a different output.

## Configuration

Settings are stored in `~/.config/omascribe/config.yaml`:

```yaml
ai_provider: anthropic        # none | openai | anthropic | openrouter | assemblyai | deepinfra | local
ai_model: haiku               # haiku/sonnet | mini/standard | cheap/balanced/premium
whisper_model: base           # tiny | base | small | medium | large
whisper_device: cpu           # cpu | cuda | auto
recording_mode: combined      # mic | system | combined
editor: nvim
notes_dir: notes
transcripts_dir: transcripts
transcriber: whisper          # whisper | assemblyai
```

## Development

```bash
python -m venv venv
source venv/bin/activate
pip install -e ".[all,dev]"
pytest          # 127 tests
ruff check omascribe/ tests/
```

## License

MIT
