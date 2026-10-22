"""Вырезает заготовки из настоящего проекта Ableton (в нём должны быть
MIDI-дорожка с VST3-эффектом, аудиодорожка с клипом и MIDI-клип в аранжировке).

python3 -I tools/make_templates.py ref.als fl2ableton/templates
"""
import copy, gzip, os, sys
import xml.etree.ElementTree as ET

src, out = sys.argv[1], sys.argv[2]
os.makedirs(out, exist_ok=True)
root = ET.fromstring(gzip.open(src).read())
ls = root.find('LiveSet')
tracks = ls.find('Tracks')


def save(el, name):
    ET.ElementTree(el).write(os.path.join(out, name), encoding='utf-8', xml_declaration=True)


def clear(el):
    if el is not None:
        for c in list(el):
            el.remove(c)


def strip_track(t):
    clear(t.find('DeviceChain/DeviceChain/Devices'))
    clear(t.find('AutomationEnvelopes/Envelopes'))
    clear(t.find('TakeLanes/TakeLanes'))
    for p in ('DeviceChain/MainSequencer/ClipTimeable/ArrangerAutomation/Events',
              'DeviceChain/MainSequencer/Sample/ArrangerAutomation/Events'):
        clear(t.find(p))
    for slot in t.iter('ClipSlot'):
        v = slot.find('ClipSlot/Value')
        clear(v)
    return t


midi = next(t for t in tracks if t.tag == 'MidiTrack')
audio = next(t for t in tracks if t.tag == 'AudioTrack')

plugin = None
for t in tracks:
    for d in t.iter('PluginDevice'):
        if d.find('PluginDesc/Vst3PluginInfo') is not None and d.find('PluginDesc/Vst3PluginInfo/DeviceType').get('Value') == '2':
            plugin = copy.deepcopy(d)
            break
param = copy.deepcopy(next(p for p in plugin.iter('PluginFloatParameter') if p.find('ParameterId').get('Value') != '-1'))
clear(plugin.find('ParameterList'))

midiclip = copy.deepcopy(next(c for t in tracks for c in t.findall(
    'DeviceChain/MainSequencer/ClipTimeable/ArrangerAutomation/Events/MidiClip')))
clear(midiclip.find('Notes/KeyTracks'))
audioclip = copy.deepcopy(audio.find('DeviceChain/MainSequencer/Sample/ArrangerAutomation/Events/AudioClip'))
clear(audioclip.find('WarpMarkers'))
sc = audioclip.find('SampleRef/SourceContext')
if sc is not None:
    audioclip.find('SampleRef').remove(sc)

save(strip_track(copy.deepcopy(midi)), 'miditrack.xml')
save(strip_track(copy.deepcopy(audio)), 'audiotrack.xml')
save(plugin, 'plugindevice.xml')
save(param, 'pluginparam.xml')
save(midiclip, 'midiclip.xml')
save(audioclip, 'audioclip.xml')

clear(tracks)
clear(ls.find('MainTrack/DeviceChain/DeviceChain/Devices'))
clear(ls.find('Locators/Locators'))
save(root, 'base.xml')
for f in sorted(os.listdir(out)):
    print(f, os.path.getsize(os.path.join(out, f)))
