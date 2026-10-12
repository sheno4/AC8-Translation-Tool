"""Read-only discovery of the AC8 locale suffix hook in an original PE image.

Signatures describe audited instruction layouts, not a Steam build number or a
whole-file allowlist. They deliberately refuse changed compiler output. The
returned configuration is bound to the exact image just inspected; deployment
must still recheck that hash and the original bytes before changing any file.
No game code is loaded or executed, and this module never writes a file.
"""
from __future__ import annotations

from bisect import bisect_right
import hashlib
import json
import mmap
from pathlib import Path
import re
import struct


class HookCompatibilityError(ValueError):
    """An image cannot be confirmed compatible with the audited hook."""


def _refuse(message):
    raise HookCompatibilityError("当前游戏 EXE 无法安全定位汉化补丁：" + message)


class _PE:
    def __init__(self, data):
        self.data = data
        if len(data) < 0x100 or data[:2] != b"MZ":
            _refuse("不是有效的 Windows PE 文件。")
        pe = self.u32(0x3C)
        if pe + 24 > len(data) or data[pe:pe + 4] != b"PE\0\0":
            _refuse("PE 文件头无效。")
        if self.u16(pe + 4) != 0x8664:
            _refuse("只支持 x64 游戏 EXE。")
        count, optional_size = self.u16(pe + 6), self.u16(pe + 20)
        optional = pe + 24
        if not 1 <= count <= 96 or optional_size < 240 or optional + optional_size + count * 40 > len(data):
            _refuse("PE 区段表或可选文件头不完整。")
        if self.u16(optional) != 0x20B or self.u32(optional + 108) < 16:
            _refuse("不是完整的 PE32+ 文件头。")
        self.base = self.u64(optional + 24)
        self.image_size = self.u32(optional + 56)
        self.directories = [struct.unpack_from("<II", data, optional + 112 + i * 8) for i in range(16)]
        self.sections = []
        for i in range(count):
            at = optional + optional_size + i * 40
            virtual_size, rva, size, offset = struct.unpack_from("<IIII", data, at + 8)
            flags = self.u32(at + 36)
            if size and (offset < optional + optional_size + count * 40 or offset + size > len(data)):
                _refuse("PE 区段范围越界。")
            if rva + max(size, virtual_size) > self.image_size:
                _refuse("PE 区段超出映像范围。")
            self.sections.append(dict(rva=rva, size=size, offset=offset, virtual_size=virtual_size, flags=flags))
        for i, first in enumerate(self.sections):
            for second in self.sections[i + 1:]:
                if first["size"] and second["size"] and max(first["offset"], second["offset"]) < min(first["offset"] + first["size"], second["offset"] + second["size"]):
                    _refuse("PE 文件区段重叠。")
                if max(first["rva"], second["rva"]) < min(first["rva"] + max(first["size"], first["virtual_size"]), second["rva"] + max(second["size"], second["virtual_size"])):
                    _refuse("PE 虚拟区段重叠。")
        exception_rva, exception_size = self.directories[3]
        if not exception_rva or not exception_size or exception_size % 12:
            _refuse("缺少可验证的 x64 函数边界表。")
        at = self.offset(exception_rva, exception_size)
        self.functions = [struct.unpack_from("<III", data, at + i) for i in range(0, exception_size, 12)]
        self.starts = [row[0] for row in self.functions]
        if self.starts != sorted(set(self.starts)):
            _refuse("x64 函数边界表未按地址排列。")
        for start, end, unwind in self.functions:
            if start >= end or not self.section(start, end - start).get("flags", 0) & 0x20000000:
                _refuse("函数边界不在可执行区段中。")
            if not unwind or unwind & 3:
                _refuse("函数展开信息地址无效。")
            self.offset(unwind, 4)

    def u16(self, at):
        return struct.unpack_from("<H", self.data, at)[0]

    def u32(self, at):
        return struct.unpack_from("<I", self.data, at)[0]

    def u64(self, at):
        return struct.unpack_from("<Q", self.data, at)[0]

    def section(self, rva, length=1):
        for section in self.sections:
            if section["rva"] <= rva and rva + length <= section["rva"] + max(section["size"], section["virtual_size"]):
                return section
        _refuse("代码或数据地址没有有效区段映射。")

    def offset(self, rva, length=1):
        section = self.section(rva, length)
        relative = rva - section["rva"]
        if relative + length > section["size"]:
            _refuse("所需数据没有完整的文件内容。")
        return section["offset"] + relative

    def rva(self, offset):
        for section in self.sections:
            if section["offset"] <= offset < section["offset"] + section["size"]:
                return section["rva"] + offset - section["offset"]
        _refuse("文件位置不在 PE 区段内。")

    def function_at(self, rva):
        index = bisect_right(self.starts, rva) - 1
        if index >= 0 and self.functions[index][0] <= rva < self.functions[index][1]:
            return self.functions[index]
        return None


def _pattern(signature):
    raw, mask = bytes.fromhex(signature["bytes_hex"]), bytes.fromhex(signature["mask_hex"])
    if len(raw) != len(mask) or len(raw) < 128 or any(value not in (0, 255) for value in mask):
        _refuse("工具内置代码签名无效。")
    return re.compile(b"".join(re.escape(bytes([value])) if keep else b"." for value, keep in zip(raw, mask)), re.DOTALL)


def _unique_code_match(pe, signature, label):
    pattern = _pattern(signature)
    matches = []
    for section in pe.sections:
        if section["flags"] & 0x20000000:
            for match in pattern.finditer(pe.data, section["offset"], section["offset"] + section["size"]):
                matches.append(match.start())
                if len(matches) > 1:
                    _refuse(label + "出现多个候选，已停止。")
    if not matches:
        _refuse(label + "签名已变化，需要适配当前游戏更新。")
    return matches[0]


def _validate_references(pe, start_offset, signature):
    start_rva = pe.rva(start_offset)
    for reference in signature["references"]:
        target = start_rva + reference["end"] + struct.unpack_from("<i", pe.data, start_offset + reference["offset"])[0]
        if reference.get("image_base"):
            if target != 0:
                _refuse("语言匹配函数的映像基址计算已变化。")
            continue
        if reference["kind"] == "branch":
            if not pe.section(target)["flags"] & 0x20000000:
                _refuse("代码签名的调用或跳转目标不是可执行地址。")
            pe.offset(target)
        elif "expected_hex" in reference:
            expected = bytes.fromhex(reference["expected_hex"])
            at = pe.offset(target, len(expected))
            if pe.data[at:at + len(expected)] != expected:
                _refuse("语言资源目录、扩展名或比较常量已变化。")
        else:
            pe.section(target)


def _utf16_culture(pe, pointer):
    at = pe.offset(pointer - pe.base, 2)
    for length in range(0, 32, 2):
        if pe.data[at + length:at + length + 2] == b"\0\0":
            return pe.data[at:at + length].decode("utf-16le")
    _refuse("语言表包含无法确认的语言代码。")


def _validate_function(pe, at, signature, cultures):
    rva = pe.rva(at)
    expected_length = len(bytes.fromhex(signature["bytes_hex"]))
    function = pe.function_at(rva)
    if function is None or function[:2] != (rva, rva + expected_length):
        _refuse("语言后缀函数的 x64 边界与已审计实现不一致。")
    _validate_references(pe, at, signature)
    table_rva = pe.u32(at + signature["culture_table_rva_offset"])
    table = pe.offset(table_rva, len(cultures) * 8)
    if [_utf16_culture(pe, pe.u64(table + i * 8)) for i in range(len(cultures))] != cultures:
        _refuse("语言表顺序已变化，不能保证中文仍使用索引 12。")
    case_tables = [pe.u32(at + position) for position in signature["case_table_rva_offsets"]]
    if len(set(case_tables)) != 1:
        _refuse("语言比较表地址不一致。")
    pe.offset(case_tables[0], 128)
    hook = at + signature["hook_offset"]
    if pe.data[hook:hook + 5] != bytes.fromhex("664183c641"):
        _refuse("待替换的后缀指令不是预期的 add r14w, 0x41。")
    return hook


def _relocation_entries(pe):
    rva, size = pe.directories[5]
    if not rva and not size:
        return
    if not rva or not size:
        _refuse("基址重定位目录不完整。")
    at, end = pe.offset(rva, size), pe.offset(rva, size) + size
    while at < end:
        if end - at < 8:
            _refuse("基址重定位目录截断。")
        page, length = struct.unpack_from("<II", pe.data, at)
        if length < 8 or length % 2 or at + length > end:
            _refuse("基址重定位块无效。")
        for position in range(at + 8, at + length, 2):
            value = pe.u16(position)
            if value >> 12:
                yield page + (value & 0xFFF)
        at += length


def _guard_entries(pe):
    """Read CFG/long-jump/EH continuation entries when their fields exist."""
    rva, size = pe.directories[10]
    if not rva and not size:
        return
    if not rva or size < 4:
        _refuse("LoadConfig 目录不完整。")
    at = pe.offset(rva, size)
    declared_size = pe.u32(at)
    if declared_size > size:
        _refuse("LoadConfig 大小超出目录。")
    flags = pe.u32(at + 144) if declared_size >= 148 else 0
    for table_field, count_field, stride in ((128, 136, 4 + (flags >> 28)), (176, 184, 4), (264, 272, 4)):
        if declared_size < count_field + 8:
            continue
        table, count = pe.u64(at + table_field), pe.u64(at + count_field)
        if not table and not count:
            continue
        if not table or not count or count > 4_000_000:
            _refuse("控制流入口表无效。")
        cursor = pe.offset(table - pe.base, count * stride)
        for i in range(count):
            yield pe.u32(cursor + i * stride)


def _validate_unused_ranges(pe, cave, hook, verified_layouts):
    cave_rva = pe.rva(cave)
    ranges = ((cave_rva, cave_rva + 20), (pe.rva(hook) + 1, pe.rva(hook) + 5))

    def forbidden(target):
        return any(start <= target < end for start, end in ranges)

    for target in _relocation_entries(pe):
        if any(target < end and target + 8 > start for start, end in ranges):
            _refuse("补丁空隙或指令内部被基址重定位引用。")
    for target in _guard_entries(pe):
        if forbidden(target):
            _refuse("补丁空隙或指令内部被控制流入口表引用。")
    pointers = [struct.pack("<Q", pe.base + target) for start, end in ranges for target in range(start, end)]
    if re.search(b"|".join(re.escape(pointer) for pointer in pointers), pe.data):
        _refuse("补丁空隙或指令内部存在绝对地址引用。")
    patterns = ((re.compile(rb"[\xe8\xe9]....", re.DOTALL), 5, 1),
                (re.compile(rb"\x0f[\x80-\x8f]....", re.DOTALL), 6, 2))
    for section in pe.sections:
        if not section["flags"] & 0x20000000:
            continue
        start, end = section["offset"], section["offset"] + section["size"]
        for pattern, width, displacement in patterns:
            # Lookahead includes overlapping byte candidates. Embedded data can
            # produce a conservative false positive, which must refuse safely.
            overlapping = re.compile(b"(?=(" + pattern.pattern + b"))", re.DOTALL)
            for match in overlapping.finditer(pe.data, start, end):
                target = section["rva"] + match.start() - start + width + struct.unpack_from("<i", pe.data, match.start() + displacement)[0]
                if forbidden(target):
                    _refuse("补丁空隙或指令内部存在相对调用/跳转引用。")
        for first, last in ranges:
            lower = max(start, section["offset"] + first - section["rva"] - 130)
            upper = min(end - 1, section["offset"] + last - section["rva"] + 128)
            for at in range(lower, upper):
                # The exact audited instruction layout proves which bytes are
                # operands, not branches. In particular the 0x77 displacement
                # of "lea rdx,[rbp+0x77]" must not be mistaken for a JA opcode.
                if any(begin <= at < limit and at - begin not in boundaries
                       for begin, limit, boundaries in verified_layouts):
                    continue
                opcode = pe.data[at]
                if opcode == 0xEB or 0x70 <= opcode <= 0x7F or 0xE0 <= opcode <= 0xE3:
                    target = section["rva"] + at - start + 2 + struct.unpack_from("<b", pe.data, at + 1)[0]
                    if forbidden(target):
                        _refuse("补丁空隙或指令内部存在短跳转引用。")


def _validate_cave(pe, at, signature, hook, function_at, function_signature):
    cave = at + signature["cave_offset"]
    cave_rva = pe.rva(cave)
    padding = signature["padding_bytes"]
    if pe.data[cave:cave + padding] != b"\xCC" * padding or pe.data[cave + padding:cave + padding + 1] == b"\xCC":
        _refuse("已审计的代码空隙长度或内容已变化。")
    if pe.data[cave - 5] != 0xE9:
        _refuse("代码空隙前不再是没有顺序落入的 JMP。")
    previous = pe.function_at(cave_rva + signature["previous_runtime_function_start_delta"])
    if previous is None or previous[:2] != (cave_rva + signature["previous_runtime_function_start_delta"], cave_rva + signature["previous_runtime_function_end_delta"]):
        _refuse("代码空隙邻近函数的展开边界已变化。")
    if any(start < cave_rva + padding and end > cave_rva for start, end, _ in pe.functions):
        _refuse("代码空隙被 x64 函数展开范围覆盖。")
    section = pe.section(cave_rva, padding)
    if not section["flags"] & 0x20000000 or section["flags"] & 0x80000000:
        _refuse("代码空隙不是只读可执行区段。")
    _validate_references(pe, at, signature)
    layouts = [(position, position + len(bytes.fromhex(record["bytes_hex"])),
                set(record["instruction_boundaries"]))
               for position, record in ((at, signature), (function_at, function_signature))]
    _validate_unused_ranges(pe, cave, hook, layouts)
    return cave


def _hashes(data, patches):
    original, patched = hashlib.sha256(), hashlib.sha256()
    cursor = 0
    for offset, replacement in sorted(patches):
        for start in range(cursor, offset, 1024 * 1024):
            chunk = data[start:min(start + 1024 * 1024, offset)]
            original.update(chunk)
            patched.update(chunk)
        original.update(data[offset:offset + len(replacement)])
        patched.update(replacement)
        cursor = offset + len(replacement)
    for start in range(cursor, len(data), 1024 * 1024):
        chunk = data[start:start + 1024 * 1024]
        original.update(chunk)
        patched.update(chunk)
    return original.hexdigest(), patched.hexdigest()


def resolve_hook_config(exe_path, base_config=None):
    """Return deployment's exact-hash/byte-bound config for this original EXE.

    A healthy installed game's *original backup*, rather than its patched EXE,
    must be passed. Already patched or unknown modified code is refused.
    """
    root = Path(__file__).resolve().parent / "data"
    if base_config is None:
        base_config = json.loads((root / "hook-config.json").read_text("utf-8-sig"))
    elif isinstance(base_config, (str, Path)):
        base_config = json.loads(Path(base_config).read_text("utf-8-sig"))
    metadata = json.loads((root / "exe-hook-signatures.json").read_text("utf-8-sig"))
    if metadata.get("schema") != "ac8-exe-hook-signature-v1":
        _refuse("工具内置代码签名版本无效。")
    try:
        with Path(exe_path).open("rb") as stream:
            if stream.seek(0, 2) < 0x100:
                _refuse("EXE 文件不完整。")
            with mmap.mmap(stream.fileno(), 0, access=mmap.ACCESS_READ) as data:
                pe = _PE(data)
                function = _unique_code_match(pe, metadata["function"], "语言后缀函数")
                hook = _validate_function(pe, function, metadata["function"], metadata["cultures"])
                context = _unique_code_match(pe, metadata["cave"], "已审计的代码空隙")
                cave = _validate_cave(pe, context, metadata["cave"], hook, function, metadata["function"])
                hook_rva, cave_rva = pe.rva(hook), pe.rva(cave)
                outgoing, incoming = cave_rva - (hook_rva + 5), (hook_rva + 5) - (cave_rva + 20)
                if not all(-(1 << 31) <= value < (1 << 31) for value in (outgoing, incoming)):
                    _refuse("代码空隙超出相对跳转范围。")
                replacement = b"\xE9" + struct.pack("<i", outgoing)
                trampoline = bytes.fromhex("4183fe0c75044183c60d664183c641e9") + struct.pack("<i", incoming)
                original_hash, patched_hash = _hashes(data, [(hook, replacement), (cave, trampoline)])
                patches = [dict(offset=position, original_hex=data[position:position + len(value)].hex(), patched_hex=value.hex()) for position, value in ((hook, replacement), (cave, trampoline))]
    except (OSError, struct.error, UnicodeError, OverflowError) as exc:
        raise HookCompatibilityError("无法读取或完整解析游戏 EXE：" + str(exc)) from exc
    result = dict(base_config)
    result.update(supported_exe_sha256=original_hash, patched_exe_sha256=patched_hash,
                  byte_patches=patches, culture_index=12, replacement_suffix="Z")
    result["compatibility"] = dict(method="audited-local-signatures-v1", hook_file_offset=hook,
                                  trampoline_file_offset=cave, file_sha256=original_hash,
                                  runtime_verified=False, compiler_layout_changes_supported=False)
    return result
