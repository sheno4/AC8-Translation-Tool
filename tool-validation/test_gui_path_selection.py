"""Synthetic path-selection checks without a Tk window or real game access.

Dialog calls, messages, operation dispatch, and rollback are mocked. Window
construction is bypassed so Steam discovery and native UI are never invoked.
"""
from __future__ import annotations

from pathlib import Path
import sys
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'tool'))
import gui


class FakeVariable:
    def __init__(self, value=''):
        self.value = value

    def get(self):
        return self.value

    def set(self, value):
        self.value = value


class GuiPathSelectionTests(unittest.TestCase):
    def setUp(self):
        for target in ('gui.tk.Tk', 'gui.find_game_directories'):
            guard = patch(target, side_effect=AssertionError(
                'Synthetic checks must not create UI or discover real games'))
            guard.start()
            self.addCleanup(guard.stop)
        self.window = object.__new__(gui.Window)
        self.window.game = FakeVariable()
        self.window.catalog = FakeVariable()
        self.window.export_language = FakeVariable('日英中')
        self.window.compare_language = FakeVariable('日英中')
        self.window.import_variables = {
            key: FakeVariable(value)
            for key, value in gui.DEFAULT_IMPORT_OPTIONS.items()
        }
        self.window.busy = False
        self.window.run = Mock()
        info = patch.object(gui.messagebox, 'showinfo')
        self.info = info.start()
        self.addCleanup(info.stop)
        self.game = str(Path(__file__).resolve().parent / 'synthetic-game')
        self.source = str(Path(__file__).resolve().parent / 'synthetic-translations' / 'my.json')

    def test_first_translation_open_does_not_choose_an_initial_directory(self):
        self.assertEqual(gui.DEFAULT_GAME, '')
        self.assertEqual(gui.DEFAULT_CATALOG, '')
        with patch.object(gui.filedialog, 'askopenfilename', return_value='') as dialog:
            self.window.browse_catalog()
        self.assertNotIn('initialdir', dialog.call_args.kwargs)
        self.assertNotIn('initialfile', dialog.call_args.kwargs)
        self.assertEqual(self.window.catalog.get(), '')

    def test_first_export_uses_default_name_without_forcing_a_directory(self):
        self.window.game.set(self.game)
        with patch.object(gui.filedialog, 'asksaveasfilename', return_value='') as dialog:
            self.window.export()
        self.assertNotIn('initialdir', dialog.call_args.kwargs)
        self.assertEqual(dialog.call_args.kwargs['initialfile'], 'AC8翻译.json')
        self.assertEqual(self.window.catalog.get(), '')
        self.window.run.assert_not_called()

    def test_selected_translation_controls_both_open_and_export_locations(self):
        self.window.game.set(self.game)
        self.window.catalog.set(self.source)
        expected_directory = str(Path(self.source).parent)
        with patch.object(gui.filedialog, 'askopenfilename', return_value='') as dialog:
            self.window.browse_catalog()
        self.assertEqual(dialog.call_args.kwargs['initialdir'], expected_directory)
        self.assertEqual(self.window.catalog.get(), self.source)
        with patch.object(gui.filedialog, 'asksaveasfilename', return_value='') as dialog:
            self.window.export()
        self.assertEqual(dialog.call_args.kwargs['initialdir'], expected_directory)
        self.assertEqual(dialog.call_args.kwargs['initialfile'], Path(self.source).name)
        self.assertEqual(self.window.catalog.get(), self.source)
        self.window.run.assert_not_called()

    def test_new_export_selection_becomes_current_translation(self):
        self.window.game.set(self.game)
        with patch.object(gui.filedialog, 'asksaveasfilename', return_value=self.source):
            self.window.export()
        self.assertEqual(self.window.catalog.get(), self.source)
        self.window.run.assert_called_once()
        self.assertEqual(self.window.run.call_args.args[0], '导出文本')
        self.assertTrue(callable(self.window.run.call_args.args[1]))

    def test_first_game_picker_is_unseeded_and_cancel_preserves_selection(self):
        with patch.object(gui.filedialog, 'askdirectory', return_value='') as dialog:
            self.window.browse_game()
        self.assertNotIn('initialdir', dialog.call_args.kwargs)
        self.assertEqual(self.window.game.get(), '')
        self.window.game.set(self.game)
        with patch.object(gui.filedialog, 'askdirectory', return_value='') as dialog:
            self.window.browse_game()
        self.assertEqual(dialog.call_args.kwargs['initialdir'], self.game)
        self.assertEqual(self.window.game.get(), self.game)

    def test_missing_game_blocks_only_install_and_restore(self):
        self.window.catalog.set(self.source)
        for operation in ('install', 'restore'):
            with self.subTest(operation=operation):
                self.window.run.reset_mock()
                self.info.reset_mock()
                self.window.game.set('   ')
                with patch.object(gui.filedialog, 'asksaveasfilename') as dialog:
                    getattr(self.window, operation)()
                self.window.run.assert_not_called()
                dialog.assert_not_called()
                self.info.assert_called_once()
                self.assertEqual(self.info.call_args.args[0], '游戏目录')

    def test_offline_operations_allow_an_empty_game_directory(self):
        self.window.catalog.set(self.source)
        for operation in ('export', 'check', 'compare'):
            with self.subTest(operation=operation):
                self.window.run.reset_mock()
                self.info.reset_mock()
                self.window.game.set('')
                with patch.object(gui.filedialog, 'asksaveasfilename', return_value=self.source):
                    getattr(self.window, operation)()
                self.window.run.assert_called_once()
                self.info.assert_not_called()

    def test_missing_translation_blocks_check_compare_and_import(self):
        self.window.game.set(self.game)
        self.window.catalog.set('  ')
        for operation in ('check', 'compare', 'install'):
            with self.subTest(operation=operation):
                self.window.run.reset_mock()
                self.info.reset_mock()
                getattr(self.window, operation)()
                self.window.run.assert_not_called()
                self.info.assert_called_once()
                self.assertEqual(self.info.call_args.args[0], '翻译文件')

    def test_restore_does_not_require_a_translation_file(self):
        self.window.game.set(self.game)
        self.window.catalog.set('')
        with patch.object(gui.deployment, 'restore', return_value={'state': 'restored'}) as restore:
            self.window.restore()
            self.window.run.assert_called_once()
            name, callback = self.window.run.call_args.args
            self.assertEqual(name, '撤回修改')
            self.assertEqual(callback(), {'state': 'restored'})
        restore.assert_called_once_with(self.game, gui.PATCH_ID)
        self.info.assert_not_called()

if __name__ == '__main__':
    unittest.main(verbosity=2)
