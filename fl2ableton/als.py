"""Сборка проекта Ableton (.als) из заготовок, вырезанных из настоящего проекта Live 12."""
import copy, gzip, math, os, re, struct, sys
import xml.etree.ElementTree as ET
from urllib.parse import quote

TPL = os.path.join(os.path.dirname(__file__), 'templates')
# Эти Id сквозные на весь проект, остальные нумеруются внутри своего списка
GLOBAL_ID = re.compile(r'(Target|Pointee)$|^ControllerTargets\.\d+$')
ENV_START = '-63072000'
if sys.version_info < (3, 8):
    # До 3.8 ElementTree сортирует атрибуты при записи — держим порядок, как у Live
    ET.sorted = lambda items: items


def load(name):
    return ET.parse(os.path.join(TPL, name)).getroot()


def setv(el, path, value):
    el.find(path).set('Value', str(value))


def num(x):
    return ('%.10f' % x).rstrip('0').rstrip('.') if isinstance(x, float) else str(x)


def s32(x):
    return x - (1 << 32) if x >= 1 << 31 else x


def hexblock(b):
    h = b.hex().upper()
    return '\n' + '\n'.join(h[i:i+80] for i in range(0, len(h), 80)) + '\n' if h else ''


def guid_fields(g):
    """Байты GUID в порядке FL (Windows) -> четыре поля Uid и строка для BranchDeviceId."""
    d1, d2, d3 = struct.unpack('<IHH', g[:8])
    d4 = g[8:]
    fields = [d1, (d2 << 16) | d3, int.from_bytes(d4[:4], 'big'), int.from_bytes(d4[4:], 'big')]
    text = '%08x-%04x-%04x-%s-%s' % (d1, d2, d3, d4[:2].hex(), d4[2:].hex())
    return [s32(f) for f in fields], text


class AlsBuilder:
    def __init__(self, tempo):
        self.root = load('base.xml')
        self.ls = self.root.find('LiveSet')
        self.next_id = int(self.ls.find('NextPointeeId').get('Value'))
        self.track_id = 10
        self.tpl = {n: load(n + '.xml') for n in
                    ('miditrack', 'audiotrack', 'grouptrack', 'plugindevice', 'vst2device', 'pluginparam',
                     'midiclip', 'audioclip', 'eq8', 'sampler', 'utility')}
        mixer = self.ls.find('MainTrack/DeviceChain/Mixer')
        setv(mixer, 'Tempo/Manual', num(float(tempo)))
        tempo_id = mixer.find('Tempo/AutomationTarget').get('Id')
        for env in self.ls.find('MainTrack/AutomationEnvelopes/Envelopes'):
            if env.find('EnvelopeTarget/PointeeId').get('Value') == tempo_id:
                for fe in env.iter('FloatEvent'):
                    fe.set('Value', num(float(tempo)))

    def clone(self, name):
        el = copy.deepcopy(self.tpl[name])
        for e in el.iter():
            if 'Id' in e.attrib and GLOBAL_ID.search(e.tag):
                e.set('Id', str(self.next_id))
                self.next_id += 1
        return el

    # ---------- дорожки ----------
    def add_track(self, kind, name, color=None):
        """kind: 'midi', 'audio' или 'group'."""
        t = self.clone({'midi': 'miditrack', 'group': 'grouptrack'}.get(kind, 'audiotrack'))
        t.set('Id', str(self.track_id))
        self.track_id += 1
        setv(t, 'Name/EffectiveName', name)
        setv(t, 'Name/UserName', name)
        # в заготовке MIDI-дорожки осталось −5 дБ из проекта-образца
        setv(t, 'DeviceChain/Mixer/Volume/Manual', 1)
        if color is not None:
            setv(t, 'Color', color)
        self.ls.find('Tracks').append(t)
        return t

    def set_group(self, track, group):
        setv(track, 'TrackGroupId', group.get('Id'))
        out = track.find('DeviceChain/AudioOutputRouting')
        setv(out, 'Target', 'AudioOut/GroupTrack')
        setv(out, 'UpperDisplayString', 'Group')

    def order_tracks(self, order):
        """Live требует, чтобы дорожки группы шли сразу за ней."""
        tracks = self.ls.find('Tracks')
        rest = [t for t in tracks if t not in order]
        for t in list(tracks):
            tracks.remove(t)
        for t in list(order) + rest:
            tracks.append(t)

    def mixer_target(self, track, what):
        """Id цели автоматизации громкости ('Volume') или панорамы ('Pan') дорожки."""
        return track.find('DeviceChain/Mixer/%s/AutomationTarget' % what).get('Id')

    def main_track(self):
        return self.ls.find('MainTrack')

    def set_mixer(self, track, gain=None, pan=None):
        mixer = track.find('DeviceChain/Mixer')
        if gain is not None:
            setv(mixer, 'Volume/Manual', num(gain))
        if pan is not None:
            setv(mixer, 'Pan/Manual', num(pan))

    # ---------- плагины ----------
    def add_vst3(self, track, plugin, instrument):
        devices = track.find('DeviceChain/DeviceChain/Devices')
        d = self.clone('plugindevice')
        d.set('Id', str(len(devices)))
        fields, text = guid_fields(plugin.guid)
        kind = 'instr' if instrument else 'audiofx'
        bsc = d.find('SourceContext/Value/BranchSourceContext')
        setv(bsc, 'BranchDeviceId', 'device:vst3:%s:%s' % (kind, text))
        setv(bsc, 'BrowserContentPath', 'query:Plugins#VST3:%s:%s' % (quote(plugin.vendor), quote(plugin.name)))
        info = d.find('PluginDesc/Vst3PluginInfo')
        setv(info, 'Name', plugin.name)
        setv(info, 'NumAudioInputs', 0 if instrument else 1)
        for node in (info, info.find('Preset/Vst3Preset')):
            setv(node, 'DeviceType', 1 if instrument else 2)
            for i, f in enumerate(fields):
                setv(node, 'Uid/Fields.%d' % i, f)
        preset = info.find('Preset/Vst3Preset')
        preset.find('ProcessorState').text = hexblock(plugin.processor)
        preset.find('ControllerState').text = hexblock(plugin.controller)
        devices.append(d)
        return d

    def add_vst2(self, track, plugin, instrument):
        """VST2 с чанком состояния (как его отдаёт сам плагин). Путь к .dll — тот, что был у FL."""
        devices = track.find('DeviceChain/DeviceChain/Devices')
        d = self.clone('vst2device')
        d.set('Id', str(len(devices)))
        kind = 'instr' if instrument else 'audiofx'
        bsc = d.find('SourceContext/Value/BranchSourceContext')
        setv(bsc, 'BranchDeviceId', 'device:vst:%s:%d?n=%s' % (kind, plugin.vst2_id, quote(plugin.name)))
        setv(bsc, 'BrowserContentPath', 'view:X-Plugins#%s' % quote(plugin.name))
        info = d.find('PluginDesc/VstPluginInfo')
        setv(info, 'Path', plugin.path.replace('\\', '/'))
        setv(info, 'PlugName', plugin.name)
        setv(info, 'NumAudioInputs', 0 if instrument else 1)
        setv(info, 'Category', 2 if instrument else 1)
        preset = info.find('Preset/VstPreset')
        for node in (info, preset):
            setv(node, 'UniqueId', s32(plugin.vst2_id))
        preset.find('Buffer').text = hexblock(plugin.state)
        devices.append(d)
        return d

    def add_eq8(self, track, bands):
        """bands: до 8 штук (вкл, режим EQ Eight, частота, усиление дБ, Q); остальные полосы выключены."""
        devices = track.find('DeviceChain/DeviceChain/Devices')
        d = self.clone('eq8')
        d.set('Id', str(len(devices)))
        for i in range(8):
            pa = d.find('Bands.%d/ParameterA' % i)
            on, mode, freq, gain, q = bands[i] if i < len(bands) else (False, 3, 1000.0, 0.0, 0.7071067095)
            setv(pa, 'IsOn/Manual', 'true' if on else 'false')
            setv(pa, 'Mode/Manual', mode)
            setv(pa, 'Freq/Manual', num(float(freq)))
            setv(pa, 'Gain/Manual', num(float(gain)))
            setv(pa, 'Q/Manual', num(float(q)))
        devices.append(d)
        return d

    def add_utility(self, track, gain, balance):
        """Utility: громкость (множитель) и баланс -1..1."""
        devices = track.find('DeviceChain/DeviceChain/Devices')
        d = self.clone('utility')
        d.set('Id', str(len(devices)))
        setv(d, 'Gain/Manual', num(float(gain)))
        setv(d, 'Balance/Manual', num(float(balance)))
        devices.append(d)
        return d

    def add_sampler(self, track, name, sample, start=0.0, end=1.0):
        """Sampler с одним сэмплом на всю клавиатуру, корень — C3 (нота 60, как C5 у FL).
        start, end — звучащий отрезок, доли файла."""
        devices = track.find('DeviceChain/DeviceChain/Devices')
        d = self.clone('sampler')
        d.set('Id', str(len(devices)))
        part = d.find('Player/MultiSampleMap/SampleParts/MultiSamplePart')
        setv(part, 'Name', name)
        last = sample['frames'] - 1
        first = int(round(max(0.0, min(1.0, start)) * last))
        last = max(first + 1, int(round(max(0.0, min(1.0, end)) * last)))
        for path in ('SampleStart', 'SustainLoop/Start', 'ReleaseLoop/Start'):
            setv(part, path, first)
        for path in ('SampleEnd', 'SustainLoop/End', 'ReleaseLoop/End'):
            setv(part, path, last)
        self._sample_ref(part.find('SampleRef'), sample)
        devices.append(d)
        return d

    def set_sidechain(self, device, source):
        """Сайдчейн плагина — выход дорожки source после её эффектов."""
        r = device.find('SideChain/RoutedInput/Routable')
        setv(r, 'Target', 'AudioIn/Track.%s/PostFxOut' % source.get('Id'))
        setv(r, 'UpperDisplayString', source.find('Name/EffectiveName').get('Value'))
        setv(r, 'LowerDisplayString', 'Post FX')

    def add_param(self, device, param_id, value, name='', lo=0, hi=1):
        """Параметр плагина для автоматизации; value и диапазон — в шкале, которую показывает Ableton
        (у Serum 2 это 0..100, внутри плагина всё равно 0..1). Возвращает id цели."""
        plist = device.find('ParameterList')
        p = self.clone('pluginparam')
        p.set('Id', str(len(plist)))
        setv(p, 'ParameterName', name or 'Param %d' % param_id)
        setv(p, 'ParameterId', param_id)
        setv(p, 'VisualIndex', len(plist))
        pv = p.find('ParameterValue')
        setv(pv, 'Manual', num(float(value)))
        for path, v in (('MidiControllerRange/Min', lo), ('MidiControllerRange/Max', hi)):
            setv(pv, path, v)
        for path, v in (('LastUserRange/First', lo), ('LastUserRange/Last', hi),
                        ('LastInternalRange/First', 0), ('LastInternalRange/Last', 1)):
            setv(p, path, v)
        plist.append(p)
        return pv.find('AutomationTarget').get('Id')

    def add_envelope(self, track, target_id, points):
        envs = track.find('AutomationEnvelopes/Envelopes')
        env = ET.SubElement(envs, 'AutomationEnvelope', Id=str(len(envs)))
        ET.SubElement(ET.SubElement(env, 'EnvelopeTarget'), 'PointeeId', Value=str(target_id))
        auto = ET.SubElement(env, 'Automation')
        events = ET.SubElement(auto, 'Events')
        ET.SubElement(events, 'FloatEvent', Id='0', Time=ENV_START, Value=num(float(points[0][1])))
        for i, (t, v) in enumerate(points, 1):
            ET.SubElement(events, 'FloatEvent', Id=str(i), Time=num(float(t)), Value=num(float(v)))
        tv = ET.SubElement(auto, 'AutomationTransformViewState')
        ET.SubElement(tv, 'IsTransformPending', Value='false')
        ET.SubElement(tv, 'TimeAndValueTransforms')

    # ---------- клипы ----------
    def _clip_times(self, c, time, length, loop_start, loop_len=None):
        """time, length — доли на дорожке; loop_* — в единицах содержимого (у аудио без растяжки — секунды)."""
        if loop_len is None:
            loop_len = length
        c.set('Time', num(time))
        setv(c, 'CurrentStart', num(time))
        setv(c, 'CurrentEnd', num(time + length))
        setv(c, 'Loop/LoopStart', num(loop_start))
        setv(c, 'Loop/LoopEnd', num(loop_start + loop_len))
        setv(c, 'Loop/StartRelative', 0)
        setv(c, 'Loop/LoopOn', 'false')
        setv(c, 'Loop/OutMarker', num(loop_start + loop_len))
        setv(c, 'Loop/HiddenLoopStart', num(loop_start))
        setv(c, 'Loop/HiddenLoopEnd', num(loop_start + loop_len))

    def _sample_ref(self, sr, sample):
        fr = sr.find('FileRef')
        setv(fr, 'RelativePathType', 3)
        setv(fr, 'RelativePath', sample['rel_path'])
        setv(fr, 'Path', sample['rel_path'])
        setv(fr, 'OriginalFileSize', sample['size'])
        setv(fr, 'OriginalCrc', sample['crc'])
        setv(sr, 'LastModDate', int(sample['mtime']))
        setv(sr, 'DefaultDuration', sample['frames'])
        setv(sr, 'DefaultSampleRate', sample['rate'])

    def add_midi_clip(self, track, name, time, length, loop_start, notes, color=None):
        """notes: [(время в долях от начала содержимого, длительность, нота, скорость)]"""
        events = track.find('DeviceChain/MainSequencer/ClipTimeable/ArrangerAutomation/Events')
        c = self.clone('midiclip')
        c.set('Id', str(len(events)))
        self._clip_times(c, time, length, loop_start)
        setv(c, 'Name', name)
        if color is not None:
            setv(c, 'Color', color)
        setv(c, 'GrooveSettings/GrooveId', -1)
        keytracks = c.find('Notes/KeyTracks')
        note_id = 1
        for i, key in enumerate(sorted({n[2] for n in notes})):
            kt = ET.SubElement(keytracks, 'KeyTrack', Id=str(i))
            ns = ET.SubElement(kt, 'Notes')
            for t, d, k, vel in sorted(n for n in notes if n[2] == key):
                ET.SubElement(ns, 'MidiNoteEvent', Time=num(float(t)), Duration=num(float(d)),
                              Velocity=str(vel), OffVelocity='64', NoteId=str(note_id))
                note_id += 1
            ET.SubElement(kt, 'MidiKey', Value=str(key))
        setv(c, 'Notes/NoteIdGenerator/NextId', note_id)
        events.append(c)
        return c

    def add_audio_clip(self, track, name, time, length, loop_start, sample, color=None,
                       warped=True, loop_len=None, pitch=0, warp_mode=None):
        """sample: dict(rel_path, size, mtime, crc, frames, rate, sec, beats) — beats: сколько долей
        занимает весь файл после растяжки. Без растяжки (warped=False) loop_* — в секундах.
        pitch — центы; сдвиг высоты в Live возможен только с растяжкой. warp_mode: 0 Beats, 3 Re-Pitch, 6 Complex Pro."""
        events = track.find('DeviceChain/MainSequencer/Sample/ArrangerAutomation/Events')
        c = self.clone('audioclip')
        c.set('Id', str(len(events)))
        self._clip_times(c, time, length, loop_start, loop_len)
        setv(c, 'Name', name)
        if color is not None:
            setv(c, 'Color', color)
        self._sample_ref(c.find('SampleRef'), sample)
        setv(c, 'IsWarped', 'true' if warped else 'false')
        if pitch:
            coarse = int(round(pitch / 100))
            setv(c, 'PitchCoarse', coarse)
            setv(c, 'PitchFine', int(round(pitch - coarse * 100)))
        if warp_mode is not None:
            setv(c, 'WarpMode', warp_mode)
        wm = c.find('WarpMarkers')
        sec, beats = sample['sec'], sample['beats']
        tail = 1 / 32
        for i, (s, b) in enumerate(((0, 0), (sec, beats), (sec + tail * sec / beats, beats + tail))):
            ET.SubElement(wm, 'WarpMarker', Id=str(i), SecTime=num(float(s)), BeatTime=num(float(b)))
        events.append(c)
        return c

    def add_locator(self, time, name):
        locs = self.ls.find('Locators/Locators')
        loc = ET.SubElement(locs, 'Locator', Id=str(len(locs)))
        ET.SubElement(loc, 'LomId', Value='0')
        ET.SubElement(loc, 'Time', Value=num(float(time)))
        ET.SubElement(loc, 'Name', Value=name)
        ET.SubElement(loc, 'Annotation', Value='')
        ET.SubElement(loc, 'IsSongStart', Value='false')

    # ---------- запись ----------
    def write(self, path):
        top = 0
        for e in self.root.iter():
            if 'Id' in e.attrib and GLOBAL_ID.search(e.tag):
                top = max(top, int(e.get('Id')))
        setv(self.ls, 'NextPointeeId', max(self.next_id, top + 1))
        body = ET.tostring(self.root, encoding='unicode')
        with gzip.open(path, 'wb') as f:
            f.write(b'<?xml version="1.0" encoding="UTF-8"?>\n' + body.encode('utf-8'))
