"""Build or reuse the scoped localization-PAK proxy; never start a game."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import struct
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / 'tool' / 'data' / 'runtime'
MINHOOK = ROOT / 'vendor' / 'minhook'
MINHOOK_SOURCES = ('src/buffer.c', 'src/hook.c', 'src/trampoline.c', 'src/hde/hde64.c')
SOURCES = ('native/runtime_loader.cpp', 'native/entry_gate.S', 'native/dinput8.def',
           'scripts/build_runtime_loader.py',
           *(path.relative_to(ROOT).as_posix() for path in sorted(MINHOOK.rglob('*')) if path.is_file()))
SCHEMA = 'ac8-pak-runtime-loader-v1'


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def check_pe(data: bytes) -> None:
    if len(data) < 256 or data[:2] != b'MZ':
        raise ValueError('运行时加载器不是有效的 Windows DLL。')
    pe = struct.unpack_from('<I', data, 0x3c)[0]
    if pe + 24 > len(data) or data[pe:pe + 4] != b'PE\0\0':
        raise ValueError('运行时加载器 PE 文件头无效。')
    machine = struct.unpack_from('<H', data, pe + 4)[0]
    flags = struct.unpack_from('<H', data, pe + 22)[0]
    if machine != 0x8664 or not flags & 0x2000:
        raise ValueError('运行时加载器必须是 AMD64 DLL。')


def verify(output: Path = OUTPUT) -> dict:
    """Reject missing or stale sources/artifacts without needing a compiler."""
    output = Path(output)
    manifest = json.loads((output / 'manifest.json').read_text(encoding='utf-8'))
    if not isinstance(manifest, dict):
        raise ValueError('运行时加载器构建记录不是有效对象。')
    expected_sources = {name: sha256(ROOT / name) for name in SOURCES}
    if manifest.get('schema') != SCHEMA or manifest.get('source_sha256') != expected_sources:
        raise ValueError('运行时加载器源码已变更，需要重新编译。')
    dll = output / 'dinput8.dll'
    data = dll.read_bytes()
    expected = {'bytes': len(data), 'sha256': hashlib.sha256(data).hexdigest()}
    if manifest.get('artifacts') != {'dinput8.dll': expected}:
        raise ValueError('运行时加载器与构建记录不一致。')
    license_name = 'licenses/MinHook_LICENSE.txt'
    license_data = (output / license_name).read_bytes()
    expected_license = {'bytes': len(license_data),
                        'sha256': hashlib.sha256(license_data).hexdigest()}
    if (license_data != (MINHOOK / 'LICENSE.txt').read_bytes()
            or manifest.get('licenses') != {license_name: expected_license}):
        raise ValueError('MinHook 许可文本缺失或与构建记录不一致。')
    check_pe(data)
    return manifest


def build(output: Path = OUTPUT, compiler: str | None = None) -> dict:
    output = Path(output).resolve()
    try:
        manifest = verify(output)
    except (OSError, ValueError, TypeError):
        pass
    else:
        print('运行时加载器已是最新，复用现有 DLL。')
        return manifest
    selected = compiler or shutil.which('g++.exe') or shutil.which('g++')
    if not selected and Path(r'C:\mingw64\bin\g++.exe').is_file():
        selected = r'C:\mingw64\bin\g++.exe'
    if not selected:
        raise ValueError('运行时加载器缺失或过期，请使用 MinGW-w64 重新编译。')
    output.mkdir(parents=True, exist_ok=True)
    environment = dict(os.environ)
    environment['PATH'] = str(Path(selected).resolve().parent) + os.pathsep + environment.get('PATH', '')
    cc = Path(selected).resolve().with_name('gcc.exe')
    if not cc.is_file():
        found = shutil.which('gcc.exe') or shutil.which('gcc')
        if not found:
            raise ValueError('找不到 MinGW-w64 的 gcc，无法编译 MinHook。')
        cc = Path(found)
    with tempfile.TemporaryDirectory(prefix='.runtime-loader-', dir=output) as temporary:
        destination = Path(temporary) / 'dinput8.dll'
        objects = []
        for name in MINHOOK_SOURCES:
            source = MINHOOK / name
            object_file = Path(temporary) / (source.stem + '.o')
            subprocess.run([
                str(cc), '-std=c11', '-O2', '-DNDEBUG', '-DUNICODE', '-D_UNICODE',
                '-D_WIN32_WINNT=0x0601', '-I', str(MINHOOK / 'include'),
                '-c', str(source), '-o', str(object_file),
            ], cwd=ROOT, env=environment, check=True)
            objects.append(str(object_file))
        subprocess.run([
            str(selected), '-std=c++17', '-O2', '-DNDEBUG', '-DUNICODE', '-D_UNICODE',
            '-D_WIN32_WINNT=0x0601', '-shared', '-static', '-static-libgcc', '-static-libstdc++',
            '-ffunction-sections', '-fdata-sections', '-Wl,--gc-sections',
            '-Wl,--no-insert-timestamp', '-Wl,--nxcompat', '-Wl,--dynamicbase',
            '-I', str(MINHOOK / 'include'),
            str(ROOT / 'native' / 'runtime_loader.cpp'), str(ROOT / 'native' / 'entry_gate.S'),
            str(ROOT / 'native' / 'dinput8.def'), *objects, '-luser32', '-o', str(destination),
        ], cwd=ROOT, env=environment, check=True)
        data = destination.read_bytes()
        check_pe(data)
        manifest = {
            'schema': SCHEMA,
            'source_sha256': {name: sha256(ROOT / name) for name in SOURCES},
            'artifacts': {'dinput8.dll': {
                'bytes': len(data), 'sha256': hashlib.sha256(data).hexdigest(),
            }},
            'exe_modified_on_disk': False,
            'route': 'scoped-original-path-localization-pak',
            'pak_filename': 'pakchunk0-Windows_998_P.pak',
            'game_started': False,
            'runtime_verified': False,
        }
        license_data = (MINHOOK / 'LICENSE.txt').read_bytes()
        license_name = 'licenses/MinHook_LICENSE.txt'
        license_path = output / license_name
        license_path.parent.mkdir(parents=True, exist_ok=True)
        license_path.write_bytes(license_data)
        manifest['licenses'] = {license_name: {
            'bytes': len(license_data), 'sha256': hashlib.sha256(license_data).hexdigest(),
        }}
        os.replace(destination, output / 'dinput8.dll')
        (output / 'manifest.json').write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    verify(output)
    print('运行时加载器构建完成。')
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser(description='构建或复用不修改磁盘 EXE 的运行时加载器。')
    parser.add_argument('--output', type=Path, default=OUTPUT)
    parser.add_argument('--compiler')
    parser.add_argument('--verify', action='store_true', help='只验证，不编译')
    arguments = parser.parse_args()
    try:
        if arguments.verify:
            verify(arguments.output)
            print('运行时加载器源码及 DLL 一致。')
        else:
            build(arguments.output, arguments.compiler)
    except (OSError, ValueError, TypeError, subprocess.CalledProcessError) as error:
        parser.exit(1, '运行时加载器未完成：' + str(error) + '\n')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
