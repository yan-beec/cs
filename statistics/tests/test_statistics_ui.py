"""Run the actual report UI in Node to check date switching and fresh resets."""

import json
from pathlib import Path
import shutil
import subprocess

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "web" / "reports.js"
NODE = shutil.which("node")

HARNESS = r"""
const assert = require('node:assert/strict');
const vm = require('node:vm');
const nodes = new Map(), requests = [], downloads = [];
let now = Date.parse('2026-10-20T16:30:00Z');
class ClockDate extends Date {
  constructor(...args) {super(...(args.length ? args : [now]));}
  static now() {return now;}
}
class Element {
  constructor(id) {
    this.id = id; this.value = ''; this.type = ['reportDate','reportStartDate','reportEndDate'].includes(id) ? 'date' : '';
    this.innerHTML = ''; this.textContent = ''; this.disabled = false; this.handlers = {};
    this.required = this.type === 'date'; this.validationMessage = '';
    this.min = this.required ? '2000-01-01' : ''; this.max = this.required ? '2099-12-31' : '';
    this.clientWidth = 800; this.clientHeight = 180;
    const classes = new Set();
    this.classList = {toggle(name,on) {on ??= !classes.has(name); on ? classes.add(name) : classes.delete(name);},
      add(name) {classes.add(name);}, remove(name) {classes.delete(name);}, contains(name) {return classes.has(name);}};
  }
  addEventListener(event, handler) {this.handlers[event] = handler;}
  setCustomValidity(message) {this.validationMessage = message;}
  reportValidity() {return [...nodes.values()].every(n => n.disabled || (!n.validationMessage && (!n.required ||
    (n.value && (!n.min || n.value >= n.min) && (!n.max || n.value <= n.max)))));}
}
const node = id => {if (!nodes.has(id)) nodes.set(id, new Element(id)); return nodes.get(id);};
let responseAgents = [];
const data = url => {
  const parsed = new URL(url,'http://localhost'), p = parsed.searchParams;
  if (parsed.pathname.endsWith('/filters')) return {sources:[{source_key:'a',account_id:1}],channels:[{key:'wechat',name:'微信'}]};
  const buckets = [];
  if (p.get('mode') === 'custom') {
    for (let day = new Date(p.get('start_date')+'T00:00:00Z'), end = new Date(p.get('end_date')+'T00:00:00Z'); day <= end; day.setUTCDate(day.getUTCDate()+1)) {
      const label = day.toISOString().slice(0,10);
      buckets.push({key:label+'T00:00:00+08:00',label,future:false,partial:false});
    }
  }
  const common = {mode:p.get('mode'),date:p.get('date')||p.get('start_date'),start_date:p.get('start_date'),end_date:p.get('end_date'),
    generated_at:'2026-10-21T00:30:00+08:00',buckets,rows:buckets.map(b=>({key:b.key,incoming:0,ai_only:0,handoff:0,rejected:0,effective_human:0,low_rating:0,channels:{}})),
    totals:{incoming:0,ai_only:0,handoff:0,rejected:0,effective_human:0,low_rating:0},channels:{},processing:{pending_jobs:0}};
  if (parsed.pathname.endsWith('/quality')) return {...common,agents:[],total:35,page:Number(p.get('page')),page_size:20,
    total_inquiries:0,total_failed_inquiries:0,human_failure_rate:null,
    ai:{total_rounds:0,handoff:0,ai_only:0,handoff_failed:0,failed_rounds:0,failure_rate:null}};
  return parsed.pathname.endsWith('/time') ? common : {...common, presence_summary:{observed_minutes:0,samples:0,expected_samples:0,peak_online:null},
    presence_rows:[],presence_points:buckets.map(b=>({...b,online:null})),agents:responseAgents,total:35,page:Number(p.get('page')),page_size:20,retention_cutoff_at:'2026-09-06T04:00:00+08:00',total_inquiries:0,total_replies:0};
};
let deferDate = '';
let deferExport = false, failExport = false;
const api = (url, options) => {
  const request = {url,options}; requests.push(request);
  if (url.includes('/export.xlsx')) {
    if (deferExport) return new Promise(resolve => request.resolve = resolve);
    if (failExport) return Promise.reject(new Error('导出暂不可用'));
    return Promise.resolve('excel-blob');
  }
  if (deferDate && url.includes('date='+deferDate)) return new Promise(resolve => request.resolve = resolve);
  return Promise.resolve(data(url));
};
const context = vm.createContext({window:{addEventListener(){}}, document:{getElementById:node},Date:ClockDate,Intl,
  URLSearchParams, AbortController, setTimeout, clearTimeout, console});
const tick = async () => {for (let i=0;i<8;i++) await Promise.resolve();};
const change = async (id,value) => {node(id).value=value; node(id).handlers.change(); await tick();};
const last = () => new URL([...requests].reverse().find(r=>!r.url.endsWith('/filters')).url,'http://localhost').searchParams;
"""

CASES = {
    "exports_all_pages_with_applied_filters_and_no_download_after_filter_change": r"""
for (const kind of ['time','agents','quality']) {
  ui.start(kind); await tick(); await change('reportMode','custom');
  await change('reportStartDate','2026-10-01'); await change('reportEndDate','2026-10-02');
  if (kind === 'time') await change('reportChannel','wechat');
  else {node(kind === 'quality' ? 'reportQualityNext' : 'reportNext').handlers.click(); await tick();}
  assert.equal(node('reportExport').disabled,false);
  node('reportExport').handlers.click(); await tick();
  let request=requests.at(-1), url=new URL(request.url,'http://localhost');
  assert.equal(url.pathname,`/admin/api/statistics/${kind}/export.xlsx`);
  assert.equal(url.searchParams.get('start_date'),'2026-10-01');
  assert.equal(url.searchParams.get('end_date'),'2026-10-02');
  assert.equal(url.searchParams.has('channel_key'),kind==='time');
  assert.equal(url.searchParams.has('page'),false); assert.equal(url.searchParams.has('page_size'),false);
  assert.equal(request.options.responseType,'blob');
  assert.equal(downloads.at(-1).filename,`${{time:'时间统计',agents:'坐席细化',quality:'质检统计'}[kind]}_2026-10-01_2026-10-02.xlsx`);
  deferExport=true;
  const count=downloads.length;
  node('reportExport').handlers.click(); await tick();
  request=requests.at(-1);
  assert.equal(node('reportExport').disabled,true);
  node('reportExport').handlers.click(); await tick(); assert.equal(requests.at(-1),request);
  node('reportReset').handlers.click(); await tick();
  assert.equal(request.options.signal.aborted,true);
  request.resolve('old-excel'); await tick();
  assert.equal(downloads.length,count);
  assert.equal(node('reportExport').disabled,false);
  deferExport=false;
}
""",
    "export_errors_restore_button_and_invalid_dates_disable_it": r"""
ui.start('agents'); await tick();
failExport=true;
node('reportExport').handlers.click(); await tick();
assert.equal(node('reportStatus').textContent,'导出暂不可用');
assert.equal(node('reportExport').disabled,false);
assert.equal(downloads.length,0);
failExport=false;
node('reportExport').handlers.click(); await tick();
assert.equal(downloads.length,1);
await change('reportMode','custom');
await change('reportStartDate','2026-10-22');
assert.equal(node('reportExport').disabled,true);
node('reportExport').handlers.click(); await tick(); assert.equal(downloads.length,1);
node('reportReset').handlers.click(); await tick();
deferExport=true; node('reportExport').handlers.click(); await tick();
const pending=requests.at(-1); ui.stop(true);
assert.equal(pending.options.signal.aborted,true);
pending.resolve('old'); await tick(); assert.equal(downloads.length,1);
""",
    "agent_timings_display_round_counts_and_explain_transfer_based_equal_weighting": r"""
responseAgents=[{source_key:'a',account_id:1,agent_id:'7',agent_name:'客服甲',buckets:{},
  first_response_seconds:90,first_response_samples:2,average_response_seconds:82.5,response_samples:3,response_rounds:2}];
ui.start('agents'); await tick();
let table=node('reportTable').innerHTML;
assert.ok(table.includes('1分30秒')); assert.ok(table.includes('1分22.5秒'));
assert.ok(table.includes('有效首响对话 2 通'));
assert.ok(table.includes('有效响应对话 2 通，已完成响应 3 次'));
assert.ok(table.includes('先算每通对话中该坐席的平均响应，再对各通对话等权平均'));
assert.ok(table.includes('用户第一条有效消息'));
assert.ok(node('reportTableSubtitle').textContent.includes('转人工时间'));
assert.ok(node('reportRules').innerHTML.includes('最后一条有效消息'));
assert.ok(!node('reportRules').innerHTML.includes('首次等待从转人工开始'));
responseAgents[0]={...responseAgents[0],first_response_seconds:null,average_response_seconds:null,
  first_response_samples:0,response_samples:0,response_rounds:0};
await ui.refresh(); await tick();
table=node('reportTable').innerHTML;
assert.ok(table.includes('有效响应对话 0 通，已完成响应 0 次'));
assert.ok(!table.includes('NaN')); assert.ok(table.includes('—'));
""",
    "custom_dates_filter_all_three_reports_and_survive_pagination": r"""
for (const kind of ['time','agents','quality']) {
  ui.start(kind); await tick();
  await change('reportMode','custom');
  assert.equal(node('reportDate').disabled,true);
  assert.equal(node('reportStartDate').disabled,false);
  assert.equal(node('reportEndDate').disabled,false);
  assert.equal(node('reportDateField').classList.contains('hidden'),true);
  assert.equal(node('reportStartField').classList.contains('hidden'),false);
  await change('reportStartDate','2025-12-30'); await change('reportEndDate','2026-01-02');
  if (kind === 'time') await change('reportChannel','wechat');
  assert.equal(last().get('mode'),'custom');
  assert.equal(last().get('start_date'),'2025-12-30'); assert.equal(last().get('end_date'),'2026-01-02');
  assert.equal(last().has('date'),false);
  assert.equal(last().has('channel_key'),kind === 'time');
  assert.ok(node('reportStatus').textContent.includes('2025-12-30 至 2026-01-02'));
  if (kind === 'time') {
    assert.ok(node('reportTable').innerHTML.includes('2025-12-31'));
    assert.ok(node('reportTable').innerHTML.includes('2026-01-01'));
    assert.ok(node('reportChart').innerHTML.includes('2026-01-02'));
  } else {
    if (kind === 'quality') assert.ok(node('reportAgents').innerHTML.includes('2025-12-30 至 2026-01-02'));
    node(kind === 'quality' ? 'reportQualityNext' : 'reportNext').handlers.click(); await tick();
    assert.equal(last().get('page'),'2');
    assert.equal(last().get('start_date'),'2025-12-30'); assert.equal(last().get('end_date'),'2026-01-02');
  }
}
""",
    "invalid_custom_dates_do_not_query_and_can_be_corrected": r"""
ui.start('time'); await tick(); await change('reportMode','custom');
let before=requests.length;
await change('reportStartDate','2026-10-22');
assert.equal(requests.length,before);
assert.equal(node('reportEndDate').validationMessage,'结束日期不能早于起始日期。');
await change('reportEndDate','2026-10-22');
assert.ok(requests.length>before);
assert.equal(last().get('start_date'),'2026-10-22'); assert.equal(last().get('end_date'),'2026-10-22');
assert.equal(node('reportEndDate').validationMessage,'');
before=requests.length; await change('reportEndDate','');
assert.equal(requests.length,before);
await change('reportEndDate','2100-01-01'); assert.equal(requests.length,before);
await change('reportEndDate','2026-10-23'); assert.ok(requests.length>before);
""",
    "custom_switches_to_quick_periods_and_reset_returns_latest_day_for_all_reports": r"""
for (const kind of ['time','agents','quality']) {
  ui.start(kind); await tick(); await change('reportMode','custom');
  await change('reportStartDate','2024-02-28'); await change('reportEndDate','2024-03-01');
  for (const mode of ['year','month','day']) {
    await change('reportMode',mode);
    assert.equal(node('reportDate').disabled,false);
    assert.equal(node('reportStartDate').disabled,true); assert.equal(node('reportEndDate').disabled,true);
    assert.equal(last().get('mode'),mode);
    assert.equal(last().has('start_date'),false); assert.equal(last().has('end_date'),false);
    assert.equal(node('reportDate').value,{year:'2026',month:'2026-10',day:'2026-10-21'}[mode]);
  }
  await change('reportMode','custom'); await change('reportEndDate','2024-02-27');
  node('reportReset').handlers.click(); await tick();
  assert.equal(last().get('mode'),'day'); assert.equal(last().get('date'),'2026-10-21');
  assert.equal(last().has('start_date'),false); assert.equal(last().has('end_date'),false);
  if (kind !== 'time') assert.equal(last().get('page'),'1');
  assert.equal(node('reportEndDate').validationMessage,'');
  assert.equal(node('reportStartDate').value,'2026-10-21'); assert.equal(node('reportEndDate').value,'2026-10-21');
}
""",
    "reset_aborts_old_custom_request_and_ignores_its_response": r"""
ui.start('time'); await tick(); await change('reportMode','custom');
deferDate='2026-10-17'; await change('reportStartDate',deferDate);
const old=requests.at(-1);
node('reportReset').handlers.click(); await tick();
assert.equal(old.options.signal.aborted,true);
old.resolve(data(old.url)); await tick();
assert.equal(last().get('mode'),'day');
assert.ok(node('reportStatus').textContent.includes('2026-10-21'));
assert.ok(!node('reportStatus').textContent.includes(' 至 '));
""",
    "quality_pagination_reset_and_zero_denominators": r"""
ui.start('quality'); await tick();
assert.ok(node('reportStatus').textContent.includes('质检统计'));
assert.equal(node('reportTableTitle').textContent,'人工质检表');
assert.equal(node('reportAgentTitle').textContent,'AI质检报表');
assert.ok(node('reportAgents').innerHTML.includes('—'));
assert.ok(!node('reportAgents').innerHTML.includes('NaN'));
node('reportQualityNext').handlers.click(); await tick();
assert.equal(last().get('page'),'2');
await change('reportMode','year'); await change('reportDate','2024');
await change('reportMode','month');
assert.equal(node('reportDate').value,'2026-10');
node('reportReset').handlers.click(); await tick();
assert.equal(last().get('page'),'1'); assert.equal(last().get('mode'),'day');
assert.equal(last().get('date'),'2026-10-21');
assert.equal(last().has('channel_key'),false);
""",
    "year_to_month_and_day_default_to_today": r"""
ui.start('time'); await tick();
await change('reportMode','year'); await change('reportDate','2025');
assert.equal(last().get('date'),'2025-01-01');
await change('reportMode','month');
assert.equal(node('reportDate').value,'2026-10');
assert.equal(last().get('mode'),'month');
await change('reportMode','year'); await change('reportDate','2024');
await change('reportMode','day');
assert.equal(node('reportDate').value,'2026-10-21');
assert.equal(last().get('date'),'2026-10-21');
""",
    "month_to_day_does_not_reuse_first_of_month": r"""
ui.start('time'); await tick();
await change('reportMode','month'); await change('reportDate','2024-02');
await change('reportMode','day');
assert.equal(node('reportDate').value,'2026-10-21');
assert.equal(last().get('date'),'2026-10-21');
""",
    "reset_clears_channel_and_fetches_fresh_beijing_day": r"""
ui.start('time'); await tick();
await change('reportMode','year'); await change('reportDate','2024');
await change('reportChannel','wechat');
assert.equal(last().get('channel_key'),'wechat');
now=Date.parse('2026-10-21T16:30:00Z');
const before=requests.length;
node('reportReset').handlers.click(); await tick();
assert.ok(requests.length>before);
assert.equal(node('reportDate').type,'date');
assert.equal(node('reportMode').value,'day');
assert.equal(node('reportDate').value,'2026-10-22');
assert.equal(node('reportChannel').value,'');
assert.equal(last().get('mode'),'day'); assert.equal(last().get('date'),'2026-10-22');
assert.equal(last().has('source_key'),false); assert.equal(last().has('channel_key'),false);
""",
    "agent_reset_returns_to_first_page_and_ignores_old_result": r"""
ui.start('agents'); await tick();
node('reportNext').handlers.click(); await tick();
assert.equal(last().get('page'),'2');
deferDate='2026-10-17'; await change('reportDate',deferDate);
const old=requests.at(-1);
node('reportReset').handlers.click(); await tick();
assert.equal(old.options.signal.aborted,true);
assert.equal(last().get('page'),'1'); assert.equal(last().get('mode'),'day');
assert.equal(last().get('date'),'2026-10-21');
old.resolve(data(old.url)); await tick();
assert.ok(node('reportStatus').textContent.includes('2026-10-21'));
assert.ok(!node('reportStatus').textContent.includes('2026-10-17'));
""",
}


@pytest.mark.skipif(NODE is None, reason="Node.js is required for UI behavior checks")
@pytest.mark.parametrize("case", CASES)
def test_report_date_switching_and_reset(case):
    program = HARNESS + "\nvm.runInContext(" + json.dumps(SCRIPT.read_text(encoding="utf-8")) + ", context);\n"
    program += "const ui=context.window.ICSReports.create({api,escapeHtml:s=>String(s??''),getToken:()=> 'token',isActive:()=>true,download:(blob,filename)=>downloads.push({blob,filename})});\n"
    program += "(async()=>{\n" + CASES[case] + "\n})().catch(error=>{console.error(error);process.exitCode=1});"
    result = subprocess.run([NODE, "-"], input=program, text=True, encoding="utf-8", capture_output=True, timeout=15)
    assert result.returncode == 0, result.stdout + result.stderr
