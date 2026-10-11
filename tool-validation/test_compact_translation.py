"""Targeted synthetic checks for compact translation exchange; no game runs."""
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

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / 'tool'))
import ac8compact as compact
import ac8tool as tool
import ac8compare as compare


class CompactTranslationTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix='ac8-compact-fixture-')
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.game = self.root / 'game'
        self.game.mkdir()
        self.game_rows = [
            dict(domain='CP', index=0, ja='JP {n}\n<f100>\ue001', en='EN {n}\n<f100>\ue001',
                 text_ids=[{'ascii': 'v1000_story_line'}]),
            dict(domain='CP', index=1, ja='JP {n}\n<f100>\ue001', en='EN {n}\n<f100>\ue001',
                 text_ids=[{'ascii': 'mainmenutop_button'}]),
            dict(domain='CP', index=2, ja='NEXT', en='NEXT', text_ids=[]),
            dict(domain='CP', index=3, ja='JA ONLY', en=None, text_ids=[]),
            dict(domain='ON', index=0, ja='JP ON', en='EN ON', text_ids=[]),
        ]
        self.engine_rows = [dict(target='Engine', target_path='Engine/test', namespace='Test',
                                 key='One', source_hash=11, namespace_hash=12, key_hash=13,
                                 ja=None, en='Engine only')]
        self.version = dict(buildid='fixture', supported_exe_sha256='a' * 64)
        self.revision = 'b' * 64
        self.official_slots = {(row['domain'], row['index']):
                               'FAKE OLD ' + row['domain'] + ' ' + str(row['index'])
                               for row in self.game_rows}
        self.official_reference = dict(schema='fake-reference', source_revision=self.revision)
        patches = [patch.object(tool, '_sources', return_value=(
                       self.game_rows, self.engine_rows, self.version, self.revision)),
                   patch.object(tool, 'check_game_version', return_value=dict(
                       ok=True, buildid='fixture', errors=[], official_chinese_read=False)),
                   patch.object(tool, '_official_sources', return_value=(
                       self.official_slots, self.official_reference)),
                   patch.object(tool, '_font_coverage', return_value={ord(c) for c in '译文'})]
        for replacement in patches:
            replacement.start()
            self.addCleanup(replacement.stop)
        tool._entries.cache_clear()
        self.addCleanup(tool._entries.cache_clear)

    def document(self, *, official=False, classified=False, scope='game'):
        result = tool._document(scope, include_official=official, include_classification=classified,
                                selected_categories=['story'] if classified else None,
                                excluded_policy='en')
        for entry in result['entries']:
            original = entry[entry['format_source']]
            entry['translation'] = original.replace('JP', 'ZH').replace('JA', 'ZH')
        return result

    def decode_pool(self, path):
        value = zlib.decompress(tool._xor(path.read_bytes(), 12))
        self.assertTrue(value.endswith(b'\0'))
        return [part.decode('utf-8') for part in value.split(b'\0')[:-1]]

    def test_compact_source_mapping_and_no_category_inference(self):
        original = self.document()
        projected = compact.pack_document(original)
        self.assertEqual(projected['columns'], ['id', 'ja', 'en', 'translation'])
        self.assertEqual([row[0] for row in projected['rows']], [1, 2, 3, 4])
        self.assertEqual(projected['rows'][1][2], 0)
        self.assertIsNone(projected['rows'][2][2])
        self.assertEqual(set(projected), {'schema', 'source_revision', 'scope', 'columns', 'rows'})
        projected['rows'].reverse()
        self.assertEqual(compact.expand_document(projected), original)
        with patch('ac8categories.categories_for_entry', side_effect=AssertionError('classification disabled')):
            validation, entries = tool._validate_document(projected)
            self.assertTrue(validation['ok'])
            plan = tool.selection_plan(original, entries)
        self.assertEqual(plan['counts']['selected_slots'], 5)
        for value in plan['per_entry'].values():
            for occurrence in value['occurrences']:
                self.assertIsNone(occurrence['category'])
                self.assertIsNone(occurrence['label'])
                self.assertIsNone(occurrence['reason'])

    def test_null_empty_and_engine_missing_source_are_distinct(self):
        original = self.document(scope='all')
        original['entries'][0]['translation'] = None
        original['entries'][1]['translation'] = ''
        projected = compact.pack_document(original)
        restored = compact.expand_document(projected)
        self.assertIsNone(restored['entries'][0]['translation'])
        self.assertEqual(restored['entries'][1]['translation'], '')
        self.assertIsNone(projected['rows'][-1][1])
        self.assertEqual(restored['entries'][-1]['format_source'], 'en')
        self.assertEqual(restored, original)

    def test_fake_official_text_dedup_and_translation_is_last_column(self):
        original = self.document(official=True)
        projected = compact.pack_document(original)
        self.assertEqual(projected['columns'], ['id', 'ja', 'en', 'official_zh', 'translation'])
        self.assertEqual(projected['rows'][0][3], ['FAKE OLD CP 0', 'FAKE OLD CP 1'])
        self.assertEqual(projected['rows'][1][3], 'FAKE OLD CP 2')
        self.assertEqual(projected['rows'][0][-1], original['entries'][0]['translation'])
        self.assertEqual(projected['official_revision'],
                         hashlib.sha256(tool._canonical(original['official_reference'])).hexdigest())
        self.assertEqual(compact.expand_document(projected), original)
        same_reference = copy.deepcopy(original)
        same_reference['entries'][0]['official_zh'][1]['text'] = same_reference['entries'][0]['official_zh'][0]['text']
        self.assertIsInstance(compact.pack_document(same_reference)['rows'][0][3], str)
        tampered = copy.deepcopy(projected)
        tampered['rows'][0][3] = 'CHANGED FAKE REFERENCE'
        with self.assertRaises(ValueError):
            compact.expand_document(tampered)
        for mode in ('missing', 'drift'):
            with self.subTest(official_revision=mode):
                changed = copy.deepcopy(projected)
                if mode == 'missing':
                    del changed['official_revision']
                else:
                    changed['official_revision'] = 'c' * 64
                with self.assertRaises(ValueError):
                    compact.expand_document(changed)

    def test_legacy_selection_format_and_existing_translation_survive_copy(self):
        original = self.document(classified=True)
        original['entries'][0]['format_source'] = 'en'
        original['entries'][1]['translate'] = False
        original['entries'][-1]['translate'] = True
        source = self.root / 'user.json'
        destination = self.root / 'ai.json'
        tool._json_write(source, original)
        before = source.read_bytes()
        report = tool.compact_translation_file(source, destination)
        self.assertTrue(report['classification_preserved'])
        projected = tool._json_read(destination)
        self.assertEqual(projected['format_sources'], {'1': 'en'})
        self.assertEqual(projected['translate_overrides'], {'2': False, '4': True})
        self.assertEqual(projected['selection'], original['translation_selection'])
        self.assertEqual(projected['classification_revision'],
                         original['classification_reference']['rules_sha256'])
        self.assertEqual(tool.load_translation_document(destination), original)
        self.assertEqual(tool.selection_plan(original), tool.selection_plan(tool.load_translation_document(destination)))
        self.assertEqual(source.read_bytes(), before)
        with self.assertRaises(ValueError):
            tool.compact_translation_file(source, source)
        with self.assertRaises(ValueError):
            tool.compact_translation_file(source, destination)
        self.assertEqual(source.read_bytes(), before)
        drifted = copy.deepcopy(projected)
        drifted['classification_revision'] = 'c' * 64
        with self.assertRaises(ValueError):
            compact.expand_document(drifted)
        missing_revision = copy.deepcopy(projected)
        del missing_revision['classification_revision']
        with self.assertRaises(ValueError):
            compact.expand_document(missing_revision)

    def test_strict_ids_sources_revision_and_sparse_settings(self):
        projected = compact.pack_document(self.document())
        mutations = {
            'revision': lambda value: value.update(source_revision='c' * 64),
            'columns': lambda value: value['columns'].append('category'),
            'extra_metadata': lambda value: value.update(occurrences=[]),
            'official_revision_without_official': lambda value: value.update(official_revision='c' * 64),
            'boolean_id': lambda value: value['rows'][0].__setitem__(0, True),
            'float_id': lambda value: value['rows'][0].__setitem__(0, 1.0),
            'duplicate_id': lambda value: value['rows'][1].__setitem__(0, 1),
            'unknown_id': lambda value: value['rows'][0].__setitem__(0, 99),
            'missing_row': lambda value: value['rows'].pop(),
            'japanese_changed': lambda value: value['rows'][0].__setitem__(1, 'CHANGED'),
            'english_changed': lambda value: value['rows'][0].__setitem__(2, 'CHANGED'),
            'boolean_zero_marker': lambda value: value['rows'][1].__setitem__(2, False),
            'missing_equal_english': lambda value: value['rows'][1].__setitem__(2, None),
            'translation_type': lambda value: value['rows'][0].__setitem__(-1, 7),
            'format_leading_zero_id': lambda value: value.update(format_sources={'01': 'en'}),
            'format_missing_english': lambda value: value.update(format_sources={'3': 'en'}),
            'override_without_selection': lambda value: value.update(translate_overrides={'1': False}),
            'selection_fields': lambda value: value.update(selection={'selected_categories': []}),
            'override_type': lambda value: value.update(selection={'selected_categories': None,
                'excluded_policy': 'en'}, classification_revision=tool._classification_reference()['rules_sha256'],
                translate_overrides={'1': 0}),
        }
        for description, change in mutations.items():
            with self.subTest(description=description):
                corrupted = copy.deepcopy(projected)
                change(corrupted)
                with self.assertRaises(ValueError):
                    compact.expand_document(corrupted)

    def test_utf8_writer_preserves_escaped_newlines_tags_and_icons(self):
        projected = compact.pack_document(self.document())
        projected['rows'][0][-1] = '译文 {n}\n<f100>\ue001'
        path = self.root / 'ai.json'
        compact.write_document(path, projected)
        raw = path.read_bytes()
        self.assertFalse(raw.startswith(b'\xef\xbb\xbf'))
        self.assertIn('译文'.encode('utf-8'), raw)
        self.assertEqual(json.loads(raw.decode('utf-8')), projected)
        lines = raw.decode('utf-8').splitlines()
        self.assertEqual(sum(line.startswith('[') and line[1:2].isdigit() for line in lines), 4)
        self.assertIn('\\n<f100>', lines[-6])
        self.assertFalse(list(self.root.glob('*.tmp-*')))

    def test_compact_staging_and_comparison_preserve_every_original_slot(self):
        original = self.document()
        full_path = self.root / 'user.json'
        path = self.root / 'ai.json'
        tool._json_write(full_path, original)
        projected = compact.pack_document(original)
        projected['rows'].reverse()
        compact.write_document(path, projected)
        originals = {full_path: full_path.read_bytes(), path: path.read_bytes()}
        staged = tool.stage_translations(self.game, path, self.root / 'stage')
        payload = Path(staged['payload_dir']) / 'Live/Content/Localization/GameData'
        self.assertEqual(self.decode_pool(payload / 'CP_M.dat'),
                         ['ZH {n}\n<f100>\ue001', 'ZH {n}\n<f100>\ue001', 'NEXT', 'ZH ONLY'])
        self.assertEqual(self.decode_pool(payload / 'ON_M.dat'), ['ZH ON'])
        self.assertEqual(staged['completed_game_slots'], 5)
        self.assertFalse(staged['official_chinese_read'])
        result = compare.export_comparison(self.game, path, self.root / 'compare', include_official=False)
        comparison = tool._json_read(result['json_path'])
        self.assertEqual(comparison['counts']['total_slots'], 5)
        self.assertEqual(comparison['counts']['unique_entries'], 4)
        self.assertFalse(comparison['classification_enabled'])
        self.assertTrue(all(row['text_category'] is None for row in comparison['rows']))
        self.assertEqual(comparison['rows'][0]['translation'], comparison['rows'][1]['translation'])
        for source, before in originals.items():
            self.assertEqual(source.read_bytes(), before)
        self.assertFalse(list(self.game.iterdir()))

    def test_default_export_is_compact_and_full_format_is_still_available(self):
        destination = self.root / 'new-ai.json'
        report = tool.export_translations(self.game, self.root / 'workspace', output_path=destination)
        self.assertEqual(report['translation_format'], compact.SCHEMA)
        exported = tool._json_read(destination)
        self.assertEqual(exported['columns'], compact.OFFICIAL_COLUMNS)
        self.assertIn('official_revision', exported)
        self.assertNotIn('selection', exported)
        self.assertTrue(all(row[-1] is None for row in exported['rows']))
        ja_en = self.root / 'new-ja-en.json'
        tool.export_translations(self.game, self.root / 'ja-en-workspace',
                                 output_path=ja_en, include_official=False)
        self.assertEqual(tool._json_read(ja_en)['columns'], compact.COLUMNS)
        self.assertNotIn('official_revision', tool._json_read(ja_en))
        full = self.root / 'new-full.json'
        tool.export_translations(self.game, self.root / 'full-workspace', output_path=full, compact=False)
        self.assertEqual(tool._json_read(full)['schema'], tool.SCHEMA)
        self.assertIn('official_reference', tool._json_read(full))
        self.assertNotIn('classification_reference', tool._json_read(full))
        with self.assertRaises(ValueError):
            tool.export_translations(self.game, self.root / 'workspace', output_path=destination)

    def test_result_only_output_recovers_metadata_and_checks_ids_and_format(self):
        original = self.document(official=True, classified=True)
        original['entries'][0]['format_source'] = 'en'
        original['entries'][1]['translate'] = False
        original['entries'][-1]['translate'] = True
        result = compact.pack_document(original)
        result['columns'] = list(compact.RESULT_COLUMNS)
        result['rows'] = [[row[0], row[-1]] for row in result['rows']]
        result['rows'].reverse()
        self.assertEqual(compact.expand_document(result), original)
        self.assertTrue(tool._validate_document(result)[0]['ok'])

        plain_original = self.document()
        plain = compact.pack_document(plain_original)
        plain['columns'] = list(compact.RESULT_COLUMNS)
        plain['rows'] = [[row[0], row[-1]] for row in plain['rows']]
        mutations = {
            'missing': lambda value: value['rows'].pop(),
            'duplicate': lambda value: value['rows'][1].__setitem__(0, 1),
            'unknown': lambda value: value['rows'][0].__setitem__(0, 99),
            'boolean_id': lambda value: value['rows'][0].__setitem__(0, True),
            'translation_type': lambda value: value['rows'][0].__setitem__(1, 7),
        }
        for description, mutation in mutations.items():
            with self.subTest(result_only=description):
                changed = copy.deepcopy(plain)
                mutation(changed)
                with self.assertRaises(ValueError):
                    compact.expand_document(changed)
        missing_variable = copy.deepcopy(plain)
        missing_variable['rows'][0][1] = 'BROKEN\n<f100>\ue001'
        restored = compact.expand_document(missing_variable)
        self.assertEqual(restored['entries'][0]['translation'], 'BROKEN\n<f100>\ue001')
        validation, _ = tool._validate_document(restored)
        self.assertFalse(validation['ok'])
        self.assertGreater(validation['format_issue_count'], 0)

        source = self.root / 'result-only.json'
        compact.write_document(source, plain)
        before = source.read_bytes()
        staged = tool.stage_translations(self.game, source, self.root / 'result-stage')
        payload = Path(staged['payload_dir']) / 'Live/Content/Localization/GameData'
        self.assertEqual(self.decode_pool(payload / 'CP_M.dat'),
                         ['ZH {n}\n<f100>\ue001', 'ZH {n}\n<f100>\ue001', 'NEXT', 'ZH ONLY'])
        self.assertEqual(self.decode_pool(payload / 'ON_M.dat'), ['ZH ON'])
        comparison_report = compare.export_comparison(
            self.game, source, self.root / 'result-compare', include_official=False)
        comparison = tool._json_read(comparison_report['json_path'])
        self.assertEqual(comparison['counts']['total_slots'], 5)
        self.assertEqual(comparison['rows'][0]['translation'], comparison['rows'][1]['translation'])
        self.assertEqual(source.read_bytes(), before)
        self.assertFalse(list(self.game.iterdir()))


if __name__ == '__main__':
    unittest.main()
