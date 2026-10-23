# CLAUDE.md

Notes for Claude Code (and humans) working on this repo. Read this first, then README.md.

## What it is

Two-way converter FL Studio `.flp` ⇄ Ableton Live 12 `.als`. Neither DAW is needed to run it.
Both file formats are undocumented: everything here was learned by diffing real projects
saved by FL Studio 25.2.5 / 26.1.7 and Ableton Live 12.4.6 (Windows).

## Hard rules

- **Standard library only, Python 3.6+.** The production server runs Python 3.6: no `dataclasses`
  (use `flp.Record`), no walrus, no `dict | dict`, no new typing syntax. No pip dependencies.
  The only exception is the optional Serum 1 → Serum 2 bridge (`serum1to2.py`), which runs
  an external tool in a separate interpreter.
- Run scripts with `python3 -I` (isolated mode) — the inputs are untrusted user archives.
- Output must stay deterministic: the same input gives a byte-identical `.als`/`.flp`.
  This is how regressions are caught (convert a known project before and after a change, compare).
- Anything that can't be converted goes into the warning list, never silently dropped.
  Warnings are user-facing and in Russian; `compact_warnings()` groups repeated ones.
- Code comments are in Russian, short (1–3 lines), explain *why*. Keep that style.

## Layout

| file | role |
|---|---|
| `fl2als.py` | CLI entry: `.flp`/folder/zip → Ableton folder; also `--zip` mode for the web front end, picks direction by archive contents |
| `als2flp.py` | CLI entry: `.als` → FL (`--fl 25|26`) |
| `fl2ableton/flp.py` | `.flp` reader → `Project` / `Channel` / `Insert` / `Item` / `Plugin` records; also `Record` base class |
| `fl2ableton/convert.py` | FL → Ableton: mixer layout, groups, sidechain, clips, samples, automation, plugin params |
| `fl2ableton/als.py` | `AlsBuilder`: assembles the Live XML from templates, keeps Live's ids consistent |
| `fl2ableton/alsread.py` | `.als` reader → `AlsProject` / `Track` / `Clip` / `Vst3` / `Eq8` / `Utility` / `Sampler` |
| `fl2ableton/reverse.py` | Ableton → FL: `FlpBuilder`, EQ/Utility mapping, Serum 1 → 2, Vital VST2 → VST3, FL 25 downgrade |
| `fl2ableton/flpwrite.py` | low-level FLP event encoder |
| `fl2ableton/colors.py` | Live colour index ⇄ FL RGB |
| `fl2ableton/templates/` | pieces cut out of real empty projects (XML for Live, raw event blobs for FL); regenerate with `tools/make_*` |
| `tools/flpdump.py` | dump every FLP event (id, size, utf-16 strings) — the main research tool |
| `tools/alsdump.py` | short summary of an `.als` (tracks, devices, clips, automation) |
| `tools/wrapdump.py` | dump the Fruity Wrapper (VST) blob of a channel |

## How to work on a format question

1. Get a **pair**: the same small project saved in FL and rebuilt by hand in Ableton
   (or the other way), with tracks named the same. Without a pair, don't guess units.
2. `python3 -I tools/flpdump.py project.flp > a.txt`, change one thing in FL, dump again, diff.
   For Live: `zcat project.als > a.xml` and diff the XML.
3. Encode the finding next to the code that uses it, with a comment on where it came from.
4. Re-convert the previous sample projects and check the output didn't change unexpectedly.

Sample projects are **not** in the repo (they are other people's music, see `.gitignore`).
Ask the user for one; keep it in `ref*/` locally.

## Things already learned (don't rediscover them)

FLP:
- Event id ranges: `<64` byte, `<128` word, `<192` dword, `>=192` varint length + data.
  **Exception: event 172 is 3 bytes in FL 25.2+**, otherwise parsing desyncs (tempo is lost).
- Tempo: event 156 = BPM × 1000.
- Channel → mixer insert: event 104. A mixer slot is closed by event 98.
- Mixer params: event 225; `(cd − 0x7000) >> 6` = insert number, id 192 = volume.
- Playlist (FL 26): 88-byte items; position/offset in int ticks for patterns, float for audio channels.
  Item's track = `499 − u32` at offset +12. Event 238 = track number (1-based), next 239 = its name.
- Routing: event 235 = one byte per target insert. Insert routed only to another insert → Live GroupTrack.
- Sidechain: wrapper section 31, inputs of 12 bytes `(n, ?, 0)`; `n` = n-th insert routed into this one;
  `(0,0,0)` = not selected, `(0,1,0)` = plugin without sidechain.
- Automation link: event 227, target `(channel << 16) | 0x8000 | param_index`; index into wrapper list 4 → VST3 ParameterId.
  Mixer volume/pan targets: `((0x7000 + n) << 6) << 16 | 0x1fc0` / `0x1fc1`; value 0..1, 0.8 = 0 dB, pan = v·2 − 1.
  Automation points are stored as a step from the previous point.
- Sample pitch: event 219 bytes 8..12 = cents (→ Re-Pitch), event 215 bytes 100..104 = shift inside stretch (→ Complex Pro).
  Stretch units: value / 192 = beats.
- Fruity Wrapper (VST3): blocks `[u32 type][u64 len]`; 52 = GUID (Windows byte order), 54 name, 56 vendor,
  53 state (inside: 3 = processor, 2 = controller, 4 = param id list). VST3 state is byte-identical to Live's.
- FL volume → dB is only calibrated at a few points (`FL_VOL_DB` in convert.py).

ALS:
- gzip'ed XML. `AutomationTarget`/`Pointee` ids are global across the set, others are per list (`GLOBAL_ID` in als.py).
- Samples: Live needs the `Ableton Project Info` folder to resolve relative paths (RelativePathType 3),
  and `OriginalCrc` = CRC-16 (poly 0x8005, init 0, no reflection) over the first 16 KB of the file,
  otherwise the sample shows as offline.
- Plugin parameter ranges in Live come from the plugin itself, so they're a table (`PARAM_RANGES`, `PARAM_DB`);
  unknown plugins are written as 0..1 with a warning.
- Python < 3.8 sorts XML attributes on write; als.py patches that to keep Live's order.

Plugin states:
- JUCE plugins: VST3 state = VST2 chunk wrapped as `"VC2!" + size + XML` → VST2 ⇄ VST3 is a rewrap (`juce_vst3_state`).
- FabFilter: VST3 `FabF` v2 vs VST2/CLAP `FFBS` v1, same 32 floats in the same order.
- Serum 1 → Serum 2 needs the external serum-preset-packager (see README); without it the `.fxp` is put next to the project.

## Open ends (good first contributions)

- More calibration points for FL volume in the middle of the fader.
- FL native effects other than Parametric EQ 2 / Balance (Delay 3, Reeverb 2, …).
- Plugin automation for more plugins (needs the Live parameter range for each).
- Tested on a handful of real projects only — every new sample project tends to show something.
