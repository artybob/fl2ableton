"""FL Studio (.flp) -> папка проекта Ableton Live 12."""
import math, os, re, shutil, struct, zipfile, tempfile
from . import flp
from .als import AlsBuilder
from .colors import fl_to_live

# Громкость канала микшера FL -> дБ. Точки сняты с настоящих проектов, между ними — по логарифму значения.
# TODO: добрать точек (−10, −20 дБ), пока середина шкалы приблизительная
FL_VOL_DB = [(10092, -5.0), (12800, 0.0), (16000, 5.6)]


# Шкала параметров плагинов в Ableton (FL хранит 0..1). Сняты с настоящих проектов Live.
# Неизвестные пишутся как 0..1 с предупреждением.
PARAM_RANGES = {('Serum2', 7000000): (0, 100), ('OTT', 0): (0, 100)}
# Параметры в децибелах: Ableton показывает их множителем, шкала FL линейна по дБ (мин, макс).
# kHs Gain: в Live 0,0316..31,6 — сверено с ref5 (0 дБ -> −30 дБ = 1 -> 0,0316)
PARAM_DB = {('kHs Gain', 1): (-30.0, 30.0)}
NAMED_COLOR = 6972764   # цвет, который FL ставит переименованному каналу микшера сам
BALANCE_MAX = 1.25      # громкость Fruity Balance: 0..320, 256 = 0 дБ (0,8 шкалы)


def compact_warnings(warn):
    """Одинаковые потери на разных дорожках — одной строкой: «текст — «A», «B», …»."""
    groups = {}
    for w in warn:
        m = re.match(r'(«.*?»|мастер): (.*)$', w, re.S)
        where, text = (m.group(1), m.group(2)) if m else (None, w)
        groups.setdefault(text, []).append(where)
    out = []
    for text, wheres in groups.items():
        names = list(dict.fromkeys(x for x in wheres if x))
        if not names:
            out.append(text)
        elif len(names) == 1:
            out.append('%s: %s' % (names[0], text))
        else:
            more = ' и ещё %d' % (len(names) - 6) if len(names) > 6 else ''
            out.append('%s — %s%s' % (text, ', '.join(names[:6]), more))
    return out


def fl_volume_to_gain(v):
    if v <= 0:
        return 0.0
    pts = FL_VOL_DB
    if v <= pts[0][0]:
        a, b = pts[0], pts[1]
    else:
        a, b = next(((p, q) for p, q in zip(pts, pts[1:]) if v <= q[0]), (pts[-2], pts[-1]))
    k = (b[1] - a[1]) / math.log(b[0] / a[0])
    db = a[1] + k * math.log(v / a[0])
    return 10 ** (db / 20)


def wav_info(path):
    """(кадров, частота) по заголовку; сам звук не читаем — файлы бывают по сотне мегабайт."""
    size = os.path.getsize(path)
    rate = block = frames = None
    with open(path, 'rb') as f:
        p = 12
        while p + 8 <= size:
            f.seek(p)
            cid, n = f.read(4), struct.unpack('<I', f.read(4))[0]
            if cid == b'fmt ':
                _, chans, rate, _, block = struct.unpack('<HHIIH', f.read(14))
            elif cid == b'data' and block:
                frames = min(n, size - p - 8) // block
            p += 8 + n + (n & 1)
    return frames, rate


def aiff_info(path):
    with open(path, 'rb') as f:
        head = f.read(12)
        if head[:4] != b'FORM' or head[8:12] not in (b'AIFF', b'AIFC'):
            return None, None
        while True:
            h = f.read(8)
            if len(h) < 8:
                return None, None
            cid, n = h[:4], struct.unpack('>I', h[4:])[0]
            if cid == b'COMM':
                c = f.read(18)
                frames = struct.unpack('>I', c[2:6])[0]
                # частота — 80-битное число с плавающей точкой
                exp = struct.unpack('>H', c[8:10])[0] & 0x7fff
                mant = struct.unpack('>Q', c[10:18])[0]
                return frames, int(round(mant * 2.0 ** (exp - 16383 - 63)))
            f.seek(n + (n & 1), 1)


def flac_info(path):
    with open(path, 'rb') as f:
        if f.read(4) != b'fLaC':
            return None, None
        f.read(4)  # заголовок блока STREAMINFO, он всегда первый
        si = f.read(18)
    x = int.from_bytes(si[10:18], 'big')
    return x & 0xfffffffff, x >> 44


MP3_RATES = {3: (44100, 48000, 32000), 2: (22050, 24000, 16000), 0: (11025, 12000, 8000)}
MP3_KBPS = {  # (MPEG-1, MPEG-2/2.5) для Layer III
    True: (0, 32, 40, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320),
    False: (0, 8, 16, 24, 32, 40, 48, 56, 64, 80, 96, 112, 128, 144, 160),
}


def mp3_info(path):
    """Число сэмплов берём из заголовка Xing/Info, а без него — пробегом по заголовкам кадров."""
    size = os.path.getsize(path)
    with open(path, 'rb') as f:
        data = f.read(min(size, 1 << 16))
        p = 0
        if data[:3] == b'ID3':
            p = 10 + ((data[6] << 21) | (data[7] << 14) | (data[8] << 7) | data[9])

        def header(buf, i):
            if i + 4 > len(buf) or buf[i] != 0xff or (buf[i+1] & 0xe0) != 0xe0:
                return None
            ver, layer = (buf[i+1] >> 3) & 3, (buf[i+1] >> 1) & 3
            br, sr, pad = buf[i+2] >> 4, (buf[i+2] >> 2) & 3, (buf[i+2] >> 1) & 1
            if ver == 1 or layer != 1 or br in (0, 15) or sr == 3:
                return None
            mpeg1 = ver == 3
            rate = MP3_RATES[ver][sr]
            spf = 1152 if mpeg1 else 576
            flen = spf // 8 * MP3_KBPS[mpeg1][br] * 1000 // rate + pad
            mono = (buf[i+3] >> 6) == 3
            return rate, spf, flen, mpeg1, mono

        f.seek(p)
        buf = f.read(1 << 16)
        i = 0
        while i < len(buf) - 4 and not header(buf, i):
            i += 1
        h = header(buf, i)
        if not h:
            return None, None
        rate, spf, flen, mpeg1, mono = h
        side = (17 if mono else 32) if mpeg1 else (9 if mono else 17)
        x = buf[i + 4 + side:i + 4 + side + 12]
        if x[:4] in (b'Xing', b'Info') and struct.unpack('>I', x[4:8])[0] & 1:
            return struct.unpack('>I', x[8:12])[0] * spf, rate
        pos, frames = p + i, 0
        while True:
            f.seek(pos)
            h = header(f.read(4), 0)
            if not h:
                break
            frames += 1
            pos += h[2]
        return (frames * spf or None), rate


AUDIO_INFO = {'.wav': wav_info, '.aif': aiff_info, '.aiff': aiff_info, '.flac': flac_info, '.mp3': mp3_info}


def audio_info(path):
    fn = AUDIO_INFO.get(os.path.splitext(path)[1].lower())
    try:
        return fn(path) if fn else (None, None)
    except (OSError, struct.error, IndexError, KeyError):
        return None, None


def live_crc(path):
    """Контрольная сумма сэмпла, как её пишет Ableton (OriginalCrc): CRC-16 по первым 16 КБ файла.
    С нулём Live помечает сэмпл офлайн."""
    with open(path, 'rb') as f:
        data = f.read(16384)
    crc = 0
    for b in data:
        crc ^= b << 8
        for _ in range(8):
            crc = ((crc << 1) ^ 0x8005) & 0xffff if crc & 0x8000 else (crc << 1) & 0xffff
    return crc


def find_file(fl_path, roots):
    if os.path.exists(fl_path):
        return fl_path
    base = fl_path.replace('\\', '/').split('/')[-1]
    for root in roots:
        for dp, _, files in os.walk(root):
            if base in files:
                return os.path.join(dp, base)
    return None


def zip_name(info):
    """Имя файла в архиве. Без флага UTF-8 zipfile читает имена как cp437 — у архивов из Windows
    там обычно UTF-8 или cp866, иначе русские имена сэмплов не сойдутся с путями в .flp."""
    if info.flag_bits & 0x800:
        return info.filename
    try:
        raw = info.filename.encode('cp437')
    except UnicodeEncodeError:  # Python 3.12+ уже взял имя из юникод-поля архива
        return info.filename
    for enc in ('utf-8', 'cp866'):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            pass
    return info.filename


def extract_zip(src, dst):
    with zipfile.ZipFile(src) as z:
        for info in z.infolist():
            info.filename = zip_name(info)
            z.extract(info, dst)


def unpack_input(src, work_dir=None, remove_src=False):
    """Принимает .flp, папку или zip (File -> Export -> Project data files). Возвращает (flp, папка поиска сэмплов)."""
    if src.lower().endswith('.zip'):
        tmp = tempfile.mkdtemp(prefix='fl2als-', dir=work_dir)
        extract_zip(src, tmp)
        if remove_src:
            os.remove(src)
        src = tmp
    if os.path.isdir(src):
        flps = [os.path.join(dp, f) for dp, _, fs in os.walk(src) for f in fs
                if f.lower().endswith('.flp') and 'Backup' not in dp]
        if len(flps) != 1:
            raise SystemExit('в папке должен быть ровно один .flp, нашлось: %d' % len(flps))
        return flps[0], src
    return src, os.path.dirname(os.path.abspath(src))


def convert(src, out_dir, work_dir=None, move_samples=False, remove_src=False):
    """move_samples: сэмплы переносить, а не копировать — когда исходник всё равно временный.
    remove_src: удалить входной zip сразу после распаковки."""
    flp_path, search_root = unpack_input(src, work_dir, remove_src)
    pr = flp.parse(open(flp_path, 'rb').read())
    warn = pr.warnings
    ppq = pr.ppq
    name = os.path.splitext(os.path.basename(flp_path))[0]
    proj_dir = os.path.join(out_dir, name + ' Project')
    sample_dir = os.path.join(proj_dir, 'Samples', 'Imported')
    os.makedirs(sample_dir, exist_ok=True)
    # По этой папке Live узнаёт корень проекта; без неё пути «относительно проекта» не находятся
    # и сэмплы открываются офлайн
    os.makedirs(os.path.join(proj_dir, 'Ableton Project Info'), exist_ok=True)

    b = AlsBuilder(pr.tempo)

    # что звучит на плейлисте: аудиоклипы и паттерны по каналам
    used_audio, used_pat = {}, {}
    for it in pr.items:
        if it.pattern is not None:
            for n in pr.patterns.get(it.pattern, []):
                lst = used_pat.setdefault(n[4], [])
                if all(x is not it for x in lst):
                    lst.append(it)
        elif it.channel is not None:
            used_audio.setdefault(it.channel, []).append(it)
    # источник = дорожка Ableton: (канал, 'audio') для аудиоклипов, (канал, 'midi') для нот
    sources = []
    # по порядку дорожек плейлиста, как у FL
    first_track = {i: min(it.track for it in used_audio.get(i, []) + used_pat.get(i, []))
                   for i in set(used_audio) | set(used_pat)}
    for idx in sorted(first_track, key=lambda i: (first_track[i], i)):
        ch = pr.channels.get(idx)
        if ch is None or ch.type == flp.CH_AUTOMATION:
            continue
        if idx in used_audio:
            sources.append((idx, 'audio'))
        if idx in used_pat:
            sources.append((idx, 'midi'))

    insert_users = {}
    for idx, kind in sources:
        ch = pr.channels[idx]
        if ch.insert:
            insert_users.setdefault(ch.insert, []).append((idx, kind))
    merged = mergeable_inserts(pr, insert_users, used_audio)
    mix = MixerLayout(pr, insert_users, merged, warn)

    lost = Losses()
    tracks = {}        # индекс канала FL -> дорожка Ableton (для автоматизации инструмента)
    devices = {}       # индекс канала FL -> (устройство-инструмент, плагин)
    slot_devices = {}  # (канал микшера, слот) -> (дорожка, устройство, плагин)
    carriers = {}      # канал микшера -> дорожка, на которой его эффекты и громкость
    created = []
    groups = {}
    merged_tracks = {}  # канал микшера -> общая дорожка слитых аудиоклипов

    def group_of(n):
        if n not in groups:
            ins = pr.inserts[n]
            g = groups[n] = b.add_track('group', ins.name or 'Канал %d' % n, color=track_color(pr, [], ins))
            created.append(g)
            put_insert(b, g, ins, lost, slot_devices, warn)
            carriers[n] = g
            parent = mix.parent.get(n)
            if parent:
                b.set_group(g, group_of(parent))
        return groups[n]

    for idx, kind in sources:
        ch = pr.channels[idx]
        n = ch.insert if ch.insert and ch.insert < len(pr.inserts) else 0
        if kind == 'audio':
            t = merged_tracks.get(n) if n in merged else None
            if t is None:
                tname = track_name(pr, ch, used_audio[idx], insert_users)
                color = track_color(pr, used_audio[idx], pr.inserts[n] if n else None)
                if n in merged:
                    tname = pr.inserts[n].name or tname
                    merged_tracks[n] = t = b.add_track('audio', tname, color=color)
                else:
                    t = b.add_track('audio', tname, color=color)
                created.append(t)
            for it in used_audio[idx]:
                put_audio(b, t, pr, ch, it, search_root, sample_dir, warn, move_samples)
        else:
            t = b.add_track('midi', track_name(pr, ch, used_pat[idx], insert_users),
                            color=track_color(pr, used_pat[idx], pr.inserts[n] if n else None))
            created.append(t)
            pl = ch.plugin
            if ch.type in (flp.CH_SAMPLER, flp.CH_AUDIOCLIP):
                put_sampler(b, t, ch, search_root, sample_dir, warn, move_samples)
            elif pl and pl.format:
                dev, used_pl = add_plugin(b, t, pl, True, warn)
                if dev is not None:
                    devices[idx] = (dev, used_pl)
                else:
                    warn.append('«%s»: инструмент %s (%s) пока не переносится, дорожка без инструмента'
                                % (ch.name, pl.name, pl.format.upper()))
            else:
                warn.append('«%s»: встроенный плагин FL (%s) не переносится, дорожка без инструмента'
                            % (ch.name, pl.name if pl else '?'))
            for it in used_pat[idx]:
                put_midi(b, t, pr, idx, it)
        tracks.setdefault(idx, t)
        if not n:
            continue
        if n in mix.groups:
            b.set_group(t, group_of(n))
        elif n not in carriers:
            # канал микшера только у этой дорожки или аудиоклипы слиты на одну дорожку
            put_insert(b, t, pr.inserts[n], lost, slot_devices, warn)
            carriers[n] = t
            if mix.parent.get(n):
                b.set_group(t, group_of(mix.parent[n]))

    if pr.inserts:
        carriers[0] = b.main_track()
        put_insert(b, carriers[0], pr.inserts[0], lost, slot_devices, warn)

    for src, dst, slot in mix.sidechains:
        # канал без звука (никто в него не идёт) — сайдчейну нечего слушать
        if src in carriers and (dst, slot) in slot_devices:
            b.set_sidechain(slot_devices[(dst, slot)][1], carriers[src])

    b.order_tracks(track_order(created))
    for pos, mname in pr.markers:
        b.add_locator(pos / pr.ppq, mname)
    put_automation(b, pr, tracks, devices, slot_devices, carriers, warn)

    out = os.path.join(proj_dir, name + '.als')
    b.write(out)
    warn[:] = compact_warnings(warn + lost.lines())
    return out, warn


def track_color(pr, items, ins):
    """Цвет дорожки Live: свой цвет дорожек плейлиста, где лежат клипы (если он один),
    иначе цвет канала микшера, если его задали руками."""
    colors = {pr.track_colors[it.track] for it in items if it.track in pr.track_colors}
    if len(colors) == 1:
        return fl_to_live(colors.pop())
    if ins is not None and ins.color is not None and ins.color != NAMED_COLOR:
        return fl_to_live(ins.color)
    return None


def track_name(pr, ch, items, insert_users):
    """Имя дорожки плейлиста, если все клипы на одной именованной; иначе имя канала микшера,
    если он только у этого канала; иначе имя канала FL."""
    names = {pr.track_names.get(it.track) for it in items}
    if len(names) == 1 and None not in names and '' not in names:
        return names.pop()
    if ch.insert and ch.insert < len(pr.inserts) and len(insert_users.get(ch.insert, [])) == 1:
        return pr.inserts[ch.insert].name or ch.name
    return ch.name


def track_order(created):
    """Дорожки по порядку создания, но каждая группа — перед своими дорожками."""
    parent = {}
    by_id = {t.get('Id'): t for t in created}
    for t in created:
        g = t.find('TrackGroupId').get('Value')
        if g in by_id:
            parent[t] = by_id[g]
    children = {}
    for t in created:
        children.setdefault(parent.get(t), []).append(t)
    first = {}

    def first_index(t):
        if t not in first:
            first[t] = min([created.index(t)] + [first_index(c) for c in children.get(t, [])])
        return first[t]

    out = []

    def emit(t):
        out.append(t)
        for c in sorted(children.get(t, []), key=first_index):
            emit(c)
    for t in sorted(children.get(None, []), key=first_index):
        emit(t)
    return out


class MixerLayout:
    """Связи каналов микшера FL. Канал, который идёт только в другой канал (не в мастер),
    становится дорожкой внутри группы; на группе — эффекты канала-получателя.
    Сайдчейн: у плагина выбран второй вход — N-й по счёту канал, идущий в этот канал микшера.
    Группой становится и канал, куда идут несколько одновременно звучащих дорожек."""

    def __init__(self, pr, insert_users, merged, warn):
        self.parent, self.groups = {}, set()
        self.sidechains = []   # (откуда, куда, слот плагина)
        n_ins = len(pr.inserts)
        incoming = {}
        for ins in pr.inserts[1:]:
            for r in ins.routes:
                if r != ins.index and r < n_ins:
                    incoming.setdefault(r, []).append(ins.index)
        sc_routes = set()
        for dst, srcs in incoming.items():
            for slot, pl in sorted(pr.inserts[dst].slots.items()):
                if 0 < pl.sc_input <= len(srcs):
                    self.sidechains.append((srcs[pl.sc_input - 1], dst, slot))
                    sc_routes.add((srcs[pl.sc_input - 1], dst))
        for ins in pr.inserts[1:]:
            routes = [r for r in ins.routes if r != ins.index and r < n_ins]
            audio = [r for r in routes if (ins.index, r) not in sc_routes]
            if not audio and routes:
                # канал идёт только в сайдчейн — звучит там же, куда идёт
                audio = [max(routes, key=lambda r: len(incoming.get(r, [])))]
            if len(audio) > 1 and 0 in audio:
                # идёт и в мастер, и ещё куда-то: похоже на сайдчейн, у которого в FL не выбран вход плагина
                for r in audio:
                    if r == 0:
                        continue
                    free = [k for k, pl in sorted(pr.inserts[r].slots.items()) if pl.sc_free]
                    self.sidechains += [(ins.index, r, k) for k in free]
                    warn.append('канал микшера %s идёт и в мастер, и в %s — считаю это сайдчейном%s'
                                % (Losses.where(ins), Losses.where(pr.inserts[r]),
                                   ' для ' + ', '.join(pr.inserts[r].slots[k].name for k in free) +
                                   ' (в FL у плагина не выбран вход сайдчейна — проверь)' if free else
                                   ', но в FL его не принимает ни один плагин'))
                audio = [0]
            if len(audio) == 1 and audio[0]:
                self.parent[ins.index] = audio[0]
            elif len(audio) > 1:
                self.parent[ins.index] = max(audio, key=lambda r: len(incoming.get(r, [])))
                warn.append('канал микшера %s идёт сразу в несколько каналов — оставил %s'
                            % (Losses.where(ins), Losses.where(pr.inserts[self.parent[ins.index]])))
        self.groups = set(self.parent.values())
        for n, users in insert_users.items():
            if len(users) > 1 and n not in merged:
                self.groups.add(n)


def mergeable_inserts(pr, insert_users, used_audio):
    """Каналы микшера, на которые идут только аудиоклипы, не звучащие одновременно, — их можно
    сложить на одну дорожку Ableton вместе с эффектами канала. Клипы внахлёст так не сложить:
    на дорожке Live поздний клип обрезает ранний."""
    out = set()
    for n, users in insert_users.items():
        if len(users) < 2 or any(kind != 'audio' for _, kind in users):
            continue
        spans = sorted((it.pos, it.pos + it.length) for i, _ in users for it in used_audio[i])
        if all(b0 >= a1 for (_, a1), (b0, _) in zip(spans, spans[1:])):
            out.add(n)
    return out


# Встроенные в FL анализаторы и рекордеры: на звук не влияют, в Ableton они не нужны
FL_SILENT = {'Edison', 'Wave Candy', 'Fruity dB Meter', 'Fruity Spectroman', 'Fruity Big Clock',
             'Fruity Scope', 'Fruity Dance', 'Fruity Notebook', 'Fruity Notebook 2'}


class Losses:
    """Что не перенеслось из микшера — одной строкой на плагин, а не на каждый слот."""

    def __init__(self):
        self.plugins = {}   # (имя, вид) -> [где]
        self.silent = []
        self.shared_inserts = []

    @staticmethod
    def where(ins):
        if ins.index == 0:
            return 'мастер'
        return '%d «%s»' % (ins.index, ins.name) if ins.name else str(ins.index)

    def plugin(self, ins, pl):
        if pl.format == '' and pl.name in FL_SILENT:
            if pl.name not in self.silent:
                self.silent.append(pl.name)
            return
        kind = {'vst2': 'VST2', 'clap': 'CLAP — Ableton его не поддерживает'}.get(pl.format, 'встроенный в FL')
        places = self.plugins.setdefault((pl.name, kind), [])
        if (ins.index, self.where(ins)) not in places:
            places.append((ins.index, self.where(ins)))

    def shared(self, ins):
        if self.where(ins) not in self.shared_inserts:
            self.shared_inserts.append(self.where(ins))

    def lines(self):
        out = ['%s (%s) не перенесён — канал микшера: %s' % (n, k, ', '.join(i for _, i in sorted(p)))
               for (n, k), p in self.plugins.items()]
        for w in self.shared_inserts:
            out.append('канал микшера %s: на него идут несколько дорожек, звучащих одновременно, '
                       '— эффекты канала не перенесены' % w)
        if self.silent:
            out.append('пропущены анализаторы FL, на звук не влияют: %s' % ', '.join(self.silent))
        return out


def put_insert(b, track, ins, lost, slot_devices, warn):
    for slot in sorted(ins.slots):
        pl = ins.slots[slot]
        dev, used = add_plugin(b, track, pl, False, warn)
        if dev is not None:
            slot_devices[(ins.index, slot)] = (track, dev, used)
        else:
            lost.plugin(ins, pl)
    b.set_mixer(track, gain=fl_volume_to_gain(ins.volume), pan=max(-1, min(1, ins.pan / 6400)) if ins.pan else None)


# VST3-версии плагинов, в которые переводим CLAP и VST2 того же производителя:
# имя у FL -> (имя VST3, производитель, GUID в порядке FL). GUID сняты с проектов пользователя.
VST3_TWINS = {
    'Pro-L 2': ('FabFilter Pro-L 2', 'FabFilter', '722fd9af049ab747b5e8d1d568dea985'),
    'FabFilter Pro-L 2': ('FabFilter Pro-L 2', 'FabFilter', '722fd9af049ab747b5e8d1d568dea985'),
    'Pro-Q 3': ('FabFilter Pro-Q 3', 'FabFilter', '71dbc4724d7a9a45b97e51745d84b39d'),
    'FabFilter Pro-Q 3': ('FabFilter Pro-Q 3', 'FabFilter', '71dbc4724d7a9a45b97e51745d84b39d'),
}


def fabfilter_vst3(pl):
    """CLAP или VST2 FabFilter -> такой же плагин VST3. Параметры у них одни и те же, по порядку:
    CLAP/VST2 хранят «FFBS», версия 1, число параметров, значения float;
    VST3 — «FabF», версия 2, имя пресета, 0, число параметров, те же значения, хвост 1, 1."""
    twin = VST3_TWINS.get(pl.name)
    st = pl.state
    p = st.find(b'FFBS')
    if not twin or p < 0 or p > 4 or len(st) < p + 12:
        return None
    n = struct.unpack('<I', st[p+8:p+12])[0]
    values = st[p+12:p+12+4*n]
    if len(values) != 4 * n:
        return None
    preset = b''
    tail = st[p+12+4*n:]
    # после значений у CLAP: метка из 4 байт, версия, длина и имя пресета
    if len(tail) >= 12:
        ln = struct.unpack('<I', tail[8:12])[0]
        if 0 < ln < 256 and len(tail) >= 12 + ln:
            preset = tail[12:12+ln]
    proc = (b'FabF' + struct.pack('<II', 2, len(preset)) + preset + struct.pack('<II', 0, n) + values
            + struct.pack('<II', 1, 1))
    name, vendor, guid = twin
    return flp.Plugin(name=name, vendor=vendor, format='vst3', guid=bytes.fromhex(guid), processor=proc,
                      controller=b'FFed' + struct.pack('<I', 0) + struct.pack('<f', 1.0),
                      param_ids=list(range(n)), sc_input=pl.sc_input, sc_free=pl.sc_free)


def add_plugin(b, track, pl, instrument, warn):
    """Плагин FL -> устройство Ableton. Возвращает (устройство, плагин, которым его собрали) или (None, None)."""
    if pl.format == 'vst3':
        return b.add_vst3(track, pl, instrument), pl
    if pl.format in ('clap', 'vst2'):
        twin = fabfilter_vst3(pl)
        if twin:
            warn.append('%s (%s) заменён на %s (VST3) с теми же настройками'
                        % (pl.name, pl.format.upper(), twin.name))
            return b.add_vst3(track, twin, instrument), twin
    if pl.format == 'vst2' and pl.state and pl.vst2_id:
        return b.add_vst2(track, pl, instrument), pl
    if pl.format == '' and pl.name == 'Fruity Balance' and len(pl.data) >= 8:
        bal, vol = struct.unpack('<ii', pl.data[:8])
        if bal:
            warn.append('Fruity Balance -> Utility: шкала баланса угадана (±128 = край) — проверь')
        return b.add_utility(track, vol / 256, max(-1.0, min(1.0, bal / 128))), pl
    if pl.format == '' and pl.name == 'Fruity Parametric EQ 2' and len(pl.data) >= 144:
        warn.append('Parametric EQ 2 заменён на EQ Eight — частоты точные, ширина полос и крутизна срезов '
                    'приблизительные')
        return b.add_eq8(track, parametric_eq2_bands(pl.data)), pl
    return None, None


# Типы полос Parametric EQ 2 -> режим EQ Eight. Срезы у EQ Eight 12 или 48 дБ/окт — берём 48.
EQ2_MODES = {1: 7, 3: 0, 4: 4, 5: 2, 6: 3, 7: 5}


def parametric_eq2_bands(d):
    """Данные Parametric EQ 2: 36 чисел int32 — версия, 7 усилений (сотые дБ), 7 частот (0..65536,
    20 Гц * 1000^x), 7 ширин, 7 типов, 7 чисел неизвестного назначения."""
    v = struct.unpack('<36i', d[:144])
    bands = []
    for i in range(7):
        gain, freq, width, kind = v[1+i], v[8+i], v[15+i], v[22+i]
        mode = EQ2_MODES.get(kind)
        hz = 20 * 1000 ** (freq / 65536)
        # подогнано по двум полосам образца: ширина 19570 -> Q 3, 32842 -> Q 1
        q = max(0.1, min(18.0, 15.2 * math.exp(-8.28e-5 * width)))
        bands.append((mode is not None, mode if mode is not None else 3, hz,
                      max(-15.0, min(15.0, gain / 100)), q))
    return bands


def put_midi(b, t, pr, idx, it):
    ppq = pr.ppq
    start = it.start if it.start is not None else 0
    end = it.end if it.end is not None else start + it.length
    notes = [(pos / ppq, ln / ppq, key, max(1, min(127, vel)))
             for pos, ln, key, vel, rack in pr.patterns[it.pattern] if rack == idx and start <= pos < end]
    b.add_midi_clip(t, pr.channels[idx].name, it.pos / ppq, it.length / ppq, start / ppq, notes)


def take_sample(ch, search_root, sample_dir, warn, move):
    """Копирует сэмпл канала в проект. Возвращает описание для Ableton или None."""
    # сначала среди уже перенесённых: при move исходника на старом месте нет
    src = find_file(ch.sample_path, [sample_dir, search_root])
    if not src:
        warn.append('«%s»: не найден файл %s' % (ch.name, ch.sample_path))
        return None
    base = os.path.basename(src)
    dst = os.path.join(sample_dir, base)
    if src != dst and not os.path.exists(dst):
        (shutil.move if move else shutil.copy2)(src, dst)
    frames, rate = audio_info(dst)
    if not frames or not rate:
        warn.append('«%s»: не разобрал формат %s (понимаю wav, aiff, flac, mp3)' % (ch.name, base))
        return None
    return dict(rel_path='Samples/Imported/' + base, size=os.path.getsize(dst), mtime=os.path.getmtime(dst),
                crc=live_crc(dst), frames=frames, rate=rate, sec=frames / rate)


# По этим словам в имени дорожки, канала или файла звук считаем ударными: им в Live подходит Beats,
# всему остальному (басы, вокал, синты) — Complex Pro
DRUM_WORDS = {'kick', 'kicks', 'kik', 'snare', 'snares', 'snr', 'clap', 'claps', 'hat', 'hats', 'hihat', 'hh',
              'perc', 'percs', 'percussion', 'drum', 'drums', 'tom', 'toms', 'cymbal', 'crash', 'ride',
              'shaker', 'shakers', 'break', 'breaks', 'top', 'tops', 'rim', 'snap', 'fill', 'fills'}


def warp_mode_for(*names):
    words = set(re.split(r'[^a-z]+', ' '.join(names).lower()))
    return 0 if words & DRUM_WORDS else 6


def put_audio(b, t, pr, ch, it, search_root, sample_dir, warn, move=False):
    sample = take_sample(ch, search_root, sample_dir, warn, move)
    if not sample:
        return
    sec = sample['sec']
    length = it.length / pr.ppq
    # у аудиоклипа FL начало в мс; −1 — клип не обрезан
    offset = max(0.0, it.start or 0.0) / 1000
    if ch.stretch_time > 0 or ch.stretch_pitch:
        # растяжка FL: темп не влияет, высота меняется без изменения скорости
        # единицы растяжки: 3072 = 16 долей, 111 = 0,578 доли (два проекта)
        beats = ch.stretch_time / 192 if ch.stretch_time > 0 else sec * pr.tempo / 60
        sample['beats'] = beats
        tname = t.find('Name/EffectiveName').get('Value')
        b.add_audio_clip(t, ch.name, it.pos / pr.ppq, length, offset * beats / sec, sample,
                         pitch=ch.pitch + ch.stretch_pitch,
                         warp_mode=warp_mode_for(tname, ch.name, os.path.basename(sample['rel_path'])))
    elif ch.pitch:
        # без растяжки FL меняет высоту вместе со скоростью — в Live это Re-Pitch
        factor = 2 ** (ch.pitch / 1200)
        sample['beats'] = sec * pr.tempo / 60 / factor
        b.add_audio_clip(t, ch.name, it.pos / pr.ppq, length, offset * sample['beats'] / sec, sample, warp_mode=3)
    else:
        # без растяжки и сдвига — клип играет как есть, в Live это клип без Warp (границы в секундах)
        sample['beats'] = sec * pr.tempo / 60
        b.add_audio_clip(t, ch.name, it.pos / pr.ppq, length, offset, sample, warped=False,
                         loop_len=length * 60 / pr.tempo)


def put_sampler(b, t, ch, search_root, sample_dir, warn, move=False):
    sample = take_sample(ch, search_root, sample_dir, warn, move)
    if not sample:
        return
    start = max(0.0, min(1.0, ch.sample_start))
    b.add_sampler(t, ch.name, sample, start, start + max(0.0, min(1.0, ch.sample_length)) * (1 - start))
    if ch.pitch or ch.stretch_pitch:
        warn.append('«%s»: высота канала сэмплера (%+d центов) не перенесена' % (ch.name, ch.pitch + ch.stretch_pitch))


def automation_target(pr, target, devices, slot_devices, tracks, carriers):
    """Код цели из события 227 -> ('param', дорожка, устройство, плагин, индекс параметра),
    ('mixer', дорожка, 'Volume' | 'Pan') или None.
    Инструмент: (канал << 16) | 0x8000 | параметр.
    Эффект в микшере: ((0x7000 + канал микшера << 6 + слот) << 16) | 0x8000 | параметр.
    Громкость и панорама канала микшера: ((0x7000 + канал << 6) << 16) | 0x1fc0 / 0x1fc1."""
    hi, lo = target >> 16, target & 0xffff
    if hi >= 0x7000 and not lo & 0x8000:
        n = (hi - 0x7000) >> 6
        what = {0xc0: 'Volume', 0xc1: 'Pan'}.get(lo & 0xff)
        if what and n in carriers:
            return 'mixer', carriers[n], what
        return None
    if not lo & 0x8000:
        return None
    if hi >= 0x7000:
        key = ((hi - 0x7000) >> 6, hi & 0x3f)
        if key not in slot_devices:
            return None
        track, dev, pl = slot_devices[key]
        return 'param', track, dev, pl, lo & 0x7fff
    if hi not in devices:
        return None
    dev, pl = devices[hi]
    return 'param', tracks[hi], dev, pl, lo & 0x7fff


def put_automation(b, pr, tracks, devices, slot_devices, carriers, warn):
    ppq = pr.ppq
    params = {}
    for auto_ch, target in pr.connections:
        ach = pr.channels.get(auto_ch)
        if not ach or not ach.automation:
            continue
        found = automation_target(pr, target, devices, slot_devices, tracks, carriers)
        if not found:
            warn.append('автоматизация «%s» не перенесена: её цель не попала в Ableton или пока не поддерживается'
                        % ach.name)
            continue
        pts = []
        items = sorted((it for it in pr.items if it.channel == auto_ch), key=lambda i: i.pos)
        for it in items:
            # −1 — клип не обрезан, играет с начала огибающей
            s = it.start if it.start is not None and it.start >= 0 else 0.0
            e = it.end if it.end is not None and it.end > s else s + it.length / ppq
            for t, v in clip_points(ach.automation, s, e):
                pts.append((it.pos / ppq + t - s, v))
        if not pts:
            continue
        if found[0] == 'mixer':
            _, track, what = found
            if what == 'Volume':
                # у FL фейдер 0..1, 0,8 — 0 дБ; в Live не тише −70 дБ
                pts = [(t, max(0.0003162277571, fl_volume_to_gain(v * 16000))) for t, v in pts]
            else:
                pts = [(t, v * 2 - 1) for t, v in pts]
            b.add_envelope(track, b.mixer_target(track, what), pts)
            continue
        _, track, dev, pl, index = found
        if pl.format == '' and pl.name == 'Fruity Balance' and index in (0, 1):
            # 1 — громкость (0..1 шкалы = 0..320, 256 = 0 дБ), 0 — баланс
            what = 'Gain' if index == 1 else 'Balance'
            pts = [(t, v * BALANCE_MAX if index == 1 else v * 2 - 1) for t, v in pts]
            b.add_envelope(track, dev.find(what + '/AutomationTarget').get('Id'), pts)
            continue
        if pl.format == 'vst2':
            pid = index
        elif index < len(pl.param_ids):
            pid = pl.param_ids[index]
        else:
            warn.append('автоматизация «%s»: нет параметра №%d у %s' % (ach.name, index, pl.name))
            continue
        if (pl.name, pid) in PARAM_DB:
            lo_db, hi_db = PARAM_DB[(pl.name, pid)]
            lo_v, hi_v = 10 ** (lo_db / 20), 10 ** (hi_db / 20)
            pts = [(t, 10 ** ((lo_db + v * (hi_db - lo_db)) / 20)) for t, v in pts]
        else:
            lo_v, hi_v = PARAM_RANGES.get((pl.name, pid), (0, 1))
            if (pl.name, pid) not in PARAM_RANGES:
                warn.append('автоматизация «%s»: шкала параметра %d у %s неизвестна, пишу 0..1 — проверь в Ableton'
                            % (ach.name, pid, pl.name))
            pts = [(t, lo_v + v * (hi_v - lo_v)) for t, v in pts]
        key = (id(dev), index)
        if key not in params:
            # «Serum 2 - Filter» -> «Filter», «Replika XT - BASS - Mix» -> «Mix»
            pname = ach.name.rsplit(' - ', 1)[1] if ' - ' in ach.name else ''
            params[key] = b.add_param(dev, pid, pts[0][1], pname, lo_v, hi_v)
        b.add_envelope(track, params[key], pts)


def clip_points(points, s, e):
    """Точки огибающей в окне [s, e] (доли), с добавленными значениями на краях."""
    def at(x):
        if x <= points[0][0]:
            return points[0][1]
        for (t0, v0), (t1, v1) in zip(points, points[1:]):
            if t0 <= x <= t1:
                return v0 if t1 == t0 else v0 + (v1 - v0) * (x - t0) / (t1 - t0)
        return points[-1][1]
    inside = [(t, v) for t, v in points if s < t < e]
    return [(s, at(s))] + inside + [(e, at(e))]
