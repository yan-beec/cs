"""运行真实查看页脚本，核对目录筛选与整轮消息组合规则。"""
import json
from pathlib import Path
import shutil
import subprocess

import pytest


VIEWER = Path(__file__).parents[1] / "offline/web/viewer.js"


def run_js(body):
    """通过 Node 执行查看页的公共函数，避免用字符串检查替代行为检查。"""
    node = shutil.which("node")
    if not node:
        pytest.skip("需要 Node 来运行查看页行为测试")
    source = f"const v = require({json.dumps(str(VIEWER))});\n{body}"
    result = subprocess.run([node, "-e", source], capture_output=True, text=True, encoding="utf-8")
    assert result.returncode == 0, result.stderr


def test_round_start_pending_and_statistic_filters():
    run_js("""
const assert = require('node:assert/strict');
const e = {round_id:'r1', reception_type:'human', round_started_at:'2026-11-10 08:00:00', first_at:'2026-11-11 08:00:00', last_at:'2026-11-12 08:00:00', assisted:true, is_lost:false, rated:true, rating:3};
assert.equal(v.matches(e, {}, {start:'2026-11-11'}), false);
assert.equal(v.matches(e, {}, {end:'2026-11-11'}), true);
assert.equal(v.matches({...e, round_id:null}, {}, {start:'2026-11-11'}), true);
assert.equal(v.matches(e, {}, {human:'pending'}), true);
assert.equal(v.matches({...e, reception_type:'ai_only'}, {}, {human:'pending'}), false);
assert.equal(v.matches(e, {}, {ai:'pending'}), true);
assert.equal(v.matches(e, {}, {assisted:'true', lost:'false'}), true);
assert.equal(v.matches(e, {}, {assisted:'false'}), false);
assert.equal(v.matches(e, {}, {lost:'true'}), false);
assert.equal(v.matches({...e,rated:false}, {}, {rating:'rated'}), false);
assert.equal(v.matches(e, {}, {rating:'low'}), true);
""")


def test_round_message_attachment_combinations():
    run_js("""
const assert = require('node:assert/strict');
const text = {content:'退款', sender_name:'小王', attachments:[]};
const other = {content:'其他问题', sender_name:'小李', attachments:[{kind:'image'}]};
const image = {...text, attachments:[{kind:'image', status:'linked'}]};
assert.equal(v.roundMatches([text, other], {keyword:'退款', media:'any'}), false);
assert.equal(v.roundMatches([image], {keyword:'退款', media:'any'}), true);
assert.equal(v.roundMatches([text, image], {keyword:'退款', media:'none'}), false);
assert.equal(v.roundMatches([text, other], {keyword:'退款', media:'none'}), true);
assert.equal(v.roundMatches([other], {media:'any'}), true);
assert.equal(v.roundMatches([other], {media:'none'}), false);
assert.equal(v.roundMatches([text], {media:'none'}), true);
assert.equal(v.roundMatches([], {media:'none'}), true);
assert.equal(v.roundMatches([text, other], {sender:'小王', media:'any'}), false);
assert.equal(v.roundMatches([image], {sender:'小王', media:'any'}), true);
assert.equal(v.roundMatches([{...text, attachments:[{status:'blocked'}]}], {media:'any'}), true);
assert.equal(v.roundMatches([text, other], {keyword:'退款', media:'image'}), false);
assert.equal(v.roundMatches([image], {keyword:'退款', media:'image'}), true);
assert.equal(v.roundMatches([{...text, attachments:[{kind:'audio'}]}], {media:'video'}), true);
assert.equal(v.roundMatches([{...text, attachments:[{status:'unavailable'}]}], {media:'unavailable'}), true);
for (const media of ['any','none','image','video','unavailable']) assert.equal(v.needsMessages({media}), true);
assert.equal(v.needsMessages({}), false);
assert.equal(v.needsMessages({keyword:'退款'}), true);
assert.equal(v.needsMessages({sender:'小王'}), true);
""")


def test_service_cells_and_real_round_counts():
    run_js("""
const assert = require('node:assert/strict');
assert.equal(v.qualityDuration(0.1), '不足1秒');
assert.equal(v.qualityDuration(86461), '1天1分1秒');
assert.equal(v.qualityDuration(null), '—');
const e = {round_id:'r1', responsibility_basis:'ics_assignment', responsible_agent_id:'7', responsible_agent_name:'小王', rated:true, rating:5, rating_feedback:'满意', service_metrics:{first_response_status:'recorded',first_response_seconds:60,average_response_seconds:30,last_message_by_agent:false,last_message_sender_role:'contact',duration_seconds:600,human_phase_message_count:2,human_phase_customer_count:1,human_phase_agent_count:1}};
const cells = v.serviceCells(e);
assert.deepEqual(cells.map(c=>c[1]), ['小王', '1分', '30秒', '否 · 客户', '10分', '5 分 · 满意', '2（1：1）']);
assert.equal(v.serviceCells({...e, assisted:true, assigned_agent_name:'小李'})[0][1], '小李 · 协助接待，不计考核');
assert.equal(v.serviceCells({...e, round_id:null}).length, 0);
assert.equal(v.countEntries([e,e,{messages:3}]), '1 轮对话 · 未归轮消息 3 条');
""")


def test_filter_summary_distinguishes_locked_and_unlocked_message_time():
    run_js("""
const assert = require('node:assert/strict');
const fs = require('node:fs'), vm = require('node:vm');
const script = fs.readFileSync(require.resolve(VIEWER_PATH), 'utf8');
function summary(meta) {
  const nodes = new Map();
  const node = id => {
    if (!nodes.has(id)) nodes.set(id, {textContent:'', title:'', addEventListener(){}});
    return nodes.get(id);
  };
  node('catalog').textContent = JSON.stringify({meta, customers:[], entries:[]});
  const context = {document:{readyState:'loading', getElementById:node, addEventListener(){}}};
  vm.runInNewContext(script, context);
  context.ICSArchive.start();
  return node('exportScope');
}
for (const mode of ['rounds', 'customers']) {
  const meta = {scope:{mode, filters:{}}, summary:'关键词：退款', snapshot_at:'2026-11-12T02:41:00Z'};
  const unlocked = summary(meta);
  assert.ok(unlocked.textContent.includes('当前筛选（关键词：退款）'));
  assert.ok(unlocked.textContent.includes('未锁定消息时间'));
  assert.ok(unlocked.textContent.includes('内容、评分与质检取自生成时刻'));
  assert.ok(!unlocked.textContent.includes('时间未知'));
  assert.ok(!unlocked.textContent.includes('（锁定）'));
  assert.ok(unlocked.title.includes('未锁定消息时间'));
  assert.ok(!unlocked.title.includes('按锁定时刻限定'));
  for (const lockedMeta of [{...meta,as_of:'2026-11-12T02:30:00Z'}, {...meta,scope:{mode,filters:{as_of:'2026-11-12T02:30:00Z'}}}]) {
    const locked = summary(lockedMeta);
    assert.ok(locked.textContent.includes('消息时间截至'));
    assert.ok(locked.textContent.includes('（锁定）'));
    assert.ok(locked.title.includes('按锁定时刻限定消息时间范围'));
  }
}
""".replace("VIEWER_PATH", json.dumps(str(VIEWER))))


def test_rating_review_snapshot_keeps_raw_score_and_marks_selected_invitation_only():
    run_js("""
const assert = require('node:assert/strict');
const e={round_id:'r1',rating:2,rated:true,rating_review:{round_id:'r1',status:'invalid',selected_rating:{invitation_message_id:7}}};
assert.equal(v.matches(e,{}, {rating:'low'}),true);
assert.equal(v.ratingReviewText(e.rating_review),'已复核标记无效');
assert.equal(v.ratingMessageReviewText({message_id:7,visitor_rating:{rating:2}},e),'已复核标记无效');
assert.equal(v.ratingMessageReviewText({message_id:8,visitor_rating:{rating:2}},e),'');
assert.equal(v.ratingMessageReviewText({message_id:7,visitor_rating:{rating:null}},e),'');
assert.equal(e.rating,2);
""")
