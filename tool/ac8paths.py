"""Read-only discovery of AC8 installations registered with local Steam.

Discovery reads Steam registry paths and its small VDF/ACF configuration files.
It only checks whether the game executable exists; it does not read its contents,
inspect versions, scan drives, or change any Steam or game files.
"""
from __future__ import annotations

import os
from pathlib import Path, PureWindowsPath
from typing import Iterable


APP_ID = '2288340'
GAME_EXECUTABLE = Path('Game') / 'Binaries' / 'Win64' / 'AceCombat8.exe'
_MAX_CONFIG_CHARACTERS = 2 * 1024 * 1024


def _vdf_tokens(text: str):
    """Tokenize quoted/bare values, braces, and Valve-style comments."""
    index = 0
    while index < len(text):
        char = text[index]
        if char.isspace():
            index += 1
            continue
        if text.startswith('//', index):
            end = text.find('\n', index + 2)
            index = len(text) if end < 0 else end + 1
            continue
        if text.startswith('/*', index):
            end = text.find('*/', index + 2)
            if end < 0:
                raise ValueError('Unterminated VDF comment')
            index = end + 2
            continue
        if char in '{}':
            yield char, True
            index += 1
            continue
        if char == '"':
            value = []
            index += 1
            while index < len(text):
                char = text[index]
                if char == '"':
                    index += 1
                    yield ''.join(value), False
                    break
                if char == '\\' and index + 1 < len(text):
                    following = text[index + 1]
                    escapes = {'\\': '\\', '"': '"', 'n': '\n', 'r': '\r', 't': '\t'}
                    if following in escapes:
                        value.append(escapes[following])
                        index += 2
                        continue
                value.append(char)
                index += 1
            else:
                raise ValueError('Unterminated VDF string')
            continue
        start = index
        while index < len(text) and not text[index].isspace() and text[index] not in '{}"':
            if text.startswith('//', index) or text.startswith('/*', index):
                break
            index += 1
        if index == start:
            raise ValueError('Invalid VDF token')
        yield text[start:index], False


def _parse_vdf(text: str) -> dict:
    tokens = iter(_vdf_tokens(text))

    def parse_object(nested=False):
        result = {}
        for key, structural in tokens:
            if structural:
                if key == '}' and nested:
                    return result
                raise ValueError('Unexpected VDF brace')
            value = next(tokens, None)
            if value is None:
                raise ValueError('Missing VDF value')
            if value == ('{', True):
                result[key.casefold()] = parse_object(True)
            elif value[1]:
                raise ValueError('Missing VDF value before closing brace')
            else:
                result[key.casefold()] = value[0]
        if nested:
            raise ValueError('Unterminated VDF object')
        return result

    return parse_object()


def _read_vdf(path: Path) -> dict:
    try:
        with path.open('r', encoding='utf-8-sig') as stream:
            text = stream.read(_MAX_CONFIG_CHARACTERS + 1)
        if len(text) > _MAX_CONFIG_CHARACTERS:
            return {}
        return _parse_vdf(text)
    except (OSError, UnicodeError, ValueError, RecursionError):
        return {}


def _normal_path(value) -> Path | None:
    try:
        value = os.path.expandvars(os.fspath(value))
        if not value or '\0' in value:
            return None
        path = Path(value).expanduser()
        if not path.is_absolute():
            return None
        return path.resolve()
    except (TypeError, OSError, ValueError, RuntimeError):
        return None


def _unique_paths(values: Iterable) -> list[Path]:
    paths = []
    seen = set()
    for value in values:
        path = _normal_path(value)
        if path is None:
            continue
        key = os.path.normcase(str(path))
        if key not in seen:
            seen.add(key)
            paths.append(path)
    return paths


def _steam_roots() -> list[Path]:
    candidates = []
    try:
        import winreg
    except ImportError:
        winreg = None
    if winreg is not None:
        views = {0, winreg.KEY_WOW64_32KEY, winreg.KEY_WOW64_64KEY}
        for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
            for view in views:
                try:
                    with winreg.OpenKey(hive, r'Software\Valve\Steam', 0,
                                        winreg.KEY_READ | view) as key:
                        for name in ('SteamPath', 'InstallPath'):
                            try:
                                value, kind = winreg.QueryValueEx(key, name)
                                if kind in (winreg.REG_SZ, winreg.REG_EXPAND_SZ):
                                    candidates.append(value)
                            except OSError:
                                pass
                except OSError:
                    pass
    for name in ('ProgramFiles(x86)', 'ProgramFiles', 'ProgramW6432'):
        directory = os.environ.get(name)
        if directory:
            candidates.append(Path(directory) / 'Steam')
    return _unique_paths(candidates)


def _steam_libraries(roots: Iterable[Path]) -> list[Path]:
    libraries = []
    for root in roots:
        libraries.append(root)
        document = _read_vdf(root / 'steamapps' / 'libraryfolders.vdf')
        folders = document.get('libraryfolders', {})
        if not isinstance(folders, dict):
            continue
        for number, folder in folders.items():
            if not number.isdecimal():
                continue
            value = folder.get('path') if isinstance(folder, dict) else folder
            if isinstance(value, str):
                libraries.append(value)
    return _unique_paths(libraries)


def _game_from_library(library: Path) -> Path | None:
    manifest = _read_vdf(library / 'steamapps' / f'appmanifest_{APP_ID}.acf')
    state = manifest.get('appstate', {})
    if not isinstance(state, dict) or state.get('appid') != APP_ID:
        return None
    install = state.get('installdir')
    if not isinstance(install, str) or not install.strip() or '\0' in install:
        return None
    # Explicitly reject traversal, rooted paths, and Windows drive/UNC paths.
    parts = PureWindowsPath(install)
    if parts.drive or parts.root or '..' in parts.parts:
        return None
    try:
        common = (library / 'steamapps' / 'common').resolve()
        game = (common / Path(install)).resolve()
        game.relative_to(common)
        if game == common:
            return None
        return game if (game / GAME_EXECUTABLE).is_file() else None
    except (OSError, ValueError, RuntimeError):
        return None


def find_game_directories(steam_paths=None) -> list[Path]:
    """Return unique installed AC8 directories without reading the game.

    ``steam_paths`` optionally supplies Steam roots for callers or synthetic
    tests. With no argument, roots come from local registry and Program Files.
    Missing, malformed, or inaccessible configurations are skipped silently.
    """
    if steam_paths is None:
        roots = _steam_roots()
    else:
        values = [steam_paths] if isinstance(steam_paths, (str, os.PathLike)) else steam_paths
        roots = _unique_paths(values)
    return _unique_paths(game for library in _steam_libraries(roots)
                         if (game := _game_from_library(library)) is not None)
