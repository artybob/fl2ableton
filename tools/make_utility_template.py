"""Заготовка Utility (StereoGain) из проекта Ableton; образец ref4 (UTBL.als, дорожка 2).

python3 -I tools/make_utility_template.py проект.als fl2ableton/templates
"""
import copy, gzip, os, sys
import xml.etree.ElementTree as ET

src, out = sys.argv[1], sys.argv[2]
ls = ET.fromstring(gzip.open(src).read()).find('LiveSet')
# у первой дорожки образца Utility с автоматизацией — берём последний, без неё
dev = copy.deepcopy(list(ls.iter('StereoGain'))[-1])
for name in ('Gain', 'Balance', 'StereoWidth'):
    dev.find(name + '/Manual').set('Value', '1' if name != 'Balance' else '0')
ET.ElementTree(dev).write(os.path.join(out, 'utility.xml'), encoding='utf-8', xml_declaration=True)
print('utility.xml', os.path.getsize(os.path.join(out, 'utility.xml')))
