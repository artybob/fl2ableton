"""Вырезает заготовки для записи .flp из настоящего проекта FL Studio 26. В нём должны быть:
сэмплер, канал с VST3-инструментом, аудиоклип, клип автоматизации параметра VST3 и VST3-эффект
на мастере. Заодно собирает справочник VST3-плагинов (имя, производитель, путь, список параметров).

python3 -I tools/make_flp_templates.py ref.flp [ещё.flp ...] fl2ableton/templates/fl
"""
import json, os, struct, sys
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..'))
from fl2ableton import flp
from fl2ableton.flpwrite import write_flp

*srcs, out = sys.argv[1:]
os.makedirs(out, exist_ok=True)
data = open(srcs[0], 'rb').read()
ppq, ev = flp.read_events(data)


def save(name, events):
    with open(os.path.join(out, name + '.flp'), 'wb') as f:
        f.write(write_flp(events, ppq, 0))


def utf16z(s):
    return (s + '\0').encode('utf-16-le')


ids = [e for e, _ in ev]
first_pattern = ids.index(65)
head = list(ev[:first_pattern])
for i, (e, v) in enumerate(head):
    if e == 202:
        head[i] = (e, utf16z(''))          # папка проекта автора заготовки
    elif e == 216:
        head[i] = (e, bytes(8) + v[8:])    # «последний тронутый параметр» — ссылка на чужой канал
save('head', head)

# Блоки каналов: от 64 до следующего 64 или до плейлиста (99)
arr_start = ids.index(99)
starts = [i for i, e in enumerate(ids) if e == 64]
blocks = {}
for a, b in zip(starts, starts[1:] + [arr_start]):
    block = ev[a:b]
    ctype = next(v for e, v in block if e == 21)
    internal = next((flp.utf16(v) for e, v in block if e == 201), '')
    has_sample = any(e == 196 for e, _ in block)
    kind = {0: 'sampler', 4: 'audio', 5: 'auto'}.get(ctype)
    if ctype == 2 and internal == 'Fruity Wrapper':
        kind = 'plugin'
    if kind and kind not in blocks and (kind != 'sampler' or has_sample):
        if kind == 'sampler':
            block = [(e, v) for e, v in block if e != 196]
        if kind == 'plugin':
            block = [(e, b'' if e == 213 else v) for e, v in block]
        blocks[kind] = block
for kind in ('sampler', 'plugin', 'audio', 'auto'):
    if kind not in blocks:
        raise SystemExit('в проекте нет канала вида %s' % kind)
    save('chan_' + kind, blocks[kind])

save('conn', [next((e, v) for e, v in ev if e == 227)])

# Аранжировка: от 99 до первого канала микшера; 233 — список элементов плейлиста
first_insert = ids.index(236)
# 42 (и имя с цветом) идут перед 236 своего канала микшера — их пишет сборщик
arrange = ev[arr_start:first_insert - 1 if ids[first_insert - 1] == 42 else first_insert]
save('arrange', arrange)
items = next(v for e, v in arrange if e == 233)
version = next(v for e, v in ev if e == 199).decode('ascii', 'replace').rstrip('\0')
size = flp.item_size(version)
chan_type = {}
for a in starts:
    chan_type[ev[a][1]] = next(v for e, v in ev[a:] if e == 21)
kinds = {}
for j in range(0, len(items), size):
    it = items[j:j+size]
    _, base, idx, _ = struct.unpack('<IHHI', it[:12])
    k = 'pattern' if idx > base else {4: 'audio', 5: 'auto'}.get(chan_type.get(idx))
    if k:
        kinds.setdefault(k, it)
with open(os.path.join(out, 'items.bin'), 'wb') as f:
    for k in ('pattern', 'audio', 'auto'):
        f.write(kinds[k])

# Канал микшера: начало (236), «хвост» после слотов (235 ... 147) — у мастера и у остальных свои
ins_starts = [i for i, e in enumerate(ids) if e == 236]
tail_start = ids.index(225, ins_starts[-1])


def insert_parts(a, b):
    block = ev[a:b]
    last_slot = max(i for i, (e, _) in enumerate(block) if e == 98)
    rest = block[last_slot + 1:]
    tail = [(e, v) for e, v in rest if e not in (149, 42, 204)]
    return block[0], tail


m_open, m_tail = insert_parts(ins_starts[0], ins_starts[1])
o_open, o_tail = insert_parts(ins_starts[1], ins_starts[2])
save('insert_master', [m_open] + m_tail)
save('insert', [o_open] + o_tail)
save('tail', ev[tail_start:])

# Слот эффекта: события от 201 до 98 у первого эффекта мастера
a = ins_starts[0]
slot = []
for e, v in ev[a + 1:]:
    if e == 98:
        break
    slot.append((e, b'' if e == 213 else v))
save('slot', slot)

# Обёртки VST3 без данных плагина: инструмент и эффект
def wrapper_template(v):
    s = flp.sections(v, 4)
    inner = flp.sections(s[53], 4)
    return {'head': v[:4].hex(), 'sections': [[t, d.hex()] for t, d in s.items() if t not in (52, 53, 54, 55, 56)],
            'state_head': s[53][:4].hex(), 'inner1': inner[1].hex()}


wrappers = {}
plugins = {}
for path in srcs:
    _, evs = flp.read_events(open(path, 'rb').read())
    for i, (e, v) in enumerate(evs):
        if e != 213 or v[:4] != b'\x0c\0\0\0':
            continue
        s = flp.sections(v, 4)
        if 52 not in s:
            continue
        inner = flp.sections(s[53], 4)
        kind = 'gen' if struct.unpack('<I', s[50][:4])[0] == 8 else 'fx'
        wrappers.setdefault(kind, wrapper_template(v))
        plugins[s[52].hex()] = {
            'name': s.get(54, b'').decode('utf-8', 'replace'),
            'vendor': s.get(56, b'').decode('utf-8', 'replace'),
            'path': s.get(55, b'').decode('utf-8', 'replace'),
            'params': inner.get(4, b'').hex(),
        }
with open(os.path.join(out, 'wrappers.json'), 'w', encoding='utf-8') as f:
    json.dump(wrappers, f, indent=1)
with open(os.path.join(out, 'plugins.json'), 'w', encoding='utf-8') as f:
    json.dump(plugins, f, indent=1, ensure_ascii=False)
print('ok:', sorted(blocks), sorted(kinds), sorted(wrappers), [p['name'] for p in plugins.values()])
