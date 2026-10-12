"""Self-contained resource compatibility tests; no game files or private keys."""
from pathlib import Path
import hashlib
import struct
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / 'tool'))
import ac8resourcecompat as compat


def _string(value):
    raw = value.encode('ascii') + b'\0'
    return struct.pack('<i', len(raw)) + raw


def _write_pak(path, entries, prefix=b''):
    """Small standard unencrypted PAK v11 with a full directory index."""
    body, encoded, groups = bytearray(prefix), bytearray(), {}
    for resource, data in entries.items():
        offset, encoded_offset = len(body), len(encoded)
        body.extend(struct.pack('<QQQI', 0, len(data), len(data), 0))
        body.extend(hashlib.sha1(data).digest() + struct.pack('<BI', 0, 0))
        body.extend(data)
        encoded.extend(struct.pack('<III', (1 << 31) | (1 << 30), offset, len(data)))
        parent, name = resource.rsplit('/', 1)
        groups.setdefault(parent + '/', []).append((name, encoded_offset))
    directory = bytearray(struct.pack('<I', len(groups)))
    for parent, items in groups.items():
        directory.extend(_string(parent) + struct.pack('<I', len(items)))
        for name, offset in items:
            directory.extend(_string(name) + struct.pack('<i', offset))
    directory_offset = len(body)
    body.extend(directory)
    index = (_string('../../../') + struct.pack('<IQII', len(entries), 0, 0, 1) +
             struct.pack('<QQ', directory_offset, len(directory)) + hashlib.sha1(directory).digest() +
             struct.pack('<I', len(encoded)) + encoded + struct.pack('<I', 0))
    index_offset = len(body)
    body.extend(index)
    body.extend(b'\0' * 16 + struct.pack('<BIIQQ', 0, 0x5a6f12e1, 11, index_offset, len(index)) +
                hashlib.sha1(index).digest() + b'\0' * 160)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(body)


class _NoCrypto:
    def decrypt(self, data, key):
        raise AssertionError('Unencrypted synthetic fixtures must not use crypto.')

    def close(self):
        pass


class ResourceCompatibilityTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='ac8-resource-test-')
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.exe = self.root / 'unused-synthetic.exe'
        self.exe.write_bytes(b'Not a real game executable; never parsed or run.')
        names = ('CP_Cmn.dat', 'CP_A.dat', 'CP_B.dat', 'CP_M.dat',
                 'ON_Cmn.dat', 'ON_A.dat', 'ON_B.dat', 'ON_M.dat')
        self.source = {'Live/Content/Localization/GameData/' + name:
                       b'opaque-test-resource:' + name.encode('ascii') + b'\0\x01\x02'
                       for name in names}
        self.baseline = dict(schema='ac8-resource-compatibility-v1', resources=[
            dict(path=path, size=len(data), payload_sha256=hashlib.sha256(data).hexdigest())
            for path, data in self.source.items()])
        provider = patch.object(compat, '_AesEcb', _NoCrypto)
        provider.start()
        self.addCleanup(provider.stop)
        scanner = patch.object(compat, '_key_candidates',
                               side_effect=AssertionError('No EXE or key scan is allowed in this test.'))
        scanner.start()
        self.addCleanup(scanner.stop)

    def _game(self, name, entries, prefix=b''):
        folder = self.root / name
        _write_pak(folder / 'Game/Content/Paks/rebuilt.pak', entries, prefix)
        return folder

    def _check(self, folder):
        return compat.check_resources(folder, self.exe, self.baseline)

    def test_other_assets_offsets_package_size_and_index_may_change(self):
        entries = {'Other/NewAsset.bin': b'unrelated new content' * 500}
        entries.update(reversed(list(self.source.items())))
        result = self._check(self._game('unrelated-update', entries, b'new padding' * 37))
        self.assertTrue(result['ok'], result)
        self.assertEqual(result['state'], 'compatible')
        self.assertFalse(result['archive_size_locked'])
        self.assertFalse(result['whole_archive_hash_locked'])
        self.assertEqual(len(result['resources']), 8)

    def test_changed_common_id_table_is_rejected(self):
        entries = dict(self.source)
        path = 'Live/Content/Localization/GameData/CP_Cmn.dat'
        entries[path] = entries[path][:-1] + b'X'
        result = self._check(self._game('common-update', entries))
        self.assertFalse(result['ok'])
        self.assertEqual(result['state'], 'incompatible')
        self.assertEqual([error['path'] for error in result['errors']], [path])

    def test_changed_official_language_resource_is_rejected(self):
        entries = dict(self.source)
        path = 'Live/Content/Localization/GameData/ON_M.dat'
        entries[path] = entries[path][:-1] + b'X'
        result = self._check(self._game('language-update', entries))
        self.assertFalse(result['ok'])
        self.assertEqual(result['state'], 'incompatible')
        self.assertIn('需要更新原文字库后导入', result['message'])
        self.assertEqual([error['path'] for error in result['errors']], [path])

    def test_nested_shadow_package_with_changed_common_is_rejected(self):
        folder = self._game('shadow-update', self.source)
        path = 'Live/Content/Localization/GameData/ON_Cmn.dat'
        _write_pak(folder / 'Game/Content/Paks/~mods/new_P.pak', {path: b'changed slot lookup'})
        result = self._check(folder)
        self.assertFalse(result['ok'])
        self.assertEqual(result['state'], 'incompatible')
        self.assertEqual(len(result['archives_checked']), 2)
        self.assertEqual([error['path'] for error in result['errors']], [path])

    def test_missing_required_resource_is_rejected(self):
        entries = dict(self.source)
        path = 'Live/Content/Localization/GameData/CP_B.dat'
        del entries[path]
        result = self._check(self._game('missing-resource', entries))
        self.assertFalse(result['ok'])
        self.assertEqual(result['state'], 'incompatible')
        self.assertEqual([error['path'] for error in result['errors']], [path])


if __name__ == '__main__':
    unittest.main()
