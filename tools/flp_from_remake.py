#!/usr/bin/env python3
"""flp_from_remake.py - dal remake.json di loop_remake.py a un progetto FL (.flp) pronto da aprire.

    python3 flp_from_remake.py remake.json --out "CHANCE - REMAKE [claude].flp" \
        --sound "Kick=/percorso/kick.wav" --sound "Basso=/percorso/bass.wav@36" ...

--sound PARTE=CAMPIONE[@ROOT]: ROOT = nota MIDI reale del campione (36 = C2). Le note della parte vengono
trasposte (tasto = nota - ROOT + 60) cosi' il Sampler di FL, che suona il campione alla sua altezza sul
C5 (60), le fa suonare giuste. Senza @ROOT (batteria) la nota resta com'e'.
Track 1 = il type beat come clip audio di riferimento (tick 0 = inizio audio). Ogni parte e' un pattern
di un loop, messo in playlist nei giri in cui suona nell'originale, spostato di shift_ticks (la battuta 1
del beat rispetto all'inizio dell'audio): cosi' tutto resta a tempo col type beat e con le voci.
Parti senza --sound: il pattern viene creato ma non messo in playlist.
"""
import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from flp_write import Project, PPQ  # noqa: E402

BAR = 4 * PPQ


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("spec")
    ap.add_argument("--out", required=True)
    ap.add_argument("--sound", action="append", default=[], help="PARTE=CAMPIONE[@ROOT]")
    ap.add_argument("--off", action="append", default=[], help="parte da creare ma non mettere in playlist")
    ap.add_argument("--no-reference", action="store_true", help="non mettere il type beat su Track 1")
    a = ap.parse_args()

    spec = json.load(open(a.spec))
    sounds = {}
    for s in a.sound:
        part, rest = s.split("=", 1)
        path, root = (rest.rsplit("@", 1) + [None])[:2] if "@" in rest else (rest, None)
        sounds[part] = (path, int(root) if root else None)
    for part, (path, _) in sounds.items():
        if not os.path.exists(path):
            sys.exit(f"campione non trovato per {part}: {path}")

    p = Project(spec["bpm"])
    L, shift = spec["loop_bars"], spec["shift_ticks"]
    track = 1
    if not a.no_reference:
        ref = p.add_audio_channel("TYPE BEAT (riferimento)", spec["audio"])
        p.add_audio_clip(ref, 0, int(round(spec["audio_seconds"] * spec["bpm"] / 60 * PPQ)), track)
    channels, tracks = {}, {}
    for part, info in spec["parts"].items():
        base = info.get("base", part)
        notes = [tuple(n) for n in info["loop"]]
        if base not in sounds:
            print(f"  {part}: nessun suono indicato per '{base}', saltata")
            continue
        path, root = sounds[base]
        if base not in channels:
            channels[base] = p.add_sampler(base, path)
        ch = channels[base]
        out = []
        for t, d, pitch, v in notes:
            key = pitch - root + 60 if root is not None else pitch
            if 0 <= key <= 127:
                out.append((t, d, ch, key, v))
        sec = f" sez. {info['section']}" if "section" in info else ""
        pat = p.add_pattern(f"{base}{sec} (loop {L} batt.)", out)
        if base in a.off:
            continue
        if base not in tracks:
            track += 1
            tracks[base] = track
        for r in info["active_reps"]:
            p.add_clip(pat, shift + r * L * BAR, L * BAR, tracks[base])
    print(p.save(a.out))


if __name__ == "__main__":
    main()
