"""Read-only, per-resource compatibility checks for the bundled text snapshot.

This module never loads/starts a game executable, decodes Chinese text, writes
game files, stores a key, or requires an externally installed crypto package.
It discovers transient AES candidates from x64 PE code and validates them
against the CURRENT PAK index's own SHA1, using Windows CNG. Offsets, archive
size, compression layout and index hashes are observations, not build locks.

Format references: local CUE4Parse FPakInfo/FPakEntry/PakFileReader sources.
Supported archive layout is UE PAK v11 with a full directory index. An unknown
layout or unverified key fails closed rather than treating an update as safe.
"""
from __future__ import annotations

import bisect
from collections import defaultdict
import ctypes
from ctypes import wintypes
import gzip
import hashlib
import json
import mmap
import os
from pathlib import Path
import re
import struct
import zlib

DEFAULT_BASELINE = Path(__file__).resolve().parent / 'data' / 'resource-compatibility-baseline.json'
MAX_INDEX_BYTES = 128 * 1024 * 1024
MAX_RESOURCE_BYTES = 32 * 1024 * 1024
MAX_CANDIDATES = 50000


class ResourceCompatibilityError(ValueError):
    pass


def _fail(message):
    raise ResourceCompatibilityError(message)


def _read_exact(stream, count):
    if count < 0 or count > MAX_INDEX_BYTES:
        _fail('资源元数据长度超出安全读取范围。')
    data = stream.read(count)
    if len(data) != count:
        _fail('资源包文件不完整。')
    return data


class _Reader:
    def __init__(self, data):
        self.data, self.pos = data, 0

    def take(self, count):
        if count < 0 or self.pos + count > len(self.data):
            _fail('PAK 索引结构越界，无法验证当前版本。')
        value = self.data[self.pos:self.pos + count]
        self.pos += count
        return value

    def number(self, code):
        return struct.unpack(code, self.take(struct.calcsize(code)))[0]

    def string(self):
        length = self.number('<i')
        if not length:
            return ''
        if length == -2147483648 or abs(length) > 1024 * 1024:
            _fail('PAK 索引中的字符串长度无效。')
        raw = self.take(length if length > 0 else -length * 2)
        terminator = b'\0' if length > 0 else b'\0\0'
        if not raw.endswith(terminator):
            _fail('PAK 索引中的路径缺少终止符。')
        try:
            return raw[:-len(terminator)].decode('utf-8' if length > 0 else 'utf-16le')
        except UnicodeError as error:
            raise ResourceCompatibilityError('PAK 路径编码与受支持格式不一致。') from error


class _AesEcb:
    """AES through bcrypt.dll; keys exist only in process memory."""
    def __init__(self):
        if os.name != 'nt':
            _fail('资源索引校验需要 Windows CNG。')
        self.dll = ctypes.WinDLL('bcrypt.dll')
        pointer, ulong = ctypes.c_void_p, wintypes.ULONG
        declarations = {
            'BCryptOpenAlgorithmProvider': [ctypes.POINTER(pointer), wintypes.LPCWSTR, wintypes.LPCWSTR, ulong],
            'BCryptSetProperty': [pointer, wintypes.LPCWSTR, pointer, ulong, ulong],
            'BCryptGetProperty': [pointer, wintypes.LPCWSTR, pointer, ulong, ctypes.POINTER(ulong), ulong],
            'BCryptGenerateSymmetricKey': [pointer, ctypes.POINTER(pointer), pointer, ulong, pointer, ulong, ulong],
            'BCryptDecrypt': [pointer, pointer, ulong, pointer, pointer, ulong, pointer, ulong, ctypes.POINTER(ulong), ulong],
            'BCryptDestroyKey': [pointer],
            'BCryptCloseAlgorithmProvider': [pointer, ulong],
        }
        for name, args in declarations.items():
            function = getattr(self.dll, name)
            function.argtypes, function.restype = args, wintypes.LONG
        self.provider = pointer()
        self._status(self.dll.BCryptOpenAlgorithmProvider(ctypes.byref(self.provider), 'AES', None, 0))
        mode = ctypes.create_string_buffer('ChainingModeECB\0'.encode('utf-16le'))
        self._status(self.dll.BCryptSetProperty(self.provider, 'ChainingMode', mode, len(mode.raw) - 1, 0))
        size, returned = ulong(), ulong()
        self._status(self.dll.BCryptGetProperty(self.provider, 'ObjectLength', ctypes.byref(size),
                                                ctypes.sizeof(size), ctypes.byref(returned), 0))
        self.object_length = size.value

    @staticmethod
    def _status(value):
        if value < 0:
            _fail(f'Windows AES 校验失败：0x{value & 0xffffffff:08X}')

    def decrypt(self, data, key):
        if len(key) != 32 or len(data) % 16:
            _fail('AES 索引长度或候选结构无效。')
        handle = ctypes.c_void_p()
        key_object = ctypes.create_string_buffer(self.object_length)
        secret = ctypes.create_string_buffer(key, len(key))
        input_buffer = ctypes.create_string_buffer(data, len(data))
        output = ctypes.create_string_buffer(len(data))
        result_size = wintypes.ULONG()
        try:
            self._status(self.dll.BCryptGenerateSymmetricKey(self.provider, ctypes.byref(handle),
                key_object, len(key_object), secret, len(key), 0))
            self._status(self.dll.BCryptDecrypt(handle, input_buffer, len(data), None, None, 0,
                                                output, len(data), ctypes.byref(result_size), 0))
            if result_size.value != len(data):
                _fail('Windows AES 返回的索引长度不一致。')
            return output.raw[:result_size.value]
        finally:
            if handle.value:
                self.dll.BCryptDestroyKey(handle)
            ctypes.memset(secret, 0, len(secret))
            ctypes.memset(key_object, 0, len(key_object))

    def close(self):
        if self.provider.value:
            self.dll.BCryptCloseAlgorithmProvider(self.provider, 0)
            self.provider = ctypes.c_void_p()


def _pe_sections(data):
    if len(data) < 64 or data[:2] != b'MZ':
        _fail('索引密钥来源不是有效的 Windows EXE。')
    pe = struct.unpack_from('<I', data, 0x3c)[0]
    if pe + 24 > len(data) or data[pe:pe + 4] != b'PE\0\0':
        _fail('EXE 的 PE 结构无效。')
    machine, count = struct.unpack_from('<HH', data, pe + 4)
    optional_size = struct.unpack_from('<H', data, pe + 20)[0]
    table = pe + 24 + optional_size
    if machine != 0x8664 or count > 96 or table + count * 40 > len(data):
        _fail('当前只支持具有可解析初始化序列的 x64 游戏 EXE。')
    sections = []
    for index in range(count):
        offset = table + index * 40
        virtual_size, rva, size, start = struct.unpack_from('<IIII', data, offset + 8)
        flags = struct.unpack_from('<I', data, offset + 36)[0]
        if start + size > len(data):
            _fail('EXE 节区超出文件长度。')
        if flags & (0x20 | 0x20000000):
            sections.append(dict(start=start, end=start + size, rva=rva))
    if not sections:
        _fail('EXE 没有可检查的代码节。')
    return sections


def _store(data, position, section):
    """Decode MOV r/m32,imm32 control fields; never log immediates."""
    end = section['end']
    if position + 6 > end or data[position] != 0xc7:
        return None
    modrm = data[position + 1]
    mode, register, rm = modrm >> 6, (modrm >> 3) & 7, modrm & 7
    if mode == 3 or register:
        return None
    rex = data[position - 1] if position > section['start'] and 0x40 <= data[position - 1] <= 0x4f else 0
    if rex & 8:
        return None
    cursor = position + 2
    base, index, scale = rm + (8 if rex & 1 else 0), None, 1
    rip_relative, absolute = mode == 0 and rm == 5, False
    if rm == 4:
        if cursor >= end:
            return None
        sib = data[cursor]
        cursor += 1
        scale, raw_index = 1 << (sib >> 6), (sib >> 3) & 7
        index = None if raw_index == 4 and not rex & 2 else raw_index + (8 if rex & 2 else 0)
        raw_base = sib & 7
        base, absolute = raw_base + (8 if rex & 1 else 0), mode == 0 and raw_base == 5
    displacement = 0
    if mode == 1:
        if cursor + 1 > end:
            return None
        displacement = struct.unpack_from('<b', data, cursor)[0]
        cursor += 1
    elif mode == 2 or rip_relative or absolute:
        if cursor + 4 > end:
            return None
        displacement = struct.unpack_from('<i', data, cursor)[0]
        cursor += 4
    if cursor + 4 > end:
        return None
    immediate, finish = bytes(data[cursor:cursor + 4]), cursor + 4
    if rip_relative:
        destination, family = section['rva'] + finish - section['start'] + displacement, ('rip',)
    elif absolute:
        destination, family = displacement, ('absolute', index, scale)
    else:
        destination, family = displacement, ('register', base, index, scale)
    return dict(offset=position, end=finish, destination=destination, family=family, value=immediate)


def _keys_from_stores(stores):
    offsets = [store['offset'] for store in stores]
    seen = set()
    for first_index, first in enumerate(stores):
        limit = min(first_index + 32, bisect.bisect_right(offsets, first['offset'] + 160))
        destinations, previous_end = {}, first['end']
        for store in stores[first_index:limit]:
            if store['offset'] > previous_end + 16:
                break
            previous_end = max(previous_end, store['end'])
            if store['family'] != first['family']:
                continue
            destinations[store['destination']] = store['value']
            ordered = sorted(destinations)
            for start in range(max(0, len(ordered) - 7)):
                addresses = ordered[start:start + 8]
                if addresses != list(range(addresses[0], addresses[0] + 32, 4)):
                    continue
                key = b''.join(destinations[address] for address in addresses)
                if len(set(key)) >= 8 and key not in seen:
                    seen.add(key)
                    yield key
            if len(destinations) > 12:
                break


def _key_candidates(exe_source, hint_offset=None):
    with Path(exe_source).open('rb') as stream, mmap.mmap(stream.fileno(), 0, access=mmap.ACCESS_READ) as data:
        sections = _pe_sections(data)
        seen = set()
        # A hint only accelerates the known build; no match falls back to a full
        # structural code scan, so relocation/recompilation is not a build lock.
        if isinstance(hint_offset, int):
            for section in sections:
                if section['start'] <= hint_offset < section['end']:
                    # Match positions above are relative to the bounded slice.
                    stores = [_store(data, hint_offset + match.start(), section) for match in
                              re.finditer(b'\xc7', data[hint_offset:min(hint_offset + 192, section['end'])])]
                    for key in _keys_from_stores([store for store in stores if store]):
                        seen.add(key)
                        yield key
        token = re.compile(b'\xc7')
        for section in sections:
            stores = []
            for match in token.finditer(data, section['start'], section['end']):
                store = _store(data, match.start(), section)
                if store:
                    stores.append(store)
            for key in _keys_from_stores(stores):
                if key not in seen:
                    if len(seen) >= MAX_CANDIDATES:
                        _fail('EXE 中的初始化候选过多，无法安全确认资源索引。')
                    seen.add(key)
                    yield key


def _footer(path):
    with Path(path).open('rb') as stream:
        stream.seek(0, 2)
        size = stream.tell()
        if size < 221:
            _fail(f'{Path(path).name}: 不是支持的 PAK v11 格式。')
        stream.seek(-221, 2)
        footer = _read_exact(stream, 221)
        magic, version = struct.unpack_from('<II', footer, 17)
        if magic != 0x5a6f12e1 or version != 11 or footer[16] not in (0, 1):
            _fail(f'{Path(path).name}: PAK 格式改变，需要适配新格式。')
        offset, length = struct.unpack_from('<QQ', footer, 25)
        if length > MAX_INDEX_BYTES or offset + length > size - 221:
            _fail(f'{Path(path).name}: PAK 索引范围无效。')
        methods = ['None'] + [footer[start:start + 32].split(b'\0', 1)[0].decode('ascii', errors='strict')
                              for start in range(61, 221, 32)]
        return dict(size=size, index_offset=offset, index_size=length,
                    encrypted=bool(footer[16]), index_sha1=footer[41:61], methods=methods)


def _valid_mount_probe(plain):
    try:
        reader = _Reader(plain)
        mount = reader.string()
        return (mount.startswith('../../../') or mount == '/') and len(mount) < 256
    except (ResourceCompatibilityError, struct.error):
        return False


def _index_parts(plain):
    reader = _Reader(plain)
    mount = reader.string()
    count = reader.number('<I')
    reader.take(8)  # PathHashSeed; not part of text compatibility identity.
    path_hash = reader.number('<I')
    if path_hash not in (0, 1):
        _fail('PAK PathHashIndex 标记不受支持。')
    if path_hash:
        reader.take(36)
    has_directory = reader.number('<I')
    if has_directory == 0 and count == 0:
        return dict(mount=mount, count=0, directory_offset=None)
    if has_directory != 1:
        _fail('PAK 没有完整目录索引，无法证明本地化资源未变化。')
    directory_offset, directory_size = reader.number('<q'), reader.number('<q')
    directory_sha1 = reader.take(20)
    encoded_size = reader.number('<I')
    encoded = reader.take(encoded_size)
    if directory_size < 0 or directory_size > MAX_INDEX_BYTES or directory_offset < 0:
        _fail('PAK 目录索引范围无效。')
    # Non-encoded entries exist in v11, but the localization snapshot uses encoded
    # entries. They are parsed only if a relevant updated entry needs one below.
    return dict(mount=mount, count=count, encoded=encoded, reader=reader,
                directory_offset=directory_offset, directory_size=directory_size,
                directory_sha1=directory_sha1)


def _normalize_path(path):
    path = path.replace('\\', '/')
    while path.startswith('../'):
        path = path[3:]
    return path.lstrip('/')


def _directory_entries(plain, mount, wanted):
    reader = _Reader(plain)
    count = reader.number('<I')
    if count > 1000000:
        _fail('PAK 目录数量异常。')
    found = {}
    for _ in range(count):
        directory = reader.string()
        files = reader.number('<I')
        if files > 2000000:
            _fail('PAK 文件数量异常。')
        for _ in range(files):
            name, entry_offset = reader.string(), reader.number('<i')
            path = _normalize_path(mount + directory.lstrip('/') + name)
            if path in wanted:
                if path in found:
                    _fail(f'PAK 目录中有重复的本地化路径：{path}')
                found[path] = entry_offset
    # UE AES padding need not contain zeros; the current directory SHA1 already
    # authenticates it. At most 15 alignment bytes remain after native arrays.
    if len(reader.data) - reader.pos > 15:
        _fail('PAK 目录尾部出现不识别的数据。')
    return found


def _encoded_entry(data, position):
    if position < 0:
        _fail('相关本地化条目改用了非编码目录格式，需要适配。')
    reader = _Reader(data)
    reader.pos = position
    bits = reader.number('<I')
    block_size = reader.number('<I') if bits & 63 == 63 else (bits & 63) << 11
    offset = reader.number('<I' if bits & (1 << 31) else '<Q')
    uncompressed = reader.number('<I' if bits & (1 << 30) else '<Q')
    method = (bits >> 23) & 63
    compressed = reader.number('<I' if bits & (1 << 29) else '<Q') if method else uncompressed
    encrypted = bool(bits & (1 << 22))
    blocks = (bits >> 6) & 65535
    header_size = 53 + (4 + blocks * 16 if method else 0)
    lengths = ([compressed] if blocks == 1 and not encrypted else
               [reader.number('<I') for _ in range(blocks)]) if blocks else []
    return dict(offset=offset, uncompressed_size=uncompressed, compressed_size=compressed,
                method=method, encrypted=encrypted, blocks=blocks, block_size=block_size,
                header_size=header_size, block_lengths=lengths)


def _entry_payload(stream, metadata, info, key, aes):
    offset, size, compressed = metadata['offset'], metadata['uncompressed_size'], metadata['compressed_size']
    if size > MAX_RESOURCE_BYTES or compressed > MAX_RESOURCE_BYTES or offset + metadata['header_size'] > info['index_offset']:
        _fail('相关本地化资源范围超出可验证界限。')
    stream.seek(offset)
    header = _read_exact(stream, metadata['header_size'])
    stored_size, stored_plain_size, stored_method = struct.unpack_from('<QQI', header, 8)
    if (stored_size, stored_plain_size, stored_method) != (compressed, size, metadata['method']):
        _fail('PAK 目录与资源条目头不一致。')
    header_sha1 = header[28:48].hex()
    flag_position = 48 + (4 + metadata['blocks'] * 16 if metadata['method'] else 0)
    flags = header[flag_position]
    if bool(flags & 1) != metadata['encrypted'] or flags & 2:
        _fail('相关资源已删除或加密标记不一致。')
    position = offset + metadata['header_size']
    if not metadata['method']:
        length = (compressed + 15) & ~15 if metadata['encrypted'] else compressed
        if position + length > info['index_offset']:
            _fail('本地化资源数据超出资源包有效范围。')
        stream.seek(position)
        payload = _read_exact(stream, length)
        if metadata['encrypted']:
            if key is None:
                _fail('相关条目加密，但当前 EXE 的密钥未验证。')
            payload = aes.decrypt(payload, key)
        payload = payload[:size]
    else:
        method = info['methods'][metadata['method']] if metadata['method'] < len(info['methods']) else ''
        if method not in ('Zlib', 'Gzip'):
            _fail(f'相关资源改用 {method or "未知"} 压缩；需适配后才能证明兼容。')
        chunks = []
        for length in metadata['block_lengths']:
            read_length = (length + 15) & ~15 if metadata['encrypted'] else length
            if position + read_length > info['index_offset']:
                _fail('本地化压缩块超出资源包有效范围。')
            stream.seek(position)
            raw = _read_exact(stream, read_length)
            if metadata['encrypted']:
                if key is None:
                    _fail('相关压缩块加密，但密钥未验证。')
                raw = aes.decrypt(raw, key)
            raw = raw[:length]
            chunk = zlib.decompress(raw) if method == 'Zlib' else gzip.decompress(raw)
            chunks.append(chunk)
            position += read_length
        payload = b''.join(chunks)
        if len(payload) != size:
            _fail('本地化资源解压后的大小与目录不一致。')
    # The native GameData container remains opaque. No strings are decoded.
    return payload, header_sha1


def _scan_archive(path, wanted, exe_source, hint_offset, keys, aes):
    info = _footer(path)
    with Path(path).open('rb') as stream:
        stream.seek(info['index_offset'])
        index = _read_exact(stream, info['index_size'])
        key = None
        if info['encrypted']:
            if len(index) % 16:
                _fail(f'{Path(path).name}: 加密索引未按 AES 块对齐。')
            plain = None
            def candidates():
                cached = set(keys)
                yield from tuple(keys)
                # A patch container may use another key initialized by the same
                # EXE. Only scan after cached keys fail; do not assume all PAKs
                # share the first validated key.
                for candidate in _key_candidates(exe_source, hint_offset):
                    if candidate not in cached:
                        yield candidate
            for candidate in candidates():
                probe = aes.decrypt(index[:min(256, len(index))], candidate)
                if not _valid_mount_probe(probe):
                    continue
                decrypted = aes.decrypt(index, candidate)
                if hashlib.sha1(decrypted).digest() == info['index_sha1']:
                    key, plain = candidate, decrypted
                    if candidate not in keys:
                        keys.append(candidate)
                    break
            if plain is None:
                _fail(f'{Path(path).name}: 无法从当前原 EXE 定位并验证 PAK 索引初始化；没有跳过资源校验。')
        else:
            plain = index
            if hashlib.sha1(plain).digest() != info['index_sha1']:
                _fail(f'{Path(path).name}: 当前索引自身的 SHA1 校验失败。')
        parts = _index_parts(plain)
        if parts['directory_offset'] is None:
            return [], dict(archive=Path(path).name, format='PAK v11',
                            relevant_entries=0, own_index_verified=True)
        if parts['directory_offset'] + parts['directory_size'] > info['size'] - 221:
            _fail('PAK 目录位于不允许的范围。')
        stream.seek(parts['directory_offset'])
        directory = _read_exact(stream, parts['directory_size'])
        if info['encrypted']:
            directory = aes.decrypt(directory, key)
        if hashlib.sha1(directory).digest() != parts['directory_sha1']:
            _fail(f'{Path(path).name}: 当前完整目录自身的 SHA1 校验失败。')
        found = _directory_entries(directory, parts['mount'], wanted)
        observations = []
        for resource_path, encoded_offset in found.items():
            if encoded_offset == -2147483648:
                _fail(f'资源被目录删除标记覆盖：{resource_path}')
            metadata = _encoded_entry(parts['encoded'], encoded_offset)
            payload, entry_sha1 = _entry_payload(stream, metadata, info, key, aes)
            observations.append(dict(path=resource_path, archive=Path(path).name,
                size=len(payload), payload_sha256=hashlib.sha256(payload).hexdigest(),
                entry_sha1=entry_sha1, compression=info['methods'][metadata['method']],
                encrypted=metadata['encrypted']))
        return observations, dict(archive=Path(path).name, format='PAK v11',
                                   relevant_entries=len(observations), own_index_verified=True)


def check_resources(game_dir, exe_source, expected_baseline=None, *, owned_archives=None):
    """Return readable compatibility status; never modify game or persist AES.

    expected_baseline is a baseline dict or JSON path. exe_source must point to
    the current original EXE (or its deployment-verified original backup).
    Archives are resolved dynamically; every occurrence of a relevant path
    must match, preventing an updated patch container from shadowing old text.
    A deployment-owned localization PAK may be excluded only by its exact
    managed path and a freshly verified SHA256 supplied by the deployment layer.
    """
    baseline = expected_baseline or DEFAULT_BASELINE
    try:
        if not isinstance(baseline, dict):
            baseline = json.loads(Path(baseline).read_text(encoding='utf-8-sig'))
        if baseline.get('schema') != 'ac8-resource-compatibility-v1':
            _fail('资源兼容基准格式无效。')
        expected = {item['path']: item for item in baseline['resources']}
        if len(expected) != 8 or len(baseline['resources']) != 8:
            _fail('资源兼容基准必须包含八个完整本地化资源。')
        game_dir, exe_source = Path(game_dir).resolve(), Path(exe_source).resolve()
        if not exe_source.is_file():
            _fail('找不到用于只读资源检查的原 EXE。')
        paks_dir = game_dir / 'Game/Content/Paks'
        archives = sorted(paks_dir.rglob('*.pak'))
        ignored = []
        for relative, expected_hash in (owned_archives or {}).items():
            if (relative != 'Game/Content/Paks/pakchunk0-Windows_998_P.pak'
                    or not isinstance(expected_hash, str)
                    or not re.fullmatch(r'[0-9a-f]{64}', expected_hash)):
                _fail('自有资源补丁的检查范围无效。')
            own = game_dir / relative
            if own not in archives or not own.is_file() or own.is_symlink():
                _fail('自有资源补丁已丢失或不再是普通文件。')
            with own.open('rb') as stream:
                if hashlib.file_digest(stream, 'sha256').hexdigest() != expected_hash:
                    _fail('自有资源补丁在兼容检查期间发生变化。')
            archives.remove(own)
            ignored.append(relative)
        if not archives:
            _fail('找不到游戏 PAK 资源包。')
        aes, keys, observed, archives_checked = _AesEcb(), [], defaultdict(list), []
        try:
            # Main chunk first usually discovers the key; naming does not form
            # part of the compatibility baseline and no archive size is locked.
            archives.sort(key=lambda path: (path.name != 'pakchunk0-Windows.pak', path.name))
            for path in archives:
                items, archive_info = _scan_archive(path, set(expected), exe_source,
                    baseline.get('preferred_key_scan_file_offset'), keys, aes)
                archives_checked.append(archive_info)
                for item in items:
                    observed[item['path']].append(item)
            # Existing loose source-table overrides must also be compatible.
            # The tool's own CP_Z/ON_Z deployment files are intentionally outside
            # this set; they do not redefine the common key/index tables.
            for resource_path in expected:
                filename = resource_path.rsplit('/', 1)[1]
                for folder in ('Game', 'Live'):
                    loose = game_dir / folder / 'Content/Localization/GameData' / filename
                    if loose.is_file():
                        if loose.stat().st_size > MAX_RESOURCE_BYTES:
                            _fail('松散本地化源资源超出可验证界限。')
                        data = loose.read_bytes()
                        observed[resource_path].append(dict(path=resource_path, archive='loose:' + folder,
                            size=len(data), payload_sha256=hashlib.sha256(data).hexdigest(),
                            compression='None', encrypted=False))
        finally:
            aes.close()
            keys.clear()
        mismatches, checked = [], []
        for path, reference in expected.items():
            copies = observed.get(path, [])
            if not copies:
                mismatches.append(dict(path=path, reason='找不到相关文本资源。'))
                continue
            valid = all(item['size'] == reference['size'] and
                        item['payload_sha256'] == reference['payload_sha256'] for item in copies)
            checked.append(dict(path=path, compatible=valid, occurrences=copies))
            if not valid:
                mismatches.append(dict(path=path,
                    reason='相关文本、槽 ID 映射或基准官中数据已改变，不能覆盖旧快照。'))
        compatible = not mismatches
        return dict(ok=compatible, compatible=compatible, state='compatible' if compatible else 'incompatible',
            message='CP/ON 槽表及日英官中资源与翻译快照一致。' if compatible else
                    '相关本地化资源发生变化，需要更新原文字库后导入，已停止覆盖。',
            errors=mismatches, resources=checked, archives_checked=archives_checked,
            owned_resource_archives=ignored,
            compatibility_scope='CP/ON Cmn, Japanese, English and official Chinese opaque resource bytes',
            snapshot_fingerprint=baseline.get('snapshot_fingerprint'),
            archive_size_locked=False, whole_archive_hash_locked=False, fixed_build_locked=False,
            key_embedded=False, key_saved=False, official_chinese_text_decoded=False,
            game_files_changed=False, game_started=False)
    except (ResourceCompatibilityError, OSError, ValueError, struct.error, zlib.error, KeyError) as error:
        return dict(ok=False, compatible=False, state='unverified', message=str(error),
                    errors=[dict(reason=str(error))], key_embedded=False, key_saved=False,
                    official_chinese_text_decoded=False, game_files_changed=False, game_started=False)


if __name__ == '__main__':
    import argparse
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument('game_dir')
    cli.add_argument('exe_source')
    cli.add_argument('--baseline')
    cli.add_argument('--report')
    args = cli.parse_args()
    result = check_resources(args.game_dir, args.exe_source, args.baseline)
    if args.report:
        Path(args.report).write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    if hasattr(__import__('sys').stdout, 'reconfigure'):
        __import__('sys').stdout.reconfigure(encoding='utf-8')
    print(json.dumps(result, ensure_ascii=False))
    raise SystemExit(0 if result['ok'] else 2)
