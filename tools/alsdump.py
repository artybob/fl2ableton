"""Краткая сводка .als: дорожки, устройства, клипы, ноты, автоматизация."""
import gzip, sys, xml.etree.ElementTree as ET
r = ET.fromstring(gzip.open(sys.argv[1]).read()); ls = r.find('LiveSet')
print('tempo', ls.find('MainTrack/DeviceChain/Mixer/Tempo/Manual').get('Value'))
params = {}
for p in r.iter('PluginFloatParameter'):
    params[p.find('ParameterValue/AutomationTarget').get('Id')] = p.find('ParameterId').get('Value')
for t in list(ls.find('Tracks')) + [ls.find('MainTrack')]:
    mx = t.find('DeviceChain/Mixer')
    print(t.tag, t.find('Name/EffectiveName').get('Value'), 'vol', mx.find('Volume/Manual').get('Value'))
    for d in t.find('DeviceChain/DeviceChain/Devices'):
        i = d.find('PluginDesc/Vst3PluginInfo')
        if i is None: print('   dev', d.tag); continue
        ps = i.find('Preset/Vst3Preset')
        print('   vst3', i.find('Name').get('Value'), [f.get('Value') for f in i.find('Uid')], 'type', i.find('DeviceType').get('Value'),
              'state', len(''.join((ps.find('ProcessorState').text or '').split())) // 2, len(''.join((ps.find('ControllerState').text or '').split())) // 2)
    for c in t.findall('DeviceChain/MainSequencer/ClipTimeable/ArrangerAutomation/Events/MidiClip') + t.findall('DeviceChain/MainSequencer/Sample/ArrangerAutomation/Events/AudioClip'):
        notes = [(kt.find('MidiKey').get('Value'), n.get('Time'), n.get('Duration'), n.get('Velocity')) for kt in c.iter('KeyTrack') for n in kt.iter('MidiNoteEvent')]
        print('   clip', c.tag, c.get('Time'), c.find('CurrentEnd').get('Value'), 'loop', c.find('Loop/LoopStart').get('Value'), c.find('Loop/LoopEnd').get('Value'), 'notes', notes[:3], len(notes),
              [(w.get('SecTime'), w.get('BeatTime')) for w in c.iter('WarpMarker')][:2])
    for e in t.find('AutomationEnvelopes/Envelopes'):
        pid = e.find('EnvelopeTarget/PointeeId').get('Value')
        print('   env ->', 'param ' + params[pid] if pid in params else pid, [(f.get('Time'), f.get('Value')) for f in e.iter('FloatEvent')])
