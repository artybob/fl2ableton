"""python3.8 serum1to2.py <папка serum-preset-packager> <пресет.fxp> <файл состояния>
Serum 1 (.fxp) -> состояние процессора Serum 2 (VST3), как его хранит FL. Перевод делает внешний
fxp_to_serumpreset (github.com/kasperikaivola/serum-preset-packager, без лицензии — не копируем, только
запускаем); ему нужны Python 3.7+, cbor2 и zstandard, поэтому отдельный процесс, а не импорт из конвертера.
Заводские wavetable и шумы Serum берём из справочника templates/serum_assets.json (только описание файлов).
Код выхода 0 — готово, иначе причина в stderr."""
import hashlib, json, os, struct, sys
from pathlib import Path

sys.path.insert(0, sys.argv[1])
import zstandard
import cbor2
import fxp_to_serumpreset as F

ASSETS = json.load(open(os.path.join(os.path.dirname(os.path.abspath(__file__)), 'fl2ableton', 'templates',
                                     'serum_assets.json'), encoding='utf-8'))
_asset = F._asset


def asset(name, kind, roots, *, wavetable=False):
    info = ASSETS.get(kind + '/' + (name or '').replace('\\', '/').lstrip('/'))
    if info is None:
        return _asset(name, kind, roots, wavetable=wavetable)
    key = 'relativePathToWT' if kind == 'Tables' else 'relativePathToNoiseSample'
    info = {k: v for k, v in info.items() if wavetable or k != '_legacyInterpolation'}
    return dict({key: name.replace('\\', '/')}, **info)


F._asset = asset


def processor_state(data):
    """Как serumfmt.processor_state: с полем fileType Serum молча не принимает состояние."""
    data = {k: v for k, v in data.items() if k != 'fileType'}
    data['component'] = 'processor'
    raw = cbor2.dumps(data)
    z = zstandard.ZstdCompressor(level=3).compress(raw)
    meta = {'component': 'processor', 'hash': hashlib.md5(z).hexdigest(), 'product': 'Serum2',
            'productVersion': '2.1.5', 'url': 'https://xferrecords.com/', 'vendor': 'Xfer Records', 'version': 11.0}
    j = json.dumps(meta, separators=(',', ':')).encode()
    return b'XferJson\0' + struct.pack('<Q', len(j)) + j + struct.pack('<II', len(raw), 2) + z


if __name__ == '__main__':
    obj = F.translate_fxp(Path(sys.argv[2]), serum_roots=[])
    with open(sys.argv[3], 'wb') as f:
        f.write(processor_state(obj['data']))
