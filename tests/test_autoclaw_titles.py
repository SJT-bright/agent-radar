import json
import struct
import tempfile
import unittest
from pathlib import Path
from collector.autoclaw_titles import KEY, TitleReader, checksum, snappy, table_values


def vint(n):
    out = bytearray()
    while n >= 128:
        out.append((n & 127) | 128); n >>= 7
    return bytes(out + bytes([n]))


def wal(seq, value, key=KEY):
    raw = struct.pack('<QI', seq, 1)
    raw += bytes([0 if value is None else 1]) + vint(len(key)) + key
    if value is not None:
        raw += vint(len(value)) + value
    return struct.pack('<IHB', checksum(b'\x01' + raw), len(raw), 1) + raw


def stored(rows):
    return b'\x00' + json.dumps(rows, ensure_ascii=False).encode('utf-16-le')


def block(items):
    data, last = b'', b''
    for key, value in items:
        data += b'\x00' + vint(len(key)) + vint(len(value)) + key + value
    return data + struct.pack('<II', 0, 1)


def table(seq, value):
    raw = block([(KEY + struct.pack('<Q', (seq << 8) | 1), value)])
    def trailer(data):
        return data + b'\x00' + struct.pack('<I', checksum(data + b'\x00'))
    data = trailer(raw)
    index = block([(b'z', vint(0) + vint(len(raw)))])
    footer = b'\x00\x00' + vint(len(data)) + vint(len(index))
    return data + trailer(index) + footer.ljust(40, b'\x00') + bytes.fromhex('57fb808b247547db')


class TitleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.reader = TitleReader(self.temp.name)
        self.reader.directory.mkdir(parents=True)
        self.p = self.reader.directory
        self.rows = [dict(key='agent:main:one', displayName='真实标题', private='do not retain')]

    def tearDown(self):
        self.temp.cleanup()

    def test_exact_whitelist_ignores_other_storage(self):
        (self.p / '001.log').write_bytes(wal(1, stored(self.rows), b'other-key') + wal(2, stored(self.rows)))
        self.assertEqual(self.reader.read(), {'agent:main:one': '真实标题'})
        self.assertNotIn('private', str(self.reader.result))

    def test_latest_sequence_merges_sst_and_wal_and_tombstone(self):
        (self.p / '001.ldb').write_bytes(table(10, stored(self.rows)))
        (self.p / '002.log').write_bytes(wal(11, stored([dict(key='agent:main:one', displayName='新标题')])))
        self.assertEqual(self.reader.read()['agent:main:one'], '新标题')
        (self.p / '002.log').write_bytes(wal(12, None))
        self.assertEqual(self.reader.read(), {})

    def test_incomplete_or_corrupt_data_never_returns_cache(self):
        path = self.p / '001.log'
        path.write_bytes(wal(1, stored(self.rows)))
        self.assertTrue(self.reader.read())
        path.write_bytes(wal(2, stored(self.rows))[:-3])
        self.assertEqual(self.reader.read(), {})
        data = bytearray(wal(3, stored(self.rows))); data[-1] ^= 1
        path.write_bytes(data)
        self.assertEqual(self.reader.read(), {})

    def test_ambiguous_normalized_titles_not_addressable(self):
        rows = [dict(key='a', displayName='同名'), dict(key='b', displayName='同名 ')]
        (self.p / '001.log').write_bytes(wal(1, stored(rows)))
        self.assertEqual(self.reader.read(), {})

    def test_snappy_literal_and_overlapping_copy(self):
        self.assertEqual(snappy(b'\x06\x04ab\x0e\x02\x00'), b'ababab')
        with self.assertRaises(ValueError):
            snappy(b'\x04\x0e\x02\x00')


if __name__ == '__main__':
    unittest.main()
