"""Standalone Windows entry point, including a window-free verification mode."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys


def main():
    parser = argparse.ArgumentParser(description='AC8翻译工具')
    parser.add_argument('--verify', action='store_true', help='仅检查捆绑资源，不创建窗口或访问游戏。')
    parser.add_argument('--report', type=Path, help='将自检结果写入指定 UTF-8 JSON。')
    args = parser.parse_args()
    if getattr(sys, 'frozen', False):
        os.environ['AC8_TOOL_HOME'] = str(Path(sys.executable).resolve().parent)
    try:
        if args.verify:
            from verify_startup import verify_bundle
            result = verify_bundle()
            if args.report:
                from ac8tool import _json_write
                _json_write(args.report.resolve(), result)
            if sys.stdout is not None:
                print(json.dumps(result, ensure_ascii=False))
            return 0
        import gui
        gui.Window().root.mainloop()
        return 0
    except Exception as error:
        if args.verify:
            if args.report:
                from ac8tool import _json_write
                _json_write(args.report.resolve(), dict(ok=False, error=str(error)))
            if sys.stderr is not None:
                print(str(error), file=sys.stderr)
        else:
            from tkinter import messagebox
            messagebox.showerror('工具未能启动', str(error))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
