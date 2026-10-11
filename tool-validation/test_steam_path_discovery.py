"""Synthetic Steam path discovery checks; no actual game/Steam files accessed."""
from __future__ import annotations

from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'tool'))
import ac8paths


def quote(value):
    return '"' + str(value).replace('\\', '\\\\').replace('"', '\\"') + '"'


class SteamPathDiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='ac8-steam-paths-')
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)

    def config(self, root, filename, text):
        destination = root / 'steamapps' / filename
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(text, encoding='utf-8-sig')

    def installed(self, root, name='ACE COMBAT 8', *, appid=ac8paths.APP_ID):
        self.config(root, f'appmanifest_{ac8paths.APP_ID}.acf',
                    f'"AppState" {{ "appid" "{appid}" "installdir" {quote(name)} }}')
        game = root / 'steamapps' / 'common' / name
        executable = game / ac8paths.GAME_EXECUTABLE
        executable.parent.mkdir(parents=True, exist_ok=True)
        executable.write_bytes(b'synthetic placeholder; not a game executable')
        return game.resolve()

    def test_modern_multiple_libraries_and_duplicate_roots(self):
        steam = self.base / 'Steam 空格'
        extra = self.base / '库 二'
        first = self.installed(steam)
        second = self.installed(extra, '皇牌 空战 8')
        self.config(steam, 'libraryfolders.vdf', f'''
            // Modern Steam format
            "libraryfolders" {{
                "0" {{ "path" {quote(steam)} "apps" {{ "2288340" "1" }} }}
                "1" {{ "path" {quote(extra)} }}
                "2" {{ "path" {quote(extra)} }}
                "description" "ignored metadata"
            }}''')
        with patch.object(Path, 'read_bytes', side_effect=AssertionError('EXE read forbidden')):
            self.assertEqual(ac8paths.find_game_directories([steam, steam / '.', extra]),
                             [first, second])

    def test_old_libraries_and_missing_installations(self):
        steam = self.base / 'Steam'
        extra = self.base / '旧版库'
        missing = self.base / 'Missing'
        expected = self.installed(extra)
        self.config(steam, 'libraryfolders.vdf', f'''
            LibraryFolders {{
                0 {quote(steam)} /* Inline old-format comment */
                1 {quote(extra)}
                2 {quote(missing)}
                TimeNextStatsReport "12345"
            }}''')
        self.config(missing, f'appmanifest_{ac8paths.APP_ID}.acf',
                    '"AppState" { "appid" "2288340" "installdir" "Not Installed" }')
        self.assertEqual(ac8paths.find_game_directories(steam), [expected])

    def test_broken_library_config_still_finds_steam_root_install(self):
        steam = self.base / 'Steam'
        expected = self.installed(steam)
        self.config(steam, 'libraryfolders.vdf', '"libraryfolders" { "0" "unterminated')
        self.assertEqual(ac8paths.find_game_directories([steam]), [expected])

    def test_invalid_manifests_and_install_directory_escape(self):
        unsafe = ('../escaped', r'..\escaped', r'C:\escaped', r'\escaped',
                  r'\\server\share', '.', '', 'nested/../../escaped')
        for number, install in enumerate(unsafe):
            root = self.base / f'unsafe-{number}'
            self.config(root, f'appmanifest_{ac8paths.APP_ID}.acf',
                        f'"AppState" {{ "appid" "2288340" "installdir" {quote(install)} }}')
            self.assertEqual(ac8paths.find_game_directories([root]), [], install)
        wrong = self.base / 'Wrong App'
        self.installed(wrong, appid='1')
        broken = self.base / 'Broken Manifest'
        self.config(broken, f'appmanifest_{ac8paths.APP_ID}.acf', '"AppState" {')
        self.assertEqual(ac8paths.find_game_directories([wrong, broken, self.base / 'absent']), [])

    def test_unreadable_configs_are_skipped(self):
        steam = self.base / 'Steam'
        self.installed(steam)
        with patch.object(Path, 'open', side_effect=PermissionError('synthetic restriction')):
            self.assertEqual(ac8paths.find_game_directories([steam]), [])

    def test_vdf_braces_comments_and_escaped_characters(self):
        parsed = ac8paths._parse_vdf(
            r'"Object" { "path" "D:\\Steam 库\\common" "label" "say \"yes\" { } // text" }')
        self.assertEqual(parsed['object']['path'], 'D:\\Steam 库\\common')
        self.assertEqual(parsed['object']['label'], 'say "yes" { } // text')
        for malformed in ('"key"', '}', '"key" {', '/* open', '"key" }'):
            with self.assertRaises(ValueError):
                ac8paths._parse_vdf(malformed)

    def test_default_roots_use_discovery_without_hardcoded_personal_path(self):
        steam = self.base / 'Steam'
        expected = self.installed(steam)
        with patch.object(ac8paths, '_steam_roots', return_value=[steam]) as discovery:
            self.assertEqual(ac8paths.find_game_directories(), [expected])
        discovery.assert_called_once_with()


if __name__ == '__main__':
    unittest.main()
