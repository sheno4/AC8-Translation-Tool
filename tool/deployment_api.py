"""Bind compatibility inspection to the transactional game deployment module."""
import json
from pathlib import Path
import shutil
import tempfile
import runtime_deployment as deployment

DATA = Path(__file__).resolve().parent / 'data'


def compatibility_report(game_dir):
    """Inspect only the code and text resources this patch actually depends on."""
    base = json.loads((DATA / 'hook-config.json').read_text(encoding='utf-8'))
    observations = {}
    try:
        from ac8pak import resolve_loader_config
        from ac8resourcecompat import check_resources
        source = deployment.inspect_exe_source(game_dir, base['patch_id'])
        hook = resolve_loader_config(source['path'], base_config=base)
        if hook['supported_exe_sha256'] != source['sha256']:
            raise deployment.DeploymentError('游戏 EXE 在兼容检查期间发生变化，请重试。')
        hook['rebase_exe'] = source['rebase_exe']
        owned_archives = {}
        if source.get('installed') is True and source.get('mode') == 'pak_loader':
            current = deployment.status(game_dir, base['patch_id'])
            if current.get('state') != 'installed':
                raise deployment.DeploymentError('自有资源补丁在兼容检查期间发生变化。')
            own_pak = next(item for item in current['files']
                           if item['path'] == deployment.PAK_PATH)
            owned_archives[deployment.PAK_PATH] = own_pak['installed_sha256']
        resources = check_resources(game_dir, source['path'], owned_archives=owned_archives)
        observations.update(exe_sha256=source['sha256'],
                            exe_status='new_game_exe' if source['rebase_exe'] else
                            'legacy_patch_to_migrate' if source.get('mode') == 'legacy' else
                            'verified_resource_patch' if source['installed'] else 'compatible_original',
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


def install(game_dir, payload_dir, hook_config):
    """Package the selected text resources, then deploy without patching the EXE."""
    from ac8pak import build_pak, load_runtime_loader
    payload_dir = Path(payload_dir).resolve(strict=True)
    parent = payload_dir.parent
    temporary = Path(tempfile.mkdtemp(prefix='.ac8-pak-', dir=parent))
    try:
        pak = temporary / Path(deployment.PAK_PATH).name
        packaged = build_pak(payload_dir, pak)
        result = deployment.install(game_dir, pak, hook_config, loader=load_runtime_loader())
        result['resource_package'] = {key: value for key, value in packaged.items()
                                      if key not in ('path', 'output', 'pak_path')}
        return result
    finally:
        safe = temporary.resolve()
        if safe.parent != parent or not safe.name.startswith('.ac8-pak-'):
            raise ValueError('资源打包临时目录检查失败。')
        if temporary.exists():
            shutil.rmtree(temporary)
