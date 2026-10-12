"""Bind compatibility inspection to the transactional game deployment module."""
import json
from pathlib import Path
import deployment

DATA = Path(__file__).resolve().parent / 'data'


def compatibility_report(game_dir):
    """Inspect only the code and text resources this patch actually depends on."""
    base = json.loads((DATA / 'hook-config.json').read_text(encoding='utf-8'))
    observations = {}
    try:
        from ac8exehook import resolve_hook_config
        from ac8resourcecompat import check_resources
        source = deployment.inspect_exe_source(game_dir, base['patch_id'])
        hook = resolve_hook_config(source['path'], base_config=base)
        if hook['supported_exe_sha256'] != source['sha256']:
            raise deployment.DeploymentError('游戏 EXE 在兼容检查期间发生变化，请重试。')
        hook['rebase_exe'] = source['rebase_exe']
        resources = check_resources(game_dir, source['path'])
        observations.update(exe_sha256=source['sha256'],
                            exe_status='new_game_exe' if source['rebase_exe'] else
                            'verified_tool_patch' if source['installed'] else 'compatible_original',
                            resources=resources)
        if not resources['ok']:
            errors = []
            for item in resources.get('errors', []):
                if isinstance(item, dict):
                    name = item.get('path', '').rsplit('/', 1)[-1]
                    errors.append((name + '：' if name else '') + item.get('reason', '无法确认兼容。'))
                else:
                    errors.append(str(item))
            return dict(ok=False, mode='structural_compatibility', game_dir=str(game_dir),
                        errors=errors or ['游戏文本结构无法确认兼容。'],
                        observations=observations, official_chinese_read=False)
        return dict(ok=True, mode='structural_compatibility', game_dir=str(game_dir),
                    errors=[], observations=observations, hook_config=hook,
                    official_chinese_read=False)
    except (OSError, ValueError, deployment.DeploymentError) as error:
        return dict(ok=False, mode='structural_compatibility', game_dir=str(game_dir),
                    errors=[str(error)], observations=observations, official_chinese_read=False)


def validate_patched_executable(game_dir, actual_sha256, original_sha256):
    hook = json.loads((DATA / 'hook-config.json').read_text(encoding='utf-8'))
    state = deployment.status(game_dir, hook['patch_id'])
    return (state.get('state') == 'installed'
            and state.get('original_exe_sha256') == original_sha256
            and state.get('patched_exe_sha256') == actual_sha256)


def restore(game_dir, workspace=None):
    hook = json.loads((DATA / 'hook-config.json').read_text(encoding='utf-8'))
    return dict(deployment.restore(game_dir, hook['patch_id']), ok=True)
