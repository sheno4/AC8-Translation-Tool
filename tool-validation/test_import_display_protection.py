"""Targeted synthetic checks for optional HUD and equipment-short imports.

Sources, official texts, font coverage, and game-version checks are mocked.
Only temporary JSON and staged DAT pools are touched; no real game, GUI,
image recognition, or deployment operation is involved.
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
import ac8compare
import ac8importoptions
import ac8tool


class ImportDisplayProtectionTests(unittest.TestCase):
    def setUp(self):
        def occurrence(index, text_id, domain='CP'):
            return dict(domain=domain, index=index, text_ids_ascii=[text_id])

        def entry(identity, text, occurrences):
            return dict(id='gd:' + identity, category='GameData', ja=text, en=text,
                        translation=None, format_source='ja', occurrences=occurrences)

        self.entries = (
            entry('mixed', 'FLR {count}', [
                occurrence(0, 'HudPlayerinfo_Name_Flrratio'),
                occurrence(1, 'v1000_story_line'),
                occurrence(2, 'HudPlayerinfo_Name_Flrratio'),
                occurrence(3, 'v1000_story_line'),
            ]),
            entry('weapon', 'GUN', [
                occurrence(4, 'WeaponShort_Name_mg'),
                occurrence(5, 'Weapon_Name_mg'),
            ]),
            entry('parts', 'ACCEL+', [occurrence(6, 'PartsShort_Name_1')]),
            entry('hud', 'NEXT {count} ', [occurrence(7, 'HudMissioninfo_Name_Next')]),
            entry('story', 'STORY {n}', [occurrence(8, 'v1001_story_line')]),
            entry('aircraft', 'F-22A', [
                occurrence(9, 'AircraftShort_Name_f22a'),
                occurrence(10, 'Aircraft_GetAircraft_ShortName_f22a'),
            ]),
            entry('online', 'Online', [occurrence(0, 'Online_Common_Mainmenu_Name', 'ON')]),
        )
        self.sources = []
        for item in self.entries:
            for slot in item['occurrences']:
                self.sources.append(dict(domain=slot['domain'], index=slot['index'],
                                         ja=item['ja'], en=item['en'],
                                         text_ids=[dict(ascii=value)
                                                   for value in slot['text_ids_ascii']]))
        self.official = {
            ('CP', 0): 'Original HUD A',
            ('CP', 1): 'Original story B',
            ('CP', 2): 'Original HUD A',
            ('CP', 3): 'Original story B',
            ('CP', 4): 'GUN',
            ('CP', 5): 'Original full weapon name',
            ('CP', 6): 'ACCEL+',
            ('CP', 7): 'NEXT {count} ',
            ('CP', 8): 'Original story',
            ('CP', 9): 'F-22A',
            ('CP', 10): 'F-22A',
            ('ON', 0): 'Original online',
        }
        self.version = dict(ok=True, buildid='fixture', errors=[], official_chinese_read=False)
        fixtures = (
            patch.object(ac8tool, '_sources', return_value=(
                self.sources, [], {'buildid': 'fixture'}, 'fixture-source')),
            patch.object(ac8tool, '_entries', return_value=self.entries),
            patch.object(ac8tool, '_official_sources', return_value=(
                self.official, dict(schema='ac8-official-zh-v1', catalog_sha256='fixture-official'))),
            patch.object(ac8tool, '_font_coverage', return_value=frozenset()),
            patch.object(ac8tool, '_classification_reference', return_value=dict(
                schema='ac8-classification-v1', rules_sha256='fixture-rules',
                source_revision='fixture-source')),
            patch.object(ac8tool, 'check_game_version', return_value=self.version),
        )
        for fixture in fixtures:
            fixture.start()
            self.addCleanup(fixture.stop)
        self.document = self.make_document()
        self.expected_protected_cp = [
            'Original HUD A', 'Story translated {count}',
            'Original HUD A', 'Story translated {count}',
            'GUN', 'Weapon translated', 'ACCEL+', 'NEXT {count} ',
            'Story translated {n}', 'F-22A', 'F-22A',
        ]
        self.expected_full_cp = [
            'HUD translated {count}', 'Story translated {count}',
            'HUD translated {count}', 'Story translated {count}',
            'Weapon translated', 'Weapon translated', 'Parts translated',
            'HUD translated {count} ', 'Story translated {n}',
            'Aircraft translated', 'Aircraft translated',
        ]

    def make_document(self, classified=False):
        document = ac8tool._document('game', include_official=True,
                                     include_classification=classified,
                                     excluded_policy='en')
        values = {
            'gd:mixed': ['HUD translated {count}', 'Story translated {count}'],
            'gd:weapon': 'Weapon translated',
            'gd:parts': 'Parts translated',
            'gd:hud': 'HUD translated {count} ',
            'gd:story': 'Story translated {n}',
            'gd:aircraft': 'Aircraft translated',
            'gd:online': 'Online translated',
        }
        for item in document['entries']:
            item['translation'] = copy.deepcopy(values[item['id']])
        return document

    @staticmethod
    def decode_pool(payload, domain):
        encoded = (Path(payload) / f'Live/Content/Localization/GameData/{domain}_M.dat').read_bytes()
        raw = zlib.decompress(ac8tool._xor(encoded, 12))
        if not raw.endswith(b'\0'):
            raise AssertionError('Staged pool must end with its original slot terminator')
        return [part.decode('utf-8') for part in raw.split(b'\0')[:-1]]

    def build_strings(self, document, import_options):
        validation, entries = ac8tool._validate_document(
            document, missing_policy='keep', import_options=import_options)
        self.assertTrue(validation['ok'], validation.get('format_issues'))
        with tempfile.TemporaryDirectory(prefix='ac8-display-payload-') as temporary:
            payload = Path(temporary)
            report = {}
            ac8tool._build_payload(payload, entries, False, document=document,
                                   selection_report=report, missing_policy='keep',
                                   import_options=import_options)
            return {domain: self.decode_pool(payload, domain) for domain in ('CP', 'ON')}, report

    def test_rules_match_original_ids_and_keep_descriptions_and_tutorials(self):
        for text_id in ('WeaponShort_Name_mg', 'AircraftShort_Name_f22a',
                        'Aircraft_GetAircraft_ShortName_f22a', 'PartsShort_Name_1'):
            with self.subTest(text_id=text_id):
                groups = ac8importoptions.groups_for_occurrence(dict(
                    domain='CP', text_ids_ascii=[text_id], ja='Any body', en='Any body'))
                self.assertEqual(set(groups), {'equipment_short'})
        for text_id in ('Weapon_Name_mg', 'Weapon_Description_mg', 'Parts_Description_1',
                        'Aircraft_Description_f22a', 'HangarTree_Dialogue_Firstspweapon',
                        'HudCampaignTutorial_Name_Weapons', 'v1000_story_line'):
            with self.subTest(text_id=text_id):
                self.assertEqual(ac8importoptions.groups_for_occurrence(dict(
                    domain='CP', text_ids_ascii=[text_id], ja='GUN', en='GUN')), ())
        self.assertIn('hud_labels', ac8importoptions.groups_for_occurrence(dict(
            domain='CP', text_ids_ascii=['HudPlayerinfo_Name_Flrratio'])))
        self.assertIn('hud_labels', ac8importoptions.groups_for_occurrence(dict(
            domain='ON', text_ids_ascii=['Online_Hud_PlayerInfo_FlrRatio_Name'])))
        self.assertEqual(ac8importoptions.groups_for_occurrence(dict(
            domain='ON', text_ids_ascii=['Online_Hud_StatusNotification_MissionStart_Name'])), ())
        self.assertEqual(ac8importoptions.normalize_options({}),
                         {'hud_labels': False, 'equipment_short': False})
        for options in ([], {'hud_labels': 1}, {'equipment_short': 'false'}, {'other': False}):
            with self.subTest(options=options), self.assertRaises(ValueError):
                ac8importoptions.normalize_options(options)

    def test_default_protection_separates_shared_slots_and_restores_official(self):
        plan = ac8tool.selection_plan(self.document, import_options={})
        mixed = plan['per_entry']['gd:mixed']['occurrences']
        weapon = plan['per_entry']['gd:weapon']['occurrences']
        self.assertEqual([slot['selected'] for slot in mixed], [False, True, False, True])
        self.assertEqual([slot['selected'] for slot in weapon], [False, True])
        # Unclassified legacy files have excluded_policy=en. Protection must
        # still restore each original official slot rather than that fallback.
        self.assertEqual(plan['selection']['excluded_policy'], 'en')
        strings, _ = self.build_strings(self.document, {})
        self.assertEqual(strings['CP'], self.expected_protected_cp)
        self.assertEqual(strings['ON'], ['Online translated'])

    def test_classified_true_cannot_bypass_protection_and_false_still_excludes(self):
        document = self.make_document(classified=True)
        document['translation_selection']['selected_categories'] = []
        document['entries'][0]['translate'] = True
        mixed = ac8tool.selection_plan(document, import_options={})['per_entry']['gd:mixed']
        self.assertEqual([slot['selected'] for slot in mixed['occurrences']],
                         [False, True, False, True])
        document['translation_selection']['selected_categories'] = None
        document['entries'][0]['translate'] = False
        mixed = ac8tool.selection_plan(document, import_options={})['per_entry']['gd:mixed']
        self.assertFalse(mixed['selected'])
        strings, _ = self.build_strings(document, {})
        expected = list(self.expected_protected_cp)
        expected[1] = expected[3] = 'FLR {count}'
        self.assertEqual(strings['CP'], expected)

    def test_repeated_official_variant_mapping_and_types_are_always_checked(self):
        item = self.document['entries'][0]
        self.assertEqual([ac8tool.translation_variant_index(item, index) for index in range(4)],
                         [0, 1, 0, 1])
        strings, _ = self.build_strings(self.document, {'hud_labels': True})
        self.assertEqual(strings['CP'][:4], self.expected_full_cp[:4])
        self.assertEqual(strings['CP'][4:7], ['GUN', 'Weapon translated', 'ACCEL+'])
        for value in ([1, 'Story translated {count}'], ['Only one variant'],
                      {'translation': 'Invalid object'}, 0):
            with self.subTest(value=value):
                bad = copy.deepcopy(self.document)
                bad['entries'][0]['translation'] = value
                with self.assertRaises(ValueError):
                    ac8tool._validate_document(bad, missing_policy='keep', import_options={})
        # Even a wholly protected entry must keep structural value checks.
        bad = copy.deepcopy(self.document)
        bad['entries'][2]['translation'] = False
        with self.assertRaises(ValueError):
            ac8tool._validate_document(bad, missing_policy='keep', import_options={})

    def test_protected_format_is_ignored_but_shared_story_format_still_blocks(self):
        document = copy.deepcopy(self.document)
        document['entries'][0]['translation'][0] = 'Missing protected variable'
        document['entries'][3]['translation'] = 'Missing protected variable and trailing space'
        report, _ = ac8tool._validate_document(document, missing_policy='keep', import_options={})
        self.assertTrue(report['ok'])
        self.assertEqual(report['format_issue_count'], 0)
        strings, _ = self.build_strings(document, {})
        self.assertEqual(strings['CP'], self.expected_protected_cp)
        document['entries'][0]['translation'][1] = 'Missing story variable'
        report, _ = ac8tool._validate_document(document, missing_policy='keep', import_options={})
        self.assertFalse(report['ok'])
        self.assertEqual(report['format_issue_count'], 1)
        issue = report['format_issues'][0]
        self.assertEqual(issue['id'], 'gd:mixed')
        self.assertEqual(issue['variant_issues'][0]['occurrence_indices'], [1, 3])
        self.assertTrue(all(item['index'] in (1, 3) for item in issue['occurrences']))
        with tempfile.TemporaryDirectory(prefix='ac8-display-refused-') as temporary:
            root = Path(temporary)
            source = root / 'translation.json'
            ac8tool._json_write(source, document)
            with patch.object(ac8tool, '_build_payload') as build, self.assertRaises(ValueError):
                ac8tool.stage_translations(root / 'unread-game', source, root / 'workspace',
                                          import_options={})
            build.assert_not_called()
            self.assertFalse((root / 'unread-game').exists())

    def test_repeated_stage_rebuilds_protected_slots_without_editing_compact_json(self):
        with tempfile.TemporaryDirectory(prefix='ac8-display-stage-') as temporary:
            root = Path(temporary)
            source = root / 'translation.json'
            ac8compact.write_document(source, ac8compact.pack_document(copy.deepcopy(self.document)))
            before = source.read_bytes()
            workspace = root / 'workspace'
            first = ac8tool.stage_translations(root / 'unread-game', source, workspace,
                                             import_options={'hud_labels': True,
                                                             'equipment_short': True})
            self.assertEqual(self.decode_pool(first['payload_dir'], 'CP'), self.expected_full_cp)
            self.assertFalse(first['game_files_changed'])
            second = ac8tool.stage_translations(root / 'unread-game', source, workspace,
                                              import_options={})
            self.assertEqual(self.decode_pool(second['payload_dir'], 'CP'), self.expected_protected_cp)
            self.assertEqual(self.decode_pool(second['payload_dir'], 'ON'), ['Online translated'])
            self.assertFalse(second['game_files_changed'])
            self.assertFalse(second['game_installed'])
            checked = ac8tool.check_translations(root / 'unread-game', source, import_options={})
            self.assertTrue(checked['ok'])
            self.assertEqual(source.read_bytes(), before)
            self.assertFalse((root / 'unread-game').exists())
            self.assertTrue(ac8tool._json_read(workspace / 'stage-state.json')['previous_payload'])

    def test_hud_target_protection_does_not_exclude_briefing_or_hangar_ui(self):
        def entry(identity, text, translation, slots, domain='CP'):
            return dict(id='gd:' + identity, category='GameData', ja=text, en=text,
                        translation=translation, format_source='ja', occurrences=[
                            dict(domain=domain, index=index, text_ids_ascii=[text_id])
                            for index, text_id in slots])

        entries = (
            entry('rex', 'REX', '雷克斯', [
                (0, 'ContainerBriefing_Name_Rex'),
                (1, 'HangarSelectaircraftTitle_Name_Player'),
                (2, 'v1000_story_line'),
                # Synthetic existing-HUD rule coverage; not an identified replay resource.
                (7, 'ContainerAllyCallsign_Name_SyntheticPlayer'),
            ]),
            entry('rex-story', 'Rex, listen.', '雷克斯，注意。', [(3, 'Msg_Han_v0004_T0080')]),
            entry('menu', 'SELECT AIRCRAFT', '选择机体', [
                (4, 'HangarSelectaircraftTitle_Name_Selectaircraft'),
                (5, 'HangarSelectaircraftTitle_Name_Selectaircraft2'),
            ]),
            entry('wingman', 'WINGMAN', '僚机', [(6, 'HangarSelectaircraftTitle_Name_Wingman1')]),
            entry('online', 'Online', '线上中文', [(0, 'Online_Common_Mainmenu_Name')], 'ON'),
        )
        sources = [dict(domain=slot['domain'], index=slot['index'],
                        ja=item['ja'], en=item['en'],
                        text_ids=[dict(ascii=value) for value in slot['text_ids_ascii']])
                   for item in entries for slot in item['occurrences']]
        official = {(source['domain'], source['index']): source['en'] for source in sources}
        with patch.object(ac8tool, '_sources', return_value=(
                sources, [], {'buildid': 'fixture'}, 'fixture-source')), patch.object(
                ac8tool, '_entries', return_value=entries), patch.object(
                ac8tool, '_official_sources', return_value=(official, dict(
                    schema='ac8-official-zh-v1', catalog_sha256='fixture-official'))), patch.object(
                ac8tool, '_font_coverage', return_value=frozenset(
                    ord(character) for item in entries for character in item['translation'])), \
                tempfile.TemporaryDirectory(prefix='ac8-rex-payload-') as temporary:
            root = Path(temporary)
            document = ac8tool._document('game', include_official=True,
                                        include_classification=False)
            source = root / 'translation.json'
            ac8compact.write_document(source, ac8compact.pack_document(document))
            before = source.read_bytes()
            protected = ac8tool.stage_translations(root / 'unread-game', source,
                                                   root / 'workspace', import_options={})
            self.assertEqual(self.decode_pool(protected['payload_dir'], 'CP'),
                             ['雷克斯', '雷克斯', '雷克斯', '雷克斯，注意。',
                              '选择机体', '选择机体', '僚机', 'REX'])
            self.assertEqual(self.decode_pool(protected['payload_dir'], 'ON'), ['线上中文'])
            full = ac8tool.stage_translations(root / 'unread-game', source, root / 'workspace',
                                             import_options={'hud_labels': True})
            self.assertEqual(self.decode_pool(full['payload_dir'], 'CP'),
                             ['雷克斯', '雷克斯', '雷克斯', '雷克斯，注意。',
                              '选择机体', '选择机体', '僚机', '雷克斯'])
            self.assertEqual(source.read_bytes(), before)
            self.assertFalse((root / 'unread-game').exists())

    def test_none_preserves_legacy_api_behavior(self):
        self.assertIsNone(ac8importoptions.normalize_options(None))
        self.assertEqual(ac8tool.selection_plan(self.document),
                         ac8tool.selection_plan(self.document, import_options=None))
        implicit = ac8tool._validate_document(self.document, missing_policy='keep')[0]
        explicit = ac8tool._validate_document(self.document, missing_policy='keep', import_options=None)[0]
        self.assertEqual(implicit, explicit)
        self.assertTrue(explicit['ok'])
        self.assertEqual(explicit['selection_counts']['selected_slots'], 12)
        strings, _ = self.build_strings(self.document, None)
        self.assertEqual(strings['CP'], self.expected_full_cp)
        legacy = self.make_document(classified=True)
        legacy['entries'][0]['translate'] = False
        legacy_plan = ac8tool.selection_plan(legacy, import_options=None)
        self.assertFalse(legacy_plan['per_entry']['gd:mixed']['selected'])
        with tempfile.TemporaryDirectory(prefix='ac8-display-legacy-') as temporary:
            root = Path(temporary)
            source = root / 'legacy.json'
            ac8tool._json_write(source, self.document)
            checked = ac8tool.check_translations(root / 'unread-game', source, import_options=None)
            self.assertTrue(checked['ok'])
            self.assertEqual(checked['completed_game_slots'], 12)

    def test_preview_and_comparison_follow_runtime_options(self):
        with tempfile.TemporaryDirectory(prefix='ac8-display-preview-') as temporary:
            root = Path(temporary)
            source = root / 'translation.json'
            ac8compact.write_document(source, ac8compact.pack_document(copy.deepcopy(self.document)))
            before = source.read_bytes()
            preview_path = root / 'protection-preview.json'
            preview = ac8tool.inspect_import_protection(source, {}, preview_path)
            self.assertTrue(preview['ok'])
            self.assertFalse(preview['game_accessed'])
            self.assertFalse(preview['game_files_changed'])
            self.assertFalse(preview['translation_file_modified'])
            self.assertEqual(preview['import_options'],
                             {'hud_labels': False, 'equipment_short': False})
            self.assertEqual(len(preview['rows']), 7)
            protected = {(row['domain'], row['index']) for row in preview['rows']}
            self.assertEqual(protected, {('CP', index) for index in (0, 2, 4, 6, 7, 9, 10)})
            for row in preview['rows']:
                self.assertTrue(row['protected'])
                self.assertFalse(row['selected_for_translation'])
                self.assertEqual(row['effective_text'], row['official_zh'])
                self.assertEqual(row['effective_text'], self.official[(row['domain'], row['index'])])
            self.assertEqual(ac8tool._json_read(preview_path)['rows'], preview['rows'])
            self.assertEqual(source.read_bytes(), before)

            exported = ac8compare.export_comparison(root / 'unread-game', source,
                                                    root / 'comparison', import_options={})
            self.assertTrue(exported['ok'])
            self.assertFalse(exported['game_files_changed'])
            self.assertFalse(exported['translation_file_modified'])
            compared = ac8tool._json_read(exported['json_path'])
            self.assertEqual(compared['import_options'], preview['import_options'])
            rows = {(row['domain'], row['index']): row for row in compared['rows']}
            self.assertEqual({identity for identity, row in rows.items()
                              if row.get('import_protected')}, protected)
            for identity in protected:
                self.assertFalse(rows[identity]['selected_for_translation'])
                self.assertEqual(rows[identity]['excluded_policy'], 'official')
                self.assertEqual(rows[identity]['status'], 'excluded')
                self.assertEqual(rows[identity]['official_zh'], self.official[identity])
            for index in (1, 3):
                self.assertTrue(rows[('CP', index)]['selected_for_translation'])
                self.assertEqual(rows[('CP', index)]['translation'], 'Story translated {count}')
            self.assertTrue(rows[('CP', 5)]['selected_for_translation'])
            self.assertEqual(source.read_bytes(), before)
            self.assertFalse((root / 'unread-game').exists())


if __name__ == '__main__':
    unittest.main()
