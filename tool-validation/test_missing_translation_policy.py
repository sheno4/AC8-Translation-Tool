"""Offline checks that each import restores blank slots to original Chinese.

Only synthetic temporary DAT files are read or written. No game, GUI, image
recognition, or deployment operation is used.
"""
from __future__ import annotations

import copy
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
import zlib

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'tool'))
import ac8compact
import ac8tool
import test_translation_variants as variants


class MissingTranslationPolicyTests(unittest.TestCase):
    def setUp(self):
        # Reuse only the source fixture, without inheriting its existing tests.
        variants.TranslationVariantsTests.setUp(self)

    def build_strings(self, *, partial=False, missing_policy=None):
        entries = {entry['id']: entry for entry in self.document['entries']}
        with tempfile.TemporaryDirectory(prefix='ac8-keep-payload-') as temporary:
            root = Path(temporary)
            policies = {} if partial and missing_policy is None else {
                'missing_policy': missing_policy or 'keep'}
            ac8tool._build_payload(root, entries, partial, document=self.document,
                                  preserved_game=self.official, **policies)
            result = {}
            for domain in ('CP', 'ON'):
                encoded = (root / f'Live/Content/Localization/GameData/{domain}_M.dat').read_bytes()
                raw = zlib.decompress(ac8tool._xor(encoded, 12))
                self.assertTrue(raw.endswith(b'\0'))
                result[domain] = [part.decode('utf-8') for part in raw.split(b'\0')[:-1]]
            return result

    def test_null_scalar_restores_each_original_official_slot(self):
        self.official.update({('CP', index): f'Original official {index}' for index in range(5)})
        self.document = ac8tool._document('game', include_official=True)
        for entry in self.document['entries']:
            entry['translation'] = None
        report, _ = ac8tool._validate_document(self.document, missing_policy='keep')
        self.assertTrue(report['ok'])
        self.assertEqual(report['missing_unique'], 2)
        built = self.build_strings()
        self.assertEqual(built['CP'], [self.official[('CP', index)] for index in range(5)])
        self.assertEqual(built['ON'], [self.official[('ON', 0)]])

    def test_null_variant_restores_original_official_for_all_repeated_slots(self):
        self.document['entries'][0]['translation'][1] = None
        report, _ = ac8tool._validate_document(self.document, missing_policy='keep')
        self.assertTrue(report['ok'])
        self.assertEqual(report['missing_unique'], 1)
        self.assertEqual(self.build_strings()['CP'],
                         ['A {count}', 'B', 'A {count}', 'B', 'C {count}'])

    def test_empty_string_is_filled_and_still_checked(self):
        self.document['entries'][1]['translation'] = ''
        report, _ = ac8tool._validate_document(self.document, missing_policy='keep')
        self.assertTrue(report['ok'])
        self.assertEqual(report['missing_unique'], 0)
        self.assertEqual(self.build_strings()['ON'], [''])
        self.document['entries'][0]['translation'] = ''
        report, _ = ac8tool._validate_document(self.document, missing_policy='keep')
        self.assertFalse(report['ok'])
        self.assertEqual(report['missing_unique'], 0)
        self.assertEqual(report['format_issue_count'], 1)

    def test_keep_does_not_hide_invalid_filled_placeholder(self):
        self.document['entries'][0]['translation'] = [None, 'Missing placeholder', 'C {count}']
        report, _ = ac8tool._validate_document(self.document, missing_policy='keep')
        self.assertFalse(report['ok'])
        self.assertEqual(report['missing_unique'], 1)
        self.assertEqual(report['format_issue_count'], 1)
        issue = report['format_issues'][0]
        self.assertEqual(issue['variant_issues'][0]['occurrence_indices'], [1, 3])

    def test_legacy_allow_partial_keeps_english_fallback(self):
        self.document['entries'][0]['translation'][1] = None
        self.document['entries'][1]['translation'] = None
        report, _ = ac8tool._validate_document(self.document, allow_partial=True)
        self.assertTrue(report['ok'])
        built = self.build_strings(partial=True)
        self.assertEqual(built['CP'], ['A {count}', 'Mission {count}', 'A {count}',
                                     'Mission {count}', 'C {count}'])
        self.assertEqual(built['ON'], ['Online'])

    def test_public_check_and_stage_default_keep_without_changing_input(self):
        self.document['entries'][0]['translation'][1] = None
        with tempfile.TemporaryDirectory(prefix='ac8-keep-json-') as temporary:
            root = Path(temporary)
            source = root / 'fixture.json'
            ac8compact.write_document(source, ac8compact.pack_document(copy.deepcopy(self.document)))
            before = source.read_bytes()
            version = dict(ok=True, buildid='fixture', errors=[])
            with patch.object(ac8tool, 'check_game_version', return_value=version):
                checked = ac8tool.check_translations(root / 'game', source)
                self.assertTrue(checked['ok'])
                staged = ac8tool.stage_translations(root / 'game', source, root / 'workspace')
            raw = zlib.decompress(ac8tool._xor(
                (Path(staged['payload_dir']) /
                 'Live/Content/Localization/GameData/CP_M.dat').read_bytes(), 12))
            self.assertEqual([part.decode('utf-8') for part in raw.split(b'\0')[:-1]],
                             ['A {count}', 'B', 'A {count}', 'B', 'C {count}'])
            self.assertEqual(source.read_bytes(), before)
            self.assertFalse(staged['game_files_changed'])

    def test_second_stage_replaces_filled_and_restores_null_to_original_official(self):
        self.document['entries'][0]['translation'] = 'First {count}'
        self.document['entries'][1]['translation'] = 'First online'
        with tempfile.TemporaryDirectory(prefix='ac8-rebuild-second-') as temporary:
            root = Path(temporary)
            source = root / 'fixture.json'
            workspace = root / 'workspace'
            ac8compact.write_document(source, ac8compact.pack_document(copy.deepcopy(self.document)))
            with patch.object(ac8tool, 'check_game_version', return_value=dict(
                    ok=True, buildid='fixture', errors=[])):
                first = ac8tool.stage_translations(root / 'game', source, workspace)
                first_pool = (Path(first['payload_dir']) /
                              'Live/Content/Localization/GameData/CP_M.dat').read_bytes()
                first_raw = zlib.decompress(ac8tool._xor(first_pool, 12))
                self.assertEqual([part.decode('utf-8') for part in first_raw.split(b'\0')[:-1]],
                                 ['First {count}'] * 5)
                self.document['entries'][0]['translation'] = ['Second {count}', None, None]
                self.document['entries'][1]['translation'] = None
                ac8compact.write_document(source, ac8compact.pack_document(copy.deepcopy(self.document)))
                second_input = source.read_bytes()
                second = ac8tool.stage_translations(root / 'game', source, workspace)
            second_root = Path(second['payload_dir'])
            for domain, expected in (
                    ('CP', ['Second {count}', 'B', 'Second {count}', 'B', 'C']),
                    ('ON', ['Online official'])):
                raw = zlib.decompress(ac8tool._xor(
                    (second_root / f'Live/Content/Localization/GameData/{domain}_M.dat').read_bytes(), 12))
                self.assertEqual([part.decode('utf-8') for part in raw.split(b'\0')[:-1]], expected)
            self.assertEqual(source.read_bytes(), second_input)
            self.assertFalse(second['game_files_changed'])

    def test_keep_check_and_stage_both_refuse_missing_engine_template(self):
        # A missing Engine entry must not pass check then fail only at import.
        self.online_entry['category'] = 'Engine'
        document = ac8tool._document('all', include_official=True)
        document['entries'][0]['translation'] = 'Translated {count}'
        with tempfile.TemporaryDirectory(prefix='ac8-engine-keep-') as temporary:
            root = Path(temporary)
            source = root / 'fixture.json'
            ac8tool._json_write(source, document)
            with patch.object(ac8tool, 'check_game_version', return_value=dict(
                    ok=True, buildid='fixture', errors=[])):
                checked = ac8tool.check_translations(root / 'game', source)
                self.assertFalse(checked['ok'])
                self.assertEqual(checked['missing_engine_slots'], 1)
                self.assertIn('原官中模板', checked['missing_policy_errors'][0])
                with self.assertRaisesRegex(ValueError, '原官中模板'):
                    ac8tool.stage_translations(root / 'game', source, root / 'workspace')
            self.assertFalse((root / 'workspace').exists())

    def test_failed_import_replaces_stale_report_and_leaves_payload_untouched(self):
        self.document['entries'][0]['translation'][1] = 'Missing placeholder'
        with tempfile.TemporaryDirectory(prefix='ac8-import-diagnostics-') as temporary:
            root = Path(temporary)
            source = root / 'fixture.json'
            ac8compact.write_document(source, ac8compact.pack_document(copy.deepcopy(self.document)))
            before = source.read_bytes()
            workspace = root / 'workspace'
            payload = workspace / 'payload'
            payload.mkdir(parents=True)
            sentinel = payload / 'old.dat'
            sentinel.write_bytes(b'Keep previous staged payload')
            ac8tool._json_write(workspace / 'check-report.json', dict(format_issue_count=3007))
            with patch.object(ac8tool, 'check_game_version', return_value=dict(
                    ok=True, buildid='fixture', errors=[])), \
                    patch.object(ac8tool, '_build_payload') as build:
                with self.assertRaises(ValueError) as captured:
                    ac8tool.stage_translations(root / 'game', source, workspace)
                build.assert_not_called()
            self.assertIn('#1', str(captured.exception))
            self.assertIn('变量不一致', str(captured.exception))
            self.assertIn('译文数组第 2 项', str(captured.exception))
            self.assertIn(str(workspace / 'check-report.json'), str(captured.exception))
            report = ac8tool._json_read(workspace / 'check-report.json')
            self.assertEqual(report['format_issue_count'], 1)
            self.assertEqual(report['translation_file'], str(source))
            self.assertFalse(report['game_files_changed'])
            self.assertEqual(source.read_bytes(), before)
            self.assertEqual(sentinel.read_bytes(), b'Keep previous staged payload')


if __name__ == '__main__':
    unittest.main()
