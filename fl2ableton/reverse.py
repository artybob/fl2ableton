"""Ableton Live (.als) -> папка проекта FL Studio 26 (.flp + сэмплы).

Файл собирается из заготовок настоящего проекта FL (templates/fl, см. tools/make_flp_templates.py):
служебные события берутся как есть, подставляются только темп, каналы, ноты, плейлист и микшер.
"""
import io, json, math, os, re, shutil, struct, subprocess, tempfile, zipfile
from . import flp, alsread
from .colors import live_to_fl
from .convert import FL_VOL_DB, compact_warnings, extract_zip, find_file
from .flpwrite import write_flp

TPL = os.path.join(os.path.dirname(__file__), 'templates', 'fl')
PATTERN_BASE = 20480        # у элемента плейлиста номер паттерна хранится как base + номер
TRACKS = 500                # столько дорожек в плейлисте FL 26
MIN_INSERTS = 18            # столько каналов микшера в заготовке (мастер + 16 + служебный «Current»)
CURRENT_INSERT = 501        # номер служебного последнего канала в параметрах микшера (событие 225)
SLOTS = 10
# версии FL, в которые умеем писать: строка версии, сборка
FL_VERSIONS = {'26': ('26.1.7.5653', 5653), '25': ('25.2.5.5319', 5319)}
BALANCE_MAX = 1.25          # громкость Fruity Balance: 0..320, 256 = 0 дБ
# kHs Gain (Kilohearts, VST3) — замена Utility: ±30 дБ, параметр Gain (id 1) линейный по дБ, 0,5 = 0 дБ
KHS_GUID = bytes.fromhex('5116aba77200cc4400e35a8d00748379')
KHS_DB = 30.0
# Serum 1 (VST2, UniqueId 'XfsX') ставим как Serum 2 (VST3): настройки Serum 1 Serum 2 сам перевести не даёт —
# кладём их .fxp рядом с проектом, пользователь загружает его в Serum 2 (Load preset)
SERUM1_ID = 0x58667358
SERUM1_VST3_GUID = bytes.fromhex('58545356736673586572756d00000000')   # у VST3 Serum 1 состояние = тот же чанк
SERUM2_GUID = bytes.fromhex('58455356736673506572756d20320000')
SERUM1_DIR = 'Serum 1 presets'
PLACEHOLDER = 'пустой (служебный)'
# хвост состояния VST2 без чанка, как у MasterMagic в KANINE: программа «Default» и служебные поля
VST2_PARAMS_TAIL = bytes.fromhex('01000000') + b'Default\0' + bytes.fromhex('08000000 00704e14 00000000 0001000000')
# VST2 на JUCE, которые ставим как VST3 того же плагина: UniqueId VST2 -> GUID VST3 (есть в plugins.json).
# Состояние у JUCE-VST3 — то же, что чанк VST2, в обёртке (см. juce_vst3_state)
VST2_AS_VST3 = {
    0x56697461: '56545356746976616974616c00000000',     # Vital
}
# перевод Serum 1 -> Serum 2 (serum1to2.py): отдельный Python 3.7+ с cbor2/zstandard и внешний переводчик
S1_PYTHON = os.environ.get('FL2ABLETON_S1_PYTHON', '/opt/s1conv/venv/bin/python')
S1_TOOL = os.environ.get('FL2ABLETON_S1_TOOL', '/opt/s1conv/serum-preset-packager')
FL_VOL_MAX = 16000          # фейдер канала микшера FL: +5,6 дБ
# режим полосы EQ Eight -> тип полосы Parametric EQ 2 (1 LP, 3 HP, 4 notch, 5 low shelf, 6 колокол, 7 high shelf)
EQ8_TO_EQ2 = {0: 3, 1: 3, 2: 5, 3: 6, 4: 4, 5: 7, 6: 1, 7: 1}
NAMED_COLOR = 6972764       # цвет, с которым FL пишет переименованный канал микшера


def eq2_freq(hz):
    """Гц -> 0..1 частоты Parametric EQ 2: 20·1000^x."""
    return max(0.0, min(1.0, math.log(max(hz, 20) / 20) / math.log(1000)))


def eq2_width(q):
    """Q -> 0..1 ширины Parametric EQ 2 (Q = 15,2·e^(−8,28e−5·w), w = 0..65535)."""
    return max(0.0, min(1.0, -math.log(max(q, 0.1) / 15.2) / 8.28e-5 / 65535))


def eq2_value(what, v):
    """Значение EQ Eight (Гц, дБ, Q) -> 0..1 параметра Parametric EQ 2. Усиление ±18 дБ (в данных −1800..1800)."""
    if what == 'Freq':
        return eq2_freq(v)
    if what == 'Gain':
        return max(0.0, min(1.0, (v + 18) / 36))
    return eq2_width(v)


def load(name):
    return flp.read_events(open(os.path.join(TPL, name + '.flp'), 'rb').read())[1]


def utf16z(s):
    return (s + '\0').encode('utf-16-le')


def section(t, data):
    return struct.pack('<IQ', t, len(data)) + data


def gain_to_fl_volume(g):
    """Обратное convert.fl_volume_to_gain: множитель громкости -> значение FL."""
    if g <= 0:
        return 0
    db = 20 * math.log10(g)
    pts = FL_VOL_DB
    if db <= pts[0][1]:
        a, b = pts[0], pts[1]
    else:
        a, b = next(((p, q) for p, q in zip(pts, pts[1:]) if db <= q[1]), (pts[-2], pts[-1]))
    k = (b[1] - a[1]) / math.log(b[0] / a[0])
    return int(round(a[0] * math.exp((db - a[1]) / k)))


class FlpBuilder:
    def __init__(self, tempo):
        self.tempo = tempo
        self.wrappers = json.load(open(os.path.join(TPL, 'wrappers.json'), encoding='utf-8'))
        self.known = json.load(open(os.path.join(TPL, 'plugins.json'), encoding='utf-8'))
        items = open(os.path.join(TPL, 'items.bin'), 'rb').read()
        size = len(items) // 3
        self.item_tpl = dict(zip(('pattern', 'audio', 'auto'), (items[i*size:(i+1)*size] for i in range(3))))
        self.ppq = 96
        self.patterns = []      # [(номер, [(тики, длина, нота, сила, канал)])]
        self.channels = []      # готовые блоки событий
        self.connections = []
        self.items = []         # (позиция, байты)
        self.inserts = {}       # номер -> dict(name, effects, gain, pan, route, color)
        self.track_info = {}    # дорожка плейлиста (с нуля) -> (имя, цвет FL или None)
        self.plugin_count = 0
        self.warnings = []
        self.markers = []       # (доля, имя)

    # ---------- плагины ----------
    def wrapper(self, pl, instrument, param_ids):
        """Данные Fruity Wrapper (событие 213) для VST3: имя, GUID, путь, состояние и список параметров."""
        if pl.vst2_id:
            return self.wrapper_vst2(pl, instrument)
        tpl = self.wrappers['gen' if instrument else 'fx']
        known = self.known.get(pl.guid.hex(), {})
        name = known.get('name') or pl.name
        path = known.get('path') or r'C:\Program Files\Common Files\VST3\%s.vst3' % pl.name
        ids = struct.pack('<I', len(param_ids)) + b''.join(struct.pack('<I', i) for i in param_ids)
        state = bytes.fromhex(tpl['state_head']) + section(1, bytes.fromhex(tpl['inner1'])) + section(3, pl.processor)
        if pl.controller:
            state += section(2, pl.controller)
        state += section(4, ids)
        out = bytes.fromhex(tpl['head'])
        for t, d in tpl['sections']:
            out += section(t, bytes.fromhex(d))
        out += (section(52, pl.guid) + section(54, name.encode('utf-8')) + section(55, path.encode('utf-8'))
                + section(56, (known.get('vendor') or pl.vendor).encode('utf-8')) + section(53, state))
        return out

    def eq2_data(self, eq, track, keep=()):
        """EQ Eight / Auto Filter -> данные Parametric EQ 2 (формулы обратные convert.parametric_eq2_bands).
        Полосы без действия (колокол/полка/notch с 0 дБ) пропускаются, если их не автоматизируют (keep):
        у FL их 7, у Live 8. Куда легла каждая полоса — в eq.slot_of."""
        for note in eq.notes:
            self.warnings.append('«%s»: %s — %s' % (track, eq.name, note))
        bands = sorted((i for i, b in enumerate(eq.bands) if b[0] in (0, 1, 6, 7) or abs(b[2]) >= 0.01 or i in keep),
                       key=lambda i: eq.bands[i][1])
        lows = [i for i in bands if eq.bands[i][0] in (0, 1, 2)]
        highs = [i for i in bands if eq.bands[i][0] in (5, 6, 7)]
        mids = [i for i in bands if eq.bands[i][0] in (3, 4)]
        room = 7 - len(lows) - len(highs)
        if len(mids) > room:
            self.warnings.append('«%s»: в EQ Eight больше 7 полос, самые слабые не перенесены' % track)
            keep_mids = sorted(mids, key=lambda i: (i not in keep, -abs(eq.bands[i][2])))[:max(room, 0)]
            mids = [i for i in mids if i in keep_mids]
        slots = [None] * 7
        for k, i in enumerate(lows[:7]):
            slots[k] = i
        for k, i in enumerate(reversed(highs)):
            if 6 - k >= len(lows):
                slots[6 - k] = i
        free = [k for k in range(7) if slots[k] is None]
        for k, i in zip(free, mids):
            slots[k] = i
        if eq.name == 'EQ Eight' and any(eq.bands[i][0] in (0, 1, 6, 7) for i in bands):
            self.warnings.append('«%s»: %s -> Parametric EQ 2, крутизна срезов — как у FL по умолчанию'
                                 % (track, eq.name))
        eq.slot_of = {i: k for k, i in enumerate(slots) if i is not None}

        tpl = dict(load('slot_eq2'))[213]
        v = list(struct.unpack('<36i', tpl[:144]))
        for k, i in enumerate(slots):
            if i is None:
                # пустая полоса — колокол 0 дБ на месте из заготовки
                v[1+k], v[22+k], v[15+k], v[29+k] = 0, 6, 25412, 0
                continue
            kind, hz, gain, q = eq.bands[i]
            v[1+k] = int(round(max(-18.0, min(18.0, gain)) * 100))
            v[8+k] = int(round(eq2_freq(hz) * 65536))
            v[15+k] = int(round(eq2_width(q) * 65535))
            v[22+k] = EQ8_TO_EQ2[kind]
            v[29+k] = 0
        return struct.pack('<36i', *v) + tpl[144:]

    @staticmethod
    def khs_state(gain):
        """Состояние kHs Gain: u32 1, u32 длина, zip со state.json; громкость в нём — множитель."""
        tpl = open(os.path.join(TPL, '..', 'khs_gain.bin'), 'rb').read()
        src = zipfile.ZipFile(io.BytesIO(tpl[8:8 + struct.unpack('<I', tpl[4:8])[0]]))
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, 'w', zipfile.ZIP_DEFLATED) as z:
            for info in src.infolist():
                data = src.read(info)
                if info.filename == 'state.json':
                    data = re.sub(rb'"gain": "[^"]*"', b'"gain": "%s"' % (b'%.6g' % gain), data, 1)
                # дата из заготовки — чтобы одинаковый вход давал одинаковый файл
                zi = zipfile.ZipInfo(info.filename, info.date_time)
                zi.compress_type = zipfile.ZIP_DEFLATED
                z.writestr(zi, data)
        return struct.pack('<II', 1, len(buf.getvalue())) + buf.getvalue()

    def khs_gain(self, u, track, envs):
        """Utility -> kHs Gain, если баланс не нужен; иначе None (тогда Fruity Balance)."""
        if abs(u.balance) > 1e-6 or any(p == 'Balance' for p, _ in envs):
            return None
        gains = [v for p, pts in envs if p == 'Gain' for _, v in pts]
        quiet = 10 ** (-KHS_DB / 20)
        if gains and min(gains) < quiet:
            if max(gains + [u.gain]) <= BALANCE_MAX:
                return None     # уводит в тишину, а kHs тише −30 дБ не умеет — Balance умеет
            self.warnings.append('«%s»: kHs Gain не тише −30 дБ — где Utility уводит громкость в тишину, '
                                 'останется −30 дБ' % track)
        for note in u.notes:
            self.warnings.append('«%s»: Utility — %s' % (track, note))
        db = 20 * math.log10(u.gain) if u.gain > 0 else -KHS_DB
        if db > KHS_DB + 0.05:
            self.warnings.append('«%s»: Utility %+.1f дБ — у kHs Gain максимум +30 дБ, остаток добавлен '
                                 'к громкости канала микшера' % (track, db))
        db = max(-KHS_DB, min(KHS_DB, db))
        pl = alsread.Vst3(name='kHs Gain', vendor='Kilohearts', guid=KHS_GUID, processor=self.khs_state(10 ** (db / 20)),
                          controller=b'', params={})
        return pl, self.param_list(pl, [])

    @staticmethod
    def khs_value(g):
        """Множитель громкости Utility -> 0..1 параметра Gain у kHs Gain."""
        if g <= 0:
            return 0.0
        return max(0.0, min(1.0, 0.5 + 20 * math.log10(g) / (2 * KHS_DB)))

    def balance_data(self, u, track):
        """Utility -> данные Fruity Balance: баланс и громкость (int32)."""
        for note in u.notes:
            self.warnings.append('«%s»: Utility — %s' % (track, note))
        if u.gain > BALANCE_MAX + 1e-6:
            self.warnings.append('«%s»: Utility %+.1f дБ — у Fruity Balance максимум %+.1f дБ, остаток добавлен '
                                 'к громкости канала микшера' % (track, 20 * math.log10(u.gain),
                                                                  20 * math.log10(BALANCE_MAX)))
        if u.balance:
            self.warnings.append('«%s»: баланс Utility перенесён по догадке о шкале Fruity Balance — проверь' % track)
        vol = int(round(min(u.gain, BALANCE_MAX) * 256))
        return struct.pack('<ii', int(round(max(-1, min(1, u.balance)) * 128)), vol)

    def effect(self, pl, track, wanted, envs=()):
        """Запись слота микшера: (плагин, данные Parametric EQ 2 / Fruity Balance) или (VST3, список параметров).
        Utility становится kHs Gain (VST3), а если нужен баланс или тишина — Fruity Balance."""
        if isinstance(pl, alsread.Eq8):
            return pl, self.eq2_data(pl, track, {pid[0] for pid in wanted})
        if isinstance(pl, alsread.Utility):
            return self.khs_gain(pl, track, envs) or (pl, self.balance_data(pl, track))
        return pl, self.param_list(pl, wanted)

    @staticmethod
    def param_index(pl, extra, pid):
        """Номер параметра для цели автоматизации FL: у Balance 0 — баланс, 1 — громкость;
        у VST3 — место в списке параметров."""
        if isinstance(pl, alsread.Utility):
            return 1 if pid == 'Gain' else 0
        if isinstance(pl, alsread.Eq8):
            # Parametric EQ 2: номер параметра = место в данных — 7 усилений, 7 частот, 7 ширин
            # (усиления и частоты сходятся с DawVert, ширины не проверены)
            k = pl.slot_of.get(pid[0])
            return None if k is None else {'Gain': 0, 'Freq': 7, 'Q': 14}[pid[1]] + k
        if isinstance(pl, alsread.Vst3) and pl.vst2_id:
            return pid
        if isinstance(pl, alsread.Vst3) and pl.guid == KHS_GUID and pid == 'Gain':
            pid = 1
        return extra.index(pid) if isinstance(pl, alsread.Vst3) and pid in extra else None

    def param_list(self, pl, wanted):
        """Список id параметров плагина в порядке FL: из справочника, а для незнакомого плагина —
        только автоматизируемые (по нему FL находит параметр для автоматизации)."""
        if pl.vst2_id:
            return []
        known = self.known.get(pl.guid.hex())
        if known and known['params']:
            raw = bytes.fromhex(known['params'])
            n = struct.unpack('<I', raw[:4])[0]
            ids = list(struct.unpack('<%dI' % n, raw[4:4+4*n]))
            missing = [p for p in wanted if p not in ids]
            return ids + missing
        if wanted:
            self.warnings.append('«%s»: плагина нет в справочнике, список параметров только из автоматизации — '
                                 'проверь, что автоматизация в FL идёт на нужный параметр' % pl.name)
        return list(wanted)

    def wrapper_vst2(self, pl, instrument):
        """Fruity Wrapper для VST2 (образцы — KANINE/DOPAMINE): 51 — UniqueId, 57 — версия VST,
        30 — число входов и выходов, 31/32 — по 12 байт на вход/выход, 50 = 0 (VST2),
        53 — f7ffffff 0d feffffff, размер, 8 нулей и чанк плагина; у плагина без чанка (MasterMagic) —
        f7ffffff 05 feffffff, 12 нулей, число параметров, float на каждый и хвост с именем программы."""
        tpl = self.wrappers['gen' if instrument else 'fx']
        sec = dict(tpl['sections'])
        bus = struct.pack('<3I', 0, 1, 0)
        ins = 0 if instrument else max(1, pl.ins)
        if pl.values:
            state = (b'\xf7\xff\xff\xff\x05\xfe\xff\xff\xff' + bytes(12) + struct.pack('<I', len(pl.values))
                     + struct.pack('<%df' % len(pl.values), *pl.values) + VST2_PARAMS_TAIL)
        else:
            state = b'\xf7\xff\xff\xff\x0d\xfe\xff\xff\xff' + struct.pack('<I', len(pl.chunk)) + bytes(8) + pl.chunk
        out = bytes.fromhex(tpl['head'])
        out += section(1, bytes.fromhex(sec[1])) + section(2, bytes.fromhex(sec[2]))
        out += section(30, struct.pack('<4I', ins, max(1, pl.outs), 0, 0))
        if ins:
            out += section(31, bus * ins)
        out += section(32, bus * max(1, pl.outs)) + section(50, bytes(16))
        out += (section(51, struct.pack('<I', pl.vst2_id)) + section(57, struct.pack('<I', pl.vst_version))
                + section(54, pl.name.encode('utf-8')) + section(55, pl.path.encode('utf-8'))
                + section(56, pl.vendor.encode('utf-8')) + section(53, state))
        return out

    # ---------- каналы ----------
    def add_channel(self, kind, name, insert=0, plugin=None, param_ids=(), sample_path=None,
                    stretch=0.0, automation=None, color=None, start=0.0, end=1.0):
        if not self.channels and name != PLACEHOLDER:
            # канал 0 (аудиоклипы, VST) FL играет мусором (NaN глушит мастер); у FL на месте 0 всегда
            # сэмплер — ставим пустой
            self.add_channel('sampler', PLACEHOLDER)
        idx = len(self.channels)
        block = []
        for e, v in load('chan_' + kind):
            if e == 64:
                v = idx
            elif e == 128 and color is not None:
                v = color
            elif e == 203:
                v = utf16z(name)
            elif e == 104:
                v = insert
            elif e == 213 and plugin is not None:
                v = self.wrapper(plugin, True, param_ids)
            elif e == 132 and kind == 'plugin':
                self.plugin_count += 1
                v = (self.plugin_count << 16) | self.plugin_count
            elif e == 215 and kind == 'audio':
                v = v[:96] + struct.pack('<f', stretch) + v[100:]
                if len(v) >= 144 and (start > 0 or end < 1):
                    # начало — доля файла, длина — доля остатка после начала
                    length = (end - start) / (1 - start) if start < 1 else 1.0
                    v = v[:128] + struct.pack('<dd', start, max(0.0, min(1.0, length))) + v[144:]
            elif e == 196:
                v = utf16z(sample_path)
            elif e == 234 and automation is not None:
                v = self.automation_data(v, automation)
            block.append((e, v))
        self.channels.append(block)
        return idx

    @staticmethod
    def automation_data(tpl, points):
        """Клип автоматизации (событие 234): точки — шаг от предыдущей в долях, значение 0..1."""
        n = struct.unpack('<I', tpl[17:21])[0]
        head, tail = tpl[:17], tpl[21 + 24 * n:]
        out = head + struct.pack('<I', len(points))
        prev = None
        for t, v in points:
            # у первой точки последние 4 байта нулевые, у остальных — 0x02 (как пишет FL)
            out += struct.pack('<ddf', t if prev is None else t - prev, max(0.0, min(1.0, v)), 0.0)
            out += b'\0\0\0\0' if prev is None else b'\0\0\0\x02'
            prev = t
        return out + tail

    # ---------- паттерны и плейлист ----------
    def add_pattern(self, notes, chan):
        """notes: [(доля, длина, нота, сила[, канал])] — без канала нота идёт в chan."""
        num = len(self.patterns) + 1
        self.patterns.append((num, [(int(round(n[0] * self.ppq)), max(1, int(round(n[1] * self.ppq))), n[2], n[3],
                                     n[4] if len(n) > 4 else chan) for n in notes]))
        return num

    def add_item(self, kind, pos, length, track, ref, start=None, end=None):
        it = bytearray(self.item_tpl[kind])
        ref_code = PATTERN_BASE + ref if kind == 'pattern' else ref
        struct.pack_into('<IHHI', it, 0, int(round(pos * self.ppq)), PATTERN_BASE, ref_code,
                         max(1, int(round(length * self.ppq))))
        struct.pack_into('<I', it, 12, TRACKS - 1 - track)
        if kind == 'pattern':
            struct.pack_into('<ii', it, 24, 0, max(1, int(round(length * self.ppq))))
        else:
            struct.pack_into('<ff', it, 24, start, end)
        struct.pack_into('<I', it, 32, len(self.items) + 1)
        self.items.append((pos, bytes(it)))

    # ---------- запись ----------
    def pattern_events(self):
        ev = []
        for num, notes in self.patterns:
            data = b''
            for pos, ln, key, vel, chan in sorted(notes):
                data += struct.pack('<IHHIHH', pos, 0x4000, chan, ln, key, 0)
                data += bytes((0x78, 0, 0x40, 0, 0x40, max(1, min(127, vel)), 0x80, 0x80))
            ev += [(65, num), (224, data)]
        return ev

    def mixer_events(self, count):
        ev = []
        slot_tpl = load('slot')
        for n in range(count):
            ins = self.inserts.get(n, {})
            if ins.get('name'):
                color = ins.get('color')
                ev += [(149, NAMED_COLOR if color is None else color), (42, 1), (204, utf16z(ins['name']))]
            else:
                ev.append((42, 0))
            # последний канал у FL служебный («Current»): как мастер, но без выхода на звуковую карту
            current = n == count - 1
            tpl = load('insert_master' if n == 0 or current else 'insert')
            ev.append(tpl[0])
            effects = ins.get('effects', [])
            for s in range(SLOTS):
                if s < len(effects):
                    pl, extra = effects[s]
                    if isinstance(pl, alsread.Eq8):
                        ev += [(e, extra if e == 213 else v) for e, v in load('slot_eq2')]
                    elif isinstance(pl, alsread.Utility):
                        ev += [(e, extra if e == 213 else v) for e, v in load('slot_balance')]
                    else:
                        ev += [(e, self.wrapper(pl, False, extra) if e == 213 else v) for e, v in slot_tpl]
                ev.append((98, s))
            for e, v in tpl[1:]:
                if e == 235:
                    # байт на каждый канал-получатель; у мастера и служебного пусто
                    v = b'\0' if n == 0 or current else bytes(ins.get('route', 0)) + b'\1'
                elif e == 147 and current:
                    v = 0xffffffff
                ev.append((e, v))
        return ev

    def mixer_params(self, data, count):
        """Параметры микшера (событие 225) на все каналы: записи по 12 байт (0, параметр, 31, код, значение),
        код = 0x7000 + (канал << 6) + слот. Как у FL: общая запись, каналы 0..count-2 и служебный (501).
        Набор записей канала берём у первого канала заготовки."""
        recs = [data[j:j+12] for j in range(0, len(data) - 11, 12)]
        head = [r for r in recs if struct.unpack('<H', r[6:8])[0] < 0x7000]
        base = 0x7000 + (1 << 6)
        one = [r for r in recs if base <= struct.unpack('<H', r[6:8])[0] < base + 64]
        out = list(head)
        for n in list(range(count - 1)) + [CURRENT_INSERT]:
            ins = self.inserts.get(n, {})
            vol = gain_to_fl_volume(ins.get('gain', 1.0))
            if vol > FL_VOL_MAX:
                self.warnings.append('канал микшера %d «%s»: громче +5,6 дБ фейдер FL не поднимается — выставлен максимум'
                                     % (n, ins.get('name', '')))
            values = {192: min(vol, FL_VOL_MAX), 193: int(round(ins.get('pan', 0) * 6400))}
            for r in one:
                _, pid, kind, cd, value = struct.unpack('<IBBHi', r)
                if cd == base and pid in values:
                    value = values[pid]
                out.append(struct.pack('<IBBHi', 0, pid, kind, cd - base + 0x7000 + (n << 6), value))
        return b''.join(out)

    def marker_events(self):
        # набор событий маркера как у FL 26 (образец — KANINE.flp)
        ev = []
        for t, name in sorted(self.markers):
            ev += [(148, int(round(t * self.ppq))), (33, 4), (34, 4), (46, 0), (45, 0), (168, 0), (101, 0),
                   (205, utf16z(name))]
        return ev

    def track_events(self, e, v, pending):
        """Дорожка плейлиста: 238 (номер с 1, цвет RGB в байтах 4..8), 43 (1 — свой цвет), 239 (имя)."""
        if e == 238:
            pending[:] = []
            name, color = self.track_info.get(struct.unpack('<I', v[:4])[0] - 1, ('', None))
            if color is not None:
                v = v[:4] + struct.pack('<I', color) + v[8:]
            if color is not None or name:
                pending[:] = [color is not None, name]
            return [(e, v)]
        if e == 43 and pending:
            out = [(e, 1 if pending[0] else v)]
            if pending[1]:
                out.append((239, utf16z(pending[1])))
            pending[:] = []
            return out
        return [(e, v)]

    def write(self, path, version='26'):
        count = max(MIN_INSERTS, max(self.inserts, default=0) + 2)
        if version == '25':
            # FL 25 всегда сохраняет все 127 каналов микшера, FL 26 — только до последнего занятого
            count = max(count, 127)
        if count > 127:
            self.warnings.append('дорожек больше, чем каналов в микшере FL (125) — лишние могут не открыться')
        ev = []
        for e, v in load('head'):
            ev.append((e, int(round(self.tempo * 1000)) if e == 156 else v))
        ev += self.pattern_events()
        ev += self.connections
        for block in self.channels:
            ev += block
        pending = []
        for e, v in load('arrange'):
            if e == 233:
                v = b''.join(b for _, b in sorted(self.items, key=lambda x: x[0]))
            elif e == 103:
                v = count
            ev += self.track_events(e, v, pending)
            if e == 233:
                ev += self.marker_events()
        ev += self.mixer_events(count)
        for e, v in load('tail'):
            ev.append((e, self.mixer_params(v, count) if e == 225 else v))
        if version == '25':
            ev = to_fl25(ev)
        with open(path, 'wb') as f:
            f.write(write_flp(ev, self.ppq, len(self.channels)))


def to_fl25(ev):
    """Проект FL 26 -> FL 25.2.5 (сверено с KANINE.flp и DOPAMINE_HIT_REMAKE.flp из 25.2.5):
    другая строка версии и флаги 169, а элемент плейлиста на 8 байт короче — в 26 добавлено ffffffff 00000000
    на месте 76..84. Пары 172=256, 0=0 после 195 в FL 25 нет — с ней FL 25 считает проект повреждённым."""
    ver, build = FL_VERSIONS['25']
    out = []
    for i, (e, v) in enumerate(ev):
        if (e == 172 and ev[i-1][0] == 195) or (e == 0 and i > 1 and ev[i-1][0] == 172 and ev[i-2][0] == 195):
            continue
        if e == 199:
            v = ver.encode('ascii') + b'\0'
        elif e == 159:
            v = build
        elif e == 169:
            v = 7
        elif e == 192:
            v = utf16z('FL Studio %s.%d' % (ver, build))
        elif e == 233:
            v = b''.join(v[j:j+76] + v[j+84:j+88] for j in range(0, len(v), 88))
        out.append((e, v))
    return out


def serum1_fxp(chunk, name):
    """Пресет Serum 1 (.fxp, FPCh): заголовок CcnK, id XfsX, имя до 28 байт и чанк как его отдаёт плагин."""
    body = (b'FPCh' + struct.pack('>I', 1) + b'XfsX' + struct.pack('>II', 1, 1)
            + name.encode('latin-1', 'replace')[:27].ljust(28, b'\0') + struct.pack('>I', len(chunk)) + chunk)
    return b'CcnK' + struct.pack('>I', len(body)) + body


def juce_vst3_state(pl):
    """Состояние процессора JUCE-VST3 из чанка VST2 (образец — Vital в ref): 'VstW' + 8, 1, 0 (BE),
    банк CcnK/FBCh с id VST2 и чанком, в конце чанка 16 нулей и 'JUCEPrivateData'."""
    chunk = pl.chunk + bytes(16) + b'JUCEPrivateData'
    bank = (b'FBCh' + struct.pack('>I', 2) + struct.pack('>I', pl.vst2_id) + struct.pack('>II', pl.plugin_version, 0)
            + bytes(128) + struct.pack('>I', len(chunk)) + chunk)
    return b'VstW' + struct.pack('>III', 8, 1, 0) + b'CcnK' + struct.pack('>I', len(bank)) + bank


def as_vst3(b, pl, where, warn, automated):
    """VST2 из VST2_AS_VST3 -> такой же плагин VST3; остальные как есть."""
    guid = VST2_AS_VST3.get(pl.vst2_id) if isinstance(pl, alsread.Vst3) else None
    if guid is None:
        return pl
    known = b.known.get(guid, {})
    if automated:
        warn.append('«%s»: автоматизация %s (VST2) не перенесена — у VST3 другие номера параметров'
                    % (where, pl.name))
    return alsread.Vst3(name=known.get('name', pl.name), vendor=known.get('vendor', ''), guid=bytes.fromhex(guid),
                        instrument=pl.instrument, processor=juce_vst3_state(pl))


def serum2_state(fxp):
    """Состояние Serum 2 из .fxp Serum 1 через serum1to2.py: (байты, None) или (None, причина)."""
    if not (os.path.isfile(S1_PYTHON) and os.path.isdir(S1_TOOL)):
        return None, 'переводчик Serum 1 не установлен'
    out = fxp + '.state'
    script = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'serum1to2.py')
    try:
        r = subprocess.run([S1_PYTHON, '-I', script, S1_TOOL, fxp, out], stdout=subprocess.PIPE,
                           stderr=subprocess.PIPE, timeout=120)
    except (OSError, subprocess.TimeoutExpired) as e:
        return None, str(e)
    if r.returncode or not os.path.isfile(out):
        lines = r.stderr.decode('utf-8', 'replace').strip().splitlines()
        return None, lines[-1] if lines else 'код %d' % r.returncode
    with open(out, 'rb') as f:
        data = f.read()
    os.remove(out)
    return data, None


def put_effects(b, tr, insert, autos):
    """Эффекты дорожки -> слоты канала микшера; их автоматизация и автоматизация громкости/панорамы
    дорожки -> клипы автоматизации на этот канал."""
    where = tr.name or 'мастер'
    ins = b.inserts[insert]
    wanted, envs = {}, {}
    for pl, pid, _, pts in tr.envelopes:
        wanted.setdefault(id(pl), []).append(pid)
        envs.setdefault(id(pl), []).append((pid, pts))
    effects = []
    for pl in tr.effects:
        # Utility на 0 дБ без автоматизации ничего не делает — в FL не ставим
        if (isinstance(pl, alsread.Utility) and abs(pl.gain - 1) < 1e-4 and abs(pl.balance) < 1e-6
                and id(pl) not in envs):
            for note in pl.notes:
                b.warnings.append('«%s»: Utility — %s' % (where, note))
            continue
        # автоматизация заменённого плагина отвалится ниже с предупреждением «не перенесена»
        effects.append(as_vst3(b, pl, where, b.warnings, False))
    if len(effects) > SLOTS:
        b.warnings.append('«%s»: больше %d эффектов, лишние не перенесены' % (where, SLOTS))
    effects = effects[:SLOTS]
    for pl in effects:
        slot = b.effect(pl, where, wanted.get(id(pl), []), envs.get(id(pl), []))
        ins['effects'].append(slot)
        if isinstance(pl, alsread.Utility):
            # громче, чем может замена, — остаток на фейдер канала
            top = BALANCE_MAX if isinstance(slot[0], alsread.Utility) else 10 ** (KHS_DB / 20)
            if pl.gain > top:
                ins['gain'] *= pl.gain / top
    slot_of = {id(pl): i for i, pl in enumerate(effects)}
    for pl, pid, pname, pts in tr.envelopes:
        if pl is tr.instrument:
            continue
        slot = slot_of.get(id(pl))
        if slot is None:
            b.warnings.append('«%s»: автоматизация %s не перенесена' % (where, pl.name))
            continue
        dev, extra = ins['effects'][slot]
        index = b.param_index(dev, extra, pid)
        if index is None:
            b.warnings.append('«%s»: автоматизация %s не перенесена' % (where, pl.name))
            continue
        if isinstance(pl, alsread.Utility):
            # значения Utility: Gain — множитель, Balance −1..1
            if pid == 'Balance':
                pts = [(t, (v + 1) / 2) for t, v in pts]
            elif isinstance(dev, alsread.Utility):
                # громкость Fruity Balance 0..320, 256 = 0 дБ (0,8 шкалы); громче +1,9 дБ некуда
                pts = [(t, max(0.0, min(1.0, v * 0.8))) for t, v in pts]
            else:
                pts = [(t, b.khs_value(v)) for t, v in pts]
            pname = 'Volume' if pid == 'Gain' else 'Balance'
        elif isinstance(pl, alsread.Eq8):
            pts = [(t, eq2_value(pid[1], v)) for t, v in pts]
        # эффект в микшере: ((0x7000 + канал << 6 + слот) << 16) | 0x8000 | параметр
        code = ((0x7000 + (insert << 6) + slot) << 16) | 0x8000 | index
        label = ('Balance' if isinstance(dev, alsread.Utility) else
                 'Parametric EQ 2' if isinstance(dev, alsread.Eq8) else dev.name)
        autos.append((code, '%s - %s - %s' % (label, where, pname or pid), pts))
    for what, pts in tr.mixer_envelopes:
        if what == 'Volume':
            # фейдер FL 0..1, 0,8 = 0 дБ
            norm = [(t, min(1.0, gain_to_fl_volume(v) / 16000)) for t, v in pts]
        else:
            norm = [(t, (v + 1) / 2) for t, v in pts]
        code = ((0x7000 + (insert << 6)) << 16) | (0x1fc0 if what == 'Volume' else 0x1fc1)
        autos.append((code, '%s - %s' % (where, what), norm))


def convert(src, out_dir, work_dir=None, move_samples=False, remove_src=False, version='26'):
    """src: .als, папка проекта Live или zip с ней; version — '26' или '25' (FL 25.2.5).
    Возвращает (путь .flp, предупреждения)."""
    als_path, search_root = find_als(src, work_dir, remove_src)
    pr = alsread.parse(open(als_path, 'rb').read())
    b = FlpBuilder(pr.tempo)
    b.markers = pr.locators
    warn = b.warnings
    warn += pr.warnings
    name = os.path.splitext(os.path.basename(als_path))[0]
    proj_dir = os.path.join(out_dir, name)
    sample_dir = os.path.join(proj_dir, 'Samples')
    placed = {}  # (path, rel_path) из .als -> файл в Samples
    os.makedirs(sample_dir, exist_ok=True)

    used_fxp = set()
    def serum1_file(track):
        """Имя .fxp по имени дорожки: без запрещённых в Windows знаков, без повторов."""
        os.makedirs(os.path.join(proj_dir, SERUM1_DIR), exist_ok=True)
        base = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '_', track).strip(' .') or 'Serum'
        name, k = base, 1
        while name.lower() in used_fxp:
            k += 1
            name = '%s (%d)' % (base, k)
        used_fxp.add(name.lower())
        return name + '.fxp'

    autos = []          # (код цели FL, имя клипа, огибающая 0..1)
    # группа Ableton = канал микшера, куда идут только каналы её дорожек
    group_insert = {tr.id: i + 1 for i, tr in enumerate(pr.tracks) if tr.kind == 'group'}
    row = -1            # дорожка плейлиста; у групп своей нет
    next_insert = [len(pr.tracks) + 1]  # каналы микшера цепочек и пэдов — после дорожек
    bus_insert = {}     # id(alsread.Bus) -> канал микшера
    def place(sample, where):
        """Сэмпл из .als -> файл в Samples; возвращает имя файла или None."""
        key = (sample['path'], sample['rel_path'])
        # при move_samples исходник уже перенесён первым клипом — берём его из Samples
        src_file = placed.get(key) \
            or find_file(sample['path'], [os.path.dirname(als_path), search_root]) \
            or find_file(sample['rel_path'], [os.path.dirname(als_path), search_root])
        base = os.path.basename(sample['rel_path'] or sample['path'].replace('\\', '/'))
        if not src_file:
            warn.append('«%s»: не найден сэмпл %s' % (where, base))
            return None
        dst = os.path.join(sample_dir, base)
        if os.path.abspath(src_file) != os.path.abspath(dst) and not os.path.exists(dst):
            (shutil.move if move_samples else shutil.copy2)(src_file, dst)
        placed[key] = dst
        return base

    def skipped(where, names):
        off = [n[:-len(' (выключено)')] for n in names if n.endswith(' (выключено)')]
        if off:
            warn.append('%s: выключенные устройства не переносятся (%s)' % (where, ', '.join(off)))
        for n in dict.fromkeys(n for n in names if not n.endswith(' (выключено)')):
            warn.append('%s: %s не переносится' % (where, n))

    def instrument(pl, name, to, envelopes, color):
        """Инструмент-плагин -> канал FL (Serum 1 -> Serum 2, VST2 на JUCE -> VST3) и его автоматизация."""
        automated = any(e[0] is pl for e in envelopes)
        if pl.vst2_id == SERUM1_ID or pl.guid == SERUM1_VST3_GUID:
            fname = serum1_file(name)
            fxp = os.path.join(proj_dir, SERUM1_DIR, fname)
            with open(fxp, 'wb') as f:
                f.write(serum1_fxp(pl.chunk or pl.processor, name))
            state, why = serum2_state(fxp)
            if state:
                warn.append('«%s»: Serum 1 переведён в Serum 2 автоматически — проверь на слух '
                            '(исходник: %s\\%s)' % (name, SERUM1_DIR, fname))
            else:
                warn.append('«%s»: Serum 1 заменён на Serum 2 — загрузи в него %s\\%s (Load preset); '
                            'автоперевод не вышел: %s' % (name, SERUM1_DIR, fname, why))
            if automated:
                warn.append('«%s»: автоматизация Serum 1 не перенесена (у Serum 2 другие параметры)' % name)
            pl = alsread.Vst3(name='Serum 2', vendor='Xfer Records', guid=SERUM2_GUID, instrument=True,
                              processor=state or b'')
        if pl.vst2_id in VST2_AS_VST3:
            pl = as_vst3(b, pl, name, warn, automated)
        ids = b.param_list(pl, [pid for pl2, pid, _, _ in envelopes if pl2 is pl])
        ch = b.add_channel('plugin', name, to, plugin=pl, param_ids=ids, color=color)
        for pl2, pid, pname, pts in envelopes:
            if pl2 is pl:
                # инструмент: (канал << 16) | 0x8000 | параметр
                index = pid if pl.vst2_id else ids.index(pid)
                autos.append(((ch << 16) | 0x8000 | index, '%s - %s' % (pl.name, pname or pid), pts))
        return ch

    for ti, tr in enumerate(pr.tracks):
        insert = ti + 1
        color = live_to_fl(tr.color)
        b.inserts[insert] = dict(name=tr.name, gain=tr.gain, pan=tr.pan, effects=[],
                                 route=group_insert.get(tr.group, 0), color=color)
        skipped('«%s»' % tr.name, tr.skipped)
        put_effects(b, tr, insert, autos)
        if tr.kind == 'group':
            continue
        row += 1
        b.track_info[row] = (tr.name, color)
        if tr.kind == 'midi':
            if tr.samplers:
                # Simpler, слои Instrument Rack, пэды Drum Rack -> каналы FL; у FL сэмпл звучит без сдвига на ноте 60.
                # Цепочка или пэд со своими эффектами/громкостью — свой канал микшера, он идёт в следующий
                chans = []
                for sm in tr.samplers:
                    to = insert
                    for bus in reversed(sm.chain):
                        if not bus.needed():
                            continue
                        if id(bus) not in bus_insert:
                            where = '%s / %s' % (tr.name, bus.name)
                            bus_insert[id(bus)] = n = next_insert[0]
                            next_insert[0] += 1
                            b.inserts[n] = dict(name=where, gain=bus.gain, pan=bus.pan, effects=[], route=to,
                                                color=color)
                            put_effects(b, alsread.Track(name=where, effects=bus.effects, envelopes=bus.envelopes),
                                        n, autos)
                        to = bus_insert[id(bus)]
                    label = sm.name or (sm.chain[0].name if sm.chain else tr.name)
                    if sm.plugin is not None:
                        ch = instrument(sm.plugin, label, to, sm.envelopes, color)
                        if ch is not None:
                            chans.append((sm, ch))
                        continue
                    base = place(sm.sample, tr.name)
                    if base is None:
                        continue
                    chans.append((sm, b.add_channel('audio', sm.name or os.path.splitext(base)[0], to,
                                                    sample_path='Samples\\' + base, color=color,
                                                    start=sm.start, end=sm.end)))
                missed = 0
                for c in tr.clips:
                    notes = []
                    for p, d, k, v in c.notes:
                        k += tr.transpose
                        hit = [(sm, ch) for sm, ch in chans if sm.key is None or sm.key == k]
                        missed += not hit
                        for sm, ch in hit:
                            if sm.plugin is not None:
                                played = (k if sm.key is None else sm.send_note) + sm.transpose
                            else:
                                played = (k if sm.key is None else 60) - sm.root + 60
                            notes.append((p, d, max(0, min(127, played)), v, ch))
                    if chans:
                        b.add_item('pattern', c.time, c.length, row, b.add_pattern(notes, chans[0][1]))
                if missed:
                    warn.append('«%s»: %d нот на пустые пэды Drum Rack пропущены' % (tr.name, missed))
                continue
            ch = None
            if tr.instrument is not None:
                ch = instrument(tr.instrument, tr.name, insert, tr.envelopes, color)
            if ch is None:
                warn.append('«%s»: нет VST3-инструмента — ноты на пустом сэмплере' % tr.name)
                ch = b.add_channel('sampler', tr.name, insert, color=color)
            for c in tr.clips:
                notes = [(p, d, k + tr.transpose, v) for p, d, k, v in c.notes]
                num = b.add_pattern(notes, ch)
                b.add_item('pattern', c.time, c.length, row, num)
        else:
            channels = {}
            for c in tr.clips:
                base = place(c.sample, tr.name)
                if base is None:
                    continue
                if base not in channels:
                    stretch = c.sample_beats * 192 if c.warped and c.sample_beats else 0.0
                    channels[base] = b.add_channel('audio', os.path.splitext(base)[0], insert,
                                                   sample_path='Samples\\' + base, stretch=stretch, color=color)
                # смещение и длина клипа в миллисекундах сэмпла
                sec_per_beat = (c.sample_sec / c.sample_beats if c.warped and c.sample_beats
                                else 60.0 / pr.tempo)
                start = c.offset * sec_per_beat * 1000
                b.add_item('audio', c.time, c.length, row, channels[base], start, start + c.length * sec_per_beat * 1000)

    main = pr.main
    b.inserts[0] = dict(name='', gain=main.gain, pan=main.pan, effects=[])
    skipped('«мастер»', main.skipped)
    put_effects(b, main, 0, autos)

    conn_tpl = load('conn')[0][1]
    for k, (target, label, pts) in enumerate(autos):
        t0, t1 = pts[0][0], pts[-1][0]
        length = max(t1 - t0, 0.25)
        rel = [(t - t0, v) for t, v in pts] if len(pts) > 1 else [(0.0, pts[0][1]), (length, pts[0][1])]
        auto = b.add_channel('auto', label, automation=rel)
        conn = bytearray(conn_tpl)
        struct.pack_into('<H', conn, 2, auto)
        struct.pack_into('<I', conn, 8, target)
        b.connections.append((227, bytes(conn)))
        b.add_item('auto', t0, length, row + 1 + k, auto, 0.0, length)

    out = os.path.join(proj_dir, name + '.flp')
    b.write(out, version)
    warn[:] = compact_warnings(warn)
    return out, warn


def find_als(src, work_dir=None, remove_src=False):
    """Принимает .als, папку проекта Live или zip (File -> Collect All and Save, затем заархивировать)."""
    if src.lower().endswith('.als'):
        return src, os.path.dirname(os.path.abspath(src))
    if src.lower().endswith('.zip'):
        tmp = tempfile.mkdtemp(prefix='als2fl-', dir=work_dir)
        extract_zip(src, tmp)
        if remove_src:
            os.remove(src)
        src = tmp
    found = [os.path.join(dp, f) for dp, _, fs in os.walk(src) for f in fs
             if f.lower().endswith('.als') and 'Backup' not in dp]
    if len(found) != 1:
        raise SystemExit('в папке должен быть ровно один .als (не считая Backup), нашлось: %d' % len(found))
    return found[0], src
