"""实际 Node 执行离线筛选器的消息组合和输入归一。"""
import json
import subprocess
from pathlib import Path


def test_real_filters_same_message_history_and_negative_attachment():
    script = Path(__file__).parents[1] / "offline/web/filters.js"
    result = subprocess.run(["node", "-e", """
const f=require(process.argv[1]),a=require('node:assert/strict');
const e={round_id:'r',round_started_at:'2026-11-01T00:00:00Z',last_message_at:'2026-11-01T00:02:00Z',reception_type:'human'},c={id:'c'};
const rows=[{message_id:'1',sender_role:'contact',sent_at:'2026-11-01T00:01:00Z',search_contents:['旧退款'],has_original_attachment:false},
{message_id:'2',sender_role:'agent',is_human_reply:true,sent_at:'2026-11-01T00:02:00Z',search_contents:['其他'],has_original_attachment:true}];
const run=p=>f.select([e],[c],()=>rows,p).round_ids;
a.deepEqual(run({keyword:'退款',message_scope:'agent'}),[]);
a.deepEqual(run({keyword:'退款',has_attachment:false}),['r']);
a.deepEqual(run({keyword:'退款',has_attachment:true}),[]);
rows.push({...rows[0],message_id:'3',has_original_attachment:true});
a.deepEqual(run({keyword:'退款',has_attachment:false}),[]);
a.deepEqual(run({keyword:'退款',time_field:'reply_at',start:'2026-11-01T00:02:00Z'}),['r']);
a.throws(()=>f.normalize({start:'2026-11-03',end:'2026-11-01'}));
a.throws(()=>f.normalize({agent_id:'8'}));
a.throws(()=>f.normalize({conversation_id:'1e3'}));
a.deepEqual(f.select([e],[c],()=>rows,{as_of:'2026-11-01T00:00:30Z',has_attachment:false}).round_ids,[]);
f.configureCasefold({'927':'ο','931':'σ','304':'i'});
a.equal(f.lower('ΟΣ'),'οσ');a.equal(f.includes('ΟΣ','ος'),false);a.equal(f.includes('İ','i\u0307'),false);
a.equal(f.time('2026-11-01T00:00:00.000100Z')<f.time('2026-11-01T00:00:00.000900Z'),true);
""", str(script)], capture_output=True, text=True, encoding="utf-8")
    assert result.returncode == 0, result.stderr


def test_database_case_mapping_is_included_in_contract_and_budget(tmp_path):
    import pytest
    from quality.offline.bundle import build_bundle, BundleLimit
    from quality.offline.media import MediaDownloader
    from quality.tests.test_offline_bundle import message, settings, parse
    mapping = {"927": "ο", "931": "σ", "304": "i"}
    path = build_bundle(tmp_path / "ok", [message(1)], settings(tmp_path), MediaDownloader([]),
                        lambda **v: None, lambda: None, metadata={"casefold_map": mapping})
    _, catalog, _, _ = parse(path)
    assert catalog["meta"]["filter_contract"]["casefold_map"] == mapping
    with pytest.raises(BundleLimit):
        build_bundle(tmp_path / "large", [message(1)], settings(tmp_path, max_bytes=1024), MediaDownloader([]),
                     lambda **v: None, lambda: None, metadata={"casefold_map": {"65": "a" * 2048}})
    assert not (tmp_path / "large/archive.html").exists()
