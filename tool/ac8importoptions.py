"""Choose whether to import translations into specific display-sensitive slots.

Rules use the original resource IDs of each CP/ON occurrence. They do not
classify translation rows, inspect their text, or read catalogs or game files.
False means retain the original official text at matching slots; None keeps
the legacy API behavior without applying these import options.
"""
from __future__ import annotations


GROUP_DEFINITIONS = (
    dict(id='hud_labels', label='导入 HUD 标牌译文',
         description='目标框、呼号、仪表与短状态标牌；不勾选时恢复各位置原标牌，'
                     '避免缺字、字体压缩或超长。剧情、无线电和教程仍按译文导入。'),
    dict(id='equipment_short', label='导入装备简称译文',
         description='武器、机体和零件简称，可能在飞行 HUD、科技树与机库共用；'
                     '不勾选时保留原简称，装备完整名称与说明仍按译文导入。'),
)

DEFAULT_IMPORT_OPTIONS = {'hud_labels': False, 'equipment_short': False}

_CP_TARGET_PREFIXES = (
    'ContainerAircraft_Name_',
    'ContainerGround_Name_',
    'ContainerAllyCallsign_Name_',
    'ContainerEnemyCallsign_Name_',
    'ContainerAllyTac_Name_',
    'ContainerUnknown_Name_',
    'ContainerEnemySecondName_',
    'ContainerNamedEnemyCallsign_',
    'ContainerBossPartsName_',
    'ContainerEscortTargetName_',
    'ContainerWaypoint_Name_',
    'ContainerCorpsName_',
    'HudContainerinfo_Name_',
)

# The replay player label uses this briefing-named slot. Keep the separate
# HangarSelectaircraftTitle_Name_Player occurrence available for translation.
_CP_TARGET_EXACT_IDS = frozenset(('ContainerBriefing_Name_Rex',))

_ON_TARGET_PREFIXES = (
    'Online_ContainerCorpsName_',
    'Online_Hud_OnlineMissionCallsign_',
    'Online_HudContainerInfo_',
)

_CP_STATUS_PREFIXES = (
    'HudWarning_Name_',
    'HudAttack_Name_',
    'HudIndicator_Name_',
    'HudPlayerinfo_Name_',
    'HudMissionprogress_Name_',
    'HudMinigames_Name_',
    'HudMissioninfo_Name_',
    'Chronicle_Hud_Name_',
)

# These IDs already have localized messages in the official text snapshot.
_CP_OTHER_EXCLUDED = frozenset(
    'HudOther_Name_' + suffix for suffix in (
        'Wingmanattack1',
        'Wingmanattack2',
        'Wingmandisperse',
        'Wingmancover',
        'Wingmanspecialweapon1',
        'Wingmanspecialweapon2',
        'Wingmanspecialweapon3',
        'Attentionjp',
        'Yesjp',
        'Nojp',
    )
)

_EQUIPMENT_SHORT_PREFIXES = (
    'WeaponShort_Name_',
    'AircraftShort_Name_',
    'Aircraft_GetAircraft_ShortName_',
    'PartsShort_Name_',
)


def normalize_options(options):
    """Return validated options, preserving None for legacy callers."""
    if options is None:
        return None
    if not isinstance(options, dict):
        raise ValueError('导入选项必须为对象或 null。')
    unknown = set(options) - set(DEFAULT_IMPORT_OPTIONS)
    if unknown:
        names = '、'.join(repr(key) for key in sorted(unknown, key=repr))
        raise ValueError(f'导入选项包含未知字段：{names}')
    result = DEFAULT_IMPORT_OPTIONS.copy()
    for key, value in options.items():
        if type(value) is not bool:
            raise ValueError(f'导入选项 {key} 必须为布尔值。')
        result[key] = value
    return result


def _hud_label(domain, text_id):
    if domain == 'CP':
        if (text_id in _CP_TARGET_EXACT_IDS
                or text_id.startswith(_CP_TARGET_PREFIXES + _CP_STATUS_PREFIXES)):
            return True
        return (text_id.startswith('HudOther_Name_')
                and text_id not in _CP_OTHER_EXCLUDED)
    if text_id.startswith(_ON_TARGET_PREFIXES):
        return True
    if not text_id.startswith('Online_Hud_'):
        return False
    if text_id.startswith('Online_Hud_StatusNotification_'):
        return False
    if text_id.startswith('Online_Hud_ChatRadioHud_'):
        return text_id == 'Online_Hud_ChatRadioHud_Attack_Name'
    return True


def groups_for_occurrence(occurrence):
    """Match a raw occurrence, never its grouped translation row or body."""
    if not isinstance(occurrence, dict):
        return ()
    domain = occurrence.get('domain')
    if domain not in ('CP', 'ON'):
        return ()
    text_ids = occurrence.get('text_ids_ascii', ())
    if not isinstance(text_ids, (list, tuple)):
        return ()
    text_ids = tuple(text_id for text_id in text_ids if isinstance(text_id, str))
    groups = []
    if any(_hud_label(domain, text_id) for text_id in text_ids):
        groups.append('hud_labels')
    if domain == 'CP' and any(text_id.startswith(_EQUIPMENT_SHORT_PREFIXES)
                              for text_id in text_ids):
        groups.append('equipment_short')
    return tuple(groups)
