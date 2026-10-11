"""Offline, read-only comparison of AC8 sources and the user's translation."""
from __future__ import annotations

import argparse
from collections import Counter
import json
from pathlib import Path
import sys
import time
import uuid

import ac8tool
import ac8categories


def _safe_json(value, *, indent=None):
    # Preserve ordinary Unicode, but represent invalid surrogate input safely so
    # that a comparison can still show its validation problem.
    return json.dumps(value, ensure_ascii=False, indent=indent,
                      separators=(',', ':') if indent is None else None).encode(
                          'utf-8', errors='backslashreplace').decode('utf-8')


def _write_text(path, text):
    temporary = path.with_name(path.name + '.tmp-' + uuid.uuid4().hex)
    try:
        temporary.write_text(text, encoding='utf-8')
        temporary.replace(path)
    finally:
        if temporary.exists():
            temporary.unlink()


def _html(document):
    payload = _safe_json(document).replace('&', '\\u0026').replace('<', '\\u003c').replace(
        '>', '\\u003e').replace('\u2028', '\\u2028').replace('\u2029', '\\u2029')
    return _HTML.replace('__COMPARISON_DATA__', payload)


def export_comparison(game_dir, input_path, output_dir, include_official=True, import_options=None):
    """Generate a local HTML/JSON comparison without changing input or game.

    Empty translations and format errors are allowed here. Original JA/EN and
    any attached official source fields are still validated as read-only.
    ``include_official`` controls the displayed column. A file already carrying
    official text must validate that source even when the column is hidden.
    """
    input_path = Path(input_path).resolve()
    output_dir = Path(output_dir).resolve()
    game_root = Path(game_dir).resolve()
    if output_dir == game_root or game_root in output_dir.parents:
        raise ValueError('对照文件请保存到项目目录，不能写入游戏目录。')
    source = ac8tool.load_translation_document(input_path)
    validation, entries = ac8tool._validate_document(source, allow_partial=True, import_options=import_options)
    plan = ac8tool.selection_plan(source, entries, import_options=import_options)
    game_check = ac8tool.check_game_version(game_root)
    if not game_check['ok']:
        raise ValueError('；'.join(game_check['errors']))
    official, reference = ac8tool._official_sources() if include_official else ({}, None)
    issues = {issue['id']: issue for issue in validation['format_issues']}
    short_ids = {entry['id']: number for number, entry in
                 enumerate(ac8tool._entries(source['scope']), 1)}
    rows, counts = [], Counter()
    category_counts = {}
    issue_entries = set()
    excluded_policy = plan['selection']['excluded_policy']
    category_labels = {item['id']: item['label']
                       for item in ac8categories.CATEGORY_DEFINITIONS}
    for entry in entries.values():
        entry_issue = issues.get(entry['id'], {})
        planned = plan['per_entry'][entry['id']]['occurrences']
        if len(planned) != len(entry['occurrences']):
            raise ValueError('翻译范围与文本槽数量不一致。')
        for occurrence_index, occurrence in enumerate(entry['occurrences']):
            text = ac8tool.translation_for_occurrence(entry, occurrence_index)
            if 'variant_issues' in entry_issue:
                entry_problems = list(dict.fromkeys(problem
                    for variant in entry_issue['variant_issues']
                    if occurrence_index in variant['occurrence_indices']
                    for problem in variant['problems']))
            else:
                entry_problems = entry_issue.get('problems', [])
            selection = planned[occurrence_index]
            selected = bool(selection['selected'])
            category = selection.get('category')
            reason = selection.get('reason')
            label = category_labels.get(category)
            row = dict(entry_id=entry['id'], short_id=short_ids[entry['id']], category=entry['category'],
                       ja=entry['ja'], en=entry['en'], translation=text,
                       format_source=entry['format_source'],
                       format_problems=entry_problems if selected else [],
                       text_category=category, text_category_label=label,
                       text_category_reason=reason,
                       selected_for_translation=selected, excluded_policy=excluded_policy)
            if selection.get('force_original_official'):
                row.update(excluded_policy='official', import_protected=True,
                           import_protection_groups=selection['import_protection_groups'])
                row['text_category_reason'] = '导入显示保护：此位置保留原标牌或简称；JSON 译文仍保留。'
            if entry['category'] == 'GameData':
                row.update(domain=occurrence['domain'], index=occurrence['index'],
                           text_ids_ascii=occurrence['text_ids_ascii'])
                if include_official:
                    row['official_zh'] = official[(occurrence['domain'], occurrence['index'])]
            else:
                row.update(domain='Engine', index=None, target=occurrence['target'],
                           target_path=occurrence['target_path'], namespace=occurrence['namespace'],
                           key=occurrence['key'], text_ids_ascii=[])
            if not selected:
                row['status'] = 'excluded'
            elif text is None:
                row['status'] = 'missing'
            elif 'official_zh' in row:
                row['status'] = 'same' if text == row['official_zh'] else 'different'
            else:
                row['status'] = 'filled'
            counts['total_slots'] += 1
            counts['game_slots' if entry['category'] == 'GameData' else 'engine_slots'] += 1
            counts['selected_slots' if selected else 'excluded_slots'] += 1
            if selected:
                counts['missing_slots' if text is None else 'filled_slots'] += 1
            if category is not None:
                per_category = category_counts.setdefault(category, dict(
                    category=category, label=label, total_slots=0,
                    selected_slots=0, excluded_slots=0))
                per_category['total_slots'] += 1
                per_category['selected_slots' if selected else 'excluded_slots'] += 1
            if 'official_zh' in row:
                counts['official_available_slots'] += 1
            if row['status'] == 'same':
                counts['same_as_official_slots'] += 1
            elif row['status'] == 'different':
                counts['different_from_official_slots'] += 1
            if selected and entry_problems:
                counts['format_issue_slots'] += 1
                issue_entries.add(entry['id'])
            rows.append(row)
    rows.sort(key=lambda row: (row['domain'], row['index'] if row['index'] is not None else -1,
                               row.get('target_path', ''), row.get('namespace', ''), row.get('key', '')))
    for key in ('total_slots', 'game_slots', 'engine_slots', 'selected_slots', 'excluded_slots',
                'filled_slots', 'missing_slots',
                'official_available_slots', 'same_as_official_slots', 'different_from_official_slots',
                'format_issue_slots'):
        counts.setdefault(key, 0)
    counts['unique_entries'] = len(entries)
    counts['selected_unique'] = plan['counts']['selected_unique']
    counts['excluded_unique'] = plan['counts']['excluded_unique']
    counts['mixed_unique'] = plan['counts']['mixed_unique']
    counts['format_issue_unique'] = len(issue_entries)
    read_official = bool(include_official or validation['official_chinese_read'])
    document = dict(schema='ac8-comparison-v1', source_revision=source['source_revision'],
                    buildid=game_check['buildid'], translation_file=str(input_path),
                    official_column_visible=bool(include_official), official_reference=reference,
                    official_chinese_read=read_official, counts=dict(counts), rows=rows,
                    translation_selection=plan['selection'],
                    classification_enabled=plan['classification_enabled'],
                    category_counts=[category_counts[item['id']]
                                     for item in ac8categories.CATEGORY_DEFINITIONS
                                     if item['id'] in category_counts],
                    validation=validation, translation_file_modified=False, game_files_changed=False)
    if 'import_options' in plan:
        document.update(import_options=plan['import_options'], import_protection=plan['import_protection'])
    output_dir.mkdir(parents=True, exist_ok=True)
    basename = 'comparison-' + time.strftime('%Y%m%d-%H%M%S') + '-' + uuid.uuid4().hex[:8]
    json_path, html_path = output_dir / (basename + '.json'), output_dir / (basename + '.html')
    _write_text(json_path, _safe_json(document, indent=2) + '\n')
    _write_text(html_path, _html(document))
    return dict(ok=True, action='comparison', html_path=str(html_path), json_path=str(json_path),
                counts=dict(counts), official_chinese_read=read_official,
                official_column_visible=bool(include_official),
                translation_file_modified=False, game_files_changed=False,
                validation=dict(ok=validation['ok'], missing_unique=validation['missing_unique'],
                                format_issue_count=validation['format_issue_count']))


_HTML = r'''<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; base-uri 'none'; form-action 'none'">
<title>ACE COMBAT 8 · 译文对照</title>
<style>
:root{color-scheme:light dark;font-family:"Microsoft YaHei UI",system-ui,sans-serif}
body{margin:0;padding:24px;background:#f5f6f8;color:#182535}h1{font-size:23px;margin:0 0 8px}
p{margin:8px 0;color:#435064}.bar{display:flex;gap:12px;align-items:center;flex-wrap:wrap;margin:18px 0}
input,select,button{font:inherit;border:1px solid #b9c2cf;border-radius:5px;background:white;color:#182535;padding:8px}
#search{min-width:260px;flex:1}button{cursor:pointer}button:disabled{opacity:.45;cursor:default}
#page{width:60px}label{display:flex;gap:6px;align-items:center}#summary,#result{font-size:14px}
.table-wrap{overflow-x:auto;background:white;border:1px solid #d5dbe3;border-radius:7px}
table{border-collapse:collapse;table-layout:fixed;width:100%;min-width:1080px}th,td{border-bottom:1px solid #dce1e8;padding:12px;text-align:left;vertical-align:top}
th{background:#eaf0f7}th:first-child{width:190px}td{white-space:pre-wrap;overflow-wrap:anywhere;font-size:14px;line-height:1.65}
.meta{font-size:12px;color:#5d6878}.badge{display:block;color:#245aa8;margin-top:7px}.problem{color:#a8321f;font-size:12px;margin-top:7px}.note{display:block;font-size:12px;color:#5d6878;margin-top:8px}
.empty{color:#737d8c}footer{font-size:12px;color:#69758a;margin:18px 0}
@media(prefers-color-scheme:dark){body{background:#161c24;color:#e2e9f4}p,.meta,.note,footer{color:#b1bdce}input,select,button,.table-wrap{background:#202a37;color:#e2e9f4;border-color:#4a586a}th{background:#2c3b50}td,th{border-color:#415066}.problem{color:#ff9987}.badge{color:#99c2ff}}
</style></head><body>
<h1>ACE COMBAT 8 · 译文对照</h1>
<p id="source"></p><p id="summary"></p>
<div class="bar"><input id="search" type="search" placeholder="搜索日文、英文、中文或文本 ID" aria-label="搜索">
<select id="status" aria-label="译文状态"><option value="all">全部条目</option><option value="filled">已填写</option><option value="missing">未填写</option><option value="same">与官中相同</option><option value="different">与官中不同</option></select>
<select id="domain" aria-label="文本分区"><option value="all">全部分区</option><option value="CP">CP</option><option value="ON">ON</option><option value="Engine">Engine</option></select>
<select id="scope" aria-label="翻译范围"><option value="all">全部范围</option><option value="selected">参与翻译</option><option value="excluded">不翻译</option></select>
<label><input id="issues" type="checkbox">只看应用译文的格式问题</label></div>
<div class="bar"><span id="result"></span><button id="prev" type="button">上一页</button><label>第 <input id="page" type="number" min="1" value="1"> 页</label><button id="next" type="button">下一页</button>
<select id="size" aria-label="每页条目数"><option value="50">每页 50 条</option><option value="100" selected>每页 100 条</option><option value="200">每页 200 条</option></select></div>
<div class="table-wrap"><table><thead><tr id="head"></tr></thead><tbody id="body"></tbody></table></div>
<footer>本页为离线只读对照。相同／不同按逐个文本槽精确比较，不表示翻译质量。旧文件排除的文本仍按原选择处理。修改译文后请重新生成对照。</footer>
<script id="comparison-data" type="application/json">__COMPARISON_DATA__</script>
<script>
'use strict';
const data=JSON.parse(document.getElementById('comparison-data').textContent);
const byId=id=>document.getElementById(id);
const statusNames={missing:'未填写',filled:'已填写',same:'与官中相同',different:'与官中不同',excluded:'不翻译'};
const excludedNames={official:'保留官中',en:'使用英文'};
const official=!!data.official_column_visible;
const summary=data.counts;
byId('source').textContent='Steam build '+data.buildid+' · '+(official?'日文 / 英文 / 官中 / 自译':'日文 / 英文 / 自译');
byId('summary').textContent='共 '+summary.total_slots+' 个文本槽，参与翻译 '+summary.selected_slots+'，不翻译 '+summary.excluded_slots+'。参与翻译中：已填写 '+summary.filled_slots+'，未填写 '+summary.missing_slots+'，格式问题 '+summary.format_issue_slots+(official?'，与官中相同 '+summary.same_as_official_slots+'，不同 '+summary.different_from_official_slots:'');
if(!official){for(const option of Array.from(byId('status').options)){if(['same','different'].includes(option.value))option.remove();}}
const columns=official?['文本槽 / 状态','日文','英文','官方简体中文','你的中文']:['文本槽 / 状态','日文','英文','你的中文'];
for(const column of columns){const th=document.createElement('th');th.textContent=column;byId('head').append(th);}
let filtered=data.rows,page=1,timer;
function cell(text){const td=document.createElement('td');if(text===null||text===undefined){td.textContent='（未填写）';td.className='empty';}else td.textContent=text;return td;}
function render(){
 const size=Number(byId('size').value),pages=Math.max(1,Math.ceil(filtered.length/size));page=Math.min(pages,Math.max(1,page));
 const fragment=document.createDocumentFragment();
 for(const row of filtered.slice((page-1)*size,page*size)){
  const tr=document.createElement('tr'),td=document.createElement('td'),meta=document.createElement('div');
  td.textContent='序号 '+row.short_id+' · '+row.domain+(row.index===null?' / '+row.target+' / '+row.key:' #'+row.index);
  meta.className='meta';meta.textContent=row.text_ids_ascii.join('\n');td.append(meta);
  const badge=document.createElement('span');badge.className='badge';badge.textContent=statusNames[row.status]+(row.selected_for_translation?' · 格式参考 '+row.format_source.toUpperCase():' · '+excludedNames[row.excluded_policy]);td.append(badge);
  if(row.format_problems.length){const problem=document.createElement('div');problem.className='problem';problem.textContent=row.format_problems.join('\n');td.append(problem);}
  tr.append(td,cell(row.ja),cell(row.en));if(official)tr.append(cell(row.official_zh));
  const translation=cell(row.translation);
  if(!row.selected_for_translation){const note=document.createElement('span');note.className='note';note.textContent='此处自译不会应用；'+excludedNames[row.excluded_policy]+'。';translation.append(note);}
  tr.append(translation);fragment.append(tr);
 }
 byId('body').replaceChildren(fragment);byId('page').value=page;byId('page').max=pages;
 byId('prev').disabled=page<=1;byId('next').disabled=page>=pages;
 byId('result').textContent='筛选结果 '+filtered.length+' 条 · '+page+' / '+pages+' 页';
}
function filter(){
 const query=byId('search').value.toLocaleLowerCase(),status=byId('status').value,domain=byId('domain').value,scope=byId('scope').value,issues=byId('issues').checked;
 filtered=data.rows.filter(row=>{
  if(domain!=='all'&&row.domain!==domain)return false;
  if(scope==='selected'&&!row.selected_for_translation)return false;
  if(scope==='excluded'&&row.selected_for_translation)return false;
  if(status!=='all'&&!row.selected_for_translation)return false;
  if(status==='filled'&&row.translation===null)return false;
  if(!['all','filled'].includes(status)&&row.status!==status)return false;
  if(issues&&(!row.selected_for_translation||!row.format_problems.length))return false;
  if(query){const fields=[row.ja,row.en,row.translation,row.entry_id,row.short_id,row.domain,row.index,row.text_category,row.text_category_label,row.text_category_reason,...row.text_ids_ascii];if(official)fields.push(row.official_zh);if(!fields.some(value=>value!==null&&value!==undefined&&String(value).toLocaleLowerCase().includes(query)))return false;}
  return true;
 });page=1;render();
}
byId('search').addEventListener('input',()=>{clearTimeout(timer);timer=setTimeout(filter,200);});
for(const id of ['status','domain','scope','issues'])byId(id).addEventListener('change',filter);
byId('size').addEventListener('change',()=>{page=1;render();});
byId('prev').addEventListener('click',()=>{page--;render();});byId('next').addEventListener('click',()=>{page++;render();});
byId('page').addEventListener('change',()=>{page=Number(byId('page').value)||1;render();});render();
</script></body></html>'''


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--game-dir', default=ac8tool.GAME_DEFAULT)
    parser.add_argument('--input', required=True)
    parser.add_argument('--output-dir', required=True)
    parser.add_argument('--without-official', action='store_true')
    parser.add_argument('--translate-hud-labels', action='store_true')
    parser.add_argument('--translate-equipment-short', action='store_true')
    args = parser.parse_args()
    try:
        result = export_comparison(args.game_dir, args.input, args.output_dir,
                                   include_official=not args.without_official,
                                   import_options=dict(hud_labels=args.translate_hud_labels,
                                                       equipment_short=args.translate_equipment_short))
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except (ValueError, OSError) as error:
        print(json.dumps(dict(ok=False, error=str(error), game_files_changed=False), ensure_ascii=False))
        return 2


if __name__ == '__main__':
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8')
    raise SystemExit(main())
