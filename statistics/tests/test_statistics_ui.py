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
const nodes = new Map(), requests = [], downloads = [], archives = [];
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
  click() {if (!this.disabled) this.handlers.click?.();}
  addEventListener(event, handler) {this.handlers[event] = handler;}
  setAttribute(name, value) {this[name] = String(value);}
  setCustomValidity(message) {this.validationMessage = message;}
  reportValidity() {return [...nodes.values()].every(n => n.disabled || (!n.validationMessage && (!n.required ||
    (n.value && (!n.min || n.value >= n.min) && (!n.max || n.value <= n.max)))));}
}
const node = id => {if (!nodes.has(id)) nodes.set(id, new Element(id)); return nodes.get(id);};
let responseAgents = [];
let data = url => {
  const parsed = new URL(url,'http://localhost'), p = parsed.searchParams;
  if (parsed.pathname.endsWith('/users/detail')) return {items:[{assigned_at:'2026-10-20T09:00:05+08:00',conversation_id:12,round_no:1,round_id:'r1',agent_id:'7',agent_name:'客服1',channel_key:'wechat'},
    {assigned_at:'2026-10-20T10:00:00+08:00',conversation_id:22,round_no:2,round_id:'r2',agent_id:'8',agent_name:'客服2',channel_key:'wechat'}]};
  if (parsed.pathname.endsWith('/users')) return {mode:p.get('mode'),date:p.get('date')||p.get('start_date'),start_date:'2026-10-20',end_date:'2026-10-20',
    start:'2026-10-20T00:00:00+08:00',end:'2026-10-21T00:00:00+08:00',start_label:'2026-10-20 00:00:00',end_label:'2026-10-20 23:59:59',
    generated_at:'2026-10-21T00:30:00+08:00',as_of:'2026-10-20T16:30:00+00:00',buckets:[],total_assignments:3,users_total:2,unidentified_assignments:0,
    ranked_assignments:3,ranked_share:100,before_earliest:false,earliest_assigned_at:'2026-10-01T00:00:00+08:00',total:2,page:Number(p.get('page')),page_size:20,
    users:[{rank:1,source_key:'a',account_id:1,contact_id:'42',customer_id:'customer:x',display_name:'客户甲',channels:['wechat'],assignments:2,conversations:2,daily_count:24,weekly_count:60,
      first_assigned_at:'2026-10-20T09:00:05+08:00',last_assigned_at:'2026-10-20T10:00:00+08:00',blacklist_status:'active'},
      {rank:2,source_key:'a',account_id:1,contact_id:'99',customer_id:'customer:y',display_name:null,channels:['wechat'],assignments:1,conversations:1,daily_count:0,weekly_count:null,
      first_assigned_at:'2026-10-20T08:00:00+08:00',last_assigned_at:'2026-10-20T08:00:00+08:00',blacklist_status:null}]};
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
let api = (url, options) => {
  const request = {url,options}; requests.push(request);
  if (url.includes('/export.xlsx')) {
    if (deferExport) return new Promise(resolve => request.resolve = resolve);
    if (failExport) return Promise.reject(new Error('导出暂不可用'));
    return Promise.resolve('excel-blob');
  }
  if (deferDate && url.includes('date='+deferDate)) return new Promise(resolve => request.resolve = resolve);
  return Promise.resolve(data(url));
};
let timerNow=0, timerId=0, token='token', active=true;
const timers=new Map();
const setTimer=(callback,delay)=>{const id=++timerId;timers.set(id,{callback,at:timerNow+delay});return id;};
const clearTimer=id=>timers.delete(id);
const flush = async () => {for (let i=0;i<8;i++) await Promise.resolve();};
const advance = async ms => {
  const until=timerNow+ms;
  while (true) {
    const next=[...timers].filter(([id,t])=>t.at<=until).sort((a,b)=>a[1].at-b[1].at)[0];
    if (!next) break;
    timerNow=next[1].at; timers.delete(next[0]); next[1].callback(); await flush();
  }
  timerNow=until; await flush();
};
const context = vm.createContext({window:{addEventListener(){}}, document:{getElementById:node},Date:ClockDate,Intl,
  URLSearchParams, AbortController, setTimeout:setTimer, clearTimeout:clearTimer, console});
const tick = async () => {await flush();await advance(400);};
const change = async (id,value) => {
  if (id === 'reportMode') node(`reportMode-${value}`).handlers.click();
  else {node(id).value=value; node(id).handlers.change();}
  await tick();
};
const last = () => new URL([...requests].reverse().find(r=>!r.url.endsWith('/filters')).url,'http://localhost').searchParams;
"""

PAGE_INPUTS = json.loads((Path(__file__).resolve().parents[2] / "quality/tests/page_inputs.json").read_text(encoding="utf-8"))
PAGINATION_CASE = r"""
for(const kind of ['agents','quality','users']) {
 for(const [raw,expected] of INPUTS) {
  for(const event of ['click','keydown']) {
   const original=data;
   data=url=>({...original(url),total:200,page:3});
   ui.start(kind); await tick();
   const prefix=kind==='agents'?'report':'reportQuality';
   data=url=>({...original(url),total:200});
   const before=requests.length;
   node(prefix+'JumpInput').value=raw;
   let prevented=false;
   if(event==='click') node(prefix+'Jump').handlers.click();
   else node(prefix+'JumpInput').handlers.keydown({key:'Enter',preventDefault(){prevented=true;}});
   if(event==='keydown') assert.equal(prevented,true);
   assert.equal(requests.length-before,expected===null||expected===3?0:1,kind+' '+raw);
   if(expected===null) assert.equal(node(prefix+'JumpInput').value,raw);
   else assert.equal(String(node(prefix+'JumpInput').value),String(expected));
   if(expected!==null&&expected!==3) {
    for(const suffix of ['First','Last','Prev','Next','Jump','JumpInput']) assert.equal(node(prefix+suffix).disabled,true,prefix+suffix);
    node(prefix+'Next').handlers.click(); node(prefix+'Jump').handlers.click();
    assert.equal(requests.length-before,1);
   }
   await tick(); data=original;
  }
 }
}
""".replace('INPUTS',json.dumps(PAGE_INPUTS))

CASES = {
    "late_filter_options_preserve_failed_page_error": r"""
const savedApi=api,resolvers=[];let fail=false;
api=(url,options)=>{
 if(url.endsWith('/filters'))return new Promise(resolve=>resolvers.push(()=>resolve(data(url))));
 if(fail)return Promise.reject(new Error('测试分页失败'));
 return savedApi(url,options);
};
ui.start('agents');await tick();fail=true;
node('reportNext').handlers.click();await tick();
assert.match(node('reportStatus').textContent,/测试分页失败/);
assert.match(node('reportPageInfo').textContent,/查询未完成/);
for(const resolve of resolvers)resolve();await tick();
assert.match(node('reportStatus').textContent,/测试分页失败/);
assert.match(node('reportPageInfo').textContent,/查询未完成/);
""",
    "stale_finally_cannot_unlock_new_query_filter_change_can_replace_inflight_page_and_failure_can_retry": r"""
for(const kind of ['agents','quality','users']) {
 const original=data;data=url=>({...original(url),total:200});
 ui.start(kind);await tick();
 const prefix=kind==='agents'?'report':'reportQuality';
 deferDate=last().get('date');
 node(prefix+'Next').handlers.click();const old=requests.at(-1);
 node('reportDate').value='2026-10-20';node('reportDate').handlers.change();await tick();
 assert.equal(old.options.signal.aborted,true);assert.equal(last().get('page'),'1');
 // 新请求仍在读取时，旧请求完成不能解锁。
 deferDate='2026-10-19';node('reportDate').value=deferDate;node('reportDate').handlers.change();await tick();
 const newer=requests.at(-1);old.resolve(data(old.url));await tick();
 for(const suffix of ['First','Last','Prev','Next','Jump','JumpInput'])assert.equal(node(prefix+suffix).disabled,true);
 newer.resolve(data(newer.url));await tick();
 deferDate='';const savedApi=api;api=(url,options)=>url.includes('/filters')?savedApi(url,options):Promise.reject(new Error('测试失败'));
 node(prefix+'Next').handlers.click();await tick();
 assert.match(node('reportStatus').textContent,/测试失败/);
 assert.match(node(prefix+'PageInfo').textContent,/1 \/ 10/);
 assert.equal(node(prefix+'Jump').disabled,false);
 api=savedApi;const before=requests.length;node(prefix+'Next').handlers.click();await tick();assert.equal(requests.length,before+1);
 data=original;
}
""",
    "shrunk_total_corrects_once": r"""
for(const kind of ['agents','quality','users']) {
 const original=data;data=url=>({...original(url),total:200});ui.start(kind);await tick();
 const prefix=kind==='agents'?'report':'reportQuality';let calls=0;
 data=url=>{calls++;return {...original(url),total:calls===1?40:20};};
 const before=requests.length;node(prefix+'JumpInput').value='10';node(prefix+'Jump').handlers.click();await tick();
 assert.equal(requests.length,before+2);assert.equal(last().get('page'),'2');
 assert.match(node('reportStatus').textContent,/页数已变化，请重试/);
 assert.match(node(prefix+'PageInfo').textContent,/1 \/ 10/);
 assert.equal(node(prefix+'JumpInput').disabled,false);data=original;
}
""",
    "jump_rejects_fraction_and_exponent_and_rapid_click_and_enter_send_one_request": PAGINATION_CASE,
    "week_buttons_query_export_and_reset_for_all_reports": r"""
for (const kind of ['time','agents','quality','users']) {
  ui.start(kind); await tick();
  await change('reportMode','week');
  assert.equal(last().get('mode'),'custom');
  assert.equal(last().get('start_date'),'2026-10-19');
  assert.equal(last().get('end_date'),'2026-10-25');
  assert.equal(last().has('date'),false);
  assert.equal(last().has('start_time'),false);
  assert.equal(node('reportWeekRange').textContent,'2026-10-19 至 2026-10-25');
  assert.equal(node('reportWeekRange').classList.contains('hidden'),false);
  for (const mode of ['day','week','month','year','custom']) {
    assert.equal(node(`reportMode-${mode}`)['aria-pressed'],String(mode==='week'));
  }
  if (kind !== 'time') assert.equal(last().get('page'),'1');
  await change('reportDate','2026-01-01');
  assert.equal(last().get('start_date'),'2025-12-29');
  assert.equal(last().get('end_date'),'2026-01-04');
  node('reportExport').handlers.click(); await tick();
  assert.equal(last().get('start_date'),'2025-12-29');
  assert.equal(last().get('end_date'),'2026-01-04');
  assert.equal(last().has('page'),false);
  node('reportReset').handlers.click(); await tick();
  assert.equal(last().get('mode'),'day');
  assert.equal(node('reportMode-day')['aria-pressed'],'true');
  assert.equal(node('reportMode-week')['aria-pressed'],'false');
  assert.equal(node('reportWeekRange').classList.contains('hidden'),true);
}
""",
    "week_uses_beijing_calendar_and_keeps_custom_dates": r"""
ui.start('time'); await tick();
await change('reportMode','custom');
await change('reportStartDate','2024-02-28'); await change('reportEndDate','2024-03-01');
now=Date.parse('2026-10-25T16:30:00Z');
await change('reportMode','week');
assert.equal(last().get('start_date'),'2026-10-26');
assert.equal(last().get('end_date'),'2026-11-01');
await change('reportDate','2024-03-03');
assert.equal(last().get('start_date'),'2024-02-26');
assert.equal(last().get('end_date'),'2024-03-03');
await change('reportDate','2000-01-01');
assert.equal(last().get('start_date'),'2000-01-01');
assert.equal(last().get('end_date'),'2000-01-02');
await change('reportDate','2099-12-31');
assert.equal(last().get('start_date'),'2099-12-28');
assert.equal(last().get('end_date'),'2099-12-31');
await change('reportMode','custom');
assert.equal(last().get('start_date'),'2024-02-28');
assert.equal(last().get('end_date'),'2024-03-01');
""",
    "changes_abort_immediately_and_send_only_after_400ms": r"""
ui.start('time'); await tick();
deferDate='2026-10-17'; await change('reportDate',deferDate);
const old=requests.at(-1), before=requests.length;
node('reportDate').value='2026-10-18'; node('reportDate').handlers.change();
assert.equal(old.options.signal.aborted,true);
assert.equal(node('reportExport').disabled,true);
assert.ok(node('reportTable').innerHTML.includes('正在准备查询'));
old.resolve(data(old.url)); await flush();
assert.equal(node('reportExport').disabled,true);
assert.ok(!node('reportStatus').textContent.includes('2026-10-17'));
await advance(100);
node('reportTimeToggle').handlers.click();
node('reportStartHour').value='09'; node('reportStartHour').handlers.change();
node('reportChannel').value='wechat'; node('reportChannel').handlers.change();
await advance(399); assert.equal(requests.length,before);
await advance(1); assert.equal(requests.length,before+1);
assert.equal(last().get('date'),'2026-10-18');
assert.equal(last().get('channel_key'),'wechat');
assert.equal(last().get('start_time'),'09:00');
assert.equal(node('reportExport').disabled,false);
""",
    "scheduled_load_is_cleared_on_stop_submit_reset_and_pagination": r"""
ui.start('agents'); await tick();
const schedule=()=>node('reportDate').handlers.change();
let before=requests.length;
schedule(); ui.stop(); await advance(500); assert.equal(requests.length,before);
schedule(); active=false; await advance(500); assert.equal(requests.length,before); active=true;
schedule(); token=null; await advance(500); assert.equal(requests.length,before); token='token';
schedule(); node('reportFilters').handlers.submit({preventDefault(){}}); await flush();
assert.equal(requests.length,before+1);
await advance(500); assert.equal(requests.length,before+1);
before=requests.length;
schedule(); node('reportReset').handlers.click(); await flush();
assert.equal(requests.length,before+1);
await advance(500); assert.equal(requests.length,before+1);
before=requests.length;
node('reportNext').handlers.click(); await flush(); assert.equal(requests.length,before+1);
assert.equal(last().get('page'),'2');
""",
    "condition_change_aborts_export_before_the_new_report": r"""
ui.start('time'); await tick();
deferExport=true; node('reportExport').handlers.click(); await flush();
const pending=requests.at(-1), before=requests.length;
node('reportDate').handlers.change();
assert.equal(pending.options.signal.aborted,true);
pending.resolve('stale-export'); await flush();
assert.equal(downloads.length,0); assert.equal(node('reportExport').disabled,true);
node('reportExport').handlers.click(); await flush(); assert.equal(requests.length,before);
await advance(400); assert.equal(requests.length,before+1);
""",
    "custom_span_limit_blocks_queries_and_recovers_for_all_reports": r"""
for (const kind of ['time','agents','quality','users']) {
  ui.start(kind); await tick(); await change('reportMode','custom');
  node('reportStartDate').value='2026-01-01'; node('reportEndDate').value='2027-01-01';
  await ui.refresh(); await tick();
  assert.equal(node('reportEndDate').validationMessage,'');
  assert.equal(last().get('end_date'),'2027-01-01');
  assert.equal(node('reportExport').disabled,false);
  const before=requests.length;
  await change('reportEndDate','2027-01-02');
  assert.equal(requests.length,before,kind);
  assert.equal(node('reportEndDate').validationMessage,'自定义时间段最多 366 天（含起止日期），请缩小范围。');
  assert.equal(node('reportExport').disabled,true);
  await change('reportEndDate','2026-12-31');
  assert.ok(requests.length>before);
  assert.equal(node('reportEndDate').validationMessage,'');
  assert.equal(node('reportExport').disabled,false);
  node('reportStartDate').value='2028-01-01'; node('reportEndDate').value='2028-12-31';
  await ui.refresh(); await tick();
  assert.equal(node('reportEndDate').validationMessage,'');
  assert.equal(last().get('end_date'),'2028-12-31');
  await change('reportEndDate','2029-01-01');
  assert.ok(node('reportEndDate').validationMessage.includes('最多 366 天'));
  await change('reportMode','year');
  assert.equal(node('reportEndDate').validationMessage,'');
  assert.equal(last().get('mode'),'year');
}
""",
    "every_report_count_opens_the_archive_with_the_same_metric_window_and_seat": r"""
responseAgents=[{source_key:'a',account_id:1,agent_id:'7',agent_name:'客服1',buckets:{},low_rating:2,lost_inquiries:1,
    inquiries:3,dialogues:1,assigned_inquiries:2,first_response_samples:1,first_response_seconds:30,rated_inquiries:2,average_rating:3},
  {source_key:'a',account_id:1,agent_id:'8',agent_name:'客服2',buckets:{},low_rating:0,lost_inquiries:0}];
ui.start('agents'); await tick();
const table=node('reportTable').innerHTML;
const ids = [...table.matchAll(/data-drill="(\d+)"/g)].map(m => m[1]);
assert.ok(ids.length >= 7, 'every non-zero seat count is a link: ' + ids.length);
assert.ok(!table.includes('客服2</span><span class="report-seat-id">a / 1 / #8</span></td><td><button'), 'zero counts stay plain text');
const click = id => node('reportTable').handlers.click({target:{closest:()=>({dataset:{drill:id}})}});
for (const id of ids) click(id);
const metrics = archives.map(a => a.metric);
for (const metric of ['first_response_count','rated_inquiries','dialogues','assigned_inquiries','inquiries','low_rating','lost_inquiries'])
  assert.ok(metrics.includes(metric), metric);
const lost = archives.find(a => a.metric === 'lost_inquiries');
assert.deepEqual({...lost.seat}, {source_key:'a',account_id:1,agent_id:'7',agent_name:'客服1'});
assert.equal(lost.count, 1);
assert.equal(archives.find(a => a.metric === 'low_rating').count, 2);
""",
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
    "agent_timings_display_round_counts_and_explain_transfer_based_wait_weighting": r"""
responseAgents=[{source_key:'a',account_id:1,agent_id:'7',agent_name:'客服甲',buckets:{},
  first_response_seconds:90,first_response_samples:2,average_response_seconds:82.5,response_samples:3,response_rounds:2}];
ui.start('agents'); await tick();
let table=node('reportTable').innerHTML;
assert.ok(table.includes('1分30秒')); assert.ok(table.includes('1分22.5秒'));
assert.ok(table.includes('有效首响对话 2 通'));
assert.ok(table.includes('有效响应对话 2 通，已完成响应 3 次'));
assert.ok(table.includes('响应总时长相加 ÷ 响应总次数'));
assert.ok(table.includes('连续发言的最后一条有效消息'));
assert.ok(table.includes('不算首响'));
assert.ok(table.includes('首响超过600秒不计入统计，600秒保留'));
assert.ok(table.includes('单次响应超过600秒不计入时长及样本数，600秒保留'));
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
assert.equal(node('reportMode-day')['aria-pressed'],'true');
assert.equal(node('reportDate').value,'2026-10-22');
assert.equal(node('reportChannel').value,'');
assert.equal(last().get('mode'),'day'); assert.equal(last().get('date'),'2026-10-22');
assert.equal(last().has('source_key'),false); assert.equal(last().has('channel_key'),false);
""",
    "user_ranking_refines_time_traces_each_handoff_and_opens_the_user_in_the_archive": r"""
ui.start('users'); await tick();
assert.match(node('reportTable').innerHTML,/近24小时计数/);
assert.match(node('reportTable').innerHTML,/当前周次计数/);
assert.doesNotMatch(node('reportTable').innerHTML,/涉及会话数/);
assert.match(node('reportTable').innerHTML,/<td>24<\/td><td>60<\/td>/);
assert.match(node('reportTable').innerHTML,/<td>0<\/td><td>—<\/td>/);
let q=last();
assert.equal(new URL(requests.at(-1).url,'http://localhost').pathname,'/admin/api/statistics/users');
assert.equal(q.get('page'),'1'); assert.equal(q.has('start_time'),false);
assert.equal(node('reportTimeField').classList.contains('hidden'),false);
const table=node('reportTable').innerHTML;
assert.ok(table.includes('客户甲') && table.includes('拉黑中') && table.includes('联系人 #99'));
assert.ok(table.includes('2026-10-20 09:00:05'), 'times shown to the second in Beijing time');
await change('reportChannel','wechat');
node('reportTimeToggle').handlers.click(); await tick();
await change('reportStartHour','09'); await change('reportEndHour','18'); await change('reportEndMinute','59');
q=last();
assert.equal(q.get('start_time'),'09:00'); assert.equal(q.get('end_time'),'18:59'); assert.equal(q.get('channel_key'),'wechat');
// 溯源：展开某用户的逐条明细，条数与次数一致；每条可跳到对应轮次。
const traceButton={dataset:{trace:'a|1|42'}};
await node('reportTable').handlers.click({target:{closest:sel=>sel==='[data-trace]'?traceButton:null}}); await tick();
const detail=new URL(requests.at(-1).url,'http://localhost');
assert.equal(detail.pathname,'/admin/api/statistics/users/detail');
assert.equal(detail.searchParams.get('contact_id'),'42'); assert.equal(detail.searchParams.get('start_time'),'09:00');
assert.ok(!detail.searchParams.has('page'));
const expanded=node('reportTable').innerHTML;
assert.ok(expanded.includes('会话 12 · 第 1 轮') && expanded.includes('客服2') && !expanded.includes('不一致'));
const ids=[...expanded.matchAll(/data-drill="(\d+)"/g)].map(m=>m[1]);
for (const id of ids) node('reportTable').handlers.click({target:{closest:sel=>sel==='[data-drill]'?{dataset:{drill:id}}:null}});
const user=archives.find(a=>a.user && !a.round), round=archives.find(a=>a.round);
assert.equal(user.metric,'assigned_inquiries'); assert.equal(user.count,2); assert.equal(user.user.customer_id,'customer:x');
assert.equal(user.channel,'wechat'); assert.equal(user.period,'2026-10-20 00:00:00 至 2026-10-20 23:59:59');
assert.deepEqual({...round.round},{conversation_id:12,round_no:1,round_id:'r1'});
node('reportExport').handlers.click(); await tick();
const exported=new URL(requests.at(-1).url,'http://localhost');
assert.equal(exported.pathname,'/admin/api/statistics/users/export.xlsx');
assert.equal(exported.searchParams.get('end_time'),'18:59'); assert.ok(!exported.searchParams.has('page'));
assert.match(downloads.at(-1).filename,/^用户统计_/);
node('reportReset').handlers.click(); await tick();
assert.equal(last().has('start_time'),false); assert.equal(node('reportTimeDetail').classList.contains('hidden'),true);
await change('reportMode','month');
assert.equal(node('reportTimeField').classList.contains('hidden'),true); assert.equal(last().has('start_time'),false);
""",
    "original_reports_offer_whole_hour_time_detail": r"""
for (const kind of ['time','agents','quality']) {
  ui.start(kind); await tick();
  assert.equal(node('reportTimeField').classList.contains('hidden'),false, kind);
  assert.equal(node('reportStartMinute').disabled,true, kind); assert.equal(node('reportEndMinute').disabled,true, kind);
}
ui.start('time'); await tick();
node('reportTimeToggle').handlers.click(); await tick();
await change('reportStartHour','09'); await change('reportEndHour','18');
let q=last();
assert.equal(new URL(requests.at(-1).url,'http://localhost').pathname,'/admin/api/statistics/time');
assert.equal(q.get('start_time'),'09:00'); assert.equal(q.get('end_time'),'18:59');
ui.start('users'); await tick();
assert.equal(node('reportStartMinute').disabled,false); assert.equal(last().get('start_time'),'09:00');
await change('reportMode','month');
assert.equal(node('reportTimeField').classList.contains('hidden'),true); assert.equal(last().has('start_time'),false);
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
    program += "const ui=context.window.ICSReports.create({api:(...args)=>api(...args),escapeHtml:s=>String(s??''),getToken:()=>token,isActive:()=>active,download:(blob,filename)=>downloads.push({blob,filename}),openArchive:preset=>archives.push(preset)});\n"
    program += "(async()=>{\n" + CASES[case] + "\n})().catch(error=>{console.error(error);process.exitCode=1});"
    result = subprocess.run([NODE, "-"], input=program, text=True, encoding="utf-8", capture_output=True, timeout=15)
    assert result.returncode == 0, result.stdout + result.stderr
