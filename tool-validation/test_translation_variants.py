"""Small offline fixtures for compact official-variant translations.

No game checks, real source catalogs, GUI, or game installation are involved.
"""
from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
import zlib

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'tool'))
import ac8compact
import ac8compare
import ac8tool


class TranslationVariantsTests(unittest.TestCase):
    def setUp(self):
        self.occurrences = [dict(domain='CP', index=index, text_ids_ascii=[])
                            for index in range(5)]
        self.game_entry = dict(id='gd:fixture', category='GameData',
                               ja='Mission {count}', en='Mission {count}',
                               translation=None, format_source='ja',
                               occurrences=self.occurrences)
        self.online_entry = dict(id='gd:online', category='GameData', ja='Online', en='Online',
                                 translation=None, format_source='ja',
                                 occurrences=[dict(domain='ON', index=0, text_ids_ascii=[])])
        self.sources = [dict(domain='CP', index=index, ja='Mission {count}',
                             en='Mission {count}', text_ids=[])
                        for index in range(5)]
        self.sources.append(dict(domain='ON', index=0, ja='Online', en='Online', text_ids=[]))
        self.official = {('CP', index): value for index, value in
                         enumerate(['A', 'B', 'A', 'B', 'C'])}
        self.official[('ON', 0)] = 'Online official'
        self.reference = dict(schema='ac8-official-zh-v1', catalog_sha256='fixture-official')
        patches = [patch.object(ac8tool, '_sources', return_value=(
                       self.sources, [], {'buildid': 'fixture'}, 'fixture-source')),
                   patch.object(ac8tool, '_entries', return_value=(
                       self.game_entry, self.online_entry)),
                   patch.object(ac8tool, '_official_sources', return_value=(
                       self.official, self.reference)),
                   patch.object(ac8tool, '_font_coverage', return_value=frozenset())]
        for mocking in patches:
            mocking.start()
            self.addCleanup(mocking.stop)
        self.document = ac8tool._document('game', include_official=True)
        self.document['entries'][0]['translation'] = ['A {count}', 'B {count}', 'C {count}']
        self.document['entries'][1]['translation'] = 'Online translated'

    def compact(self):
        return ac8compact.pack_document(copy.deepcopy(self.document))

    def selected_plan(self, cp_indices, excluded_policy='official'):
        entries = self.document['entries']
        selected = set(cp_indices)
        per_entry = {}
        for entry in entries:
            occurrences = [dict(item, selected=(index in selected if item['domain'] == 'CP' else True),
                                category=None, label=None, reason=None)
                           for index, item in enumerate(entry['occurrences'])]
            count = sum(item['selected'] for item in occurrences)
            per_entry[entry['id']] = dict(selected=bool(count), selected_slots=count,
                                          excluded_slots=len(occurrences) - count,
                                          occurrences=occurrences)
        return dict(per_entry=per_entry, counts=dict(selected_slots=len(selected) + 1),
                    selection=dict(selected_categories=None, excluded_policy=excluded_policy),
                    classification_enabled=True)

    def build_strings(self, partial=False):
        entries = {entry['id']: entry for entry in self.document['entries']}
        with tempfile.TemporaryDirectory(prefix='ac8-variants-') as temporary:
            root = Path(temporary)
            ac8tool._build_payload(root, entries, partial, document=self.document)
            encoded = (root / 'Live/Content/Localization/GameData/CP_M.dat').read_bytes()
            raw = zlib.decompress(ac8tool._xor(encoded, 12))
            return [item.decode('utf-8') for item in raw.split(b'\0')[:-1]]

    def test_compact_roundtrip_maps_repeated_slots_and_result_only(self):
        compact = self.compact()
        self.assertEqual(compact['rows'][0][2], 0)
        self.assertEqual(compact['rows'][0][3], ['A', 'B', 'C'])
        expanded = ac8compact.expand_document(compact)
        entry = expanded['entries'][0]
        self.assertEqual([ac8tool.translation_variant_index(entry, index) for index in range(5)],
                         [0, 1, 0, 1, 2])
        self.assertEqual([ac8tool.translation_for_occurrence(entry, index) for index in range(5)],
                         ['A {count}', 'B {count}', 'A {count}', 'B {count}', 'C {count}'])
        result = copy.deepcopy(compact)
        result['columns'] = list(ac8compact.RESULT_COLUMNS)
        result['rows'] = [[row[0], row[-1]] for row in result['rows']]
        self.assertEqual(ac8compact.expand_document(result), expanded)
        self.assertEqual(ac8compact.pack_document(expanded), compact)

    def test_complete_validation_and_native_pool_roundtrip(self):
        report, _ = ac8tool._validate_document(self.document)
        self.assertTrue(report['ok'])
        self.assertEqual(report['completed_unique'], 2)
        self.assertEqual(report['completed_game_slots'], 6)
        self.assertEqual(report['missing_unique'], 0)
        self.assertEqual(self.build_strings(),
                         ['A {count}', 'B {count}', 'A {count}', 'B {count}', 'C {count}'])

    def test_partial_missing_variant_falls_back_for_its_slots(self):
        self.document['entries'][0]['translation'][1] = None
        report, _ = ac8tool._validate_document(self.document)
        self.assertFalse(report['ok'])
        self.assertEqual(report['missing_ids'], ['gd:fixture'])
        self.assertEqual(report['missing_short_ids'], [1])
        self.assertEqual(report['completed_unique'], 1)
        self.assertEqual(report['completed_game_slots'], 4)
        partial_report, _ = ac8tool._validate_document(self.document, allow_partial=True)
        self.assertTrue(partial_report['ok'])
        self.assertEqual(self.build_strings(partial=True),
                         ['A {count}', 'Mission {count}', 'A {count}', 'Mission {count}', 'C {count}'])
        with self.assertRaises(ValueError):
            self.build_strings(partial=False)

    def test_excluded_missing_variants_do_not_block_selected_slots(self):
        self.document['entries'][0]['translation'] = ['A {count}', None, None]
        plan = self.selected_plan([0, 2])
        with patch.object(ac8tool, 'selection_plan', return_value=plan):
            report, _ = ac8tool._validate_document(self.document)
            self.assertTrue(report['ok'])
            self.assertEqual(report['completed_unique'], 2)
            self.assertEqual(report['completed_game_slots'], 3)
            self.assertEqual(self.build_strings(), ['A {count}', 'B', 'A {count}', 'B', 'C'])
        plan['selection']['excluded_policy'] = 'en'
        with patch.object(ac8tool, 'selection_plan', return_value=plan):
            self.assertEqual(self.build_strings(),
                             ['A {count}', 'Mission {count}', 'A {count}',
                              'Mission {count}', 'Mission {count}'])

    def test_format_issue_reports_only_broken_selected_variant(self):
        self.document['entries'][0]['translation'][1] = 'Missing placeholder'
        report, _ = ac8tool._validate_document(self.document)
        self.assertFalse(report['ok'])
        self.assertEqual(report['format_issue_count'], 1)
        self.assertEqual(report['completed_game_slots'], 4)
        issue = report['format_issues'][0]
        self.assertEqual(issue['short_id'], 1)
        self.assertEqual([item['index'] for item in issue['occurrences']], [1, 3])
        self.assertEqual(issue['variant_issues'][0]['variant_index'], 1)
        self.assertEqual(issue['variant_issues'][0]['occurrence_indices'], [1, 3])
        self.assertEqual(issue['problems'], issue['variant_issues'][0]['problems'])
        with patch.object(ac8tool, 'selection_plan', return_value=self.selected_plan([0, 2, 4])):
            report, _ = ac8tool._validate_document(self.document)
            self.assertTrue(report['ok'])
            self.assertEqual(report['format_issue_count'], 0)

    def test_nul_and_surrogate_variants_are_checked(self):
        self.document['entries'][0]['translation'] = ['A {count}', 'B {count}\0', 'C {count}\ud800']
        report, _ = ac8tool._validate_document(self.document)
        self.assertEqual(report['format_issue_count'], 1)
        issue = report['format_issues'][0]
        self.assertIn('embedded NUL', issue['problems'])
        self.assertIn('invalid UTF-8 surrogate', issue['problems'])
        self.assertEqual([variant['variant_index'] for variant in issue['variant_issues']], [1, 2])
        self.assertEqual(report['completed_game_slots'], 3)

    def test_wrong_length_types_and_missing_reference_are_rejected(self):
        for value in ([], ['A'], ['A', 'B'], ['A', 'B', 0], ['A', 'B', False],
                      ['A', 'B', {}], {'A': 'translation'}, 0):
            with self.subTest(value=value):
                compact = self.compact()
                compact['rows'][0][-1] = value
                with self.assertRaises(ValueError):
                    ac8compact.expand_document(compact)
        compact = self.compact()
        compact.pop('official_revision')
        compact['columns'] = list(ac8compact.COLUMNS)
        compact['rows'] = [[row[0], row[1], row[2], row[-1]] for row in compact['rows']]
        with self.assertRaises(ValueError):
            ac8compact.expand_document(compact)
        for category, references in (('Engine', self.document['entries'][0]['official_zh']),
                                     ('GameData', None),
                                     ('GameData', [dict(domain='CP', index=index, text='A')
                                                   for index in range(5)])):
            entry = copy.deepcopy(self.document['entries'][0])
            entry['category'] = category
            if references is None:
                entry.pop('official_zh')
            else:
                entry['official_zh'] = references
            with self.assertRaises(ValueError):
                ac8tool._validate_translation_value(entry)

    def test_scalar_compatibility_and_input_file_bytes_are_preserved(self):
        self.document['entries'][0]['translation'] = 'Scalar {count}'
        entry = self.document['entries'][0]
        self.assertIsNone(ac8tool.translation_variant_index(entry, 3))
        self.assertEqual(ac8tool.translation_for_occurrence(entry, 3), 'Scalar {count}')
        report, _ = ac8tool._validate_document(self.document)
        self.assertTrue(report['ok'])
        self.document['entries'][0]['translation'] = ['A {count}', 'B {count}', 'C {count}']
        with tempfile.TemporaryDirectory(prefix='ac8-variants-json-') as temporary:
            path = Path(temporary) / 'fixture.json'
            ac8compact.write_document(path, self.compact())
            before = hashlib.sha256(path.read_bytes()).digest()
            restored = ac8tool.load_translation_document(path)
            report, _ = ac8tool._validate_document(restored)
            self.assertTrue(report['ok'])
            self.build_strings()
            self.assertEqual(before, hashlib.sha256(path.read_bytes()).digest())

    def test_comparison_displays_each_variant_and_only_its_format_problem(self):
        self.document['entries'][0]['translation'] = ['A {count}', 'Missing placeholder', None]
        with tempfile.TemporaryDirectory(prefix='ac8-variants-compare-') as temporary:
            root = Path(temporary)
            source = root / 'fixture.json'
            ac8compact.write_document(source, self.compact())
            before = source.read_bytes()
            with patch.object(ac8tool, 'check_game_version', return_value=dict(
                    ok=True, buildid='fixture', errors=[])):
                result = ac8compare.export_comparison(root / 'game', source, root / 'view')
            compared = ac8tool._json_read(result['json_path'])
            rows = [row for row in compared['rows'] if row['domain'] == 'CP']
            self.assertEqual([row['translation'] for row in rows],
                             ['A {count}', 'Missing placeholder', 'A {count}',
                              'Missing placeholder', None])
            self.assertEqual([row['index'] for row in rows if row['format_problems']], [1, 3])
            self.assertEqual(rows[4]['status'], 'missing')
            self.assertTrue(all(row['short_id'] == 1 for row in rows))
            self.assertEqual(result['counts']['format_issue_slots'], 2)
            self.assertEqual(result['counts']['missing_slots'], 1)
            self.assertEqual(source.read_bytes(), before)


if __name__ == '__main__':
    unittest.main()
