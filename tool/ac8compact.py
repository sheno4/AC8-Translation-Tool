"""Compact AI translation exchange, reconstructed from bundled source catalogs.

Only short IDs, sources and translations travel in rows. Game slot metadata
and optional legacy selection rules are reconstructed locally before checking.
This module does not read catalogs or official Chinese merely on import.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import uuid

SCHEMA = 'ac8-ai-v1'
COLUMNS = ['id', 'ja', 'en', 'translation']
OFFICIAL_COLUMNS = ['id', 'ja', 'en', 'official_zh', 'translation']
RESULT_COLUMNS = ['id', 'translation']
_REQUIRED = {'schema', 'source_revision', 'scope', 'columns', 'rows'}
_OPTIONAL = {'format_sources', 'selection', 'translate_overrides', 'classification_revision',
             'official_revision'}


def _engine():
    # Lazy import also allows ac8tool to import this module in its reader.
    import ac8tool
    return ac8tool


def _english(entry):
    """Zero denotes English identical to a nonmissing Japanese source."""
    return 0 if entry['ja'] is not None and entry['ja'] == entry['en'] else entry['en']


def _official(entry):
    if 'official_zh' not in entry:
        return None
    texts = list(dict.fromkeys(item['text'] for item in entry['official_zh']))
    return texts[0] if len(texts) == 1 else texts


def pack_document(document):
    """Project a validated full document into stable, compact source rows."""
    engine = _engine()
    if not isinstance(document, dict) or document.get('schema') != engine.SCHEMA:
        raise ValueError('精简转换需要受支持的完整翻译文档。')
    sources = engine._entries(document['scope'])
    supplied = {entry['id']: entry for entry in document['entries']}
    if len(supplied) != len(sources) or set(supplied) != {entry['id'] for entry in sources}:
        raise ValueError('精简转换的完整条目 ID 集合不一致。')
    official = 'official_reference' in document
    result = dict(schema=SCHEMA, source_revision=document['source_revision'],
                  scope=document['scope'], columns=list(OFFICIAL_COLUMNS if official else COLUMNS),
                  rows=[])
    if official:
        result['official_revision'] = hashlib.sha256(engine._canonical(document['official_reference'])).hexdigest()
    formats, overrides = {}, {}
    for number, source in enumerate(sources, 1):
        entry = supplied[source['id']]
        row = [number, entry['ja'], _english(entry)]
        if official:
            row.append(_official(entry))
        row.append(entry['translation'])
        result['rows'].append(row)
        if entry['format_source'] != source['format_source']:
            formats[str(number)] = entry['format_source']
        if entry.get('translate') is not None:
            overrides[str(number)] = entry['translate']
    if formats:
        result['format_sources'] = formats
    if 'classification_reference' in document:
        # Only the chosen categories and sparse per-entry overrides travel;
        # classification reasons, hashes, labels and slots remain local.
        result['selection'] = dict(document['translation_selection'])
        result['classification_revision'] = document['classification_reference']['rules_sha256']
        if overrides:
            result['translate_overrides'] = overrides
    return result


def _sparse_values(document, name, entries, allowed):
    values = document.get(name, {})
    if not isinstance(values, dict):
        raise ValueError(f'{name} 必须为短 ID 到设置的 JSON 对象。')
    for identity, value in values.items():
        if (not isinstance(identity, str) or not identity.isascii() or
                not identity.isdecimal() or identity.startswith('0')):
            raise ValueError(f'{name} 包含无效的短 ID。')
        number = int(identity)
        if not 1 <= number <= len(entries) or not allowed(value, entries[number - 1]):
            raise ValueError(f'{name}: 短 ID {identity} 或设置无效。')
    return values


def expand_document(document):
    """Strictly restore a self-contained compact file using the current catalog.

    Rows may be reordered, but no ID, original source or required row may be
    changed. Translation null remains null; it is never an implicit merge.
    """
    engine = _engine()
    if not isinstance(document, dict) or document.get('schema') != SCHEMA:
        raise ValueError('不是受支持的 AI 精简翻译文件。')
    if not _REQUIRED <= set(document) or set(document) - _REQUIRED - _OPTIONAL:
        raise ValueError('AI 精简文件的顶层结构被修改。')
    if document['scope'] not in ('game', 'all'):
        raise ValueError('AI 精简文件的 scope 必须为 game 或 all。')
    if document['source_revision'] != engine._sources()[3]:
        raise ValueError('AI 精简文件的日英原文版本不一致：source_revision。')
    columns = document['columns']
    if columns not in (COLUMNS, OFFICIAL_COLUMNS, RESULT_COLUMNS):
        raise ValueError('AI 精简文件的 columns 列定义被修改。')
    result_only = columns == RESULT_COLUMNS
    official = columns == OFFICIAL_COLUMNS or result_only and 'official_revision' in document
    if not official and 'official_revision' in document:
        raise ValueError('日英精简文件不能包含 official_revision。')
    classified = 'selection' in document
    if 'translate_overrides' in document and not classified:
        raise ValueError('translate_overrides 必须与原有 selection 一起保留。')
    if classified:
        if document.get('classification_revision') != engine._classification_reference()['rules_sha256']:
            raise ValueError('AI 精简文件的原有分类规则版本不一致：classification_revision。')
    elif 'classification_revision' in document:
        raise ValueError('classification_revision 必须与原有 selection 一起保留。')
    expected = engine._document(document['scope'], include_official=official,
                                include_classification=classified)
    if official:
        reference_digest = hashlib.sha256(engine._canonical(expected['official_reference'])).hexdigest()
        if document.get('official_revision') != reference_digest:
            raise ValueError('AI 精简文件的官中参考版本不一致：official_revision。')
    entries = expected['entries']
    rows = document['rows']
    if not isinstance(rows, list) or len(rows) != len(entries):
        raise ValueError('AI 精简文件的翻译条目被新增或删除。')
    seen = set()
    for row in rows:
        if not isinstance(row, list) or len(row) != len(columns):
            raise ValueError('AI 精简文件的 row 列数或结构被修改。')
        number = row[0]
        translation = row[-1]
        if type(number) is not int or not 1 <= number <= len(entries) or number in seen:
            raise ValueError('AI 精简文件包含未知、重复或无效的短 ID。')
        seen.add(number)
        entry = entries[number - 1]
        if not result_only:
            ja, en = row[1:3]
            if (ja is not None and not isinstance(ja, str) or
                    en is not None and not isinstance(en, str) and not (type(en) is int and en == 0)):
                raise ValueError(f'{number}: 原文只能为字符串或 null；相同英文使用整数 0。')
            if ja != entry['ja'] or en != _english(entry):
                raise ValueError(f'{number}: 不可修改的日英原文发生变化。')
        if official and not result_only:
            reference = row[3]
            if (reference is not None and not isinstance(reference, str) and
                    not (isinstance(reference, list) and all(isinstance(item, str) for item in reference))):
                raise ValueError(f'{number}: 官中参考列格式无效。')
            if reference != _official(entry):
                raise ValueError(f'{number}: 不可修改的官中参考发生变化。')
        entry['translation'] = translation
        engine._validate_translation_value(entry, number)
    formats = _sparse_values(document, 'format_sources', entries,
                             lambda value, entry: isinstance(value, str) and
                             value in ('ja', 'en') and entry[value] is not None)
    for number, value in formats.items():
        entries[int(number) - 1]['format_source'] = value
    if classified:
        selection = document['selection']
        if not isinstance(selection, dict) or set(selection) != {'selected_categories', 'excluded_policy'}:
            raise ValueError('selection 结构被修改。')
        expected['translation_selection'] = engine._selection_options(**selection)
        overrides = _sparse_values(document, 'translate_overrides', entries,
                                   lambda value, entry: type(value) is bool)
        for number, value in overrides.items():
            entries[int(number) - 1]['translate'] = value
    return expected


def write_document(path, document):
    """Atomically write UTF-8, one compact row per physical line."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + '.tmp-' + uuid.uuid4().hex)
    def encoded(value):
        return json.dumps(value, ensure_ascii=False, separators=(',', ':')).encode(
            'utf-8', errors='backslashreplace')
    try:
        with temporary.open('wb') as stream:
            stream.write(b'{\n')
            items = [(key, value) for key, value in document.items() if key != 'rows']
            for key, value in items:
                stream.write(encoded(key) + b':' + encoded(value) + b',\n')
            stream.write(b'"rows":[\n')
            rows = document['rows']
            for index, row in enumerate(rows):
                stream.write(encoded(row) + (b',\n' if index + 1 < len(rows) else b'\n'))
            stream.write(b']\n}\n')
        temporary.replace(path)
    finally:
        if temporary.exists():
            temporary.unlink()
