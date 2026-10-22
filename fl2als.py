"""python3 -I fl2als.py <проект.flp | папка | zip> <папка для результата>
python3 -I fl2als.py --zip <архив.zip> <результат.zip> <рабочая папка> [версия FL: 26 | 25]
    — для сайта: zip на входе и на выходе, итог в <результат.zip>.json. Направление по содержимому:
    в архиве .flp — получится проект Ableton, .als — проект FL Studio (для FL 26 или 25.2.5)."""
import json, os, shutil, sys, tempfile, traceback, zipfile
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from fl2ableton.convert import convert, zip_name
from fl2ableton import reverse

# уже сжатое нет смысла жать ещё раз, только время на слабом сервере
STORED = ('.wav', '.aif', '.aiff', '.mp3', '.flac', '.ogg', '.zip')


def zip_dir(src, out):
    with zipfile.ZipFile(out, 'w', allowZip64=True) as z:
        for dp, dirs, files in os.walk(src):
            rel = os.path.relpath(dp, os.path.dirname(src))
            if not files and not dirs:
                z.write(dp, rel)   # пустая папка тоже нужна (Ableton Project Info)
            for f in files:
                kind = zipfile.ZIP_STORED if f.lower().endswith(STORED) else zipfile.ZIP_DEFLATED
                z.write(os.path.join(dp, f), os.path.join(rel, f), kind)


def direction(src):
    """'fl2als' или 'als2fl' — что лежит в архиве (папки Backup не в счёт)."""
    with zipfile.ZipFile(src) as z:
        names = [zip_name(i).lower() for i in z.infolist() if 'backup/' not in zip_name(i).lower()]
    if any(n.endswith('.flp') for n in names):
        return 'fl2als'
    if any(n.endswith('.als') for n in names):
        return 'als2fl'
    raise SystemExit('в архиве нет ни .flp, ни .als')


def zip_mode(src, out_zip, work, fl_version='26'):
    """Входной архив удаляется после распаковки, сэмплы переносятся — на диске одновременно
    не больше двух копий проекта."""
    res = {'ok': False, 'warnings': []}
    tmp = tempfile.mkdtemp(prefix='job-', dir=work)
    try:
        res['direction'] = direction(src)
        kw = dict(work_dir=tmp, move_samples=True, remove_src=True)
        if res['direction'] == 'fl2als':
            result, res['warnings'] = convert(src, os.path.join(tmp, 'out'), **kw)
        else:
            result, res['warnings'] = reverse.convert(src, os.path.join(tmp, 'out'), version=fl_version, **kw)
        proj = os.path.dirname(result)
        zip_dir(proj, out_zip + '.part')
        os.rename(out_zip + '.part', out_zip)
        res.update(ok=True, name=os.path.basename(proj))
    except BaseException as e:
        res['error'] = str(e) or e.__class__.__name__
        res['trace'] = traceback.format_exc()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    with open(out_zip + '.json.part', 'w', encoding='utf-8') as f:
        json.dump(res, f, ensure_ascii=False)
    os.rename(out_zip + '.json.part', out_zip + '.json')
    return res['ok']


if __name__ == '__main__':
    if len(sys.argv) in (5, 6) and sys.argv[1] == '--zip':
        sys.exit(0 if zip_mode(*sys.argv[2:]) else 1)
    if len(sys.argv) != 3:
        raise SystemExit(__doc__)
    out, warnings = convert(sys.argv[1], sys.argv[2])
    print('готово:', out)
    for w in warnings:
        print('  ! ' + w)
