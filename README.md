# fl2ableton

Converts projects between **FL Studio** and **Ableton Live** in both directions:

- `.flp` → Ableton Live 12 project folder (`.als` + samples)
- `.als` → FL Studio project (`.flp` + samples), for FL Studio 26 or 25.2.5

Pure Python 3, standard library only. No FL Studio or Ableton needed to run it.

[Описание на русском ниже](#по-русски)

## Usage

```sh
# FL Studio -> Ableton
python3 -I fl2als.py <project.flp | folder | zip> <output folder>

# Ableton -> FL Studio (default FL 26)
python3 -I als2flp.py [--fl 25|26] <project.als | Live project folder | zip> <output folder>
```

Anything that couldn't be carried over is printed as a warning after `готово:` (“done”).

There is also a zip-in / zip-out mode used by a web front end:

```sh
python3 -I fl2als.py --zip <input.zip> <result.zip> <work dir> [26|25]
```

The direction is picked from the archive contents (`.flp` or `.als`); a JSON report is written to `<result.zip>.json`.

## What gets converted

- tempo, tracks/channels, names, colors
- MIDI notes and patterns ↔ clips, playlist ↔ arrangement
- audio clips and samples (copied into the new project)
- mixer: volume, routing, groups
- FL sampler ↔ Simpler (including sample start/end)
- Drum Rack pads and Instrument Rack layers → separate FL channels
- third-party plugins (VST2/VST3) with their state where the format allows it
- EQ Eight ↔ Fruity Parametric EQ 2, Utility ↔ Fruity Balance / kHs Gain
- Vital VST2 → Vital VST3

Native effects without a counterpart on the other side are skipped with a warning.

### Serum 1 → Serum 2 (optional)

When going Ableton → FL, Serum 1 is replaced with Serum 2, and the original `.fxp` presets are put next to the project in `Serum 1 presets/`.
Automatic translation of the preset needs an external tool that is **not included** here:
[serum-preset-packager](https://github.com/kasperikaivola/serum-preset-packager) (Python 3.7+, `cbor2`, `zstandard`).
Point the converter to it with:

```sh
export FL2ABLETON_S1_PYTHON=/path/to/venv/bin/python
export FL2ABLETON_S1_TOOL=/path/to/serum-preset-packager
```

Without it everything else works, you just load the `.fxp` into Serum 2 by hand.

## Known gaps

- Plugin automation is partly supported.
- FL Volume conversion is approximate in the middle of the fader range (few calibration points).
- FL Delay 3, Fruity Reeverb 2 and many other native effects are not converted.
- Only tested on a limited set of real projects — bug reports with a small sample project are very welcome.

## Project layout

```
fl2als.py, als2flp.py      command line entry points
serum1to2.py               Serum 1 -> Serum 2 bridge (runs the external tool)
fl2ableton/flp.py          .flp reader
fl2ableton/flpwrite.py     .flp writer
fl2ableton/convert.py      FL -> Ableton
fl2ableton/alsread.py      .als reader
fl2ableton/als.py          .als builder
fl2ableton/reverse.py      Ableton -> FL
fl2ableton/templates/      pieces of real empty projects used as building blocks
tools/                     scripts that dump files and regenerate the templates
```

Code comments are in Russian.

## License

GPL-3.0, see [LICENSE](LICENSE). Fork it, improve it, send it back.

FL Studio is a trademark of Image-Line, Ableton Live of Ableton AG, Serum of Xfer Records. This project is not affiliated with any of them.

---

## По-русски

Конвертер проектов **FL Studio ⇄ Ableton Live** в обе стороны. Чистый Python 3, только стандартная библиотека, сами программы для запуска не нужны.

```sh
python3 -I fl2als.py <проект.flp | папка | zip> <папка для результата>
python3 -I als2flp.py [--fl 25|26] <проект.als | папка проекта | zip> <папка для результата>
```

Что не удалось перенести, печатается списком после «готово:».
Перевод Serum 1 → Serum 2 работает через внешний [serum-preset-packager](https://github.com/kasperikaivola/serum-preset-packager), в репозиторий он не входит (см. выше).

Лицензия GPL-3.0: форкайте, улучшайте, присылайте правки.
