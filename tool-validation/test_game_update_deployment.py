"""Game-update transactions on tiny synthetic files, never the installed game."""
from pathlib import Path
import hashlib
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'tool'))
import deployment as d

ORIGINAL = bytes(range(128))
PATCHED = ORIGINAL[:4] + b'ABC' + ORIGINAL[7:]
UPDATED = ORIGINAL + b'new game data'
UPDATED_PATCHED = PATCHED + b'new game data'
PAYLOADS = {d.DATA_PATHS[0]: b'new CP', d.DATA_PATHS[1]: b'new ON'}


def config(updated=False):
    digest = lambda value: hashlib.sha256(value).hexdigest()
    return dict(patch_id='synthetic-update',
                supported_exe_sha256=digest(UPDATED if updated else ORIGINAL),
                patched_exe_sha256=digest(UPDATED_PATCHED if updated else PATCHED),
                byte_patches=[dict(offset=4, original_hex=ORIGINAL[4:7].hex(),
                                   patched_hex=b'ABC'.hex())], rebase_exe=updated)


class GameUpdateDeploymentTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix='ac8-update-transaction-')
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.exe = self.root / d.EXE_PATH
        self.exe.parent.mkdir(parents=True)
        self.exe.write_bytes(ORIGINAL)
        guard = patch.object(d, '_assert_game_stopped')
        guard.start()
        self.addCleanup(guard.stop)
        d.install(self.root, PAYLOADS, config())
        self.exe.write_bytes(UPDATED)
        self.directory = self.root / d.BACKUP_DIRECTORY / 'synthetic-update'

    def test_updated_exe_rebases_and_restore_keeps_the_update(self):
        source = d.inspect_exe_source(self.root, 'synthetic-update')
        self.assertTrue(source['rebase_exe'])
        self.assertEqual(Path(source['path']), self.exe)
        self.assertEqual(d.install(self.root, PAYLOADS, config(True))['state'], 'installed')
        self.assertEqual(self.exe.read_bytes(), UPDATED_PATCHED)
        source = d.inspect_exe_source(self.root, 'synthetic-update')
        self.assertFalse(source['rebase_exe'])
        self.assertEqual(Path(source['path']).read_bytes(), UPDATED)
        self.assertEqual(d.restore(self.root, 'synthetic-update')['state'], 'not_installed')
        self.assertEqual(self.exe.read_bytes(), UPDATED)
        self.assertTrue(all(not (self.root / name).exists() for name in d.DATA_PATHS))

    def test_steam_restoring_the_same_updated_exe_still_reapplies_the_patch(self):
        for _ in range(2):
            self.exe.write_bytes(UPDATED)
            result = d.install(self.root, PAYLOADS, config(True))
            self.assertEqual(result['state'], 'installed')
            self.assertNotEqual(result['action'], 'unchanged')
            self.assertEqual(self.exe.read_bytes(), UPDATED_PATCHED)
        self.assertEqual(len(list((self.directory / 'backups').glob('*.bin'))), 1)
        d.restore(self.root, 'synthetic-update')
        self.assertEqual(self.exe.read_bytes(), UPDATED)

    def test_failed_update_preserves_old_backups_and_current_updated_exe(self):
        real_replace = d.os.replace
        failed = False

        def fail_once(source, destination):
            nonlocal failed
            if not failed and Path(destination) == self.root / d.DATA_PATHS[1]:
                failed = True
                raise OSError('synthetic write failure')
            return real_replace(source, destination)

        with patch.object(d.os, 'replace', side_effect=fail_once):
            with self.assertRaises(d.DeploymentError):
                d.install(self.root, PAYLOADS, config(True))
        self.assertTrue(failed)
        self.assertEqual(self.exe.read_bytes(), UPDATED)
        self.assertEqual((self.directory / 'backups/00000.bin').read_bytes(), ORIGINAL)
        self.assertEqual(len(list((self.directory / 'backups').glob('*.bin'))), 1)
        self.assertEqual(d.install(self.root, PAYLOADS, config(True))['state'], 'installed')
        d.restore(self.root, 'synthetic-update')
        self.assertEqual(self.exe.read_bytes(), UPDATED)

    def test_external_dat_edit_after_inspection_is_never_adopted(self):
        real_inspect = d.inspect_exe_source
        target = self.root / d.DATA_PATHS[0]
        foreign = b'foreign edit during import'

        def edit_after_inspection(*args, **kwargs):
            result = real_inspect(*args, **kwargs)
            target.write_bytes(foreign)
            return result

        with patch.object(d, 'inspect_exe_source', side_effect=edit_after_inspection):
            with self.assertRaises(d.DeploymentError):
                d.install(self.root, PAYLOADS, config(True))
        self.assertEqual(target.read_bytes(), foreign)
        self.assertEqual(self.exe.read_bytes(), UPDATED)
        self.assertEqual((self.directory / 'backups/00000.bin').read_bytes(), ORIGINAL)


if __name__ == '__main__':
    unittest.main(verbosity=2)
