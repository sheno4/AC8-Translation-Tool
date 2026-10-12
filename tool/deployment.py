"""Transactional deployment of two opaque GameData files and a bound EXE patch.

Importing this module never changes the game. GUI code must explicitly call
install/restore. No text decoding, network calls, image inspection or game launch.
"""
from __future__ import annotations

from contextlib import contextmanager
import ctypes
from ctypes import wintypes
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import uuid

DEFAULT_GAME_ROOT = r"S:\SteamLibrary\steamapps\common\ACE COMBAT 8"
DEFAULT_PATCH_ID = "ac8-zhcn-retranslated"
EXE_PATH = "Game/Binaries/Win64/AceCombat8.exe"
DATA_PATHS = (
    "Game/Content/Localization/GameData/CP_Z.dat",
    "Game/Content/Localization/GameData/ON_Z.dat",
)
BACKUP_DIRECTORY = ".AC8-Localization-backup"
OWNER = "AC8.Localization.Deployment"
SCHEMA_VERSION = 1


class DeploymentError(RuntimeError):
    """A safe refusal or recoverable deployment failure for the GUI to display."""


def _sha(path: Path) -> str:
    algorithm = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            algorithm.update(block)
    return algorithm.hexdigest()


def _patch_id(value: str) -> str:
    if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_.-]{0,63}", value or ""):
        raise DeploymentError("补丁标识无效。")
    if value.lower().split(".", 1)[0] in {"con", "prn", "aux", "nul", *(f"com{i}" for i in range(1, 10)), *(f"lpt{i}" for i in range(1, 10))}:
        raise DeploymentError("补丁标识不能使用 Windows 保留名称。")
    return value


def _root(game_root) -> Path:
    root = Path(game_root).resolve()
    if not root.is_dir():
        raise DeploymentError(f"游戏目录不存在：{root}")
    return root


def _safe(root: Path, relative: str) -> Path:
    relative = str(relative).replace("\\", "/")
    parts = relative.split("/")
    if not parts or any(
        not part or part in {".", ".."} or re.search(r'[<>:"|?*\x00-\x1f]', part)
        or part.endswith((" ", "."))
        or re.match(r"(?i)^(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\.|$)", part)
        for part in parts
    ):
        raise DeploymentError(f"相对路径无效：{relative}")
    candidate = root.joinpath(*parts)
    cursor = root
    for part in parts:
        cursor /= part
        try:
            info = cursor.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400):
            raise DeploymentError(f"目标路径含链接，已停止：{cursor}")
    resolved = candidate.resolve(strict=False)
    if root not in resolved.parents:
        raise DeploymentError(f"路径越界：{relative}")
    return candidate


def _json_read(path: Path) -> dict:
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise DeploymentError(f"状态文件字段重复：{key}")
            result[key] = value
        return result
    try:
        with path.open("r", encoding="utf-8-sig") as stream:
            result = json.load(stream, object_pairs_hook=unique)
        if not isinstance(result, dict):
            raise DeploymentError("补丁状态不是对象。")
        return result
    except (OSError, ValueError) as exc:
        raise DeploymentError(f"无法读取补丁状态：{path.name}：{exc}") from exc


def _json_write(path: Path, value: dict) -> None:
    temp = path.with_name(path.name + ".tmp-" + uuid.uuid4().hex)
    try:
        with temp.open("x", encoding="utf-8", newline="\n") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2, allow_nan=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
    finally:
        if temp.exists():
            temp.unlink()


def _empty_dir(path: Path) -> None:
    if path.is_dir():
        try:
            path.rmdir()
        except OSError:
            pass  # Other files, including the operation lock, must be preserved.


def _current(path: Path):
    if path.is_file():
        return _sha(path)
    if path.exists():
        raise DeploymentError(f"补丁目标不是普通文件：{path}")
    return None


def _assert_current(path: Path, expected, label: str) -> None:
    actual = _current(path)
    if actual != expected:
        raise DeploymentError(f"文件已被其他程序修改，已停止以保留现有内容：{label}")


def _assert_game_stopped(root: Path) -> None:
    """Read-only process snapshot; fail closed if enumeration is unavailable."""
    if os.name != "nt":
        raise DeploymentError("游戏导入/撤回模块仅支持 Windows。")

    class ProcessEntry(ctypes.Structure):
        _fields_ = [
            ("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD),
            ("th32ProcessID", wintypes.DWORD), ("th32DefaultHeapID", ctypes.c_size_t),
            ("th32ModuleID", wintypes.DWORD), ("cntThreads", wintypes.DWORD),
            ("th32ParentProcessID", wintypes.DWORD), ("pcPriClassBase", wintypes.LONG),
            ("dwFlags", wintypes.DWORD), ("szExeFile", wintypes.WCHAR * 260),
        ]

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    kernel.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    kernel.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(ProcessEntry)]
    kernel.Process32FirstW.restype = wintypes.BOOL
    kernel.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(ProcessEntry)]
    kernel.Process32NextW.restype = wintypes.BOOL
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
    kernel.QueryFullProcessImageNameW.restype = wintypes.BOOL
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    snapshot = kernel.CreateToolhelp32Snapshot(2, 0)
    if snapshot in (None, ctypes.c_void_p(-1).value):
        raise DeploymentError("无法枚举游戏进程，已停止。")
    found = []
    root_prefix = os.path.normcase(str(root)).rstrip("\\/") + "\\"
    try:
        entry = ProcessEntry()
        entry.dwSize = ctypes.sizeof(entry)
        if not kernel.Process32FirstW(snapshot, ctypes.byref(entry)):
            raise DeploymentError("无法读取进程列表，已停止。")
        while True:
            name = entry.szExeFile
            handle = kernel.OpenProcess(0x1000, False, entry.th32ProcessID)
            image = None
            if handle:
                try:
                    buffer = ctypes.create_unicode_buffer(32768)
                    size = wintypes.DWORD(len(buffer))
                    if kernel.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(size)):
                        image = os.path.normcase(buffer.value)
                finally:
                    kernel.CloseHandle(handle)
            if (image and image.startswith(root_prefix)) or (not image and name.lower() in {"acecombat8.exe", "start_protected_game.exe"}):
                found.append(f"{name} (PID {entry.th32ProcessID})")
            if not kernel.Process32NextW(snapshot, ctypes.byref(entry)):
                if ctypes.get_last_error() != 18:  # ERROR_NO_MORE_FILES
                    raise DeploymentError("进程列表读取不完整，已停止。")
                break
    finally:
        kernel.CloseHandle(snapshot)
    if found:
        raise DeploymentError("请先退出游戏及其启动器：" + "、".join(found))


@contextmanager
def _lock(root: Path, patch_id: str):
    directory = _safe(root, f"{BACKUP_DIRECTORY}/{patch_id}")
    directory.mkdir(parents=True, exist_ok=True)
    lock_path = _safe(root, f"{BACKUP_DIRECTORY}/{patch_id}/operation.lock")
    stream = lock_path.open("a+b")
    acquired = False
    try:
        if stream.tell() == 0:
            stream.write(b"\0")
            stream.flush()
        stream.seek(0)
        try:
            import msvcrt
            msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            acquired = True
        except (ImportError, OSError) as exc:
            raise DeploymentError("已有导入/撤回操作正在执行，或无法锁定操作目录。") from exc
        try:
            yield directory
        finally:
            stream.seek(0)
            msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
    finally:
        stream.close()
        if acquired:
            try:
                lock_path.unlink()
            except OSError:
                pass
        _empty_dir(directory)
        _empty_dir(directory.parent)


def _owned(value: dict, patch_id: str, *, journal=False) -> None:
    if value.get("owner") != OWNER or value.get("schema_version") != SCHEMA_VERSION or value.get("patch_id") != patch_id:
        raise DeploymentError("补丁备份/事务目录不属于此补丁。")
    entries = value.get("files")
    if not isinstance(entries, list) or any(not isinstance(entry, dict) for entry in entries) or [entry.get("path") for entry in entries] != [EXE_PATH, *DATA_PATHS]:
        raise DeploymentError("补丁状态管理的文件与本工具不符。")
    allowed_directories = {str(parent) for path in [EXE_PATH, *DATA_PATHS] for parent in PurePosixPath(path).parents if str(parent) != "."}
    created_directories = value.get("created_directories", [])
    if not isinstance(created_directories, list) or any(relative not in allowed_directories for relative in created_directories):
        raise DeploymentError("补丁状态含不属于管理目标的目录。")
    if journal:
        if value.get("action") not in {"install", "restore"} or value.get("phase") not in {"preparing", "prepared"} or not isinstance(value.get("had_state"), bool):
            raise DeploymentError("补丁事务类型或阶段无效。")
    elif value.get("status") not in {"installed", "restored"}:
        raise DeploymentError("补丁状态标识无效。")
    for index, entry in enumerate(entries):
        expected = f"backups/{index:05d}.bin"
        backup = entry.get("backup")
        updated_exe_backup = (index == 0 and isinstance(backup, str)
                              and re.fullmatch(r"backups/[0-9a-f]{32}-00000\.bin", backup))
        if not journal and backup not in (None, expected) and not updated_exe_backup:
            raise DeploymentError("原文件备份路径无效。")
        if not journal and bool(entry.get("backup")) != bool(entry.get("original_sha256")):
            raise DeploymentError("原文件备份与校验值不一致。")
        if journal and entry.get("rollback") != f"transactions/{value.get('transaction_id')}/rollback/{index:05d}.bin":
            raise DeploymentError("回滚文件路径无效。")
        for key in ("original_sha256", "installed_sha256", "old_sha256", "new_sha256"):
            item = entry.get(key)
            if item is not None and not re.fullmatch("[0-9a-f]{64}", item):
                raise DeploymentError("状态校验值无效。")
    if journal and not re.fullmatch("[0-9a-f]{32}", value.get("transaction_id", "")):
        raise DeploymentError("事务标识无效。")
    if journal:
        for key in ("new_backups", "retired_backups"):
            paths = value.get(key, [])
            if not isinstance(paths, list) or any(
                    not isinstance(path, str) or not re.fullmatch(
                        r"backups/(?:[0-9a-f]{32}-)?00000\.bin", path) for path in paths):
                raise DeploymentError("更新 EXE 的事务备份路径无效。")


def _internal(root: Path, patch_id: str, relative: str) -> Path:
    return _safe(root, f"{BACKUP_DIRECTORY}/{patch_id}/{relative}")


def _load_state(directory: Path, patch_id: str):
    path = _safe(directory, "state.json")
    if path.exists():
        state = _json_read(path)
        _owned(state, patch_id)
        return state
    return None


def _verify_installed(root: Path, state: dict, patch_id: str) -> None:
    for entry in state["files"]:
        _assert_current(_safe(root, entry["path"]), entry["installed_sha256"], entry["path"])
        if entry["backup"]:
            _assert_current(_internal(root, patch_id, entry["backup"]), entry["original_sha256"], "原文件备份 " + entry["path"])


def _clear_transaction(root: Path, patch_id: str, journal: dict) -> None:
    transaction_id = journal["transaction_id"]
    for index, _ in enumerate(journal["files"]):
        for folder in ("rollback", "stage"):
            path = _internal(root, patch_id, f"transactions/{transaction_id}/{folder}/{index:05d}.bin")
            if path.is_file():
                path.unlink()
    for relative in (f"transactions/{transaction_id}/rollback", f"transactions/{transaction_id}/stage", f"transactions/{transaction_id}", "transactions"):
        _empty_dir(_internal(root, patch_id, relative))
    # Keep whichever original backup the committed state currently owns. This
    # makes interruption before/after a new game EXE baseline recoverable.
    directory = _safe(root, f"{BACKUP_DIRECTORY}/{patch_id}")
    state = _load_state(directory, patch_id)
    active_backups = {entry.get("backup") for entry in state["files"]} if state else set()
    for relative in journal.get("new_backups", []) + journal.get("retired_backups", []):
        path = _internal(root, patch_id, relative)
        if relative not in active_backups and path.is_file():
            path.unlink()
    path = _internal(root, patch_id, "journal.json")
    if path.is_file():
        path.unlink()


def _clear_backups(root: Path, patch_id: str, state: dict) -> None:
    for entry in state["files"]:
        if entry["backup"]:
            path = _internal(root, patch_id, entry["backup"])
            if path.is_file():
                path.unlink()
    _empty_dir(_internal(root, patch_id, "backups"))
    path = _internal(root, patch_id, "state.json")
    if path.is_file():
        path.unlink()
    for relative in sorted(state.get("created_directories", []), key=len, reverse=True):
        _empty_dir(_safe(root, relative))


def _rollback(root: Path, patch_id: str, journal: dict) -> None:
    _owned(journal, patch_id, journal=True)
    # Preflight every target before reverting any, so unknown external content
    # is never overwritten by a partially performed recovery.
    for entry in journal["files"]:
        actual = _current(_safe(root, entry["path"]))
        if actual == entry["old_sha256"]:
            continue
        if actual != entry.get("new_sha256") or journal.get("phase") == "preparing":
            raise DeploymentError(f"无法安全恢复未完成操作，文件出现未知内容：{entry['path']}")
        if entry["old_sha256"]:
            _assert_current(_internal(root, patch_id, entry["rollback"]), entry["old_sha256"], "事务回滚备份")
    for index, entry in enumerate(journal["files"]):
        target = _safe(root, entry["path"])
        if _current(target) == entry["old_sha256"]:
            continue
        if entry["old_sha256"] is None:
            _assert_current(target, entry["new_sha256"], entry["path"])
            target.unlink()
        else:
            stage = _internal(root, patch_id, f"transactions/{journal['transaction_id']}/stage/{index:05d}.bin")
            shutil.copyfile(_internal(root, patch_id, entry["rollback"]), stage)
            _assert_current(stage, entry["old_sha256"], "待回滚原文件")
            # The all-target preflight above can precede a large file copy.
            # Recheck immediately before replacing so an intervening external
            # edit is retained with the journal instead of being overwritten.
            _assert_current(target, entry["new_sha256"], entry["path"])
            os.replace(stage, target)
    if journal["action"] == "install" and not journal["had_state"]:
        for index, _ in enumerate(journal["files"]):
            backup = _internal(root, patch_id, f"backups/{index:05d}.bin")
            if backup.is_file():
                backup.unlink()
        _empty_dir(_internal(root, patch_id, "backups"))
        for relative in sorted(journal.get("created_directories", []), key=len, reverse=True):
            _empty_dir(_safe(root, relative))
    # Delete the journal last, so a crash while removing preparation-only
    # backup files still has an ownership record and can finish its cleanup.
    _clear_transaction(root, patch_id, journal)


def _recover(root: Path, patch_id: str, directory: Path) -> None:
    state = _load_state(directory, patch_id)
    journal_path = _safe(directory, "journal.json")
    if journal_path.exists():
        journal = _json_read(journal_path)
        _owned(journal, patch_id, journal=True)
        if state and state.get("last_transaction_id") == journal["transaction_id"]:
            _clear_transaction(root, patch_id, journal)
            if state["status"] == "restored":
                _clear_backups(root, patch_id, state)
        else:
            _rollback(root, patch_id, journal)
    elif state and state["status"] == "restored":
        _clear_backups(root, patch_id, state)


def _config(value) -> dict:
    config = _json_read(Path(value)) if isinstance(value, (str, os.PathLike)) else dict(value)
    patch_id = _patch_id(config.get("patch_id", DEFAULT_PATCH_ID))
    if config.get("exe_path", EXE_PATH).replace("\\", "/") != EXE_PATH:
        raise DeploymentError("EXE 目标必须是当前游戏的 AceCombat8.exe。")
    hashes = config.get("supported_exe_sha256", [])
    if isinstance(hashes, str):
        hashes = [hashes]
    hashes = [str(item).lower() for item in hashes]
    if not hashes or any(not re.fullmatch("[0-9a-f]{64}", item) for item in hashes):
        raise DeploymentError("Hook 配置没有绑定有效游戏版本 SHA-256。")
    patches = []
    ranges = []
    for item in config.get("byte_patches", []):
        offset = item.get("offset", item.get("file_offset"))
        offset = int(offset, 0) if isinstance(offset, str) else offset
        try:
            original = bytes.fromhex(item["original_hex"])
            replacement = bytes.fromhex(item["patched_hex"])
        except (KeyError, ValueError, TypeError) as exc:
            raise DeploymentError("Hook 字节格式无效。") from exc
        if not isinstance(offset, int) or isinstance(offset, bool) or offset < 0 or not original or len(original) != len(replacement):
            raise DeploymentError("Hook 偏移或等长替换字节无效。")
        if any(offset < end and offset + len(original) > start for start, end in ranges):
            raise DeploymentError("Hook 字节替换范围重叠。")
        ranges.append((offset, offset + len(original)))
        patches.append({"offset": offset, "original_hex": original.hex(), "patched_hex": replacement.hex()})
    if not patches:
        raise DeploymentError("Hook 配置没有任何定向替换字节。")
    expected_patched = str(config.get("patched_exe_sha256", "")).lower()
    if not re.fullmatch("[0-9a-f]{64}", expected_patched):
        raise DeploymentError("Hook 必须绑定预期 patched_exe_sha256。")
    rebase = config.get("rebase_exe", False)
    if type(rebase) is not bool:
        raise DeploymentError("更新 EXE 的基准标识必须为布尔值。")
    return {"patch_id": patch_id, "patch_version": str(config.get("patch_version", "1")), "exe_path": EXE_PATH, "supported_exe_sha256": sorted(hashes), "patched_exe_sha256": expected_patched, "byte_patches": patches, "rebase_exe": rebase}


def _copy_payload(value, destination: Path) -> None:
    if isinstance(value, (bytes, bytearray, memoryview)):
        with destination.open("xb") as stream:
            stream.write(value)
            stream.flush()
            os.fsync(stream.fileno())
    else:
        source = Path(value)
        if not source.is_file():
            raise DeploymentError(f"翻译 DAT 不存在：{source}")
        shutil.copyfile(source, destination)
    if destination.stat().st_size == 0:
        raise DeploymentError("翻译 DAT 为空，已停止。")


def _apply_bytes(source: Path, stage: Path, patches: list[dict]) -> None:
    shutil.copyfile(source, stage)
    with stage.open("r+b") as stream:
        for patch in patches:
            original = bytes.fromhex(patch["original_hex"])
            replacement = bytes.fromhex(patch["patched_hex"])
            stream.seek(patch["offset"])
            if stream.read(len(original)) != original:
                raise DeploymentError(f"EXE 预期字节不匹配：0x{patch['offset']:X}")
            stream.seek(patch["offset"])
            stream.write(replacement)
        stream.flush()
        os.fsync(stream.fileno())


def _created_directories(root: Path, paths: list[str]) -> list[str]:
    result = set()
    for relative in paths:
        parent = _safe(root, relative).parent
        while parent != root and not parent.exists():
            result.add(parent.relative_to(root).as_posix())
            parent = parent.parent
    return sorted(result)


def status(game_root=DEFAULT_GAME_ROOT, patch_id=DEFAULT_PATCH_ID) -> dict:
    """Read-only, structured status. Never automatically recovers a journal."""
    root = _root(game_root)
    patch_id = _patch_id(patch_id)
    directory = _safe(root, f"{BACKUP_DIRECTORY}/{patch_id}")
    journal = _safe(root, f"{BACKUP_DIRECTORY}/{patch_id}/journal.json")
    if journal.exists():
        return {"state": "incomplete", "installed": False, "patch_id": patch_id, "message": "有未完成操作。下次导入或撤回会先恢复。"}
    state = _load_state(directory, patch_id)
    if not state or state["status"] == "restored":
        return {"state": "not_installed", "installed": False, "patch_id": patch_id, "message": "当前未导入此汉化。"}
    checks = []
    for entry in state["files"]:
        actual = _current(_safe(root, entry["path"]))
        backup_ok = not entry["backup"] or _current(_internal(root, patch_id, entry["backup"])) == entry["original_sha256"]
        checks.append({"path": entry["path"], "valid": actual == entry["installed_sha256"], "backup_valid": backup_ok, "actual_sha256": actual, "installed_sha256": entry["installed_sha256"]})
    valid = all(item["valid"] and item["backup_valid"] for item in checks)
    return {"state": "installed" if valid else "modified", "installed": True, "patch_id": patch_id, "patch_version": state["patch_version"], "original_exe_sha256": state["files"][0]["original_sha256"], "patched_exe_sha256": state["files"][0]["installed_sha256"], "fingerprint": state["fingerprint"], "files": checks, "message": "汉化已导入，文件及备份校验正常。" if valid else "已安装文件或备份发生变化，需处理后才能更新/撤回。"}


def original_exe_hash(game_root=DEFAULT_GAME_ROOT, patch_id=DEFAULT_PATCH_ID) -> str:
    """Read-only source version identity, including a healthy existing install."""
    root = _root(game_root)
    patch_id = _patch_id(patch_id)
    directory = _safe(root, f"{BACKUP_DIRECTORY}/{patch_id}")
    if (directory / "journal.json").exists():
        raise DeploymentError("有未完成操作，无法确认源 EXE 版本。请先导入或撤回以恢复。")
    state = _load_state(directory, patch_id)
    if state and state["status"] == "installed":
        _verify_installed(root, state, patch_id)
        return state["files"][0]["original_sha256"]
    result = _current(_safe(root, EXE_PATH))
    if result is None:
        raise DeploymentError("找不到游戏 AceCombat8.exe。")
    return result


def inspect_exe_source(game_root, patch_id=DEFAULT_PATCH_ID) -> dict:
    """Read an original backup or a candidate new EXE after a game update.

    The caller must validate a replacement EXE's patch structure and resources
    before requesting rebase. Existing DAT files and backups remain guarded.
    """
    root = _root(game_root)
    patch_id = _patch_id(patch_id)
    directory = _safe(root, f"{BACKUP_DIRECTORY}/{patch_id}")
    if (directory / "journal.json").exists():
        raise DeploymentError("有未完成操作，请先撤回修改以恢复事务。")
    state = _load_state(directory, patch_id)
    current = _safe(root, EXE_PATH)
    actual = _current(current)
    if actual is None:
        raise DeploymentError("找不到游戏 AceCombat8.exe。")
    if state and state["status"] == "installed":
        for index, entry in enumerate(state["files"]):
            if entry["backup"]:
                _assert_current(_internal(root, patch_id, entry["backup"]),
                                entry["original_sha256"], "原文件备份 " + entry["path"])
            if index:
                _assert_current(_safe(root, entry["path"]),
                                entry["installed_sha256"], entry["path"])
        if actual == state["files"][0]["installed_sha256"]:
            original = _internal(root, patch_id, state["files"][0]["backup"])
            return dict(path=str(original), sha256=state["files"][0]["original_sha256"],
                        current_sha256=actual, installed=True, rebase_exe=False)
        return dict(path=str(current), sha256=actual, current_sha256=actual,
                    installed=True, rebase_exe=True)
    return dict(path=str(current), sha256=actual, current_sha256=actual,
                installed=False, rebase_exe=False)


def install(game_root, payloads: dict, hook_config) -> dict:
    """Install/update byte-bound EXE hook and two opaque DAT payloads.

    payloads maps DATA_PATHS to bytes or existing file paths. hook_config is a
    dict or JSON path: patch_id, supported_exe_sha256, byte_patches containing
    offset (file offset), original_hex, patched_hex. No game auto-launch occurs.
    """
    root = _root(game_root)
    config = _config(hook_config)
    patch_id = config["patch_id"]
    values = {str(path).replace("\\", "/"): value for path, value in payloads.items()}
    if set(values) != set(DATA_PATHS):
        raise DeploymentError("导入必须恰好提供 CP_Z.dat 和 ON_Z.dat 两个二进制文件。")
    _assert_game_stopped(root)
    with _lock(root, patch_id) as directory:
        _recover(root, patch_id, directory)
        previous = _load_state(directory, patch_id)
        if previous:
            if config["rebase_exe"]:
                source = inspect_exe_source(root, patch_id)
                if not source["rebase_exe"]:
                    raise DeploymentError("游戏 EXE 状态已变化，请重新运行导入。")
                original_exe = Path(source["path"])
                original_hash = source["sha256"]
            else:
                _verify_installed(root, previous, patch_id)
                original_exe = _internal(root, patch_id, previous["files"][0]["backup"])
                original_hash = previous["files"][0]["original_sha256"]
        else:
            original_exe = _safe(root, EXE_PATH)
            original_hash = _current(original_exe)
            # Refuse an unrelated directory; allow only the currently held lock.
            if any(item.name != "operation.lock" for item in directory.iterdir()):
                raise DeploymentError("备份目录存在未知内容，已停止。")
        if original_hash not in config["supported_exe_sha256"]:
            raise DeploymentError("游戏 EXE 在兼容检查后发生变化，请重新运行导入。")
        transaction_id = uuid.uuid4().hex
        files = []
        originals = []
        created = previous.get("created_directories", []) if previous else _created_directories(root, [EXE_PATH, *DATA_PATHS])
        for index, relative in enumerate([EXE_PATH, *DATA_PATHS]):
            if index == 0 and config["rebase_exe"]:
                old_hash = original_hash
            elif previous:
                old_hash = previous["files"][index]["installed_sha256"]
            else:
                old_hash = original_hash if index == 0 else _current(_safe(root, relative))
            files.append({"path": relative, "old_sha256": old_hash, "new_sha256": None, "rollback": f"transactions/{transaction_id}/rollback/{index:05d}.bin"})
            originals.append(dict(previous["files"][index]) if previous else {"path": relative, "original_sha256": old_hash, "backup": f"backups/{index:05d}.bin" if old_hash else None})
        journal = {"owner": OWNER, "schema_version": SCHEMA_VERSION, "patch_id": patch_id, "transaction_id": transaction_id, "action": "install", "phase": "preparing", "had_state": bool(previous), "created_directories": created, "files": files}
        if previous and config["rebase_exe"]:
            replacement_backup = f"backups/{transaction_id}-00000.bin"
            journal["new_backups"] = [replacement_backup]
            journal["retired_backups"] = [originals[0]["backup"]]
            originals[0].update(original_sha256=original_hash, backup=replacement_backup)
        _json_write(directory / "journal.json", journal)
        try:
            for folder in ("backups", f"transactions/{transaction_id}/rollback", f"transactions/{transaction_id}/stage"):
                _internal(root, patch_id, folder).mkdir(parents=True, exist_ok=True)
            for index, entry in enumerate(files):
                target = _safe(root, entry["path"])
                if entry["old_sha256"]:
                    rollback = _internal(root, patch_id, entry["rollback"])
                    shutil.copyfile(target, rollback)
                    _assert_current(rollback, entry["old_sha256"], "事务备份")
                    if not previous:
                        backup = _internal(root, patch_id, originals[index]["backup"])
                        shutil.copyfile(target, backup)
                        _assert_current(backup, entry["old_sha256"], "原文件备份")
                    elif index == 0 and config["rebase_exe"]:
                        backup = _internal(root, patch_id, originals[index]["backup"])
                        shutil.copyfile(target, backup)
                        _assert_current(backup, entry["old_sha256"], "更新后 EXE 原文件备份")
                stage = _internal(root, patch_id, f"transactions/{transaction_id}/stage/{index:05d}.bin")
                if index == 0:
                    _apply_bytes(original_exe, stage, config["byte_patches"])
                else:
                    _copy_payload(values[entry["path"]], stage)
                entry["new_sha256"] = _sha(stage)
                if index == 0 and entry["new_sha256"] != config["patched_exe_sha256"]:
                    raise DeploymentError("定向修改后的 EXE 整文件 SHA-256 与预期不符，已停止。")
                originals[index]["installed_sha256"] = entry["new_sha256"]
            fingerprint = hashlib.sha256(json.dumps([config, [entry["new_sha256"] for entry in files]], sort_keys=True, separators=(",", ":")).encode()).hexdigest()
            if previous and not config["rebase_exe"] and previous["fingerprint"] == fingerprint:
                _clear_transaction(root, patch_id, journal)
                result = status(root, patch_id)
                result["action"] = "unchanged"
                return result
            journal["phase"] = "prepared"
            _json_write(directory / "journal.json", journal)
            _assert_game_stopped(root)
            for index, entry in enumerate(files):
                target = _safe(root, entry["path"])
                _assert_current(target, entry["old_sha256"], entry["path"])
                target.parent.mkdir(parents=True, exist_ok=True)
                stage = _internal(root, patch_id, f"transactions/{transaction_id}/stage/{index:05d}.bin")
                os.replace(stage, target)
                _assert_current(target, entry["new_sha256"], entry["path"])
            state = {"owner": OWNER, "schema_version": SCHEMA_VERSION, "patch_id": patch_id, "patch_version": config["patch_version"], "status": "installed", "fingerprint": fingerprint, "last_transaction_id": transaction_id, "created_directories": created, "files": originals}
            _json_write(directory / "state.json", state)
            _clear_transaction(root, patch_id, journal)
        except Exception as exc:
            committed = _load_state(directory, patch_id)
            if not committed or committed.get("last_transaction_id") != transaction_id:
                try:
                    _rollback(root, patch_id, journal)
                except Exception as recovery_exc:
                    raise DeploymentError(f"导入失败，自动回滚未完成：{recovery_exc}。备份与事务日志已保留。") from exc
            if isinstance(exc, DeploymentError):
                raise
            raise DeploymentError(f"导入失败：{exc}") from exc
        result = status(root, patch_id)
        result["action"] = "updated" if previous else "installed"
        return result


def restore(game_root=DEFAULT_GAME_ROOT, patch_id=DEFAULT_PATCH_ID) -> dict:
    """Restore the exact pre-install EXE and pre-existing Z files; idempotent."""
    root = _root(game_root)
    patch_id = _patch_id(patch_id)
    directory = _safe(root, f"{BACKUP_DIRECTORY}/{patch_id}")
    if not directory.exists():
        result = status(root, patch_id)
        result["action"] = "unchanged"
        return result
    _assert_game_stopped(root)
    with _lock(root, patch_id) as directory:
        _recover(root, patch_id, directory)
        previous = _load_state(directory, patch_id)
        if not previous:
            result = status(root, patch_id)
            result["action"] = "unchanged"
            return result
        _verify_installed(root, previous, patch_id)
        transaction_id = uuid.uuid4().hex
        files = [{"path": entry["path"], "old_sha256": entry["installed_sha256"], "new_sha256": entry["original_sha256"], "rollback": f"transactions/{transaction_id}/rollback/{index:05d}.bin"} for index, entry in enumerate(previous["files"])]
        journal = {"owner": OWNER, "schema_version": SCHEMA_VERSION, "patch_id": patch_id, "transaction_id": transaction_id, "action": "restore", "phase": "preparing", "had_state": True, "files": files}
        _json_write(directory / "journal.json", journal)
        try:
            for folder in (f"transactions/{transaction_id}/rollback", f"transactions/{transaction_id}/stage"):
                _internal(root, patch_id, folder).mkdir(parents=True, exist_ok=True)
            for index, entry in enumerate(files):
                rollback = _internal(root, patch_id, entry["rollback"])
                shutil.copyfile(_safe(root, entry["path"]), rollback)
                _assert_current(rollback, entry["old_sha256"], "事务备份")
                backup = previous["files"][index]["backup"]
                if backup:
                    stage = _internal(root, patch_id, f"transactions/{transaction_id}/stage/{index:05d}.bin")
                    shutil.copyfile(_internal(root, patch_id, backup), stage)
                    _assert_current(stage, entry["new_sha256"], "待恢复原文件")
            journal["phase"] = "prepared"
            _json_write(directory / "journal.json", journal)
            _assert_game_stopped(root)
            _verify_installed(root, previous, patch_id)
            for index, entry in enumerate(files):
                target = _safe(root, entry["path"])
                _assert_current(target, entry["old_sha256"], entry["path"])
                if entry["new_sha256"] is None:
                    target.unlink()
                else:
                    os.replace(_internal(root, patch_id, f"transactions/{transaction_id}/stage/{index:05d}.bin"), target)
            previous["status"] = "restored"
            previous["last_transaction_id"] = transaction_id
            _json_write(directory / "state.json", previous)
            _clear_transaction(root, patch_id, journal)
            _clear_backups(root, patch_id, previous)
        except Exception as exc:
            committed = _load_state(directory, patch_id)
            if committed and committed.get("last_transaction_id") != transaction_id:
                try:
                    _rollback(root, patch_id, journal)
                except Exception as recovery_exc:
                    raise DeploymentError(f"撤回失败，自动回滚未完成：{recovery_exc}。备份与事务日志已保留。") from exc
            if isinstance(exc, DeploymentError):
                raise
            raise DeploymentError(f"撤回失败：{exc}") from exc
        result = status(root, patch_id)
        result["action"] = "restored"
        return result
