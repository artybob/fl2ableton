import struct, sys, collections

def read_events(path):
    b = open(path, 'rb').read()
    assert b[:4] == b'FLhd'
    hl = struct.unpack('<I', b[4:8])[0]
    fmt, nch, ppq = struct.unpack('<HHH', b[8:14])
    p = 8 + hl
    assert b[p:p+4] == b'FLdt'
    dl = struct.unpack('<I', b[p+4:p+8])[0]
    p += 8
    end = p + dl
    ev = []
    while p < end:
        eid = b[p]; p += 1
        if eid < 64:
            v = b[p]; p += 1
        elif eid < 128:
            v = struct.unpack('<H', b[p:p+2])[0]; p += 2
        elif eid == 172:
            # FL 25.2+ пишет 172 тремя байтами, хотя id из диапазона DWORD
            v = int.from_bytes(b[p:p+3], 'little'); p += 3
        elif eid < 192:
            v = struct.unpack('<I', b[p:p+4])[0]; p += 4
        else:
            n = 0; sh = 0
            while True:
                c = b[p]; p += 1
                n |= (c & 0x7f) << sh; sh += 7
                if not c & 0x80: break
            v = b[p:p+n]; p += n
        ev.append((eid, v))
    return (fmt, nch, ppq), ev

if __name__ == '__main__':
    hdr, ev = read_events(sys.argv[1])
    print('header', hdr, 'events', len(ev))
    for i, (eid, v) in enumerate(ev):
        if isinstance(v, bytes):
            s = ''
            try:
                if len(v) >= 2 and v[1::2].count(0) >= len(v)//2 - 1:
                    s = v.decode('utf-16-le').rstrip('\0')
            except Exception: pass
            print(i, eid, 'len=%d' % len(v), repr(s) if s else v[:48].hex())
        else:
            print(i, eid, v)
