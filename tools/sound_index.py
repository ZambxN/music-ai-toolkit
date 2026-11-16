#!/usr/bin/env python3
"""Index of donmarco's sample packs ("!DON PACKS"), standard library only.

    python3 sound_index.py --scan                     # (re)build ~/.fl-studio-claude/sound_index.json
    python3 sound_index.py --find "808 dark"          # every word must match path/name/category/tags
    python3 sound_index.py --category kick --pack detroit
    python3 sound_index.py --category 808 --root C    # 808s whose root note is C
    python3 sound_index.py --random 10                # random sample, to check the classification
    python3 sound_index.py --stats                    # counts per pack and category (markdown)

Per file: pack, relative path, category, subcategory/tags, root note and BPM/key from the name,
duration and format from the WAV header. 808s and basses also get a rough pitch estimate
(autocorrelation on the tail, root_source "estimate") when the name does not give the note.

Categories: kick, snare, clap, rim, hat, open_hat, cymbal, 808, bass, perc, fx, vox,
oneshot (melodic one-shot, subcategory = instrument), loop, midi, demo (mp3 beats), other.
"""
from __future__ import annotations

import argparse
import array
import json
import math
import os
import random
import re
import struct
import sys
from collections import Counter, defaultdict
from pathlib import Path

DEFAULT_ROOT = "/Applications/FL Studio 2026.app/Contents/Resources/FL/Data/Patches/!DON PACKS"
INDEX = Path.home() / ".fl-studio-claude" / "sound_index.json"
AUDIO = {".wav", ".mp3", ".aif", ".aiff", ".flac", ".ogg"}
NOTE_NAMES = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]
FLATS = {"DB": "C#", "EB": "D#", "GB": "F#", "AB": "G#", "BB": "A#"}

# Ordered rules: first match wins. Each text (folder names deepest-first, then file name) is tested
# against all rules before moving on to the next text, so the closest folder decides.
RULES = [
    ("midi", None, r"\bmidi\b"),
    ("loop", None, r"\bloops?\b|drum n perc"),
    ("open_hat", None, r"open[ _-]?hats?|\boh\b|openhat|hi[ _-]?hat[ _-]?open"),
    ("hat", "roll", r"hat rolls?"),
    ("hat", None, r"hi[ _-]?hats?|\bhihats?\b|\bhh\b|\bhats?\b|closed hats?"),
    ("cymbal", None, r"cymb[ao]ls?|crash|ride|splash|china"),
    ("808", None, r"\b808s?\b"),
    ("bass", None, r"\bbass\b|reese|moog"),
    ("kick", None, r"kicks?\b|\bkck\b"),
    ("rim", None, r"\brims?\b|rimshots?|rim_shot"),
    ("clap", None, r"claps?\b|\bsnaps?\b"),
    ("snare", None, r"snares?\b|\bsnr\b"),
    ("perc", "stomp", r"stomps?\b"),
    ("perc", None, r"percs?\b|percussion|cowbell|shaker|tamb|clave|guiro|maraca|cabasa|woodblock|"
                   r"conga|bongo|\btoms?\b|triangle|lazer"),
    ("vox", None, r"\bvox\b|\bvoxs\b|vocals?|chants?|adlibs?"),
    ("fx", None, r"(?<![a-z])s?fxs?(?![a-z])|texture|riser|sweep|impact|noise|scratch"),
    ("oneshot", None, r"one[ -]?shots?|oneshots|keyscape|bells?|\bpiano|\bkeys?\b|pluck|pads?\b|"
                      r"guitar|flutes?|kalimba|musicbox|strings|synths?|rhodes|choir|horn"),
    ("loop", "melodic", r"\bsamples?\b"),
]
RULES = [(c, s, re.compile(p, re.I)) for c, s, p in RULES]
NEUTRAL_FOLDERS = {"samples", "drum kit", "midi kit"}
# Pack prefixes stripped before reading the leading word of a file name ("MZ Clap [French]" -> "Clap")
RE_PREFIX = re.compile(r"^(?:mz|bwb(?: wav x| x wav)?|wav x|tm-88|cymatics\s*-|dennibulli\s*-[^-]*-)\s*", re.I)
RE_BRACKET = re.compile(r"\[([^\]]*)\]|\(([^)]*)\)")
BRACKET_TAGS = {"openhat", "hihat", "808", "perc", "sfx", "fx", "bass", "bells", "snare", "kick", "clap",
                "crash", "rim", "vox"}
DRUMS = {"kick", "snare", "clap", "rim", "hat", "open_hat", "cymbal", "808", "bass", "perc", "fx", "vox"}

INSTRUMENTS = [  # subcategory for melodic one-shots, first match wins ("bass" moves it to category bass)
    ("bass", r"trilian|resobass|sub ?37"),
    ("rhodes", r"rhodes|wurli|electric piano|\bep\b|tine|dyno"),
    ("bell", r"bell|chime|glock|celest|mallet|music ?box|kalimba|dulcit|vibra|marimba|steeldrum|toy piano"),
    ("piano", r"piano|grand|c7|upright|pianet"),
    ("organ", r"organ|harmochord|harmonium|wind keyboard"),
    ("clav", r"clav|harpsichord|plucked keyboard"),
    ("pluck", r"pluck"),
    ("pad", r"\bpads?\b|atmos"),
    ("guitar", r"guitar|gtr|banjo|koto|lute|lapsteel"),
    ("flute", r"flute|piccolo"),
    ("strings", r"string|violin|cello"),
    ("choir", r"choir|choral|\boos\b|\bmms\b|\baahs?\b"),
    ("brass", r"horn|brass|trumpet|sax"),
    ("lead", r"\blead\b"),
    ("synth", r"synth|\bkeys?\b|accent|digital"),
]
INSTRUMENTS = [(n, re.compile(p, re.I)) for n, p in INSTRUMENTS]

# Packs whose melodic sounds are rooted on C even when the name does not say it
# (from donmarco's setup notes / pack readme; marked root_source "pack").
PACK_ROOT_C = ("!!Mike Zara's Stash/808s", "!Oneshots/KEYSCAPE", "!Oneshots/Cymatics", "!Oneshots/Arcadia",
               "!Oneshots/dennibulli", "!1MODERN DETROIT KIT RE-UP/One-shots")


def norm_note(n: str) -> str | None:
    n = n.strip().upper().replace("♯", "#").replace("♭", "B")
    if len(n) == 2 and n[1] == "B" and n in FLATS:
        return FLATS[n]
    n = n[0] + n[1:].replace("B", "")
    return n if n in NOTE_NAMES else None


RE_BPM = re.compile(r"(\d{2,3})\s*bpm|\((\d{2,3})\s+[A-G][#b]?m?\)", re.I)
RE_KEY = re.compile(r"(?:^|[\s(\-_\[])([A-G](?:#|b|♯|♭)?)\s*((?i:min(?:or)?|maj(?:or)?)|m(?![a-z]))?"
                    r"(?=$|[\s)\-_\].]|\d)")
RE_BPM_KEY = re.compile(r"\((\d{2,3})\s+([A-G][#b]?)(m?)\)")
RE_TUNED = re.compile(r"(?:tuned to|in)\s+([A-G][#b]?)\b", re.I)


def name_info(stem: str) -> dict:
    out = {}
    m = RE_BPM.search(stem)
    if m:
        out["bpm"] = int(m.group(1) or m.group(2))
    m = RE_BPM_KEY.search(stem)  # "Chain Gang (155 D#m)"
    if m and norm_note(m.group(2)):
        out["root"] = norm_note(m.group(2))
        out["key"] = out["root"] + m.group(3)
        return out
    if RE_PREFIX.match(stem):  # "MZ 808 [Model D]": brackets hold a nickname, not a note
        stem = re.sub(r"\[[^\]]*\]", " ", stem)
    m = RE_TUNED.search(stem)
    if m and "not in" not in stem.lower():
        out["root"] = norm_note(m.group(1))
        return out
    # Keys usually sit at the end: "- A Min", "(C)", "_C", "(165 C#m)"
    for m in reversed(list(RE_KEY.finditer(stem))):
        tail = stem[m.end():].strip(" )]-_.")
        if tail and not re.fullmatch(r"(\d{2,3}\s*bpm|bpm)?", tail, re.I):
            continue
        note = norm_note(m.group(1))
        if not note:
            continue
        mode = (m.group(2) or "").lower()
        out["root"] = note
        if mode.startswith("m") and not mode.startswith("maj"):
            out["key"] = note + "m"
        elif mode.startswith("maj"):
            out["key"] = note
        break
    return out


def wav_info(path: str) -> dict:
    """Format, duration, and acid/smpl metadata from a WAV header (no audio decoding)."""
    out = {}
    try:
        with open(path, "rb") as f:
            head = f.read(12)
            if head[:4] != b"RIFF" or head[8:12] != b"WAVE":
                return out
            while True:
                ch = f.read(8)
                if len(ch) < 8:
                    break
                cid, size = ch[:4], struct.unpack("<I", ch[4:])[0]
                if cid == b"fmt ":
                    d = f.read(size)
                    fmt, chans, rate, _, align, bits = struct.unpack_from("<HHIIHH", d)
                    if fmt == 0xFFFE and len(d) >= 26:
                        fmt = struct.unpack_from("<H", d, 24)[0]
                    out.update(format="float" if fmt == 3 else "pcm", channels=chans, rate=rate,
                               bits=bits, _align=align)
                elif cid == b"data":
                    out["_data"] = (f.tell(), size)
                    f.seek(size, 1)
                elif cid == b"acid":
                    d = f.read(size)
                    flags, root, _, _, beats, _, _, tempo = struct.unpack_from("<IHHfIHHf", d)
                    if flags & 2 and root != 60:
                        out["meta_root"] = root
                    if not flags & 1 and tempo > 0:
                        out["meta_bpm"] = round(tempo, 1)
                elif cid in (b"smpl", b"inst"):
                    d = f.read(size)
                    root = struct.unpack_from("<I", d, 12)[0] if cid == b"smpl" else d[0]
                    if 0 < root < 128 and root != 60:
                        out.setdefault("meta_root", root)
                else:
                    f.seek(size, 1)
                if size & 1:
                    f.seek(1, 1)
    except (OSError, struct.error):
        return out
    if "_data" in out and out.get("_align"):
        out["duration"] = round(out["_data"][1] / out["_align"] / out["rate"], 3)
    return out


def read_mono(path: str, info: dict, start: float, length: float, step: int):
    """Mono samples (every `step`-th frame) of a PCM/float WAV between start and start+length seconds."""
    off, size = info["_data"]
    align, chans, bits, rate = info["_align"], info["channels"], info["bits"], info["rate"]
    first = int(start * rate)
    count = min(int(length * rate), size // align - first)
    if count <= 0:
        return []
    with open(path, "rb") as f:
        f.seek(off + first * align)
        raw = f.read(count * align)
    width = bits // 8
    out = []
    for i in range(0, len(raw) - align + 1, align * step):
        fr = raw[i:i + width]
        if info["format"] == "float":
            v = struct.unpack("<f" if width == 4 else "<d", fr)[0]
        elif width == 2:
            v = struct.unpack("<h", fr)[0] / 32768
        elif width == 3:
            v = int.from_bytes(fr, "little", signed=True) / 8388608
        elif width == 4:
            v = struct.unpack("<i", fr)[0] / 2147483648
        else:
            v = (fr[0] - 128) / 128
        out.append(v)
    return out


def estimate_pitch(path: str, info: dict) -> int | None:
    """Rough MIDI note of a bass/808 (25-250 Hz) by autocorrelation on a window after the attack."""
    if not info.get("_data") or info.get("format") not in ("pcm", "float") or not info.get("rate"):
        return None
    dur = info.get("duration", 0)
    if dur < 0.15:
        return None
    step = max(1, info["rate"] // 4000)
    sr = info["rate"] / step
    x = read_mono(path, info, min(0.12, dur * 0.2), min(0.35, dur * 0.6), step)
    if len(x) < sr * 0.08:
        return None
    mean = sum(x) / len(x)
    x = [v - mean for v in x]
    energy = sum(v * v for v in x)
    if energy < 1e-6:
        return None
    lo, hi = int(sr / 250), int(sr / 25)
    n = len(x)
    best, best_lag = 0.0, None
    corr = {}
    for lag in range(lo, min(hi, n // 2)):
        c = sum(x[i] * x[i + lag] for i in range(n - lag)) / (n - lag)
        corr[lag] = c
    if not corr:
        return None
    peak = max(corr.values())
    if peak <= 0:
        return None
    # first local maximum above 85% of the global one (avoids picking a sub-harmonic)
    lags = sorted(corr)
    for a, b, c in zip(lags, lags[1:], lags[2:]):
        if corr[b] >= corr[a] and corr[b] >= corr[c] and corr[b] >= 0.85 * peak:
            best_lag = b
            break
    if best_lag is None:
        best_lag = max(corr, key=corr.get)
    hz = sr / best_lag
    return round(69 + 12 * math.log2(hz / 440))


def classify(rel: str, ext: str) -> tuple[str, str | None, list[str]]:
    parts = rel.split("/")
    folders, stem = parts[1:-1], Path(parts[-1]).stem
    tags = []
    if ext == ".mid":
        cat, sub = "midi", None
        for text in [stem] + folders[::-1]:
            for c, s, rx in RULES:
                if c not in ("midi", "loop", "oneshot") and rx.search(text):
                    sub = c
                    break
            if sub:
                break
        return cat, sub, tags
    if ext == ".mp3" and "beats" in rel.lower():
        return "demo", None, tags
    folders = [f for f in folders if f.lower() not in NEUTRAL_FOLDERS]

    def first_rule(text, only=None):
        for c, s, rx in RULES:
            if (only is None or c in only) and rx.search(text):
                return c, s, rx
        return None

    def folder_rule():
        for text in folders[::-1]:
            r = first_rule(text)
            if r:
                return r + (text,)
        return None

    hit = None
    # 1. explicit category tag in brackets ("Perc [OpenHat]", "Cold [808]"), unless the name starts with a
    #    pack prefix: there the brackets hold a nickname ("MZ Clap [808]", "MZ FX [Lazer]")
    if not RE_PREFIX.match(stem):
        for m in RE_BRACKET.finditer(stem):
            tag = (m.group(1) or m.group(2) or "").strip().lower()
            if tag in BRACKET_TAGS:
                r = first_rule(tag, DRUMS) or first_rule(tag)
                if r:
                    hit = r + (tag,)
                    break
    # 2. leading word of the file name: "Clap 1" in a Snares folder, "Kick_10" in a flat folder
    if not hit:
        lead = RE_BRACKET.sub(" ", RE_PREFIX.sub("", stem.replace("_", " "))).strip()
        if any(rx.match(lead) for c, s, rx in RULES if c in DRUMS):
            r = first_rule(lead, DRUMS)
            fr = folder_rule()
            # "Bass 9" inside an 808 folder is an 808
            if r and not (r[0] == "bass" and fr and fr[0] == "808"):
                hit = r + (lead,)
    # 3. closest folder, 4. anything in the file name
    if not hit:
        hit = folder_rule()
    if not hit:
        r = first_rule(stem)
        hit = r + (stem,) if r else None
    if hit:
        c, s, _, text = hit
        # a folder named "One-shots"/"Bells" holding a bass: let the file name refine it
        if c == "oneshot":
            for c2, s2, rx2 in RULES:
                if c2 in ("bass", "808") and rx2.search(stem):
                    return c2, s2, tags
            sub = next((n for n, r in INSTRUMENTS if r.search(stem) or any(r.search(f) for f in folders)), None)
            if sub == "bass":
                return "bass", None, tags
            return c, sub, tags
        if c == "perc" and re.search(r"\(or fx\)", stem, re.I):
            tags.append("fx")
        if c == "snare" and re.search(r"perc", text, re.I):
            tags.append("perc")
        if re.search(r"short", stem + " " + text, re.I) and c in ("808", "bass"):
            s = "short"
        if c == "clap" and re.search(r"snap", stem + " " + text, re.I):
            s = "snap"
        if c == "perc" and re.search(r"\btoms?\b", stem + " " + text, re.I):
            s = "tom"
        return c, s, tags
    return "other", None, tags


def scan(root: str, pitch: bool = True) -> list[dict]:
    items = []
    for dp, dn, fn in os.walk(root):
        dn.sort()
        for f in sorted(fn):
            ext = Path(f).suffix.lower()
            if ext not in AUDIO and ext != ".mid":
                continue
            full = os.path.join(dp, f)
            rel = os.path.relpath(full, root)
            pack = rel.split("/")[0]
            cat, sub, tags = classify(rel, ext)
            it = {"pack": pack, "path": rel, "name": Path(f).stem, "ext": ext[1:], "category": cat}
            if sub:
                it["sub"] = sub
            if tags:
                it["tags"] = tags
            ni = name_info(Path(f).stem)
            info = wav_info(full) if ext == ".wav" else {}
            for k in ("duration", "rate", "bits", "channels", "format"):
                if k in info:
                    it[k] = info[k]
            if "bpm" in ni:
                it["bpm"] = ni["bpm"]
            if "key" in ni:
                it["key"] = ni["key"]
            if ni.get("root"):
                it["root"], it["root_source"] = ni["root"], "name"
            elif "meta_root" in info:
                it["root"], it["root_source"] = NOTE_NAMES[info["meta_root"] % 12], "wav"
            elif cat in ("808", "bass", "oneshot") and any(rel.startswith(p) for p in PACK_ROOT_C):
                it["root"], it["root_source"] = "C", "pack"
            if pitch and cat in ("808", "bass") and info.get("_data") and it.get("root_source") != "name":
                est = estimate_pitch(full, info)
                if est is not None:
                    it["pitch_estimate"] = NOTE_NAMES[est % 12] + str(est // 12 - 1)
                    if "root" not in it:
                        it["root"], it["root_source"] = NOTE_NAMES[est % 12], "estimate"
            # loops: BPM in the name of a longer sound
            if cat in ("oneshot", "perc", "fx", "vox", "other") and it.get("bpm") and it.get("duration", 0) > 1.5:
                it.setdefault("tags", []).append("loop-like")
            items.append(it)
    return items


def load() -> dict:
    if not INDEX.exists():
        raise SystemExit(f"{INDEX} not found: run `sound_index.py --scan` first")
    return json.loads(INDEX.read_text())


def fmt_item(it: dict, root: str | None = None) -> str:
    bits = [f"{it['category']}" + (f"/{it['sub']}" if it.get("sub") else "")]
    if it.get("root"):
        bits.append(f"root {it['root']} ({it['root_source']})")
    if it.get("pitch_estimate") and it.get("root_source") != "estimate":
        bits.append(f"pitch ~{it['pitch_estimate']}")
    elif it.get("pitch_estimate"):
        bits[-1] = f"root ~{it['pitch_estimate']} (estimate)"
    if it.get("key"):
        bits.append(f"key {it['key']}")
    if it.get("bpm"):
        bits.append(f"{it['bpm']} BPM")
    if it.get("duration") is not None:
        bits.append(f"{it['duration']:.2f}s")
    return f"{it['path']}\n    " + " | ".join(bits)


def stats_md(idx: dict) -> str:
    items = idx["items"]
    by_pack = defaultdict(Counter)
    for it in items:
        by_pack[it["pack"]][it["category"]] += 1
    cats = [c for c, _ in Counter(it["category"] for it in items).most_common()]
    lines = ["| Pack | Tot | " + " | ".join(cats) + " |", "|---|---:|" + "---:|" * len(cats)]
    for pack in sorted(by_pack, key=lambda p: -sum(by_pack[p].values())):
        c = by_pack[pack]
        lines.append(f"| {pack} | {sum(c.values())} | " + " | ".join(str(c.get(k, "")) for k in cats) + " |")
    tot = Counter(it["category"] for it in items)
    lines.append(f"| **Totale** | **{len(items)}** | " + " | ".join(f"**{tot[k]}**" for k in cats) + " |")
    return "\n".join(lines)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--scan", action="store_true", help="rebuild the index")
    ap.add_argument("--root", default=DEFAULT_ROOT, help="packs folder (default: !DON PACKS in FL app bundle)")
    ap.add_argument("--no-pitch", action="store_true", help="skip the 808/bass pitch estimate while scanning")
    ap.add_argument("--find", help="words that must all appear (path, category, sub, root, key)")
    ap.add_argument("--category")
    ap.add_argument("--sub")
    ap.add_argument("--pack", help="substring of the pack name")
    ap.add_argument("--root-note", "--key-root", dest="root_note", help="root note, e.g. C, F#")
    ap.add_argument("--bpm", type=int)
    ap.add_argument("--random", type=int, metavar="N")
    ap.add_argument("--seed", type=int)
    ap.add_argument("--limit", type=int, default=40)
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--stats", action="store_true")
    a = ap.parse_args()

    if a.scan:
        items = scan(a.root, pitch=not a.no_pitch)
        INDEX.parent.mkdir(parents=True, exist_ok=True)
        INDEX.write_text(json.dumps({"root": a.root, "count": len(items), "items": items}, ensure_ascii=False,
                                    indent=0))
        print(f"indexed {len(items)} files -> {INDEX}")
        print(Counter(it["category"] for it in items).most_common())
        return
    idx = load()
    if a.stats:
        print(stats_md(idx))
        return
    res = idx["items"]
    if a.category:
        res = [it for it in res if it["category"] == a.category.lower()]
    if a.sub:
        res = [it for it in res if (it.get("sub") or "") == a.sub.lower()]
    if a.pack:
        res = [it for it in res if a.pack.lower() in it["pack"].lower()]
    if a.root_note:
        want = norm_note(a.root_note)
        res = [it for it in res if it.get("root") == want]
    if a.bpm:
        res = [it for it in res if it.get("bpm") == a.bpm]
    if a.find:
        words = a.find.lower().split()

        def hay(it):
            return " ".join(str(it.get(k, "")) for k in ("path", "category", "sub", "root", "key", "tags")).lower()
        res = [it for it in res if all(w in hay(it) for w in words)]
    if a.random:
        rnd = random.Random(a.seed)
        res = rnd.sample(res, min(a.random, len(res)))
    if a.json:
        json.dump(res[:a.limit], sys.stdout, ensure_ascii=False, indent=1)
        return
    for it in res[:a.limit]:
        print(fmt_item(it))
    print(f"\n{len(res)} match" + ("" if len(res) <= a.limit else f" (first {a.limit} shown, use --limit)"))


if __name__ == "__main__":
    main()
