import struct, sys
sys.path.insert(0, sys.argv[0].rsplit('/',1)[0])
from flpdump import read_events

def chunks(d):
    hdr = struct.unpack('<I', d[:4])[0]
    p = 4; out = []
    while p + 12 <= len(d):
        t, n = struct.unpack('<IQ', d[p:p+12]); p += 12
        out.append((t, d[p:p+n])); p += n
    return hdr, out, p == len(d)

if __name__ == '__main__':
    _, ev = read_events(sys.argv[1])
    for i, (eid, v) in enumerate(ev):
        if eid == 213 and v[:4] == b'\x0c\0\0\0':
            hdr, ch, ok = chunks(v)
            print('== event', i, 'len', len(v), 'hdr', hdr, 'clean', ok)
            for t, d in ch:
                s = ''
                try:
                    if len(d) > 2 and d[1::2].count(0) >= len(d)//2 - 1: s = repr(d.decode('utf-16-le'))
                except Exception: pass
                print('   type', t, 'len', len(d), s or d[:40].hex())
