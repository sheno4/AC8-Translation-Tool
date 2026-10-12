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
             patch('ac8exehook.resolve_hook_config', return_value=dict(self.hook)), \
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


if __name__ == '__main__':
    unittest.main(verbosity=2)
