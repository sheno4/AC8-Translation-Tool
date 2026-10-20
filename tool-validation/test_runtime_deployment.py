"""Synthetic PAK transactions; never discover, read or launch an installed game."""
from pathlib import Path
import hashlib
import json
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'tool'))
import deployment as legacy
import runtime_deployment as deployment

ORIGINAL = bytes(range(128))
PATCHED = ORIGINAL[:4] + b'ABC' + ORIGINAL[7:]
UPDATED = ORIGINAL + b'updated game executable'
PATCH_ID = 'synthetic-pak'
PAK = b'synthetic translation pak'
LOADER = b'synthetic loader dll'


def sha(data):
    return hashlib.sha256(data).hexdigest()


class SimulatedCrash(BaseException):
    pass


class RuntimeDeploymentTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='ac8-pak-transaction-')
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        guard = patch.object(legacy, '_assert_game_stopped')
        guard.start()
        self.addCleanup(guard.stop)

    def game(self, name='game'):
        root = self.base / name
        executable = root / deployment.EXE_PATH
        executable.parent.mkdir(parents=True)
        executable.write_bytes(ORIGINAL)
        return root

    def config(self, root):
        source = deployment.inspect_exe_source(root, PATCH_ID)
        return dict(patch_id=PATCH_ID, patch_version='synthetic-2',
                    supported_exe_sha256=source['sha256'], rebase_exe=source['rebase_exe'])

    def install(self, root, pak=PAK, loader=LOADER):
        return deployment.install(root, pak, self.config(root), loader)

    def legacy_install(self, root, original_dats=False):
        if original_dats:
            for index, relative in enumerate(legacy.DATA_PATHS):
                target = root / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(b'pre-existing dat ' + bytes([index]))
        config = dict(patch_id=PATCH_ID, patch_version='synthetic-1',
                      supported_exe_sha256=sha(ORIGINAL), patched_exe_sha256=sha(PATCHED),
                      byte_patches=[dict(offset=4, original_hex=ORIGINAL[4:7].hex(), patched_hex=b'ABC'.hex())])
        return legacy.install(root, {relative: b'old translated dat ' + bytes([index])
                                     for index, relative in enumerate(legacy.DATA_PATHS)}, config)

    def snapshot(self, root):
        return {file.relative_to(root).as_posix(): file.read_bytes()
                for file in root.rglob('*') if file.is_file()}

    def state_directory(self, root):
        return root / legacy.BACKUP_DIRECTORY / PATCH_ID

    def recover(self, root):
        with legacy._lock(root, PATCH_ID) as directory:
            deployment._recover(root, PATCH_ID, directory)

    def test_fresh_install_update_and_restore_never_write_executable(self):
        root = self.game()
        self.assertEqual(deployment.inspect_exe_source(root, PATCH_ID)['mode'], 'pak_loader')
        executable = root / deployment.EXE_PATH
        original_mtime = executable.stat().st_mtime_ns
        result = self.install(root)
        self.assertEqual(deployment.inspect_exe_source(root, PATCH_ID)['mode'], 'pak_loader')
        self.assertEqual(result['mode'], 'pak_loader')
        self.assertFalse(result['exe_modified'])
        self.assertEqual({item['path'] for item in result['files']}, set(deployment.MANAGED_PATHS))
        pak_file = self.base / 'new.pak'
        pak_file.write_bytes(PAK + b' second translation')
        self.install(root, pak_file, LOADER + b' updated')
        self.assertEqual((root / deployment.PAK_PATH).read_bytes(), pak_file.read_bytes())
        self.assertEqual(executable.read_bytes(), ORIGINAL)
        self.assertEqual(executable.stat().st_mtime_ns, original_mtime)
        self.assertEqual(deployment.restore(root, PATCH_ID)['state'], 'not_installed')
        self.assertEqual(executable.read_bytes(), ORIGINAL)
        self.assertEqual(executable.stat().st_mtime_ns, original_mtime)
        self.assertTrue(all(not (root / relative).exists() for relative in deployment.MANAGED_PATHS))

    def test_v1_migration_restores_executable_removes_z_and_archives_old_backups(self):
        root = self.game()
        self.legacy_install(root)
        old_state = json.loads((self.state_directory(root) / 'state.json').read_text('utf-8'))
        self.assertEqual(deployment.status(root, PATCH_ID)['mode'], 'legacy')
        self.assertEqual(deployment.inspect_exe_source(root, PATCH_ID)['mode'], 'legacy')
        self.assertEqual(self.install(root)['state'], 'installed')
        self.assertEqual((root / deployment.EXE_PATH).read_bytes(), ORIGINAL)
        self.assertTrue(all(not (root / path).exists() for path in legacy.DATA_PATHS))
        state = json.loads((self.state_directory(root) / 'state.json').read_text('utf-8'))
        self.assertEqual(state['legacy_archive'], old_state)
        self.assertEqual([item['path'] for item in state['files']], list(deployment.MANAGED_PATHS))
        self.assertEqual((self.state_directory(root) / old_state['files'][0]['backup']).read_bytes(), ORIGINAL)
        self.assertEqual(deployment.restore(root, PATCH_ID)['state'], 'not_installed')
        self.assertEqual((root / deployment.EXE_PATH).read_bytes(), ORIGINAL)
        self.assertFalse(self.state_directory(root).exists())

    def test_migration_restores_preexisting_z_files_and_later_leaves_them_alone(self):
        root = self.game()
        self.legacy_install(root, original_dats=True)
        self.install(root)
        for index, relative in enumerate(legacy.DATA_PATHS):
            self.assertEqual((root / relative).read_bytes(), b'pre-existing dat ' + bytes([index]))
        foreign_dat = root / legacy.DATA_PATHS[0]
        foreign_dat.write_bytes(b'subsequent unrelated dat')
        deployment.restore(root, PATCH_ID)
        self.assertEqual(foreign_dat.read_bytes(), b'subsequent unrelated dat')

    def test_updated_executable_survives_v2_update_and_removal(self):
        root = self.game()
        self.install(root)
        executable = root / deployment.EXE_PATH
        executable.write_bytes(UPDATED)
        update_mtime = executable.stat().st_mtime_ns
        self.assertTrue(deployment.inspect_exe_source(root, PATCH_ID)['rebase_exe'])
        self.assertEqual(deployment.status(root, PATCH_ID)['state'], 'installed')
        self.install(root, PAK + b' updated')
        self.assertEqual(executable.read_bytes(), UPDATED)
        deployment.restore(root, PATCH_ID)
        self.assertEqual(executable.read_bytes(), UPDATED)
        self.assertEqual(executable.stat().st_mtime_ns, update_mtime)

    def test_v1_rebase_migration_does_not_restore_outdated_executable(self):
        root = self.game()
        self.legacy_install(root)
        executable = root / deployment.EXE_PATH
        executable.write_bytes(UPDATED)
        update_mtime = executable.stat().st_mtime_ns
        self.assertEqual(self.install(root)['state'], 'installed')
        self.assertEqual(executable.read_bytes(), UPDATED)
        self.assertEqual(executable.stat().st_mtime_ns, update_mtime)
        deployment.restore(root, PATCH_ID)
        self.assertEqual(executable.read_bytes(), UPDATED)

    def test_foreign_loader_and_same_named_pak_are_never_overwritten(self):
        for index, relative in enumerate(deployment.MANAGED_PATHS):
            with self.subTest(path=relative):
                root = self.game(f'conflict-{index}')
                target = root / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(b'unrelated mod')
                before = self.snapshot(root)
                with self.assertRaisesRegex(deployment.DeploymentError, '拒绝覆盖'):
                    self.install(root)
                self.assertEqual(self.snapshot(root), before)

    def test_foreign_loader_appearing_after_initial_preflight_is_not_adopted(self):
        root = self.game()
        target = root / deployment.LOADER_PATH
        real_current = legacy._current
        observations = 0

        def create_foreign_loader(path):
            nonlocal observations
            if path == target:
                observations += 1
                if observations == 2:
                    target.write_bytes(b'foreign loader installed concurrently')
            return real_current(path)

        with patch.object(legacy, '_current', side_effect=create_foreign_loader):
            with self.assertRaises(deployment.DeploymentError):
                self.install(root)
        self.assertEqual(target.read_bytes(), b'foreign loader installed concurrently')
        self.assertFalse((root / deployment.PAK_PATH).exists())
        self.assertEqual((root / deployment.EXE_PATH).read_bytes(), ORIGINAL)

    def test_every_migration_replace_failure_rolls_back_exact_v1_state(self):
        targets = [deployment.EXE_PATH, *legacy.DATA_PATHS, deployment.PAK_PATH,
                   deployment.LOADER_PATH, f'{legacy.BACKUP_DIRECTORY}/{PATCH_ID}/state.json']
        for number, relative in enumerate(targets):
            with self.subTest(target=relative):
                root = self.game(f'replace-failure-{number}')
                self.legacy_install(root, original_dats=True)
                before = self.snapshot(root)
                real_replace = deployment.os.replace
                failed = False

                def fail_once(source, destination):
                    nonlocal failed
                    if not failed and Path(destination) == root / relative:
                        failed = True
                        raise OSError('synthetic replace failure')
                    return real_replace(source, destination)

                with patch.object(deployment.os, 'replace', side_effect=fail_once):
                    with self.assertRaises(deployment.DeploymentError):
                        self.install(root)
                self.assertTrue(failed)
                self.assertEqual(self.snapshot(root), before)

    def test_each_migration_z_deletion_failure_rolls_back_exact_v1_state(self):
        for number, relative in enumerate(legacy.DATA_PATHS):
            with self.subTest(target=relative):
                root = self.game(f'unlink-failure-{number}')
                self.legacy_install(root)
                before = self.snapshot(root)
                real_unlink = Path.unlink
                failed = False

                def fail_once(path, *args, **kwargs):
                    nonlocal failed
                    if not failed and path == root / relative:
                        failed = True
                        raise OSError('synthetic unlink failure')
                    return real_unlink(path, *args, **kwargs)

                with patch.object(Path, 'unlink', new=fail_once):
                    with self.assertRaises(deployment.DeploymentError):
                        self.install(root)
                self.assertTrue(failed)
                self.assertEqual(self.snapshot(root), before)

    def test_preparing_and_partially_committed_crashes_recover_v1(self):
        for stage in ('preparing', 'targets_replaced'):
            with self.subTest(stage=stage):
                root = self.game('crash-' + stage)
                self.legacy_install(root)
                before = self.snapshot(root)
                if stage == 'preparing':
                    real_copy = legacy._copy_payload

                    def crash_copy(source, destination):
                        real_copy(source, destination)
                        raise SimulatedCrash('preparation interrupted')

                    interruption = patch.object(legacy, '_copy_payload', side_effect=crash_copy)
                else:
                    real_replace = deployment.os.replace

                    def crash_replace(source, destination):
                        real_replace(source, destination)
                        if Path(destination) == root / deployment.LOADER_PATH:
                            raise SimulatedCrash('targets replaced, state not committed')

                    interruption = patch.object(deployment.os, 'replace', side_effect=crash_replace)
                with interruption, self.assertRaises(SimulatedCrash):
                    self.install(root)
                self.assertEqual(deployment.status(root, PATCH_ID)['state'], 'incomplete')
                self.recover(root)
                self.assertEqual(self.snapshot(root), before)

    def test_crash_after_state_commit_keeps_new_installation(self):
        root = self.game()
        self.legacy_install(root)
        with patch.object(deployment, '_clear_transaction', side_effect=SimulatedCrash('committed')):
            with self.assertRaises(SimulatedCrash):
                self.install(root)
        self.recover(root)
        self.assertEqual(deployment.status(root, PATCH_ID)['mode'], 'pak_loader')
        self.assertEqual((root / deployment.EXE_PATH).read_bytes(), ORIGINAL)
        self.assertEqual((root / deployment.PAK_PATH).read_bytes(), PAK)
        self.assertTrue(all(not (root / relative).exists() for relative in legacy.DATA_PATHS))


if __name__ == '__main__':
    unittest.main(verbosity=2)
