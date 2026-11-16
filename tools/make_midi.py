#!/usr/bin/env python3
"""Write MIDI files to drag into FL Studio. Standard library only.

Note names follow FL Studio's convention: C5 = MIDI 60 (middle C).

Chords from symbols (one chord per N bars, or per beat count with "Cm9:2"):
    python3 make_midi.py chords "Cm9 Abmaj7 Ebmaj7 Bb6" -o chords.mid --bpm 130 --bars 1 \
        --octave 5 --voicing spread --bass
Drum grid (x = hit, X = accent, . = rest; 16 steps = 1 bar by default):
    python3 make_midi.py drums "kick: x.....x...x.....|snare: ....X.......X...|hat: x.x.x.x.x.x.x.x." \
        -o drums.mid --bpm 140
Anything else, from JSON:
    python3 make_midi.py json spec.json -o out.mid
    spec = {"bpm": 130, "ppq": 96, "tracks": [
              {"name": "melody", "notes": [[0, 0.5, "D#5", 100], [0.5, 1.5, 63, 90]]}]}
    note = [start_beat, length_beats, note name or MIDI number, velocity 1-127]
"""
from __future__ import annotations

import argparse
import json
import struct

PPQ = 96
NOTE_INDEX = {"C": 0, "D": 2, "E": 4, "F": 5, "G": 7, "A": 9, "B": 11}

CHORDS = {
    "": [0, 4, 7], "maj": [0, 4, 7], "M": [0, 4, 7], "m": [0, 3, 7], "min": [0, 3, 7], "-": [0, 3, 7],
    "dim": [0, 3, 6], "o": [0, 3, 6], "aug": [0, 4, 8], "+": [0, 4, 8], "5": [0, 7],
    "sus2": [0, 2, 7], "sus4": [0, 5, 7], "sus": [0, 5, 7], "6": [0, 4, 7, 9], "m6": [0, 3, 7, 9],
    "7": [0, 4, 7, 10], "maj7": [0, 4, 7, 11], "M7": [0, 4, 7, 11], "m7": [0, 3, 7, 10], "-7": [0, 3, 7, 10],
    "m7b5": [0, 3, 6, 10], "ø": [0, 3, 6, 10], "dim7": [0, 3, 6, 9], "o7": [0, 3, 6, 9],
    "mmaj7": [0, 3, 7, 11], "mM7": [0, 3, 7, 11], "7sus4": [0, 5, 7, 10], "7sus": [0, 5, 7, 10],
    "add9": [0, 4, 7, 14], "madd9": [0, 3, 7, 14], "6/9": [0, 4, 7, 9, 14], "m6/9": [0, 3, 7, 9, 14],
    "9": [0, 4, 7, 10, 14], "maj9": [0, 4, 7, 11, 14], "M9": [0, 4, 7, 11, 14], "m9": [0, 3, 7, 10, 14],
    "7b9": [0, 4, 7, 10, 13], "7#9": [0, 4, 7, 10, 15], "11": [0, 4, 7, 10, 14, 17],
    "m11": [0, 3, 7, 10, 14, 17], "maj7#11": [0, 4, 7, 11, 18], "13": [0, 4, 7, 10, 14, 21],
    "maj13": [0, 4, 7, 11, 14, 21], "m13": [0, 3, 7, 10, 14, 21],
}

# FL Studio's FPC / General MIDI-ish drum map (C5 = 60 convention: kick 36 = C3)
DRUMS = {"kick": 36, "bd": 36, "snare": 38, "sd": 38, "rim": 37, "clap": 39, "cp": 39,
         "hat": 42, "hh": 42, "chh": 42, "pedal": 44, "ohat": 46, "oh": 46, "ohh": 46,
         "tom": 45, "ltom": 41, "mtom": 45, "htom": 48, "crash": 49, "ride": 51,
         "perc": 56, "cowbell": 56, "shaker": 70, "808": 36}


def parse_note(n) -> int:
    if isinstance(n, int):
        return n
    s = str(n).strip()
    if s.lstrip("-").isdigit():
        return int(s)
    pc = NOTE_INDEX[s[0].upper()]
    i = 1
    while i < len(s) and s[i] in "#b":
        pc += 1 if s[i] == "#" else -1
        i += 1
    return pc + 12 * int(s[i:])


def parse_chord(sym: str) -> tuple[int, list[int], int | None]:
    """'Ebmaj7/G' -> (root pitch class, intervals, bass pitch class or None)."""
    bass = None
    if "/" in sym and sym.split("/")[-1][:1].upper() in NOTE_INDEX and sym not in ("6/9", "m6/9"):
        sym, b = sym.rsplit("/", 1)
        bass = parse_note(b + "0") % 12
    root = NOTE_INDEX[sym[0].upper()]
    i = 1
    while i < len(sym) and sym[i] in "#b":
        root += 1 if sym[i] == "#" else -1
        i += 1
    quality = sym[i:]
    if quality not in CHORDS:
        raise SystemExit(f"unknown chord quality '{quality}' in '{sym}'. Known: {sorted(CHORDS)}")
    return root % 12, CHORDS[quality], bass


def voice(root_pc: int, intervals: list[int], octave: int, voicing: str) -> list[int]:
    base = root_pc + 12 * octave
    notes = [base + iv for iv in intervals]
    if voicing == "spread" and len(notes) >= 3:
        notes[0] -= 12                       # root down an octave, rest stays: open, wide sound
    elif voicing == "drop2" and len(notes) >= 4:
        top = sorted(notes)
        top[-2] -= 12
        notes = top
    elif voicing == "shell" and len(notes) >= 4:
        notes = [notes[0], notes[1], notes[3]]  # root, 3rd, 7th
    return sorted(notes)


class Track:
    def __init__(self, name: str, channel: int = 0):
        self.name, self.channel, self.events = name, channel, []

    def note(self, start: float, length: float, key: int, vel: int = 100):
        s, e = round(start * PPQ), round((start + length) * PPQ)
        self.events.append((s, 1, bytes([0x90 | self.channel, key, max(1, min(127, vel))])))
        self.events.append((max(e, s + 1), 0, bytes([0x80 | self.channel, key, 0])))


def _vlq(n: int) -> bytes:
    out = [n & 0x7F]
    while n > 127:
        n >>= 7
        out.insert(0, (n & 0x7F) | 0x80)
    return bytes(out)


def write(path: str, tracks: list[Track], bpm: float) -> None:
    chunks = [b"\x00\xff\x51\x03" + int(60_000_000 / bpm).to_bytes(3, "big") +
              b"\x00\xff\x58\x04\x04\x02\x18\x08\x00\xff\x2f\x00"]
    for t in tracks:
        name = t.name.encode()[:120]
        data, now = b"\x00\xff\x03" + _vlq(len(name)) + name, 0
        for tick, _, msg in sorted(t.events, key=lambda e: (e[0], e[1])):
            data += _vlq(tick - now) + msg
            now = tick
        chunks.append(data + b"\x00\xff\x2f\x00")
    with open(path, "wb") as f:
        f.write(b"MThd" + struct.pack(">IHHH", 6, 1, len(chunks), PPQ))
        for c in chunks:
            f.write(b"MTrk" + struct.pack(">I", len(c)) + c)
    n = sum(len(t.events) // 2 for t in tracks)
    print(f"wrote {path}: {n} notes, {len(tracks)} track(s), {bpm} BPM, PPQ {PPQ}")


def cmd_chords(a) -> list[Track]:
    chords, bass = Track("Chords"), Track("Bass", 1)
    beat = 0.0
    for tok in a.progression.replace("|", " ").split():
        sym, _, dur = tok.partition(":")
        length = float(dur) if dur else a.bars * 4
        root, ivs, slash = parse_chord(sym)
        for i, k in enumerate(voice(root, ivs, a.octave, a.voicing)):
            chords.note(beat + i * a.strum / 1000 * a.bpm / 60, length * a.legato, k, a.velocity)
        if a.bass:
            bass.note(beat, length * a.legato, (slash if slash is not None else root) + 12 * (a.octave - 2), a.velocity)
        beat += length
    return [chords] + ([bass] if a.bass else [])


def cmd_drums(a) -> list[Track]:
    tracks = []
    step = 4 / a.steps_per_bar
    for i, row in enumerate(r for r in a.grid.split("|") if r.strip()):
        name, _, pattern = row.partition(":")
        name = name.strip()
        key = DRUMS.get(name.lower()) if not name.lstrip("-").isdigit() else int(name)
        if key is None:
            key = parse_note(name)
        t = Track(name, 9)
        for j, ch in enumerate(pattern.replace(" ", "")):
            if ch in "xX":
                t.note(j * step, step * 0.9, key, 127 if ch == "X" else a.velocity)
        tracks.append(t)
    return tracks


def cmd_json(a) -> list[Track]:
    global PPQ
    spec = json.load(open(a.spec))
    PPQ = spec.get("ppq", PPQ)
    a.bpm = spec.get("bpm", a.bpm)
    tracks = []
    for i, tr in enumerate(spec["tracks"]):
        t = Track(tr.get("name", f"Track {i + 1}"), tr.get("channel", i % 16))
        for n in tr["notes"]:
            t.note(float(n[0]), float(n[1]), parse_note(n[2]), int(n[3]) if len(n) > 3 else 100)
        tracks.append(t)
    return tracks


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("chords")
    c.add_argument("progression")
    c.add_argument("--bars", type=float, default=1, help="bars per chord unless given as Chord:beats")
    c.add_argument("--octave", type=int, default=5, help="octave of the chord root (FL: 5 = middle C)")
    c.add_argument("--voicing", choices=["close", "spread", "drop2", "shell"], default="close")
    c.add_argument("--bass", action="store_true", help="add a bass track with roots (or slash bass)")
    c.add_argument("--strum", type=float, default=0, help="strum in ms between chord notes")
    c.add_argument("--legato", type=float, default=1.0, help="note length factor (0.5 = staccato)")
    c.add_argument("--velocity", type=int, default=96)
    d = sub.add_parser("drums")
    d.add_argument("grid")
    d.add_argument("--steps-per-bar", type=int, default=16)
    d.add_argument("--velocity", type=int, default=100)
    j = sub.add_parser("json")
    j.add_argument("spec")
    for p in (c, d, j):
        p.add_argument("-o", "--out", required=True)
        p.add_argument("--bpm", type=float, default=120)
    a = ap.parse_args()
    tracks = {"chords": cmd_chords, "drums": cmd_drums, "json": cmd_json}[a.cmd](a)
    write(a.out, tracks, a.bpm)


if __name__ == "__main__":
    main()
