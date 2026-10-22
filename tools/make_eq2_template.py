"""Заготовка слота микшера со встроенным плагином FL: первый такой плагин в проекте, от 201 до 213.
Parametric EQ 2 (для EQ Eight из Ableton) — образец ref3 (BASS_GROUP);
Fruity Balance (для Utility) — образец ref4 (UTBL_FL, канал 1).

python3 -I tools/make_eq2_template.py проект.flp fl2ableton/templates/fl ["Fruity Balance" slot_balance]
"""
import os, sys
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..'))
from fl2ableton import flp
from fl2ableton.flpwrite import write_flp

src, out = sys.argv[1:3]
plugin, fname = sys.argv[3:5] if len(sys.argv) > 4 else ('Fruity Parametric EQ 2', 'slot_eq2')
ppq, ev = flp.read_events(open(src, 'rb').read())
name = (plugin + '\0').encode('utf-16-le')
start = next(i for i, (e, v) in enumerate(ev) if e == 201 and v == name)
end = next(i for i in range(start, len(ev)) if ev[i][0] == 213)
with open(os.path.join(out, fname + '.flp'), 'wb') as f:
    f.write(write_flp(ev[start:end + 1], ppq, 0))
