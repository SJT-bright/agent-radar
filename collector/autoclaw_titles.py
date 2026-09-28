"""Bounded, read-only LevelDB decoding for ONE AutoClaw localStorage key.

No database open/repair, credential parsing, or raw storage output. Chromium's
live WAL and SST tables are merged by sequence; partial WAL records are ignored.
Only session key/displayName pairs leave this module.
"""
import json
import struct
from pathlib import Path

KEY = b'_file://\x00\x01autoclaw.localSessions.v1'
LIMIT = 8 * 1024 * 1024


def checksum(data):
    crc = 0xffffffff
    for b in data:
        crc ^= b
        for _ in range(8):
            crc = (crc >> 1) ^ (0x82f63b78 if crc & 1 else 0)
    crc ^= 0xffffffff
    return (((crc >> 15) | (crc << 17)) + 0xa282ead8) & 0xffffffff


def varint(data, pos):
    value = shift = 0
    for _ in range(10):
        b = data[pos]
        pos += 1
        value |= (b & 127) << shift
        if b < 128:
            return value, pos
        shift += 7
    raise ValueError('varint')


def snappy(data):
    size, p = varint(data, 0)
    if size > LIMIT:
        raise ValueError('block limit')
    out = bytearray()
    while p < len(data) and len(out) < size:
        tag = data[p]
        p += 1
        kind = tag & 3
        if kind == 0:
            n = tag >> 2
            if n >= 60:
                count = n - 59
                n = int.from_bytes(data[p:p + count], 'little')
                p += count
            n += 1
            out.extend(data[p:p + n])
            p += n
        else:
            if kind == 1:
                n = 4 + ((tag >> 2) & 7)
                offset = ((tag & 224) << 3) | data[p]
                p += 1
            else:
                count = 2 if kind == 2 else 4
                n = 1 + (tag >> 2)
                offset = int.from_bytes(data[p:p + count], 'little')
                p += count
            if offset < 1 or offset > len(out):
                raise ValueError('copy offset')
            for _ in range(n):
                out.append(out[-offset])
        if len(out) > size:
            raise ValueError('size')
    if len(out) != size:
        raise ValueError('truncated')
    return bytes(out)


def block(data, handle):
    offset, p = varint(handle, 0)
    size, _ = varint(handle, p)
    if size > LIMIT or offset + size + 5 > len(data):
        raise ValueError('block range')
    raw, compression = data[offset:offset + size], data[offset + size]
    if checksum(data[offset:offset + size + 1]) != int.from_bytes(data[offset + size + 1:offset + size + 5], 'little'):
        raise ValueError('block checksum')
    if compression == 1:
        return snappy(raw)
    if compression != 0:
        raise ValueError('compression')
    return raw


def entries(data):
    count = struct.unpack('<I', data[-4:])[0]
    end = len(data) - 4 - count * 4
    if end < 0:
        raise ValueError('restarts')
    p, key = 0, b''
    while p < end:
        shared, p = varint(data, p)
        unshared, p = varint(data, p)
        length, p = varint(data, p)
        if shared > len(key) or p + unshared + length > end:
            raise ValueError('entry')
        key = key[:shared] + data[p:p + unshared]
        p += unshared
        value = data[p:p + length]
        p += length
        yield key, value


def table_values(data):
    if data[-8:] != bytes.fromhex('57fb808b247547db'):
        raise ValueError('table magic')
    footer = data[-48:-8]
    _, p = varint(footer, 0)
    _, p = varint(footer, p)
    for _, handle in entries(block(data, footer[p:])):
        for key, value in entries(block(data, handle)):
            if len(key) > 8 and key[:-8] == KEY:
                tag = int.from_bytes(key[-8:], 'little')
                yield tag >> 8, value if tag & 255 == 1 else None


def log_values(data):
    assembled = bytearray()
    for start in range(0, len(data), 32768):
        p, end = start, min(start + 32768, len(data))
        while p + 7 <= end:
            crc = int.from_bytes(data[p:p + 4], 'little')
            length = int.from_bytes(data[p + 4:p + 6], 'little')
            kind = data[p + 6]
            p += 7
            if not length or p + length > end:
                break
            fragment = data[p:p + length]
            p += length
            if checksum(bytes([kind]) + fragment) != crc:
                raise ValueError('wal checksum')
            if kind in (1, 2):
                assembled = bytearray(fragment)
            elif kind in (3, 4):
                assembled.extend(fragment)
            else:
                assembled.clear()
            if len(assembled) > LIMIT:
                raise ValueError('wal limit')
            if kind not in (1, 4):
                continue
            if len(assembled) < 12:
                continue
            sequence, count = struct.unpack('<QI', assembled[:12])
            q = 12
            for i in range(min(count, 100000)):
                tag = assembled[q]
                q += 1
                n, q = varint(assembled, q)
                key = bytes(assembled[q:q + n])
                q += n
                value = None
                if tag == 1:
                    n, q = varint(assembled, q)
                    value = bytes(assembled[q:q + n])
                    q += n
                elif tag != 0:
                    raise ValueError('wal tag')
                if q > len(assembled):
                    raise ValueError('wal partial')
                if key == KEY:
                    yield sequence + i, value
            assembled.clear()


class TitleReader:
    def __init__(self, home=None):
        self.directory = (Path(home) if home else Path.home()) / 'Library/Application Support/AutoClaw/Local Storage/leveldb'
        self.signature = None
        self.result = {}

    def read(self):
        try:
            paths = sorted(p for p in self.directory.iterdir() if p.suffix in ('.log', '.ldb', '.sst'))[:64]
            signature = tuple((p.name, p.stat().st_size, p.stat().st_mtime_ns) for p in paths)
            if signature == self.signature:
                return self.result
            if sum(x[1] for x in signature) > 32 * 1024 * 1024:
                return {}
            latest = (-1, None)
            for p in paths:
                if p.stat().st_size > LIMIT:
                    return {}
                raw = p.read_bytes()
                for sequence, value in (log_values(raw) if p.suffix == '.log' else table_values(raw)):
                    if sequence > latest[0]:
                        latest = sequence, value
            result = {}
            value = latest[1]
            if value:
                decoded = value[1:].decode('utf-16-le' if value[0] == 0 else 'latin1')
                rows = json.loads(decoded)
                if isinstance(rows, list):
                    for row in rows[:1000]:
                        if not isinstance(row, dict):
                            continue
                        key, title = row.get('key'), row.get('displayName')
                        if isinstance(key, str) and isinstance(title, str) and 0 < len(title) <= 160:
                            result[key] = title.strip()
            # Ambiguous same-title chats cannot be safely addressed through AX.
            counts = {}
            for title in result.values():
                counts[title] = counts.get(title, 0) + 1
            self.result = {k: v for k, v in result.items() if counts[v] == 1}
            self.signature = signature
            return self.result
        except (OSError, ValueError, IndexError, struct.error, UnicodeError):
            return {}
