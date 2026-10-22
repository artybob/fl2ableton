"""python3 -I als2flp.py [--fl 25|26] <проект.als | папка проекта Live | zip> <папка для результата>
    --fl 25 — для FL Studio 25.2.5 (по умолчанию 26)"""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from fl2ableton.reverse import convert, FL_VERSIONS

if __name__ == '__main__':
    args = sys.argv[1:]
    version = '26'
    if args[:1] == ['--fl'] and len(args) > 1:
        version, args = args[1], args[2:]
    if len(args) != 2 or version not in FL_VERSIONS:
        raise SystemExit(__doc__)
    out, warnings = convert(args[0], args[1], version=version)
    print('готово:', out)
    for w in warnings:
        print('  ! ' + w)
