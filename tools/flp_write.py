#!/usr/bin/env python3
"""flp_write.py - scrive un progetto FL Studio 2026 (.flp) NUOVO partendo da un template.

Costruisce: tempo, canali Sampler con il campione (percorso assoluto), pattern con note e nome, clip di
pattern in playlist, e opzionalmente canali "audio clip" con la loro clip (per esempio il type beat di
riferimento). Non tocca mai un .flp esistente: scrive solo file nuovi.

Il formato e' documentato in docs/flp-format.it.md. I blocchi che non capiamo per
intero (canale Sampler, metadati del pattern, record della clip) sono COPIATI da file salvati da FL e
si cambiano solo i campi noti:
- canale Sampler: blocco del canale 0 di templates/empty-fl26.flp (64 indice, 203 nome, 196 sample, 104 insert)
- canale audio clip: canale 5 di templates/audio-clip-channel.flp (tipo 4)
- metadati pattern (65, 193 nome, 150 colore, segno 4/4 ...): da un progetto vero
- clip playlist: 88 byte; pos u32, 0x5000, 0x5000+pattern (o indice canale per l'audio), durata u32,
  traccia = 500 - n. traccia (1-based)
Ordine degli eventi come lo scrive FL: info progetto -> 216 -> note di TUTTI i pattern (65 + 224) ->
226 -> canale 0 -> metadati di TUTTI i pattern -> canali 1..n -> arrangiamento (99, 241, 36, 233 ...) -> mixer.

Uso come libreria:
    p = Project(bpm=100)
    k = p.add_sampler("Kick", "/Users/.../Kick.wav")
    pat = p.add_pattern("Kick loop", [(tick, durata, canale, tasto, velocity), ...])
    p.add_clip(pat, pos_tick, length_tick, track=2)
    p.save("nuovo.flp")
"""
import os
import struct
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from flp_inspect import read_events  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
TEMPLATES = os.path.join(os.path.dirname(HERE), "templates")
PPQ = 96

# metadati di un pattern come li salva FL 2026 (da 140 - Philadelphia, pattern 1): dopo 65/193/150
PATTERN_META_TAIL = [
    (157, b"\xff\xff\xff\xff"), (158, b"\xff\xff\xff\xff"), (148, struct.pack("<I", 0x08000000)),
    (33, b"\x04"), (34, b"\x04"), (46, b"\x00"), (45, b"\x00"), (168, b"\x00\x00\x00\x00"),
    (101, b"\x00\x00"), (205, "4/4\0".encode("utf-16-le")), (164, b"\x00\x00\x00\x00"), (52, b"\x00"),
    (171, b"\x00\x00\x00\x00"), (252, b""), (253, bytes.fromhex("30002c00" * 12)),
]
# record di una clip di pattern salvata da FL 2026 (CHANCE_2.flp): si cambiano solo i primi 16 byte
CLIP_TEMPLATE = bytes.fromhex(
    "000000000050015080610000ee0100007800400040648080ffffffffffffffff19000000000000000000000000000000"
    "00000000000000000000000000000000000000000000f03f00000000ffffffff0000000000000000")


def utf16(s):
    return (s + "\0").encode("utf-16-le")


def varint(n):
    out = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        out.append(b | (0x80 if n else 0))
        if not n:
            return bytes(out)


def encode(events):
    out = bytearray()
    for eid, data in events:
        out.append(eid)
        if eid >= 192:
            out += varint(len(data))
        out += data
    return bytes(out)


def note_bytes(tick, dur, channel, key, vel=100, pan=64):
    return struct.pack("<IHHIHHBBBBBBBB", int(tick), 0x4000, channel, int(dur), key, 0, 120, 0, 64, 0, pan,
                       max(1, min(127, int(vel))), 128, 128)


def channel_blocks(events):
    starts = [i for i, (e, _) in enumerate(events) if e == 64]
    end = next(i for i, (e, _) in enumerate(events) if e == 99)
    return [events[s:(starts[k + 1] if k + 1 < len(starts) else end)] for k, s in enumerate(starts)], starts[0], end


def set_event(block, eid, data, first_only=True):
    out, done = [], False
    for e, d in block:
        if e == eid and not (first_only and done):
            out.append((e, data))
            done = True
        else:
            out.append((e, d))
    if not done:
        raise ValueError(f"evento {eid} non presente nel blocco modello")
    return out


class Project:
    def __init__(self, bpm, template=os.path.join(TEMPLATES, "empty-fl26.flp")):
        self.events, self.header = read_events(template)
        blocks, self.ch_start, self.arr_start = channel_blocks(self.events)
        self.sampler_model = blocks[0]
        audio_tpl = os.path.join(TEMPLATES, "audio-clip-channel.flp")
        self.audio_model = None
        if os.path.exists(audio_tpl):
            ab, _, _ = channel_blocks(read_events(audio_tpl)[0])
            self.audio_model = next((b for b in ab if b[1] == (21, b"\x04")), None)
        self.bpm = bpm
        self.channels, self.patterns, self.clips = [], [], []

    # ------------------------------------------------------------ contenuti
    def add_sampler(self, name, sample_path, insert=None):
        idx = len(self.channels)
        b = set_event(self.sampler_model, 64, struct.pack("<H", idx))
        b = set_event(b, 203, utf16(name))
        b = set_event(b, 196, utf16(sample_path))
        b = set_event(b, 104, struct.pack("<H", insert if insert is not None else idx + 1))
        self.channels.append(b)
        return idx

    def add_audio_channel(self, name, sample_path, insert=None):
        if not self.audio_model:
            raise RuntimeError("manca templates/audio-clip-channel.flp")
        idx = len(self.channels)
        b = set_event(self.audio_model, 64, struct.pack("<H", idx))
        b = set_event(b, 203, utf16(name))
        b = set_event(b, 196, utf16(sample_path))
        try:
            b = set_event(b, 104, struct.pack("<H", insert if insert is not None else idx + 1))
        except ValueError:
            pass
        self.channels.append(b)
        return idx

    def add_pattern(self, name, notes, color=0x6A665C):
        """notes: (tick, durata, canale, tasto, velocity). Ritorna il numero del pattern (1-based)."""
        n = len(self.patterns) + 1
        data = b"".join(note_bytes(t, d, c, k, v) for t, d, c, k, v in sorted(notes))
        self.patterns.append((n, name, data, color))
        return n

    def add_clip(self, pattern, pos, length, track):
        r = bytearray(CLIP_TEMPLATE)
        struct.pack_into("<IHHIH", r, 0, int(pos), 0x5000, 0x5000 + pattern, int(length), 500 - track)
        self.clips.append((pos, bytes(r)))

    def add_audio_clip(self, channel, pos, length, track):
        r = bytearray(CLIP_TEMPLATE)
        struct.pack_into("<IHHIH", r, 0, int(pos), 0x5000, channel, int(length), 500 - track)
        self.clips.append((pos, bytes(r)))

    # ------------------------------------------------------------ scrittura
    def build_events(self):
        ev = self.events
        i216 = next(i for i, (e, _) in enumerate(ev) if e == 216)
        pre = [(e, struct.pack("<I", int(round(self.bpm * 1000)))) if e == 156 else (e, d) for e, d in ev[:i216 + 1]]
        mid = ev[i216 + 1:self.ch_start]
        notes = []
        for n, name, data, color in self.patterns:
            notes += [(65, struct.pack("<H", n)), (224, data)]
        meta = []
        for n, name, data, color in self.patterns:
            meta += [(65, struct.pack("<H", n)), (193, utf16(name)), (150, struct.pack("<I", color))]
            meta += PATTERN_META_TAIL
        chans = self.channels or [self.sampler_model]
        body = pre + notes + mid + chans[0] + meta + [e for b in chans[1:] for e in b]
        arr = []
        for e, d in ev[self.arr_start:]:
            if e == 233:
                d = b"".join(r for _, r in sorted(self.clips, key=lambda x: x[0]))
            arr.append((e, d))
        return body + arr

    def save(self, path):
        if os.path.exists(path):
            raise FileExistsError(f"{path} esiste gia': flp_write non sovrascrive mai")
        data = encode(self.build_events())
        n_ch = len(self.channels) or 1
        head = b"FLhd" + struct.pack("<IhHH", 6, self.header["format"], n_ch, self.header["ppq"])
        with open(path, "wb") as f:
            f.write(head + b"FLdt" + struct.pack("<I", len(data)) + data)
        # rilettura: deve tornare tutto
        ev2, h2 = read_events(path)
        assert h2["channel_count"] == n_ch
        assert sum(1 for e, _ in ev2 if e == 64) == n_ch
        return path


if __name__ == "__main__":
    # prova: 2 battute di kick + snare
    p = Project(100)
    packs = os.path.expanduser("~/Music/DON PACKS/Frank Ocean Drumkit by SeanBarbera/")
    k = p.add_sampler("Kick", packs + "Kicks/Nights Kick .wav")
    s = p.add_sampler("Snare", packs + "Snares/Nights Snare.wav")
    pat = p.add_pattern("Prova", [(0, 24, k, 60, 110), (192, 24, s, 60, 110), (384, 24, k, 60, 110), (576, 24, s, 60, 110)])
    p.add_clip(pat, 0, 768, 1)
    out = sys.argv[1] if len(sys.argv) > 1 else "/tmp/prova.flp"
    print(p.save(out))
