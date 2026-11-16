#!/usr/bin/env python3
"""Read-only inspector for FL Studio project files (.flp), FL 12 -> FL 2026.

Standard library only. Never writes to the .flp.

    python3 flp_inspect.py PROJECT.flp                 # human-readable summary
    python3 flp_inspect.py PROJECT.flp --json          # full summary as JSON
    python3 flp_inspect.py PROJECT.flp --notes "Pattern 1"      # notes of one pattern (name or number)
    python3 flp_inspect.py PROJECT.flp --midi "Pattern 1" out.mid  # export a pattern to a MIDI file
    python3 flp_inspect.py PROJECT.flp --events        # raw event dump (debugging the format)

Format notes (verified against FL Studio 2026.1 files):
- Header "FLhd" (format, channel count, PPQ) then "FLdt" with a flat list of events.
- Event id < 64: 1 byte, < 128: 2 bytes, < 192: 4 bytes, >= 192: varint length + payload.
- FL 2025/2026 write event 172 with a 3-byte payload; the parser tries both layouts and keeps
  the shortest one that ends exactly at the end of the data and matches the header channel count.
- Playlist items are 60 (FL 21), 80 (FL 24/25) or 88 (FL 2026) bytes; size is auto-detected.
- Channel -> mixer insert routing is not decoded (use the MCP server live for that).
"""
from __future__ import annotations

import argparse
import json
import struct
import sys
from collections import Counter, defaultdict

NOTE_NAMES = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]
CHANNEL_TYPES = {0: "sampler", 1: "ts404", 2: "native", 3: "layer", 4: "instrument", 5: "automation"}

# Event ids (from PyFLP's reverse engineering, checked on FL 2026 files)
EV_VERSION, EV_TEMPO, EV_TITLE, EV_GENRE, EV_ARTISTS, EV_COMMENTS = 199, 156, 194, 206, 207, 195
EV_TS_NUM, EV_TS_BEAT = 17, 18
EV_CH_NEW, EV_CH_TYPE, EV_CH_SAMPLE, EV_CH_NAME_OLD = 64, 21, 196, 192
EV_PLUGIN_INTERNAL, EV_PLUGIN_NAME, EV_PLUGIN_DATA = 201, 203, 213
EV_PAT_NEW, EV_PAT_NAME, EV_PAT_NOTES, EV_PAT_LENGTH = 65, 193, 224, 164
EV_ARR_NEW, EV_ARR_NAME, EV_PLAYLIST, EV_TRACK_NAME = 99, 241, 233, 239
EV_INS_FLAGS, EV_INS_NAME, EV_INS_OUTPUT, EV_SLOT_INDEX = 236, 204, 147, 98
TEXT_IDS = set(range(192, 208)) | {231, 239, 241}


def note_name(key: int) -> str:
    return f"{NOTE_NAMES[key % 12]}{key // 12}"


def _parse_events(b: bytes, special: dict[int, int]):
    pos = 22
    end = pos + struct.unpack_from("<I", b, 18)[0]
    events = []
    while pos < end:
        eid = b[pos]
        pos += 1
        if eid in special:
            n = special[eid]
        elif eid < 64:
            n = 1
        elif eid < 128:
            n = 2
        elif eid < 192:
            n = 4
        else:
            n = shift = 0
            while True:
                c = b[pos]
                pos += 1
                n |= (c & 0x7F) << shift
                shift += 7
                if not c & 0x80:
                    break
        events.append((eid, b[pos:pos + n]))
        pos += n
    return events, pos == end


def read_events(path: str):
    b = open(path, "rb").read()
    if b[:4] != b"FLhd" or b[14:18] != b"FLdt":
        raise SystemExit(f"{path}: not an FL Studio project (missing FLhd/FLdt)")
    fmt, n_channels, ppq = struct.unpack_from("<hHH", b, 8)
    candidates = []
    for special in ({}, {172: 3}):
        try:
            events, clean = _parse_events(b, special)
        except IndexError:
            continue
        if clean and sum(1 for e, _ in events if e == EV_CH_NEW) == n_channels:
            candidates.append(events)
    if candidates:
        # A wrong layout shreds text payloads into many 1-byte events, so the shortest stream wins.
        return min(candidates, key=len), {"format": fmt, "channel_count": n_channels, "ppq": ppq}
    raise SystemExit(f"{path}: could not parse event stream (unknown FL version layout?)")


def _text(eid: int, d: bytes) -> str:
    if eid == EV_VERSION:
        return d.decode("ascii", "ignore").strip("\0")
    return d.decode("utf-16-le", "ignore").strip("\0")


def _int(d: bytes, signed=False) -> int:
    return int.from_bytes(d, "little", signed=signed)


def _vst_info(d: bytes) -> dict:
    """Name/vendor of a VST hosted by Fruity Wrapper, from its plugin-data event."""
    out = {}
    if len(d) < 4 or _int(d[:4]) not in (8, 10, 12):  # 12 since FL 2026
        return out
    pos = 4
    while pos + 12 <= len(d):
        vid = _int(d[pos:pos + 4])
        size = _int(d[pos + 4:pos + 12])
        payload = d[pos + 12:pos + 12 + size]
        pos += 12 + size
        if vid == 54:
            out["name"] = payload.decode("utf-8", "ignore").strip("\0")
        elif vid == 56:
            out["vendor"] = payload.decode("utf-8", "ignore").strip("\0")
        elif vid == 55:
            out["path"] = payload.decode("utf-8", "ignore").strip("\0")
    return out


def _parse_notes(d: bytes):
    notes = []
    for i in range(0, len(d) - len(d) % 24, 24):
        pos, flags, ch, length, key, group, fine, _u, rel, midich, pan, vel, mx, my = \
            struct.unpack_from("<IHHIHHBBBBBBBB", d, i)
        notes.append({"pos": pos, "length": length, "key": key, "note": note_name(key),
                      "velocity": vel, "pan": pan, "channel": ch, "fine_pitch": fine,
                      "slide": bool(flags & 8)})
    return notes


def _playlist_item_size(d: bytes):
    for size in (88, 80, 60, 32):
        if len(d) % size == 0 and all(_int(d[i + 4:i + 6]) == 20480 for i in range(0, len(d), size)):
            return size
    return None


def inspect(path: str) -> dict:
    events, header = read_events(path)
    proj = {"file": path, **header, "fl_version": None, "tempo": None, "title": "", "genre": "",
            "artists": "", "time_signature": None}
    channels, patterns, inserts, arrangements = [], {}, [], []
    cur_ch = cur_pat = cur_arr = None
    cur_slot = {}
    cur_insert = {"slots": [], "name": ""}
    section = "project"  # project -> channels -> arrangements -> mixer
    ts = [None, None]

    for eid, d in events:
        if eid == EV_VERSION:
            proj["fl_version"] = _text(eid, d)
        elif eid == EV_TEMPO:
            proj["tempo"] = _int(d) / 1000
        elif eid == EV_TITLE:
            proj["title"] = _text(eid, d)
        elif eid == EV_GENRE:
            proj["genre"] = _text(eid, d)
        elif eid == EV_ARTISTS:
            proj["artists"] = _text(eid, d)
        elif eid == EV_TS_NUM and section == "project":
            ts[0] = _int(d)
        elif eid == EV_TS_BEAT and section == "project":
            ts[1] = _int(d)

        # patterns (can appear anywhere before the mixer)
        elif eid == EV_PAT_NEW:
            pid = _int(d)
            cur_pat = patterns.setdefault(pid, {"id": pid, "name": f"Pattern {pid}", "notes": []})
        elif eid == EV_PAT_NAME and cur_pat is not None:
            cur_pat["name"] = _text(eid, d) or cur_pat["name"]
        elif eid == EV_PAT_NOTES and cur_pat is not None:
            cur_pat["notes"].extend(_parse_notes(d))

        # channel rack
        elif eid == EV_CH_NEW:
            section = "channels"
            cur_ch = {"id": _int(d), "name": "", "type": None, "plugin": "", "vst": None, "sample": ""}
            channels.append(cur_ch)
        elif section == "channels" and cur_ch is not None and eid == EV_CH_TYPE:
            cur_ch["type"] = CHANNEL_TYPES.get(_int(d), _int(d))
        elif section == "channels" and cur_ch is not None and eid == EV_PLUGIN_INTERNAL:
            cur_ch["plugin"] = _text(eid, d)
        elif section == "channels" and cur_ch is not None and eid in (EV_PLUGIN_NAME, EV_CH_NAME_OLD):
            cur_ch["name"] = cur_ch["name"] or _text(eid, d)
        elif section == "channels" and cur_ch is not None and eid == EV_PLUGIN_DATA:
            cur_ch["vst"] = _vst_info(d) or cur_ch["vst"]
        elif section == "channels" and cur_ch is not None and eid == EV_CH_SAMPLE:
            cur_ch["sample"] = _text(eid, d)

        # arrangements / playlist
        elif eid == EV_ARR_NEW:
            section = "arrangements"
            cur_arr = {"id": _int(d), "name": "", "items": [], "tracks": []}
            arrangements.append(cur_arr)
        elif eid == EV_ARR_NAME and cur_arr is not None:
            cur_arr["name"] = _text(eid, d)
        elif eid == EV_TRACK_NAME and cur_arr is not None:
            cur_arr["tracks"].append(_text(eid, d))
        elif eid == EV_PLAYLIST and cur_arr is not None:
            size = _playlist_item_size(d)
            for i in range(0, len(d), size or len(d) + 1):
                pos, base, idx, length, rv = struct.unpack_from("<IHHIH", d, i)
                item = {"pos": pos, "length": length, "track": 500 - rv}
                if idx >= base:
                    item["pattern"] = idx - base
                else:
                    item["channel"] = idx
                cur_arr["items"].append(item)

        # mixer: insert = [236 flags] ... slots (plugin events, then 98 index) ... [147 output]
        elif eid == EV_INS_FLAGS:
            section = "mixer"
        elif section == "mixer" and eid == EV_PLUGIN_INTERNAL:
            cur_slot = {"plugin": _text(eid, d)}
        elif section == "mixer" and eid == EV_PLUGIN_NAME:
            cur_slot["name"] = _text(eid, d)
        elif section == "mixer" and eid == EV_PLUGIN_DATA:
            vst = _vst_info(d)
            if vst:
                cur_slot["vst"] = vst
        elif section == "mixer" and eid == EV_SLOT_INDEX:
            if cur_slot.get("plugin"):
                cur_slot["slot"] = _int(d)
                cur_insert["slots"].append(cur_slot)
            cur_slot = {}
        elif section == "mixer" and eid == EV_INS_NAME:
            cur_insert["name"] = _text(eid, d)
        elif section == "mixer" and eid == EV_INS_OUTPUT:
            cur_insert["index"] = len(inserts)
            inserts.append(cur_insert)
            cur_insert = {"slots": [], "name": ""}

    if ts[0] and ts[1]:
        proj["time_signature"] = f"{ts[0]}/{ts[1]}"
    ch_names = {c["id"]: c["name"] or f"Channel {c['id']}" for c in channels}
    ppq = header["ppq"] or 96
    beats_per_bar = ts[0] or 4
    for p in patterns.values():
        for n in p["notes"]:
            n["channel_name"] = ch_names.get(n["channel"], str(n["channel"]))
        end = max((n["pos"] + n["length"] for n in p["notes"]), default=0)
        p["length_bars"] = round(end / (ppq * beats_per_bar), 2)
    for a in arrangements:
        a["tracks"] = [t for t in a["tracks"] if t]
    proj.update(channels=channels, patterns=sorted(patterns.values(), key=lambda p: p["id"]),
                mixer=[i for i in inserts if i["slots"] or i["name"]], arrangements=arrangements)
    return proj


def _plugin_label(x: dict) -> str:
    vst = x.get("vst") or {}
    if vst.get("name"):
        return vst["name"] + (f" ({vst['vendor']})" if vst.get("vendor") else "")
    return x.get("plugin") or ""


def print_summary(p: dict) -> None:
    print(f"# {p['file']}")
    print(f"FL {p['fl_version']} | tempo {p['tempo']} BPM | PPQ {p['ppq']} | "
          f"time sig {p['time_signature'] or '?'}")
    for k in ("title", "artists", "genre"):
        if p[k]:
            print(f"{k}: {p[k]}")
    print(f"\n## Channel rack ({len(p['channels'])})")
    for c in p["channels"]:
        src = _plugin_label(c) or ("Sampler" if c["type"] == "sampler" else "audio clip" if c["sample"] else "")
        extra = f"  <- {c['sample']}" if c["sample"] else ""
        print(f"  [{c['id']:>3}] {c['name'] or '(unnamed)':<40} {c['type'] or '':<10} {src}{extra}")
    print(f"\n## Patterns ({len(p['patterns'])})")
    for pat in p["patterns"]:
        by_ch = Counter(n["channel_name"] for n in pat["notes"])
        detail = ", ".join(f"{k} x{v}" for k, v in by_ch.most_common())
        print(f"  [{pat['id']:>3}] {pat['name']:<30} {len(pat['notes']):>4} notes, "
              f"{pat['length_bars']} bars  {detail}")
    print(f"\n## Mixer (inserts with plugins or names; 0 = Master)")
    for ins in p["mixer"]:
        plugs = ", ".join(f"{s.get('slot')}:{_plugin_label(s)}" for s in ins["slots"])
        print(f"  [{ins['index']:>3}] {ins['name'] or '':<20} {plugs}")
    for a in p["arrangements"]:
        usage = defaultdict(int)
        for it in a["items"]:
            usage[("pattern", it["pattern"]) if "pattern" in it else ("clip", it["channel"])] += 1
        pats = {pt["id"]: pt["name"] for pt in p["patterns"]}
        chs = {c["id"]: c["name"] for c in p["channels"]}
        print(f"\n## Arrangement '{a['name']}' - {len(a['items'])} playlist clips")
        if a["tracks"]:
            print("  named tracks: " + ", ".join(a["tracks"]))
        for (kind, i), n in sorted(usage.items(), key=lambda x: -x[1]):
            label = pats.get(i, f"Pattern {i}") if kind == "pattern" else f"{chs.get(i, i)} (clip)"
            print(f"  {label}: x{n}")


def find_pattern(p: dict, ref: str) -> dict:
    for pat in p["patterns"]:
        if pat["name"].lower() == ref.lower() or str(pat["id"]) == ref:
            return pat
    raise SystemExit(f"pattern '{ref}' not found; have: {[x['name'] for x in p['patterns']]}")


def write_midi(pat: dict, ppq: int, tempo: float, out: str) -> None:
    """One MIDI track per rack channel used in the pattern, same PPQ as the project."""
    def vlq(n):
        bs = [n & 0x7F]
        while n > 127:
            n >>= 7
            bs.insert(0, (n & 0x7F) | 0x80)
        return bytes(bs)
    tracks = []
    tempo_us = int(60_000_000 / (tempo or 120))
    tracks.append(b"\x00\xff\x51\x03" + tempo_us.to_bytes(3, "big") + b"\x00\xff\x2f\x00")
    by_ch = defaultdict(list)
    for n in pat["notes"]:
        by_ch[n["channel_name"]].append(n)
    for i, (name, notes) in enumerate(by_ch.items()):
        ev = []
        for n in notes:
            vel = max(1, min(127, round(n["velocity"] * 127 / 128)))
            ev.append((n["pos"], 1, bytes([0x90 | (i % 16), n["key"], vel])))
            ev.append((n["pos"] + max(1, n["length"]), 0, bytes([0x80 | (i % 16), n["key"], 0])))
        ev.sort(key=lambda e: (e[0], e[1]))
        nm = name.encode("utf-8")[:120]
        data, t = b"\x00\xff\x03" + vlq(len(nm)) + nm, 0
        for tick, _, msg in ev:
            data += vlq(tick - t) + msg
            t = tick
        tracks.append(data + b"\x00\xff\x2f\x00")
    with open(out, "wb") as f:
        f.write(b"MThd" + struct.pack(">IHHH", 6, 1, len(tracks), ppq))
        for tr in tracks:
            f.write(b"MTrk" + struct.pack(">I", len(tr)) + tr)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("flp")
    ap.add_argument("--json", action="store_true", help="print the full summary as JSON")
    ap.add_argument("--notes", metavar="PATTERN", help="print notes of a pattern (name or id)")
    ap.add_argument("--midi", nargs=2, metavar=("PATTERN", "OUT.mid"), help="export a pattern to MIDI")
    ap.add_argument("--events", action="store_true", help="raw event dump")
    a = ap.parse_args()
    if a.events:
        events, header = read_events(a.flp)
        print(header)
        for i, (eid, d) in enumerate(events):
            val = _text(eid, d) if eid in TEXT_IDS else (_int(d) if eid < 192 else f"<{len(d)} bytes> {d[:32].hex()}")
            print(i, eid, val)
        return
    p = inspect(a.flp)
    if a.notes:
        pat = find_pattern(p, a.notes)
        ppq = p["ppq"]
        for n in pat["notes"]:
            print(f"beat {n['pos'] / ppq:7.3f}  len {n['length'] / ppq:6.3f}  {n['note']:<4} (key {n['key']:>3})"
                  f"  vel {n['velocity']:>3}  {n['channel_name']}")
    elif a.midi:
        pat = find_pattern(p, a.midi[0])
        write_midi(pat, p["ppq"], p["tempo"], a.midi[1])
        print(f"wrote {a.midi[1]} ({len(pat['notes'])} notes)")
    elif a.json:
        json.dump(p, sys.stdout, indent=1, ensure_ascii=False)
    else:
        print_summary(p)


if __name__ == "__main__":
    main()
