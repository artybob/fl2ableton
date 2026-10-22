"""Чтение проекта FL Studio (.flp) в простую модель."""
import struct


class Record:
    """Простая запись с полями по умолчанию: на сервере Python 3.6, там нет dataclasses."""
    FIELDS = ()   # (имя, значение по умолчанию); list/dict копируются на каждый объект

    def __init__(self, *args, **kw):
        for (name, default), v in zip(self.FIELDS, args):
            kw.setdefault(name, v)
        for name, default in self.FIELDS:
            v = kw.pop(name, default)
            setattr(self, name, type(v)() if v is default and isinstance(v, (list, dict)) else v)
        if kw:
            raise TypeError('лишние поля: %s' % ', '.join(kw))


CH_SAMPLER, CH_NATIVE, CH_AUDIOCLIP, CH_AUTOMATION = 0, 2, 4, 5


def read_events(data):
    assert data[:4] == b'FLhd', 'не FLP-файл'
    hl = struct.unpack('<I', data[4:8])[0]
    fmt, nch, ppq = struct.unpack('<HHH', data[8:14])
    p = 8 + hl
    assert data[p:p+4] == b'FLdt'
    end = p + 8 + struct.unpack('<I', data[p+4:p+8])[0]
    p += 8
    events = []
    while p < end:
        eid = data[p]; p += 1
        if eid < 64:
            v = data[p]; p += 1
        elif eid < 128:
            v = struct.unpack('<H', data[p:p+2])[0]; p += 2
        elif eid == 172:
            # FL 25.2+ пишет 172 тремя байтами, хотя id из диапазона DWORD
            v = int.from_bytes(data[p:p+3], 'little'); p += 3
        elif eid < 192:
            v = struct.unpack('<I', data[p:p+4])[0]; p += 4
        else:
            n = sh = 0
            while True:
                c = data[p]; p += 1
                n |= (c & 0x7f) << sh; sh += 7
                if not c & 0x80:
                    break
            v = data[p:p+n]; p += n
        events.append((eid, v))
    return ppq, events


def utf16(b):
    return b.decode('utf-16-le', 'replace').rstrip('\0')


def sections(d, start=0):
    """Цепочка блоков [u32 тип][u64 длина][данные]."""
    out, p = {}, start
    while p + 12 <= len(d):
        t, n = struct.unpack('<IQ', d[p:p+12]); p += 12
        out[t] = d[p:p+n]; p += n
    return out


class Plugin(Record):
    FIELDS = (
        ('name', None),
        ('vendor', ''),
        ('format', ''),          # 'vst3', 'vst2', 'clap' или '' (встроенный FL)
        ('guid', b''),         # VST3 class id в порядке байтов FL (как GUID Windows)
        ('processor', b''),
        ('controller', b''),
        ('param_ids', []),  # индекс параметра FL -> id параметра VST3
        ('state', b''),     # VST2 — чанк плагина, CLAP — состояние как есть
        ('vst2_id', 0),
        ('path', ''),
        ('sc_input', 0),       # второй вход плагина: номер входящего канала микшера (с 1), 0 — не выбран
        ('sc_free', False),    # второй вход есть, но не выбран и не занят ничем (0, 0, 0)
        ('data', b''),      # встроенный плагин FL — его собственные данные
    )


def parse_plugin(internal, data):
    if internal != 'Fruity Wrapper':
        return Plugin(name=internal or 'встроенный', format='', data=data or b'')
    ch = sections(data, 4)
    pl = Plugin(name=ch.get(54, b'').decode('utf-8', 'replace'),
                vendor=ch.get(56, b'').decode('utf-8', 'replace'),
                path=ch.get(55, b'').decode('utf-8', 'replace'))
    # 31 — входы плагина по 12 байт: (номер входа FL, ?, 0). Для второго входа номер — какой по счёту
    # из каналов, идущих в этот канал микшера (KANINE: два ShaperBox на одном канале — 1 и 2)
    buses = ch.get(31, b'')
    if len(buses) >= 24:
        bus = struct.unpack('<3I', buses[12:24])
        pl.sc_input, pl.sc_free = bus[0], bus == (0, 0, 0)
    guid = ch.get(52, b'')
    state = ch.get(53, b'')
    # у CLAP есть свой id (58) и путь к .clap; состояние в формате плагина, без обёртки VST
    if 58 in ch or pl.path.lower().endswith('.clap'):
        pl.format, pl.state = 'clap', state
    elif len(guid) == 16 and state[:4] == b'\x01\0\0\0':
        pl.format, pl.guid = 'vst3', guid
        st = sections(state, 4)
        pl.processor = st.get(3, b'')
        pl.controller = st.get(2, b'')
        ids = st.get(4, b'')
        if len(ids) >= 4:
            n = struct.unpack('<I', ids[:4])[0]
            pl.param_ids = list(struct.unpack('<%dI' % n, ids[4:4+4*n]))
    else:
        pl.format = 'vst2'
        if 51 in ch:
            pl.vst2_id = struct.unpack('<I', ch[51][:4])[0]
        # f7ffffff, 0d, feffffff, u32 размер, 8 нулей, дальше чанк плагина как есть.
        # С 05 вместо 0d чанка нет — плагин хранит только значения параметров
        if len(state) >= 21 and state[:4] == b'\xf7\xff\xff\xff' and state[4] == 0x0d:
            n = struct.unpack('<I', state[9:13])[0]
            pl.state = state[21:21+n]
    return pl


class Channel(Record):
    FIELDS = (
        ('index', None),
        ('type', 0),
        ('name', ''),
        ('plugin', None),
        ('sample_path', ''),
        ('insert', 0),
        ('stretch_time', 0.0),
        ('pitch', 0),          # центы, высота канала: без растяжки FL меняет и скорость
        ('stretch_pitch', 0),  # центы, сдвиг высоты в растяжке: скорость не меняется
        ('sample_start', 0.0),  # начало сэмпла, доля файла
        ('sample_length', 1.0), # длина — доля остатка после начала
        ('automation', []),  # [(позиция в долях, значение 0..1)]
    )


class Insert(Record):
    FIELDS = (
        ('index', None),
        ('name', ''),
        ('color', None),  # свой цвет канала микшера (событие 149)
        ('slots', {}),   # номер слота -> Plugin
        ('volume', 12800),
        ('pan', 0),
        ('routes', []),   # куда идёт звук: номера каналов микшера
    )


class Item(Record):
    FIELDS = (
        ('pos', None),          # тики
        ('length', None),       # тики
        ('pattern', None),
        ('channel', None),
        ('start', None),   # смещения: тики у паттернов, доли у автоматизации, мс у аудио
        ('end', None),
        ('track', 0),      # дорожка плейлиста, с нуля
    )


class Project(Record):
    FIELDS = (
        ('version', ''),
        ('ppq', 96),
        ('tempo', 140.0),
        ('channels', {}),
        ('patterns', {}),  # номер -> [(pos, len, key, vel, канал)]
        ('inserts', []),
        ('items', []),
        ('connections', []),  # (канал автоматизации, код цели)
        ('track_names', {}),  # дорожка плейлиста (с нуля) -> имя
        ('track_colors', {}),  # дорожка плейлиста (с нуля) -> свой цвет (R | G << 8 | B << 16)
        ('markers', []),      # [(тики, имя)] — маркеры над плейлистом
        ('warnings', []),
    )


def item_size(version):
    major = int(version.split('.')[0]) if version else 20
    if major >= 26:
        return 88
    if version >= '24.2.99' or major >= 25:
        return 80
    return 60


def parse_automation(d):
    n = struct.unpack('<I', d[17:21])[0]
    pts, pos = [], 0.0
    for i in range(n):
        dp, val = struct.unpack('<dd', d[21+24*i:37+24*i])
        # позиция хранится шагом от предыдущей точки (проверено на клипе из 9 точек)
        pos = dp if i == 0 else pos + dp
        pts.append((pos, val))
    return pts


def parse(data):
    ppq, events = read_events(data)
    pr = Project(ppq=ppq)
    ch = None
    pat = None
    in_mixer = False
    ins = None
    pending_name = ''
    pending_color = None
    track_color = None
    plug_internal, plug_data = '', None
    arrangement = None
    track_no = None
    marker = None
    for i, (eid, v) in enumerate(events):
        if eid == 199:
            pr.version = v.decode('ascii', 'replace').rstrip('\0')
        elif eid == 156:
            pr.tempo = v / 1000
        elif eid == 64:
            ch = pr.channels[v] = Channel(index=v)
            plug_internal, plug_data = '', None
        elif eid == 21 and ch and not in_mixer:
            ch.type = v
        elif eid == 201:
            plug_internal, plug_data = utf16(v), None
        elif eid == 213:
            plug_data = v
            if in_mixer:
                continue
            if ch is not None:
                ch.plugin = parse_plugin(plug_internal, v)
        elif eid == 203 and ch and not in_mixer:
            ch.name = utf16(v)
        elif eid == 196 and ch:
            ch.sample_path = utf16(v)
        elif eid == 104 and ch and not in_mixer:
            ch.insert = v
        elif eid == 215 and ch and not in_mixer and len(v) >= 104:
            ch.stretch_time = struct.unpack('<f', v[96:100])[0]
            ch.stretch_pitch = struct.unpack('<i', v[100:104])[0]
            if len(v) >= 144:
                ch.sample_start, ch.sample_length = struct.unpack('<dd', v[128:144])
        elif eid == 219 and ch and not in_mixer and len(v) >= 12:
            ch.pitch = struct.unpack('<i', v[8:12])[0]
        elif eid == 234 and ch:
            ch.automation = parse_automation(v)
        elif eid == 65:
            pat = v
            pr.patterns.setdefault(pat, [])
        elif eid == 224 and pat is not None:
            for j in range(0, len(v) - 23, 24):
                pos, _, rack, ln, key = struct.unpack('<IHHIH', v[j:j+14])
                vel = v[j+21]
                pr.patterns[pat].append((pos, ln, key, vel, rack))
        elif eid == 99:
            arrangement = v
        elif eid == 233 and arrangement in (None, 0):
            size = item_size(pr.version)
            for j in range(0, len(v) - size + 1, size):
                it = v[j:j+size]
                pos, base, idx, ln, tr = struct.unpack('<IHHII', it[:16])
                item = Item(pos=pos, length=ln, track=499 - tr)
                if idx > base:
                    item.pattern = idx - base
                    s, e = struct.unpack('<ii', it[24:32])
                    item.start, item.end = (None, None) if s < 0 else (s, e)
                else:
                    item.channel = idx
                    item.start, item.end = struct.unpack('<ff', it[24:32])
                pr.items.append(item)
        elif eid == 227 and len(v) >= 12:
            auto_ch = struct.unpack('<H', v[2:4])[0]
            target = struct.unpack('<I', v[8:12])[0]
            pr.connections.append((auto_ch, target))
        # маркер: 148 позиция, дальше 33/34 размер и 205 имя; с флагом 0x08000000 — смена размера
        elif eid == 148 and arrangement in (None, 0):
            marker = None
            if not v & 0x08000000:
                marker = [v, '']
                pr.markers.append(marker)
        elif eid == 205 and marker is not None:
            marker[1] = utf16(v)
            marker = None
        elif eid == 238 and arrangement in (None, 0):
            track_no = struct.unpack('<I', v[:4])[0] - 1
            track_color = struct.unpack('<I', v[4:8])[0] & 0xffffff
        elif eid == 43 and i and events[i-1][0] == 238 and arrangement in (None, 0):
            # сразу за дорожкой: 1 — у неё свой цвет
            if v:
                pr.track_colors[track_no] = track_color
        elif eid == 239 and arrangement in (None, 0):
            pr.track_names[track_no] = utf16(v)
        elif eid == 235 and ins is not None:
            ins.routes = [j for j, x in enumerate(v) if x]
        elif eid == 204:
            pending_name = utf16(v)
        elif eid == 149:
            pending_color = v & 0xffffff
        elif eid == 236:
            in_mixer = True
            ins = Insert(index=len(pr.inserts), name=pending_name, color=pending_color)
            pr.inserts.append(ins)
            pending_name, pending_color = '', None
            plug_internal, plug_data = '', None
        elif eid == 98 and ins is not None:
            if plug_data is not None:
                ins.slots[v] = parse_plugin(plug_internal, plug_data)
            plug_internal, plug_data = '', None
        elif eid == 225:
            for j in range(0, len(v) - 11, 12):
                _, pid, _, cd, msg = struct.unpack('<IBBHi', v[j:j+12])
                n = (cd - 0x7000) >> 6
                if 0 <= n < len(pr.inserts) and cd >= 0x7000:
                    if pid == 192:
                        pr.inserts[n].volume = msg
                    elif pid == 193:
                        pr.inserts[n].pan = msg
    return pr
