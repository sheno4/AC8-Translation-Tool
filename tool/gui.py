"""Desktop entry for the offline AC8 text export and replacement tool."""
from __future__ import annotations
import json
import os
from pathlib import Path
import queue
import sys
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
VERSION_FILE = ROOT / 'VERSION'
if not VERSION_FILE.is_file():
    VERSION_FILE = ROOT.parent / 'VERSION'
TOOL_VERSION = VERSION_FILE.read_text(encoding='utf-8-sig').strip()
APP_TITLE = 'AC8翻译工具 v' + TOOL_VERSION
import ac8tool
import ac8compare
import deployment
from ac8paths import find_game_directories
from ac8importoptions import DEFAULT_IMPORT_OPTIONS, GROUP_DEFINITIONS

FROZEN = bool(getattr(sys, 'frozen', False))
DEFAULT_GAME = ''
PROJECT_ROOT = (Path(sys.executable).resolve().parent if FROZEN else
                Path(os.environ.get('AC8_TOOL_HOME', str(ROOT.parent))).resolve())
DEFAULT_WORK = PROJECT_ROOT / 'workspace'
DEFAULT_CATALOG = ''
DEFAULT_EXPORT_NAME = 'AC8翻译.json'
DEFAULT_EXPORT_LANGUAGE = '日英中'
PATCH_ID = 'ac8-zh-text-25201480-v1'


def confirm_import_result(installed):
    """Accept success only after all managed files and backups were verified."""
    required = {deployment.EXE_PATH, *deployment.DATA_PATHS}
    checks = installed.get('files') if isinstance(installed, dict) else None
    if (not isinstance(installed, dict) or installed.get('state') != 'installed'
            or installed.get('installed') is not True or not isinstance(checks, list)
            or len(checks) != len(required)
            or any(not isinstance(item, dict) or item.get('valid') is not True
                   or item.get('backup_valid') is not True for item in checks)
            or {item.get('path') for item in checks} != required):
        detail = installed.get('message', '') if isinstance(installed, dict) else ''
        raise ValueError('未能确认全部游戏文件及备份通过验证，导入尚未完成。' + detail)


class Window:
    def __init__(self):
        self.root = tk.Tk()
        self.root.title(APP_TITLE)
        self.root.geometry('940x680')
        self.root.minsize(900, 620)
        self.messages = queue.Queue()
        self.busy = False
        game_directories = find_game_directories()
        self.game = tk.StringVar(value=str(game_directories[0]) if game_directories else DEFAULT_GAME)
        self.catalog = tk.StringVar(value=DEFAULT_CATALOG)
        self.export_language = tk.StringVar(value=DEFAULT_EXPORT_LANGUAGE)
        self.compare_language = tk.StringVar(value='日英中')
        self.import_variables = {key: tk.BooleanVar(value=value)
                                 for key, value in DEFAULT_IMPORT_OPTIONS.items()}
        style = ttk.Style(self.root)
        if 'vista' in style.theme_names():
            style.theme_use('vista')
        style.configure('TLabel', font=('Microsoft YaHei UI', 10))
        style.configure('TButton', font=('Microsoft YaHei UI', 10), padding=8)
        outer = ttk.Frame(self.root, padding=20)
        outer.pack(fill='both', expand=True)
        ttk.Label(outer, text=APP_TITLE, font=('Microsoft YaHei UI', 16)).pack(anchor='w')
        ttk.Label(outer, text='ACE COMBAT 8 · 导出原文 → 填写译文 → 校验并导入', foreground='#666666').pack(anchor='w', pady=(8, 16))
        self.path_row(outer, '游戏目录', self.game, self.browse_game)
        self.path_row(outer, '翻译文件', self.catalog, self.browse_catalog)
        languages = ttk.Frame(outer)
        languages.pack(fill='x', pady=(8, 4))
        ttk.Label(languages, text='导出原文').pack(side='left')
        ttk.Combobox(languages, textvariable=self.export_language, values=('日英', '日英中'),
                     state='readonly', width=9).pack(side='left', padx=(8, 24))
        ttk.Label(languages, text='对照显示').pack(side='left')
        ttk.Combobox(languages, textvariable=self.compare_language, values=('日英', '日英中'),
                     state='readonly', width=9).pack(side='left', padx=(8, 12))
        ttk.Label(languages, text='“中”为原官中；你的译文会单独显示。', foreground='#666666').pack(side='left')
        row = ttk.Frame(outer)
        row.pack(fill='x', pady=(12, 6))
        self.buttons = []
        for label, action in [('导出文本', self.export), ('校验译文', self.check),
                              ('译文对照', self.compare), ('导入译文', self.install), ('撤回修改', self.restore)]:
            button = ttk.Button(row, text=label, command=action)
            button.pack(side='left', padx=(0, 8))
            self.buttons.append(button)
            if label == '导入译文':
                self.import_button = button
        options = ttk.Frame(outer)
        options.pack(fill='x', pady=(4, 10))
        ttk.Label(options, text='每次按当前文件替换；null 使用各位置原官中。').pack(side='left')
        ttk.Button(options, text='使用说明', command=self.show_help).pack(side='right')
        protection = ttk.LabelFrame(outer, text='导入时是否替换这些文字', padding=(10, 6))
        protection.pack(fill='x', pady=(8, 6))
        choices = ttk.Frame(protection)
        choices.pack(fill='x')
        for item in GROUP_DEFINITIONS:
            button = ttk.Checkbutton(choices, text=item['label'],
                                     variable=self.import_variables[item['id']])
            button.pack(side='left', padx=(0, 18))
            self.buttons.append(button)
        ttk.Label(protection, text='默认不勾选：恢复原标牌和简称。装备简称可能由 HUD、科技树、机库共用。').pack(anchor='w', pady=(5, 0))
        ttk.Label(protection, text='剧情、无线电、教程和装备说明照常翻译；JSON 中已有译文保留。').pack(anchor='w')
        self.log = tk.Text(outer, wrap='word', height=12, font=('Microsoft YaHei UI', 10), state='disabled')
        self.log.pack(fill='both', expand=True, pady=(14, 6))
        self.progress = ttk.Progressbar(outer, mode='indeterminate')
        self.progress.pack(fill='x')
        self.write(('已自动找到游戏目录，可通过“选择”更换。' if game_directories else
                    '导入或撤回前请选择游戏目录。')
                   + '\n请选择已有翻译 JSON，或先导出文本；翻译文件默认留空。\n只填写最后一列的译文，保存后校验并导入。导出和对照默认日英中。\n导入会备份游戏文件；“撤回修改”可恢复导入前的原文件。')
        self.root.after(100, self.poll)
        self.root.protocol('WM_DELETE_WINDOW', self.close)

    def path_row(self, parent, label, variable, browse):
        row = ttk.Frame(parent)
        row.pack(fill='x', pady=4)
        ttk.Label(row, text=label, width=10).pack(side='left')
        ttk.Entry(row, textvariable=variable).pack(side='left', fill='x', expand=True)
        ttk.Button(row, text='选择', command=browse).pack(side='left', padx=(8, 0))

    def browse_game(self):
        options = {'initialdir': self.game.get()} if self.game.get().strip() else {}
        path = filedialog.askdirectory(title='选择 ACE COMBAT 8 游戏目录', **options)
        if path:
            self.game.set(path)

    def browse_catalog(self):
        path = filedialog.askopenfilename(title='选择翻译 JSON', filetypes=[('翻译文件', '*.json')],
                                         **self.translation_dialog_options())
        if path:
            self.catalog.set(path)

    def translation_dialog_options(self, save=False):
        source = self.catalog.get().strip()
        options = {}
        if source:
            options['initialdir'] = str(Path(source).expanduser().resolve().parent)
        if save:
            options['initialfile'] = Path(source).name if source else DEFAULT_EXPORT_NAME
        return options

    def paths_selected(self, translation=True, require_game=True):
        if require_game and not self.game.get().strip():
            messagebox.showinfo('游戏目录', '未找到游戏，请先选择游戏目录。')
            return False
        if translation and not self.catalog.get().strip():
            messagebox.showinfo('翻译文件', '请选择已有翻译 JSON，或先导出文本。')
            return False
        return True

    def write(self, text):
        self.log.configure(state='normal')
        self.log.insert('end', text + '\n\n')
        self.log.see('end')
        self.log.configure(state='disabled')

    def run(self, name, function):
        if self.busy:
            return
        self.busy = True
        for button in self.buttons:
            button.configure(state='disabled')
        self.progress.start(12)
        self.root.title(APP_TITLE)
        if name == '导入译文':
            self.import_button.configure(text='正在导入…')
            self.root.title(APP_TITLE + ' · 正在导入，尚未完成')
            self.write('正在导入，尚未完成，请勿关闭。')
        else:
            self.write(name + '…')
        def worker():
            try:
                result = function()
                self.messages.put(('ok', name, result))
            except Exception as exc:
                self.messages.put(('error', name, str(exc)))
        threading.Thread(target=worker, daemon=True).start()

    def poll(self):
        try:
            kind, name, result = self.messages.get_nowait()
            if kind == 'progress':
                self.write(name + '：\n' + str(result))
                self.root.after(100, self.poll)
                return
            self.busy = False
            self.progress.stop()
            for button in self.buttons:
                button.configure(state='normal')
            if name == '导入译文':
                self.import_button.configure(text='导入译文')
                self.root.title(APP_TITLE + ' · ' + ('导入未完成' if kind == 'error' else '导入完成'))
            if kind == 'error':
                self.write(name + '未完成：\n' + result)
                messagebox.showerror('导入未完成' if name == '导入译文' else '操作未完成', result, parent=self.root)
            elif name == '校验译文':
                summary = ('校验通过。' if result.get('ok') else '校验未通过。')
                summary += (f"\n已填写 {result.get('completed_unique', 0)} 条；"
                            f"未填写 {result.get('missing_unique', 0)} 条；"
                            f"格式错误 {result.get('format_issue_count', 0)} 条。")
                if result.get('partial_mode'):
                    summary += '\n已启用英文补全，未填写条目将使用英文。'
                elif result.get('missing_policy') == 'keep':
                    summary += (f"\n未填写的 {result.get('missing_game_slots', 0):,} 个游戏位置保留原官中，"
                                '不会因未填而阻止导入。')
                if result.get('missing_short_ids'):
                    summary += '\n未填写序号（前 30 条）：' + ', '.join(map(str, result['missing_short_ids']))
                if 'selection_counts' in result:
                    summary += '\n翻译范围：' + json.dumps(result['selection_counts'], ensure_ascii=False)
                for item in result.get('import_protection', {}).get('groups', []):
                    summary += f"\n{item['label']}：{item['recognized_slots']:,} 个已识别位置，{item['protected_slots']:,} 个保留原文。"
                if result.get('game_check', {}).get('errors'):
                    summary += '\n' + '\n'.join(result['game_check']['errors'])
                if result.get('missing_policy_errors'):
                    summary += '\n' + '\n'.join(result['missing_policy_errors'])
                if result.get('format_issues'):
                    displayed = [{key: value for key, value in issue.items()
                                  if key in ('short_id', 'format_source', 'problems', 'variant_issues')}
                                 for issue in result['format_issues'][:10]]
                    summary += '\n前 10 条格式错误：\n' + json.dumps(displayed, ensure_ascii=False, indent=2)
                summary += '\n完整报告：' + result.get('report_path', '')
                self.write(summary)
            elif name == '导入译文':
                self.write('导入完成，可以启动游戏了。')
            elif name == '译文对照':
                self.write('对照文件已生成。\n' + json.dumps(result.get('counts', {}), ensure_ascii=False, indent=2)
                           + '\n页面：' + result['html_path'] + '\n数据：' + result['json_path'])
                try:
                    os.startfile(result['html_path'])
                except OSError as exc:
                    self.write('页面未能自动打开，请从上方路径打开：' + str(exc))
            else:
                self.write(name + '完成。\n' + json.dumps(result, ensure_ascii=False, indent=2))
        except queue.Empty:
            pass
        self.root.after(100, self.poll)

    def export(self):
        if not self.paths_selected(translation=False, require_game=False):
            return
        selected = filedialog.asksaveasfilename(title='导出待翻译文本（' + self.export_language.get() + '）',
            defaultextension='.json', filetypes=[('翻译文件', '*.json')],
            **self.translation_dialog_options(save=True))
        if not selected:
            return
        self.catalog.set(selected)
        game = self.game.get()
        include_official = self.export_language.get() == '日英中'
        def work():
            return ac8tool.export_translations(game, DEFAULT_WORK, scope='game', output_path=selected,
                include_official=include_official, include_classification=False, compact=True)
        self.run('导出文本', work)

    def check(self):
        if not self.paths_selected(require_game=False):
            return
        game, source = self.game.get(), self.catalog.get()
        options = self.import_options()
        self.run('校验译文', lambda: ac8tool.check_translations(game, source, missing_policy='keep',
            report_path=DEFAULT_WORK / 'check-report.json', import_options=options))

    def import_options(self):
        return {key: variable.get() for key, variable in self.import_variables.items()}

    def compare(self):
        if not self.paths_selected(require_game=False):
            return
        game, source = self.game.get(), self.catalog.get()
        include_official = self.compare_language.get() == '日英中'
        options = self.import_options()
        self.run('译文对照', lambda: ac8compare.export_comparison(
            game, source, DEFAULT_WORK / 'comparisons', include_official=include_official,
            import_options=options))

    def install(self):
        if not self.paths_selected():
            return
        game, source = self.game.get(), self.catalog.get()
        options = self.import_options()
        def work():
            result = ac8tool.stage_translations(game, source, DEFAULT_WORK, missing_policy='keep',
                                               import_options=options)
            counts = result.get('selection_counts', {})
            policy = result.get('selection_build', {}).get('excluded_policy', 'en')
            protected = result.get('import_protection', {}).get('protected_slots', 0)
            other_excluded = counts.get('excluded_slots', 0) - protected
            self.messages.put(('progress', '正在导入，尚未完成',
                f"参与翻译 {counts.get('selected_slots', 0):,} 个位置，"
                f"显示保护 {protected:,} 个位置，保留各位置原官中。"
                + (f" 旧选择另排除 {other_excluded:,} 个位置，"
                   + ('保留官中。' if policy == 'official' else '使用英文。') if other_excluded else '')
                + f" 未填的 {result.get('missing_game_slots', 0):,} 个位置使用原官中。"))
            payload_root = Path(result['payload_dir'])
            candidates = {name: list(payload_root.rglob(name)) for name in ('CP_M.dat', 'ON_M.dat')}
            if any(len(paths) != 1 for paths in candidates.values()):
                raise ValueError('生成的中文资源不完整，游戏尚未修改。')
            payloads = {'Game/Content/Localization/GameData/CP_Z.dat': candidates['CP_M.dat'][0],
                        'Game/Content/Localization/GameData/ON_Z.dat': candidates['ON_M.dat'][0]}
            hook_config = result.get('game_check', {}).get('hook_config')
            if not isinstance(hook_config, dict):
                raise ValueError('未获得当前游戏的兼容补丁，游戏尚未修改。')
            installed = deployment.install(game, payloads, hook_config)
            confirm_import_result(installed)
            return {'result': '中文资源已导入；撤回可恢复导入前文件', 'translation_counts': result.get('counts'), 'deployment': installed}
        self.run('导入译文', work)

    def restore(self):
        if not self.paths_selected(translation=False):
            return
        game = self.game.get()
        self.run('撤回修改', lambda: deployment.restore(game, PATCH_ID))

    def show_help(self):
        os.startfile(str(ROOT / '使用说明.txt'))

    def close(self):
        if self.busy:
            messagebox.showinfo('正在处理', '请等待当前导出、导入或撤回操作完成后关闭。')
            return
        self.root.destroy()


if __name__ == '__main__':
    Window().root.mainloop()
