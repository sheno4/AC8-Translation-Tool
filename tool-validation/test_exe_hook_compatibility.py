"""Synthetic PE checks; no installed game is read, changed, or launched.

The fixture reuses the audited instruction bytes and supplies small PE tables
and synthetic data references. It verifies discovery/constraints and generated
bytes, not the execution of those instructions or a new game's runtime safety.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import struct
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tool"))
from ac8exehook import HookCompatibilityError, resolve_hook_config


def small_pe(*, file_shift=0, rva_shift=0, cave_shift=0):
    """Construct a ~28 KiB x64 PE containing the two exact audited layouts."""
    metadata = json.loads((ROOT / "tool/data/exe-hook-signatures.json").read_text("utf-8"))
    image_base = 0x140000000
    text_raw, text_rva, text_size = 0x400 + file_shift, 0x1000 + rva_shift, 0x4000
    data_raw, data_rva, data_size = text_raw + text_size, 0x8000 + rva_shift, 0x2000
    pdata_raw, pdata_rva, pdata_size = data_raw + data_size, 0xC000 + rva_shift, 0x400
    image = bytearray(pdata_raw + pdata_size)

    def put16(at, value):
        struct.pack_into("<H", image, at, value)

    def put32(at, value):
        struct.pack_into("<I", image, at, value)

    def put64(at, value):
        struct.pack_into("<Q", image, at, value)

    image[:2] = b"MZ"
    put32(0x3C, 0x80)
    image[0x80:0x84] = b"PE\0\0"
    put16(0x84, 0x8664)
    put16(0x86, 3)
    put16(0x94, 240)
    put16(0x96, 0x22)
    optional = 0x98
    put16(optional, 0x20B)
    put64(optional + 24, image_base)
    put32(optional + 32, 0x1000)
    put32(optional + 36, 0x200)
    put32(optional + 56, pdata_rva + 0x1000)
    put32(optional + 60, 0x400)
    put32(optional + 108, 16)
    struct.pack_into("<II", image, optional + 112 + 3 * 8, pdata_rva, 24)
    section_at = optional + 240
    sections = ((b".text", text_rva, text_raw, text_size, 0x60000020),
                (b".rdata", data_rva, data_raw, data_size, 0x40000040),
                (b".pdata", pdata_rva, pdata_raw, pdata_size, 0x40000040))
    for i, (name, rva, raw, size, flags) in enumerate(sections):
        at = section_at + i * 40
        image[at:at + len(name)] = name
        struct.pack_into("<IIII", image, at + 8, size, rva, size, raw)
        put32(at + 36, flags)

    function_raw, function_rva = text_raw + 0x100, text_rva + 0x100
    context_raw, context_rva = text_raw + 0x1400 + cave_shift, text_rva + 0x1400 + cave_shift
    hook_raw = function_raw + metadata["function"]["hook_offset"]
    hook_rva = function_rva + metadata["function"]["hook_offset"]
    cave_raw = context_raw + metadata["cave"]["cave_offset"]
    cave_rva = context_rva + metadata["cave"]["cave_offset"]
    function_bytes = bytes.fromhex(metadata["function"]["bytes_hex"])
    context_bytes = bytes.fromhex(metadata["cave"]["bytes_hex"])
    image[function_raw:function_raw + len(function_bytes)] = function_bytes
    image[context_raw:context_raw + len(context_bytes)] = context_bytes
    stub_raw, stub_rva = text_raw + 0x3000, text_rva + 0x3000
    image[stub_raw] = 0xC3
    # Both runtime entries use a minimal valid version-1 UNWIND_INFO header.
    image[data_raw + 0x100:data_raw + 0x104] = bytes((1, 0, 0, 0))
    runtime_functions = ((function_rva, function_rva + len(function_bytes), data_rva + 0x100),
                         (cave_rva - 19, cave_rva - 6, data_rva + 0x100))
    for i, row in enumerate(runtime_functions):
        struct.pack_into("<III", image, pdata_raw + i * 12, *row)

    allocation = 0x200

    def allocate(raw):
        nonlocal allocation
        at, rva = data_raw + allocation, data_rva + allocation
        image[at:at + len(raw)] = raw
        allocation += (len(raw) + 15) & ~15
        return at, rva

    for signature, raw, rva in ((metadata["function"], function_raw, function_rva),
                                 (metadata["cave"], context_raw, context_rva)):
        for reference in signature["references"]:
            if reference.get("image_base"):
                target = 0
            elif reference["kind"] == "branch":
                target = stub_rva
            elif "expected_hex" in reference:
                _, target = allocate(bytes.fromhex(reference["expected_hex"]))
            else:
                target = data_rva + 0x900
            displacement = target - (rva + reference["end"])
            struct.pack_into("<i", image, raw + reference["offset"], displacement)

    culture_table_raw, culture_table_rva = allocate(b"\0" * (len(metadata["cultures"]) * 8))
    culture_offsets = []
    for i, culture in enumerate(metadata["cultures"]):
        at, rva = allocate((culture + "\0").encode("utf-16le"))
        culture_offsets.append(at)
        put64(culture_table_raw + i * 8, image_base + rva)
    put32(function_raw + metadata["function"]["culture_table_rva_offset"], culture_table_rva)
    _, case_table_rva = allocate(bytes(range(128)))
    for offset in metadata["function"]["case_table_rva_offsets"]:
        put32(function_raw + offset, case_table_rva)
    return image, dict(image_base=image_base, hook_raw=hook_raw, hook_rva=hook_rva,
                       cave_raw=cave_raw, cave_rva=cave_rva, function_raw=function_raw,
                       culture_offsets=culture_offsets, pointer_raw=data_raw + 0x1000)


class ExeHookCompatibilityTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)

    def resolve(self, image, name="synthetic.exe"):
        path = Path(self.directory.name) / name
        path.write_bytes(image)
        return resolve_hook_config(path, dict(patch_id="synthetic-fixture", exe_path="Game/Binaries/Win64/AceCombat8.exe"))

    def assert_generated_hashes(self, image, config):
        self.assertEqual(config["supported_exe_sha256"], hashlib.sha256(image).hexdigest())
        patched = bytearray(image)
        for patch in config["byte_patches"]:
            at = patch["offset"]
            original = bytes.fromhex(patch["original_hex"])
            replacement = bytes.fromhex(patch["patched_hex"])
            self.assertEqual(image[at:at + len(original)], original)
            self.assertEqual(len(original), len(replacement))
            patched[at:at + len(replacement)] = replacement
        self.assertEqual(config["patched_exe_sha256"], hashlib.sha256(patched).hexdigest())

    def test_unrelated_file_change_is_not_a_version_rejection(self):
        image, positions = small_pe()
        first = self.resolve(image, "first.exe")
        modified = bytearray(image)
        modified[-1] = 0x61  # Outside code and the live exception entries.
        second = self.resolve(modified, "unrelated-change.exe")
        self.assertNotEqual(first["supported_exe_sha256"], second["supported_exe_sha256"])
        self.assertEqual(first["byte_patches"], second["byte_patches"])
        self.assertEqual(first["byte_patches"][0]["offset"], positions["hook_raw"])
        self.assert_generated_hashes(image, first)
        self.assert_generated_hashes(modified, second)

    def test_file_and_rva_movement_recomputes_both_relative_jumps(self):
        image, positions = small_pe(file_shift=0x2000, rva_shift=0x40000, cave_shift=0x400)
        config = self.resolve(image)
        hook, cave = config["byte_patches"]
        self.assertEqual(hook["offset"], positions["hook_raw"])
        self.assertEqual(cave["offset"], positions["cave_raw"])
        jump = bytes.fromhex(hook["patched_hex"])
        trampoline = bytes.fromhex(cave["patched_hex"])
        self.assertEqual(positions["hook_rva"] + 5 + struct.unpack_from("<i", jump, 1)[0], positions["cave_rva"])
        self.assertEqual(positions["cave_rva"] + 20 + struct.unpack_from("<i", trampoline, 16)[0], positions["hook_rva"] + 5)
        self.assert_generated_hashes(image, config)

    def test_changed_language_table_is_refused(self):
        image, positions = small_pe()
        at = positions["culture_offsets"][12]
        image[at:at + 12] = "zh-CHT".encode("utf-16le")
        with self.assertRaisesRegex(HookCompatibilityError, "语言表顺序"):
            self.resolve(image)

    def test_changed_function_instruction_is_refused(self):
        image, positions = small_pe()
        image[positions["function_raw"]] ^= 1
        with self.assertRaisesRegex(HookCompatibilityError, "语言后缀函数签名"):
            self.resolve(image)

    def test_changed_padding_is_refused(self):
        image, positions = small_pe()
        image[positions["cave_raw"] + 2] = 0x90
        with self.assertRaisesRegex(HookCompatibilityError, "代码空隙.*签名"):
            self.resolve(image)

    def test_absolute_pointer_into_cave_is_refused(self):
        image, positions = small_pe()
        struct.pack_into("<Q", image, positions["pointer_raw"], positions["image_base"] + positions["cave_rva"] + 3)
        with self.assertRaisesRegex(HookCompatibilityError, "绝对地址引用"):
            self.resolve(image)


if __name__ == "__main__":
    unittest.main()
