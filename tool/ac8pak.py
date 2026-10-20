"""Build the two-resource localization PAK and inspect its native loader inputs.

No game executable is loaded, executed, or modified. PAK generation only writes
the requested output directory. The bundled repak executable and native loader
are verified against their respective manifests before use.
"""
from __future__ import annotations

import hashlib
import json
import mmap
import os
from pathlib import Path
import re
import stat
import struct
import subprocess
import tempfile

from ac8exehook import HookCompatibilityError, _PE


RUNTIME_DIR = Path(__file__).resolve().parent / "data" / "runtime"
PAK_ENTRIES = (
    "Live/Content/Localization/GameData/CP_M.dat",
    "Live/Content/Localization/GameData/ON_M.dat",
)
PAK_FILENAME = "pakchunk0-Windows_998_P.pak"
ENTRY_BYTES = bytes.fromhex("488da42468ffffff4889842420000000")
CONSTRUCTOR_BYTES = bytes.fromhex(
    "48895c241048896c241848897424205741544155415641574883ec30488d0500"
    "0000004c8bea4889014c8bf1488d050000000033ff48894110498bd089790841"
)
SIGNED_STORE_BYTES = bytes.fromhex("4188b610020000")
SIGNED_STORE_OFFSET = 0x20E


class PakBackendError(ValueError):
    """Resource packaging or native loader compatibility could not be verified."""


def _fail(message):
    raise PakBackendError(message)


def _is_link(info):
    return stat.S_ISLNK(info.st_mode) or bool(getattr(info, "st_file_attributes", 0) & 0x400)


def _absolute_checked(path, *, must_exist=True):
    """Reject symlinks/junctions in the path, including all existing ancestors."""
    path = Path(os.path.abspath(os.fspath(path)))
    for current in (path, *path.parents):
        try:
            info = current.lstat()
        except FileNotFoundError:
            if current == path and not must_exist:
                continue
            _fail("路径不存在：" + str(current))
        if _is_link(info):
            _fail("不接受符号链接或目录联接：" + str(current))
    return path


def _regular_file(path):
    path = _absolute_checked(path)
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        _fail("资源或运行文件必须为普通文件，不能使用链接：" + str(path))
    return path


def _runtime_artifact(manifest_name, schema, artifact_name):
    try:
        manifest_path = _regular_file(RUNTIME_DIR / manifest_name)
        metadata = json.loads(manifest_path.read_text("utf-8-sig"))
        if metadata.get("schema") != schema:
            _fail("工具内置运行文件清单版本不受支持。")
        expected = metadata["artifacts"][artifact_name]["sha256"]
        if not isinstance(expected, str) or not re.fullmatch(r"[0-9a-fA-F]{64}", expected):
            _fail("工具内置运行文件清单缺少有效校验值。")
        artifact_path = _regular_file(RUNTIME_DIR / artifact_name)
        value = artifact_path.read_bytes()
        if hashlib.sha256(value).hexdigest() != expected.lower():
            _fail("工具内置运行文件校验失败，请重新解压完整工具包：" + artifact_name)
        return artifact_path, value
    except (OSError, UnicodeError, json.JSONDecodeError, KeyError, TypeError) as error:
        raise PakBackendError("无法读取工具内置运行文件：" + artifact_name) from error


def load_runtime_loader():
    """Return this tool's own dinput8 proxy after manifest/hash verification."""
    return _runtime_artifact("manifest.json", "ac8-pak-runtime-loader-v1", "dinput8.dll")[1]


def _load_repak():
    return _runtime_artifact("repak-manifest.json", "ac8-repak-runtime-v1", "repak.exe")[0]


def _run_repak(executable, arguments):
    try:
        result = subprocess.run(
            [str(executable), *map(str, arguments)], stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0), timeout=180,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise PakBackendError("资源打包工具未能完成，请检查工具包和输出目录。") from error
    if result.returncode:
        detail = result.stderr.decode("utf-8", "replace").strip()
        _fail("资源打包工具执行失败。" + ("\n" + detail[:1200] if detail else ""))
    return result.stdout


def _payload_files(root):
    root = _absolute_checked(root)
    if not root.is_dir():
        _fail("资源输入路径不是文件夹。")
    expected_files = set(PAK_ENTRIES)
    expected_dirs = {"/".join(entry.split("/")[:index])
                     for entry in PAK_ENTRIES for index in range(1, len(entry.split("/")))}
    found = {}

    def visit(directory):
        with os.scandir(directory) as rows:
            for row in rows:
                path = Path(row.path)
                # Windows DirEntry.stat caches st_nlink as zero; use a real
                # lstat so ordinary files and actual hard links are distinct.
                info = path.lstat()
                if _is_link(info):
                    _fail("资源输入包含符号链接或目录联接：" + row.name)
                relative = path.relative_to(root).as_posix()
                if stat.S_ISDIR(info.st_mode):
                    if relative not in expected_dirs:
                        _fail("资源输入包含非翻译目录：" + relative)
                    visit(path)
                elif stat.S_ISREG(info.st_mode) and info.st_nlink == 1:
                    if relative not in expected_files:
                        _fail("资源输入包含非翻译文件：" + relative)
                    found[relative] = path
                else:
                    _fail("资源输入包含链接或非普通文件：" + relative)

    visit(root)
    if set(found) != expected_files:
        _fail("资源输入必须恰好包含 CP_M.dat 和 ON_M.dat，且使用原游戏目录结构。")
    return root, found


def build_pak(payload_dir, output_path):
    """Pack exactly CP_M/ON_M, then compare every archived byte with its input.

    The output parent must exist. A temporary PAK in that same directory is
    verified before atomic replacement, leaving a previous output intact on
    failure. No extraction or recursive deletion is used.
    """
    temporary = None
    try:
        payload, files = _payload_files(payload_dir)
        output = _absolute_checked(output_path, must_exist=False)
        if output == payload or payload in output.parents:
            _fail("PAK 输出必须位于资源输入文件夹之外。")
        parent = output.parent
        if not parent.is_dir():
            _fail("PAK 输出目录不存在。")
        if output.exists():
            _regular_file(output)
        executable = _load_repak()
        source_hashes = {name: hashlib.sha256(path.read_bytes()).hexdigest()
                         for name, path in files.items()}
        temporary = Path(tempfile.mkdtemp(prefix=".ac8-pak-", dir=parent))
        candidate = temporary / "localized.pak"
        _run_repak(executable, ["pack", "--version", "V11", "--mount-point", "../../../",
                                payload, candidate])
        _regular_file(candidate)
        raw_list = _run_repak(executable, ["list", candidate]).decode("utf-8", "strict")
        entries = raw_list.splitlines()
        if sorted(entries) != sorted(PAK_ENTRIES):
            _fail("生成的 PAK 文件清单与两份翻译资源不一致，已停止。")
        archived_hashes = {}
        for name in PAK_ENTRIES:
            archived = _run_repak(executable, ["get", candidate, name])
            digest = hashlib.sha256(archived).hexdigest()
            if digest != source_hashes[name] or archived != files[name].read_bytes():
                _fail("PAK 回读内容与生成的翻译资源不一致：" + name)
            archived_hashes[name] = digest
        # Refuse a path replaced by a junction/link while repak was running.
        _absolute_checked(parent)
        if output.exists():
            _regular_file(output)
        os.replace(candidate, output)
        return dict(path=str(output), entries=list(PAK_ENTRIES), hashes=archived_hashes,
                    sha256=hashlib.sha256(output.read_bytes()).hexdigest(), version="V11",
                    mount_point="../../../")
    except (OSError, UnicodeError) as error:
        raise PakBackendError("无法生成或验证翻译 PAK：" + str(error)) from error
    finally:
        if temporary is not None:
            # The only possible child is the exact generated file. Do not use
            # rmtree or follow unknown directory contents during cleanup.
            _absolute_checked(temporary)
            if temporary.parent != parent or not temporary.name.startswith(".ac8-pak-"):
                _fail("临时打包目录范围校验失败，已保留现场。")
            candidate = temporary / "localized.pak"
            if candidate.exists():
                _regular_file(candidate).unlink()
            temporary.rmdir()


def _has_dinput_import(pe):
    rva, size = pe.directories[1]
    if not rva or size < 20 or size > 1024 * 1024:
        return False
    at = pe.offset(rva, size)
    found, terminated = False, False
    for offset in range(0, size - 19, 20):
        descriptor = struct.unpack_from("<IIIII", pe.data, at + offset)
        if not any(descriptor):
            terminated = True
            break
        name_rva, thunk = descriptor[3], descriptor[4]
        if not name_rva or not thunk:
            _fail("游戏 EXE 导入表不完整。")
        pe.offset(thunk, 8)
        name_offset = pe.offset(name_rva)
        section = pe.section(name_rva)
        limit = min(name_offset + 256, section["offset"] + section["size"])
        end = pe.data.find(b"\0", name_offset, limit)
        if end < 0:
            _fail("游戏 EXE 导入模块名无效。")
        name = pe.data[name_offset:end].decode("ascii", "strict")
        found = found or name.lower() == "dinput8.dll"
    if not terminated:
        _fail("游戏 EXE 导入表缺少结束标记。")
    return found


def _has_legacy_redirect(pe):
    """Identify the paired old Z-suffix detour when no verified backup is used."""
    prefix = bytes.fromhex('4183fe0c75044183c60d664183c641e9')
    pattern = re.compile(re.escape(prefix))
    for section in pe.sections:
        if not section['flags'] & 0x20000000:
            continue
        end = section['offset'] + section['size']
        for match in pattern.finditer(pe.data, section['offset'], end):
            cave = match.start()
            if cave + 20 > end:
                continue
            cave_rva = pe.rva(cave)
            hook_rva = cave_rva + 20 + struct.unpack_from('<i', pe.data, cave + 16)[0] - 5
            try:
                hook = pe.offset(hook_rva, 5)
            except HookCompatibilityError:
                continue
            if (pe.data[hook] == 0xE9
                    and hook_rva + 5 + struct.unpack_from('<i', pe.data, hook + 1)[0] == cave_rva):
                return True
    return False


def resolve_loader_config(exe_path, base_config=None):
    """Read-only signature discovery, independent of the locale suffix hook.

    The returned image hash binds this inspection to this deployment operation;
    it is not an allowlist of a fixed Steam build or an EXE patch instruction.
    """
    try:
        if base_config is None:
            base_config = json.loads((Path(__file__).resolve().parent / "data" / "hook-config.json").read_text("utf-8-sig"))
        elif isinstance(base_config, (str, Path)):
            base_config = json.loads(Path(base_config).read_text("utf-8-sig"))
        result = dict(base_config)
        with Path(exe_path).open("rb") as stream:
            if stream.seek(0, 2) < 0x100:
                _fail("游戏 EXE 文件不完整。")
            with mmap.mmap(stream.fileno(), 0, access=mmap.ACCESS_READ) as data:
                pe = _PE(data)
                if _has_legacy_redirect(pe):
                    _fail('检测到旧版 EXE 重定向残留，当前状态无法安全迁移；请先使用旧工具撤回修改。')
                optional = pe.u32(0x3C) + 24
                entry_rva = pe.u32(optional + 16)
                entry_at = pe.offset(entry_rva, len(ENTRY_BYTES))
                if not pe.section(entry_rva, len(ENTRY_BYTES))["flags"] & 0x20000000:
                    _fail("游戏 EXE 入口不在可执行区段中。")
                if data[entry_at:entry_at + len(ENTRY_BYTES)] != ENTRY_BYTES:
                    _fail("游戏 EXE 入口与已审计指令不一致，不能使用当前资源加载器。")
                if not _has_dinput_import(pe):
                    _fail("游戏 EXE 未直接导入 dinput8.dll，无法确认资源加载器会被加载。")
                masked = set(range(31, 35)) | set(range(47, 51))
                pattern = re.compile(b"".join(b"." if index in masked else re.escape(bytes([value]))
                                              for index, value in enumerate(CONSTRUCTOR_BYTES)), re.DOTALL)
                matches = []
                required = SIGNED_STORE_OFFSET + len(SIGNED_STORE_BYTES)
                for section in pe.sections:
                    if not section["flags"] & 0x20000000 or section["size"] < required:
                        continue
                    end = section["offset"] + section["size"]
                    for match in pattern.finditer(data, section["offset"], end):
                        at = match.start()
                        if at + required > end:
                            continue
                        if data[at + SIGNED_STORE_OFFSET:at + required] == SIGNED_STORE_BYTES:
                            matches.append(pe.rva(at))
                            if len(matches) > 1:
                                _fail("游戏资源构造函数存在多个匹配位置，已停止。")
                if not matches:
                    _fail("游戏资源构造函数签名已变化，需要适配当前游戏更新。")
                digest = hashlib.sha256(data).hexdigest()
    except (OSError, struct.error, UnicodeError, OverflowError, json.JSONDecodeError, TypeError) as error:
        raise PakBackendError("无法读取或完整解析游戏 EXE：" + str(error)) from error
    except HookCompatibilityError as error:
        raise PakBackendError("无法确认资源加载器兼容性：" + str(error)) from error
    for obsolete in ("byte_patches", "patched_exe_sha256", "culture_index", "replacement_suffix"):
        result.pop(obsolete, None)
    # Preserve the deployment identity so a pre-PAK installation can find its
    # existing backups and restore the original EXE during migration.
    result.setdefault("patch_id", "ac8-zh-text-25201480-v1")
    result.update(patch_version="1.1", supported_exe_sha256=digest, runtime_verified=False)
    result.setdefault("exe_path", "Game/Binaries/Win64/AceCombat8.exe")
    result.setdefault("process_names", ["AceCombat8.exe", "start_protected_game.exe"])
    result["compatibility"] = dict(method="pak-loader-signatures-v1", entry_rva=entry_rva,
                                   pak_constructor_rva=matches[0],
                                   signed_reader_store_rva=matches[0] + SIGNED_STORE_OFFSET,
                                   dinput8_direct_import=True, file_sha256=digest,
                                   runtime_verified=False)
    return result
