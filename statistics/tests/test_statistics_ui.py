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
const nodes = new Map(), requests = [];
let now = Date.parse('2026-10-20T16:30:00Z');
class ClockDate extends Date {
  constructor(...args) {super(...(args.length ? args : [now]));}
  static now() {return now;}
}
class Element {
  constructor(id) {
    this.id = id; this.value = ''; this.type = id === 'reportDate' ? 'date' : '';
    this.innerHTML = ''; this.textContent = ''; this.disabled = false; this.handlers = {};
    this.clientWidth = 800; this.clientHeight = 180;
    this.classList = {toggle() {}, add() {}, remove() {}};
  }
  addEventListener(event, handler) {this.handlers[event] = handler;}
  reportValidity() {return true;}
}
const node = id => {if (!nodes.has(id)) nodes.set(id, new Element(id)); return nodes.get(id);};
const data = url => {
  const parsed = new URL(url,'http://localhost'), p = parsed.searchParams;
  if (parsed.pathname.endsWith('/filters')) return {sources:[{source_key:'a',account_id:1}],channels:[{key:'wechat',name:'微信'}]};
  const common = {date:p.get('date'),generated_at:'2026-10-21T00:30:00+08:00',buckets:[],rows:[],
    totals:{incoming:0,ai_only:0,handoff:0,rejected:0,effective_human:0,low_rating:0},channels:{},processing:{pending_jobs:0}};
  if (parsed.pathname.endsWith('/quality')) return {...common,agents:[],total:35,page:Number(p.get('page')),page_size:20,
    total_inquiries:0,total_failed_inquiries:0,human_failure_rate:null,
    ai:{total_rounds:0,handoff:0,ai_only:0,handoff_failed:0,failed_rounds:0,failure_rate:null}};
  return parsed.pathname.endsWith('/time') ? common : {...common, presence_summary:{observed_minutes:0,samples:0,expected_samples:0,peak_online:null},
    presence_rows:[],presence_points:[],agents:[],total:35,page:Number(p.get('page')),page_size:20,retention_cutoff_at:'2026-09-06T04:00:00+08:00',total_inquiries:0,total_replies:0};
};
let deferDate = '';
const api = (url, options) => {
  const request = {url,options}; requests.push(request);
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
    program += "const ui=context.window.ICSReports.create({api,escapeHtml:s=>String(s??''),getToken:()=> 'token',isActive:()=>true});\n"
    program += "(async()=>{\n" + CASES[case] + "\n})().catch(error=>{console.error(error);process.exitCode=1});"
    result = subprocess.run([NODE, "-"], input=program, text=True, encoding="utf-8", capture_output=True, timeout=15)
    assert result.returncode == 0, result.stdout + result.stderr
