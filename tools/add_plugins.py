"""Дополняет справочник VST3 (templates/fl/plugins.json) плагинами из проектов FL и сохраняет
заготовку состояния kHs Gain (templates/khs_gain.bin) — на него Ableton -> FL переводит Utility.
Образец kHs Gain с громкостью 0 дБ — ref5 (ABLE_FL_BALANCE_UTIL22, канал микшера 3).

python3 -I tools/add_plugins.py fl2ableton/templates проект.flp [проект.flp ...]
"""
import io, json, os, re, struct, sys, zipfile
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..'))
from fl2ableton import flp

KHS_GAIN = '5116aba77200cc4400e35a8d00748379'

out, srcs = sys.argv[1], sys.argv[2:]
path = os.path.join(out, 'fl', 'plugins.json')
plugins = json.load(open(path, encoding='utf-8'))
added = []
for src in srcs:
    _, evs = flp.read_events(open(src, 'rb').read())
    for e, v in evs:
        if e != 213 or v[:4] != b'\x0c\0\0\0':
            continue
        s = flp.sections(v, 4)
        if 52 not in s or s[52].hex() in plugins:
            continue
        inner = flp.sections(s[53], 4)
        key = s[52].hex()
        if key == KHS_GAIN:
            z = zipfile.ZipFile(io.BytesIO(inner[3][8:]))
            if not re.search(r'"gain": "1"', z.read('state.json').decode('utf-8')):
                continue        # нужна копия с 0 дБ
            with open(os.path.join(out, 'khs_gain.bin'), 'wb') as f:
                f.write(inner[3])
        plugins[key] = {
            'name': s.get(54, b'').decode('utf-8', 'replace'),
            'vendor': s.get(56, b'').decode('utf-8', 'replace'),
            'path': s.get(55, b'').decode('utf-8', 'replace'),
            'params': inner.get(4, b'').hex(),
        }
        added.append(plugins[key]['name'])
with open(path, 'w', encoding='utf-8') as f:
    json.dump(plugins, f, indent=1, ensure_ascii=False)
print('добавлены:', added)
