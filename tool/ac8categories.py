"""Classify localization slots from their original IDs and JA/EN sources.

The rules describe the purposes visible in this game's resource IDs. They do
not read any catalog, official Chinese, filesystem state or game at import
time. Unidentified slots remain unknown instead of inheriting a neighbour's
category. These are useful rule-based groups, not a semantic certainty.
"""
from __future__ import annotations

from collections import Counter
import re


CATEGORY_DEFINITIONS = (
    dict(id='story', label='剧情对白与无线电',
         description='单人剧情、过场字幕、机库对话与战斗无线电。'),
    dict(id='briefing', label='任务简报与目标',
         description='任务简报、战果汇报、任务资料、目标与战斗 HUD 提示。'),
    dict(id='tutorial', label='教程与操作',
         description='训练任务、操作教程、机库指引与载入技巧。'),
    dict(id='menus', label='菜单与设置',
         description='主菜单、暂停与回放、图鉴菜单、成绩奖章、按键及音画设置。'),
    dict(id='equipment', label='机体武器装备',
         description='机体、武器、零件、涂装、徽章、地面装备与机库配置。'),
    dict(id='lore', label='人物与世界观',
         description='人物姓名、呼号、部队名称、人物档案与背景战况。'),
    dict(id='multiplayer', label='多人联机',
         description='联机资源、联机无线电、大厅、商店与网络设置。'),
    dict(id='system', label='系统提示与错误',
         description='存档和设备状态、错误、确认弹窗、启动状态与可选引擎资源。'),
    dict(id='technical', label='技术许可与调试',
         description='版权许可、制作名单、调试占位与静默标记。'),
    dict(id='unknown', label='未确定',
         description='缺少可读用途 ID、未匹配规则或同一槽的用途有冲突；保留人工判断。'),
)

_CATEGORY_IDS = frozenset(item['id'] for item in CATEGORY_DEFINITIONS)
_VOICE_ID = re.compile(r'^v\d{4}(?:_[A-Za-z0-9.-]+)+$')
_MISSION_SPEAKER_ID = re.compile(r'^ms\d{2}(?:-\d+)?_')


def _result(category, reason):
    return dict(category=category, reason=reason)


def _classify_id(text_id):
    """Return one ID's purpose, using exact families before broader families."""
    lower = text_id.lower()
    if lower.startswith(('online_dummytext_', 'silent_')) or any(
            marker in lower for marker in ('_debug_', '_dummy_', '_placeholder_')):
        return _result('technical', f'ID 的调试或占位标记：{text_id}')
    if lower.startswith(('openingcredit_', 'mainmenucopyright',
                         'optioncopyright', 'optioneula', 'optionprivacypolicy',
                         'mainmenuwarningfiction')) or lower in (
                             'mainmenurights_name_license', 'mainmenutop_eula'):
        return _result('technical', f'版权、许可或制作名单 ID：{text_id}')
    if lower.startswith(('online', 'systemnetwork', 'systemlobby',
                         'systemonline', 'mainmenucrossplay', 'otersugc')):
        return _result('multiplayer', f'联机、大厅或网络 ID：{text_id}')
    if lower.startswith(('others_colorsupport_', 'others_soundsupport_')):
        return _result('menus', f'辅助功能设置 ID：{text_id}')
    if lower.startswith(('others_dialogue_', 'othersps4_dialogue_',
                         'othersxbox_dialogue_', 'otherssteam_dialogue_',
                         'mainmenusave', 'mainmenureplaycapacityfailure',
                         'mainmenufirstsave', 'mainmenufirstlinkerror',
                         'mainmenufirstmaintenance', 'mainmenufirstupdate',
                         'mainmenushadercompile', 'mainmenuplaygo',
                         'mainmenumove')):
        return _result('system', f'系统状态、错误或确认 ID：{text_id}')
    if lower.startswith(('training', 'hudcampaigntutorial_',
                         'hangarguidetutrial_', 'campaignloadingtips_')):
        return _result('tutorial', f'训练、教程或操作指引 ID：{text_id}')
    if lower.startswith(('msg_bri_', 'vo_msg_deb_', 'text_bri_',
                         'campaignbriefing', 'campaigndebriefing',
                         'containerbriefing_', 'hangarmissionrequirements_',
                         'recordassulthint_', 'medalhint_', 'mission')):
        return _result('briefing', f'任务简报、目标或任务资料 ID：{text_id}')
    if lower.startswith(('hud', 'chronicle_hud_', 'containerunknown_',
                         'containerwaypoint_')):
        return _result('briefing', f'战斗 HUD 状态与目标 ID：{text_id}')
    if lower.startswith(('msg_cin_', 'msg_igc_', 'msg_han_', 'cin_',
                         'text_motiongraphics_', 'text_art_', 'text_place_',
                         'text_placejp_', 'text_datetime_', 'text_datetimejp_',
                         'text_localization_')) or _VOICE_ID.match(text_id):
        return _result('story', f'剧情字幕、动作选项或无线电 ID：{text_id}')
    if lower.startswith(('aircraft', 'weapon', 'parts', 'emblem',
                         'containeraircraft_', 'containerground_',
                         'containerbosspartsname_', 'datavieweraircraft',
                         'hangar')):
        return _result('equipment', f'机体、装备、外观或机库 ID：{text_id}')
    if lower.startswith(('cinema_', 'live_', 'common_', 'extra_speaker',
                         'three_way_voice', 'callsign_', 'containerally',
                         'containerenemy', 'containernamedenemy',
                         'containercorps', 'containerescorttarget',
                         'recordassult', 'battlesituationexplanation_',
                         'text_mainunitname_', 'text_subunitname_',
                         'text_unitintro_', 'dataviewerpilotdata',
                         'dataviewerassault')) or _MISSION_SPEAKER_ID.match(text_id):
        return _result('lore', f'人物、呼号、部队或背景资料 ID：{text_id}')
    if lower.startswith(('mainmenu', 'option', 'systemmenu_', 'campaign',
                         'dataview', 'musicplayer_', 'trophy_', 'medal_',
                         'medalgetitem_', 'keygenral_', 'keyassign_',
                         'others_name_')):
        return _result('menus', f'菜单、设置、记录或通用按键 ID：{text_id}')
    return _result('unknown', f'ID 用途尚未匹配：{text_id}')


def classify_occurrence(domain, index, text_ids_ascii, ja, en):
    """Classify a single source slot. ``index`` is identity, never a heuristic.

    ON denotes the original multiplayer resource pool, so its visible texts
    stay together even where a radio ID resembles a campaign radio ID.
    Technical placeholders are the exception. Missing IDs stay unknown.
    """
    del index
    if domain not in ('CP', 'ON'):
        return _result('system', f'可选引擎或子系统资源：{domain}')
    if not (ja or '').strip() and not (en or '').strip():
        return _result('technical', '日英原文都为空或仅含空白，属于空白占位。')
    ids = tuple(value for value in (text_ids_ascii or ())
                if isinstance(value, str) and value)
    if not ids:
        return _result('unknown', '缺少可读文本用途 ID，未按文本长度或邻近索引猜测。')
    matched = [_classify_id(value) for value in ids]
    categories = {item['category'] for item in matched}
    if domain == 'ON' and categories != {'technical'}:
        return _result('multiplayer', f'ON 联机资源池；文本 ID：{" / ".join(ids)}')
    if len(categories) != 1:
        return _result('unknown', f'同一文本槽存在用途冲突：{" / ".join(ids)}')
    return _result(matched[0]['category'], '；'.join(item['reason'] for item in matched))


def categories_for_entry(entry):
    """Return a classification for every occurrence in its original order."""
    results = []
    for occurrence in entry.get('occurrences', ()):
        domain = occurrence.get('domain', 'Engine')
        index = occurrence.get('index')
        result = classify_occurrence(domain, index,
                                     occurrence.get('text_ids_ascii', ()),
                                     entry.get('ja'), entry.get('en'))
        results.append(dict(domain=domain, index=index, **result))
    return results


def category_counts(entries):
    """Return total slots and distinct entries per purpose.

    An entry shared by multiple purposes is counted once in each relevant
    category; therefore category entry counts need not sum to total entries.
    Slot counts are disjoint and always sum to the complete occurrence count.
    """
    slot_counts = Counter()
    entry_counts = Counter()
    total_entries = 0
    for entry in entries:
        total_entries += 1
        categories = [item['category'] for item in categories_for_entry(entry)]
        slot_counts.update(categories)
        entry_counts.update(set(categories))
    categories = [dict(id=item['id'], label=item['label'],
                       slots=slot_counts[item['id']],
                       entries=entry_counts[item['id']])
                  for item in CATEGORY_DEFINITIONS]
    assert set(slot_counts) <= _CATEGORY_IDS
    return dict(total_entries=total_entries, total_slots=sum(slot_counts.values()),
                categories=categories)
