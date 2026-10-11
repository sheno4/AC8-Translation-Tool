"""One targeted synthetic validation of the optional comparison backend."""
from __future__ import annotations
import copy
import hashlib
import json
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / 'tool'))
import ac8tool as tool
import ac8compare as compare


def assert_refused(call):
    try:
        call()
    except ValueError:
        return
    raise AssertionError('Expected source mutation to be refused')


def run():
    checks = []
    source = [
        dict(domain='CP', index=0, ja='JP {n}\n<f100>', en='EN {n}\n<f100>', text_ids=[]),
        dict(domain='CP', index=1, ja='JP {n}\n<f100>', en='EN {n}\n<f100>', text_ids=[]),
        dict(domain='ON', index=0, ja='JP {value}', en='EN {value}', text_ids=[]),
        dict(domain='ON', index=1, ja='JP missing', en='EN missing', text_ids=[])]
    version = dict(buildid='25201480', supported_exe_sha256='a' * 64)
    revision = 'b' * 64
    original_sources, original_check, original_data = tool._sources, tool.check_game_version, tool.DATA_DIR
    tool._sources = lambda: (source, [], version, revision)
    tool.check_game_version = lambda game: dict(ok=True, buildid=version['buildid'], errors=[],
                                               official_chinese_read=False)
    tool._entries.cache_clear()
    tool._official_sources.cache_clear()
    with tempfile.TemporaryDirectory(prefix='comparison-fixture-', dir=PROJECT / 'tool-validation') as temp:
        root = Path(temp)
        tool.DATA_DIR = root / 'data'
        tool.DATA_DIR.mkdir()
        game = root / 'game'
        game.mkdir()
        document = tool._document('game')
        assert set(document) == {'schema', 'tool', 'source_revision', 'scope', 'game_version', 'entries'}
        assert document['source_revision'] == revision and len(document['entries']) == 3
        valid, _ = tool._validate_document(document, allow_partial=True)
        assert valid['ok'] and not valid['official_chinese_read']
        plain_path = root / 'plain.json'
        tool._json_write(plain_path, document)
        first = compare.export_comparison(game, plain_path, root / 'plain-view', include_official=False)
        assert first['ok'] and not first['official_chinese_read']
        assert not any('official_zh' in row for row in tool._json_read(first['json_path'])['rows'])
        checks.append('old JA/EN document and hidden comparison do not require/read an official snapshot')

        catalog = dict(schema='ac8-official-zh-v1', buildid=version['buildid'],
                       supported_exe_sha256=version['supported_exe_sha256'], source_revision=revision,
                       rows=[dict(domain='CP', index=0, text='OLD {n}\n<f100>'),
                             dict(domain='CP', index=1, text='ANOTHER {n}\n<f100>'),
                             dict(domain='ON', index=0, text='OLD {value}'),
                             dict(domain='ON', index=1, text='OLD missing')])
        official_path = tool.DATA_DIR / 'official-zh-catalog.json'
        tool._json_write(official_path, catalog)
        richer = tool._document('game', include_official=True)
        assert richer['source_revision'] == document['source_revision']
        assert [entry['id'] for entry in richer['entries']] == [entry['id'] for entry in document['entries']]
        assert richer['entries'][0]['official_zh'] == catalog['rows'][:2]
        assert richer['official_reference']['catalog_sha256'] == tool._sha256(official_path)
        checks.append('same JA/EN entry keeps IDs and separately aligns differing official text by each slot')

        for key in ('buildid', 'supported_exe_sha256', 'source_revision'):
            invalid = copy.deepcopy(catalog)
            invalid[key] += '-changed'
            tool._json_write(official_path, invalid)
            tool._official_sources.cache_clear()
            assert_refused(tool._official_sources)
        invalid = copy.deepcopy(catalog)
        invalid['rows'][1] = copy.deepcopy(invalid['rows'][0])
        tool._json_write(official_path, invalid)
        tool._official_sources.cache_clear()
        assert_refused(tool._official_sources)
        invalid = copy.deepcopy(catalog)
        invalid['rows'][1]['index'] = 100
        tool._json_write(official_path, invalid)
        assert_refused(tool._official_sources)
        tool._json_write(official_path, catalog)
        tool._official_sources.cache_clear()
        checks.append('snapshot rejects build, EXE, revision, duplicate and unknown slot mismatches')

        for mutation in ('text', 'identity', 'reference'):
            changed = copy.deepcopy(richer)
            if mutation == 'text':
                changed['entries'][0]['official_zh'][0]['text'] = 'changed'
            elif mutation == 'identity':
                changed['entries'][0]['official_zh'][0]['index'] = 9
            else:
                changed['official_reference']['catalog_sha256'] = '0' * 64
            assert_refused(lambda: tool._validate_document(changed, allow_partial=True))
        checks.append('attached official text, slot identities and reference digest remain immutable')

        richer['entries'][0]['translation'] = 'OLD {n}\n<f100>'
        attack = '</script><img src=x onerror=alert(1)>\0" {value}\ud800'
        richer['entries'][1]['translation'] = attack
        input_path = root / 'translated.json'
        input_path.write_text(json.dumps(richer, ensure_ascii=True), encoding='utf-8')
        digest_before = tool._sha256(input_path)
        result = compare.export_comparison(game, input_path, root / 'full-view', include_official=True)
        result_json = tool._json_read(result['json_path'])
        counts = result['counts']
        assert counts['total_slots'] == 4 and counts['filled_slots'] == 3 and counts['missing_slots'] == 1
        assert counts['same_as_official_slots'] == 1 and counts['different_from_official_slots'] == 2
        assert counts['format_issue_slots'] == 1 and counts['format_issue_unique'] == 1
        assert result['ok'] and not result['validation']['ok']
        assert result_json['rows'][2]['translation'] == attack
        assert result['official_chinese_read'] and not result['translation_file_modified']
        assert tool._sha256(input_path) == digest_before and not list(game.iterdir())
        html = Path(result['html_path']).read_text(encoding='utf-8')
        embedded = re.search(r'<script id="comparison-data" type="application/json">(.*?)</script>', html, re.S).group(1)
        assert '</script>' not in embedded and '<img' not in embedded
        assert json.loads(embedded)['rows'][2]['translation'] == attack
        assert 'innerHTML' not in html and 'textContent' in html
        checks.append('partial and malformed translations still compare; NUL, quotes, script tags and surrogates are safely represented')
        checks.append('slot counts distinguish exact equality/difference, missing translation and format issues without input/game mutation')

        hidden = compare.export_comparison(game, input_path, root / 'hidden-view', include_official=False)
        assert hidden['official_chinese_read'] and not hidden['official_column_visible']
        assert not any('official_zh' in row for row in tool._json_read(hidden['json_path'])['rows'])
        assert_refused(lambda: compare.export_comparison(game, input_path, game / 'unsafe', include_official=False))
        checks.append('hiding a previously attached official reference reports actual read and refuses output inside game')
        cli_export = tool.export_translations(game, root / 'export', output_path=root / 'new.json', include_official=True, compact=False)
        assert cli_export['official_chinese_read']
        assert tool._json_read(cli_export['translation_file'])['official_reference'] == richer['official_reference']
        checks.append('optional export reports official read and preserves its read-only snapshot reference')
        node = shutil.which('node')
        if node:
            script = re.findall(r'<script>(.*?)</script>', html, re.S)[0]
            js_path = root / 'comparison.js'
            js_path.write_text(script, encoding='utf-8')
            subprocess.run([node, '--check', str(js_path)], check=True, capture_output=True, text=True)
            checks.append('embedded JavaScript passes Node syntax check without opening a browser')
    tool._sources, tool.check_game_version, tool.DATA_DIR = original_sources, original_check, original_data
    tool._entries.cache_clear()
    tool._official_sources.cache_clear()
    report = dict(ok=True, scope='synthetic targeted backend validation', checks=checks,
                  actual_game_modified=False, user_translation_modified=False,
                  image_recognition_used=False, real_game_started=False)
    tool._json_write(PROJECT / 'tool-validation' / 'comparison-backend-validation.json', report)
    print(json.dumps(report, ensure_ascii=False))


if __name__ == '__main__':
    run()
