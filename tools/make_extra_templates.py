"""Заготовки группы, EQ Eight, Sampler и VST2-устройства из проекта Ableton, где они есть
(образец ref3: группа BASS_GROUP с EQ Eight, дорожка SAMPLER, VST2_VITAL).

python3 -I tools/make_extra_templates.py ref.als fl2ableton/templates
"""
import copy, gzip, os, sys
import xml.etree.ElementTree as ET

src, out = sys.argv[1], sys.argv[2]


def save(el, name):
    ET.ElementTree(el).write(os.path.join(out, name), encoding='utf-8', xml_declaration=True)


def clear(el):
    if el is not None:
        for c in list(el):
            el.remove(c)


ls = ET.fromstring(gzip.open(src).read()).find('LiveSet')
tracks = ls.find('Tracks')

group = copy.deepcopy(next(t for t in tracks if t.tag == 'GroupTrack'))
eq8 = copy.deepcopy(next(d for t in tracks for d in t.iter('Eq8')))
clear(group.find('DeviceChain/DeviceChain/Devices'))
clear(group.find('AutomationEnvelopes/Envelopes'))
clear(group.find('TakeLanes/TakeLanes'))
# посылы образца ссылаются на его Return-дорожки, у нас их нет — Live не откроет
clear(group.find('DeviceChain/Mixer/Sends'))

sampler = copy.deepcopy(next(d for t in tracks for d in t.iter('MultiSampler')))
for sr in sampler.iter('SampleRef'):
    sc = sr.find('SourceContext')
    if sc is not None:
        sr.remove(sc)

vst2 = None
for t in tracks:
    for d in t.iter('PluginDevice'):
        info = d.find('PluginDesc/VstPluginInfo')
        if info is not None and info.find('Category').get('Value') == '1':
            vst2 = copy.deepcopy(d)
clear(vst2.find('ParameterList'))

save(group, 'grouptrack.xml')
save(eq8, 'eq8.xml')
save(sampler, 'sampler.xml')
save(vst2, 'vst2device.xml')
for f in ('grouptrack.xml', 'eq8.xml', 'sampler.xml', 'vst2device.xml'):
    print(f, os.path.getsize(os.path.join(out, f)))
