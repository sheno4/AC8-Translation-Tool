"""Process-guard regressions using synthetic Toolhelp data, never real processes."""
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path, PureWindowsPath
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'tool'))
import deployment as d


ROOT = PureWindowsPath(r'S:\SteamLibrary\steamapps\common\ACE COMBAT 8')


@dataclass(frozen=True)
class Process:
    pid: int
    name: str
    image: str | None
    openable: bool = True


class FakeCall:
    """Callable Win32 export that permits ctypes signature assignment."""

    def __init__(self, implementation):
        self.implementation = implementation

    def __call__(self, *args):
        return self.implementation(*args)


class FakeKernel:
    SNAPSHOT = 1
    HANDLE_OFFSET = 100000

    def __init__(self, processes):
        self.processes = list(processes)
        self.by_pid = {process.pid: process for process in self.processes}
        self.cursor = 0
        self.opened_pids = []
        self.queried_pids = []
        self.closed_handles = []
        self.CreateToolhelp32Snapshot = FakeCall(self.snapshot)
        self.Process32FirstW = FakeCall(self.first)
        self.Process32NextW = FakeCall(self.next)
        self.OpenProcess = FakeCall(self.open)
        self.QueryFullProcessImageNameW = FakeCall(self.query)
        self.CloseHandle = FakeCall(self.close)

    def snapshot(self, flags, pid):
        assert flags == 2 and pid == 0
        return self.SNAPSHOT

    def fill_entry(self, entry_pointer):
        if self.cursor >= len(self.processes):
            return False
        process = self.processes[self.cursor]
        entry = entry_pointer._obj
        entry.th32ProcessID = process.pid
        entry.szExeFile = process.name
        return True

    def first(self, snapshot, entry_pointer):
        assert snapshot == self.SNAPSHOT
        self.cursor = 0
        return self.fill_entry(entry_pointer)

    def next(self, snapshot, entry_pointer):
        assert snapshot == self.SNAPSHOT
        self.cursor += 1
        return self.fill_entry(entry_pointer)

    def open(self, access, inherit, pid):
        assert access == 0x1000 and not inherit
        self.opened_pids.append(pid)
        if not self.by_pid[pid].openable:
            return 0
        return self.HANDLE_OFFSET + pid

    def query(self, handle, flags, buffer, size_pointer):
        assert flags == 0
        pid = handle - self.HANDLE_OFFSET
        self.queried_pids.append(pid)
        image = self.by_pid[pid].image
        if image is None:
            return False
        buffer.value = image
        size_pointer._obj.value = len(image)
        return True

    def close(self, handle):
        self.closed_handles.append(handle)
        return True


@contextmanager
def fake_processes(processes):
    kernel = FakeKernel(processes)
    with patch.object(d.os, 'name', 'nt'), \
            patch.object(d.ctypes, 'WinDLL', return_value=kernel, create=True), \
            patch.object(d.ctypes, 'get_last_error', return_value=18, create=True):
        yield kernel


class GameProcessGuardTests(unittest.TestCase):
    def test_tool_parent_child_and_other_executables_inside_game_are_allowed(self):
        processes = [
            Process(14596, 'AC8翻译工具.exe', str(ROOT / 'AC8翻译工具.exe')),
            Process(4948, 'AC8翻译工具.exe', str(ROOT / 'AC8翻译工具.exe')),
            Process(3120, 'unrelated.exe', str(ROOT / 'Game/Binaries/Win64/unrelated.exe')),
            Process(3121, 'AC8翻译工具.exe', None, openable=False),
        ]
        with fake_processes(processes) as kernel:
            d._assert_game_stopped(ROOT)
        self.assertEqual(kernel.opened_pids, [])
        self.assertEqual(kernel.queried_pids, [])
        self.assertEqual(kernel.closed_handles, [kernel.SNAPSHOT])

    def test_game_and_launcher_in_target_installation_are_still_blocked(self):
        cases = [
            Process(1200, 'ACECOMBAT8.EXE',
                    's:/SteamLibrary/steamapps/common/ace combat 8/Game/Binaries/Win64/AceCombat8.exe'),
            Process(1201, 'start_protected_game.exe', str(ROOT / 'start_protected_game.exe')),
            Process(1202, 'AceCombat8.exe', r'\\?' + '\\' + str(ROOT / 'Game/Binaries/Win64/AceCombat8.exe')),
        ]
        for process in cases:
            with self.subTest(name=process.name, image=process.image):
                with fake_processes([process]) as kernel:
                    with self.assertRaises(d.DeploymentError) as error:
                        d._assert_game_stopped(ROOT)
                self.assertIn(f'{process.name} (PID {process.pid})', str(error.exception))
                self.assertEqual(kernel.opened_pids, [process.pid])
                self.assertEqual(kernel.queried_pids, [process.pid])
                self.assertEqual(kernel.closed_handles,
                                 [kernel.HANDLE_OFFSET + process.pid, kernel.SNAPSHOT])

    def test_other_installations_and_similar_directory_names_are_allowed(self):
        processes = [
            Process(2200, 'AceCombat8.exe',
                    r'D:\SteamLibrary\steamapps\common\ACE COMBAT 8\Game\Binaries\Win64\AceCombat8.exe'),
            Process(2201, 'AceCombat8.exe',
                    r'S:\SteamLibrary\steamapps\common\ACE COMBAT 8-copy\Game\Binaries\Win64\AceCombat8.exe'),
            Process(2202, 'start_protected_game.exe',
                    r'S:\SteamLibrary\steamapps\common\ACE COMBAT 80\start_protected_game.exe'),
        ]
        with fake_processes(processes) as kernel:
            d._assert_game_stopped(ROOT)
        self.assertEqual(kernel.opened_pids, [process.pid for process in processes])
        self.assertEqual(kernel.queried_pids, [process.pid for process in processes])
        self.assertEqual(kernel.closed_handles[-1], kernel.SNAPSHOT)

    def test_unreadable_paths_block_only_the_two_game_process_names(self):
        cases = [
            Process(3300, 'AceCombat8.exe', None, openable=False),
            Process(3301, 'START_PROTECTED_GAME.EXE', None),
        ]
        for target in cases:
            with self.subTest(name=target.name, openable=target.openable):
                tools = [
                    Process(14596, 'AC8翻译工具.exe', None, openable=False),
                    Process(4948, 'AC8翻译工具.exe', None),
                    Process(3400, 'other_launcher.exe', None),
                ]
                with fake_processes([*tools, target]) as kernel:
                    with self.assertRaises(d.DeploymentError) as error:
                        d._assert_game_stopped(ROOT)
                message = str(error.exception)
                self.assertIn(f'{target.name} (PID {target.pid})', message)
                self.assertNotIn('AC8翻译工具', message)
                self.assertNotIn('other_launcher', message)
                self.assertEqual(kernel.opened_pids, [target.pid])
                self.assertEqual(kernel.queried_pids, [target.pid] if target.openable else [])
                self.assertEqual(kernel.closed_handles[-1], kernel.SNAPSHOT)


if __name__ == '__main__':
    unittest.main(verbosity=2)
