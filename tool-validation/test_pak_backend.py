"""One small PAK round-trip plus synthetic PE guards; never reads a game.

The PE fixture only validates static parsing and refusal conditions. It does
not claim native loader execution or in-game text rendering has been tested.
"""
from __future__ import annotations

import hashlib
from pathlib import Path
import struct
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tool"))
import ac8pak


def synthetic_pe():
    data = bytearray(0x1E00)
    data[:2] = b"MZ"
    struct.pack_into("<I", data, 0x3C, 0x80)
    data[0x80:0x84] = b"PE\0\0"
    struct.pack_into("<HH", data, 0x84, 0x8664, 3)
    struct.pack_into("<H", data, 0x94, 240)
    optional = 0x98
    struct.pack_into("<H", data, optional, 0x20B)
    struct.pack_into("<I", data, optional + 16, 0x1800)
    struct.pack_into("<Q", data, optional + 24, 0x140000000)
    struct.pack_into("<I", data, optional + 56, 0x5000)
    struct.pack_into("<I", data, optional + 108, 16)
    struct.pack_into("<II", data, optional + 112 + 8, 0x3000, 40)
    struct.pack_into("<II", data, optional + 112 + 3 * 8, 0x4000, 12)
    for index, (name, raw, rva, size, flags) in enumerate((
        (b".text", 0x400, 0x1000, 0x1000, 0x60000020),
        (b".rdata", 0x1400, 0x3000, 0x800, 0x40000040),
        (b".pdata", 0x1C00, 0x4000, 0x200, 0x40000040),
    )):
        at = optional + 240 + index * 40
        data[at:at + len(name)] = name
        struct.pack_into("<IIII", data, at + 8, size, rva, size, raw)
        struct.pack_into("<I", data, at + 36, flags)
    data[0xC00:0xC10] = ac8pak.ENTRY_BYTES
    data[0x500:0x540] = ac8pak.CONSTRUCTOR_BYTES
    data[0x70E:0x715] = ac8pak.SIGNED_STORE_BYTES
    struct.pack_into("<IIIII", data, 0x1400, 0, 0, 0, 0x3060, 0x3080)
    data[0x1460:0x146C] = b"DINPUT8.dll\0"
    data[0x1500:0x1504] = b"\x01\0\0\0"
    struct.pack_into("<III", data, 0x1C00, 0x1100, 0x1500, 0x3100)
    return data


class PakBackendTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="ac8-pak-")
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)

    def resolve(self, data):
        path = self.root / "synthetic.exe"
        path.write_bytes(data)
        return ac8pak.resolve_loader_config(path, {
            "patch_id": "legacy", "byte_patches": [{"offset": 0}],
            "patched_exe_sha256": "old", "culture_index": 12, "replacement_suffix": "Z",
        })

    def test_discovery_is_signature_bound_without_fixed_build_or_patch_bytes(self):
        image = synthetic_pe()
        self.assertEqual(len(ac8pak.CONSTRUCTOR_BYTES), 64)
        config = self.resolve(image)
        self.assertEqual(config["patch_id"], "legacy")
        self.assertEqual(config["patch_version"], "1.1")
        self.assertEqual(config["compatibility"]["method"], "pak-loader-signatures-v1")
        self.assertEqual(config["compatibility"]["entry_rva"], 0x1800)
        self.assertEqual(config["compatibility"]["pak_constructor_rva"], 0x1100)
        self.assertEqual(config["compatibility"]["signed_reader_store_rva"], 0x130E)
        self.assertEqual(config["supported_exe_sha256"], hashlib.sha256(image).hexdigest())
        self.assertFalse(config["runtime_verified"])
        for name in ("byte_patches", "patched_exe_sha256", "culture_index", "replacement_suffix"):
            self.assertNotIn(name, config)
        changed = bytearray(image)
        changed[-1] = 0x61
        changed[0x51F:0x523] = b"\x01\x02\x03\x04"  # Audited RIP-relative displacement mask.
        config2 = self.resolve(changed)
        self.assertNotEqual(config2["supported_exe_sha256"], config["supported_exe_sha256"])
        self.assertEqual(config2["compatibility"]["pak_constructor_rva"], 0x1100)

    def test_unknown_entry_constructor_signed_store_and_import_are_refused(self):
        variants = []
        for at, label in ((0xC00, "入口"), (0x500, "构造函数签名"),
                          (0x70E, "构造函数签名"), (0x1460, "未直接导入")):
            data = synthetic_pe()
            data[at] ^= 0x01
            variants.append((data, label))
        duplicate = synthetic_pe()
        duplicate[0x900:0x940] = ac8pak.CONSTRUCTOR_BYTES
        duplicate[0xB0E:0xB15] = ac8pak.SIGNED_STORE_BYTES
        variants.append((duplicate, "多个匹配"))
        for image, label in variants:
            with self.subTest(label=label):
                with self.assertRaisesRegex(ac8pak.PakBackendError, label):
                    self.resolve(image)

    def test_unowned_old_exe_redirect_is_not_silently_kept(self):
        data = synthetic_pe()
        hook, cave = 0xD00, 0xE00
        data[hook:hook + 5] = b'\xE9' + struct.pack('<i', cave - hook - 5)
        data[cave:cave + 20] = (bytes.fromhex('4183fe0c75044183c60d664183c641e9')
                               + struct.pack('<i', hook + 5 - cave - 20))
        with self.assertRaisesRegex(ac8pak.PakBackendError, '旧版 EXE 重定向残留'):
            self.resolve(data)

    def payload(self):
        directory = self.root / "中文资源"
        for index, name in enumerate(ac8pak.PAK_ENTRIES):
            path = directory / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(bytes([index]) + bytes(range(256)) + "剧情文本".encode("utf-16le"))
        return directory

    def test_two_resource_v11_pack_round_trip(self):
        payload = self.payload()
        output = self.root / "中文补丁.pak"
        result = ac8pak.build_pak(payload, output)
        self.assertEqual(result["entries"], list(ac8pak.PAK_ENTRIES))
        self.assertEqual(result["version"], "V11")
        self.assertEqual(result["mount_point"], "../../../")
        self.assertEqual(result["sha256"], hashlib.sha256(output.read_bytes()).hexdigest())
        self.assertEqual(result["hashes"], {name: hashlib.sha256((payload / name).read_bytes()).hexdigest()
                                             for name in ac8pak.PAK_ENTRIES})
        self.assertFalse(list(self.root.glob(".ac8-pak-*")))

    def test_extra_file_and_failed_pack_preserve_existing_output(self):
        payload = self.payload()
        output = self.root / "existing.pak"
        output.write_bytes(b"previous-good-output")
        extra = payload / "unexpected.txt"
        extra.write_text("unrelated", encoding="utf-8")
        with self.assertRaisesRegex(ac8pak.PakBackendError, "非翻译文件"):
            ac8pak.build_pak(payload, output)
        extra.unlink()
        with patch.object(ac8pak, "_run_repak", side_effect=ac8pak.PakBackendError("synthetic packing failure")):
            with self.assertRaisesRegex(ac8pak.PakBackendError, "synthetic packing failure"):
                ac8pak.build_pak(payload, output)
        self.assertEqual(output.read_bytes(), b"previous-good-output")
        self.assertFalse(list(self.root.glob(".ac8-pak-*")))


if __name__ == "__main__":
    unittest.main()
