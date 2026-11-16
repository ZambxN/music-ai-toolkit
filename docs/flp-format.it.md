# Formato .flp di FL Studio 2026 (per il generatore)

Scoperte fatte confrontando il template vuoto con copie che hanno UNA modifica fatta in FL
(`work/flp-diff/`, 2026-10-01, FL 26.1.6) e leggendo i progetti veri di Marco (Philadelphia 140, Orlando 156).
Strumenti: `tools/flp_inspect.py` (`read_events`) e `tools/evdiff.py` (diff a livello
di eventi tra due .flp). Stato: ✅ confermato da diff · 🔎 visto nei progetti di Marco, non provato con un diff
· ❓ da verificare.

## Struttura generale
- Header `FLhd` (format 0, n. canali, PPQ 96) + blocco `FLdt` con una lista di eventi `(id, dati)`.
  Id < 64: 1 byte · 64–127: 2 byte · 128–191: 4 byte · ≥ 192: lunghezza variabile (varint) + dati.
- Il numero di canali nell'header deve combaciare con i canali nel corpo (in 03-sample passa da 5 a 6). ✅
- Ordine nel corpo: info progetto → pattern (note) → canali → arrangiamento (playlist, marker, tracce) → mixer.

## Info progetto
| Evento | Significato | Stato |
|---|---|---|
| 199 | versione FL (ASCII, es. `26.1.6.5406`) | ✅ |
| 156 | **tempo = BPM × 1000** (130000 → 140000) | ✅ |
| 237 | 16 byte: due double (data di creazione + tempo di lavoro). Cambia a ogni salvataggio: si può ignorare/copiare | ✅ |
| 242 | 28 byte, cambia anche solo aggiungendo un marker: probabilmente stato della vista playlist. Ignorabile | ❓ |
| 9 | passa da 0 a 1 dopo aver creato il marker: probabilmente loop/modalità song | ❓ |

## Pattern e note
- `65 = n` apre il pattern n (1-based); `193` = nome del pattern (UTF-16) 🔎; `150` = colore 🔎.
- `224` = **note del pattern**, 24 byte per nota, subito dopo il `65` del pattern. ✅ Layout:

| Byte | Campo | Esempio (step 1 del Kick) |
|---|---|---|
| 0–3 | posizione in tick (u32, PPQ 96: 1 battuta 4/4 = 384) | 0 |
| 4–5 | flag (u16) | 0x4000 |
| 6–7 | canale del rack (u16, 0-based) | 0 |
| 8–11 | durata in tick (u32; 0 = step del sequencer) | 0 |
| 12–13 | tasto (u16, 60 = C5 in FL) | 60 |
| 14–15 | gruppo (u16) | 0 |
| 16 | fine pitch (120 = centro) | 0x78 |
| 17 | sconosciuto | 0 |
| 18 | release (64) | 0x40 |
| 19 | canale MIDI | 0 |
| 20 | pan (64 = centro) | 0x40 |
| 21 | velocity (100) | 0x64 |
| 22–23 | mod X / mod Y (128) | 0x80 0x80 |

Nei progetti di Marco le note hanno durate vere (es. 0x90 = 144 tick) e canali diversi nello stesso pattern 🔎.

## Canali (channel rack)
- Ogni canale inizia con `64 = indice` (0-based), poi `21 = tipo`: 0 sampler, 2 plugin nativo/VST, 4 audio clip
  (flp_inspect lo chiama "instrument"). ✅
- `201` nome del plugin (vuoto = Sampler) · `212` dati del plugin · `203` **nome del canale** ✅.
- `104` = **insert del mixer** a cui è mandato il canale 🔎 (Philadelphia: DHL Kick → 9, clap → 10).
- `196` = **percorso del sample**. Per i pack di Marco FL scrive
  `%FLStudioFactoryData%/Data/Patches/!DON PACKS/<pack>/.../<file>.wav` ✅ — il generatore deve usare
  questo prefisso (i pack sono dentro l'app). Per audio fuori dai pack: percorso assoluto 🔎.
- 03-sample: trascinare un sample dal browser ha creato un **canale nuovo di tipo audio clip** (n. 5,
  `203 'BWB WAV X FX (1)'`) e un gruppo filtro `231 'Audio'`, non ha sostituito il sample del Kick. Il blocco
  completo di eventi di un canale (~47 eventi) è nel diff: per scrivere un canale conviene **copiare il blocco
  di un canale Sampler del template** e cambiare `64`, `203`, `196`, `104`.

## Playlist (arrangiamento)
- `99` / `241 'Arrangement'` aprono l'arrangiamento; `233` contiene **tutti i clip**, 88 byte ciascuno
  (vuoto nel template). Layout 🔎 (Philadelphia, 87 clip) + ✅ un clip audio in 03-sample:

| Byte | Campo |
|---|---|
| 0–3 | posizione in tick (u32) |
| 4–5 | base, sempre 0x5000 (20480) |
| 6–7 | indice: **0x5000 + n. pattern** per un clip di pattern; **indice del canale** per un clip audio |
| 8–11 | durata in tick (1536 = 4 battute) |
| 12–13 | traccia, salvata come 500 − traccia (❓ 0- o 1-based) |
| 14–15 | gruppo |
| 16–87 | resto: `7800400040648080`, offset inizio/fine (`ffffffffffffffff` = intero), un id, float/double fissi. **Copiare da un clip esistente** e cambiare solo i campi sopra |

- Dopo `233` ci sono le tracce della playlist: `238` (dati traccia) + `43`, una coppia per traccia.

## Marker
✅ (05-marker: marker "Hook" a battuta 9). Inserito subito dopo `233`, come gruppo di eventi:
`148 = posizione` · `33 = 4` · `34 = 4` (tempo in chiave) · `46 = 0` · `45 = 0` · `168 = 0` · `101 = 0` ·
`205 = nome`.
- `148` = posizione in tick (3072 = battuta 9) + **tipo nel byte alto**: 0 = marker normale. Nei progetti di
  Marco: 0x08000000 = cambio di tempo in chiave (nome `4/4`), 0x06000000 = loop (`Loop: Bar`),
  0x0B000000 = altro tipo ❓.

## Mixer
🔎 Per ogni insert: nome `204` (se impostato) **subito prima** del suo `236` (12 byte), poi `147` (routing/
uscita ❓). Il nome di un insert è opzionale.

## Cose ancora da fare (prima o durante `flp_write.py`)
- Clip di pattern in playlist e nome/routing di un insert: non serve più un diff fatto in FL — il formato si
  vede nei progetti di Marco. Si verifica scrivendo un .flp e aprendolo in FL.
- Verificare: indice della traccia (0/1-based), significato di `9`, `147`.
- Lezione di computer use: le finestre di testo di FL (nome marker, rinomina) non si possono usare in
  background → evitarle; il generatore scrive i nomi direttamente nel file.
