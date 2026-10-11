"""Bind the export engine to the version-checked game deployment module."""
import json
from pathlib import Path
import deployment

DATA = Path(__file__).resolve().parent / 'data'


def validate_patched_executable(game_dir, actual_sha256, original_sha256):
    hook = json.loads((DATA / 'hook-config.json').read_text(encoding='utf-8'))
    if actual_sha256 != hook['patched_exe_sha256'] or original_sha256 != hook['supported_exe_sha256']:
        return False
    state = deployment.status(game_dir, hook['patch_id'])
    return (state.get('state') == 'installed'
            and state.get('original_exe_sha256') == original_sha256
            and state.get('patched_exe_sha256') == actual_sha256)


def restore(game_dir, workspace=None):
    hook = json.loads((DATA / 'hook-config.json').read_text(encoding='utf-8'))
    return dict(deployment.restore(game_dir, hook['patch_id']), ok=True)
