# music-ai-toolkit

Python tools that let an AI assistant work inside a real music production workflow (FL Studio 2026):
take a reference beat apart, turn audio into MIDI, read and write FL Studio project files.

I built this in October and November 2026 with **Claude Code**, as the core of a private Claude Code plugin
I use for my own productions. This repository is a curated extract: the scripts I wrote, without the
FL Studio manual and the third-party code that the private version bundles.

I am not a trained developer. I come from marketing and from ten years as a DJ, and I used AI-assisted
development to build tools I actually needed. The interesting part, for me, was the method: verify each
step against real files instead of trusting what the model assumes.

## What is inside

| Tool | What it does | Stack |
|---|---|---|
| `tools/analyze_audio.py` | From a beat (mp3/wav) to 6 stems and MIDI per instrument, ready to drag into FL Studio | Demucs (`htdemucs_6s`), Basic Pitch, librosa, mido |
| `tools/loop_remake.py` | "Loop-consensus" transcription: beats are loops, so it folds the transcription onto the loop and keeps only the notes that come back in most repetitions. Cleaner MIDI than transcribing bar by bar | numpy, on top of `analyze_audio` |
| `tools/flp_inspect.py` | Read-only inspector for `.flp` files (FL 12 to FL 2026): tempo, channels, samples, plugins, patterns and notes, mixer, arrangement. Exports a pattern to MIDI | standard library only |
| `tools/flp_write.py` | Writes a **new** `.flp` project: Sampler channels with their samples, named patterns with notes, playlist clips, optional audio clip of the reference | standard library only |
| `tools/flp_from_remake.py` | Glue: from the `loop_remake` output to an FL project that opens in time with the original | |
| `tools/evdiff.py` | Event-level diff between two `.flp` files: the tool used to reverse-engineer the format | standard library only |
| `tools/make_midi.py` | MIDI from chord symbols (`Cm9 Abmaj7 Ebmaj7 Bb6`), drum grids (`kick: x...x...`) or JSON | standard library only |
| `tools/sound_index.py` | Index and search for a sample library: category, tags, key and BPM from file names, WAV header parsing, rough pitch estimate for 808s and basses | standard library only |

## How the .flp format was reverse-engineered

FL Studio's project format is binary and only partly documented. The method:

1. Save an empty project, then copies of it with **one** change each (tempo, one note, one sample,
   one playlist clip, one marker, one mixer change).
2. Diff them at event level with `evdiff.py` to see which event IDs change and how.
3. Check the hypotheses against real projects of mine with `flp_inspect.py`.
4. Write new files with `flp_write.py` and open them in FL Studio: the only real test.

The notes are in [`docs/flp-format.it.md`](docs/flp-format.it.md) (in Italian), with each finding marked as
confirmed by a diff, seen in real projects, or still to verify.

## Quick start

```bash
# no dependencies
python3 tools/make_midi.py chords "Cm9 Abmaj7 Ebmaj7 Bb6" -o chords.mid --bpm 130 --bars 1
python3 tools/make_midi.py drums "kick: x.....x...x.....|snare: ....X.......X...|hat: x.x.x.x.x.x.x.x." -o drums.mid --bpm 140
python3 tools/flp_inspect.py MyProject.flp            # summary
python3 tools/flp_inspect.py MyProject.flp --json     # full summary as JSON

# audio tools (Python 3.11 recommended)
python3 -m venv .venv-audio && .venv-audio/bin/pip install -r requirements-audio.txt
.venv-audio/bin/python tools/analyze_audio.py BEAT.mp3 --stems stems/ --midi midi/ --bpm 98
```

`flp_write.py` needs two template projects saved from FL Studio 2026 in `templates/`
(`empty-fl26.flp` and `audio-clip-channel.flp`). They are not included: project files store local paths.

## Notes

- Code comments are partly in Italian, my working language. Happy to walk through any part in English.
- The private plugin also includes a skill for the assistant, setup scripts for macOS and Windows and an
  MCP server for live control of the DAW, based on [fl-studio-mcp](https://github.com/karl-andres/fl-studio-mcp) (MIT).

Marco Zambon · [github.com/ZambxN](https://github.com/ZambxN)
