"""Чтение проекта Ableton Live (.als) в простую модель для записи во FL."""
import gzip, math, os, re, struct
import xml.etree.ElementTree as ET
from urllib.parse import unquote
from .flp import Record
from .convert import PARAM_DB

# Время «до начала песни», с которого Live пишет значение огибающей по умолчанию
ENV_DEFAULT_TIME = -63072000


class Vst3(Record):
    FIELDS = (
        ('name', ''),
        ('vendor', ''),
        ('guid', b''),          # class id в порядке байтов FL (как GUID Windows)
        ('instrument', False),
        ('processor', b''),
        ('controller', b''),
        ('params', {}),         # AutomationTarget Id -> (ParameterId, мин, макс, имя); мин/макс — шкала Live
        # VST2 (vst2_id != 0): чанк состояния как его отдаёт плагин, путь к .dll и шины из Live
        ('vst2_id', 0),
        ('path', ''),
        ('chunk', b''),
        ('values', []),         # VST2 без чанка (FxBk): значения параметров 0..1 по порядку
        ('vst_version', 2400),
        ('plugin_version', 0),
        ('ins', 1),
        ('outs', 1),
    )


class Eq8(Record):
    """EQ Eight или Auto Filter (одна полоса-срез) — переносятся в Parametric EQ 2."""
    FIELDS = (
        ('name', 'EQ Eight'),
        ('bands', []),          # включённые полосы: [(режим EQ Eight, Гц, дБ, Q)]
        ('notes', []),          # что перенесётся неточно
        ('targets', ()),        # AutomationTarget Id его параметров
        ('auto', {}),           # AutomationTarget Id -> (номер в bands, 'Freq' | 'Gain' | 'Q')
        ('slot_of', {}),        # номер в bands -> полоса Parametric EQ 2 (заполняет запись во FL)
    )


class Utility(Record):
    """Utility (StereoGain) — переносится в kHs Gain или Fruity Balance: громкость и баланс."""
    FIELDS = (
        ('name', 'Utility'),
        ('gain', 1.0),          # множитель, 1.0 = 0 дБ (у Live до +35 дБ)
        ('balance', 0.0),       # -1..1
        ('notes', []),
        ('params', {}),         # AutomationTarget Id -> имя параметра ('Gain', 'Balance', 'StereoWidth'…)
        ('targets', ()),
    )


class Sampler(Record):
    """Simpler или пэд Drum Rack — канал-сэмплер FL с этим сэмплом."""
    FIELDS = (
        ('name', ''),
        ('sample', None),       # dict(path, rel_path, size), как у аудиоклипа
        ('root', 60),           # нота, на которой сэмпл звучит без сдвига
        ('key', None),          # пэд Drum Rack: на какую ноту отзывается; None — на все
        ('start', 0.0),         # звучащий отрезок сэмпла, доли файла
        ('end', 1.0),
        ('plugin', None),       # инструмент-плагин вместо сэмпла (Vst3): пэд или цепочка Instrument Rack
        ('send_note', 60),      # пэд: какую ноту шлёт плагину
        ('transpose', 0),       # плагину: сдвиг нот (MidiPitcher в цепочке)
        ('envelopes', []),      # автоматизация плагина, как Track.envelopes
        ('chain', []),          # [Bus] от ближнего к дальнему: цепочка стойки, пэд Drum Rack
    )


class Bus(Record):
    """Цепочка Instrument Rack или пэд Drum Rack: свои эффекты и громкость — в FL свой канал микшера."""
    FIELDS = (
        ('name', ''),
        ('effects', []),
        ('envelopes', []),
        ('gain', 1.0),
        ('pan', 0.0),
    )

    def needed(self):
        return bool(self.effects) or abs(self.gain - 1) > 1e-4 or abs(self.pan) > 1e-3


class Clip(Record):
    FIELDS = (
        ('time', 0.0),          # начало в аранжировке, доли
        ('length', 0.0),        # доли
        ('name', ''),
        ('notes', []),          # [(позиция от начала клипа, длина, нота, сила)] — уже развёрнуты по повторам
        ('sample', None),       # аудио: dict(path, rel_path, size)
        ('offset', 0.0),        # аудио: с какой доли сэмпла начинается клип
        ('sample_beats', 0.0),  # аудио: сколько долей занимает весь сэмпл при растяжке
        ('sample_sec', 0.0),
        ('warped', True),
    )


class Track(Record):
    FIELDS = (
        ('kind', ''),           # midi, audio, group, main
        ('id', -1),
        ('group', -1),          # Id группы, в которую входит дорожка
        ('name', ''),
        ('color', -1),          # номер цвета в палитре Live
        ('gain', 1.0),          # громкость микшера, множитель (1.0 = 0 дБ)
        ('pan', 0.0),           # -1..1
        ('transpose', 0),       # MidiPitcher перед инструментом, полутоны
        ('instrument', None),   # Vst3
        ('samplers', []),       # [Sampler] — Simpler или пэды Drum Rack вместо VST3-инструмента
        ('effects', []),        # [Vst3]
        ('clips', []),
        ('envelopes', []),      # [(Vst3, ParameterId, имя параметра, [(доля, 0..1)])]
                                # у Utility: (Utility, 'Gain' | 'Balance', имя, [(доля, значение как в Live)])
        ('mixer_envelopes', []),  # [('Volume' | 'Pan', [(доля, значение как в Live)])]
        ('skipped', []),        # имена устройств, которые не переносятся
    )


class AlsProject(Record):
    FIELDS = (
        ('tempo', 120.0),
        ('tracks', []),
        ('main', None),
        ('locators', []),      # [(доля, имя)]
        ('warnings', []),
    )


def val(el, path, default=None):
    x = el.find(path)
    return x.get('Value') if x is not None else default


def num(el, path, default=0.0):
    v = val(el, path)
    return float(v) if v is not None else default


def guid_bytes(fields):
    """Четыре поля Uid из Live -> байты GUID в порядке FL (обратное als.guid_fields)."""
    f = [int(x) & 0xffffffff for x in fields]
    return (struct.pack('<IHH', f[0], f[1] >> 16, f[1] & 0xffff)
            + f[2].to_bytes(4, 'big') + f[3].to_bytes(4, 'big'))


def hexdata(el):
    return bytes.fromhex(''.join((el.text or '').split())) if el is not None else b''


def read_vst3(dev):
    info = dev.find('PluginDesc/Vst3PluginInfo')
    if info is None:
        return None
    preset = info.find('Preset/Vst3Preset')
    path = val(dev, 'SourceContext/Value/BranchSourceContext/BrowserContentPath', '')
    m = re.match(r'query:Plugins#VST3:([^:]*):', path)
    pl = Vst3(name=val(info, 'Name', ''), vendor=unquote(m.group(1)) if m else '',
              guid=guid_bytes([x.get('Value') for x in info.find('Uid')]),
              instrument=val(info, 'DeviceType') == '1',
              processor=hexdata(preset.find('ProcessorState')) if preset is not None else b'',
              controller=hexdata(preset.find('ControllerState')) if preset is not None else b'')
    for p in dev.iter('PluginFloatParameter'):
        target = p.find('ParameterValue/AutomationTarget')
        if target is None:
            continue
        lo = num(p, 'ParameterValue/MidiControllerRange/Min', 0.0)
        hi = num(p, 'ParameterValue/MidiControllerRange/Max', 1.0)
        pl.params[target.get('Id')] = (int(val(p, 'ParameterId')), lo, hi, val(p, 'ParameterName', ''))
    return pl


def read_vst2(dev):
    """VST2 с чанком состояния (FBCh/FPCh) или только со значениями параметров (FxBk:
    в Buffer 28 байт имени программы и float на каждый параметр)."""
    info = dev.find('PluginDesc/VstPluginInfo')
    preset = info.find('Preset/VstPreset') if info is not None else None
    if preset is None:
        return None
    kind = int(num(preset, 'Type')) & 0xffffffff
    chunk = hexdata(preset.find('Buffer'))
    values = []
    if kind == 0x4678426b:
        # банк программ, берём текущую
        n, size = int(num(preset, 'ParameterCount')), 28 + 4 * int(num(preset, 'ParameterCount'))
        at = int(num(preset, 'ProgramNumber')) * size
        if not n or len(chunk) != int(num(preset, 'ProgramCount', 1)) * size or at + size > len(chunk):
            return None
        values, chunk = list(struct.unpack('<%df' % n, chunk[at+28:at+size])), b''
    elif kind not in (0x46424368, 0x46504368) or not chunk:
        return None
    pl = Vst3(name=val(info, 'PlugName', ''), vst2_id=int(num(info, 'UniqueId')) & 0xffffffff,
              path=val(info, 'Path', '').replace('/', '\\'), chunk=chunk, values=values,
              vst_version=int(num(info, 'VstVersion', 2400)), instrument=val(info, 'Category') == '2',
              plugin_version=int(num(info, 'Version')),
              ins=int(num(info, 'NumAudioInputs', 1)), outs=int(num(info, 'NumAudioOutputs', 1)))
    # у VST2 ParameterId — номер параметра, по нему же FL находит цель автоматизации
    for p in dev.iter('PluginFloatParameter'):
        target = p.find('ParameterValue/AutomationTarget')
        if target is None:
            continue
        lo = num(p, 'ParameterValue/MidiControllerRange/Min', 0.0)
        hi = num(p, 'ParameterValue/MidiControllerRange/Max', 1.0)
        pl.params[target.get('Id')] = (int(val(p, 'ParameterId')), lo, hi, val(p, 'ParameterName', ''))
    return pl


def read_notes(clip):
    """Ноты клипа в окне, которое видно в аранжировке, с повторами петли."""
    start = num(clip, 'CurrentStart')
    end = num(clip, 'CurrentEnd')
    length = end - start
    ls, le = num(clip, 'Loop/LoopStart'), num(clip, 'Loop/LoopEnd')
    rel = num(clip, 'Loop/StartRelative')
    looped = val(clip, 'Loop/LoopOn') == 'true'
    raw = []
    for kt in clip.iter('KeyTrack'):
        key = int(val(kt, 'MidiKey'))
        for n in kt.iter('MidiNoteEvent'):
            if n.get('IsEnabled', 'true') != 'true':
                continue
            raw.append((float(n.get('Time')), float(n.get('Duration')), key, int(float(n.get('Velocity')))))
    notes = []
    if looped and le > ls:
        period = le - ls
        # первая прокрутка начинается с LoopStart + StartRelative, дальше — целые петли
        t0 = -rel
        while t0 < length:
            for t, d, k, v in raw:
                if ls <= t < le:
                    pos = t0 + (t - ls)
                    if 0 <= pos < length:
                        notes.append((pos, min(d, le - t), k, v))
            t0 += period
    else:
        for t, d, k, v in raw:
            pos = t - ls
            if 0 <= pos < length:
                notes.append((pos, d, k, v))
    notes.sort()
    return start, length, notes


def read_audio(clip):
    start = num(clip, 'CurrentStart')
    end = num(clip, 'CurrentEnd')
    sample = file_ref(clip.find('SampleRef/FileRef'))
    frames = num(clip, 'SampleRef/DefaultDuration')
    rate = num(clip, 'SampleRef/DefaultSampleRate', 44100.0) or 44100.0
    sec = frames / rate if frames else 0.0
    markers = [(float(w.get('SecTime')), float(w.get('BeatTime'))) for w in clip.iter('WarpMarker')]
    warped = val(clip, 'IsWarped') == 'true'
    if len(markers) >= 2 and markers[-1][0] > markers[0][0]:
        # растяжка линейная по крайним маркерам; доли на весь сэмпл
        (s0, b0), (s1, b1) = markers[0], markers[-1]
        beats = (b1 - b0) / (s1 - s0) * sec if sec else b1 - b0
    else:
        beats = 0.0
    c = Clip(time=start, length=end - start, name=val(clip, 'Name', ''), sample=sample,
             offset=num(clip, 'Loop/LoopStart') + num(clip, 'Loop/StartRelative'),
             sample_beats=beats, sample_sec=sec, warped=warped)
    return c


def read_eq8(dev):
    eq = Eq8(bands=[], notes=[], targets=tuple(a.get('Id') for a in dev.iter('AutomationTarget')), auto={},
             slot_of={})
    if val(dev, 'Mode', '0') != '0':
        eq.notes.append('режим L/R или M/S — перенесена только полоса A')
    if num(dev, 'GlobalGain/Manual') != 0:
        eq.notes.append('общий Gain не переносится')
    scale = num(dev, 'Scale/Manual', 1.0)
    for i in range(8):
        b = dev.find('Bands.%d/ParameterA' % i)
        if b is None or val(b, 'IsOn/Manual') != 'true':
            continue
        for what in ('Freq', 'Gain', 'Q'):
            a = b.find(what + '/AutomationTarget')
            if a is not None:
                eq.auto[a.get('Id')] = (len(eq.bands), what)
        eq.bands.append((int(num(b, 'Mode/Manual', 3)), num(b, 'Freq/Manual', 1000.0),
                         num(b, 'Gain/Manual') * scale, num(b, 'Q/Manual', 0.71)))
    return eq


# Auto Filter: тип фильтра -> режим полосы EQ Eight (1 срез низа, 6 срез верха, 4 notch)
AUTOFILTER_MODES = {0: 6, 1: 1, 3: 4}


def read_autofilter(dev):
    """Auto Filter -> одна полоса Parametric EQ 2 (срез низа/верха или notch); частоту можно автоматизировать."""
    kind = int(num(dev, 'Filter_Type/Manual'))
    if kind not in AUTOFILTER_MODES:
        return None
    eq = Eq8(name='Auto Filter', bands=[], notes=[], auto={}, slot_of={},
             targets=tuple(a.get('Id') for a in dev.iter('AutomationTarget')))
    # резонанс 0..1 -> Q полосы; 0 — без подъёма у среза (0,71), подобрано на глаз
    res = num(dev, 'Filter_Resonance/Manual')
    eq.bands.append((AUTOFILTER_MODES[kind], num(dev, 'Filter_Frequency/Manual', 20000.0), 0.0, 0.71 + 6 * res))
    a = dev.find('Filter_Frequency/AutomationTarget')
    if a is not None:
        eq.auto[a.get('Id')] = (0, 'Freq')
    eq.notes.append('стал полосой Parametric EQ 2: резонанс приблизительно, крутизна — как у FL по умолчанию')
    if num(dev, 'Lfo_Amount/Manual') or num(dev, 'Envelope_Amount/Manual'):
        eq.notes.append('LFO и огибающая Auto Filter не переносятся')
    if num(dev, 'Filter_Drive/Manual') or abs(num(dev, 'DryWet/Manual', 1.0) - 1) > 1e-3:
        eq.notes.append('Drive и Dry/Wet Auto Filter не переносятся')
    return eq


def read_utility(dev):
    u = Utility(gain=num(dev, 'Gain/Manual', 1.0), balance=num(dev, 'Balance/Manual'), notes=[],
                params={}, targets=tuple(a.get('Id') for a in dev.iter('AutomationTarget')))
    for p in dev:
        a = p.find('AutomationTarget')
        if a is not None:
            u.params[a.get('Id')] = p.tag
    if abs(num(dev, 'StereoWidth/Manual', 1.0) - 1) > 1e-6 or val(dev, 'Mono/Manual') == 'true':
        u.notes.append('ширина/моно не переносится')
    if val(dev, 'Mute/Manual') == 'true':
        u.notes.append('кнопка Mute не переносится')
    if val(dev, 'PhaseInvertL/Manual') == 'true' or val(dev, 'PhaseInvertR/Manual') == 'true':
        u.notes.append('переворот фазы не переносится')
    return u


def file_ref(fr):
    return dict(path=val(fr, 'Path', ''), rel_path=val(fr, 'RelativePath', ''),
                size=int(val(fr, 'OriginalFileSize', '0') or 0))


def read_simpler(dev, name, key=None):
    part = dev.find('Player/MultiSampleMap/SampleParts/MultiSamplePart')
    fr = part.find('SampleRef/FileRef') if part is not None else None
    if fr is None or not (val(fr, 'Path') or val(fr, 'RelativePath')):
        return None
    frames = num(part, 'SampleRef/DefaultDuration')
    start, end = 0.0, 1.0
    if frames > 1:
        # маркеры Simpler в кадрах, ручки Start/Length — доли отрезка между ними
        s = num(part, 'SampleStart') / (frames - 1)
        e = num(part, 'SampleEnd', frames - 1) / (frames - 1)
        s += num(dev, 'Player/LoopModulators/SampleStart/Manual') * (e - s)
        e = s + num(dev, 'Player/LoopModulators/SampleLength/Manual', 1.0) * (e - s)
        start, end = max(0.0, min(1.0, s)), max(0.0, min(1.0, e))
    return Sampler(name=name, sample=file_ref(fr), root=int(num(part, 'RootKey', 60)), key=key,
                   start=start, end=end)


# имена устройств Live, как в браузере (в .als — внутренние)
LIVE_NAMES = {'AutoFilter2': 'Auto Filter', 'OriginalSimpler': 'Simpler', 'DrumGroupDevice': 'Drum Rack',
              'InstrumentGroupDevice': 'Instrument Rack', 'AudioEffectGroupDevice': 'Audio Effect Rack',
              'Erosion2': 'Erosion', 'Eq8': 'EQ Eight', 'StereoGain': 'Utility', 'DrumBuss': 'Drum Buss',
              'MxDeviceAudioEffect': 'Max for Live', 'MxDeviceInstrument': 'Max for Live',
              'MxDeviceMidiEffect': 'Max for Live', 'MultiSampler': 'Sampler', 'Hybrid': 'Hybrid Reverb',
              'Saturator2': 'Saturator', 'Compressor2': 'Compressor', 'GlueCompressor': 'Glue Compressor',
              'AutoPan2': 'Auto Pan', 'PingPongDelay': 'Ping Pong Delay'}


def device_name(dev):
    if dev.tag == 'PluginDevice':
        return (val(dev, 'PluginDesc/Vst3PluginInfo/Name') or
                val(dev, 'PluginDesc/VstPluginInfo/PlugName', 'PluginDevice') + ' (VST2)')
    return val(dev, 'UserName', '') or LIVE_NAMES.get(dev.tag, dev.tag)


def active_branches(dev, tag):
    """Цепочки стойки, которые звучат (не выключены кнопкой цепочки)."""
    return [b for b in dev.iterfind('Branches/' + tag)
            if val(b, 'MixerDevice/Speaker/Manual', 'true') == 'true']


def voices_of(t):
    """Источники звука цепочки (Simpler, слои, инструмент-плагин) с учётом MidiPitcher перед ними."""
    if t.samplers:
        for sm in t.samplers:
            if sm.plugin is None:
                sm.root -= t.transpose
            else:
                sm.transpose += t.transpose
        return t.samplers
    if t.instrument is not None:
        return [Sampler(name=t.name, plugin=t.instrument, transpose=t.transpose)]
    return []


def read_devices(devices, tr, kind, plugins, lost):
    """Устройства дорожки по порядку; стойку с одной звучащей цепочкой разворачиваем —
    её устройства идут на место стойки."""
    def skip(dev, name):
        tr.skipped.append(name)
        for a in dev.iter('AutomationTarget'):
            lost[a.get('Id')] = name

    for dev in (devices if devices is not None else []):
        if dev.find('On/Manual') is not None and val(dev, 'On/Manual') == 'false':
            tr.skipped.append('%s (выключено)' % device_name(dev))
            continue
        if dev.tag == 'MidiPitcher' and tr.instrument is None and not tr.samplers:
            tr.transpose += int(num(dev, 'Pitch/Manual'))
            continue
        if dev.tag in ('InstrumentGroupDevice', 'AudioEffectGroupDevice'):
            tag = 'InstrumentBranch' if dev.tag == 'InstrumentGroupDevice' else 'AudioEffectBranch'
            chain = 'MidiToAudioDeviceChain' if tag == 'InstrumentBranch' else 'AudioToAudioDeviceChain'
            branches = active_branches(dev, tag)
            if (len(branches) > 1 and tag == 'InstrumentBranch' and kind == 'midi'
                    and tr.instrument is None and not tr.samplers):
                # слои: каждая цепочка — свои каналы FL со своим каналом микшера
                for br in branches:
                    layer = Track(kind='midi', name=val(br, 'Name/EffectiveName', ''))
                    read_devices(br.find('DeviceChain/%s/Devices' % chain), layer, kind, plugins, lost)
                    tr.skipped += ['цепочка «%s»: %s' % (layer.name, n) for n in layer.skipped]
                    voices = voices_of(layer)
                    if not voices:
                        tr.skipped.append('цепочка «%s» в %s (нет Simpler или инструмента)'
                                          % (layer.name, device_name(dev)))
                    bus = Bus(name=layer.name, effects=layer.effects, gain=layer.gain *
                              num(br, 'MixerDevice/Volume/Manual', 1.0), pan=num(br, 'MixerDevice/Panorama/Manual'))
                    for sm in voices:
                        sm.chain.append(bus)
                    tr.samplers += voices
                for a in dev.iter('AutomationTarget'):
                    lost.setdefault(a.get('Id'), 'макросы ' + device_name(dev))
                continue
            if len(branches) != 1:
                skip(dev, '%s (%d цепочки)' % (device_name(dev), len(branches)))
                continue
            br = branches[0]
            tr.gain *= num(br, 'MixerDevice/Volume/Manual', 1.0)
            if abs(num(br, 'MixerDevice/Panorama/Manual')) > 1e-3:
                tr.skipped.append('панорама цепочки в %s' % device_name(dev))
            for a in dev.iter('AutomationTarget'):
                lost.setdefault(a.get('Id'), 'макросы ' + device_name(dev))
            read_devices(br.find('DeviceChain/%s/Devices' % chain), tr, kind, plugins, lost)
            continue
        if dev.tag == 'OriginalSimpler' and kind == 'midi' and tr.instrument is None and not tr.samplers:
            sm = read_simpler(dev, tr.name)
            if sm is not None:
                tr.samplers.append(sm)
                for a in dev.iter('AutomationTarget'):
                    lost[a.get('Id')] = 'Simpler'
                continue
        if dev.tag == 'DrumGroupDevice' and kind == 'midi' and tr.instrument is None and not tr.samplers:
            for br in active_branches(dev, 'DrumBranch'):
                # нота пэда в Live хранится как 128 − нота
                key = 128 - int(num(br, 'BranchInfo/ReceivingNote', 92))
                send = int(num(br, 'BranchInfo/SendingNote', 60))
                pad = Track(kind='midi', name=val(br, 'Name/EffectiveName', '') or 'пэд %d' % key)
                read_devices(br.find('DeviceChain/MidiToAudioDeviceChain/Devices'), pad, kind, plugins, lost)
                tr.skipped += ['пэд «%s»: %s' % (pad.name, n) for n in pad.skipped]
                voices = voices_of(pad)
                if not voices:
                    tr.skipped.append('пэд Drum Rack %d (нет Simpler или инструмента)' % key)
                    continue
                bus = Bus(name=pad.name, effects=pad.effects, gain=pad.gain * num(br, 'MixerDevice/Volume/Manual', 1.0),
                          pan=num(br, 'MixerDevice/Panorama/Manual'))
                for sm in voices:
                    sm.key = key
                    if sm.plugin is None:
                        # пэд шлёт в Simpler свою ноту (SendingNote): сводим к «играем ноту 60»
                        sm.root -= send - 60
                        if len(voices) == 1 and not sm.chain:
                            sm.name = os.path.splitext(os.path.basename(sm.sample['rel_path'] or sm.sample['path']))[0]
                    else:
                        sm.send_note = send
                    sm.chain.append(bus)
                tr.samplers += voices
            for a in dev.iter('AutomationTarget'):
                lost.setdefault(a.get('Id'), 'Drum Rack')
            continue
        if dev.tag == 'StereoGain':
            u = read_utility(dev)
            for target in u.targets:
                plugins[target] = u
            tr.effects.append(u)
            continue
        if dev.tag in ('Eq8', 'AutoFilter2'):
            eq = read_eq8(dev) if dev.tag == 'Eq8' else read_autofilter(dev)
            if eq is None:
                skip(dev, device_name(dev) + ' (полосовой/morph)')
                continue
            for target in eq.targets:
                plugins[target] = eq
            tr.effects.append(eq)
            continue
        pl = (read_vst3(dev) or read_vst2(dev)) if dev.tag == 'PluginDevice' else None
        if pl is None:
            skip(dev, device_name(dev))
            continue
        for target in pl.params:
            plugins[target] = pl
        if pl.instrument and tr.instrument is None and not tr.samplers and kind == 'midi':
            tr.instrument = pl
        else:
            tr.effects.append(pl)


def read_track(t, kind, warn):
    # Без своего имени Live показывает «1-Serum 2»: номер дорожки + имя устройства или клипа
    name = val(t, 'Name/UserName', '') or re.sub(r'^\d+-', '', val(t, 'Name/EffectiveName', ''))
    if kind == 'main':
        name = 'мастер'

    tr = Track(kind=kind, name=name, id=int(t.get('Id', -1)), group=int(num(t, 'TrackGroupId', -1)),
               color=int(num(t, 'Color', -1)))
    mixer = t.find('DeviceChain/Mixer')
    tr.gain = num(mixer, 'Volume/Manual', 1.0)
    tr.pan = num(mixer, 'Pan/Manual', 0.0)
    plugins = {}    # AutomationTarget Id -> устройство
    lost = {}       # AutomationTarget Id устройств, которые не переносятся -> имя
    read_devices(t.find('DeviceChain/DeviceChain/Devices'), tr, kind, plugins, lost)

    seq = t.find('DeviceChain/MainSequencer')
    if kind == 'midi':
        for c in seq.findall('ClipTimeable/ArrangerAutomation/Events/MidiClip'):
            if val(c, 'Disabled') == 'true':
                continue
            start, length, notes = read_notes(c)
            tr.clips.append(Clip(time=start, length=length, name=val(c, 'Name', ''), notes=notes))
    elif kind == 'audio':
        for c in seq.findall('Sample/ArrangerAutomation/Events/AudioClip'):
            if val(c, 'Disabled') != 'true':
                tr.clips.append(read_audio(c))

    mixer_targets = {}
    for what in ('Volume', 'Pan'):
        a = mixer.find(what + '/AutomationTarget') if mixer is not None else None
        if a is not None:
            mixer_targets[a.get('Id')] = what
    for env in t.findall('AutomationEnvelopes/Envelopes/AutomationEnvelope'):
        target = val(env, 'EnvelopeTarget/PointeeId')
        pts = [(float(e.get('Time')), float(e.get('Value'))) for e in env.iter('FloatEvent')]
        moving = [p for p in pts if p[0] > ENV_DEFAULT_TIME]
        if not moving:
            continue        # одна точка «по умолчанию» — это просто значение, не автоматизация
        if target in mixer_targets:
            tr.mixer_envelopes.append((mixer_targets[target], moving))
            continue
        pl = plugins.get(target)
        if pl is None:
            if target in lost:
                warn.append('«%s»: автоматизация %s не переносится вместе с устройством' % (tr.name, lost[target]))
            else:
                warn.append('«%s»: автоматизация не параметра плагина (посыл, темп…) пока не переносится' % tr.name)
            continue
        if isinstance(pl, Eq8):
            what = pl.auto.get(target)
            if what is None:
                warn.append('«%s»: у %s переносится автоматизация только частоты, усиления и Q полос'
                            % (tr.name, pl.name))
                continue
            tr.envelopes.append((pl, what, '%s %d' % (what[1], what[0] + 1), moving))
            continue
        if isinstance(pl, Utility):
            param = pl.params.get(target)
            if param not in ('Gain', 'Balance'):
                warn.append('«%s»: автоматизация %s у Utility не переносится (только Gain и Balance)'
                            % (tr.name, param))
                continue
            tr.envelopes.append((pl, param, param, moving))
            continue
        pid, lo, hi, pname = pl.params[target]
        if (pl.name, pid) in PARAM_DB:
            lo_db, hi_db = PARAM_DB[(pl.name, pid)]
            norm = [(t, max(0.0, min(1.0, (20 * math.log10(v) - lo_db) / (hi_db - lo_db))) if v > 0 else 0.0)
                    for t, v in moving]
        else:
            norm = [(t, (v - lo) / (hi - lo) if hi != lo else v) for t, v in moving]
        tr.envelopes.append((pl, pid, pname, norm))
    # автоматизация инструментов слоёв и эффектов цепочек/пэдов — к ним
    taken = set()
    for sm in tr.samplers:
        for owner, devs in [(sm, [sm.plugin])] + [(bus, bus.effects) for bus in sm.chain]:
            mine = {id(x) for x in devs}
            owner.envelopes = [e for e in tr.envelopes if id(e[0]) in mine]
            taken |= mine
    tr.envelopes = [e for e in tr.envelopes if id(e[0]) not in taken]
    return tr


def parse(data):
    root = ET.fromstring(gzip.decompress(data) if data[:2] == b'\x1f\x8b' else data)
    ls = root.find('LiveSet')
    pr = AlsProject()
    main = ls.find('MainTrack')
    if main is None:
        main = ls.find('MasterTrack')    # Live 11 и старше
    pr.tempo = num(main, 'DeviceChain/Mixer/Tempo/Manual', 120.0)
    kinds = {'MidiTrack': 'midi', 'AudioTrack': 'audio', 'GroupTrack': 'group'}
    for t in ls.find('Tracks'):
        if t.tag not in kinds:
            pr.warnings.append('дорожка «%s» (%s) не переносится' % (val(t, 'Name/EffectiveName', ''), t.tag))
            continue
        pr.tracks.append(read_track(t, kinds[t.tag], pr.warnings))
    pr.main = read_track(main, 'main', pr.warnings)
    for loc in ls.iterfind('Locators/Locators/Locator'):
        pr.locators.append((num(loc, 'Time'), val(loc, 'Name', '')))
    return pr
