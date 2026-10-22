"""Запись проекта FL Studio (.flp): кодирование событий и сборка из заготовок настоящего проекта."""
import struct


def encode_event(eid, v):
    if eid < 64:
        return bytes((eid, v))
    if eid < 128:
        return bytes((eid,)) + struct.pack('<H', v)
    if eid == 172:
        # FL 25.2+ пишет 172 тремя байтами (см. read_events)
        return bytes((eid,)) + v.to_bytes(3, 'little')
    if eid < 192:
        return bytes((eid,)) + struct.pack('<I', v)
    n, out = len(v), bytearray((eid,))
    while True:
        c = n & 0x7f
        n >>= 7
        out.append(c | 0x80 if n else c)
        if not n:
            break
    return bytes(out) + v


def encode_events(events):
    return b''.join(encode_event(e, v) for e, v in events)


def write_flp(events, ppq, channels, fmt=0):
    """Файл целиком: заголовок FLhd (формат, число каналов, PPQ) и блок данных FLdt."""
    body = encode_events(events)
    return (b'FLhd' + struct.pack('<IHHH', 6, fmt, channels, ppq)
            + b'FLdt' + struct.pack('<I', len(body)) + body)
