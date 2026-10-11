"""Synthetic import-completion checks without a window, Steam, or game access."""
from __future__ import annotations

from pathlib import Path
import queue
import sys
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'tool'))
import gui


class FakeWidget:
    def __init__(self, **options):
        self.options = options
        self.running = False

    def configure(self, **options):
        self.options.update(options)

    def start(self, interval):
        self.running = True

    def stop(self):
        self.running = False


class FakeRoot:
    def __init__(self):
        self.window_title = ''

    def title(self, value):
        self.window_title = value

    def after(self, delay, callback):
        pass


class DeferredThread:
    def __init__(self, target, daemon):
        self.target = target

    def start(self):
        pass


class GuiImportCompletionTests(unittest.TestCase):
    def setUp(self):
        for target in ('gui.tk.Tk', 'gui.find_game_directories'):
            guard = patch(target, side_effect=AssertionError(
                'Synthetic checks must not open UI or discover real games'))
            guard.start()
            self.addCleanup(guard.stop)
        for attribute in ('showinfo', 'showerror'):
            dialog = patch.object(gui.messagebox, attribute)
            setattr(self, attribute, dialog.start())
            self.addCleanup(dialog.stop)
        thread = patch.object(gui.threading, 'Thread', side_effect=DeferredThread)
        thread.start()
        self.addCleanup(thread.stop)
        self.valid_result = dict(
            state='installed', installed=True, action='installed',
            files=[dict(path=path, valid=True, backup_valid=True)
                   for path in (gui.deployment.EXE_PATH, *gui.deployment.DATA_PATHS)])
        self.window = object.__new__(gui.Window)
        self.window.root = FakeRoot()
        self.window.messages = queue.Queue()
        self.window.busy = False
        self.window.import_button = FakeWidget(text='导入译文', state='normal')
        self.window.buttons = [self.window.import_button, FakeWidget(state='normal')]
        self.window.progress = FakeWidget(mode='indeterminate', value=0)
        self.window.write = Mock()

    def start_import(self):
        self.window.run('导入译文', lambda: None)

    def test_progress_keeps_import_pending(self):
        self.start_import()
        self.assertTrue(self.window.busy)
        self.assertEqual(self.window.root.window_title, 'AC8翻译工具 · 正在导入，尚未完成')
        self.assertEqual(self.window.import_button.options['text'], '正在导入…')
        self.assertTrue(all(button.options['state'] == 'disabled'
                            for button in self.window.buttons))
        self.assertTrue(self.window.progress.running)
        self.window.messages.put(('progress', '导入译文', '资源生成完成，游戏尚未导入。'))
        self.window.poll()
        self.assertTrue(self.window.busy)
        self.assertTrue(self.window.progress.running)
        self.assertEqual(self.window.import_button.options['text'], '正在导入…')
        self.showinfo.assert_not_called()
        self.showerror.assert_not_called()

    def test_final_result_shows_completion(self):
        self.start_import()
        self.window.messages.put(('ok', '导入译文', dict(
            deployment=self.valid_result, translation_file='translation.json',
            game_dir='synthetic-game')))
        self.window.poll()
        self.assertFalse(self.window.busy)
        self.assertFalse(self.window.progress.running)
        self.assertEqual(self.window.root.window_title, 'AC8翻译工具 · 导入完成')
        self.assertEqual(self.window.import_button.options['text'], '导入译文')
        self.assertTrue(all(button.options['state'] == 'normal'
                            for button in self.window.buttons))
        self.assertTrue(any('导入完成' in call.args[0]
                            for call in self.window.write.call_args_list))
        self.showinfo.assert_not_called()
        self.showerror.assert_not_called()

    def test_error_never_shows_completion(self):
        self.start_import()
        error = 'synthetic import failure'
        self.window.messages.put(('error', '导入译文', error))
        self.window.poll()
        self.assertFalse(self.window.busy)
        self.assertFalse(self.window.progress.running)
        self.assertEqual(self.window.root.window_title, 'AC8翻译工具 · 导入未完成')
        self.assertEqual(self.window.import_button.options['text'], '导入译文')
        self.showerror.assert_called_once_with('导入未完成', error, parent=self.window.root)
        self.showinfo.assert_not_called()

    def test_completion_requires_verified_installation(self):
        gui.confirm_import_result(self.valid_result)
        with self.assertRaisesRegex(ValueError, '未能确认全部游戏文件及备份通过验证'):
            gui.confirm_import_result({**self.valid_result, 'state': 'modified'})


if __name__ == '__main__':
    unittest.main(verbosity=2)
