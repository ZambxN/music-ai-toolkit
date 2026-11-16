#!/usr/bin/env python3
"""analyze_audio.py - da un beat (mp3/wav) a stem + MIDI pronti da trascinare in FL Studio.

Gira nel venv audio del repo (Python 3.11: demucs, basic-pitch, librosa, mido):

    .venv-audio/bin/python skills/fl-studio/tools/analyze_audio.py BEAT.mp3 \
        --stems DIR_STEM --midi DIR_MIDI [--bpm 98] [--flp PROGETTO.flp] [--name "SATISFACTION"]

Cosa produce:
- DIR_STEM/: 6 stem demucs (htdemucs_6s): drums, bass, piano, guitar, other, vocals.
- DIR_MIDI/: "00 TUTTO.mid" (una traccia per strumento: FL crea un canale per traccia) e un .mid per
  strumento. Kick/Snare/HiHat sono a nota C5 (60) cosi' un Sampler suona il campione alla sua altezza.
  Accordi = blocchi semplificati (radice in ottava 3 + note sopra) ricavati dal cromagramma degli stem
  armonici; Piano/Chitarra/Altro = trascrizione basic-pitch quantizzata; Basso = monofonico.
- DIR_MIDI/ANALISI.txt: BPM, tonalita', offset della griglia, accordi battuta per battuta, mappa di
  quali stem suonano in ogni battuta, e (con --flp) dove mettere la pattern in playlist.

Tempo: il MIDI ha tick 0 = battuta 1 della griglia del beat. La griglia e' stimata (fase dei beat +
battuta col kick piu' forte); l'offset rispetto all'inizio dell'audio e' scritto in ANALISI.txt.
Tutto e' approssimato: batteria e accordi vanno controllati a orecchio.
"""
import argparse
from collections import defaultdict
import json
import os
import shutil
import subprocess
import sys

import librosa
import mido
import numpy as np

PPQ = 96  # come FL
STEMS6 = ["drums", "bass", "piano", "guitar", "other", "vocals"]
NOTE_NAMES = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]
SR = 44100


# ---------------------------------------------------------------- stem
def separate(audio, outdir, model="htdemucs_6s"):
    os.makedirs(outdir, exist_ok=True)
    paths = {s: os.path.join(outdir, f"{s}.wav") for s in STEMS6}
    if all(os.path.exists(p) for p in paths.values()):
        return paths
    import torch
    dev = "mps" if torch.backends.mps.is_available() else "cpu"
    tmp = os.path.join(outdir, "_demucs_tmp")
    subprocess.run([sys.executable, "-m", "demucs.separate", "-n", model, "-d", dev, "-o", tmp, audio],
                   check=True)
    src = os.path.join(tmp, model, os.path.splitext(os.path.basename(audio))[0])
    for s, p in paths.items():
        shutil.move(os.path.join(src, s + ".wav"), p)
    shutil.rmtree(tmp, ignore_errors=True)
    return paths


def load(path):
    y, _ = librosa.load(path, sr=SR, mono=True)
    return y


# ---------------------------------------------------------------- griglia
def estimate_grid(mix, drums, bpm_hint):
    tempo_est = float(np.atleast_1d((librosa.feature.rhythm.tempo if hasattr(librosa.feature, "rhythm") else librosa.beat.tempo)(y=mix, sr=SR, start_bpm=bpm_hint or 120))[0])
    bpm = bpm_hint or round(tempo_est, 1)
    period = 60.0 / bpm
    hop = 256
    env = librosa.onset.onset_strength(y=drums if np.abs(drums).max() > 1e-3 else mix, sr=SR, hop_length=hop)
    t = librosa.times_like(env, sr=SR, hop_length=hop)
    # fase: quella che massimizza l'onset sommato sui beat (passo 2 ms)
    best, phase = -1, 0.0
    for ph in np.arange(0, period, 0.002):
        idx = np.round((ph + np.arange(0, t[-1] - ph, period)) * SR / hop).astype(int)
        idx = idx[idx < len(env)]
        sc = env[idx].sum()
        if sc > best:
            best, phase = sc, ph
    # battuta 1: offset (0..3 beat) col kick piu' forte
    low = band_env(drums, 30, 120, hop)
    sc = []
    for c in range(4):
        times = phase + (c + 4 * np.arange(0, int(t[-1] / (4 * period)) + 1)) * period
        idx = np.round(times * SR / hop).astype(int)
        idx = idx[idx < len(low)]
        sc.append(low[idx].sum())
    t0 = phase + int(np.argmax(sc)) * period
    bar = 4 * period
    origin = t0 - bar * round(t0 / bar)  # secondi: dove cade il tick 0 rispetto all'inizio dell'audio
    return bpm, tempo_est, origin


def band_env(y, lo, hi, hop=256):
    S = np.abs(librosa.stft(y, n_fft=2048, hop_length=hop))
    f = librosa.fft_frequencies(sr=SR, n_fft=2048)
    sel = (f >= lo) & (f < hi)
    return librosa.onset.onset_strength(S=librosa.amplitude_to_db(S[sel], ref=np.max), sr=SR, hop_length=hop)


FEELS = {  # posizioni ammesse dentro un beat (frazioni) -> tick con PPQ 96
    "dritto (sedicesimi)": [0, 1 / 4, 1 / 2, 3 / 4, 1],
    "terzinato": [0, 1 / 3, 2 / 3, 1],
    "misto (sedicesimi + terzine)": [0, 1 / 4, 1 / 3, 1 / 2, 2 / 3, 3 / 4, 1],
}


class Grid:
    def __init__(self, bpm, origin, feel="dritto (sedicesimi)"):
        self.bpm, self.origin, self.feel = bpm, origin, feel
        self.beat = 60.0 / bpm
        self.pos = np.array(FEELS[feel])

    def q(self, sec):
        """secondi -> tick quantizzati sulla griglia del feel"""
        b = (sec - self.origin) / self.beat
        ib = np.floor(b)
        frac = self.pos[np.abs(self.pos - (b - ib)).argmin()]
        return int(round((ib + frac) * PPQ))


def detect_feel(onsets, origin, beat):
    """quota di colpi su terzine (1/3, 2/3) contro sedicesimi in levare (1/4, 3/4)"""
    tri = six = 0
    for sec, st, *_ in onsets:
        if st < 0.3:
            continue
        f = ((sec - origin) / beat) % 1
        d16 = min(abs(f - 0.25), abs(f - 0.75))
        d3 = min(abs(f - 1 / 3), abs(f - 2 / 3))
        dn = min(f, 1 - f, abs(f - 0.5))
        if min(d16, d3, dn) > 0.06 or dn <= min(d16, d3):
            continue
        if d3 < d16:
            tri += 1
        else:
            six += 1
    tot = tri + six
    if tot < 8 or tri / tot < 0.15:
        return "dritto (sedicesimi)", tri, six
    if six / tot < 0.15:
        return "terzinato", tri, six
    return "misto (sedicesimi + terzine)", tri, six

    def bar_of(self, sec):
        return int(np.floor((sec - self.origin) / (4 * self.beat)))


def bpm_check(drums, bpm, hop=256):
    """periodo piu' forte della batteria (1-5 s) confrontato con 2 beat / 1 battuta / 2 battute del BPM"""
    env = librosa.onset.onset_strength(y=drums, sr=SR, hop_length=hop)
    env = env - env.mean()
    ac = np.correlate(env, env, "full")[len(env) - 1:]
    lags = np.arange(len(ac)) * hop / SR
    sel = (lags > 1.0) & (lags < 5.0)
    per = float(lags[sel][ac[sel].argmax()])
    beat = 60.0 / bpm
    for mult, lab in [(2, "2 beat"), (4, "1 battuta"), (8, "2 battute"), (3, "3 beat"), (6, "6 beat")]:
        if abs(per - mult * beat) < 0.025 * mult * beat:
            ok = mult in (2, 4, 8)
            return per, (f"ok: il giro della batteria ({per:.3f} s) = {lab} a {bpm:g} BPM" if ok else
                         f"ATTENZIONE: il giro della batteria ({per:.3f} s) = {lab} a {bpm:g} BPM, controlla il tempo")
    return per, (f"ATTENZIONE: il giro della batteria ({per:.3f} s) non torna con {bpm:g} BPM "
                 f"(sarebbe {240 / per:.1f} o {480 / per:.1f} BPM a battuta)")


# ---------------------------------------------------------------- batteria
def drum_onsets(drums, hop=256):
    """colpi della batteria: (secondi, forza, log energia bassa - media, energia alta relativa)"""
    S = np.abs(librosa.stft(drums, n_fft=2048, hop_length=hop)) ** 2
    f = librosa.fft_frequencies(sr=SR, n_fft=2048)
    env = librosa.onset.onset_strength(S=librosa.power_to_db(S, ref=np.max), sr=SR, hop_length=hop)
    env = env / (np.percentile(env, 99.5) + 1e-9)
    pk = librosa.util.peak_pick(env, pre_max=3, post_max=3, pre_avg=12, post_avg=12, delta=0.15,
                                wait=int(0.06 * SR / hop))
    lo, mid, hi = (f >= 30) & (f < 150), (f >= 200) & (f < 5000), f >= 7000
    w = int(0.06 * SR / hop)
    out = []
    for p in pk:
        seg = S[:, p:p + w]
        el, em, eh = seg[lo].sum() + 1e-9, seg[mid].sum() + 1e-9, seg[hi].sum() + 1e-9
        out.append((p * hop / SR, float(env[p]), np.log(el / em), eh / (el + em + eh)))
    return out


def two_means(x):
    x = np.asarray(x)
    c = np.percentile(x, [25, 75]).astype(float)
    for _ in range(30):
        lab = np.abs(x[:, None] - c[None]).argmin(1)
        for k in range(2):
            if (lab == k).any():
                c[k] = x[lab == k].mean()
    return (c[0] + c[1]) / 2


def drum_hits(drums, grid):
    hop = 256
    ons = drum_onsets(drums, hop)
    body = [o for o in ons if o[3] < 0.5]  # i colpi "solo hat" non sono ne' kick ne' snare
    thr = two_means([o[2] for o in body]) if len(body) > 8 else 0.0
    k, s = {}, {}
    for sec, st, ratio, _ in body:
        tk = grid.q(sec)
        if tk < 0:
            continue
        d = k if ratio > thr else s
        d[tk] = max(d.get(tk, 0), st)
    # hat: banda alta, a parte (spesso suona insieme a kick e snare)
    env = band_env(drums, 7000, 16000, hop)
    env = env / (np.percentile(env, 99.5) + 1e-9)
    pk = librosa.util.peak_pick(env, pre_max=3, post_max=3, pre_avg=12, post_avg=12, delta=0.12,
                                wait=int(0.05 * SR / hop))
    h = {}
    for p in pk:
        tk = grid.q(p * hop / SR)
        if tk >= 0:
            h[tk] = max(h.get(tk, 0), float(env[p]))
    out = {"Kick": k, "Snare": s, "HiHat": h}
    return {n: [(tk, PPQ // 4, 60, vel(v)) for tk, v in sorted(d.items())] for n, d in out.items()}


def backbeat_shift(hits, grid_ppq=PPQ):
    """quanti beat spostare la battuta 1: snare su 2 e 4, oppure sul 3 se il beat e' half-time
    (uno snare per battuta). Il kick sull'1 fa da spareggio."""
    on_beat = [(tk // grid_ppq, v) for tk, _, _, v in hits["Snare"] if tk % grid_ppq == 0]
    if not on_beat:
        return 0
    per_bar = defaultdict(int)
    for b, _ in on_beat:
        per_bar[b // 4] += 1
    half_time = np.mean([n == 1 for n in per_bar.values()]) >= 0.6
    targets = (2,) if half_time else (1, 3)
    best, shift = -1e9, 0
    for c in range(4):
        sc = 0.0
        for b, v in on_beat:
            sc += v if (b + c) % 4 in targets else -v
        for tk, _, _, v in hits["Kick"]:
            if tk % (4 * grid_ppq) == ((4 - c) % 4) * grid_ppq:
                sc += 0.5 * v
        if sc > best:
            best, shift = sc, c
    return shift


def vel(x):
    return int(np.clip(55 + 70 * min(x, 1.4) / 1.4, 40, 127))


# ---------------------------------------------------------------- note (basic-pitch)
def transcribe(path, grid, lo_hz, hi_hz, mono=False, min_amp=0.25):
    from basic_pitch.inference import predict
    _, _, events = predict(path, onset_threshold=0.55, frame_threshold=0.3, minimum_note_length=80,
                           minimum_frequency=lo_hz, maximum_frequency=hi_hz, melodia_trick=True)
    ev = sorted([e for e in events if e[3] >= min_amp], key=lambda e: (e[0], -e[3]))
    if mono:  # basso: una nota alla volta, vince la piu' forte, la precedente si tronca
        mono_ev = []
        for e in ev:
            if mono_ev and e[0] < mono_ev[-1][1]:
                if e[3] < mono_ev[-1][3] * 0.8 and e[0] - mono_ev[-1][0] < 0.05:
                    continue
                prev = mono_ev[-1]
                mono_ev[-1] = (prev[0], e[0], prev[2], prev[3])
            mono_ev.append(tuple(e[:4]))
        ev = mono_ev
    notes = []
    for s, e, p, a, *_ in ev:
        ts, te = grid.q(s), grid.q(e)
        if ts < 0:
            continue
        te = max(te, ts + PPQ // 6)
        notes.append((ts, te - ts, int(p), vel(a * 1.4)))
    # stessa nota stesso tick: una sola
    seen, clean = set(), []
    for n in notes:
        if (n[0], n[2]) not in seen:
            seen.add((n[0], n[2]))
            clean.append(n)
    return clean


# ---------------------------------------------------------------- accordi e tonalita'
QUALITIES = {
    "": [0, 4, 7], "m": [0, 3, 7], "maj7": [0, 4, 7, 11], "m7": [0, 3, 7, 10], "7": [0, 4, 7, 10],
    "dim": [0, 3, 6], "sus2": [0, 2, 7], "sus4": [0, 5, 7], "m9": [0, 3, 7, 10, 2], "maj9": [0, 4, 7, 11, 2],
}
KS_MAJ = np.array([6.35, 2.23, 3.48, 2.33, 4.38, 4.09, 2.52, 5.19, 2.39, 3.66, 2.29, 2.88])
KS_MIN = np.array([6.33, 2.68, 3.52, 5.38, 2.60, 3.53, 2.54, 4.75, 3.98, 2.69, 3.34, 3.17])


def chord_templates():
    T = []
    for r in range(12):
        for q, iv in QUALITIES.items():
            v = np.zeros(12)
            for i, x in enumerate(iv):
                v[(r + x) % 12] = 1.3 if i == 0 else (0.8 if i >= 3 else 1.0)
            T.append((NOTE_NAMES[r] + q, r, iv, v / np.linalg.norm(v)))
    return T


def chords_and_key(harm, bass, grid, n_bars):
    hop = 2048
    C = librosa.feature.chroma_cqt(y=harm, sr=SR, hop_length=hop)
    Cb = librosa.feature.chroma_cqt(y=bass, sr=SR, hop_length=hop) if np.abs(bass).max() > 1e-3 else None
    t = librosa.times_like(C, sr=SR, hop_length=hop)
    prof = C.sum(axis=1)
    if Cb is not None:
        prof = prof + 0.5 * Cb.sum(axis=1)
    keys = []
    for r in range(12):
        keys.append((np.corrcoef(np.roll(KS_MAJ, r), prof)[0, 1], NOTE_NAMES[r] + " maggiore"))
        keys.append((np.corrcoef(np.roll(KS_MIN, r), prof)[0, 1], NOTE_NAMES[r] + " minore"))
    key = max(keys)[1]
    T = chord_templates()
    half = 2 * grid.beat
    out = []  # (tick_start, nome, radice, intervalli)
    for b in range(n_bars * 2):
        s = grid.origin + b * half
        sel = (t >= s) & (t < s + half)
        if not sel.any():
            continue
        v = C[:, sel].mean(axis=1)
        if v.max() < 0.15 or np.abs(harm[int(max(s, 0) * SR):int((s + half) * SR)]).max(initial=0) < 0.01:
            out.append((b * 2 * PPQ, None, None, None))
            continue
        if Cb is not None:
            bv = Cb[:, sel].mean(axis=1)
            v = v + 0.6 * (bv == bv.max()) * bv.max()
        v = v / (np.linalg.norm(v) + 1e-9)
        name, r, iv, _ = max(T, key=lambda x: float(x[3] @ v) - 0.02 * len(x[2]))
        out.append((b * 2 * PPQ, name, r, iv))
    # unisci mezze battute uguali
    merged = []
    for c in out:
        if merged and merged[-1][1] == c[1]:
            continue
        merged.append(c)
    return key, merged


def chord_notes(chords, end_tick):
    notes = []
    for i, (tk, name, r, iv) in enumerate(chords):
        if name is None:
            continue
        nxt = chords[i + 1][0] if i + 1 < len(chords) else end_tick
        notes.append((tk, nxt - tk, 48 + r, 90))
        for x in iv[1:]:
            notes.append((tk, nxt - tk, 60 + (r + x) % 12, 80))
    return notes


# ---------------------------------------------------------------- MIDI
def track(name, notes, bpm=None):
    tr = mido.MidiTrack()
    tr.append(mido.MetaMessage("track_name", name=name, time=0))
    if bpm:
        tr.append(mido.MetaMessage("set_tempo", tempo=mido.bpm2tempo(bpm), time=0))
        tr.append(mido.MetaMessage("time_signature", numerator=4, denominator=4, time=0))
    ev = []
    for s, d, p, v in notes:
        ev.append((s, 1, p, v))
        ev.append((s + d, 0, p, 0))
    ev.sort(key=lambda e: (e[0], e[1]))
    last = 0
    for tk, on, p, v in ev:
        tr.append(mido.Message("note_on" if on else "note_off", note=p, velocity=v if on else 0, time=tk - last))
        last = tk
    return tr


def write_mid(path, tracks, bpm):
    m = mido.MidiFile(ticks_per_beat=PPQ)
    first = True
    for name, notes in tracks:
        m.tracks.append(track(name, notes, bpm if first else None))
        first = False
    m.save(path)


# ---------------------------------------------------------------- progetto FL
def flp_clips(flp, audio):
    insp = os.path.join(os.path.dirname(os.path.abspath(__file__)), "flp_inspect.py")
    py = shutil.which("python3") or sys.executable
    r = subprocess.run([py, insp, flp, "--json"], capture_output=True, text=True)
    if r.returncode:
        return None
    d = json.loads(r.stdout)
    base = os.path.splitext(os.path.basename(audio))[0].lower()
    ids = [c["id"] for c in d["channels"] if c.get("sample") and
           os.path.splitext(os.path.basename(c["sample"]))[0].lower() == base]
    clips = []
    for a in d.get("arrangements", []):
        for it in a["items"]:
            if it.get("channel") in ids:
                clips.append((it["pos"], it["length"], it.get("track")))
    return {"ppq": d["ppq"], "tempo": d["tempo"], "clips": sorted(clips)}


def bbt(tick, ppq):
    bar = tick // (4 * ppq)
    rest = tick - bar * 4 * ppq
    return f"{bar + 1}:{rest // ppq + 1}:{rest % ppq:02d}"


# ---------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("audio")
    ap.add_argument("--stems", required=True)
    ap.add_argument("--midi", required=True)
    ap.add_argument("--bpm", type=float)
    ap.add_argument("--flp")
    ap.add_argument("--name", default="")
    a = ap.parse_args()

    print("1/5 stem (demucs)...", flush=True)
    st = separate(a.audio, a.stems)
    y = {s: load(p) for s, p in st.items()}
    mix = load(a.audio)
    print("2/5 griglia...", flush=True)
    bpm, tempo_est, origin = estimate_grid(mix, y["drums"], a.bpm)
    feel, n_tri, n_16 = detect_feel(drum_onsets(y["drums"]) if np.abs(y["drums"]).max() > 1e-3 else [],
                                    origin, 60.0 / bpm)
    g = Grid(bpm, origin, feel)
    _, bpm_msg = bpm_check(y["drums"], bpm) if np.abs(y["drums"]).max() > 1e-3 else (0, "(niente batteria)")
    dur = len(mix) / SR
    n_bars = int(np.ceil((dur - origin) / (4 * g.beat)))
    end_tick = n_bars * 4 * PPQ
    rms_mix = np.sqrt(np.mean(mix ** 2)) + 1e-9
    active = {s: np.sqrt(np.mean(y[s] ** 2)) / rms_mix > 0.04 for s in STEMS6}

    print("3/5 batteria...", flush=True)
    dr = drum_hits(y["drums"], g) if active["drums"] else {"Kick": [], "Snare": [], "HiHat": []}
    if active["drums"]:
        c = backbeat_shift(dr)
        if c:  # sposta la battuta 1 di c beat e rifai la batteria sulla griglia nuova
            origin = origin - c * g.beat
            origin = origin - 4 * g.beat * round(origin / (4 * g.beat))
            g = Grid(bpm, origin, feel)
            n_bars = int(np.ceil((dur - origin) / (4 * g.beat)))
            end_tick = n_bars * 4 * PPQ
            dr = drum_hits(y["drums"], g)
    print("4/5 note (basic-pitch)...", flush=True)
    mel = {}
    if active["bass"]:
        mel["Basso"] = transcribe(st["bass"], g, 30, 400, mono=True, min_amp=0.2)
    for s, label, lo, hi in [("piano", "Piano", 60, 3000), ("guitar", "Chitarra", 70, 2500),
                             ("other", "Altro (synth, pad, melodie)", 60, 4000)]:
        if active[s]:
            mel[label] = transcribe(st[s], g, lo, hi)
    print("5/5 accordi e tonalita'...", flush=True)
    harm = y["piano"] + y["guitar"] + y["other"]
    key, chords = chords_and_key(harm, y["bass"], g, n_bars)

    os.makedirs(a.midi, exist_ok=True)
    tracks = [("Kick", dr["Kick"]), ("Snare", dr["Snare"]), ("HiHat", dr["HiHat"])]
    if "Basso" in mel:
        tracks.append(("Basso", mel["Basso"]))
    tracks.append(("Accordi", chord_notes(chords, end_tick)))
    tracks += [(k, v) for k, v in mel.items() if k != "Basso"]
    tracks = [t for t in tracks if t[1]]
    write_mid(os.path.join(a.midi, "00 TUTTO.mid"), tracks, bpm)
    for i, (name, notes) in enumerate(tracks, 1):
        write_mid(os.path.join(a.midi, f"{i:02d} {name.split(' (')[0]}.mid"), [(name, notes)], bpm)

    # ---- ANALISI.txt
    L = [f"ANALISI {a.name or os.path.basename(a.audio)}", f"Beat: {os.path.basename(a.audio)}", ""]
    L.append(f"BPM usato: {bpm:g}" + (f"  (stima dall'audio: {tempo_est:.1f})" if a.bpm else ""))
    L.append(f"Verifica BPM: {bpm_msg}")
    L.append(f"Tonalita' stimata: {key}")
    L.append(f"Feel della batteria: {feel}  (colpi in levare: {n_tri} su terzina, {n_16} su sedicesimo)")
    L.append(f"Battute: {n_bars}  (durata {dur:.1f} s)")
    L.append(f"Griglia: il tick 0 dei MIDI cade a {origin * 1000:+.0f} ms dall'inizio dell'audio "
             "(negativo = prima che parta l'audio).")
    if a.flp:
        info = flp_clips(a.flp, a.audio)
        if info and info["clips"]:
            ppq = info["ppq"]
            off = int(round(origin / g.beat * ppq))
            L.append("")
            L.append(f"Nel progetto {os.path.basename(a.flp)} (PPQ {ppq}, {info['tempo']:g} BPM) il type beat e' in playlist a:")
            for pos, ln, trk in info["clips"]:
                L.append(f"  - battuta {bbt(pos, ppq)} (track {trk}, lunga {ln / (4 * ppq):.1f} battute)"
                         f" -> metti la pattern dei MIDI a {bbt(max(pos + off, 0), ppq)}"
                         + ("  (offset sotto 1/64: puoi ignorarlo e allinearla alla battuta)" if abs(off) < ppq / 16 else ""))
            if len(info["clips"]) > 1:
                L.append("  Il beat e' tagliato in piu' clip: i MIDI seguono l'audio originale intero, "
                         "allinea la pattern alla prima clip e ritaglia come hai fatto con l'audio.")
        else:
            L.append("(type beat non trovato nella playlist del progetto: allinea la pattern a mano)")
    L.append("")
    L.append("ACCORDI (stima dal cromagramma, battuta:tempo)")
    for tk, name, r, iv in chords:
        L.append(f"  {bbt(tk, PPQ):>8}  {name or '-- (niente armonia)'}")
    L.append("")
    L.append("ARRANGIAMENTO (stem che suonano in ogni battuta: K kick, S snare, H hat, B basso, P piano, "
             "G chitarra, O altro, V voci)")
    hop = SR // 10
    def bar_rms(sig):
        r = []
        for b in range(n_bars):
            s0 = int(max(origin + b * 4 * g.beat, 0) * SR)
            s1 = int((origin + (b + 1) * 4 * g.beat) * SR)
            seg = sig[s0:s1]
            r.append(np.sqrt(np.mean(seg ** 2)) if len(seg) else 0)
        return np.array(r)
    rm = {s: bar_rms(y[s]) for s in STEMS6}
    bar_hits = {n: {tk // (4 * PPQ) for tk, *_ in v} for n, v in dr.items()}
    row = []
    for b in range(n_bars):
        flags = ""
        flags += "K" if b in bar_hits["Kick"] else "."
        flags += "S" if b in bar_hits["Snare"] else "."
        flags += "H" if b in bar_hits["HiHat"] else "."
        for s, ch in [("bass", "B"), ("piano", "P"), ("guitar", "G"), ("other", "O"), ("vocals", "V")]:
            m = rm[s].max() + 1e-9
            flags += ch if rm[s][b] > 0.15 * m and active[s] else "."
        row.append(f"{b + 1:>3} {flags}")
    for i in range(0, len(row), 4):
        L.append("   ".join(row[i:i + 4]))
    L.append("")
    L.append("FILE MIDI")
    for i, (name, notes) in enumerate(tracks, 1):
        L.append(f"  {i:02d} {name}: {len(notes)} note")
    L.append("")
    L.append("COME USARLI IN FL: trascina '00 TUTTO.mid' nel Channel rack (o File > Import > MIDI file) per avere"
             " un canale per strumento, oppure trascina i singoli .mid nel piano roll del tuo strumento.")
    L.append("Kick/Snare/HiHat sono sulla nota C5: caricali su un Sampler col tuo campione.")
    L.append("Trascrizione automatica: batteria e accordi vanno controllati a orecchio, il resto e' una base da"
             " cui partire. Per il remake cambia almeno melodia/riff principale e suoni.")
    open(os.path.join(a.midi, "ANALISI.txt"), "w").write("\n".join(L) + "\n")
    print("\n".join(L[:8]))
    print("ok ->", a.midi)


if __name__ == "__main__":
    main()
