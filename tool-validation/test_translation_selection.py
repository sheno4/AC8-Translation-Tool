"""One meaningful synthetic regression of per-slot translation selection."""
from __future__ import annotations
import copy
import json
from pathlib import Path
import sys
import tempfile
import zlib

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / 'tool'))
import ac8tool as tool


def refused(call):
    try:
        call()
    except ValueError:
        return
    raise AssertionError('Expected invalid or modified source to be refused')


def decode(path):
    plain = zlib.decompress(tool._xor(path.read_bytes(), 12))
    assert plain.endswith(b'\0')
    return [part.decode('utf-8') for part in plain.split(b'\0')[:-1]]


def run():
    checks = []
    game_rows = [
        dict(domain='CP', index=0, ja='JP {n}', en='EN {n}', text_ids=[{'ascii':'v1000_story_line'}]),
        dict(domain='CP', index=1, ja='JP {n}', en='EN {n}', text_ids=[{'ascii':'mainmenutop_button'}]),
        dict(domain='CP', index=2, ja='BAD {x}', en='BAD EN {x}', text_ids=[{'ascii':'openingcredit_line'}]),
        dict(domain='CP', index=3, ja='TRAINING', en='TRAINING EN', text_ids=[{'ascii':'training_hint'}]),
        dict(domain='ON', index=0, ja='ONLINE', en='ONLINE EN', text_ids=[{'ascii':'online_name'}])]
    version = dict(buildid='25201480', supported_exe_sha256='a' * 64)
    revision = 'b' * 64
    original_sources, original_check, original_data = tool._sources, tool.check_game_version, tool.DATA_DIR
    tool._sources = lambda: (game_rows, [], version, revision)
    tool.check_game_version = lambda game: dict(ok=True, buildid=version['buildid'], errors=[],
                                               official_chinese_read=False)
    tool._entries.cache_clear()
    tool._official_sources.cache_clear()
    with tempfile.TemporaryDirectory(prefix='selection-fixture-', dir=PROJECT / 'tool-validation') as temp:
        root = Path(temp)
        game = root / 'game'
        game.mkdir()
        tool.DATA_DIR = root / 'data'
        tool.DATA_DIR.mkdir()
        catalog = dict(schema='ac8-official-zh-v1', buildid=version['buildid'],
                       supported_exe_sha256=version['supported_exe_sha256'], source_revision=revision,
                       rows=[dict(domain=row['domain'], index=row['index'],
                                  text='ORIGINAL '+row['domain']+' #'+str(row['index'])) for row in game_rows])
        tool._json_write(tool.DATA_DIR / 'official-zh-catalog.json', catalog)
        legacy = tool._document('game')
        assert set(legacy) == {'schema','tool','source_revision','scope','game_version','entries'}
        legacy_validation, _ = tool._validate_document(legacy, allow_partial=True)
        assert legacy_validation['ok'] and legacy_validation['missing_unique'] == 4
        assert legacy_validation['selection_counts']['selected_slots'] == 5
        legacy_cn = tool._document('game', include_official=True)
        assert tool._validate_document(legacy_cn, allow_partial=True)[0]['ok']
        checks.append('legacy JA/EN and JA/EN/CN structures remain accepted with every original slot selected')

        document = tool._document('game', include_classification=True, selected_categories=['story'])
        assert document['source_revision'] == legacy['source_revision']
        assert document['entries'][0]['text_categories'][0]['category'] == 'story'
        assert document['entries'][0]['text_categories'][1]['category'] == 'menus'
        document['entries'][0]['translation'] = 'NEW {n}'
        document['entries'][1]['translation'] = 'BROKEN\0\ud800'
        validation, entries = tool._validate_document(document)
        assert validation['ok'] and validation['missing_unique'] == 0 and validation['format_issue_count'] == 0
        assert validation['selection_counts'] == dict(selected_slots=1,excluded_slots=4,
                                                     selected_unique=1,excluded_unique=4,mixed_unique=1)
        source_path = root / 'translations.json'
        tool._json_write(source_path, document)
        source_hash = tool._sha256(source_path)
        staged = tool.stage_translations(game, source_path, root / 'official-stage')
        payload = Path(staged['payload_dir']) / 'Live/Content/Localization/GameData'
        assert decode(payload / 'CP_M.dat') == ['NEW {n}','ORIGINAL CP #1','ORIGINAL CP #2','ORIGINAL CP #3']
        assert decode(payload / 'ON_M.dat') == ['ORIGINAL ON #0']
        assert staged['official_chinese_read']
        assert staged['selection_build']['excluded_sources'] == {'official_game_slots':4}
        checks.append('mixed-category shared entry applies its translation only to selected slot; remaining slots preserve distinct official originals')
        checks.append('excluded missing or malformed translations are preserved in JSON and do not block strict selected-only staging')

        english = copy.deepcopy(document)
        english['translation_selection']['excluded_policy'] = 'en'
        english_path = root / 'english.json'
        tool._json_write(english_path, english)
        staged_en = tool.stage_translations(game, english_path, root / 'english-stage')
        payload_en = Path(staged_en['payload_dir']) / 'Live/Content/Localization/GameData'
        assert decode(payload_en / 'CP_M.dat') == ['NEW {n}','EN {n}','BAD EN {x}','TRAINING EN']
        assert decode(payload_en / 'ON_M.dat') == ['ONLINE EN']
        assert not staged_en['official_chinese_read']
        checks.append('English exclusion policy fills every excluded slot without reading official snapshot during stage')

        missing = copy.deepcopy(document)
        missing['entries'][0]['translation'] = None
        assert not tool._validate_document(missing)[0]['ok']
        false_override = copy.deepcopy(document)
        false_override['entries'][0]['translate'] = False
        false_override['entries'][0]['translation'] = 'IGNORED\0'
        validation_false, entries_false = tool._validate_document(false_override)
        assert validation_false['ok'] and validation_false['selection_counts']['selected_slots'] == 0
        false_payload = root / 'false-payload'
        false_payload.mkdir()
        tool._build_payload(false_payload, entries_false, False, document=false_override)
        assert decode(false_payload / 'Live/Content/Localization/GameData/CP_M.dat')[:2] == ['ORIGINAL CP #0','ORIGINAL CP #1']
        true_override = copy.deepcopy(document)
        true_override['translation_selection']['selected_categories'] = []
        true_override['entries'][0]['translate'] = True
        assert tool.selection_plan(true_override)['counts']['selected_slots'] == 2
        true_override['entries'][-1]['translate'] = True
        assert not tool._validate_document(true_override)[0]['ok']
        checks.append('false override excludes entire shared entry and true override includes it independently of category selection; selected missing text is rejected')

        for mutation in ('reference','slot_category','selection_type','override_type'):
            bad = copy.deepcopy(document)
            if mutation == 'reference':
                bad['classification_reference']['rules_sha256'] = '0' * 64
            elif mutation == 'slot_category':
                bad['entries'][0]['text_categories'][0]['category'] = 'menus'
            elif mutation == 'selection_type':
                bad['translation_selection']['selected_categories'] = ['story','story']
            else:
                bad['entries'][0]['translate'] = 1
            refused(lambda: tool._validate_document(bad, allow_partial=True))
        checks.append('classification reference and per-slot metadata are immutable; selection and overrides remain strictly typed')

        inspected = tool.inspect_selection(source_path)
        assert inspected['input_sha256'] == source_hash
        assert sum(item['slots'] for item in inspected['categories']) == 5
        assert sum(item['selected_slots'] for item in inspected['categories']) == 1
        configured = tool.configure_translation_selection(source_path, root / 'configured.json',
                                                           selected_categories=['menus'], excluded_policy='en')
        copied = tool._json_read(configured['translation_file'])
        assert [entry['translation'] for entry in copied['entries']] == [entry['translation'] for entry in document['entries']]
        assert [entry['format_source'] for entry in copied['entries']] == [entry['format_source'] for entry in document['entries']]
        assert configured['counts']['selected_slots'] == 1
        refused(lambda: tool.configure_translation_selection(source_path, root / 'configured.json'))
        old_path = root / 'old.json'
        tool._json_write(old_path, legacy)
        old_copy = tool.configure_translation_selection(old_path, root / 'old-upgraded.json')
        assert old_copy['counts']['selected_slots'] == 5
        assert tool._sha256(source_path) == source_hash and not list(game.iterdir())
        checks.append('inspect counts align per category; configure creates a classified copy preserving translations, formatting and overrides, refuses overwrite, and upgrades old JSON')
    tool._sources, tool.check_game_version, tool.DATA_DIR = original_sources, original_check, original_data
    tool._entries.cache_clear()
    tool._official_sources.cache_clear()
    report = dict(ok=True, scope='synthetic targeted translation-selection validation', checks=checks,
                  actual_game_modified=False, existing_user_translation_modified=False,
                  image_recognition_used=False, real_game_started=False)
    tool._json_write(PROJECT / 'tool-validation' / 'translation-selection-validation.json', report)
    print(json.dumps(report, ensure_ascii=False))


if __name__ == '__main__':
    run()
