"""Read/write UE LocRes v0-v3 while retaining identity and source hashes.

Format references: CUE4Parse UE4/Localization/FTextLocalizationResource.cs
and UE4/Objects/Core/i18N/{FTextKey,FEntry,ELocResVersion}.cs.
No archive extraction or game writes are performed by this module.
"""
from __future__ import annotations

import argparse
from collections import Counter
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import re
import struct
import unicodedata

MAGIC = struct.pack('<4I', 0x7574140E, 0xFC034A67, 0x9D90154A, 0x1B7F37C3)


class Reader:
    def __init__(self, data: bytes):
        self.data, self.pos = data, 0

    def take(self, count: int) -> bytes:
        if count < 0 or self.pos + count > len(self.data):
            raise ValueError(f'Invalid read size {count} at {self.pos}')
        raw = self.data[self.pos:self.pos + count]
        self.pos += count
        return raw

    def unpack(self, code: str):
        return struct.unpack(code, self.take(struct.calcsize(code)))[0]

    def fstring(self) -> tuple[str, bytes]:
        start = self.pos
        length = self.unpack('<i')
        if length == -2147483648:
            raise ValueError('Corrupted FString length')
        if not length:
            value = ''
        elif length < 0:
            raw = self.take(-length * 2)
            if not raw.endswith(b'\x00\x00'):
                raise ValueError('UTF-16 FString lacks NUL terminator')
            value = raw[:-2].decode('utf-16-le', errors='strict')
        else:
            raw = self.take(length)
            if not raw.endswith(b'\x00'):
                raise ValueError('ANSI FString lacks NUL terminator')
            # Cooked text uses ANSI only for ASCII. Keys are retained byte-for-byte.
            try:
                value = raw[:-1].decode('ascii')
            except UnicodeDecodeError:
                value = raw[:-1].decode('cp1252', errors='strict')
        if '\x00' in value:
            raise ValueError('Embedded NUL in FString')
        return value, self.data[start:self.pos]


def fstring(value: str) -> bytes:
    if '\x00' in value:
        raise ValueError('Embedded NUL in translation')
    if not value:
        return struct.pack('<i', 0)
    if value.isascii():
        encoded = value.encode('ascii') + b'\x00'
        return struct.pack('<i', len(encoded)) + encoded
    encoded = value.encode('utf-16-le', errors='strict') + b'\x00\x00'
    # UE counts UTF-16 code units, including NUL, rather than Python characters.
    return struct.pack('<i', -(len(encoded) // 2)) + encoded


@dataclass
class Entry:
    namespace: str
    key: str
    source_hash: int
    text: str
    namespace_hash: int | None
    key_hash: int | None
    index_offset: int | None
    string_index: int | None
    legacy_string_start: int | None = None
    legacy_string_end: int | None = None

    @property
    def identity(self):
        return self.namespace, self.key

    @property
    def signature(self):
        return self.namespace, self.key, self.source_hash, self.namespace_hash, self.key_hash


@dataclass
class LocRes:
    version: int
    original: bytes
    entries: list[Entry]
    strings: list[str]
    strings_raw: list[bytes]
    string_refcounts: list[int | None]
    pool_offset: int | None
    namespace_count: int
    source_path: str | None = None

    @classmethod
    def load(cls, path: str | Path) -> 'LocRes':
        path = Path(path)
        resource = cls.parse(path.read_bytes())
        resource.source_path = str(path.resolve())
        return resource

    @classmethod
    def parse(cls, data: bytes) -> 'LocRes':
        reader = Reader(data)
        version = 0
        if data.startswith(MAGIC):
            reader.pos = 16
            version = reader.unpack('<B')
        if version not in range(4):
            raise ValueError(f'Unsupported LocRes version {version}')
        pool_offset = reader.unpack('<q') if version >= 1 else None
        declared_count = reader.unpack('<I') if version >= 2 else None
        namespace_count = reader.unpack('<I')
        entries: list[Entry] = []
        identities: set[tuple[str, str]] = set()
        for _ in range(namespace_count):
            namespace_hash = reader.unpack('<I') if version >= 2 else None
            namespace, _ = reader.fstring()
            key_count = reader.unpack('<I')
            for _ in range(key_count):
                key_hash = reader.unpack('<I') if version >= 2 else None
                key, _ = reader.fstring()
                source_hash = reader.unpack('<I')
                if (namespace, key) in identities:
                    raise ValueError(f'Duplicate LocRes identity {namespace!r}/{key!r}')
                identities.add((namespace, key))
                if version >= 1:
                    index_offset = reader.pos
                    string_index = reader.unpack('<i')
                    entry = Entry(namespace, key, source_hash, '', namespace_hash, key_hash,
                                  index_offset, string_index)
                else:
                    start = reader.pos
                    value, _ = reader.fstring()
                    entry = Entry(namespace, key, source_hash, value, namespace_hash, key_hash,
                                  None, None, start, reader.pos)
                entries.append(entry)
        if declared_count is not None and declared_count != len(entries):
            raise ValueError(f'LocRes count mismatch: {declared_count} != {len(entries)}')
        strings, strings_raw, refcounts = [], [], []
        if version >= 1:
            if pool_offset == -1:
                if entries:
                    raise ValueError('Nonempty LocRes lacks string pool')
            else:
                if pool_offset != reader.pos:
                    raise ValueError(f'Unexpected string pool offset {pool_offset}; body ended {reader.pos}')
                reader.pos = pool_offset
                for _ in range(reader.unpack('<I')):
                    value, raw = reader.fstring()
                    refcount = reader.unpack('<i') if version >= 2 else None
                    strings.append(value)
                    strings_raw.append(raw)
                    refcounts.append(refcount)
                observed_refs = Counter()
                for entry in entries:
                    if entry.string_index is None or not 0 <= entry.string_index < len(strings):
                        raise ValueError(f'Invalid LocRes string index {entry.string_index}')
                    entry.text = strings[entry.string_index]
                    observed_refs[entry.string_index] += 1
                if version >= 2:
                    for index, count in enumerate(refcounts):
                        if count != -1 and count != observed_refs[index]:
                            raise ValueError(f'Bad string reference count at {index}: {count} != {observed_refs[index]}')
        if reader.pos != len(data):
            raise ValueError(f'Unexpected trailing LocRes data at {reader.pos}/{len(data)}')
        return cls(version, data, entries, strings, strings_raw, refcounts, pool_offset, namespace_count)

    def build(self, translations: dict[tuple[str, str], str], require_complete: bool = True) -> bytes:
        known = {entry.identity for entry in self.entries}
        unknown = set(translations) - known
        if unknown:
            raise ValueError(f'Translation contains {len(unknown)} unknown identities')
        if require_complete and known - set(translations):
            raise ValueError(f'Translation missing {len(known - set(translations))} identities')
        values = [translations.get(entry.identity, entry.text) for entry in self.entries]
        if all(value == entry.text for value, entry in zip(values, self.entries)):
            return self.original
        if self.version == 0:
            chunks, last = [], 0
            for entry, value in zip(self.entries, values):
                chunks.extend((self.original[last:entry.legacy_string_start], fstring(value)))
                last = entry.legacy_string_end
            chunks.append(self.original[last:])
            return b''.join(chunks)
        if self.pool_offset in (None, -1):
            return self.original
        body = bytearray(self.original[:self.pool_offset])
        value_indices, unique_values, refs = {}, [], Counter()
        # Rebuild the pool because identical English strings may receive distinct translations.
        for entry, value in zip(self.entries, values):
            if value not in value_indices:
                value_indices[value] = len(unique_values)
                unique_values.append(value)
            index = value_indices[value]
            refs[index] += 1
            struct.pack_into('<i', body, entry.index_offset, index)
        pool = bytearray(struct.pack('<I', len(unique_values)))
        original_raw_by_value = dict(zip(self.strings, self.strings_raw))
        for index, value in enumerate(unique_values):
            pool.extend(original_raw_by_value.get(value, fstring(value)))
            if self.version >= 2:
                pool.extend(struct.pack('<i', refs[index]))
        return bytes(body + pool)

    def catalog(self) -> list[dict]:
        return [dict(namespace=entry.namespace, key=entry.key, source_hash=entry.source_hash,
                     namespace_hash=entry.namespace_hash, key_hash=entry.key_hash,
                     text=entry.text, string_index=entry.string_index)
                for entry in self.entries]

    def metadata(self) -> dict:
        return dict(version=self.version, entries=len(self.entries), namespaces=self.namespace_count,
                    strings=len(self.strings), sha256=hashlib.sha256(self.original).hexdigest())


BRACES = re.compile(r'(?<!\{)\{([^{}]*)\}(?!\})')
# A percentage after a number is ordinary prose/table data, not a printf token.
PRINTF = re.compile(r'(?<!\d)%(?:\d+\$)?[-+#0 ]*(?:\d+|\*)?(?:\.(?:\d+|\*))?(?:hh|ll|[hljztL])?[diuoxXfFeEgGaAcspn%](?![A-Za-z])')
TAGS = re.compile(r'<[^<>\r\n]*>')
ESCAPES = re.compile(r'\\(?:[nrt\\]|[uU][0-9A-Fa-f]{4,8}|x[0-9A-Fa-f]{2})')
OPERATORS = re.compile(r'\|\s*(plural|ordinal|gender|hpp)\s*\(')
NEWLINES = re.compile(r'\r\n|\r|\n')


def format_signature(value: str) -> dict:
    return dict(braces=Counter(BRACES.findall(value)), printf=Counter(PRINTF.findall(value)),
                tags=Counter(TAGS.findall(value)), escapes=Counter(ESCAPES.findall(value)),
                operators=Counter(OPERATORS.findall(value)), newlines=Counter(NEWLINES.findall(value)))


def validate_text(source: str, translation: str) -> list[str]:
    problems = []
    if '\x00' in translation:
        problems.append('embedded NUL')
    try:
        translation.encode('utf-16-le', errors='strict')
    except UnicodeEncodeError:
        problems.append('invalid UTF-16 surrogate')
    src_sig, dst_sig = format_signature(source), format_signature(translation)
    for feature in src_sig:
        if src_sig[feature] != dst_sig[feature]:
            problems.append(f'{feature} mismatch')
    source_controls = Counter(c for c in source if unicodedata.category(c) == 'Cc')
    target_controls = Counter(c for c in translation if unicodedata.category(c) == 'Cc')
    if source_controls != target_controls:
        problems.append('control-character mismatch')
    # Whitespace at the edges can carry UI indentation or concatenation spacing.
    for side in ('leading', 'trailing'):
        pattern = r'^\s*' if side == 'leading' else r'\s*$'
        if re.search(pattern, source).group() != re.search(pattern, translation).group():
            problems.append(f'{side} whitespace mismatch')
    return problems


def validate_resource(source: LocRes, result: LocRes, translations: dict[tuple[str, str], str]) -> dict:
    if source.version != result.version:
        raise ValueError('LocRes version changed')
    if [e.signature for e in source.entries] != [e.signature for e in result.entries]:
        raise ValueError('LocRes identity/order/hash changed')
    expected = [translations.get(e.identity, e.text) for e in source.entries]
    if [e.text for e in result.entries] != expected:
        raise ValueError('Generated LocRes text differs from translation input')
    issues = []
    for entry, value in zip(source.entries, expected):
        problems = validate_text(entry.text, value)
        if problems:
            issues.append(dict(namespace=entry.namespace, key=entry.key, problems=problems))
    return dict(entries=len(source.entries), identity_hashes_preserved=True,
                valid_structure=True, text_format_issues=issues)


def main():
    cli = argparse.ArgumentParser(description=__doc__)
    sub = cli.add_subparsers(dest='command', required=True)
    for action in ('inspect', 'export'):
        p = sub.add_parser(action)
        p.add_argument('input')
        if action == 'export':
            p.add_argument('output')
    p = sub.add_parser('build')
    p.add_argument('input', help='English/Japanese LocRes structure template')
    p.add_argument('translations', help='JSON list with namespace, key, zh fields')
    p.add_argument('output')
    p.add_argument('--allow-missing', action='store_true')
    args = cli.parse_args()
    locres = LocRes.load(args.input)
    if args.command == 'inspect':
        print(json.dumps(locres.metadata(), ensure_ascii=False))
    elif args.command == 'export':
        Path(args.output).write_text(json.dumps(locres.catalog(), ensure_ascii=False, indent=2), encoding='utf-8')
        print(json.dumps(locres.metadata()))
    else:
        rows = json.loads(Path(args.translations).read_text(encoding='utf-8-sig'))
        translations = {(row['namespace'], row['key']): row['zh'] for row in rows}
        if len(translations) != len(rows):
            raise ValueError('Duplicate translation identity')
        result_bytes = locres.build(translations, require_complete=not args.allow_missing)
        report = validate_resource(locres, LocRes.parse(result_bytes), translations)
        if report['text_format_issues']:
            raise ValueError(json.dumps(report['text_format_issues'], ensure_ascii=False))
        Path(args.output).write_bytes(result_bytes)
        print(json.dumps(report, ensure_ascii=False))


if __name__ == '__main__':
    main()
