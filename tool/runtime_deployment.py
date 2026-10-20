"""Transactional PAK deployment, with EXE writes only to undo owned v1 edits.

Importing this module never changes a file or starts a game/process. Old v1
originals survive migration in an archive until the new installation is removed.
"""
from __future__ import annotations

import copy
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import uuid

import deployment as legacy

DeploymentError = legacy.DeploymentError
EXE_PATH = legacy.EXE_PATH
DATA_PATHS = legacy.DATA_PATHS
DEFAULT_PATCH_ID = legacy.DEFAULT_PATCH_ID
LOADER_PATH = "Game/Binaries/Win64/dinput8.dll"
PAK_PATH = "Game/Content/Paks/pakchunk0-Windows_998_P.pak"
MANAGED_PATHS = (LOADER_PATH, PAK_PATH)
SCHEMA_VERSION = 2
MODE = "pak_loader"


def _directory(root, patch_id):
    return legacy._safe(root, f"{legacy.BACKUP_DIRECTORY}/{patch_id}")


def _internal(root, patch_id, relative):
    return legacy._internal(root, patch_id, relative)


def _digest(value, optional=False):
    return ((optional and value is None)
            or isinstance(value, str) and bool(re.fullmatch("[0-9a-f]{64}", value)))


def _allowed_directories():
    return {str(parent) for path in (*MANAGED_PATHS, *DATA_PATHS)
            for parent in PurePosixPath(path).parents if str(parent) != "."}


def _owned_state(value, patch_id):
    if value.get("schema_version") == 1:
        legacy._owned(value, patch_id)
        return
    if (value.get("owner") != legacy.OWNER or value.get("schema_version") != SCHEMA_VERSION
            or value.get("patch_id") != patch_id or value.get("mode") != MODE):
        raise DeploymentError("补丁备份目录不属于此 PAK 补丁。")
    files = value.get("files")
    if (not isinstance(files, list) or any(not isinstance(entry, dict) for entry in files)
            or [entry.get("path") for entry in files] != list(MANAGED_PATHS)):
        raise DeploymentError("PAK 补丁状态管理的文件与本工具不符。")
    if value.get("status") not in {"installed", "restored"}:
        raise DeploymentError("PAK 补丁状态标识无效。")
    created = value.get("created_directories", [])
    if not isinstance(created, list) or any(path not in _allowed_directories() for path in created):
        raise DeploymentError("PAK 状态包含未知目录。")
    for entry in files:
        # Both names must originally have been absent. Third-party content is
        # refused, even if its bytes happen to equal this tool's loader/PAK.
        if (entry.get("original_sha256") is not None or entry.get("backup") is not None
                or not _digest(entry.get("installed_sha256"))):
            raise DeploymentError("PAK 状态不能接管其他加载器或同名资源包。")
    if not _digest(value.get("original_exe_sha256")) or not _digest(value.get("fingerprint")):
        raise DeploymentError("PAK 状态的 EXE 观察值或指纹无效。")
    if not re.fullmatch("[0-9a-f]{32}", value.get("last_transaction_id", "")):
        raise DeploymentError("PAK 事务标识无效。")
    archive = value.get("legacy_archive")
    if archive is not None:
        if not isinstance(archive, dict) or archive.get("schema_version") != 1:
            raise DeploymentError("旧版迁移归档无效。")
        legacy._owned(archive, patch_id)


def _load_state(directory, patch_id):
    path = legacy._safe(directory, "state.json")
    if not path.exists():
        return None
    state = legacy._json_read(path)
    _owned_state(state, patch_id)
    return state


def _owned_journal(value, patch_id):
    if (value.get("owner") != legacy.OWNER or value.get("schema_version") != SCHEMA_VERSION
            or value.get("patch_id") != patch_id or value.get("mode") != MODE
            or value.get("action") not in {"install", "restore"}
            or value.get("phase") not in {"preparing", "prepared"}
            or not re.fullmatch("[0-9a-f]{32}", value.get("transaction_id", ""))):
        raise DeploymentError("PAK 补丁事务不属于此工具。")
    previous = value.get("previous_state")
    if previous is not None:
        if not isinstance(previous, dict):
            raise DeploymentError("事务中的原状态无效。")
        _owned_state(previous, patch_id)
    files = value.get("files")
    if not isinstance(files, list) or any(not isinstance(entry, dict) for entry in files):
        raise DeploymentError("PAK 事务文件清单无效。")
    paths = [entry.get("path") for entry in files]
    migration = paths in ([*DATA_PATHS, *MANAGED_PATHS], [EXE_PATH, *DATA_PATHS, *MANAGED_PATHS])
    if paths != list(MANAGED_PATHS) and not migration:
        raise DeploymentError("PAK 事务含未知文件。")
    if migration and (value["action"] != "install" or not previous or previous["schema_version"] != 1):
        raise DeploymentError("只有旧版迁移可以恢复 EXE 或旧资源。")
    if value["action"] == "restore" and (not previous or previous["schema_version"] != SCHEMA_VERSION):
        raise DeploymentError("PAK 撤回事务缺少有效原状态。")
    if not _digest(value.get("observed_exe_sha256"), True):
        raise DeploymentError("事务 EXE 观察值无效。")
    for index, entry in enumerate(files):
        if (not _digest(entry.get("old_sha256"), True) or not _digest(entry.get("new_sha256"), True)
                or entry.get("rollback") != f"transactions/{value['transaction_id']}/rollback/{index:05d}.bin"):
            raise DeploymentError("PAK 事务回滚路径或校验值无效。")
    created = value.get("created_directories", [])
    if not isinstance(created, list) or any(path not in _allowed_directories() for path in created):
        raise DeploymentError("PAK 事务包含未知目录。")


def _verify_files(root, state, patch_id):
    for entry in state["files"]:
        legacy._assert_current(legacy._safe(root, entry["path"]), entry["installed_sha256"], entry["path"])
        if entry["backup"]:
            legacy._assert_current(_internal(root, patch_id, entry["backup"]), entry["original_sha256"], "原文件备份 " + entry["path"])


def _verify_previous(root, patch_id, previous, observed_exe):
    if previous is None:
        return
    if previous["schema_version"] == SCHEMA_VERSION:
        _verify_files(root, previous, patch_id)
        return
    # An audited game update may have replaced v1's EXE. The two installed DAT
    # files and all original backups must still match their ownership record.
    for entry in previous["files"]:
        if entry["path"] != EXE_PATH:
            legacy._assert_current(legacy._safe(root, entry["path"]), entry["installed_sha256"], entry["path"])
        if entry["backup"]:
            legacy._assert_current(_internal(root, patch_id, entry["backup"]), entry["original_sha256"], "旧版原文件备份 " + entry["path"])
    legacy._assert_current(legacy._safe(root, EXE_PATH), observed_exe, "游戏 EXE")


def _clear_transaction(root, patch_id, journal, *, rolled_back=False):
    for index, _ in enumerate(journal["files"]):
        for folder in ("rollback", "stage"):
            path = _internal(root, patch_id, f"transactions/{journal['transaction_id']}/{folder}/{index:05d}.bin")
            if path.is_file():
                path.unlink()
    for relative in (f"transactions/{journal['transaction_id']}/rollback", f"transactions/{journal['transaction_id']}/stage",
                     f"transactions/{journal['transaction_id']}", "transactions"):
        legacy._empty_dir(_internal(root, patch_id, relative))
    if rolled_back:
        for relative in sorted(journal.get("created_directories", []), key=len, reverse=True):
            legacy._empty_dir(legacy._safe(root, relative))
    path = _internal(root, patch_id, "journal.json")
    if path.is_file():
        path.unlink()


def _cleanup_restored(root, patch_id, state):
    # These archived originals are never written back by v2 withdrawal.
    archive = state.get("legacy_archive")
    if archive:
        for entry in archive["files"]:
            if entry["backup"]:
                path = _internal(root, patch_id, entry["backup"])
                if path.is_file():
                    path.unlink()
        legacy._empty_dir(_internal(root, patch_id, "backups"))
    path = _internal(root, patch_id, "state.json")
    if path.is_file():
        path.unlink()
    for relative in sorted(state.get("created_directories", []), key=len, reverse=True):
        legacy._empty_dir(legacy._safe(root, relative))


def _rollback(root, patch_id, journal):
    _owned_journal(journal, patch_id)
    if _load_state(_directory(root, patch_id), patch_id) != journal["previous_state"]:
        raise DeploymentError("事务恢复前状态文件被其他程序修改，已停止。")
    for entry in journal["files"]:
        actual = legacy._current(legacy._safe(root, entry["path"]))
        if actual == entry["old_sha256"]:
            continue
        if journal["phase"] != "prepared" or actual != entry["new_sha256"]:
            raise DeploymentError("无法安全恢复未完成操作，文件出现未知内容：" + entry["path"])
        if entry["old_sha256"] is not None:
            legacy._assert_current(_internal(root, patch_id, entry["rollback"]), entry["old_sha256"], "事务回滚备份")
    for index, entry in enumerate(journal["files"]):
        target = legacy._safe(root, entry["path"])
        if legacy._current(target) == entry["old_sha256"]:
            continue
        if entry["old_sha256"] is None:
            legacy._assert_current(target, entry["new_sha256"], entry["path"])
            target.unlink()
        else:
            stage = _internal(root, patch_id, f"transactions/{journal['transaction_id']}/stage/{index:05d}.bin")
            shutil.copyfile(_internal(root, patch_id, entry["rollback"]), stage)
            legacy._assert_current(stage, entry["old_sha256"], "待回滚原文件")
            legacy._assert_current(target, entry["new_sha256"], entry["path"])
            os.replace(stage, target)
    _clear_transaction(root, patch_id, journal, rolled_back=True)


def _recover(root, patch_id, directory):
    journal_path = legacy._safe(directory, "journal.json")
    if journal_path.exists():
        journal = legacy._json_read(journal_path)
        if journal.get("schema_version") == 1:
            legacy._recover(root, patch_id, directory)
            return
        _owned_journal(journal, patch_id)
        state = _load_state(directory, patch_id)
        if state and state.get("last_transaction_id") == journal["transaction_id"]:
            _clear_transaction(root, patch_id, journal)
            if state["status"] == "restored":
                _cleanup_restored(root, patch_id, state)
        else:
            _rollback(root, patch_id, journal)
    else:
        state = _load_state(directory, patch_id)
        if state and state["status"] == "restored":
            if state["schema_version"] == 1:
                legacy._recover(root, patch_id, directory)
            else:
                _cleanup_restored(root, patch_id, state)


def status(game_root, patch_id=DEFAULT_PATCH_ID):
    """Read-only state; an EXE update never blocks v2 removal."""
    root, patch_id = legacy._root(game_root), legacy._patch_id(patch_id)
    directory = _directory(root, patch_id)
    if legacy._safe(directory, "journal.json").exists():
        return dict(state="incomplete", installed=False, patch_id=patch_id,
                    message="有未完成操作。下次导入或撤回会先恢复。")
    state = _load_state(directory, patch_id)
    if state and state["schema_version"] == 1:
        return dict(legacy.status(root, patch_id), mode="legacy", exe_modified=True)
    if not state or state["status"] == "restored":
        return dict(state="not_installed", installed=False, patch_id=patch_id, mode=MODE,
                    exe_modified=False, message="当前未导入此汉化。")
    checks = []
    for entry in state["files"]:
        actual = legacy._current(legacy._safe(root, entry["path"]))
        checks.append(dict(path=entry["path"], valid=actual == entry["installed_sha256"],
                           backup_valid=True, actual_sha256=actual, installed_sha256=entry["installed_sha256"]))
    valid = all(entry["valid"] for entry in checks)
    actual_exe = legacy._current(legacy._safe(root, EXE_PATH))
    return dict(state="installed" if valid else "modified", installed=True, patch_id=patch_id,
                patch_version=state["patch_version"], mode=MODE, original_exe_sha256=state["original_exe_sha256"],
                patched_exe_sha256=actual_exe, current_exe_sha256=actual_exe, exe_modified=False,
                fingerprint=state["fingerprint"], files=checks,
                message="汉化已导入，游戏 EXE 未由本工具修改。" if valid else "已安装文件发生变化，已停止更新/撤回。")


def inspect_exe_source(game_root, patch_id=DEFAULT_PATCH_ID):
    root, patch_id = legacy._root(game_root), legacy._patch_id(patch_id)
    directory = _directory(root, patch_id)
    if legacy._safe(directory, "journal.json").exists():
        raise DeploymentError("有未完成操作，请先导入或撤回以恢复事务。")
    state = _load_state(directory, patch_id)
    if state and state["schema_version"] == 1:
        return dict(legacy.inspect_exe_source(root, patch_id), mode="legacy", exe_modified=True)
    if state and state["status"] == "installed":
        _verify_files(root, state, patch_id)
    current = legacy._safe(root, EXE_PATH)
    actual = legacy._current(current)
    if actual is None:
        raise DeploymentError("找不到游戏 AceCombat8.exe。")
    installed = bool(state and state["status"] == "installed")
    return dict(path=str(current), sha256=actual, current_sha256=actual, installed=installed,
                rebase_exe=installed and actual != state["original_exe_sha256"], mode=MODE, exe_modified=False)


def _run_transaction(root, patch_id, directory, journal, stage_values, next_state):
    _owned_journal(journal, patch_id)
    legacy._json_write(directory / "journal.json", journal)
    state_committed = False
    try:
        for folder in (f"transactions/{journal['transaction_id']}/rollback", f"transactions/{journal['transaction_id']}/stage"):
            _internal(root, patch_id, folder).mkdir(parents=True, exist_ok=True)
        for index, entry in enumerate(journal["files"]):
            target = legacy._safe(root, entry["path"])
            legacy._assert_current(target, entry["old_sha256"], entry["path"])
            if entry["old_sha256"] is not None:
                rollback = _internal(root, patch_id, entry["rollback"])
                shutil.copyfile(target, rollback)
                legacy._assert_current(rollback, entry["old_sha256"], "事务备份")
            value = stage_values[entry["path"]]
            if value is not None:
                stage = _internal(root, patch_id, f"transactions/{journal['transaction_id']}/stage/{index:05d}.bin")
                legacy._copy_payload(value, stage)
                legacy._assert_current(stage, entry["new_sha256"], "待部署文件")
        journal["phase"] = "prepared"
        legacy._json_write(directory / "journal.json", journal)
        legacy._assert_game_stopped(root)
        for entry in journal["files"]:
            legacy._assert_current(legacy._safe(root, entry["path"]), entry["old_sha256"], entry["path"])
        if _load_state(directory, patch_id) != journal["previous_state"]:
            raise DeploymentError("兼容检查后补丁状态被其他程序修改，已停止。")
        _verify_previous(root, patch_id, journal["previous_state"], journal["observed_exe_sha256"])
        if journal["action"] == "install":
            legacy._assert_current(legacy._safe(root, EXE_PATH), journal["observed_exe_sha256"], "游戏 EXE")
        # Install the loader last; remove it first on withdrawal.
        order = list(range(len(journal["files"])))
        if journal["action"] == "install":
            order.sort(key=lambda index: journal["files"][index]["path"] == LOADER_PATH)
        for index in order:
            entry = journal["files"][index]
            target = legacy._safe(root, entry["path"])
            legacy._assert_current(target, entry["old_sha256"], entry["path"])
            if entry["new_sha256"] is None:
                if target.exists():
                    target.unlink()
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                os.replace(_internal(root, patch_id, f"transactions/{journal['transaction_id']}/stage/{index:05d}.bin"), target)
            legacy._assert_current(target, entry["new_sha256"], entry["path"])
        legacy._json_write(directory / "state.json", next_state)
        state_committed = True
        _clear_transaction(root, patch_id, journal)
        if next_state["status"] == "restored":
            _cleanup_restored(root, patch_id, next_state)
    except Exception as error:
        committed = _load_state(directory, patch_id)
        if not state_committed and (not committed or committed.get("last_transaction_id") != journal["transaction_id"]):
            try:
                _rollback(root, patch_id, journal)
            except Exception as recovery_error:
                raise DeploymentError(f"操作失败，自动回滚未完成：{recovery_error}。备份与事务日志已保留。") from error
        if isinstance(error, DeploymentError):
            raise
        raise DeploymentError(f"PAK 补丁操作失败：{error}") from error


def _config(value):
    config = legacy._json_read(Path(value)) if isinstance(value, (str, os.PathLike)) else dict(value)
    patch_id = legacy._patch_id(config.get("patch_id", DEFAULT_PATCH_ID))
    hashes = config.get("supported_exe_sha256", [])
    if isinstance(hashes, str):
        hashes = [hashes]
    if not isinstance(hashes, (list, tuple)) or not hashes or any(not _digest(item) for item in hashes):
        raise DeploymentError("PAK 配置没有绑定本次兼容检查的 EXE 校验值。")
    rebase = config.get("rebase_exe", False)
    if type(rebase) is not bool:
        raise DeploymentError("更新 EXE 的基准标识必须为布尔值。")
    return dict(patch_id=patch_id, patch_version=str(config.get("patch_version", "1")),
                supported_exe_sha256=sorted(set(hashes)), rebase_exe=rebase)


def install(game_root, pak, hook_config, loader: bytes):
    root = legacy._root(game_root)
    config = _config(hook_config)
    patch_id = config["patch_id"]
    if not isinstance(loader, bytes) or not loader:
        raise DeploymentError("PAK 加载器为空或格式无效。")
    if isinstance(pak, (bytes, bytearray, memoryview)):
        if not pak:
            raise DeploymentError("翻译 PAK 为空。")
        pak_hash = hashlib.sha256(pak).hexdigest()
    else:
        pak = Path(pak)
        if not pak.is_file() or pak.stat().st_size == 0:
            raise DeploymentError("翻译 PAK 不存在或为空。")
        pak_hash = legacy._sha(pak)
    legacy._assert_game_stopped(root)
    with legacy._lock(root, patch_id) as directory:
        _recover(root, patch_id, directory)
        previous = _load_state(directory, patch_id)
        source = inspect_exe_source(root, patch_id)
        if source["sha256"] not in config["supported_exe_sha256"] or config["rebase_exe"] != source["rebase_exe"]:
            raise DeploymentError("游戏 EXE 在兼容检查后发生变化，请重新运行导入。")
        if previous is None and any(item.name != "operation.lock" for item in directory.iterdir()):
            raise DeploymentError("备份目录存在未知内容，已停止。")
        if not previous or previous["schema_version"] == 1:
            for path in MANAGED_PATHS:
                if legacy._current(legacy._safe(root, path)) is not None:
                    raise DeploymentError("已有其他加载器或同名资源包，拒绝覆盖：" + path)
        transaction_id = uuid.uuid4().hex
        files = []
        stage_values = {LOADER_PATH: loader, PAK_PATH: pak}
        migration = previous and previous["schema_version"] == 1
        if migration:
            for entry in previous["files"]:
                if entry["path"] == EXE_PATH and source["rebase_exe"]:
                    continue
                files.append(dict(path=entry["path"], old_sha256=entry["installed_sha256"], new_sha256=entry["original_sha256"]))
                stage_values[entry["path"]] = _internal(root, patch_id, entry["backup"]) if entry["backup"] else None
        originals = []
        hashes = {LOADER_PATH: hashlib.sha256(loader).hexdigest(), PAK_PATH: pak_hash}
        for index, path in enumerate(MANAGED_PATHS):
            old_hash = (previous["files"][index]["installed_sha256"]
                        if previous and previous["schema_version"] == SCHEMA_VERSION else None)
            legacy._assert_current(legacy._safe(root, path), old_hash, path)
            files.append(dict(path=path, old_sha256=old_hash, new_sha256=hashes[path]))
            originals.append(dict(path=path, original_sha256=None, backup=None, installed_sha256=hashes[path]))
        for index, entry in enumerate(files):
            entry["rollback"] = f"transactions/{transaction_id}/rollback/{index:05d}.bin"
        created = sorted(set((previous or {}).get("created_directories", [])) | set(legacy._created_directories(root, list(MANAGED_PATHS))))
        fingerprint = hashlib.sha256(json.dumps([MODE, config["patch_version"], source["sha256"],
                                                [entry["installed_sha256"] for entry in originals]],
                                               sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        state = dict(owner=legacy.OWNER, schema_version=SCHEMA_VERSION, patch_id=patch_id, mode=MODE,
                     patch_version=config["patch_version"], status="installed", fingerprint=fingerprint,
                     original_exe_sha256=source["sha256"], last_transaction_id=transaction_id,
                     created_directories=created, files=originals)
        if migration:
            state["legacy_archive"] = copy.deepcopy(previous)
        elif previous and previous.get("legacy_archive"):
            state["legacy_archive"] = copy.deepcopy(previous["legacy_archive"])
        _owned_state(state, patch_id)
        journal = dict(owner=legacy.OWNER, schema_version=SCHEMA_VERSION, patch_id=patch_id, mode=MODE,
                       transaction_id=transaction_id, action="install", phase="preparing",
                       previous_state=copy.deepcopy(previous), files=files,
                       observed_exe_sha256=source["current_sha256"], created_directories=created)
        _run_transaction(root, patch_id, directory, journal, stage_values, state)
        return dict(status(root, patch_id), action="updated" if previous else "installed")


def restore(game_root, patch_id=DEFAULT_PATCH_ID):
    root, patch_id = legacy._root(game_root), legacy._patch_id(patch_id)
    directory = _directory(root, patch_id)
    if not directory.exists():
        return dict(status(root, patch_id), action="unchanged")
    legacy._assert_game_stopped(root)
    delegate_legacy = False
    with legacy._lock(root, patch_id) as directory:
        _recover(root, patch_id, directory)
        previous = _load_state(directory, patch_id)
        if not previous:
            return dict(status(root, patch_id), action="unchanged")
        if previous["schema_version"] == 1:
            delegate_legacy = True
        else:
            _verify_files(root, previous, patch_id)
            transaction_id = uuid.uuid4().hex
            files = [dict(path=entry["path"], old_sha256=entry["installed_sha256"], new_sha256=None,
                          rollback=f"transactions/{transaction_id}/rollback/{index:05d}.bin")
                     for index, entry in enumerate(previous["files"])]
            state = copy.deepcopy(previous)
            state.update(status="restored", last_transaction_id=transaction_id)
            journal = dict(owner=legacy.OWNER, schema_version=SCHEMA_VERSION, patch_id=patch_id, mode=MODE,
                           transaction_id=transaction_id, action="restore", phase="preparing", previous_state=previous,
                           files=files, observed_exe_sha256=legacy._current(legacy._safe(root, EXE_PATH)), created_directories=[])
            _run_transaction(root, patch_id, directory, journal, dict.fromkeys(MANAGED_PATHS), state)
            return dict(status(root, patch_id), action="restored")
    if delegate_legacy:
        return legacy.restore(root, patch_id)
