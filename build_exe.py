"""Build a self-contained Windows EXE using only the tool's runtime files."""
from __future__ import annotations

import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parent
TOOL = ROOT / 'tool'
WORK = ROOT / 'workspace' / 'exe-build'
DEPS = ROOT / 'workspace' / 'exe-build-deps'
NAME = 'AC8翻译工具'
OUTPUT = ROOT / (NAME + '.exe')


def main():
    if os.name != 'nt':
        raise RuntimeError('EXE 必须在 Windows 上构建。')
    sys.path.insert(0, str(DEPS))
    import PyInstaller
    version = (ROOT / 'VERSION').read_text(encoding='utf-8').strip()
    assets = WORK / 'assets'
    assets.mkdir(parents=True, exist_ok=True)
    files = {path.relative_to(TOOL).as_posix(): path.read_bytes()
             for path in sorted(TOOL.rglob('*'))
             if path.is_file() and '__pycache__' not in path.parts and path.suffix != '.pyc'}
    files['VERSION'] = (version + '\n').encode('ascii')
    python_root = Path(sys.base_prefix)
    licenses = {
        'licenses/Python.txt': python_root / 'LICENSE.txt',
        'licenses/Tk.txt': python_root / 'tcl' / 'tk8.6' / 'license.terms',
    }
    distribution = importlib.metadata.distribution('pyinstaller')
    for item in distribution.files or []:
        if item.name == 'COPYING.txt':
            licenses['licenses/PyInstaller.txt'] = Path(distribution.locate_file(item))
            break
    for name, path in licenses.items():
        if not path.is_file():
            raise FileNotFoundError('缺少运行组件的许可文本：' + str(path))
        files[name] = path.read_bytes()
    files['第三方组件说明.txt'] = (
        '本程序捆绑 Python、Tcl/Tk 及 PyInstaller 启动器。\n'
        '许可文本在内置 licenses 目录中；工具源码和游戏文本资料适用各自权利。\n'
        '捆绑内容仅来自 tool 运行目录，不包含当前用户译文、私有密钥、研究目录或 Git 历史。\n'
    ).encode('utf-8')
    manifest = {name: hashlib.sha256(raw).hexdigest() for name, raw in files.items()}
    files['bundle-manifest.json'] = (json.dumps(manifest, ensure_ascii=False, indent=2) + '\n').encode('utf-8')
    for name, raw in files.items():
        destination = assets / name
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(raw)
    components = tuple(int(value) for value in version.split('.'))
    components += (0,) * (4 - len(components))
    version_file = WORK / 'version-info.txt'
    version_file.write_text(
        'VSVersionInfo(ffi=FixedFileInfo(filevers=' + repr(components) + ', prodvers=' + repr(components)
        + ', mask=0x3f, flags=0x0, OS=0x40004, fileType=0x1, subtype=0x0, date=(0, 0)), '
        'kids=[StringFileInfo([StringTable("080404B0", ['
        'StringStruct("FileDescription", "AC8翻译工具"),'
        'StringStruct("ProductName", "AC8翻译工具"),'
        'StringStruct("FileVersion", ' + repr(version) + '),'
        'StringStruct("ProductVersion", ' + repr(version) + '),'
        'StringStruct("OriginalFilename", ' + repr(NAME + '.exe') + ')])]),'
        'VarFileInfo([VarStruct("Translation", [2052, 1200])])])', encoding='utf-8')
    environment = dict(os.environ)
    environment['PYTHONPATH'] = str(DEPS) + os.pathsep + environment.get('PYTHONPATH', '')
    environment['PYTHONDONTWRITEBYTECODE'] = '1'
    command = [sys.executable, '-B', '-X', 'utf8', '-m', 'PyInstaller',
               '--onefile', '--windowed', '--noupx', '--noconfirm', '--name', NAME,
               '--paths', str(TOOL), '--hidden-import', 'deployment_api',
               '--distpath', str(WORK / 'dist'), '--workpath', str(WORK / 'work'),
               '--specpath', str(WORK / 'spec'), '--version-file', str(version_file),
               '--log-level', 'INFO']
    # Use an explicit file list, so stale build assets are never collected.
    for name in files:
        destination = str(Path(name).parent)
        command.extend(('--add-data', str(assets / name) + ':' + destination))
    command.append(str(TOOL / 'standalone.py'))
    subprocess.run(command, cwd=ROOT, env=environment, check=True)
    built = WORK / 'dist' / (NAME + '.exe')
    if OUTPUT.is_symlink():
        raise ValueError('输出 EXE 不应是符号链接。')
    shutil.copyfile(built, OUTPUT)
    report = dict(version=version, output=str(OUTPUT), exe_bytes=OUTPUT.stat().st_size,
                  exe_sha256=hashlib.sha256(OUTPUT.read_bytes()).hexdigest(),
                  pyinstaller=PyInstaller.__version__, python=sys.version.split()[0],
                  standalone=True, external_python_required=False,
                  game_modified=False, personal_translation_bundled=False,
                  bundled_files=list(files), source_manifest=manifest)
    (ROOT / 'exe-package-report.json').write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(json.dumps({key: value for key, value in report.items()
                      if key not in ('bundled_files', 'source_manifest')}, ensure_ascii=False))


if __name__ == '__main__':
    main()
