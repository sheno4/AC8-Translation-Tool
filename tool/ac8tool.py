"""ACE COMBAT 8 translation export, check and staging engine.

Python standard library only. Reads bundled JA/EN source catalogs and opaque
game version fingerprints. Official Chinese is an optional read-only reference.
Does not launch the game, deploy files, or perform image recognition.
"""
from __future__ import annotations

import argparse
from collections import Counter
from functools import lru_cache
import hashlib
import importlib
import json
from pathlib import Path
import re
import shutil
import sys
import tempfile
import time
import unicodedata
import uuid
import zlib

SCHEMA = 'ac8-translation-v1'
# Legacy JSON source identifier; changing it would reject existing full files.
TOOL_NAME = 'ACE COMBAT 8 日英重译工具'
DATA_DIR = Path(__file__).resolve().parent / 'data'
GAME_DEFAULT = r'S:\SteamLibrary\steamapps\common\ACE COMBAT 8'


def _json_read(path):
    def unique_pairs(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f'JSON 包含重复字段：{key}')
            result[key] = value
        return result
    try:
        return json.loads(Path(path).read_text(encoding='utf-8-sig'), object_pairs_hook=unique_pairs)
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ValueError(f'无法读取 JSON：{path}：{error}') from error


def load_translation_document(path):
    """Read either supported translation format and restore local metadata."""
    document = _json_read(path)
    if isinstance(document, dict) and document.get('schema') == 'ac8-ai-v1':
        from ac8compact import expand_document
        return expand_document(document)
    return document


def _json_write(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + '.tmp-' + uuid.uuid4().hex)
    serialized = json.dumps(value, ensure_ascii=False, indent=2) + '\n'
    temporary.write_bytes(serialized.encode('utf-8', errors='backslashreplace'))
    temporary.replace(path)


def _canonical(value):
    return json.dumps(value, ensure_ascii=False, separators=(',', ':'), sort_keys=True).encode('utf-8')


def _sha256(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def _relative_path(root, value):
    root = Path(root).resolve()
    path = (root / value).resolve()
    try:
        path.relative_to(root)
    except ValueError:
        raise ValueError(f'路径越出允许目录：{value}') from None
    return path


@lru_cache(maxsize=1)
def _sources():
    names = ('game-source-catalog.json', 'engine-source-catalog.json',
             'game-version-fingerprint.json')
    loaded = {name: _json_read(DATA_DIR / name) for name in names}
    revision = hashlib.sha256(_canonical({name: hashlib.sha256(_canonical(loaded[name])).hexdigest()
                                         for name in names})).hexdigest()
    return loaded[names[0]], loaded[names[1]], loaded[names[2]], revision


@lru_cache(maxsize=1)
def _official_sources():
    """Read the official snapshot only when a caller requests that reference."""
    path = DATA_DIR / 'official-zh-catalog.json'
    catalog = _json_read(path)
    game, _, version, revision = _sources()
    if not isinstance(catalog, dict) or catalog.get('schema') != 'ac8-official-zh-v1':
        raise ValueError('官中对照快照的格式不受支持。')
    for key, value in (('buildid', version['buildid']),
                       ('supported_exe_sha256', version['supported_exe_sha256']),
                       ('source_revision', revision)):
        if catalog.get(key) != value:
            raise ValueError(f'官中对照快照与当前日英原文版本不一致：{key}')
    rows = catalog.get('rows')
    if not isinstance(rows, list) or len(rows) != len(game):
        raise ValueError('官中对照快照的文本槽数量不一致。')
    expected_slots = {(row['domain'], row['index']) for row in game}
    slots = {}
    for row in rows:
        if (not isinstance(row, dict) or set(row) != {'domain', 'index', 'text'}
                or not isinstance(row['domain'], str) or type(row['index']) is not int
                or not isinstance(row['text'], str)):
            raise ValueError('官中对照快照包含无效的文本槽。')
        identity = row['domain'], row['index']
        if identity not in expected_slots or identity in slots or '\0' in row['text']:
            raise ValueError(f'官中对照快照包含未知、重复或无效的文本槽：{identity}')
        try:
            row['text'].encode('utf-8', errors='strict')
        except UnicodeEncodeError:
            raise ValueError(f'官中对照快照包含无效 Unicode：{identity}') from None
        slots[identity] = row['text']
    if set(slots) != expected_slots:
        raise ValueError('官中对照快照没有完整覆盖日英文本槽。')
    reference = dict(schema=catalog['schema'], buildid=catalog['buildid'],
                     catalog_sha256=_sha256(path), source_revision=revision)
    return slots, reference


def _group_id(category, group_key):
    prefix = 'gd' if category == 'GameData' else 'ue'
    return prefix + ':' + hashlib.sha256(_canonical(group_key)).hexdigest()


@lru_cache(maxsize=2)
def _entries(scope='game'):
    if scope not in ('game', 'all'):
        raise ValueError('scope 必须为 game 或 all')
    game, engine, _, _ = _sources()
    grouped = {}
    for row in game:
        group_key = (row['ja'], row['en'])
        identity = _group_id('GameData', group_key)
        if identity not in grouped:
            grouped[identity] = dict(id=identity, category='GameData', ja=row['ja'], en=row['en'],
                                     translation=None, format_source='ja', occurrences=[])
        occurrence = dict(domain=row['domain'], index=row['index'],
                          text_ids_ascii=[item['ascii'] for item in row['text_ids'] if 'ascii' in item])
        grouped[identity]['occurrences'].append(occurrence)
    if scope == 'all':
        for row in engine:
            group_key = (row['target_path'], row['ja'], row['en'])
            identity = _group_id('Engine', group_key)
            if identity not in grouped:
                grouped[identity] = dict(id=identity, category='Engine', ja=row['ja'], en=row['en'],
                                         translation=None, format_source='ja' if row['ja'] is not None else 'en',
                                         occurrences=[])
            grouped[identity]['occurrences'].append({key: row[key] for key in
                ('target', 'target_path', 'namespace', 'key', 'source_hash', 'namespace_hash', 'key_hash')})
    return tuple(grouped.values())


def _counts(entries):
    categories = Counter(entry['category'] for entry in entries)
    slots = Counter()
    for entry in entries:
        slots[entry['category']] += len(entry['occurrences'])
    return dict(game_unique=categories['GameData'], game_slots=slots['GameData'],
                engine_unique=categories['Engine'], engine_slots=slots['Engine'],
                total_unique=len(entries))


def check_game_version(game_dir):
    """Read only EXE and opaque PAK footer/index bytes, never localized text."""
    _, _, expected, _ = _sources()
    game_dir = Path(game_dir).resolve()
    errors, observations = [], {}
    exe = _relative_path(game_dir, expected['exe_path'])
    pak = _relative_path(game_dir, expected['pak_path'])
    if not exe.is_file():
        errors.append(f'找不到游戏程序：{exe}')
    else:
        actual = _sha256(exe)
        observations['exe_sha256'] = actual
        observations['exe_status'] = 'original' if actual == expected['supported_exe_sha256'] else 'unknown'
        if observations['exe_status'] == 'unknown':
            # The deployment adapter must verify BOTH installed patch hash and
            # original backup hash before accepting a known patched executable.
            valid = False
            try:
                adapter = importlib.import_module('deployment_api')
                valid = adapter.validate_patched_executable(
                    str(game_dir), actual, expected['supported_exe_sha256'])
            except (ImportError, OSError, ValueError, KeyError):
                valid = False
            if valid:
                observations['exe_status'] = 'verified_tool_patch'
            else:
                errors.append('游戏程序版本不匹配；或程序被其他补丁修改。')
    if not pak.is_file():
        errors.append(f'找不到游戏资源包：{pak}')
    else:
        size = pak.stat().st_size
        observations['pak_size'] = size
        if size != expected['pak_size']:
            errors.append('游戏资源包大小与受支持版本不一致。')
        if size >= expected['footer_bytes']:
            with pak.open('rb') as stream:
                stream.seek(-expected['footer_bytes'], 2)
                footer_digest = hashlib.sha256(stream.read(expected['footer_bytes'])).hexdigest()
                observations['footer_sha256'] = footer_digest
                if footer_digest != expected['footer_sha256']:
                    errors.append('游戏资源包页脚指纹不一致。')
                offset, length = expected['index_offset'], expected['index_size']
                if offset < 0 or length < 0 or offset + length > size:
                    errors.append('游戏资源包索引范围无效。')
                else:
                    stream.seek(offset)
                    index_digest = hashlib.sha256(stream.read(length)).hexdigest()
                    observations['index_sha256'] = index_digest
                    if index_digest != expected['index_sha256']:
                        errors.append('游戏资源包索引指纹不一致。')
        else:
            errors.append('游戏资源包长度不足。')
    return dict(ok=not errors, game_dir=str(game_dir), buildid=expected['buildid'],
                errors=errors, observations=observations, official_chinese_read=False)


def _classification_reference():
    import ac8categories
    return dict(schema='ac8-classification-v1',
                rules_sha256=_sha256(Path(__file__).resolve().parent / 'ac8categories.py'),
                source_revision=_sources()[3])


def _selection_options(selected_categories=None, excluded_policy='official'):
    from ac8categories import CATEGORY_DEFINITIONS
    known = {item['id'] for item in CATEGORY_DEFINITIONS}
    if (selected_categories is not None and
            (not isinstance(selected_categories, list) or
             any(not isinstance(value, str) or value not in known for value in selected_categories) or
             len(set(selected_categories)) != len(selected_categories))):
        raise ValueError('selected_categories 必须为不重复的分类 ID 数组或 null。')
    if excluded_policy not in ('official', 'en'):
        raise ValueError('excluded_policy 只能选择 official 或 en。')
    return dict(selected_categories=None if selected_categories is None else list(selected_categories),
                excluded_policy=excluded_policy)


def selection_plan(document, entries=None, import_options=None):
    """Resolve category selection per original slot, without reading official text.

    ``excluded_unique`` counts entries having any excluded occurrence; mixed
    entries contribute to both selected and excluded unique counts.
    """
    from ac8categories import CATEGORY_DEFINITIONS
    from ac8importoptions import GROUP_DEFINITIONS, groups_for_occurrence, normalize_options
    import_options = normalize_options(import_options)
    definitions = {item['id']: item for item in CATEGORY_DEFINITIONS}
    classified = 'classification_reference' in document
    if classified:
        selection = document.get('translation_selection')
        if not isinstance(selection, dict) or set(selection) != {'selected_categories', 'excluded_policy'}:
            raise ValueError('translation_selection 结构被修改。')
        selection = _selection_options(**selection)
    else:
        selection = _selection_options(None, 'en')
    selected_categories = selection['selected_categories']
    selected_set = None if selected_categories is None else set(selected_categories)
    source_entries = document['entries'] if entries is None else (
        entries.values() if isinstance(entries, dict) else entries)
    per_entry, counts = {}, Counter()
    recognized, protected_counts = Counter(), Counter()
    recognized_entries, protected_entries = Counter(), Counter()
    protected_slots = protected_unique = mixed_protected = 0
    for entry in source_entries:
        categories = entry['text_categories'] if classified else [None] * len(entry['occurrences'])
        if len(categories) != len(entry['occurrences']):
            raise ValueError(f"{entry['id']}: 分类与文本槽数量不一致。")
        override = entry.get('translate') if classified else None
        if override is not None and type(override) is not bool:
            raise ValueError(f"{entry['id']}: translate 只能是 true、false 或 null。")
        occurrences = []
        entry_groups, entry_protected_groups = set(), set()
        for occurrence, category in zip(entry['occurrences'], categories):
            category_id = category['category'] if classified else None
            if classified and category_id not in definitions:
                raise ValueError(f"{entry['id']}: 分类 ID 无效。")
            selected = (True if not classified else override if override is not None else
                        selected_set is None or category_id in selected_set)
            slot = dict(occurrence, selected=bool(selected), category=category_id,
                        label=definitions[category_id]['label'] if classified else None,
                        reason=category['reason'] if classified else None)
            if import_options is not None:
                groups = groups_for_occurrence(occurrence)
                protected_groups = [group for group in groups if not import_options[group]]
                recognized.update(groups)
                protected_counts.update(protected_groups)
                entry_groups.update(groups)
                entry_protected_groups.update(protected_groups)
                slot.update(import_groups=list(groups), import_protection_groups=protected_groups,
                            force_original_official=bool(protected_groups))
                if protected_groups:
                    slot['selected'] = False
                    protected_slots += 1
            occurrences.append(slot)
        selected_slots = sum(item['selected'] for item in occurrences)
        excluded_slots = len(occurrences) - selected_slots
        per_entry[entry['id']] = dict(selected=bool(selected_slots), selected_slots=selected_slots,
                                     excluded_slots=excluded_slots, occurrences=occurrences)
        counts['selected_slots'] += selected_slots
        counts['excluded_slots'] += excluded_slots
        counts['selected_unique'] += bool(selected_slots)
        counts['excluded_unique'] += bool(excluded_slots)
        counts['mixed_unique'] += bool(selected_slots and excluded_slots)
        recognized_entries.update(entry_groups)
        protected_entries.update(entry_protected_groups)
        protected_unique += bool(entry_protected_groups)
        mixed_protected += bool(entry_protected_groups and selected_slots)
    result = dict(per_entry=per_entry, counts=dict(counts), selection=selection,
                  classification_enabled=classified)
    if import_options is not None:
        result.update(import_options=import_options, import_protection=dict(
            protected_slots=protected_slots, protected_unique=protected_unique,
            mixed_unique=mixed_protected, groups=[dict(item,
                translate=import_options[item['id']], recognized_slots=recognized[item['id']],
                protected_slots=protected_counts[item['id']],
                recognized_entries=recognized_entries[item['id']],
                protected_entries=protected_entries[item['id']]) for item in GROUP_DEFINITIONS]))
    return result


def _document(scope, include_official=False, include_classification=False,
              selected_categories=None, excluded_policy='official'):
    _, _, version, revision = _sources()
    # JSON round-trip prevents callers from mutating cached source metadata.
    entries = json.loads(json.dumps(_entries(scope), ensure_ascii=False))
    document = dict(schema=SCHEMA, tool=TOOL_NAME, source_revision=revision, scope=scope,
                    game_version=version, entries=entries)
    if include_official:
        slots, reference = _official_sources()
        document['official_reference'] = dict(reference)
        for entry in entries:
            if entry['category'] == 'GameData':
                entry['official_zh'] = [dict(domain=item['domain'], index=item['index'],
                    text=slots[(item['domain'], item['index'])]) for item in entry['occurrences']]
    if include_classification:
        from ac8categories import categories_for_entry
        document['classification_reference'] = _classification_reference()
        document['translation_selection'] = _selection_options(selected_categories, excluded_policy)
        for entry in entries:
            entry['text_categories'] = categories_for_entry(entry)
            entry['translate'] = None
    return document


def export_translations(game_dir, workspace, scope='game', output_path=None, include_official=True,
                        include_classification=False, selected_categories=None, excluded_policy='official',
                        compact=True):
    version = check_game_version(game_dir)
    if not version['ok']:
        raise ValueError('；'.join(version['errors']))
    workspace = Path(workspace).resolve()
    destination = Path(output_path).resolve() if output_path else workspace / 'translations.json'
    if destination.exists():
        raise ValueError(f'导出文件已存在，为避免覆盖译文，请选新文件：{destination}')
    document = _document(scope, include_official=include_official,
                         include_classification=include_classification,
                         selected_categories=selected_categories, excluded_policy=excluded_policy)
    if compact:
        from ac8compact import pack_document, write_document
        write_document(destination, pack_document(document))
    else:
        _json_write(destination, document)
    report = dict(ok=True, action='export', translation_file=str(destination),
                  counts=_counts(document['entries']), game_check=version,
                  instructions=('按 columns 仅填写 rows 的 translation 列；原文和短 ID 保持不变。'
                                if compact else '编辑 translation、format_source；来源字段保持不变。'),
                  translation_format='ac8-ai-v1' if compact else SCHEMA,
                  game_files_changed=False, official_chinese_read=bool(include_official))
    if include_classification:
        plan = selection_plan(document)
        report.update(selection_counts=plan['counts'], selection=plan['selection'])
    report_path = workspace / 'export-report.json'
    _json_write(report_path, report)
    report['report_path'] = str(report_path)
    return report


def compact_translation_file(input_path, output_path):
    """Save a compact copy without changing translations or legacy selection."""
    input_path, output_path = Path(input_path).resolve(), Path(output_path).resolve()
    if input_path == output_path:
        raise ValueError('精简副本不能覆盖原翻译文件，请选新文件。')
    if output_path.exists():
        raise ValueError(f'目标已存在，为避免覆盖译文，请选新文件：{output_path}')
    document = load_translation_document(input_path)
    validation, _ = _validate_document(document, allow_partial=True)
    from ac8compact import pack_document, write_document
    compact_document = pack_document(document)
    write_document(output_path, compact_document)
    original_bytes, compact_bytes = input_path.stat().st_size, output_path.stat().st_size
    return dict(ok=True, action='compact', translation_file=str(output_path),
                translation_format='ac8-ai-v1', source_translation_file=str(input_path),
                source_translation_file_modified=False, counts=_counts(document['entries']),
                completed_unique=validation['completed_unique'], original_bytes=original_bytes,
                compact_bytes=compact_bytes, saved_bytes=original_bytes - compact_bytes,
                classification_preserved='classification_reference' in document,
                official_chinese_read='official_reference' in document, game_files_changed=False)


@lru_cache(maxsize=1)
def _font_coverage():
    data = _json_read(DATA_DIR / 'font-coverage.json')
    return frozenset(data['cjk_common'])


def _is_han(code):
    return any(first <= code <= last for first, last in (
        (0x3400, 0x4dbf), (0x4e00, 0x9fff), (0xf900, 0xfaff),
        (0x20000, 0x2a6df), (0x2a700, 0x2b73f), (0x2b740, 0x2b81f),
        (0x2b820, 0x2ceaf), (0x2ceb0, 0x2ebef), (0x2ebf0, 0x2ee5f),
        (0x2f800, 0x2fa1f), (0x30000, 0x3134f), (0x31350, 0x323af)))


def _format_errors(source, translation):
    from locres_codec import validate_text
    issues = validate_text(source, translation)
    source_icons = Counter(c for c in source if unicodedata.category(c) == 'Co')
    translated_icons = Counter(c for c in translation if unicodedata.category(c) == 'Co')
    if source_icons != translated_icons:
        issues.append('private-use icon code mismatch')
    missing_han = sorted({ord(character) for character in translation
                          if _is_han(ord(character)) and ord(character) not in _font_coverage()})
    if missing_han:
        issues.append('font missing Han glyphs: ' + ', '.join(f'U+{code:04X}' for code in missing_han))
    src_cf = Counter(c for c in source if unicodedata.category(c) == 'Cf')
    dst_cf = Counter(c for c in translation if unicodedata.category(c) == 'Cf')
    if src_cf != dst_cf:
        issues.append('format-control-character mismatch')
    if Counter(re.findall(r'\{\{|\}\}', source)) != Counter(re.findall(r'\{\{|\}\}', translation)):
        issues.append('escaped-braces mismatch')
    return issues


def _official_variant_indices(entry):
    """Map slots to distinct official texts in their first-occurrence order."""
    references = entry.get('official_zh')
    if (entry.get('category') != 'GameData' or not isinstance(references, list)
            or len(references) != len(entry['occurrences'])
            or any(not isinstance(item, dict) or not isinstance(item.get('text'), str)
                   for item in references)):
        raise ValueError(f"{entry['id']}: 数组译文需要游戏文本的官中参考。")
    texts, indices = {}, []
    for reference in references:
        text = reference['text']
        if text not in texts:
            texts[text] = len(texts)
        indices.append(texts[text])
    return len(texts), indices


def _validate_translation_value(entry, identity=None):
    """Accept variant arrays only when their slot mapping is unambiguous."""
    identity = entry['id'] if identity is None else identity
    translation = entry['translation']
    if translation is None or isinstance(translation, str):
        return
    if not isinstance(translation, list):
        raise ValueError(f'{identity}: translation 必须为字符串、null 或官中变体对应的译文数组。')
    count, _ = _official_variant_indices(entry)
    if count < 2 or len(translation) != count:
        raise ValueError(f'{identity}: 译文数组必须对应 {count} 个不同官中变体，且至少有两个变体。')
    if any(value is not None and not isinstance(value, str) for value in translation):
        raise ValueError(f'{identity}: 译文数组中的每项必须为字符串或 null。')


def translation_variant_index(entry, occurrence_index):
    """Return the official variant index for a slot, or None for scalar text."""
    if not isinstance(entry['translation'], list):
        return None
    _validate_translation_value(entry)
    _, indices = _official_variant_indices(entry)
    return indices[occurrence_index]


def translation_for_occurrence(entry, occurrence_index):
    """Resolve one slot without losing repeated official variants."""
    variant_index = translation_variant_index(entry, occurrence_index)
    return entry['translation'] if variant_index is None else entry['translation'][variant_index]


def _missing_policy(allow_partial=False, missing_policy=None, default='error'):
    policy = missing_policy if missing_policy is not None else ('en' if allow_partial else default)
    if policy not in ('keep', 'en', 'error'):
        raise ValueError('未填译文处理只能选择 keep、en 或 error。')
    if allow_partial and policy != 'en':
        raise ValueError('--allow-partial 的英文补全与未填保留设置冲突。')
    return policy


def _validate_document(document, allow_partial=False, missing_policy=None, import_options=None):
    missing_policy = _missing_policy(allow_partial, missing_policy)
    if isinstance(document, dict) and document.get('schema') == 'ac8-ai-v1':
        from ac8compact import expand_document
        document = expand_document(document)
    if not isinstance(document, dict) or document.get('schema') != SCHEMA:
        raise ValueError('不是受支持的汉化翻译文件。')
    scope = document.get('scope')
    include_official = 'official_reference' in document
    include_classification = 'classification_reference' in document
    expected = _document(scope, include_official=include_official,
                         include_classification=include_classification)
    if set(document) != set(expected):
        raise ValueError('翻译文件的顶层结构被修改。')
    for key in expected:
        if key not in ('entries', 'translation_selection') and document[key] != expected[key]:
            raise ValueError(f'不可修改的源字段发生变化：{key}')
    entries = document['entries']
    if not isinstance(entries, list) or len(entries) != len(expected['entries']):
        raise ValueError('翻译条目被新增或删除。')
    expected_by_id = {entry['id']: entry for entry in expected['entries']}
    short_ids = {entry['id']: number for number, entry in enumerate(expected['entries'], 1)}
    supplied_by_id = {}
    for entry in entries:
        if not isinstance(entry, dict):
            raise ValueError('翻译条目必须为 JSON 对象。')
        identity = entry.get('id')
        if identity not in expected_by_id or identity in supplied_by_id:
            raise ValueError(f'未知或重复条目 ID：{identity}')
        original = expected_by_id[identity]
        if set(entry) != set(original):
            raise ValueError(f'{identity}: 条目字段被新增或删除。')
        for key in original:
            if key not in ('translation', 'format_source', 'translate') and entry[key] != original[key]:
                raise ValueError(f'{identity}: 不可修改的源字段发生变化：{key}')
        format_source = entry['format_source']
        if format_source not in ('ja', 'en') or entry[format_source] is None:
            raise ValueError(f'{identity}: format_source 只能选择本条已有的 ja 或 en。')
        supplied_by_id[identity] = entry
        _validate_translation_value(entry, short_ids[identity])
    plan = selection_plan(document, supplied_by_id, import_options=import_options)
    missing, issues, completed_slots, missing_slots = [], [], Counter(), Counter()
    completed_unique = 0
    for identity, entry in supplied_by_id.items():
        entry_plan = plan['per_entry'][identity]
        if not entry_plan['selected']:
            continue
        format_source = entry['format_source']
        selected_variants = {}
        for index, occurrence in enumerate(entry_plan['occurrences']):
            if occurrence['selected']:
                variant_index = translation_variant_index(entry, index)
                selected_variants.setdefault(variant_index, []).append(index)
        missing_variant, variant_issues = False, []
        for variant_index, occurrence_indices in selected_variants.items():
            text = translation_for_occurrence(entry, occurrence_indices[0])
            if text is None:
                missing_variant = True
                missing_slots[entry['category']] += len(occurrence_indices)
                continue
            problems = _format_errors(entry[format_source], text)
            if entry['category'] == 'GameData':
                if '\0' in text and 'embedded NUL' not in problems:
                    problems.append('embedded NUL')
                try:
                    text.encode('utf-8', errors='strict')
                except UnicodeEncodeError:
                    problems.append('invalid UTF-8 surrogate')
            if problems:
                variant_issues.append(dict(variant_index=variant_index,
                                           occurrence_indices=occurrence_indices, problems=problems))
            else:
                completed_slots[entry['category']] += len(occurrence_indices)
        if missing_variant:
            missing.append(identity)
        else:
            completed_unique += 1
        if variant_issues:
            bad_indices = {index for issue in variant_issues for index in issue['occurrence_indices']}
            issue = dict(id=identity, short_id=short_ids[identity], format_source=format_source,
                         occurrences=[occurrence for index, occurrence in enumerate(entry['occurrences'])
                                      if index in bad_indices][:3],
                         problems=list(dict.fromkeys(problem for variant in variant_issues
                                                     for problem in variant['problems'])))
            if isinstance(entry['translation'], list):
                issue['variant_issues'] = variant_issues
            issues.append(issue)
    result = dict(ok=not issues and (missing_policy != 'error' or not missing), scope=scope,
                counts=_counts(entries), completed_unique=completed_unique,
                missing_unique=len(missing), missing_ids=missing[:30],
                missing_short_ids=[short_ids[identity] for identity in missing[:30]],
                missing_game_slots=missing_slots['GameData'],
                missing_engine_slots=missing_slots['Engine'], missing_policy=missing_policy,
                completed_game_slots=completed_slots['GameData'],
                completed_engine_slots=completed_slots['Engine'],
                format_issue_count=len(issues), format_issues=issues,
                source_fields_preserved=True, partial_mode=missing_policy == 'en',
                official_chinese_read=include_official, selection_counts=plan['counts'],
                selection=plan['selection'])
    if 'import_options' in plan:
        result.update(import_options=plan['import_options'], import_protection=plan['import_protection'])
    return result, supplied_by_id


def inspect_selection(input_path):
    from ac8categories import CATEGORY_DEFINITIONS, categories_for_entry
    input_path = Path(input_path).resolve()
    document = load_translation_document(input_path)
    _, entries = _validate_document(document, allow_partial=True)
    plan = selection_plan(document, entries)
    slots, selected_slots, category_entries = Counter(), Counter(), Counter()
    for identity, item in plan['per_entry'].items():
        entry_categories = set()
        inferred = categories_for_entry(entries[identity]) if not plan['classification_enabled'] else None
        for index, occurrence in enumerate(item['occurrences']):
            category = inferred[index]['category'] if inferred is not None else occurrence['category']
            slots[category] += 1
            selected_slots[category] += occurrence['selected']
            entry_categories.add(category)
        category_entries.update(entry_categories)
    categories = [dict(definition, slots=slots[definition['id']],
                       entries=category_entries[definition['id']],
                       selected_slots=selected_slots[definition['id']])
                  for definition in CATEGORY_DEFINITIONS]
    return dict(ok=True, categories=categories, selection=plan['selection'],
                counts=plan['counts'], input_sha256=_sha256(input_path),
                classification_enabled=plan['classification_enabled'])


def configure_translation_selection(input_path, output_path, selected_categories=None,
                                    excluded_policy='official'):
    input_path, output_path = Path(input_path).resolve(), Path(output_path).resolve()
    if output_path.exists():
        raise ValueError(f'目标已存在，为避免覆盖译文，请选新文件：{output_path}')
    document = load_translation_document(input_path)
    _, entries = _validate_document(document, allow_partial=True)
    revised = _document(document['scope'], include_official='official_reference' in document,
                        include_classification=True, selected_categories=selected_categories,
                        excluded_policy=excluded_policy)
    for entry in revised['entries']:
        existing = entries[entry['id']]
        entry['translation'] = existing['translation']
        entry['format_source'] = existing['format_source']
        entry['translate'] = existing.get('translate')
    plan = selection_plan(revised)
    _json_write(output_path, revised)
    return dict(ok=True, action='configure_selection', translation_file=str(output_path),
                counts=plan['counts'], selection=plan['selection'],
                source_translation_file=str(input_path), source_translation_file_modified=False,
                official_chinese_read='official_reference' in document, game_files_changed=False)


def inspect_import_protection(input_path, import_options=None, report_path=None):
    """Preview all recognized display-sensitive slots without accessing the game."""
    from ac8importoptions import DEFAULT_IMPORT_OPTIONS, normalize_options
    options = normalize_options(DEFAULT_IMPORT_OPTIONS if import_options is None else import_options)
    document = load_translation_document(input_path)
    validation, entries = _validate_document(document, missing_policy='keep', import_options=options)
    plan = selection_plan(document, entries, import_options=options)
    official, _ = _official_sources()
    short_ids = {entry['id']: number for number, entry in enumerate(_entries(document['scope']), 1)}
    rows = []
    for entry in entries.values():
        for index, slot in enumerate(plan['per_entry'][entry['id']]['occurrences']):
            if not slot.get('import_groups'):
                continue
            identity = slot['domain'], slot['index']
            text = translation_for_occurrence(entry, index)
            if slot['force_original_official'] or text is None and slot['selected']:
                effective = official[identity]
            elif not slot['selected']:
                effective = (official[identity] if plan['selection']['excluded_policy'] == 'official'
                             else entry['en'] if entry['en'] is not None else entry['ja'])
            else:
                effective = text
            rows.append(dict(short_id=short_ids[entry['id']], domain=slot['domain'], index=slot['index'],
                             text_ids_ascii=slot['text_ids_ascii'], groups=slot['import_groups'],
                             protected=slot['force_original_official'],
                             selected_for_translation=slot['selected'],
                             ja=entry['ja'], en=entry['en'], official_zh=official[identity],
                             translation=text, effective_text=effective))
    rows.sort(key=lambda row: (row['domain'], row['index']))
    report = dict(ok=True, translation_file=str(Path(input_path).resolve()),
                  import_options=options, import_protection=plan['import_protection'], rows=rows,
                  translation_file_modified=False, game_accessed=False, game_files_changed=False,
                  validation=dict(ok=validation['ok'], format_issue_count=validation['format_issue_count']))
    if report_path:
        _json_write(report_path, report)
        report['report_path'] = str(Path(report_path).resolve())
    return report


def check_translations(game_dir, input_path=None, allow_partial=False, report_path=None,
                       missing_policy=None, import_options=None):
    missing_policy = _missing_policy(allow_partial, missing_policy, default='keep')
    version = check_game_version(game_dir)
    if input_path is None:
        report = dict(ok=version['ok'], action='check', game_check=version,
                      official_chinese_read=False, game_files_changed=False)
    else:
        validation, _ = _validate_document(load_translation_document(input_path),
                                           missing_policy=missing_policy, import_options=import_options)
        report = dict(**validation, action='check', game_check=version,
                      translation_file=str(Path(input_path).resolve()), game_files_changed=False)
        report['ok'] = report['ok'] and version['ok']
        if missing_policy == 'keep' and validation['missing_engine_slots']:
            report['ok'] = False
            report['missing_policy_errors'] = [
                '引擎文本缺少原官中模板，不能保留未填位置；请仅导入 game 范围。']
    if report_path:
        _json_write(report_path, report)
        report['report_path'] = str(Path(report_path).resolve())
    return report


def _xor(data, culture_index):
    seed, state = (len(data) + culture_index) & 0xffffffff, 0
    output = bytearray(data)
    for index in range(len(output)):
        state = ((state * 2) | ((~(state ^ (state << 3)) >> 7) & 1)) & 0xffffffff
        seed = (seed * 5 + 1) & 0xffffffff
        output[index] ^= (seed + state) & 0xff
    return bytes(output)


def _encode_pool(strings):
    plain = b''.join(text.encode('utf-8', errors='strict') + b'\0' for text in strings)
    compressed = zlib.compress(plain, 6)
    encoded = _xor(compressed, 12)
    if encoded[:2] == b'\x78\x9c':
        for strategy in (zlib.Z_FILTERED, zlib.Z_HUFFMAN_ONLY, zlib.Z_RLE):
            compressor = zlib.compressobj(6, zlib.DEFLATED, zlib.MAX_WBITS, 8, strategy)
            compressed = compressor.compress(plain) + compressor.flush()
            encoded = _xor(compressed, 12)
            if encoded[:2] != b'\x78\x9c':
                break
        else:
            raise ValueError('无法生成无歧义的原生数据包。')
    decoder = zlib.decompressobj()
    decoded = decoder.decompress(_xor(encoded, 12)) + decoder.flush()
    if not decoder.eof or decoder.unused_data or decoder.unconsumed_tail or decoded != plain:
        raise ValueError('原生数据包回解校验失败。')
    roundtrip = [part.decode('utf-8', errors='strict') for part in decoded.split(b'\0')[:-1]]
    if roundtrip != strings or decoded.count(b'\0') != len(strings):
        raise ValueError('译文槽顺序或 NUL 分隔数量校验失败。')
    return encoded, len(plain)


def _entry_translation(entry, partial, occurrence_index=0):
    translation = translation_for_occurrence(entry, occurrence_index)
    if translation is not None:
        return translation
    if not partial:
        raise ValueError(f"未填写译文：{entry['id']}")
    return entry['en'] if entry['en'] is not None else entry['ja']


def _build_payload(payload_dir, entries, partial, document=None, selection_report=None,
                   missing_policy=None, preserved_game=None, import_options=None):
    missing_policy = _missing_policy(partial, missing_policy)
    game, engine, _, _ = _sources()
    if import_options is not None and document is None:
        raise ValueError('导入保护需要原翻译文档以确认逐位置映射。')
    plan = selection_plan(document, entries, import_options=import_options) if document is not None else None
    excluded_policy = plan['selection']['excluded_policy'] if plan else 'en'
    official = None
    excluded_sources, missing_sources = Counter(), Counter()
    translations = {}
    for entry in entries.values():
        entry_plan = plan['per_entry'][entry['id']] if plan else None
        for occurrence_index, occurrence in enumerate(entry['occurrences']):
            if entry['category'] == 'GameData':
                identity = occurrence['domain'], occurrence['index']
            else:
                identity = occurrence['target_path'], occurrence['namespace'], occurrence['key']
            if identity in translations:
                raise ValueError('译文出现重复目标槽。')
            slot_plan = entry_plan['occurrences'][occurrence_index] if entry_plan else None
            selected = slot_plan is None or slot_plan['selected']
            if slot_plan and slot_plan.get('force_original_official'):
                if entry['category'] != 'GameData':
                    raise ValueError('显示保护只能应用于已有原官中映射的游戏文本。')
                if official is None:
                    official, _ = _official_sources()
                translations[identity] = official[identity]
                excluded_sources['official_game_slots'] += 1
            elif selected:
                text = translation_for_occurrence(entry, occurrence_index)
                if text is None and missing_policy == 'keep':
                    if entry['category'] != 'GameData':
                        raise ValueError('引擎文本缺少原官中模板，不能保留未填位置；请仅导入 game 范围。')
                    if preserved_game is None:
                        if official is None:
                            official, _ = _official_sources()
                        preserved_game = official
                    if identity not in preserved_game:
                        raise ValueError(f'缺少未填位置的原内容：{identity}')
                    translations[identity] = preserved_game[identity]
                    missing_sources['preserved_game_slots'] += 1
                else:
                    translations[identity] = _entry_translation(entry, missing_policy == 'en', occurrence_index)
                    if text is None:
                        missing_sources['english_game_slots' if entry['category'] == 'GameData'
                                        else 'english_engine_slots'] += 1
            elif entry['category'] == 'GameData' and excluded_policy == 'official':
                if official is None:
                    official, _ = _official_sources()
                translations[identity] = official[identity]
                excluded_sources['official_game_slots'] += 1
            else:
                translations[identity] = entry['en'] if entry['en'] is not None else entry['ja']
                excluded_sources['english_game_slots' if entry['category'] == 'GameData'
                                 else 'english_engine_slots'] += 1
    if selection_report is not None:
        selection_report.update(official_chinese_read=official is not None,
                                excluded_policy=excluded_policy,
                                excluded_sources=dict(excluded_sources),
                                missing_policy=missing_policy, missing_sources=dict(missing_sources),
                                excluded_engine_policy='English source; Japanese only when English is absent',
                                selection_counts=plan['counts'] if plan else None)
        if plan and 'import_options' in plan:
            selection_report.update(import_options=plan['import_options'],
                                    import_protection=plan['import_protection'])
    files, pool_reports = [], []
    for domain in ('CP', 'ON'):
        source = sorted((row for row in game if row['domain'] == domain), key=lambda row: row['index'])
        if [row['index'] for row in source] != list(range(len(source))):
            raise ValueError('捆绑源目录中的游戏索引不连续。')
        strings = [translations[(domain, index)] for index in range(len(source))]
        encoded, plain_size = _encode_pool(strings)
        relative = f'Live/Content/Localization/GameData/{domain}_M.dat'
        destination = _relative_path(payload_dir, relative)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(encoded)
        files.append(dict(path=relative, sha256=hashlib.sha256(encoded).hexdigest(), bytes=len(encoded),
                          category='GameData'))
        pool_reports.append(dict(domain=domain, pool_slots=len(strings), utf8_bytes=plain_size,
                                 culture_index=12, index_order_verified=True, nul_count_verified=True,
                                 common_tree_modified=False, original_id_mapping_preserved=True))
    engine_reports = []
    if any(entry['category'] == 'Engine' for entry in entries.values()):
        from locres_codec import LocRes
        cultures_file = DATA_DIR / 'engine-target-cultures.json'
        cultures = _json_read(cultures_file) if cultures_file.exists() else {}
        targets = sorted({row['target_path'] for row in engine})
        for target_path in targets:
            rows = [row for row in engine if row['target_path'] == target_path]
            target = rows[0]['target']
            template = LocRes.load(DATA_DIR / 'locres' / f'{target}.en.locres')
            catalog_by_id = {(row['namespace'], row['key']): row for row in rows}
            if len(catalog_by_id) != len(template.entries):
                raise ValueError(f'{target}: 捆绑引擎模板与源目录的条目数量不一致。')
            for entry in template.entries:
                row = catalog_by_id.get(entry.identity)
                if row is None or entry.signature != (row['namespace'], row['key'], row['source_hash'],
                                                       row['namespace_hash'], row['key_hash']) or entry.text != row['en']:
                    raise ValueError(f'{target}: 捆绑引擎模板与日英语源不一致。')
            mapping = {(row['namespace'], row['key']): translations[
                (target_path, row['namespace'], row['key'])] for row in rows}
            raw = template.build(mapping, require_complete=True)
            parsed = LocRes.parse(raw)
            if [entry.signature for entry in template.entries] != [entry.signature for entry in parsed.entries]:
                raise ValueError(f'{target}: 引擎文本的 key 或 hash 发生变化。')
            if [entry.text for entry in parsed.entries] != [mapping[entry.identity] for entry in template.entries]:
                raise ValueError(f'{target}: 引擎文本回解与译文不一致。')
            culture = cultures.get(target_path, 'zh-Hans')
            parent, filename = target_path.rsplit('/', 1)
            relative = f'{parent}/{culture}/{filename}'
            destination = _relative_path(payload_dir, relative)
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(raw)
            files.append(dict(path=relative, sha256=hashlib.sha256(raw).hexdigest(), bytes=len(raw),
                              category='Engine'))
            engine_reports.append(dict(target=target, entries=len(parsed.entries), version=parsed.version,
                                       identity_hashes_preserved=True, decoded_translation_verified=True,
                                       staged=True, deployed=False))
    return files, pool_reports, engine_reports


def stage_translations(game_dir, input_path, workspace, allow_partial=False, missing_policy=None,
                       import_options=None):
    missing_policy = _missing_policy(allow_partial, missing_policy, default='keep')
    document = load_translation_document(input_path)
    validation, entries = _validate_document(document, missing_policy=missing_policy,
                                           import_options=import_options)
    version = check_game_version(game_dir)
    if not version['ok']:
        raise ValueError('；'.join(version['errors']))
    if not validation['ok']:
        missing_detail = (f"{validation['missing_unique']} 条未填写，" if missing_policy == 'error'
                          else f"{validation['missing_unique']} 条未填写将使用"
                               + ('英文补全；' if missing_policy == 'en' else '原官中；'))
        report_path = _relative_path(workspace, 'check-report.json')
        failed_report = dict(**validation, action='import_check', game_check=version,
                             translation_file=str(Path(input_path).resolve()),
                             game_files_changed=False, game_installed=False,
                             report_path=str(report_path))
        _json_write(report_path, failed_report)
        descriptions = {
            'newlines mismatch': '换行数量或类型不一致',
            'control-character mismatch': '控制字符不一致',
            'braces mismatch': '变量不一致',
            'tags mismatch': '控制标签不一致',
            'leading whitespace mismatch': '开头空白不一致',
            'trailing whitespace mismatch': '结尾空白不一致',
        }
        details = []
        for issue in validation['format_issues'][:10]:
            problems = '；'.join(descriptions.get(problem, problem) for problem in issue['problems'])
            details.append(f"#{issue['short_id']}（格式参照 {issue['format_source']}）：{problems}")
            for variant in issue.get('variant_issues', []):
                details.append(f"  译文数组第 {variant['variant_index'] + 1} 项："
                               + '；'.join(descriptions.get(problem, problem)
                                         for problem in variant['problems']))
        detail_text = '\n' + '\n'.join(details) if details else ''
        raise ValueError(f"译文尚未通过检查：{missing_detail}"
                         f"{validation['format_issue_count']} 条格式错误。"
                         f"{detail_text}\n完整报告：{report_path}")
    preserved_game, preserved_report = None, None
    if missing_policy == 'keep' and validation['missing_engine_slots']:
        raise ValueError('引擎文本缺少原官中模板，不能保留未填位置；请仅导入 game 范围。')
    if missing_policy == 'keep' and validation['missing_game_slots']:
        preserved_game, _ = _official_sources()
        preserved_report = dict(source='original_official', official_chinese_read=True)
    workspace = Path(workspace).resolve()
    workspace.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix='.ac8-stage-', dir=workspace))
    payload = workspace / 'payload'
    try:
        selection_report = {}
        files, pools, engine = _build_payload(temporary, entries, False,
                                             document=document, selection_report=selection_report,
                                             missing_policy=missing_policy, preserved_game=preserved_game,
                                             import_options=import_options)
        previous = None
        if payload.exists():
            if not payload.is_dir() or payload.is_symlink():
                raise ValueError('已有 payload 不是普通工作目录。')
            old_state = workspace / 'stage-state.json'
            if not old_state.exists() or _json_read(old_state).get('schema') != 'ac8-stage-v1':
                raise ValueError('已有 payload 没有本工具的标记；不会覆盖。')
            previous = 'stage-backups/' + str(int(time.time())) + '-' + uuid.uuid4().hex + '/payload'
            backup = _relative_path(workspace, previous)
            backup.parent.mkdir(parents=True, exist_ok=True)
            payload.replace(backup)
        temporary.replace(payload)
        state = dict(schema='ac8-stage-v1', payload_dir='payload', previous_payload=previous,
                     files=files, translation_file=str(Path(input_path).resolve()),
                     source_revision=document['source_revision'])
        if 'import_options' in validation:
            state['import_options'] = validation['import_options']
        _json_write(workspace / 'stage-state.json', state)
        report = dict(**validation, action='import_stage', payload_dir=str(payload), files=files,
                      game_check=version, pools=pools, engine=engine,
                      game_files_changed=False, game_installed=False,
                      fallback_unique=validation['missing_unique'],
                      preserved_source=preserved_report,
                      fallback_policy=('Original official Chinese at each slot' if missing_policy == 'keep' else
                                       'English source; Japanese only when English is absent'
                                       if missing_policy == 'en' else None))
        report['official_chinese_read'] = bool(validation['official_chinese_read'] or
                                              selection_report['official_chinese_read'] or
                                              (preserved_report or {}).get('official_chinese_read', False))
        report['selection_build'] = selection_report
        report_path = workspace / 'build-report.json'
        _json_write(report_path, report)
        report['report_path'] = str(report_path)
        return report
    finally:
        # Only this call's checked temporary directory is eligible for deletion.
        if temporary.exists():
            safe = temporary.resolve()
            safe.relative_to(workspace)
            if not safe.name.startswith('.ac8-stage-'):
                raise ValueError('临时目录检查失败。')
            shutil.rmtree(safe)


def restore_game(game_dir, workspace):
    try:
        adapter = importlib.import_module('deployment_api')
        restore = getattr(adapter, 'restore')
    except (ImportError, AttributeError) as error:
        raise ValueError('游戏撤回接口尚未接入；此引擎只负责导出、校验和生成待导入文件。') from error
    return restore(str(Path(game_dir).resolve()))


def main():
    parser = argparse.ArgumentParser(description='AC8翻译工具')
    sub = parser.add_subparsers(dest='command', required=True)
    for command in ('export', 'check', 'import', 'restore'):
        cli = sub.add_parser(command)
        cli.add_argument('--game-dir', default=GAME_DEFAULT)
        if command in ('export', 'import', 'restore'):
            cli.add_argument('--workspace', required=True)
        if command in ('check', 'import'):
            cli.add_argument('--input', required=command == 'import')
            cli.add_argument('--allow-partial', action='store_true')
            cli.add_argument('--missing-policy', choices=('keep', 'en', 'error'),
                             help='未填默认使用原官中；en 英文补全；error 要求全部填完。')
            cli.add_argument('--translate-hud-labels', action='store_true',
                             help='替换已识别的 HUD 标牌和短提示；默认保留原标牌。')
            cli.add_argument('--translate-equipment-short', action='store_true',
                             help='替换 HUD/科技树/机库共用的武器、机型和零件简称；默认保留原简称。')
        if command == 'export':
            from ac8categories import CATEGORY_DEFINITIONS
            cli.add_argument('--scope', choices=('game', 'all'), default='game')
            cli.add_argument('--output')
            languages = cli.add_mutually_exclusive_group()
            languages.add_argument('--with-official', dest='include_official', action='store_true',
                                   help='导出日英及只读官方简体中文（默认）。')
            languages.add_argument('--without-official', dest='include_official', action='store_false',
                                   help='仅导出日英原文，不附官方中文。')
            cli.set_defaults(include_official=True)
            cli.add_argument('--full', action='store_true',
                             help='导出完整本地元数据；默认导出 AI 精简格式。')
            cli.add_argument('--without-classification', action='store_true',
                             help=argparse.SUPPRESS)
            cli.add_argument('--category', action='append',
                             choices=[item['id'] for item in CATEGORY_DEFINITIONS],
                             help=argparse.SUPPRESS)
            cli.add_argument('--excluded-policy', choices=('official', 'en'), default='official',
                             help=argparse.SUPPRESS)
        if command == 'check':
            cli.add_argument('--report')
    cli = sub.add_parser('compact', help='为已有翻译文件另存 AI 精简副本。')
    cli.add_argument('--input', required=True)
    cli.add_argument('--output', required=True)
    args = parser.parse_args()
    try:
        if args.command == 'export':
            report = export_translations(args.game_dir, args.workspace, args.scope, args.output,
                                         include_official=args.include_official,
                                         include_classification=bool(args.category) and not args.without_classification,
                                         selected_categories=args.category,
                                         excluded_policy=args.excluded_policy,
                                         compact=not args.full)
        elif args.command == 'compact':
            report = compact_translation_file(args.input, args.output)
        elif args.command == 'check':
            report = check_translations(args.game_dir, args.input, args.allow_partial, args.report,
                                        missing_policy=args.missing_policy, import_options=dict(
                                            hud_labels=args.translate_hud_labels,
                                            equipment_short=args.translate_equipment_short))
        elif args.command == 'import':
            report = stage_translations(args.game_dir, args.input, args.workspace, args.allow_partial,
                                        missing_policy=args.missing_policy, import_options=dict(
                                            hud_labels=args.translate_hud_labels,
                                            equipment_short=args.translate_equipment_short))
        else:
            report = restore_game(args.game_dir, args.workspace)
        print(json.dumps(report, ensure_ascii=False))
        return 0 if report.get('ok', False) else 2
    except (ValueError, OSError) as error:
        print(json.dumps(dict(ok=False, error=str(error), game_files_changed=False), ensure_ascii=False))
        return 2


if __name__ == '__main__':
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8')
    raise SystemExit(main())
