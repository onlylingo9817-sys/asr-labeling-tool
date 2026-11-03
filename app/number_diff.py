#!/usr/bin/env python3
"""Audit-only list of Chinese numerals that cannot be auto-converted safely.

No source file is modified. Humans review these rows sentence by sentence.
The goal is to avoid silently corrupting terms like 四五十万, 两万九千八,
千万别, 三百多.
"""
from __future__ import annotations
import json, re, sys
from pathlib import Path

ROOT=Path(__file__).resolve().parent.parent
NUM='零一二两三四五六七八九十百千万亿'
PAT=re.compile(rf'(?<!百分)[零一二两三四五六七八九十百千万亿][零一二两三四五六七八九十百千万亿多几]*(?:分钟|小时|公里|千米|万元|万|千元|千|百元|百|元|块|档|种|次|年|月|天|度|层|座|%|排|米|个|位|名|分|秒)')
# Terms that are not quantities in this context.
STOP={'一个','这一块','那一个','一块','千万别','千万','一直','一起','一定','一样','一些','一点','一个一个','有一个','第一个'}
# Clear single-number patterns that can be shown as a suggestion, still
# requiring human review before applying.
SAFE=re.compile(r'^(?:[零一二两三四五六七八九]|十[一二三四五六七八九]?|二十|三十|四十|五十|六十|七十|八十|九十|一百|两百|二百|三百|四百|五百|六百|七百|八百|九百)(?:分钟|小时|公里|千米|万|元|块|次|年|天|度|%)?$')

def auto_candidate(raw):
    if raw in STOP: return None
    if any(w in raw for w in ('千万','多','几','百分之')): return None
    return None  # deliberately no automatic replacement

def main():
    src=Path(sys.argv[1]) if len(sys.argv)>1 else ROOT/'work/editor/data.json'
    dst=Path(sys.argv[2]) if len(sys.argv)>2 else ROOT/'work/number_suggestions.json'
    data=json.loads(src.read_text(encoding='utf-8'))
    rows=[]
    for rec in data['recordings']:
        for u in rec['utterances']:
            text=u.get('bodyText') or u.get('text') or ''
            ms=[]
            for m in PAT.finditer(text):
                raw=m.group(0)
                if raw in STOP or raw.startswith('百分'): continue
                # Avoid spans starting mid-word ("零几个" from "几个零").
                before=text[max(0,m.start()-1):m.start()]
                after=text[m.end():m.end()+1]
                # Skip spans embedded inside a larger numeral phrase (百分之, 几个零, 第...).
                left=text[max(0,m.start()-2):m.start()]
                right=text[m.end():m.end()+2]
                if before in '第这那两三四五六七八九十百千万亿零几' or after in '个位名条种次': continue
                if left.endswith('百分') or left.endswith('几个') or right.startswith('几') or left.endswith('分之'): continue
                ms.append({'raw':raw,'start':m.start(),'end':m.end(),
                           'suggest':None,'confidence':'manual',
                           'reason':'人工确认「'+raw+'」是否应改为阿拉伯数字'})
            if ms:
                rows.append({'taskId':rec['taskId'],'id':u['id'],'start':u['start'],'end':u['end'],
                             'before':text,'after':None,'confidence':'manual','matches':ms})
    dst.write_text(json.dumps(rows,ensure_ascii=False,indent=2),encoding='utf-8')
    print(f'{len(rows)} sentences, {sum(len(r["matches"]) for r in rows)} numeral spans; no automatic replacement and no source modification.')
    print('output:',dst)

if __name__=='__main__': main()
