"""Read-only bundle verification; creates no GUI and never accesses the game."""
import hashlib
import json
import os
from pathlib import Path
import sys

def verify_bundle():
    root = Path(__file__).resolve().parent
    manifest = json.loads((root / 'bundle-manifest.json').read_text(encoding='utf-8'))
    for name, expected in manifest.items():
        path = (root / name).resolve()
        path.relative_to(root)
        actual = hashlib.sha256(path.read_bytes()).hexdigest()
        if actual != expected:
            raise ValueError('Bundled file checksum mismatch: ' + name)

    import ac8tool
    import ac8categories
    import ac8importoptions
    import ac8paths
    import deployment
    import deployment_api
    import runtime_deployment
    import ac8pak
    import gui
    import locres_codec
    ac8pak.load_runtime_loader()

    frozen = bool(getattr(sys, 'frozen', False))
    if frozen or os.environ.get('AC8_TOOL_HOME'):
        project = (Path(sys.executable).resolve().parent if frozen else
                   Path(os.environ['AC8_TOOL_HOME']).resolve())
        if (gui.PROJECT_ROOT != project or gui.DEFAULT_WORK != project / 'workspace'
                or gui.DEFAULT_CATALOG != '' or gui.DEFAULT_GAME != ''
                or (not frozen and root.parent != project / 'runtime')):
            raise ValueError('Project-relative runtime/default paths mismatch')

    rules = root / 'ac8categories.py'
    if hashlib.sha256(rules.read_bytes()).hexdigest() != manifest['ac8categories.py']:
        raise ValueError('Classification source fingerprint mismatch')
    counts = ac8tool._counts(ac8tool._entries('game'))
    if counts['game_unique'] != 46366 or counts['game_slots'] != 56141:
        raise ValueError('Bundled Japanese/English catalog counts mismatch')
    _, _, version, _ = ac8tool._sources()
    hook = json.loads((root / 'data' / 'hook-config.json').read_text(encoding='utf-8'))
    if hook['supported_exe_sha256'] != version['supported_exe_sha256']:
        raise ValueError('Bundled version/hook mismatch')
    if ac8importoptions.DEFAULT_IMPORT_OPTIONS != dict(hud_labels=False, equipment_short=False):
        raise ValueError('Default display protection options mismatch')
    return dict(ok=True, bundled_files=len(manifest), counts=counts, frozen=frozen,
                tool_version=gui.TOOL_VERSION, window_title=gui.APP_TITLE,
                external_python_required=not frozen,
                deployment_mode='pak_loader', disk_exe_patch_required=False,
                tkinter_imported=True, window_created=False,
                game_accessed=False, official_chinese_read=False,
                default_export_language=gui.DEFAULT_EXPORT_LANGUAGE,
                default_import_options=dict(ac8importoptions.DEFAULT_IMPORT_OPTIONS),
                project_root=str(gui.PROJECT_ROOT),
                default_translation=str(gui.DEFAULT_CATALOG),
                game_autodetection_on_window=True,
                default_workspace=str(gui.DEFAULT_WORK))


if __name__ == '__main__':
    print(json.dumps(verify_bundle(), ensure_ascii=False))
