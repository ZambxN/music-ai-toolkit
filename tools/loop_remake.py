#!/usr/bin/env python3
"""loop_remake.py - remake "tale quale" di un type beat: trascrizione per consenso sul loop.

I type beat sono loop (di solito 8 battute) ripetuti con strumenti che entrano ed escono. Trascrivere
ogni battuta da sola da' note sporche; qui si trascrive tutto, si "piega" sul loop e si tengono solo
le note che tornano nella maggior parte dei giri in cui lo strumento suona. Poi il loop pulito viene
rimesso su tutta la durata, solo dove lo strumento suona nell'originale.

    .venv-audio/bin/python skills/fl-studio/tools/loop_remake.py BEAT.mp3 --stems DIR_STEM --out DIR \
        --bpm 100 [--loop 8] [--key "D maggiore"]

Tempo: tick 0 = INIZIO DELL'AUDIO (non la battuta 1 stimata): la pattern va messa nello stesso punto
della clip del type beat in playlist, e resta a tempo con le voci registrate sopra. Le note stanno
sulla griglia del beat, che puo' essere spostata di qualche tick rispetto a quella di FL (scritto in
LEGGIMI.txt).
Usa le funzioni di analyze_audio.py (stem, griglia, batteria, basic-pitch).
"""
import argparse
import os
import sys
from collections import defaultdict

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import analyze_audio as A  # noqa: E402

PPQ = A.PPQ
BAR = 4 * PPQ


def loop_length(harm, g, n_bars, candidates=(4, 8, 16)):
    import librosa
    hop = 2048
    C = librosa.feature.chroma_cqt(y=harm, sr=A.SR, hop_length=hop)
    t = librosa.times_like(C, sr=A.SR, hop_length=hop)
    feats = []
    for b in range(n_bars * 2):
        s = g.origin + b * 2 * g.beat
        sel = (t >= s) & (t < s + 2 * g.beat)
        v = C[:, sel].mean(1) if sel.any() else np.zeros(12)
        feats.append(v / (np.linalg.norm(v) + 1e-9))
    F = np.array(feats)
    scores = {}
    for L in candidates:
        k = 2 * L
        if len(F) > 2 * k:
            scores[L] = float(np.mean([F[i] @ F[i + k] for i in range(len(F) - k)]))
    best = max(scores, key=lambda L: scores[L] + 0.01 * (L == 8))
    return best, scores


def consensus(notes, L, n_bars, min_share=0.5, active_min=0.25, tol=24, active_reps=None):
    """notes: (tick, dur, pitch, vel) relativi alla battuta 1. Ritorna (loop_notes, ripetizioni attive)"""
    LT = L * BAR
    reps = (n_bars + L - 1) // L
    per_rep = defaultdict(list)
    for n in notes:
        if n[0] >= 0:
            per_rep[n[0] // LT].append(n)
    if not per_rep:
        return [], []
    if active_reps is not None:
        active = [r for r in active_reps if per_rep[r]]
    else:
        counts = [len(per_rep[r]) for r in range(reps)]
        peak = max(counts)
        active = [r for r in range(reps) if counts[r] >= active_min * peak]
    pool = defaultdict(list)  # pitch -> [(pos nel loop, giro, dur, vel)]
    for r in active:
        for t, d, p, v in per_rep[r]:
            pool[p].append((t - r * LT, r, d, v))
    need = max(2, int(np.ceil(min_share * len(active)))) if len(active) >= 2 else 1
    loop = []
    for p, lst in pool.items():
        lst.sort()
        clusters, cur = [], [lst[0]]
        for x in lst[1:]:
            if x[0] - cur[-1][0] <= tol:
                cur.append(x)
            else:
                clusters.append(cur)
                cur = [x]
        clusters.append(cur)
        for cl in clusters:
            if len({x[1] for x in cl}) >= need:
                pos = int(np.median([x[0] for x in cl]))
                loop.append((pos, int(np.median([x[2] for x in cl])), p, int(np.median([x[3] for x in cl]))))
    return sorted(loop), active


def rep_sections(harm, g, L, n_bars, thr=0.82):
    """raggruppa i giri del loop per somiglianza armonica (sequenza di cromagrammi a mezze battute):
    ritorna {lettera: [giri]}. Giri quasi muti vanno nella sezione piu' vicina."""
    import librosa
    hop = 2048
    C = librosa.feature.chroma_cqt(y=harm, sr=A.SR, hop_length=hop)
    t = librosa.times_like(C, sr=A.SR, hop_length=hop)
    reps = (n_bars + L - 1) // L
    vecs = []
    for r in range(reps):
        v = []
        for h in range(L * 2):
            s0 = g.origin + (r * L * 2 + h) * 2 * g.beat
            sel = (t >= s0) & (t < s0 + 2 * g.beat)
            c = C[:, sel].mean(1) if sel.any() else np.zeros(12)
            v.append(c / (np.linalg.norm(c) + 1e-9))
        v = np.concatenate(v)
        vecs.append(v / (np.linalg.norm(v) + 1e-9))
    cents, members = [], []
    for r, v in enumerate(vecs):
        sims = [float(v @ (c / np.linalg.norm(c))) for c in cents]
        if sims and max(sims) >= thr:
            k = int(np.argmax(sims))
            members[k].append(r)
            cents[k] = cents[k] + v
        else:
            cents.append(v.copy())
            members.append([r])
    # sezioni con un solo giro e simili a un'altra almeno a 0.7: unite (code/finali parziali)
    letters = "ABCDEFGH"
    return {letters[k]: m for k, m in enumerate(members)}


def tolerant(notes, slack=12):
    """unisce le note quasi coincidenti (stessa altezza, entro `slack` tick) prima del voto"""
    out = []
    for n in sorted(notes, key=lambda x: (x[2], x[0])):
        if out and out[-1][2] == n[2] and abs(out[-1][0] - n[0]) <= slack:
            continue
        out.append(n)
    return sorted(out)


def expand(loop, active, L, shift):
    LT = L * BAR
    return [(t + r * LT + shift, d, p, v) for r in active for t, d, p, v in loop if t + r * LT + shift >= 0]


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("audio")
    ap.add_argument("--stems", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--bpm", type=float, required=True)
    ap.add_argument("--loop", type=int)
    ap.add_argument("--share", type=float, default=0.4, help="quota minima di giri in cui una nota deve tornare")
    a = ap.parse_args()

    st = A.separate(a.audio, a.stems)
    y = {s: A.load(p) for s, p in st.items()}
    mix = A.load(a.audio)
    bpm, _, origin = A.estimate_grid(mix, y["drums"], a.bpm)
    feel, _, _ = A.detect_feel(A.drum_onsets(y["drums"]), origin, 60 / bpm)
    g = A.Grid(bpm, origin, feel)
    dr = A.drum_hits(y["drums"], g)
    c = A.backbeat_shift(dr)
    if c:
        origin -= c * g.beat
        origin -= 4 * g.beat * round(origin / (4 * g.beat))
        g = A.Grid(bpm, origin, feel)
        dr = A.drum_hits(y["drums"], g)
    # battuta 1 = primo downbeat dentro l'audio (origin >= 0), cosi' nessuna nota finisce prima del tick 0
    while g.origin < 0:
        g = A.Grid(bpm, g.origin + 4 * g.beat, feel)
        dr = A.drum_hits(y["drums"], g)
    dur = len(mix) / A.SR
    n_bars = int(np.ceil((dur - g.origin) / (4 * g.beat)))
    harm = y["piano"] + y["guitar"] + y["other"]
    L, scores = (a.loop, {}) if a.loop else loop_length(harm + y["bass"], g, n_bars)
    shift = int(round(g.origin / g.beat * PPQ))  # tick audio = tick battuta + shift
    sections = rep_sections(harm + y["bass"], g, L, n_bars)

    parts = {"Kick": dr["Kick"], "Snare": dr["Snare"], "HiHat": dr["HiHat"]}
    rms_mix = np.sqrt(np.mean(mix ** 2)) + 1e-9
    act = {s: np.sqrt(np.mean(y[s] ** 2)) / rms_mix > 0.015 for s in A.STEMS6}
    if act["bass"]:
        parts["Basso"] = A.transcribe(st["bass"], g, 30, 400, mono=True, min_amp=0.2)
    for s, label, lo, hi in [("piano", "Piano", 60, 3000), ("guitar", "Chitarra", 70, 2500),
                             ("other", "Synth (other)", 60, 4000), ("vocals", "Voce chop", 80, 1200)]:
        if act[s]:
            parts[label] = A.transcribe(st[s], g, lo, hi, min_amp=0.3)

    reps = (n_bars + L - 1) // L

    def rep_active(sig):
        r = []
        for k in range(reps):
            s0 = int(max(g.origin + k * L * 4 * g.beat, 0) * A.SR)
            s1 = int((g.origin + (k + 1) * L * 4 * g.beat) * A.SR)
            seg = sig[s0:s1]
            r.append(np.sqrt(np.mean(seg ** 2)) if len(seg) else 0.0)
        r = np.array(r)
        return [k for k in range(reps) if r[k] > 0.3 * r.max()]
    stem_of = {"Kick": "drums", "Snare": "drums", "HiHat": "drums", "Basso": "bass", "Piano": "piano",
               "Chitarra": "guitar", "Synth (other)": "other", "Voce chop": "vocals"}

    os.makedirs(a.out, exist_ok=True)
    full, loops, report = [], [], []
    spec = {"bpm": bpm, "key": None, "loop_bars": L, "shift_ticks": shift, "n_bars": n_bars,
            "audio": os.path.abspath(a.audio), "audio_seconds": dur, "parts": {}}
    multi = len(sections) > 1
    for name, notes in parts.items():
        on = set(rep_active(y[stem_of[name]]))
        all_full = []
        for sec, reps_in in sections.items():
            act = [r for r in reps_in if r in on]
            if not act:
                continue
            lp, active = consensus(tolerant(notes), L, n_bars, a.share if len(act) > 2 else 0.5, active_reps=act)
            if not lp:
                continue
            nm = f"{name} {sec}" if multi else name
            loops.append((nm, lp))
            all_full += expand(lp, active, L, shift)
            spec["parts"][nm] = {"loop": lp, "active_reps": active, "base": name, "section": sec}
            bars_on = ", ".join(f"{r * L + 1}-{(r + 1) * L}" for r in active)
            report.append(f"  {nm}: {len(lp)} note nel loop, suona nei giri {bars_on}")
        if all_full:
            full.append((name, sorted(all_full)))
        else:
            report.append(f"  {name}: niente di stabile (scartato)")

    # accordi: cromagramma medio di ogni mezza battuta del loop su tutti i giri
    key, chords = A.chords_and_key(harm, y["bass"], g, n_bars)
    import librosa
    hop = 2048
    C = librosa.feature.chroma_cqt(y=harm, sr=A.SR, hop_length=hop)
    Cb = librosa.feature.chroma_cqt(y=y["bass"], sr=A.SR, hop_length=hop)
    t = librosa.times_like(C, sr=A.SR, hop_length=hop)
    T = A.chord_templates()
    spec["sections"] = sections
    sec_chords = {}
    for sec, reps_in in sections.items():
      loop_chords = []
      for h in range(L * 2):
        acc, accb = np.zeros(12), np.zeros(12)
        for r in reps_in:
            s = g.origin + (r * L * 2 + h) * 2 * g.beat
            sel = (t >= s) & (t < s + 2 * g.beat)
            if sel.any():
                acc += C[:, sel].mean(1)
                accb += Cb[:, sel].mean(1)
        v = acc / (np.linalg.norm(acc) + 1e-9) + 0.6 * (accb == accb.max()) * accb.max() / (np.linalg.norm(accb) + 1e-9)
        v = v / np.linalg.norm(v)
        name, root, iv, _ = max(T, key=lambda x: float(x[3] @ v) - 0.02 * len(x[2]))
        loop_chords.append((h * 2 * PPQ, name, root, iv))
      merged = []
      for ch in loop_chords:
          if not merged or merged[-1][1] != ch[1]:
              merged.append(ch)
      sec_chords[sec] = merged
      chord_loop = A.chord_notes(merged, L * BAR)
      nm = f"Accordi (blocchi) {sec}" if multi else "Accordi (blocchi)"
      full.append((nm, expand(chord_loop, reps_in, L, shift)))
      loops.append((nm, chord_loop))
      spec["parts"][nm] = {"loop": chord_loop, "active_reps": reps_in, "base": "Accordi (blocchi)", "section": sec}
    spec["key"] = key
    spec["chords"] = {sec: [(tk, n) for tk, n, _, _ in m] for sec, m in sec_chords.items()}
    import json
    json.dump(spec, open(os.path.join(a.out, "remake.json"), "w"), indent=1)

    A.write_mid(os.path.join(a.out, "00 TUTTO (tale quale).mid"), full, bpm)
    for i, (name, notes) in enumerate(full, 1):
        A.write_mid(os.path.join(a.out, f"{i:02d} {name}.mid"), [(name, notes)], bpm)
    A.write_mid(os.path.join(a.out, f"LOOP {L} battute (tutte le parti).mid"), loops, bpm)

    off_ticks = shift % (PPQ // 4)
    L_ = [f"REMAKE TALE QUALE - {os.path.basename(a.audio)}", "",
          f"BPM {bpm:g}, feel {feel}, tonalita' stimata {key}",
          f"Loop: {L} battute" + (f" (somiglianza tra giri: {', '.join(f'{k}={v:.2f}' for k, v in scores.items())})" if scores else ""),
          f"Battuta 1 del beat: {g.origin * 1000:.0f} ms dopo l'inizio dell'audio = tick {shift} della pattern.",
          "", "COME MONTARLO",
          "Metti la pattern ESATTAMENTE dove inizia la clip del type beat in playlist (stessa battuta, stesso tick).",
          "Il tick 0 dei MIDI e' l'inizio dell'audio: cosi' tutto resta a tempo con le voci gia' registrate.",
          (f"La griglia del beat e' spostata di {off_ticks} tick rispetto a quella di FL: in piano roll le note "
           "sembrano leggermente fuori griglia, ma suonano a tempo col beat." if off_ticks else
           "La griglia del beat coincide con quella di FL."),
          "", "SEZIONI (giri di " + str(L) + " battute): " + "; ".join(
              f"{k} = " + ", ".join(f"{r * L + 1}-{(r + 1) * L}" for r in v) for k, v in sections.items()),
          "", "ACCORDI DEL LOOP (mezze battute)"]
    for sec, merged in sec_chords.items():
        L_.append(f"  sezione {sec}: " + " | ".join(
            f"{tk // BAR + 1}.{(tk % BAR) // PPQ + 1} {name}" for tk, name, r, iv in merged))
    L_ += ["", "PARTI"] + report
    L_ += ["", "File: '00 TUTTO (tale quale).mid' = tutte le parti su tutta la durata (una traccia per strumento);",
           f"'LOOP {L} battute' = solo il giro pulito, da usare se vuoi riarrangiarlo tu; un .mid per parte."]
    open(os.path.join(a.out, "LEGGIMI.txt"), "w").write("\n".join(L_) + "\n")
    print("\n".join(L_))


if __name__ == "__main__":
    main()
