"""Inspect adapter decisions with fake resources; never access a game."""
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'tool'))
import deployment_api as api


class CompatibilityAdapterTests(unittest.TestCase):
    def setUp(self):
        self.source = dict(path='synthetic-original.exe', sha256='f' * 64,
                           current_sha256='f' * 64, installed=True, rebase_exe=True)
        self.hook = dict(supported_exe_sha256='f' * 64, patched_exe_sha256='e' * 64)

    def report(self, resources):
        with patch.object(api.deployment, 'inspect_exe_source', return_value=self.source), \
             patch('ac8pak.resolve_loader_config', return_value=dict(self.hook)), \
             patch('ac8resourcecompat.check_resources', return_value=resources):
            return api.compatibility_report('synthetic-game')

    def test_compatible_updated_exe_is_not_compared_to_the_old_build_hash(self):
        report = self.report(dict(ok=True, resources=[]))
        self.assertTrue(report['ok'])
        self.assertEqual(report['hook_config']['supported_exe_sha256'], 'f' * 64)
        self.assertTrue(report['hook_config']['rebase_exe'])

    def test_resource_errors_are_displayable_without_a_type_error(self):
        report = self.report(dict(ok=False, errors=[dict(
            path='Live/Content/Localization/GameData/CP_Cmn.dat', reason='changed slot table')]))
        self.assertFalse(report['ok'])
        self.assertIn('CP_Cmn.dat', '；'.join(report['errors']))
        self.assertNotIn('hook_config', report)

    def test_verified_owned_pak_is_excluded_from_the_original_resource_baseline(self):
        self.source.update(mode='pak_loader', rebase_exe=False)
        state = dict(state='installed', files=[dict(
            path=api.deployment.PAK_PATH, installed_sha256='a' * 64)])
        with patch.object(api.deployment, 'inspect_exe_source', return_value=self.source), \
             patch.object(api.deployment, 'status', return_value=state), \
             patch('ac8pak.resolve_loader_config', return_value=dict(self.hook)), \
             patch('ac8resourcecompat.check_resources',
                   return_value=dict(ok=True, resources=[])) as check:
            result = api.compatibility_report('synthetic-game')
        self.assertTrue(result['ok'], result)
        self.assertEqual(check.call_args.kwargs['owned_archives'],
                         {api.deployment.PAK_PATH: 'a' * 64})

    def test_uninstalled_pak_loader_can_import_without_an_owned_archive(self):
        # Both first import and import after withdrawal have this source state.
        self.source.update(installed=False, mode='pak_loader', rebase_exe=False)
        with patch.object(api.deployment, 'inspect_exe_source', return_value=self.source), \
             patch.object(api.deployment, 'status') as status, \
             patch('ac8pak.resolve_loader_config', return_value=dict(self.hook)), \
             patch('ac8resourcecompat.check_resources',
                   return_value=dict(ok=True, resources=[])) as check:
            result = api.compatibility_report('synthetic-game')
        self.assertTrue(result['ok'], result)
        self.assertEqual(result['observations']['exe_status'], 'compatible_original')
        self.assertEqual(check.call_args.kwargs['owned_archives'], {})
        status.assert_not_called()

    def test_installed_patch_disappearing_during_inspection_is_still_refused(self):
        self.source.update(mode='pak_loader', rebase_exe=False)
        with patch.object(api.deployment, 'inspect_exe_source', return_value=self.source), \
             patch.object(api.deployment, 'status', return_value=dict(state='not_installed')), \
             patch('ac8pak.resolve_loader_config', return_value=dict(self.hook)), \
             patch('ac8resourcecompat.check_resources') as check:
            result = api.compatibility_report('synthetic-game')
        self.assertFalse(result['ok'], result)
        self.assertIn('自有资源补丁在兼容检查期间发生变化', result['errors'][0])
        check.assert_not_called()


if __name__ == '__main__':
    unittest.main(verbosity=2)
