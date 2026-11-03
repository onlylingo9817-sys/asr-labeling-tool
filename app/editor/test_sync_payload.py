"""Offline regression tests for sync payload and merge semantics."""
import json, sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT/'app'/'sync'))
import sync_server

def test_parent_and_nested_regions():
    rows=[{'start':1.0,'end':5.0,'bodyText':'你好','speaker':'SALES_1','confidence':'manual',
           'tags':['overlap'],'pieces':[
             {'start':2.0,'end':2.5,'text':'嗯','speaker':'CUSTOMER_1','isInterjection':True},
             {'start':3.0,'end':4.0,'text':'继续','speaker':'SALES_1','isInterjection':False}]}]
    p=sync_server.sync(6,rows,dry_run=True)
    assert p['confirmed']==1 and p['pending']==0
    r=p['annotationResult']
    assert r[0]['value']['labels']==['SALES_1']
    assert r[1]['value']['text']==['你好']
    assert r[2]['from_name']=='tags'
    assert r[3]['value']['start']==2.0 and r[3]['value']['labels']==['CUSTOMER_1']
    assert r[4]['value']['text']==['嗯']
    print('test_parent_and_nested_regions OK')

def test_pending_only():
    rows=[{'start':10.0,'end':11.0,'bodyText':'测试','speaker':'UNKNOWN','confidence':'low',
           'pieces':[{'start':10.0,'end':11.0,'text':'测试','speaker':'UNKNOWN','isInterjection':False}]}]
    p=sync_server.sync(6,rows,dry_run=True)
    assert p['confirmed']==0 and p['pending']==1
    assert p['annotationResult']==[]
    print('test_pending_only OK')

def test_invalid_piece():
    rows=[{'start':1.0,'end':2.0,'bodyText':'x','speaker':'SALES_1','confidence':'manual',
           'pieces':[{'start':0.5,'end':1.5,'text':'x','isInterjection':True}]}]
    try: sync_server.sync(6,rows,dry_run=True)
    except ValueError: print('test_invalid_piece OK')
    else: raise AssertionError('outside-piece was accepted')

def test_tag_merge_preserves_speaker():
    existing=[
      {'id':'s1','from_name':'speaker','to_name':'audio','type':'labels','value':{'start':1,'end':2,'channel':0,'labels':['SALES_1']}},
      {'id':'s1','from_name':'text','to_name':'audio','type':'textarea','value':{'start':1,'end':2,'channel':0,'text':['old']}}]
    rows=[{'start':1,'end':2,'bodyText':'old','speaker':'UNKNOWN','confidence':'low','tags':['overlap'],
           'pieces':[{'start':1,'end':2,'text':'old','isInterjection':False}]}]
    out,ch,ad=sync_server.merge_into_annotation(existing,rows)
    assert any(x['from_name']=='speaker' and x['value']['labels']==['SALES_1'] for x in out)
    assert any(x['from_name']=='tags' and x['value']['choices']==['overlap'] for x in out)
    print('test_tag_merge_preserves_speaker OK')

def test_same_time_rows_merge():
    rows=[
      {'start':1.0,'end':2.0,'bodyText':'A','speaker':'CUSTOMER_1','confidence':'low','tags':[]},
      {'start':1.0,'end':2.0,'bodyText':'A','speaker':'CUSTOMER_1','confidence':'manual','tags':['overlap']},
    ]
    out=sync_server._merge_same_time_rows(rows)
    assert len(out)==1 and out[0]['tags']==['overlap']
    print('test_same_time_rows_merge OK')

def test_completed_by_shapes():
    assert sync_server.is_mine({'completed_by':1})
    assert sync_server.is_mine({'completed_by':{'id':1}})
    assert not sync_server.is_mine({'completed_by':{'id':2}})
    assert not sync_server.is_mine({})
    print('test_completed_by_shapes OK')

if __name__=='__main__':
    test_parent_and_nested_regions(); test_pending_only(); test_invalid_piece(); test_tag_merge_preserves_speaker(); test_completed_by_shapes(); test_same_time_rows_merge()
