"""Run the archive page's actual JavaScript against controlled browser responses."""

from __future__ import annotations

import json
from html.parser import HTMLParser
from pathlib import Path
import re
import shutil
import subprocess

import pytest


ADMIN_PAGE = Path(__file__).resolve().parents[2] / "ics_agent" / "app" / "admin.html"
NODE = shutil.which("node")

HARNESS = r"""
const assert = require('node:assert/strict');
const vm = require('node:vm');
const nodes = new Map();
const defaults = {qualityAgentScope: 'responsible', qualityTimeField: 'started_at', qualityMessageScope: 'all', presenceCheckTime: '09:00'};
const decode = value => value.replace(/&quot;/g, '"').replace(/&#039;/g, "'").replace(/&lt;/g, '<').replace(/&gt;/g, '>').replace(/&amp;/g, '&');
class Element {
  constructor(id) {
    this.id = id; this._html = ''; this.value = defaults[id] || ''; this.textContent = ''; this.disabled = false;
    this.dataset = {}; this.handlers = {}; this.attributes = {}; this.style = {}; this.checked = false;
    const classes = new Set(id === 'presenceManagement' ? ['hidden'] : []);
    this.classList = {add: name => classes.add(name), remove: name => classes.delete(name), contains: name => classes.has(name), toggle(name, value) {value ? classes.add(name) : classes.delete(name);}};
  }
  set innerHTML(value) {this._html = value; if (['qualityAgent', 'qualityInbox', 'presenceAgent'].includes(this.id)) this.value = this.options[0]?.value || '';}
  get innerHTML() {return this._html;}
  get options() {return [...this._html.matchAll(/<option value="([^"]*)"/g)].map(match => ({value: decode(match[1])}));}
  addEventListener(name, handler) {this.handlers[name] = handler;}
  setAttribute(name, value) {this.attributes[name] = value;}
  removeAttribute(name) {delete this.attributes[name];}
  querySelectorAll(selector) {
    if (this.id === 'qualityFilterForm' && selector === 'input, select') return [...Object.keys(fields),'qualityInbox','qualityAgent'].map(node);
    return [];
  }
  scrollIntoView() {}
  focus() {this.focused = true;}
  reset() {for (const [id, node] of nodes) if (Object.hasOwn(fields,id) || ['qualityInbox','qualityAgent'].includes(id)) node.value = defaults[id] || '';}
}
const node = id => {if (!nodes.has(id)) nodes.set(id, new Element(id)); return nodes.get(id);};
const fields = {
 qualityCustomer: 'customer_query', qualityKeyword: 'keyword',
 qualityAiReviewStatus: 'ai_review_status', qualityHumanReviewStatus: 'human_review_status',
 qualityReception: 'reception_type', qualityAgentScope: 'agent_scope', qualityConversation: 'conversation_id',
 qualityRoundNo: 'round_no', qualityTimeField: 'time_field', qualityStart: 'start',
 qualityEnd: 'end', qualityRatingStatus: 'rating_status', qualityRating: 'rating', qualityMessageScope: 'message_scope'
};
class BrowserFormData {
  constructor() {this.values = Object.entries(fields).map(([id, name]) => [name, node(id).value]);}
  [Symbol.iterator]() {return this.values[Symbol.iterator]();}
}
const requests = [], downloads = [], confirmations = [], confirmationAnswers = [];
const overviewRequests = [], presenceRequests = [], dailyRequests = [], timelineRequests = [], managementRequests = [], removalRequests = [], roundRequests = [], timers = new Map(), documentEvents = {};
let nextTimer = 1;
let browserNow = Date.parse('2026-10-16T08:00:00Z');
class BrowserDate extends Date {
 constructor(...args) {super(...(args.length ? args : [browserNow]));}
 static now() {return browserNow;}
}
class BrowserURL extends URL {
  static createObjectURL() {return 'blob:archive-preview';}
  static revokeObjectURL() {}
}
const context = vm.createContext({
 document: {getElementById: node, querySelectorAll: () => [], title: '', hidden:false, addEventListener(name, handler) {documentEvents[name] = handler;}, body: {appendChild() {}},
   createElement() {return {href:'',download:'',click() {downloads.push({url:this.href,filename:this.download});},remove() {}};}},
 window: {location: {origin: 'http://localhost:3001', hash: '#quality'}, addEventListener() {},
   confirm(message) {confirmations.push(message); return confirmationAnswers.length ? confirmationAnswers.shift() : true;}},
 localStorage: {getItem() {return null;}, setItem() {}, removeItem() {}},
 history: {replaceState() {}}, URL: BrowserURL, URLSearchParams, Headers, AbortController, DOMException, Intl, Date: BrowserDate,
 FormData: BrowserFormData, setTimeout() {}, clearTimeout() {},
 setInterval(callback, milliseconds) {const id=nextTimer++; timers.set(id,{callback,milliseconds}); return id;}, clearInterval(id) {timers.delete(id);},
 fetch: (url, options) => new Promise((resolve, reject) => {
   const path = new URL(url,'http://localhost').pathname;
   const queue = /\/customers\/[^/]+\/rounds$/.test(path) ? roundRequests : /\/presence\/agents\/[^/]+$/.test(path) ? removalRequests : path.endsWith('/presence/agents') ? managementRequests : path.endsWith('/presence/timeline') ? timelineRequests : path.endsWith('/presence/daily') ? dailyRequests : path.endsWith('/presence') ? presenceRequests : path.endsWith('/overview') ? overviewRequests : requests;
   queue.push({url, options, resolve, reject});
 }),
 console
});
const respondTo = (queue, index, payload, status = 200, headers = {}) => queue[index].resolve({status, ok: status >= 200 && status < 300, headers:new Headers(headers), json: async () => payload, blob: async () => ({payload})});
const respond = (index, payload, status = 200, headers = {}) => respondTo(requests,index,payload,status,headers);
const tick = () => new Promise(resolve => setImmediate(resolve));
const timelinePayload = (overrides = {}) => {
 const segments = overrides.segments || [{status:'online',start_minute:0,end_minute:1440}];
 const status_minutes = {online:0,busy:0,offline:0,unknown:0,missing:0,not_monitored:0,future:0,expired:0};
 for (const segment of segments) if (Object.hasOwn(status_minutes,segment.status)) status_minutes[segment.status] += segment.end_minute-segment.start_minute;
 return {status:'ready',day:'2026-10-16',timezone:'Asia/Shanghai',source_key:'cw',account_id:1,
  agent:{agent_id:1,agent_name:'Agent',agent_name_source:'chatwoot'},through_at:'2026-10-16T01:15:59Z',
  segments,status_minutes,granularity_minutes:1,sample_count:status_minutes.online+status_minutes.busy+status_minutes.offline+status_minutes.unknown,...overrides};
};
"""

EXPORTS = """
globalThis.ui = {api, state, qualityState, presenceState, showCurrentPage, collectQualityFilters, populateQualityFilters, qualityTime,
 qualitySafeLink, qualityAttachments, renderQualityTimeline, refreshQualityRatings, qualitySnapshotLabel, loadQualityList, qualityAgentLabel,
 openQualityDetail, loadQualityDetail, openQualityVersions, loadQualityVersions, stopQuality, logout,
 qualityRequest, loadQuality, downloadQualityEvent, changeQualityMode, closeQualityDetail, applyQualityFilters,
 setQualityFiltersCollapsed, loadQualityRoundDirectory, renderQualityRoundDirectory, renderQualityList,
 exportQuality, cancelQualityExport, updateQualityExportButtons, qualityExportFilename,
 setQualityReview, renderQualityReview, loadQualityReview, saveQualityReview,
 loadQualityOverview, loadQualityPresence, loadQualityPresenceDaily, startQualityPresence, stopQualityPresence, qualityBeijingDay, qualityMinute, presenceRequest, mergePresenceRoster, loadPresenceTimeline, renderPresenceTimeline, renderPresenceIntervals, locatePresenceMinute, presenceAgentValue, updatePresenceDateRange, ensurePresenceDay, renderQualityPresenceDaily, loadPresenceManagement, renderPresenceManagement, removePresenceAgent};
"""

CASES = {
    "authenticated_binary_api_preserves_blob_and_handles_errors": r"""
ui.state.token='session';
const controller=new AbortController();
let pending=ui.api('/admin/api/statistics/time/export.xlsx?mode=day&date=2026-10-01',
  {signal:controller.signal,responseType:'blob'});
assert.equal(requests[0].options.headers.get('Authorization'),'Bearer session');
assert.equal(requests[0].options.signal,controller.signal);
assert.equal('responseType' in requests[0].options,false);
respond(0,'binary-excel');
assert.deepEqual(await pending,{payload:'binary-excel'});
pending=ui.api('/admin/api/statistics/time/export.xlsx',{responseType:'blob'});
respond(1,{detail:'统计导出暂不可用'},503);
await assert.rejects(pending,/统计导出暂不可用/);
pending=ui.api('/admin/api/statistics/time/export.xlsx',{responseType:'blob'});
respond(2,{detail:'expired'},401);
await assert.rejects(pending,/登录已失效/);
assert.equal(ui.state.token,'');
""",
    "review_lights_show_independent_verdicts_and_hide_pending_without_expanding_cards": r"""
ui.qualityState.mode='customers';
for (const ai of ['passed','failed','pending']) for (const human of ['passed','failed','pending']) {
 ui.qualityState.items=[{customer_id:'c1',display_name:'Customer',
  review:{ai_status:'failed',human_status:'failed'},
  latest_round:{round_id:'r1',conversation_id:12,round_no:3,
    review:{ai_status:ai,human_status:human,remark:'<img src=x onerror=evil()>'}}}];
 ui.renderQualityList(); const html=node('qualityList').innerHTML;
 const expected=[ai,human].filter(s=>s!=='pending');
 assert.equal((html.match(/class="quality-review-dot"/g)||[]).length,expected.length);
 for (const status of ['passed','failed']) assert.equal((html.match(new RegExp('data-review-status="'+status+'"','g'))||[]).length,expected.filter(s=>s===status).length);
 assert.equal(html.includes('data-review-kind="ai"'),ai!=='pending');
 assert.equal(html.includes('data-review-kind="human"'),human!=='pending');
 assert.equal(html.includes('最近一轮质检结果'),expected.length>0);
 assert.equal(html.includes('<img'),false);
 assert.equal((html.match(/class="quality-customer-metric"/g)||[]).length,6);
}
ui.qualityState.items=[{customer_id:'c1',latest_round:{round_id:'r2'}}];
ui.renderQualityList(); assert.equal(node('qualityList').innerHTML.includes('quality-review-dot'),false);
ui.qualityState.items=[{customer_id:'c1'}];
ui.renderQualityList(); assert.equal(node('qualityList').innerHTML.includes('quality-review-dot'),false);
""",
    "round_list_and_directory_show_their_own_review_lights": r"""
const round={round_id:'r1',customer_id:'c1',conversation_id:12,round_no:1,
 review:{ai_status:'passed',human_status:'failed'}};
ui.qualityState.mode='rounds'; ui.qualityState.items=[round];
ui.renderQualityList();
assert.equal(node('qualityList').innerHTML.includes('aria-label="AI质检：合格"'),true);
assert.equal(node('qualityList').innerHTML.includes('aria-label="人工质检：不合格"'),true);
ui.qualityState.detail={type:'round',id:'r1',item:round};
ui.qualityState.roundDirectory={items:[round,{...round,round_id:'r2',review:{ai_status:'pending',human_status:'pending',remark:'只有备注'}}],page:1,pageSize:50,total:2,loading:false};
ui.renderQualityRoundDirectory();
assert.equal((node('qualityRoundList').innerHTML.match(/class="quality-review-dot"/g)||[]).length,2);
assert.equal(node('qualityRoundList').innerHTML.includes('data-review-kind="ai"'),true);
assert.equal(node('qualityRoundList').innerHTML.includes('data-review-kind="human"'),true);
""",
    "review_filters_are_applied_to_lists_overview_summary_and_exports": r"""
ui.state.token='session'; ui.state.view='quality'; ui.qualityState.ready=true;
node('qualityAiReviewStatus').value='failed'; node('qualityHumanReviewStatus').value='passed';
ui.applyQualityFilters({collapse:false});
for (const request of [requests[0], overviewRequests[0]]) {
 const params=new URL(request.url,'http://localhost').searchParams;
 assert.equal(params.get('ai_review_status'),'failed');
 assert.equal(params.get('human_review_status'),'passed');
}
ui.setQualityFiltersCollapsed(true);
assert.equal(node('qualityFilterSummary').textContent.includes('AI质检：不合格'),true);
assert.equal(node('qualityFilterSummary').textContent.includes('人工质检：合格'),true);
respond(0,{items:[],total:0,page:1}); respondTo(overviewRequests,0,{}); await tick();
node('qualityAiReviewStatus').value='passed'; // Unapplied changes cannot alter an export.
const download=ui.exportQuality('list');
const params=new URL(requests[1].url,'http://localhost').searchParams;
assert.equal(params.get('ai_review_status'),'failed');
assert.equal(params.get('human_review_status'),'passed');
assert.equal(confirmations[0].includes('AI质检：不合格'),true);
assert.equal(confirmations[0].includes('人工质检：合格'),true);
respond(1,'xlsx bytes'); await download;
node('qualityHumanReviewStatus').value='';
assert.equal(ui.collectQualityFilters().has('human_review_status'),false);
""",
    "round_review_saves_and_reloads_the_selected_round": r"""
ui.state.token='session'; ui.state.view='quality'; ui.qualityState.ready=true;
const round={round_id:'r1',customer_id:'c1',conversation_id:12,round_no:1,review:{ai_status:'failed',human_status:'failed',remark:'原备注',revision:'7',reviewer:'admin',reviewed_at:'2026-10-19T08:00:00Z'}};
ui.qualityState.items=[{customer_id:'c1',display_name:'Customer',latest_round:{...round}}];
ui.renderQualityList();
assert.equal((node('qualityList').innerHTML.match(/data-review-status="failed"/g)||[]).length,2);
const opening=ui.openQualityDetail('round','r1');
respond(0,{round,messages:[],total:0}); await opening;
assert.equal(node('qualityReviewHumanVerdict').value,'failed');
assert.equal(node('qualityReviewAiVerdict').value,'failed');
assert.equal(node('qualityReviewRemark').value,'原备注');
node('qualityReviewHumanVerdict').value='passed'; node('qualityReviewHumanVerdict').handlers.change();
node('qualityReviewRemark').value='<img src=x onerror=evil()>\n复核通过'; node('qualityReviewRemark').handlers.input();
const saving=ui.saveQualityReview(); await tick();
assert.equal(requests[1].url.endsWith('/rounds/r1/review'),true);
assert.equal(requests[1].options.method,'PUT');
assert.equal(requests[1].options.headers.get('Content-Type'),'application/json');
assert.deepEqual(JSON.parse(requests[1].options.body),{ai_status:'failed',human_status:'passed',remark:'<img src=x onerror=evil()>\n复核通过',revision:'7'});
assert.equal(ui.closeQualityDetail(),false); // Do not cancel an in-flight save.
const saved={ai_status:'failed',human_status:'passed',remark:'<img src=x onerror=evil()>\n复核通过',revision:'8',reviewer:'admin',reviewed_at:'2026-10-19T08:01:00Z'};
respond(1,saved); await saving;
assert.equal(ui.qualityState.review.dirty,false);
assert.equal(node('qualityReviewStatus').textContent.includes('已保存'),true);
assert.equal(node('qualityReviewRemark').value,saved.remark);
assert.equal(ui.qualityState.detail.id,'r1','Saving must keep the current detail open');
assert.equal((node('qualityList').innerHTML.match(/data-review-status="failed"/g)||[]).length,1);
assert.equal((node('qualityList').innerHTML.match(/data-review-status="passed"/g)||[]).length,1);
assert.equal(ui.qualityState.items[0].latest_round.review.revision,'8');
ui.closeQualityDetail();
const reopened=ui.openQualityDetail('round','r1'); respond(2,{round:{...round,review:saved},messages:[],total:0}); await reopened;
assert.equal(node('qualityReviewRemark').value,saved.remark);
assert.equal(node('qualityReviewHumanVerdict').value,'passed');
assert.equal(node('qualityReviewAiVerdict').value,'failed');
""",
    "full_history_review_selection_ignores_stale_round_response": r"""
ui.state.token='session'; ui.state.view='quality'; ui.qualityState.ready=true;
ui.qualityState.detail={type:'customer',id:'c1',item:{customer_id:'c1'}};
const rounds=[1,2,3].map(n=>({round_id:'r'+n,round_no:n,conversation_id:12}));
ui.qualityState.roundDirectory={customerId:'c1',items:rounds,page:1,pageSize:50,total:3,loading:false};
ui.renderQualityRoundDirectory();
assert.equal(ui.qualityState.review.roundId,'r1');
const old=ui.loadQualityReview(rounds[1]);
const current=ui.loadQualityReview(rounds[2]);
respond(1,{ai_status:'passed',human_status:'passed',remark:'第三轮',revision:'3'}); await current;
respond(0,{ai_status:'failed',human_status:'failed',remark:'第二轮旧请求',revision:'2'}); await old;
assert.equal(ui.qualityState.review.roundId,'r3');
assert.equal(node('qualityReviewRemark').value,'第三轮');
assert.equal(node('qualityReviewRound').value,'r3');
""",
    "customer_history_shows_chatwoot_contact_profile": r"""
ui.state.token='session'; ui.state.view='quality'; ui.qualityState.ready=true;
const opened=ui.openQualityDetail('customer','c1');
respond(0,{customer:{customer_id:'c1',display_name:'客户甲',contact_id:'42',chatwoot_user_id:'42',
  phone_number:'+86 13800000000',contact_note:'VIP 联系人备注',business_user_id:'visitor-42',round_count:2,message_count:8},
  messages:[],total:0,page:1}); await opened;
const meta=node('qualityDetailMeta').innerHTML;
for (const value of ['Chatwoot 用户 ID','42','手机号','+86 13800000000','联系人备注','VIP 联系人备注']) assert.equal(meta.includes(value),true,value);
""",
    "failed_review_save_preserves_draft_and_blocks_export_of_unsaved_changes": r"""
ui.state.token='session'; ui.state.view='quality'; ui.qualityState.ready=true;
ui.qualityState.detail={type:'round',id:'r1',item:{round_id:'r1',round_no:1,conversation_id:12}};
ui.setQualityReview(ui.qualityState.detail.item);
node('qualityReviewAiVerdict').value='failed'; node('qualityReviewAiVerdict').handlers.change();
node('qualityReviewHumanVerdict').value='failed'; node('qualityReviewHumanVerdict').handlers.change();
node('qualityReviewRemark').value='不可丢失的输入'; node('qualityReviewRemark').handlers.input();
const saving=ui.saveQualityReview(); respond(0,{detail:'本轮备注已在其他窗口更新，请重新读取后再保存。'},409); await saving;
assert.equal(node('qualityReviewRemark').value,'不可丢失的输入');
assert.equal(ui.qualityState.review.dirty,true);
assert.equal(JSON.parse(requests[0].options.body).ai_status,'failed');
assert.equal(JSON.parse(requests[0].options.body).human_status,'failed');
assert.equal(node('qualityReviewAiVerdict').value,'failed');
assert.equal(node('qualityReviewHumanVerdict').value,'failed');
assert.equal(node('qualityReviewStatus').textContent.includes('其他窗口'),true);
confirmationAnswers.push(false); assert.equal(ui.closeQualityDetail(),false);
assert.equal(ui.qualityState.detail.id,'r1');
await ui.exportQuality('detail'); assert.equal(requests.length,1);
assert.equal(node('toast').textContent.includes('先保存'),true);
""",
    "message_pagination_does_not_overwrite_an_unsaved_review": r"""
ui.state.token='session'; ui.state.view='quality'; ui.qualityState.ready=true;
const round={round_id:'r1',conversation_id:12,round_no:1,review:{ai_status:'failed',human_status:'passed',remark:'已保存的备注',revision:'1'}};
ui.qualityState.detail={type:'round',id:'r1',page:2,total:60,item:round};
ui.setQualityReview(round);
node('qualityReviewRemark').value='编辑中的新备注'; node('qualityReviewRemark').handlers.input();
const loading=ui.loadQualityDetail(); respond(0,{round,messages:[],total:60,page:2}); await loading;
assert.equal(node('qualityReviewRemark').value,'编辑中的新备注');
assert.equal(ui.qualityState.review.dirty,true);
""",
    "customer_cards_show_latest_round_service_metrics_and_keep_history_totals": r"""
ui.qualityState.mode='customers'; ui.qualityState.total=1;
ui.qualityState.items=[{customer_id:'c1',display_name:'Customer',contact_id:42,business_user_id:'U1',
 source_key:'PRIVATE_ORIGIN',account_id:999,round_count:12,message_count:86,
 latest_round:{conversation_id:3108,round_no:3,reception_type:'human',rating_status:'rated',rating:5,
 rating_feedback:'<img src=x onerror=evil()>',service_metrics:{first_response_status:'recorded',
 first_response_seconds:120,average_response_seconds:150,response_sample_count:2,
 last_message_by_agent:false,last_message_sender_role:'contact',duration_seconds:720,
 human_phase_message_count:8,human_phase_customer_count:5,human_phase_agent_count:3,
 human_phase_basis:'handoff_request'}}}];
ui.renderQualityList(); const html=node('qualityList').innerHTML;
for (const label of ['转人工首响','人工平均响应','最后一句为人工','本轮总时长','访客评价','人工对话数']) assert.equal(html.includes(label),true,label);
for (const value of ['12 轮 · 86 条消息','最近一轮 · 会话 3108 · 第 3 轮','2分','2分30秒','否 · 客户','12分','5 分','8（5：3）']) assert.equal(html.includes(value),true,value);
assert.equal(html.includes('PRIVATE_ORIGIN'),false); assert.equal(html.includes('账号'),false);
assert.equal(html.includes('<img'),false); assert.equal(html.includes('&lt;img'),true);
assert.equal((html.match(/class="quality-customer-metric"/g)||[]).length,6);
assert.equal(html.includes('data-quality-open="0"'),true);
""",
    "customer_metrics_distinguish_zero_unknown_and_unanswered_transfer": r"""
ui.qualityState.mode='customers';
const round={conversation_id:7,round_no:1,reception_type:'human',rating_status:'not_invited'};
ui.qualityState.items=[{customer_id:'c',latest_round:{...round,service_metrics:{first_response_status:'awaiting_agent',
 first_response_seconds:null,average_response_seconds:null,last_message_by_agent:null,duration_seconds:0,
 human_phase_message_count:0,human_phase_customer_count:0,human_phase_agent_count:0}}}];
ui.renderQualityList(); let html=node('qualityList').innerHTML;
for (const text of ['待人工回复','待确认','0秒','0（0：0）','未评价']) assert.equal(html.includes(text),true,text);
assert.equal(html.includes('NaN'),false); assert.equal(html.includes('undefined'),false);
ui.qualityState.items=[{customer_id:'empty'}]; ui.renderQualityList(); html=node('qualityList').innerHTML;
assert.equal(html.includes('暂无可确认的对话轮次'),true); assert.equal(html.includes('0秒'),false);
""",
    "customer_default_collapsible_filters_and_compact_cards": r"""
assert.equal(ui.qualityState.mode,'customers');
ui.state.token = 'session'; ui.state.view = 'quality'; ui.qualityState.ready = true;
node('qualityKeyword').value = '退款'; ui.applyQualityFilters();
assert.equal(new URL(requests[0].url).pathname,'/admin/api/quality/customers');
assert.equal(node('qualityFilterFields').classList.contains('hidden'),true);
assert.equal(node('qualityToggleFilters').attributes['aria-expanded'],'false');
assert.equal(node('qualityFilterSummary').textContent.includes('关键词：退款'),true);
assert.equal(node('qualityFilterForm').classList.contains('hidden'),false,'action buttons remain available when fields collapse');
respond(0,{items:[{customer_id:'c1',display_name:'<Customer>',round_count:20,message_count:500}],total:1,page:1}); await tick();
assert.equal(node('qualityList').innerHTML.includes('quality-customer-card'),true);
assert.equal(node('qualityList').innerHTML.includes('&lt;Customer&gt;'),true);
assert.equal(node('qualityList').innerHTML.includes('<table>'),false);
node('qualityToggleFilters').handlers.click();
assert.equal(node('qualityFilterFields').classList.contains('hidden'),false);
assert.equal(node('qualityKeyword').value,'退款');
assert.equal(requests.length,1,'expanding must retain the query and avoid a reload');
ui.stopQuality(true);
assert.equal(ui.qualityState.mode,'customers');
assert.equal(node('qualityToggleFilters').attributes['aria-expanded'],'true');
""",
    "round_directory_pages_independently_and_survives_round_switching": r"""
ui.state.token = 'session'; ui.state.view = 'quality'; ui.qualityState.ready = true;
ui.qualityState.applied = new URLSearchParams({keyword:'filtered'});
const opened = ui.openQualityDetail('customer','c1');
respond(0,{customer:{customer_id:'c1',display_name:'Customer'},messages:[],total:500,page:1}); await opened;
assert.equal(node('qualityDetail').classList.contains('hidden'),false);
assert.equal(node('qualityCloseDetail').focused,true);
assert.equal(roundRequests.length,1);
assert.equal(new URL(roundRequests[0].url).searchParams.has('keyword'),false);
respondTo(roundRequests,0,{items:[{round_id:'r1',round_no:1,conversation_id:7,started_at:'2026-10-16T00:00:00Z'}],total:55,page:1}); await tick();
assert.equal(node('qualityRoundList').innerHTML.includes('会话 7'),true);
assert.equal(node('qualityRoundNext').disabled,false);
node('qualityRoundNext').handlers.click();
assert.equal(new URL(roundRequests[1].url).searchParams.get('page'),'2');
respondTo(roundRequests,1,{items:[{round_id:'r55',round_no:55,conversation_id:7}],total:55,page:2}); await tick();
assert.equal(node('qualityRoundPrev').disabled,false);
assert.equal(node('qualityRoundNext').disabled,true);
const selected = ui.openQualityDetail('round','r55',true);
respond(1,{round:{round_id:'r55',customer_id:'c1',round_no:55},messages:[],total:2,page:1}); await selected;
assert.equal(roundRequests.length,2,'round selection must retain the current directory page');
assert.equal(node('qualityRoundList').innerHTML.includes('aria-current="true"'),true);
assert.equal(node('qualityAllHistory').attributes['aria-current'],'false');
node('qualityAllHistory').handlers.click();
respond(2,{customer:{customer_id:'c1'},messages:[],total:500,page:1}); await tick();
assert.equal(ui.qualityState.detail.type,'customer');
assert.equal(ui.qualityState.roundDirectory.page,2);
assert.equal(node('qualityAllHistory').attributes['aria-current'],'true');
""",
    "stale_round_directory_cannot_mix_customers_or_reopen_closed_drawer": r"""
ui.state.token = 'session'; ui.state.view = 'quality'; ui.qualityState.ready = true;
let opened = ui.openQualityDetail('customer','old');
respond(0,{customer:{},messages:[],total:0}); await opened;
opened = ui.openQualityDetail('customer','new');
assert.equal(roundRequests[0].options.signal.aborted,true);
respond(1,{customer:{},messages:[],total:0}); await opened;
respondTo(roundRequests,1,{items:[{round_id:'new-round',round_no:3,conversation_id:'NEW CUSTOMER'}],total:1,page:1}); await tick();
respondTo(roundRequests,0,{items:[{round_id:'old-round',conversation_id:'PRIVATE OLD CUSTOMER'}],total:1,page:1}); await tick();
assert.equal(node('qualityRoundList').innerHTML.includes('NEW CUSTOMER'),true);
assert.equal(node('qualityRoundList').innerHTML.includes('PRIVATE OLD CUSTOMER'),false);
const pending = ui.loadQualityRoundDirectory('new');
ui.closeQualityDetail();
assert.equal(roundRequests[2].options.signal.aborted,true);
respondTo(roundRequests,2,{items:[{round_id:'late'}],total:1,page:1}); await pending;
assert.equal(ui.qualityState.roundDirectory,null);
assert.equal(node('qualityDetail').classList.contains('hidden'),true);
assert.equal(node('qualityRoundList').innerHTML.includes('late'),false);
""",
    "directory_failure_keeps_history_readable_and_escape_closes_in_order": r"""
ui.state.token = 'session'; ui.state.view = 'quality'; ui.qualityState.ready = true;
const opened = ui.openQualityDetail('customer','c1');
respond(0,{customer:{},messages:[{content:'VISIBLE MESSAGE',sender_role:'contact'}],total:1}); await opened;
respondTo(roundRequests,0,{detail:'directory unavailable'},503); await tick();
assert.equal(node('qualityRoundCount').textContent,'读取失败');
assert.equal(node('qualityTimeline').innerHTML.includes('VISIBLE MESSAGE'),true);
assert.equal(ui.qualityState.ready,true);
ui.qualityState.versions = {message:{}};
documentEvents.keydown({key:'Escape'});
assert.equal(ui.qualityState.versions,null);
assert.notEqual(ui.qualityState.detail,null);
documentEvents.keydown({key:'Escape'});
assert.equal(ui.qualityState.detail,null);
assert.equal(node('qualityDetail').classList.contains('hidden'),true);
""",
    "compact_chat_keeps_order_roles_and_original_text": r"""
ui.qualityState.detail = {type:'customer'};
ui.qualityState.messages = [
 {sender_role:'contact',content:'第一句\n原始换行',round_id:'r1',round_no:1,conversation_id:7,sent_at:'2026-10-16T00:00:00Z'},
 {sender_role:'bot',content:'<机器人回复>',round_id:'r1',round_no:1,conversation_id:7,sent_at:'2026-10-16T00:00:01Z'},
 {sender_role:'agent',content:'人工回复',is_private:true,round_id:'r2',round_no:2,conversation_id:7,sent_at:'2026-10-17T00:00:00Z'}
];
ui.renderQualityTimeline(); const html = node('qualityTimeline').innerHTML;
assert.equal((html.match(/quality-chat-divider/g)||[]).length,2);
assert.equal((html.match(/quality-message outgoing/g)||[]).length,2);
assert.equal(html.includes('第一句\n原始换行'),true);
assert.equal(html.includes('&lt;机器人回复&gt;'),true);
assert.equal(html.indexOf('第一句') < html.indexOf('&lt;机器人回复&gt;'),true);
assert.equal(html.includes('私密消息 / 内部备注'),true);
assert.equal(html.includes('原文与版本'),true);
""",
    "chat_bubbles_show_current_visitor_score_feedback_and_pending_without_fabricating_scores": r"""
ui.qualityState.detail={type:'customer'};
ui.qualityState.messages=[
 {message_id:1,content:'普通消息'},
 {message_id:2,content:'请评价',is_rating:true,visitor_rating:{rating:2,feedback:'<img src=x onerror=evil()>\n等候太久'}},
 {message_id:3,content:'另一轮评价',is_rating:true,visitor_rating:{rating:5,feedback:'已解决'}},
 {message_id:4,is_rating:true,visitor_rating:{rating:null}},
 {message_id:5,is_rating:true,visitor_rating:{rating:0}},
 {message_id:6,is_rating:true,visitor_rating:{rating:6}},
 {message_id:7,is_rating:true,visitor_rating:{rating:true}}
];
ui.renderQualityTimeline();
const bubbles=node('qualityTimeline').innerHTML.split('<article').slice(1);
assert.equal(bubbles[0].includes('quality-message-rating'),false);
assert.equal(bubbles[1].includes('quality-message-rating low'),true);
assert.equal(bubbles[1].includes('2 / 5 分'),true);
assert.equal(bubbles[1].includes('★★☆☆☆'),true);
assert.equal(bubbles[1].includes('低评分'),true);
assert.equal(bubbles[1].includes('&lt;img src=x onerror=evil()&gt;\n等候太久'),true);
assert.equal(bubbles[1].includes('<img'),false);
assert.equal(bubbles[2].includes('5 / 5 分'),true);
assert.equal(bubbles[2].includes('低评分'),false);
for(const bubble of bubbles.slice(3)) {assert.equal(bubble.includes('待评价'),true); assert.equal(bubble.includes('/ 5 分'),false);}
""",
    "visitor_rating_refresh_keeps_original_messages_scroll_review_draft_and_versions": r"""
ui.state.token='session'; ui.state.view='quality'; ui.qualityState.ready=true;
const opening=ui.openQualityDetail('customer','c1');
const original={source_key:'cw',account_id:1,conversation_id:7,message_id:500,round_id:'r1',round_no:1,
 content:'原始评价邀请',is_rating:true,visitor_rating:{rating:null}};
respond(0,{customer:{customer_id:'c1'},messages:[original],total:1,page:1}); await opening;
assert.equal(timers.size,1); const timer=[...timers.values()][0]; assert.equal(timer.milliseconds,15000);
ui.qualityState.review={roundId:'r1',dirty:true,remark:'未保存备注',aiStatus:'failed'};
ui.qualityState.versions={message:original}; node('qualityVersionList').innerHTML='原文版本保持展开';
node('qualityChatScroll').scrollTop=233;
const draft=ui.qualityState.review;
const refreshing=timer.callback();
assert.equal(new URL(requests[1].url).searchParams.get('page'),'1');
await timer.callback(); assert.equal(requests.length,2,'overlapping refresh is skipped');
respond(1,{messages:[{...original,content:'后续变更正文',visitor_rating:{rating:2,feedback:'等待太久'}}]}); await refreshing;
assert.equal(node('qualityTimeline').innerHTML.includes('2 / 5 分'),true);
assert.equal(ui.qualityState.messages[0].content,'原始评价邀请');
assert.equal(node('qualityChatScroll').scrollTop,233);
assert.equal(ui.qualityState.review,draft); assert.equal(ui.qualityState.review.dirty,true);
assert.equal(ui.qualityState.versions.message,original);
assert.equal(node('qualityVersionList').innerHTML,'原文版本保持展开');
const failed=timer.callback(); respond(2,{detail:'temporary failure'},503); await failed;
assert.equal(node('qualityTimeline').innerHTML.includes('2 / 5 分'),true);
context.document.hidden=true; documentEvents.visibilitychange(); assert.equal(timers.size,0);
context.document.hidden=false; documentEvents.visibilitychange(); assert.equal(timers.size,1);
const pending=requests.at(-1); ui.closeQualityDetail(false,true);
assert.equal(pending.options.signal.aborted,true); assert.equal(timers.size,0);
pending.resolve({status:200,ok:true,json:async()=>({messages:[{...original,visitor_rating:{rating:1}}]})}); await tick();
assert.equal(ui.qualityState.detail,null); assert.equal(node('qualityTimeline').innerHTML,'');
""",
    "visitor_rating_refresh_rejects_an_old_customer_and_page_response": r"""
ui.state.token='session'; ui.state.view='quality'; ui.qualityState.ready=true;
const original={source_key:'cw',account_id:1,conversation_id:7,message_id:500,is_rating:true,visitor_rating:{rating:null}};
let opening=ui.openQualityDetail('customer','c1');
respond(0,{customer:{customer_id:'c1'},messages:[original],total:60,page:1}); await opening;
const refresh=ui.refreshQualityRatings();
ui.qualityState.detail.page=2; const page=ui.loadQualityDetail();
assert.equal(requests[1].options.signal.aborted,true);
respond(2,{customer:{customer_id:'c1'},messages:[{...original,message_id:600}],total:60,page:2}); await page;
respond(1,{messages:[{...original,visitor_rating:{rating:1,feedback:'STALE PAGE'}}]}); await refresh;
assert.equal(node('qualityTimeline').innerHTML.includes('STALE PAGE'),false);
const second=ui.refreshQualityRatings(); opening=ui.openQualityDetail('customer','c2');
assert.equal(requests[3].options.signal.aborted,true);
respond(4,{customer:{customer_id:'c2'},messages:[{...original,source_key:'another-source',visitor_rating:{rating:5}}],total:1,page:1}); await opening;
respond(3,{messages:[{...original,visitor_rating:{rating:1,feedback:'PRIVATE OLD CUSTOMER'}}]}); await second;
assert.equal(node('qualityTimeline').innerHTML.includes('PRIVATE OLD CUSTOMER'),false);
assert.equal(node('qualityTimeline').innerHTML.includes('5 / 5 分'),true);
ui.logout(); assert.equal(timers.size,0);
""",
    "overview_uses_all_filtered_rounds_and_does_not_reload_on_pagination_or_history": r"""
ui.state.token = 'session'; ui.state.view = 'quality'; ui.qualityState.ready = true;
ui.qualityState.mode = 'rounds';
node('qualityKeyword').value = 'refund'; node('qualityCustomer').value = 'customer';
node('qualityAgent').value = JSON.stringify({source_key:'cw',account_id:'1',agent_id:'1'});
node('qualityStart').value = '2026-10-15T00:00';
ui.qualityState.filters.agents = [{source_key:'cw',account_id:1,agent_id:1,agent_name:'beec',agent_name_source:'chatwoot'}];
ui.applyQualityFilters();
assert.equal(overviewRequests.length,1); assert.equal(requests.length,1); assert.equal(presenceRequests.length,0);
const overviewQuery = new URL(overviewRequests[0].url).searchParams, listQuery = new URL(requests[0].url).searchParams;
for (const [key,value] of ui.qualityState.applied) assert.equal(overviewQuery.get(key),value,key);
assert.equal(overviewQuery.has('page'),false); assert.equal(overviewQuery.has('page_size'),false);
assert.equal(listQuery.get('page'),'1');
respond(0,{items:[{round_id:'one',customer_name:'LIST READY'}],total:250,page:1}); await tick();
assert.equal(node('qualityList').innerHTML.includes('LIST READY'),true);
assert.equal(ui.qualityState.listLoading,false); assert.equal(ui.qualityState.overviewLoading,true);
respondTo(overviewRequests,0,{total_rounds:250,total_customers:100,ai_only_rounds:150,human_rounds:100,
 active_rounds:80,resolved_rounds:170,rated_rounds:200,low_rating_rounds:20,average_rating:4.2,
 human_without_public_reply_rounds:8,responsibility_unrecorded_rounds:7,participating_agent_count:3,agents_truncated:true,
 generated_at:'2026-10-16T00:13:27Z',participating_agents:[{source_key:'cw',account_id:1,agent_id:1,agent_name:'分配坐席1',responsible_rounds:80,replied_rounds:70,low_rating_rounds:4}]}); await tick();
const cards = node('qualityOverviewCards').innerHTML;
assert.equal((cards.match(/class="quality-stat"/g)||[]).length,8);
assert.equal(cards.includes('>250</strong>'),true); assert.equal(cards.includes('>100</strong>'),true);
assert.equal(cards.includes('60.0%'),true); assert.equal(cards.includes('40.0%'),true);
assert.equal(cards.includes('均分 4.20 分'),true); assert.equal(cards.includes('未观察到真人公开回复'),true);
assert.equal(cards.includes('未评价'),false); assert.equal(cards.includes('超时'),false);
assert.equal(node('qualityOverviewStatus').textContent.includes('不限当前页'),true);
assert.equal(node('qualityOverviewNotice').textContent.includes('7 轮未记录 ICS 分配'),true);
assert.equal(node('qualityOverviewNotice').textContent.includes('仅显示前 1 位'),true);
assert.equal(node('qualityOverviewAgents').innerHTML.includes('>beec<'),true);
assert.equal(node('qualityOverviewAgents').innerHTML.includes('<td>80</td><td>70</td><td>4</td>'),true);
node('qualityNext').handlers.click(); assert.equal(overviewRequests.length,1);
respond(1,{items:[],total:250,page:2}); await tick();
ui.changeQualityMode('customers'); assert.equal(overviewRequests.length,1);
respond(2,{items:[],total:100,page:1}); await tick();
const history = ui.openQualityDetail('customer','c1'); respond(3,{customer:{display_name:'Customer'},messages:[],total:1000,page:1}); await history;
assert.equal(overviewRequests.length,1); assert.equal(ui.qualityState.overview.total_rounds,250);
""",
    "overview_rejects_old_results_and_failure_keeps_the_list_available": r"""
ui.state.token = 'session'; ui.state.view = 'quality'; ui.qualityState.ready = true;
ui.qualityState.mode = 'rounds';
ui.qualityState.applied = new URLSearchParams({keyword:'old'}); const older = ui.loadQualityOverview();
ui.qualityState.applied = new URLSearchParams({keyword:'new'}); const newer = ui.loadQualityOverview();
assert.equal(overviewRequests[0].options.signal.aborted,true);
respondTo(overviewRequests,1,{total_rounds:0,total_customers:0,average_rating:null,participating_agents:[]}); await newer;
assert.equal(node('qualityOverviewCards').innerHTML.includes('暂无评分'),true);
assert.equal(node('qualityOverviewCards').innerHTML.includes('NaN'),false);
respondTo(overviewRequests,0,{total_rounds:999,participating_agents:[]}); await older;
assert.equal(ui.qualityState.overview.total_rounds,0); assert.equal(node('qualityOverviewCards').innerHTML.includes('999'),false);
node('qualityKeyword').value = 'failed overview'; ui.applyQualityFilters();
respond(0,{items:[{customer_name:'STILL QUERYABLE'}],total:1,page:1}); await tick();
respondTo(overviewRequests,2,{detail:'summary unavailable'},503); await tick();
assert.equal(node('qualityOverviewStatus').textContent.includes('概况暂不可用'),true);
assert.equal(node('qualityList').innerHTML.includes('STILL QUERYABLE'),true);
assert.equal(ui.qualityState.ready,true); assert.equal(ui.qualityState.overviewLoading,false);
""",
    "presence_and_daily_use_one_timer_pause_when_hidden_and_reject_old_responses": r"""
ui.state.token = 'session'; ui.state.view = 'presence';
ui.qualityState.applied = new URLSearchParams({keyword:'history',agent_id:'9',start:'2020-01-01'});
ui.startQualityPresence(); ui.startQualityPresence();
assert.equal(timers.size,1); assert.equal([...timers.values()][0].milliseconds,60000);
assert.equal(node('qualityPresenceDay').value,ui.qualityBeijingDay());
assert.equal(presenceRequests.length,1); assert.equal(dailyRequests.length,1);
assert.equal(new URL(presenceRequests[0].url).search,'');
const dailyQuery = new URL(dailyRequests[0].url).searchParams;
assert.deepEqual([...dailyQuery.keys()],['day']);
[...timers.values()][0].callback();
assert.equal(presenceRequests.length,1); assert.equal(dailyRequests.length,1,'pending requests cannot overlap');
respondTo(presenceRequests,0,{status:'ready',source_key:'cw',account_id:1,sampled_at:'2026-10-16T00:13:27Z',agents:[
 {agent_id:1,agent_name:'<beec>',availability_status:'online',agent_name_source:'chatwoot'},
 {source_key:'cw',account_id:1,agent_id:2,agent_name:'QQ',availability_status:'busy'},
 {source_key:'cw',account_id:1,agent_id:3,agent_name:'Third',availability_status:'offline'},
 {source_key:'cw',account_id:1,agent_id:4,agent_name:'Fourth',availability_status:'unexpected'}]});
respondTo(dailyRequests,0,{status:'ready',day:node('qualityPresenceDay').value,agents:[]}); await tick();
const seats = node('qualityPresenceAgents').innerHTML;
for (const label of ['在线','忙碌','离线','未知']) assert.equal(seats.includes(`>${label}</span>`),true,label);
assert.equal(seats.includes('&lt;beec&gt;'),true); assert.equal(seats.includes('<beec>'),false);
assert.equal(seats.includes('Chatwoot ID 1 · cw / 账号 1'),true);
assert.equal(node('qualityPresenceStatus').textContent.includes('08:13'),true);
assert.equal(node('qualityPresenceStatus').textContent.includes('08:13:27'),false,'sampling time is shown to the minute');
const selectedDay = node('qualityPresenceDay').value;
ui.stopQuality(true);
assert.equal(node('qualityPresenceDay').value,selectedDay,'archive reset must preserve the monitoring day');
assert.equal(timers.size,1,'archive lifecycle must not stop monitoring');
assert.equal(presenceRequests.length,1); assert.equal(dailyRequests.length,1);
[...timers.values()][0].callback(); assert.equal(presenceRequests.length,2); assert.equal(dailyRequests.length,2);
context.document.hidden = true; documentEvents.visibilitychange();
assert.equal(timers.size,0); assert.equal(presenceRequests[1].options.signal.aborted,true); assert.equal(dailyRequests[1].options.signal.aborted,true);
context.document.hidden = false; documentEvents.visibilitychange();
assert.equal(timers.size,1); assert.equal(presenceRequests.length,3); assert.equal(dailyRequests.length,3);
respondTo(presenceRequests,1,{status:'ready',agents:[{agent_name:'LATE PRIVATE',availability_status:'offline'}]});
respondTo(dailyRequests,1,{status:'ready',agents:[{agent_name:'LATE DAILY'}]}); await tick();
assert.equal(ui.presenceState.currentLoading,true); assert.equal(ui.presenceState.dailyLoading,true);
respondTo(presenceRequests,2,{status:'ready',agents:[{source_key:'cw',account_id:1,agent_id:1,agent_name:'CURRENT',availability_status:'online'}]});
respondTo(dailyRequests,2,{status:'ready',agents:[]}); await tick();
assert.equal(node('qualityPresenceAgents').innerHTML.includes('CURRENT'),true);
assert.equal(node('qualityPresenceAgents').innerHTML.includes('LATE'),false);
ui.logout(false); assert.equal(timers.size,0);
assert.equal(node('qualityPresenceAgents').innerHTML.includes('CURRENT'),false);
documentEvents.visibilitychange(); assert.equal(timers.size,0); assert.equal(presenceRequests.length,3);
""",
    "presence_stale_and_failed_samples_never_become_offline": r"""
ui.state.token = 'session'; ui.state.view = 'presence';
const agent = {source_key:'cw',account_id:1,agent_id:1,agent_name:'beec',availability_status:'online'};
let pending = ui.loadQualityPresence(); respondTo(presenceRequests,0,{status:'ready',sampled_at:'2026-10-16T00:00:00Z',agents:[agent],collector:{enabled:false,status:'disabled'}}); await pending;
assert.equal(node('qualityPresenceCollectorStatus').textContent.includes('采集未启用'),true);
pending = ui.loadQualityPresence(); respondTo(presenceRequests,1,{status:'stale',sampled_at:'2026-10-15T00:00:00Z',agents:[agent]}); await pending;
assert.equal(node('qualityPresenceTag').textContent,'旧采样 · 非实时');
assert.equal(node('qualityPresenceAgents').innerHTML.includes('在线 · 旧采样'),true);
assert.equal(node('qualityPresenceStatus').textContent.includes('不能视为实时状态'),true);
pending = ui.loadQualityPresence(); presenceRequests[2].reject(new Error('network unavailable')); await pending;
assert.equal(node('qualityPresenceTag').textContent,'状态未知');
assert.equal(node('qualityPresenceAgents').innerHTML.includes('>未知</span>'),true);
assert.equal(node('qualityPresenceAgents').innerHTML.includes('>离线</span>'),false);
assert.equal(node('qualityPresenceAgents').innerHTML.includes('>在线</span>'),false);
assert.equal(node('qualityPresenceStatus').textContent.includes('未将获取失败视为离线'),true);
assert.equal(node('qualityPresenceCollectorStatus').textContent.includes('历史采集状态未知'),true);
pending = ui.loadQualityPresence(); respondTo(presenceRequests,3,{status:'unconfigured',agents:[]}); await pending;
assert.equal(node('qualityPresenceTag').textContent,'状态未知'); assert.equal(node('qualityPresenceStatus').textContent.includes('请配置'),true);
""",
    "daily_presence_dates_are_independent_and_missing_minutes_are_not_offline": r"""
ui.state.token = 'session'; ui.state.view = 'presence';
node('qualityPresenceDay').value = '2026-10-15'; const older = ui.loadQualityPresenceDaily();
node('qualityPresenceDay').value = '2026-10-16'; const newer = node('qualityPresenceDay').handlers.change();
assert.equal(dailyRequests[0].options.signal.aborted,true);
assert.equal(new URL(dailyRequests[1].url).searchParams.get('day'),'2026-10-16');
respondTo(dailyRequests,1,{day:'2026-10-16',timezone:'Asia/Shanghai',source_key:'cw',account_id:1,status:'ready',through_at:'2026-10-16T00:45:59Z',monitoring_started_at:'2026-10-16T00:00:00Z',agent_count:201,agents_truncated:true,agents:[
 {agent_id:1,agent_name:'<beec>',agent_name_source:'chatwoot',online_minutes:20,busy_minutes:10,offline_minutes:5,unknown_minutes:3,sampled_minutes:38,expected_minutes:60,missing_minutes:22,last_sampled_at:'2026-10-16T00:44:59Z'}]}); await newer;
const table = node('qualityPresenceDaily').innerHTML;
assert.equal(table.includes('&lt;beec&gt;'),true); assert.equal(table.includes('<td>20</td><td>10</td><td>5</td><td>3</td>'),true);
assert.equal(table.includes('38 / 60 分钟 · 63.3%'),true); assert.equal(table.includes('缺口 22 分钟，不计为离线'),true);
assert.equal(table.includes('08:44'),true); assert.equal(table.includes('08:44:59'),false);
assert.equal(node('qualityPresenceDailyStatus').textContent.includes('采集启用于'),true);
assert.equal(node('qualityPresenceDailyStatus').textContent.includes('统计截至'),true);
assert.equal(node('qualityPresenceDailyNotice').textContent.includes('仅显示前 1 位坐席（共 201 位）'),true);
assert.equal(node('qualityPresenceDailyNotice').classList.contains('hidden'),false);
respondTo(dailyRequests,0,{day:'2026-10-15',status:'ready',agents:[{agent_name:'WRONG DAY'}]}); await older;
assert.equal(node('qualityPresenceDaily').innerHTML.includes('WRONG DAY'),false);
assert.equal(node('qualityPresenceDailyStatus').textContent.includes('2026-10-16'),true);
let pending = ui.loadQualityPresenceDaily(); await ui.loadQualityPresenceDaily(); assert.equal(dailyRequests.length,3);
respondTo(dailyRequests,2,{status:'unavailable',agents:[]}); await pending;
assert.equal(node('qualityPresenceDailyStatus').textContent.includes('暂不可用'),true);
assert.equal(node('qualityPresenceDailyNotice').classList.contains('hidden'),true);
assert.equal(node('qualityPresenceDaily').innerHTML.includes('<td>5</td>'),false);
pending = ui.loadQualityPresenceDaily(); respondTo(dailyRequests,3,{status:'ready',agents:[],monitoring_started_at:null}); await pending;
assert.equal(node('qualityPresenceDaily').innerHTML.includes('不能据此认定坐席离线'),true);
""",
    "current_presence_does_not_claim_history_is_saved_when_collector_is_disabled_or_failed": r"""
ui.state.token = 'session'; ui.state.view = 'presence'; node('qualityPresenceDay').value = '2026-10-16';
const payload = {status:'ready',source_key:'cw',account_id:1,sampled_at:'2026-10-16T00:15:59Z',agents:[{agent_id:1,agent_name:'坐席1',agent_name_source:'chatwoot',availability_status:'online'}]};
let current = ui.loadQualityPresence();
respondTo(presenceRequests,0,{...payload,collector:{enabled:true,status:'unavailable',last_saved_at:null,pending_batches:1,dropped_batches:0}}); await current;
assert.equal(node('qualityPresenceAgents').innerHTML.includes('坐席1'),true,'explicit Chatwoot names remain valid');
assert.equal(node('qualityPresenceAgents').innerHTML.includes('>在线</span>'),true);
assert.equal(node('qualityPresenceCollectorStatus').textContent.includes('历史采集暂不可用'),true);
assert.equal(node('qualityPresenceCollectorStatus').textContent.includes('当前状态不代表历史已保存'),true);
current = ui.loadQualityPresence(); respondTo(presenceRequests,1,{...payload,collector:{enabled:true,status:'ready',last_saved_at:'2026-10-16T00:13:27Z',pending_batches:1,dropped_batches:2}}); await current;
assert.equal(node('qualityPresenceCollectorStatus').textContent.includes('最近保存于'),true);
assert.equal(node('qualityPresenceCollectorStatus').textContent.includes('08:13'),true);
assert.equal(node('qualityPresenceCollectorStatus').textContent.includes('等待保存'),true);
assert.equal(node('qualityPresenceCollectorStatus').textContent.includes('缺口不计为离线'),true);
current = ui.loadQualityPresence(); respondTo(presenceRequests,2,{...payload,collector:{enabled:false,status:'configuration_error'}}); await current;
assert.equal(node('qualityPresenceCollectorStatus').textContent.includes('暂不可用'),true);
assert.equal(node('qualityPresenceCollectorStatus').textContent.includes('未启用'),false);
for (const [index,status] of ['disabled','not_started'].entries()) {
 const daily = ui.loadQualityPresenceDaily(); respondTo(dailyRequests,index,{status,agents:[]}); await daily;
 assert.equal(node('qualityPresenceDaily').innerHTML.includes('<table>'),false);
 assert.equal(node('qualityPresenceDailyStatus').textContent.includes(status === 'disabled' ? '未启用' : '尚未产生样本'),true);
}
""",
    "independent_page_lifecycles_cancel_only_their_own_requests": r"""
ui.state.token = 'session'; ui.state.view = 'quality'; ui.qualityState.ready = true;
const overview = ui.loadQualityOverview();
ui.stopQualityPresence();
assert.equal(overviewRequests[0].options.signal.aborted,false);
ui.stopQuality(); ui.state.view = 'presence'; ui.startQualityPresence();
assert.equal(timers.size,1);
const presenceGeneration = ui.presenceState.generation;
ui.stopQuality(true);
assert.equal(timers.size,1); assert.equal(ui.presenceState.generation,presenceGeneration);
assert.equal(presenceRequests[0].options.signal.aborted,false); assert.equal(dailyRequests[0].options.signal.aborted,false);
ui.stopQualityPresence(true); ui.state.view = 'channel';
assert.equal(timers.size,0); assert.equal(overviewRequests[0].options.signal.aborted,true);
assert.equal(presenceRequests[0].options.signal.aborted,true); assert.equal(dailyRequests[0].options.signal.aborted,true);
respondTo(overviewRequests,0,{total_rounds:999,participating_agents:[{agent_name:'PRIVATE'}]});
respondTo(presenceRequests,0,{status:'ready',agents:[{agent_name:'PRIVATE'}]});
respondTo(dailyRequests,0,{status:'ready',agents:[{agent_name:'PRIVATE'}]}); await overview; await tick();
for (const id of ['qualityOverviewCards','qualityOverviewAgents','qualityPresenceAgents','qualityPresenceDaily']) assert.equal(node(id).innerHTML.includes('PRIVATE'),false,id);
assert.equal(ui.presenceState.controllers.size,0); assert.equal(ui.qualityState.controllers.size,0);
documentEvents.visibilitychange(); assert.equal(timers.size,0);
""",
    "archive_route_never_loads_presence_and_presence_works_without_archive_readiness": r"""
ui.state.token = 'session'; context.window.location.hash = '#quality'; ui.showCurrentPage();
assert.equal(requests.length,1); assert.equal(new URL(requests[0].url).pathname,'/admin/api/quality/status');
assert.equal(presenceRequests.length,0); assert.equal(dailyRequests.length,0); assert.equal(timelineRequests.length,0); assert.equal(timers.size,0);
respond(0,{enabled:false,ready:false}); await tick();
assert.equal(ui.qualityState.ready,false);
context.window.location.hash = '#presence'; ui.showCurrentPage();
assert.equal(ui.state.view,'presence'); assert.equal(ui.state.route,'#presence');
assert.equal(node('pageTitle').textContent,'客服状态');
assert.equal(node('qualityPage').classList.contains('hidden'),true); assert.equal(node('presencePage').classList.contains('hidden'),false);
assert.equal(timers.size,1); assert.equal(presenceRequests.length,1); assert.equal(dailyRequests.length,1);
respondTo(presenceRequests,0,{status:'ready',source_key:'cw',account_id:1,agents:[{agent_id:1,agent_name:'Agent',availability_status:'online'}]}); await tick();
assert.equal(timelineRequests.length,1);
const generation = ui.presenceState.generation;
context.window.location.hash = '#quality'; ui.showCurrentPage();
assert.equal(timers.size,0); assert.equal(ui.presenceState.generation>generation,true);
assert.equal(dailyRequests[0].options.signal.aborted,true); assert.equal(timelineRequests[0].options.signal.aborted,true);
respondTo(timelineRequests,0,timelinePayload({agent:{agent_id:1,agent_name:'PRIVATE LATE'}}));
respondTo(dailyRequests,0,{status:'ready',agents:[{agent_id:1,agent_name:'PRIVATE LATE'}]}); await tick();
assert.equal(node('presenceTimelineStatus').textContent.includes('PRIVATE'),false); assert.equal(node('qualityPresenceDaily').innerHTML.includes('PRIVATE'),false);
assert.equal(presenceRequests.length,1); assert.equal(dailyRequests.length,1);
""",
    "presence_management_pages_and_removes_without_stale_daily_resurrection": r"""
ui.state.token='session'; ui.state.view='presence'; node('qualityPresenceDay').value='2026-10-16';
const old={source_key:'cw',account_id:1,agent_id:'7',agent_name:'<Retired>',removed_at:'2026-10-15T00:00:00Z'};
const live={source_key:'cw',account_id:1,agent_id:'8',agent_name:'Current'};
const daily={status:'ready',source_key:'cw',account_id:1,day:'2026-10-16',agents:[{...old,online_minutes:6},live]};
ui.presenceState.daily=daily; ui.presenceState.dailyRoster=daily.agents;
node('presenceManageToggle').handlers.click();
assert.equal(node('presenceManagement').classList.contains('hidden'),false);
assert.equal(node('presenceManageToggle').attributes['aria-expanded'],'true');
assert.equal(new URL(managementRequests[0].url).searchParams.get('page_size'),'50');
respondTo(managementRequests,0,{status:'ready',agents:[old,live],total:2,page:1}); await tick();
assert.equal(node('presenceManagementList').innerHTML.includes('&lt;Retired&gt;'),true);
assert.equal(node('presenceManagementList').innerHTML.includes('<Retired>'),false);
assert.equal(node('presenceManagementList').innerHTML.includes('已离开 Chatwoot 名单'),true);
const pendingDaily=ui.loadQualityPresenceDaily();
const removed=ui.removePresenceAgent(0); await ui.removePresenceAgent(0);
assert.equal(removalRequests.length,1,'double clicking must not issue another deletion');
assert.equal(dailyRequests[0].options.signal.aborted,true);
const deletion=removalRequests[0]; assert.equal(deletion.options.method,'DELETE');
assert.equal(deletion.options.headers.get('Authorization'),'Bearer session');
assert.equal(new URL(deletion.url).pathname,'/admin/api/quality/presence/agents/7');
assert.equal(new URL(deletion.url).searchParams.get('source_key'),'cw');
assert.equal(new URL(deletion.url).searchParams.get('account_id'),'1');
respondTo(removalRequests,0,{status:'ready',hidden_at:'2026-10-16T08:00:00Z'}); await tick();
assert.equal(ui.presenceState.daily.agents.length,1); assert.equal(ui.presenceState.daily.agents[0].agent_id,'8');
respondTo(dailyRequests,0,daily); await pendingDaily;
assert.equal(ui.presenceState.daily.agents.length,1,'a pre-deletion response cannot bring the retired seat back');
respondTo(dailyRequests,1,{...daily,agents:[live]}); await tick();
respondTo(managementRequests,1,{status:'ready',agents:[live],total:1,page:1}); await removed;
assert.equal(node('presenceManagementNotice').textContent.includes('历史记录保留'),true);
assert.equal(node('qualityPresenceDaily').innerHTML.includes('Retired'),false);
assert.equal(node('presenceAgent').innerHTML.includes('Retired'),false);
assert.equal(node('presenceManagementNext').disabled,true);
node('presenceManagementNext').handlers.click(); assert.equal(managementRequests.length,2);
""",
    "presence_management_history_opt_in_and_fresh_capture_restore": r"""
ui.state.token='session'; ui.state.view='presence'; node('qualityPresenceDay').value='2026-10-16';
const hidden={source_key:'cw',account_id:1,agent_id:'7',agent_name:'Hidden',hidden_at:'2026-10-16T07:59:00Z',online_minutes:6,sampled_minutes:6,expected_minutes:6};
node('presenceManageToggle').handlers.click();
respondTo(managementRequests,0,{status:'ready',agents:[],total:0}); await tick();
const oldDaily=ui.loadQualityPresenceDaily();
node('presenceIncludeHidden').checked=true; node('presenceIncludeHidden').handlers.change();
assert.equal(dailyRequests[0].options.signal.aborted,true);
assert.equal(new URL(dailyRequests[1].url).searchParams.get('include_hidden'),'true');
assert.equal(new URL(managementRequests[1].url).searchParams.get('include_hidden'),'true');
respondTo(dailyRequests,1,{status:'ready',day:'2026-10-16',agents:[hidden]});
respondTo(managementRequests,1,{status:'ready',agents:[hidden],total:1}); await tick();
respondTo(dailyRequests,0,{status:'ready',agents:[]}); await oldDaily;
assert.equal(node('qualityPresenceDaily').innerHTML.includes('已删除 · 历史'),true);
assert.equal(node('qualityPresenceDaily').innerHTML.includes('6 / 6 分钟'),true);
assert.equal(node('presenceManagementList').innerHTML.includes('历史保留'),true);
await ui.removePresenceAgent(0); assert.equal(removalRequests.length,0,'already hidden history cannot be deleted');
const restored=ui.loadQualityPresenceDaily();
respondTo(dailyRequests,2,{status:'ready',day:'2026-10-16',agents:[{...hidden,hidden_at:null,sampled_minutes:7,expected_minutes:7}]}); await restored;
assert.equal(node('qualityPresenceDaily').innerHTML.includes('已删除 · 历史'),false);
assert.equal(node('qualityPresenceDaily').innerHTML.includes('7 / 7 分钟'),true);
const late=ui.loadPresenceManagement(); ui.stopQualityPresence(true); ui.state.view='quality';
assert.equal(managementRequests[2].options.signal.aborted,true);
respondTo(managementRequests,2,{status:'ready',agents:[{...hidden,agent_name:'PRIVATE LATE'}],total:1}); await late;
assert.equal(node('presenceManagementList').innerHTML.includes('PRIVATE'),false);
assert.equal(node('presenceManagement').classList.contains('hidden'),true);
assert.equal(node('presenceIncludeHidden').checked,false);
""",
    "presence_management_pagination_and_failures_keep_current_reads_independent": r"""
ui.state.token='session'; ui.state.view='presence'; node('qualityPresenceDay').value='2026-10-16';
node('presenceManageToggle').handlers.click();
respondTo(managementRequests,0,{status:'ready',agents:[{source_key:'cw',account_id:1,agent_id:1,agent_name:'First'}],total:51,page:1}); await tick();
assert.equal(node('presenceManagementNext').disabled,false); node('presenceManagementNext').handlers.click();
assert.equal(new URL(managementRequests[1].url).searchParams.get('page'),'2');
respondTo(managementRequests,1,{status:'ready',agents:[{source_key:'cw',account_id:1,agent_id:51,agent_name:'Last'}],total:51,page:2}); await tick();
assert.equal(node('presenceManagementPrev').disabled,false); assert.equal(node('presenceManagementNext').disabled,true);
const failed=ui.removePresenceAgent(0); respondTo(removalRequests,0,{detail:'scope changed'},409); await tick();
assert.equal(node('presenceManagementNotice').textContent.includes('scope changed'),true);
respondTo(dailyRequests,0,{status:'ready',agents:[]}); await tick();
respondTo(managementRequests,2,{status:'unavailable'}); await failed;
assert.equal(node('presenceManagementStatus').textContent.includes('暂不可用'),true);
const current=ui.loadQualityPresence();
respondTo(presenceRequests,0,{status:'ready',source_key:'cw',account_id:1,agents:[{agent_id:1,agent_name:'CURRENT UNAFFECTED',availability_status:'online'}]}); await current;
assert.equal(node('qualityPresenceAgents').innerHTML.includes('CURRENT UNAFFECTED'),true);
assert.equal(ui.presenceState.current.status,'ready');
""",
    "presence_roster_merges_current_and_historical_agents_by_full_scope": r"""
ui.state.token = 'session'; ui.state.view = 'presence'; node('qualityPresenceDay').value = '2026-10-16';
ui.qualityState.filters.agents = [{source_key:'cw',account_id:1,agent_id:1,agent_name:'ARCHIVE NAME'}];
ui.presenceState.dailyRoster = [
 {source_key:'cw',account_id:1,agent_id:1,agent_name:'Old name'},
 {source_key:'cw',account_id:1,agent_id:2,agent_name:'<retired>',agent_name_source:'chatwoot'},
 {source_key:'cw-other',account_id:2,agent_id:1,agent_name:'Other account'}];
ui.presenceState.currentRoster = [
 {source_key:'cw',account_id:1,agent_id:1,agent_name:'<live>',agent_name_source:'chatwoot'},
 {source_key:'cw',account_id:1,agent_id:3,agent_name:'Current only'}];
ui.mergePresenceRoster();
assert.equal(ui.presenceState.roster.length,4); assert.equal(node('presenceAgent').options.length,4);
const options = node('presenceAgent').innerHTML;
assert.equal(options.includes('&lt;live&gt;'),true); assert.equal(options.includes('&lt;retired&gt;'),true);
assert.equal(options.includes('<live>'),false); assert.equal(options.includes('ARCHIVE NAME'),false); assert.equal(options.includes('Old name'),false);
assert.equal(options.includes('Other account'),true); assert.equal(timelineRequests.length,1,'only selected agent is requested');
assert.deepEqual([...new URL(timelineRequests[0].url).searchParams.keys()],['day','agent_id']);
const historical = ui.presenceState.roster.find(agent=>agent.source_key==='cw'&&agent.agent_id===2);
node('presenceAgent').value = ui.presenceAgentValue(historical);
const selected = node('presenceAgent').handlers.change();
assert.equal(timelineRequests.length,2); assert.equal(timelineRequests[0].options.signal.aborted,true);
assert.equal(new URL(timelineRequests[1].url).searchParams.get('agent_id'),'2');
ui.mergePresenceRoster(); assert.equal(timelineRequests.length,2); assert.equal(node('presenceAgent').value,ui.presenceAgentValue(historical));
respondTo(timelineRequests,0,timelinePayload({agent:{agent_id:1,agent_name:'OLD AGENT'}})); await tick();
assert.equal(ui.presenceState.timelineLoading,true); assert.equal(node('presenceTimelineStatus').textContent.includes('OLD AGENT'),false);
respondTo(timelineRequests,1,timelinePayload({agent:{agent_id:2,agent_name:'<retired>',agent_name_source:'chatwoot'}})); await selected;
assert.equal(node('presenceTimelineStatus').textContent.includes('<retired>'),true); assert.equal(ui.presenceState.timeline.agent.agent_id,2);
""",
    "presence_timeline_shows_full_day_and_locates_end_exclusive_minute_boundaries": r"""
ui.state.token = 'session'; ui.state.view = 'presence'; node('qualityPresenceDay').value = '2026-10-16';
const statuses = ['online','busy','offline','unknown','missing','not_monitored','future'];
const segments = statuses.map((status,index)=>({status,start_minute:index*60,end_minute:index===6?1440:(index+1)*60}));
ui.renderPresenceTimeline(timelinePayload({segments,agent:{agent_id:1,agent_name:'<script>NAME</script>',agent_name_source:'chatwoot'}}));
const bar = node('presenceTimelineBar').innerHTML;
assert.equal((bar.match(/class="presence-segment /g)||[]).length,7);
for (const status of statuses) assert.equal(bar.includes(`presence-${status}`),true,status);
assert.equal(bar.includes('<script>'),false); assert.equal(bar.includes('24:00'),true);
assert.equal((node('presenceHourTicks').innerHTML.match(/<span /g)||[]).length,25);
assert.equal(node('presenceHourTicks').innerHTML.includes('>00</span>'),true); assert.equal(node('presenceHourTicks').innerHTML.includes('>24</span>'),true);
for (const [time,label,range] of [['00:59','在线','00:00–01:00'],['01:00','忙碌','01:00–02:00'],['04:59','缺采样','04:00–05:00'],['05:00','未监管','05:00–06:00'],['23:59','未结束 / 未来','06:00–24:00']]) {
 node('presenceCheckTime').value=time; node('presenceLocate').handlers.click();
 assert.equal(node('presencePoint').textContent.includes(`${time} · ${label}`),true);
 assert.equal(node('presencePoint').textContent.includes(range),true);
}
assert.equal(node('presencePoint').textContent.includes('不计为缺采样'),true);
ui.locatePresenceMinute(240); assert.equal(node('presencePoint').textContent.includes('不计为离线'),true);
ui.locatePresenceMinute(180); assert.equal(node('presencePoint').textContent.includes('已有采样'),true);
node('presenceCheckTime').value='24:00'; node('presenceCheckTime').handlers.change();
assert.equal(node('presencePoint').textContent.includes('00:00 至 23:59'),true);
node('presenceIntervals').handlers.click({target:{closest(){return {dataset:{presenceMinute:'60'}};}}});
assert.equal(node('presenceCheckTime').value,'01:00'); assert.equal(ui.presenceState.selectedMinute,60);
""",
    "presence_interval_pagination_is_bounded_and_point_location_selects_the_correct_page": r"""
ui.state.token = 'session'; ui.state.view = 'presence'; node('qualityPresenceDay').value='2026-10-16';
const segments = Array.from({length:1440},(_,minute)=>({status:minute%2?'busy':'online',start_minute:minute,end_minute:minute+1}));
ui.renderPresenceTimeline(timelinePayload({segments}));
assert.equal((node('presenceTimelineBar').innerHTML.match(/data-presence-minute=/g)||[]).length,1440);
assert.equal((node('presenceIntervals').innerHTML.match(/data-presence-minute=/g)||[]).length,50);
assert.equal(node('presencePageInfo').textContent.includes('1 / 29 页'),true);
node('presenceNext').handlers.click(); assert.equal(ui.presenceState.page,2);
assert.equal(node('presenceIntervals').innerHTML.includes('data-presence-minute="50"'),true);
node('presenceStatusFilter').value='busy'; node('presenceStatusFilter').handlers.change();
assert.equal(node('presencePageInfo').textContent.includes('720 个区间'),true); assert.equal(ui.presenceState.page,1);
assert.equal(node('presenceIntervals').innerHTML.includes('data-presence-minute="0"'),false);
ui.locatePresenceMinute(1439);
assert.equal(node('presenceStatusFilter').value,''); assert.equal(ui.presenceState.page,29);
assert.equal((node('presenceIntervals').innerHTML.match(/data-presence-minute=/g)||[]).length,40);
assert.equal(node('presenceIntervals').innerHTML.includes('data-presence-minute="1439"'),true);
assert.equal(node('presenceIntervals').innerHTML.includes('class="presence-selected"'),true);
assert.equal(node('presenceNext').disabled,true); node('presenceNext').handlers.click(); assert.equal(ui.presenceState.page,29);
node('presenceStatusFilter').value='missing'; node('presenceStatusFilter').handlers.change();
assert.equal(node('presenceIntervals').innerHTML.includes('没有匹配区间'),true); assert.equal(node('presenceNext').disabled,true);
assert.equal(node('presencePrev').disabled,true);
""",
    "presence_timeline_rejects_stale_day_scope_and_logout_results": r"""
ui.state.token = 'session'; ui.state.view='presence'; node('qualityPresenceDay').value='2026-10-15';
ui.presenceState.currentRoster=[{source_key:'cw',account_id:1,agent_id:1,agent_name:'Agent'}]; ui.mergePresenceRoster();
assert.equal(timelineRequests.length,1);
node('qualityPresenceDay').value='2026-10-16'; node('qualityPresenceDay').handlers.change();
assert.equal(timelineRequests.length,2); assert.equal(timelineRequests[0].options.signal.aborted,true);
assert.equal(new URL(timelineRequests[1].url).searchParams.get('day'),'2026-10-16');
respondTo(timelineRequests,0,timelinePayload({day:'2026-10-15',agent:{agent_id:1,agent_name:'OLD DAY'}})); await tick();
assert.equal(ui.presenceState.timelineLoading,true); assert.equal(node('presenceTimelineStatus').textContent.includes('OLD DAY'),false);
respondTo(timelineRequests,1,timelinePayload({source_key:'OTHER SOURCE',agent:{agent_id:1,agent_name:'WRONG SCOPE'}})); await tick();
assert.equal(ui.presenceState.timeline,null); assert.equal(node('presenceTimelineStatus').textContent.includes('暂不可用'),true);
const latest=ui.loadPresenceTimeline(); respondTo(timelineRequests,2,timelinePayload()); await latest;
assert.equal(ui.presenceState.timeline.day,'2026-10-16');
const loggedOut=ui.loadPresenceTimeline(); ui.logout(false);
assert.equal(timelineRequests[3].options.signal.aborted,true); assert.equal(node('presenceAgent').value,'');
respondTo(timelineRequests,3,timelinePayload({agent:{agent_id:1,agent_name:'PRIVATE LOGIN'}})); await loggedOut;
assert.equal(ui.presenceState.timeline,null); assert.equal(node('presenceTimelineStatus').textContent.includes('PRIVATE'),false);
assert.equal(node('presenceTimelineBar').innerHTML,''); assert.equal(node('qualityPresenceDay').value,'');
""",
    "presence_daily_and_timeline_failures_do_not_block_other_panels": r"""
ui.state.token='session'; ui.state.view='presence'; ui.startQualityPresence();
respondTo(presenceRequests,0,{status:'ready',source_key:'cw',account_id:1,agents:[{agent_id:1,agent_name:'Current',availability_status:'online'}]}); await tick();
respondTo(dailyRequests,0,{detail:'daily unavailable'},503); await tick();
const day=node('qualityPresenceDay').value;
respondTo(timelineRequests,0,timelinePayload({day})); await tick();
assert.equal(node('qualityPresenceDailyStatus').textContent.includes('暂不可用'),true);
assert.equal(node('qualityPresenceAgents').innerHTML.includes('Current'),true); assert.equal(ui.presenceState.timeline.day,day);
[...timers.values()][0].callback();
assert.equal(presenceRequests.length,2); assert.equal(dailyRequests.length,2); assert.equal(timelineRequests.length,2);
respondTo(timelineRequests,1,{detail:'timeline unavailable'},503);
respondTo(dailyRequests,1,{status:'ready',day,agents:[]});
presenceRequests[1].reject(new Error('network unavailable')); await tick();
assert.equal(node('qualityPresenceTag').textContent,'状态未知'); assert.equal(node('qualityPresenceDaily').innerHTML.includes('该日暂无采样记录'),true);
assert.equal(node('presenceTimelineStatus').textContent.includes('暂不可用'),true); assert.equal(ui.presenceState.timeline,null);
assert.equal(timers.size,1); assert.equal(ui.qualityState.ready,false);
""",
    "presence_hidden_pauses_selected_timeline_and_rejects_old_results": r"""
ui.state.token='session'; ui.state.view='presence'; node('qualityPresenceDay').value='2026-10-16';
ui.presenceState.currentRoster=[{source_key:'cw',account_id:1,agent_id:1,agent_name:'Agent'}]; ui.mergePresenceRoster(); ui.startQualityPresence();
assert.equal(timelineRequests.length,1,'one timeline request while pending');
context.document.hidden=true; documentEvents.visibilitychange();
assert.equal(timers.size,0); assert.equal(timelineRequests[0].options.signal.aborted,true);
context.document.hidden=false; documentEvents.visibilitychange();
assert.equal(timers.size,1); assert.equal(timelineRequests.length,2);
respondTo(timelineRequests,0,timelinePayload({agent:{agent_id:1,agent_name:'OLD HIDDEN'}})); await tick();
assert.equal(ui.presenceState.timelineLoading,true); assert.equal(node('presenceTimelineStatus').textContent.includes('OLD'),false);
respondTo(timelineRequests,1,timelinePayload()); await tick();
assert.equal(ui.presenceState.timelineLoading,false); assert.equal(ui.presenceState.timeline.agent.agent_name,'Agent');
""",
    "presence_unavailable_and_malformed_timelines_clear_previous_private_intervals": r"""
ui.state.token='session'; ui.state.view='presence'; node('qualityPresenceDay').value='2026-10-16';
for (const [status,phrase] of [['disabled','未启用'],['not_started','尚未产生样本'],['not_found','请选择现有或历史客服'],['unavailable','暂不可用']]) {
 ui.renderPresenceTimeline(timelinePayload()); assert.equal(ui.presenceState.segments.length,1);
 ui.renderPresenceTimeline({status});
 assert.equal(node('presenceTimelineStatus').textContent.includes(phrase),true,status);
 assert.equal(ui.presenceState.segments.length,0); assert.equal(node('presenceTimelineBar').innerHTML,''); assert.equal(node('presenceIntervals').innerHTML.includes('<table>'),false);
}
for (const segments of [
 [{status:'online',start_minute:1,end_minute:1440}],
 [{status:'online',start_minute:0,end_minute:20},{status:'offline',start_minute:19,end_minute:1440}],
 Array.from({length:1441},(_,minute)=>({status:'online',start_minute:minute,end_minute:minute+1}))
]) {ui.renderPresenceTimeline(timelinePayload({segments})); assert.equal(ui.presenceState.segments.length,0);}
ui.renderPresenceTimeline(timelinePayload({segments:[{status:'<img onerror=evil>',start_minute:0,end_minute:1440}]}));
assert.equal(node('presenceTimelineBar').innerHTML.includes('<img'),false);
assert.equal(node('presenceTimelineBar').innerHTML.includes('presence-unknown'),true);
""",
    "presence_retention_uses_four_am_minus_45_days_and_clears_history_only_at_four": r"""
for (const [instant,oldest] of [['2027-01-01T00:00:00Z','2026-11-17'],['2028-03-01T00:00:00Z','2028-01-16'],['2026-10-15T19:59:59Z','2026-08-31'],['2026-10-15T20:00:00Z','2026-09-01']]) {
 browserNow=Date.parse(instant); ui.updatePresenceDateRange(); assert.equal(node('qualityPresenceDay').min,oldest);
}
browserNow=Date.parse('2026-10-16T15:59:00Z');
ui.state.token='session'; ui.state.view='presence'; node('qualityPresenceDay').value='2026-09-01'; ui.startQualityPresence();
assert.equal(node('qualityPresenceDay').min,'2026-09-01'); assert.equal(node('qualityPresenceDay').max,'2026-10-16');
assert.equal(node('presenceRetentionNote').textContent.includes('实际采样时间保留45天'),true); assert.equal(node('presenceRetentionNote').textContent.includes('04:00'),true);
assert.equal(node('presenceRetentionNote').textContent.includes('最早日期为部分保留'),true);
assert.equal(dailyRequests.length,1,'the oldest available day is included');
respondTo(presenceRequests,0,{status:'ready',source_key:'cw',account_id:1,agents:[{agent_id:1,agent_name:'CURRENT STATUS',availability_status:'online'}]});
respondTo(dailyRequests,0,{status:'ready',day:'2026-09-01',source_key:'cw',account_id:1,retention_days:45,oldest_available_day:'2026-09-01',retention_cutoff_at:'2026-08-31T20:00:00+00:00',cleanup_hour:4,agents:[{agent_id:2,agent_name:'OLD HISTORICAL'}]}); await tick();
respondTo(timelineRequests,0,timelinePayload({day:'2026-09-01'})); await tick(); ui.locatePresenceMinute(615);
assert.equal(ui.presenceState.selectedMinute,615); assert.equal(node('presenceAgent').innerHTML.includes('OLD HISTORICAL'),true);
[...timers.values()][0].callback();
assert.equal(dailyRequests.length,2); assert.equal(timelineRequests.length,2);
browserNow=Date.parse('2026-10-16T16:00:00Z'); [...timers.values()][0].callback();
assert.equal(node('qualityPresenceDay').min,'2026-09-01'); assert.equal(node('qualityPresenceDay').max,'2026-10-17');
assert.equal(dailyRequests[1].options.signal.aborted,false,'midnight does not move the cutoff');
browserNow=Date.parse('2026-10-16T20:00:00Z'); [...timers.values()][0].callback();
assert.equal(node('qualityPresenceDay').min,'2026-09-02'); assert.equal(node('qualityPresenceDay').max,'2026-10-17');
assert.equal(dailyRequests[1].options.signal.aborted,true); assert.equal(timelineRequests[1].options.signal.aborted,true);
assert.equal(dailyRequests.length,2); assert.equal(timelineRequests.length,2,'expired days make no new history requests');
assert.equal(ui.presenceState.dailyRoster.length,0); assert.equal(ui.presenceState.roster.length,0); assert.equal(node('presenceAgent').value,'');
assert.equal(ui.presenceState.timeline,null); assert.equal(ui.presenceState.selectedMinute,null); assert.equal(node('presenceTimelineBar').innerHTML,'');
for (const id of ['qualityPresenceDailyStatus','presenceTimelineStatus','presencePoint']) assert.equal(node(id).textContent.includes('记录已超出45天保留期'),true,id);
assert.equal(node('qualityPresenceAgents').innerHTML.includes('CURRENT STATUS'),true,'current status remains available');
respondTo(dailyRequests,1,{status:'ready',agents:[{agent_id:2,agent_name:'LATE HISTORICAL'}]});
respondTo(timelineRequests,1,timelinePayload({day:'2026-09-01',agent:{agent_id:1,agent_name:'LATE TIMELINE'}})); await tick();
assert.equal(ui.presenceState.timeline,null); assert.equal(node('presenceAgent').innerHTML.includes('LATE'),false);
node('presenceLocate').handlers.click(); assert.equal(node('presencePoint').textContent.includes('记录已超出45天保留期'),true);
""",
    "presence_oldest_day_keeps_expired_minutes_distinct_and_can_locate_or_filter_them": r"""
ui.state.token='session'; ui.state.view='presence'; node('qualityPresenceDay').value='2026-09-01';
ui.renderPresenceTimeline(timelinePayload({day:'2026-09-01',retention_cutoff_at:'2026-08-31T20:00:00+00:00',oldest_available_day:'2026-09-01',
 segments:[{status:'expired',start_minute:0,end_minute:240},{status:'online',start_minute:240,end_minute:1440}]}));
assert.equal(ui.presenceState.segments[0].status,'expired');
assert.equal(node('presenceTimelineBar').innerHTML.includes('presence-expired'),true);
assert.equal(node('presenceTimelineSummary').textContent.includes('已过期 240 分钟'),true);
assert.equal(node('presenceTimelineStatus').textContent.includes('最早日期为部分保留'),true);
ui.locatePresenceMinute(239);
assert.equal(node('presencePoint').textContent.includes('03:59 · 已过期'),true);
assert.equal(node('presencePoint').textContent.includes('记录已超出45天保留期'),true);
node('presenceStatusFilter').value='expired'; node('presenceStatusFilter').handlers.change();
assert.equal(node('presenceIntervals').innerHTML.includes('<td>已过期</td>'),true);
assert.equal(node('presenceIntervals').innerHTML.includes('<td>在线</td>'),false);
ui.locatePresenceMinute(240);
assert.equal(node('presencePoint').textContent.includes('04:00 · 在线'),true);
assert.equal(node('presencePoint').textContent.includes('已过期'),false);
ui.renderQualityPresenceDaily({status:'ready',oldest_available_day:'2026-09-01',retention_cutoff_at:'2026-08-31T20:00:00+00:00',agents:[]},'2026-09-01');
assert.equal(node('qualityPresenceDailyStatus').textContent.includes('仅统计04:00起的分钟'),true);
""",
    "presence_server_cutoff_is_authoritative_when_browser_clock_is_ahead": r"""
browserNow=Date.parse('2026-10-16T21:00:00Z'); ui.updatePresenceDateRange();
assert.equal(node('qualityPresenceDay').min,'2026-09-02');
ui.updatePresenceDateRange({oldest_available_day:'2026-09-01',retention_cutoff_at:'2026-08-31T20:00:00+00:00'});
assert.equal(node('qualityPresenceDay').min,'2026-09-01');
ui.updatePresenceDateRange();
assert.equal(node('qualityPresenceDay').min,'2026-09-01');
""",
    "presence_expired_api_responses_clear_both_history_panels_and_never_become_missing_or_offline": r"""
ui.state.token='session'; ui.state.view='presence';
for (const kind of ['daily','timeline']) {
 ui.stopQualityPresence(true,true); node('qualityPresenceDay').value='2026-09-02';
 ui.presenceState.currentRoster=[{source_key:'cw',account_id:1,agent_id:1,agent_name:'Agent'}]; ui.mergePresenceRoster();
 const timelineIndex=timelineRequests.length-1, dailyIndex=dailyRequests.length;
 const daily=ui.loadQualityPresenceDaily();
 const payload={status:'expired',day:'2026-09-02',retention_days:45,oldest_available_day:'2026-09-03',cleanup_hour:4,agents:[{agent_id:2,agent_name:'MUST NOT SHOW',offline_minutes:1440,missing_minutes:1440}]};
 if (kind==='daily') {respondTo(dailyRequests,dailyIndex,payload); await daily;}
 else {respondTo(timelineRequests,timelineIndex,payload); await tick();}
 assert.equal(node('qualityPresenceDay').min,'2026-09-03');
 assert.equal(ui.presenceState.timeline,null); assert.equal(ui.presenceState.daily,null); assert.equal(ui.presenceState.dailyRoster.length,0);
 assert.equal(node('presenceAgent').value,''); assert.equal(node('presenceTimelineBar').innerHTML,'');
 for (const id of ['qualityPresenceDailyStatus','presenceTimelineStatus','presencePoint']) {
  assert.equal(node(id).textContent.includes('记录已超出45天保留期'),true,kind+id);
  assert.equal(node(id).textContent.includes('离线'),false); assert.equal(node(id).textContent.includes('缺采样'),false);
 }
 assert.equal(node('qualityPresenceDaily').innerHTML.includes('MUST NOT SHOW'),false);
 if (kind==='daily') {assert.equal(timelineRequests[timelineIndex].options.signal.aborted,true); respondTo(timelineRequests,timelineIndex,timelinePayload({day:'2026-09-02'}));}
 else {assert.equal(dailyRequests[dailyIndex].options.signal.aborted,true); respondTo(dailyRequests,dailyIndex,{status:'ready',agents:[{agent_id:2,agent_name:'MUST NOT SHOW'}]}); await daily;}
 await tick(); assert.equal(ui.presenceState.timeline,null); assert.equal(ui.presenceState.roster.length,0);
}
""",
    "presence_day_changes_remove_old_historical_agents_and_point_before_new_responses": r"""
ui.state.token='session'; ui.state.view='presence'; node('qualityPresenceDay').value='2026-10-16';
ui.presenceState.currentRoster=[{source_key:'cw',account_id:1,agent_id:1,agent_name:'Current'}];
ui.presenceState.dailyRoster=[{source_key:'cw',account_id:1,agent_id:2,agent_name:'OLD DAY AGENT'}]; ui.mergePresenceRoster();
respondTo(timelineRequests,0,timelinePayload({agent:{agent_id:2,agent_name:'OLD DAY AGENT'}})); await tick(); ui.locatePresenceMinute(615);
const oldDaily=ui.loadQualityPresenceDaily(); const oldTimeline=ui.loadPresenceTimeline();
node('qualityPresenceDay').value='2026-10-15'; const newDaily=node('qualityPresenceDay').handlers.change();
assert.equal(dailyRequests[0].options.signal.aborted,true); assert.equal(timelineRequests[1].options.signal.aborted,true);
assert.equal(ui.presenceState.dailyRoster.length,0); assert.equal(node('presenceAgent').innerHTML.includes('OLD DAY AGENT'),false);
assert.equal(ui.presenceState.timeline,null); assert.equal(ui.presenceState.selectedMinute,null); assert.equal(node('presencePoint').textContent.includes('10:15'),false);
assert.equal(new URL(timelineRequests[2].url).searchParams.get('agent_id'),'1');
assert.equal(new URL(timelineRequests[2].url).searchParams.get('day'),'2026-10-15');
respondTo(dailyRequests,0,{status:'ready',source_key:'cw',account_id:1,agents:[{agent_id:2,agent_name:'LATE OLD DAY'}]});
respondTo(timelineRequests,1,timelinePayload({agent:{agent_id:2,agent_name:'LATE OLD DAY'}})); await oldDaily; await oldTimeline;
assert.equal(node('presenceAgent').innerHTML.includes('LATE'),false); assert.equal(ui.presenceState.timeline,null);
respondTo(timelineRequests,2,timelinePayload({day:'2026-10-15'}));
respondTo(dailyRequests,1,{status:'ready',day:'2026-10-15',source_key:'cw',account_id:1,agents:[{agent_id:3,agent_name:'NEW DAY HISTORY'}]}); await newDaily; await tick();
assert.equal(node('presenceAgent').innerHTML.includes('NEW DAY HISTORY'),true); assert.equal(ui.presenceState.timeline.day,'2026-10-15');
assert.equal(node('presenceTimelineStatus').textContent.includes('OLD DAY'),false);
""",
    "presence_out_of_range_input_and_resume_cannot_request_or_relabel_expired_history": r"""
ui.state.token='session'; ui.state.view='presence'; node('qualityPresenceDay').value='2026-08-31'; ui.startQualityPresence();
assert.equal(presenceRequests.length,1); assert.equal(dailyRequests.length,0); assert.equal(timelineRequests.length,0);
assert.equal(node('qualityPresenceDailyStatus').textContent.includes('记录已超出45天保留期'),true);
node('presenceCheckTime').value='10:15'; node('presenceLocate').handlers.click();
assert.equal(node('presencePoint').textContent.includes('记录已超出45天保留期'),true);
context.document.hidden=true; documentEvents.visibilitychange(); context.document.hidden=false; documentEvents.visibilitychange();
assert.equal(dailyRequests.length,0); assert.equal(timelineRequests.length,0); assert.equal(timers.size,1);
node('qualityPresenceDay').value='2026-09-02'; node('qualityPresenceDay').handlers.change();
assert.equal(dailyRequests.length,1,'returning to a retained date restores querying');
respondTo(dailyRequests,0,{status:'ready',day:'2026-09-02',source_key:'cw',account_id:1,agents:[{agent_id:7,agent_name:'Retained'}]}); await tick();
assert.equal(timelineRequests.length,1); assert.equal(node('presenceAgent').innerHTML.includes('Retained'),true);
""",
    "filters_scope_and_beijing_time": r"""
const params = ui.collectQualityFilters();
assert.equal(params.has('channel_key'), false, 'archive must default to all channels');
assert.equal(params.get('agent_scope'), 'responsible');
node('qualityStart').value = '2026-10-16T08:00';
node('qualityEnd').value = '2026-10-17T00:00';
node('qualityAgent').value = JSON.stringify({source_key: 'cw-two', account_id: '2', agent_id: '7'});
const scoped = ui.collectQualityFilters();
assert.equal(scoped.get('source_key'), 'cw-two'); assert.equal(scoped.get('account_id'), '2');
assert.equal(scoped.get('agent_id'), '7'); assert.equal(scoped.get('start'), '2026-10-16T08:00:00+08:00');
assert.equal(ui.qualityTime('2026-10-16T00:00:00Z').includes('08:00:00'), true);
node('qualityEnd').value = '2026-10-15T00:00';
assert.throws(() => ui.collectQualityFilters(), /结束时间/);
ui.qualityState.filters = {sources: [{source_key:'cw-two',account_id:'2'}],channels: [],agents:[
 {source_key:'cw-one',account_id:'1',agent_id:'7',agent_name:'A'},
 {source_key:'cw-two',account_id:'2',agent_id:'7',agent_name:'<B>'}
]};
ui.populateQualityFilters();
assert.equal(node('qualityAgent').innerHTML.includes('>A ·'), true);
assert.equal(node('qualityAgent').innerHTML.includes('&lt;B&gt;'), true);
""",
    "message_and_attachment_content_is_inert": r"""
assert.equal(ui.qualitySafeLink('javascript:alert(1)'), '');
assert.equal(ui.qualitySafeLink('data:text/html,evil'), '');
assert.equal(ui.qualitySafeLink('//evil.example/image.png'), '');
assert.equal(ui.qualitySafeLink('https://example.com/a'), 'https://example.com/a');
ui.qualityState.detail = {type:'customer'};
ui.qualityState.messages = [{message_id:'<id>',conversation_id:'<cw>',content:'<img src=x onerror=alert(1)>',
 sender_role:'agent',sender_name:'<script>evil</script>',is_private:true,is_deleted:true,has_creation_event:false,
 attachments:[{filename:'<video onload=evil>',source_url:'javascript:alert(1)'},
 {filename:'safe file',source_url:'https://example.com/attachment'}]}];
ui.renderQualityTimeline();
const html = node('qualityTimeline').innerHTML;
assert.equal(html.includes('<img'), false); assert.equal(html.includes('<video'), false);
assert.equal(html.includes('<script>'), false); assert.equal(html.includes('javascript:'), false);
assert.equal(html.includes('&lt;img'), true); assert.equal(html.includes('noopener noreferrer'), true);
assert.equal(html.includes('未归轮'), true); assert.equal(html.includes('缺少创建时原文'), true);
assert.equal(html.includes('尚未独立复制'), true);
assert.equal(ui.qualitySnapshotLabel({snapshot_kind:'backfill',id:1},{has_creation_event:false,original_snapshot_id:1}), '补录时观察');
""",
    "inbox_and_agent_choices_derive_scope_without_a_source_field": r"""
ui.qualityState.filters = {sources:[],channels:[],agents:[
 {source_key:'cw-one',account_id:1,agent_id:'7',agent_name:'Agent One'},
 {source_key:'cw-two',account_id:2,agent_id:'7',agent_name:'<Agent Two>'}
],inboxes:[
 {source_key:'cw-one',account_id:1,inbox_id:5,name:'Inbox One'},
 {source_key:'cw-two',account_id:2,inbox_id:5,name:'<Inbox Two>'}
]};
ui.populateQualityFilters();
assert.equal(node('qualityInbox').options.length,3);
assert.equal(node('qualityAgent').options.length,3);
assert.equal(node('qualityInbox').innerHTML.includes('&lt;Inbox Two&gt;'),true);
node('qualityInbox').value = JSON.stringify({source_key:'cw-two',account_id:'2',inbox_id:'5'});
node('qualityInbox').handlers.change();
node('qualityAgent').value = JSON.stringify({source_key:'cw-two',account_id:'2',agent_id:'7'});
node('qualityAgent').handlers.change();
let params = ui.collectQualityFilters();
assert.equal(params.get('inbox_id'),'5'); assert.equal(params.get('agent_id'),'7');
assert.equal(params.get('source_key'),'cw-two'); assert.equal(params.get('account_id'),'2');
node('qualityInbox').value = JSON.stringify({source_key:'cw-one',account_id:'1',inbox_id:'5'});
node('qualityInbox').handlers.change();
assert.equal(node('qualityAgent').value,'','changing account clears a conflicting seat');
assert.equal(ui.collectQualityFilters().get('source_key'),'cw-one');
node('qualityAgent').value = JSON.stringify({source_key:'cw-two',account_id:'2',agent_id:'7'});
node('qualityAgent').handlers.change();
assert.equal(node('qualityInbox').value,'','changing account clears a conflicting inbox');
assert.equal(ui.collectQualityFilters().get('source_key'),'cw-two');
node('qualityAgent').value = ''; node('qualityAgent').handlers.change();
params = ui.collectQualityFilters();
for (const key of ['source_key','account_id','agent_id','inbox_id','channel_key','status','has_attachment']) assert.equal(params.has(key),false,key);
""",
    "conflicting_inbox_and_agent_scopes_do_not_start_a_query": r"""
ui.state.token = 'session'; ui.state.view = 'quality'; ui.qualityState.ready = true;
node('qualityInbox').value = JSON.stringify({source_key:'cw-two',account_id:'2',inbox_id:'5'});
node('qualityAgent').value = JSON.stringify({source_key:'cw-one',account_id:'1',agent_id:'7'});
assert.throws(() => ui.collectQualityFilters(), /来源.*账号不一致/);
ui.applyQualityFilters(); assert.equal(requests.length,0);
assert.equal(node('qualityListSummary').textContent.includes('不一致'),true);
node('qualityInbox').value = '';
assert.equal(ui.collectQualityFilters().get('source_key'),'cw-one');
""",
    "round_inbox_and_range_filters_match_list_and_excel_parameters": r"""
ui.state.token = 'session'; ui.state.view = 'quality'; ui.qualityState.ready = true;
node('qualityInbox').value = JSON.stringify({source_key:'cw',account_id:'2',inbox_id:'5'});
node('qualityAgent').value = JSON.stringify({source_key:'cw',account_id:'2',agent_id:'7'});
node('qualityConversation').value = '123';
node('qualityRoundNo').value = '2';
node('qualityReception').value = 'human'; node('qualityMessageScope').value = 'private';
node('qualityTimeField').value = 'message_at';
node('qualityStart').value = '2026-10-16T08:00'; node('qualityEnd').value = '2026-10-17T00:00';
for (const scope of ['responsible','replied','assigned','current']) {
 node('qualityAgentScope').value = scope; assert.equal(ui.collectQualityFilters().get('agent_scope'), scope);
}
node('qualityFilterForm').handlers.submit({preventDefault() {}});
const list = new URL(requests[0].url).searchParams;
const expected = {source_key:'cw',account_id:'2',inbox_id:'5',agent_id:'7',agent_scope:'current',
 conversation_id:'123',round_no:'2',reception_type:'human',
 message_scope:'private',time_field:'message_at',
 start:'2026-10-16T08:00:00+08:00',end:'2026-10-17T00:00:00+08:00'};
for (const [key, value] of Object.entries(expected)) assert.equal(list.get(key), value, key);
respond(0, {items:[],total:0,page:1}); await tick();
node('qualityRoundNo').value = '9';
const pending = ui.exportQuality('list');
const exported = new URL(requests[1].url,'http://localhost').searchParams;
for (const [key, value] of Object.entries(expected)) assert.equal(exported.get(key), value, key);
assert.equal(exported.has('page'), false); assert.equal(exported.has('page_size'), false);
respond(1, 'xlsx'); await pending;
""",
    "directory_warning_keeps_queue_status_and_query_errors_visible": r"""
ui.state.token = 'session'; ui.state.view = 'quality';
const pending = ui.loadQuality();
respond(0, {enabled:true,ready:true,jobs:{queued:2,processing:1,retry:1,failed:1}}); await tick();
respond(1, {sources:[],channels:[],agents:[],inboxes:[],directory:{status:'unavailable'},
 directory_warning:'Chatwoot 目录暂不可用，仅显示已归档记录。<test>'}); await tick();
assert.equal(node('qualityStatus').textContent.includes('有 4 条事件等待整理'), true);
assert.equal(node('qualityStatus').textContent.includes('整理失败'), true);
assert.equal(node('qualityDirectoryWarning').classList.contains('hidden'), false);
assert.equal(node('qualityDirectoryWarning').textContent.includes('<test>'), true);
assert.equal(node('qualityDirectoryWarning').innerHTML, '');
respond(2, {detail:'本次查询失败'}, 503); await pending;
assert.equal(node('qualityListSummary').textContent, '本次查询失败');
assert.equal(node('qualityStatus').textContent.includes('有 4 条事件等待整理'), true);
assert.equal(node('qualityDirectoryWarning').textContent.includes('目录暂不可用'), true);
ui.stopQuality(true);
assert.equal(node('qualityDirectoryWarning').textContent, '');
assert.equal(node('qualityInbox').options.length, 1);
""",
    "blocked_responsibility_records_keep_history_query_available": r"""
ui.state.token = 'session'; ui.state.view = 'quality';
const pending = ui.loadQuality();
respond(0, {enabled:true,ready:true,assignment_sync_status:'degraded',
 assignment_sync_pending_count:2,assignment_sync_blocked_count:1}); await tick();
respond(1, {sources:[],channels:[],agents:[],inboxes:[]}); await tick();
assert.equal(node('qualityStatus').textContent.includes('2 条 ICS 责任记录等待同步'), true);
assert.equal(node('qualityStatus').textContent.includes('1 条 ICS 责任记录需核查'), true);
assert.equal(node('qualityStatusTag').textContent, '归档可查询 · 责任同步待处理');
assert.equal(node('qualityFilters').disabled, false);
respond(2, {items:[],total:0,page:1}); await pending;
assert.equal(ui.qualityState.ready, true);
""",
    "round_rows_and_details_resolve_inbox_names_with_source_and_account": r"""
ui.state.token = 'session'; ui.state.view = 'quality'; ui.qualityState.ready = true;
ui.qualityState.mode = 'rounds';
ui.qualityState.filters.inboxes = [
 {source_key:'cw-one',account_id:1,inbox_id:5,name:'Wrong Inbox'},
 {source_key:'cw-two',account_id:2,inbox_id:5,name:'<Correct Inbox>'}
];
const item = {round_id:'r',source_key:'cw-two',account_id:2,inbox_id:5,channel_key:null,round_no:2};
const list = ui.loadQualityList(); respond(0, {items:[item],total:1,page:1}); await list;
assert.equal(node('qualityList').innerHTML.includes('Wrong Inbox'), false);
assert.equal(node('qualityList').innerHTML.includes('&lt;Correct Inbox&gt; · #5'), true);
assert.equal(node('qualityList').innerHTML.includes('ICS 业务渠道：未映射渠道'), true);
const detail = ui.openQualityDetail('round','r');
respond(1, {round:item,messages:[],total:0,page:1}); await detail;
assert.equal(node('qualityDetailMeta').innerHTML.includes('Chatwoot 收件箱'), true);
assert.equal(node('qualityDetailMeta').innerHTML.includes('&lt;Correct Inbox&gt; · #5'), true);
""",
    "reception_type_stays_independent_from_lifecycle_and_handoff_progress": r"""
ui.state.token = 'session'; ui.state.view = 'quality'; ui.qualityState.ready = true;
ui.qualityState.mode = 'rounds';
const items = [
 {round_id:'ai-active',reception_type:'ai_only',status:'active',classification_finalized:false},
 {round_id:'ai-ended',reception_type:'ai_only',status:'resolved',classification_finalized:true},
 {round_id:'human-requested',reception_type:'human',status:'active',classification_finalized:false,handoff_result:'requested'},
 {round_id:'human-completed',reception_type:'human',status:'resolved',classification_finalized:true,handoff_result:'completed'},
 {round_id:'human-alias',reception_type:'human_involved',status:'active'},
 {round_id:'legacy-unknown',reception_type:'unknown',status:'active',classification_finalized:false}
];
const pending = ui.loadQualityList(); respond(0, {items,total:items.length,page:1}); await pending;
const rows = node('qualityList').innerHTML;
assert.equal((rows.match(/>纯 AI 接待</g) || []).length, 2);
assert.equal((rows.match(/>人工介入</g) || []).length, 3);
assert.equal((rows.match(/>未结束</g) || []).length, 4);
assert.equal((rows.match(/>已解决</g) || []).length, 2);
assert.equal((rows.match(/<span class="tag ">待确认<\/span>/g) || []).length, 1, 'legacy unknown must not be silently labeled AI');
assert.equal(/尚未定型|暂未人工接入|仅 AI 接待|进行中/.test(rows), false);
for (const [index, item] of items.entries()) {
 const detail = ui.openQualityDetail('round',item.round_id);
 respond(index + 1, {round:item,messages:[],total:0,page:1}); await detail;
 const meta = node('qualityDetailMeta').innerHTML;
 const expectedReception = ['纯 AI 接待','纯 AI 接待','人工介入','人工介入','人工介入','待确认'][index];
 assert.equal(meta.includes(`接待分类：${expectedReception}`), true, item.round_id);
 assert.equal(meta.includes(`状态：${item.status === 'active' ? '未结束' : '已解决'}`), true);
 assert.equal(/尚未定型|暂未人工接入|仅 AI 接待|进行中/.test(meta), false);
 if (item.handoff_result) {
  assert.equal(meta.includes(`转接结果：${item.handoff_result === 'requested' ? '已申请' : '已确认接管'}`), true);
  assert.equal(meta.includes(`转接结果：${item.handoff_result === 'requested' ? '已确认接管' : '已申请'}`), false);
 }
}
for (const reception of ['ai_only','human']) {
 node('qualityReception').value = reception;
 const params = ui.collectQualityFilters();
 assert.equal(params.get('reception_type'), reception);
 assert.equal(params.has('status'),false,'removed lifecycle filter must not constrain queries');
}
""",
    "round_responsibility_uses_only_original_ics_assignment_evidence": r"""
ui.state.token = 'session'; ui.state.view = 'quality'; ui.qualityState.ready = true;
ui.qualityState.mode = 'rounds';
const history = [
 {agent_id:'1',agent_name:'Original A',was_assigned:true},
 {agent_id:'2',agent_name:'Actual B',replied:true,reply_count:1},
 {agent_id:'8',agent_name:'Private note author',noted:true}
];
const items = [
 {round_id:'known',responsibility_basis:'ics_assignment',responsible_agent_id:'1',responsible_agent_name:'<Original A>',responsible_assigned_at:'2026-10-16T00:00:00Z',responsibility_event_id:'1001',responsibility_external_round_id:'<external-round>',responsibility_trigger_message_id:'701',current_assignee_id:'2',agents:history},
 {round_id:'id-only',responsibility_basis:'ics_assignment',responsible_agent_id:'3',responsible_assigned_at:null,responsibility_event_id:'1002',responsibility_trigger_message_id:'702',current_assignee_id:'2',agents:history},
 {round_id:'unobserved',responsibility_basis:'assignment_unobserved',responsible_agent_id:'2',responsible_agent_name:'Stale identity',responsible_assigned_at:'2026-10-16T00:00:00Z',responsibility_event_id:'stale',current_assignee_id:'2',csat_agent_id:'2',last_public_agent_id:'2',last_public_agent_name:'Actual B',last_public_reply_at:'2026-10-16T00:02:00Z',last_public_message_id:'703',agents:history},
 {round_id:'conflict',responsibility_basis:'assignment_conflict',responsible_agent_id:'1',responsible_agent_name:'Conflicting identity',current_assignee_id:'2',agents:history},
 {round_id:'legacy',responsibility_basis:'last_public_agent_reply',responsible_agent_id:'2',responsible_agent_name:'Old reply owner',responsible_reply_at:'2026-10-16T00:02:00Z',responsible_message_id:'old-evidence',current_assignee_id:'2',agents:history},
 {round_id:'missing-id',responsibility_basis:'ics_assignment',responsible_agent_id:null,responsible_agent_name:'Untrusted identity',current_assignee_id:'2',agents:history},
 {round_id:'assigned-no-reply',responsibility_basis:'ics_assignment',responsible_agent_id:'1',responsible_agent_name:'<Original A>',has_agent_reply:false,reply_composition:'none',current_assignee_id:'2',agents:history}
];
const pending = ui.loadQualityList(); respond(0, {items,total:items.length,page:1}); await pending;
const html = node('qualityList').innerHTML;
assert.equal(html.includes('<th>本轮责任坐席</th>'), true);
const rows = [...html.matchAll(/<tbody>(.*?)<\/tbody>/gs)][0][1];
const responsibleCells = [...rows.matchAll(/<tr>(.*?)<\/tr>/gs)].map(row => [...row[1].matchAll(/<td>(.*?)<\/td>/gs)][3][1]);
assert.deepEqual(responsibleCells, ['&lt;Original A&gt;','Chatwoot 坐席 ID 3 · 名称未同步','未记录 ICS 分配','待确认','待确认','待确认','&lt;Original A&gt;']);
for (const forbidden of ['Actual B','Private note author','Stale identity','Conflicting identity','Untrusted identity','Old reply owner']) assert.equal(html.includes(forbidden), false);
assert.equal(html.includes('<Original A>'), false);
for (const [index, item] of items.entries()) {
 const detail = ui.openQualityDetail('round',item.round_id);
 respond(index + 1, {round:item,messages:[{message_id:'999',sender_role:'agent',sender_name:'Different timeline author',is_private:true,round_id:item.round_id}],total:1,page:1}); await detail;
 const meta = node('qualityDetailMeta').innerHTML;
 assert.equal(meta.includes(`<span>本轮责任坐席：${responsibleCells[index]}</span>`), true, item.round_id);
 assert.equal(meta.includes('坐席参与及分配记录（非责任依据）'), true);
 assert.equal(meta.includes('Original A（历史分配（非责任））'), true);
 assert.equal(meta.includes('Actual B（公开回复 1 条）'), true);
 assert.equal(meta.includes('Private note author（有内部备注）'), true);
 assert.equal(meta.includes('Different timeline author'), false, 'the page must not recompute responsibility from timeline messages');
 if (item.responsibility_basis === 'ics_assignment') {
  assert.equal(meta.includes('责任依据：本轮 ICS 原始分配'), true);
  assert.equal(meta.includes(`ICS 原始分配时间：${ui.qualityTime(item.responsible_assigned_at)}`), true);
  assert.equal(meta.includes(`分配事件 ID：${item.responsibility_event_id ?? '待确认'}`), true);
  assert.equal(meta.includes(`触发消息 ID：${item.responsibility_trigger_message_id ?? '待确认'}`), true);
  if (index === 0) assert.equal(meta.includes('ICS 轮次标识：&lt;external-round&gt;'), true);
 } else {
  assert.equal(meta.includes('ICS 原始分配时间：'), false); assert.equal(meta.includes('分配事件 ID：'), false);
  if (item.responsibility_basis === 'assignment_unobserved') assert.equal(meta.includes('责任依据：未记录 ICS 分配'), true);
  if (item.responsibility_basis === 'assignment_conflict') assert.equal(meta.includes('责任依据：ICS 分配记录冲突，待确认'), true);
 }
 if (item.last_public_agent_id) {
  assert.equal(meta.includes('实际最后人工回复人（非责任依据）：Actual B'), true);
  assert.equal(meta.includes(`实际最后人工回复时间：${ui.qualityTime(item.last_public_reply_at)}`), true);
  assert.equal(meta.includes('实际最后人工回复消息 ID：703'), true);
 } else assert.equal(meta.includes('实际最后人工回复人（非责任依据）：'), false);
 assert.equal(meta.includes('Stale identity'), false); assert.equal(meta.includes('Untrusted identity'), false);
 assert.equal(meta.includes('Conflicting identity'), false); assert.equal(meta.includes('Old reply owner'), false);
 assert.equal(meta.includes('old-evidence'), false); assert.equal(meta.includes('<Original A>'), false);
}
""",
    "directory_agent_names_use_exact_scope_and_preserve_original_message_evidence": r"""
ui.state.token = 'session'; ui.state.view = 'quality'; ui.qualityState.ready = true;
ui.qualityState.mode = 'rounds';
ui.qualityState.filters.agents = [
 {source_key:'cw',account_id:1,agent_id:1,agent_name:'beec',agent_name_source:'chatwoot'},
 {source_key:'cw',account_id:1,agent_id:2,agent_name:'QQ',agent_name_source:'chatwoot'},
 {source_key:'cw',account_id:2,agent_id:1,agent_name:'<Other account>',agent_name_source:'chatwoot'},
 {source_key:'other-cw',account_id:1,agent_id:1,agent_name:'Other source',agent_name_source:'chatwoot'},
 {source_key:'cw',account_id:1,agent_id:3,agent_name:'#3',agent_name_source:'unobserved'},
 {source_key:'cw',account_id:1,agent_id:4,agent_name:'  ',agent_name_source:'chatwoot'},
 {source_key:'cw',account_id:1,agent_id:5,agent_name:'坐席5',agent_name_source:'chatwoot'}
];
const base = {source_key:'cw',account_id:1,responsibility_basis:'ics_assignment',responsible_agent_id:'1',responsible_agent_name:'分配坐席1'};
const items = [
 {...base,round_id:'original',current_assignee_id:'2',current_assignee_name:'分配坐席2',last_public_agent_id:'2',last_public_agent_name:'Archived QQ',last_public_reply_at:'2026-10-16T00:02:00Z',last_public_message_id:'702',agents:[{agent_id:'1',agent_name:'分配坐席1',was_assigned:true},{agent_id:'2',agent_name:'Archived QQ',was_assigned:true,replied:true,reply_count:1}]},
 {...base,round_id:'other-account',account_id:'2'},
 {...base,round_id:'other-source',source_key:'other-cw'},
 {...base,round_id:'archived-name',source_key:'deleted-cw',responsible_agent_name:'<Archived A>'},
 {...base,round_id:'unknown',responsible_agent_id:'3',responsible_agent_name:'分配坐席3'},
 {...base,round_id:'empty-live-name',responsible_agent_id:'4',responsible_agent_name:'<Historical name>'},
 {...base,round_id:'real-placeholder-name',responsible_agent_id:'5',responsible_agent_name:'Old name'},
 {...base,round_id:'conflict',responsibility_basis:'assignment_conflict'},
 {...base,round_id:'id-not-remapped',responsible_agent_id:'01',responsible_agent_name:null}
];
const unchanged = JSON.stringify(items);
const list = ui.loadQualityList(); respond(0,{items,total:items.length,page:1}); await list;
const cells = [...node('qualityList').innerHTML.matchAll(/<tbody>(.*?)<\/tbody>/gs)][0][1];
const names = [...cells.matchAll(/<tr>(.*?)<\/tr>/gs)].map(row => [...row[1].matchAll(/<td>(.*?)<\/td>/gs)][3][1]);
assert.deepEqual(names,['beec','&lt;Other account&gt;','Other source','&lt;Archived A&gt;','Chatwoot 坐席 ID 3 · 名称未同步','&lt;Historical name&gt;','坐席5','待确认','Chatwoot 坐席 ID 01 · 名称未同步']);
assert.equal(node('qualityList').innerHTML.includes('分配坐席1'),false);
const message = {message_id:'702',source_key:'cw',account_id:1,sender_id:'2',sender_role:'agent',sender_name:'<Archived QQ>',content:'原始内容 <内容> & unchanged',round_id:'original'};
const originalMessage = JSON.stringify(message);
const detail = ui.openQualityDetail('round','original');
respond(1,{round:items[0],messages:[message],total:1,page:1}); await detail;
const metadata = node('qualityDetailMeta').innerHTML;
assert.equal(metadata.includes('<span>本轮责任坐席：beec</span>'),true);
assert.equal(metadata.includes('<span>轮内最后分配（非责任）：QQ</span>'),true);
assert.equal(metadata.includes('实际最后人工回复人（非责任依据）：QQ'),true);
assert.equal(metadata.includes('beec（历史分配（非责任））'),true);
assert.equal(metadata.includes('QQ（公开回复 1 条，历史分配（非责任））'),true);
assert.equal(metadata.includes('责任依据：本轮 ICS 原始分配'),true);
assert.equal(node('qualityTimeline').innerHTML.includes('<strong>&lt;Archived QQ&gt;</strong>'),true);
assert.equal(node('qualityTimeline').innerHTML.includes('原始内容 &lt;内容&gt; &amp; unchanged'),true);
assert.equal(node('qualityTimeline').innerHTML.includes('<strong>QQ</strong>'),false);
assert.equal(JSON.stringify(items),unchanged);
assert.equal(JSON.stringify(ui.qualityState.messages[0]),originalMessage);
const versions = ui.openQualityVersions(message);
respond(2,{items:[{snapshot_id:1,event_id:1,sender_id:'2',sender_role:'agent',sender_name:'<Version original>',content:'原始版本 <正文>'}],total:1,page:1}); await versions;
assert.equal(node('qualityVersionList').innerHTML.includes('<strong>&lt;Version original&gt;</strong>'),true);
assert.equal(node('qualityVersionList').innerHTML.includes('原始版本 &lt;正文&gt;'),true);
assert.equal(node('qualityVersionList').innerHTML.includes('<strong>QQ</strong>'),false);
""",
    "agent_name_placeholders_do_not_impersonate_real_names": r"""
const scope = {source_key:'cw',account_id:1};
const fallback = 'Chatwoot 坐席 ID 1 · 名称未同步';
for (const placeholder of ['', ' ', '1','#1','坐席 1','分配坐席1','分配坐席 1','Chatwoot 坐席 ID 1','Chatwoot 坐席 ID 1 · 名称未同步']) {
 assert.equal(ui.qualityAgentLabel(scope,'1',placeholder),fallback,placeholder);
 ui.qualityState.filters.agents = [{...scope,agent_id:1,agent_name:placeholder}];
 assert.equal(ui.qualityAgentLabel(scope,'1','Archived real name'),'Archived real name',placeholder);
 ui.qualityState.filters.agents = [];
}
assert.equal(ui.qualityAgentLabel(scope,'1','坐席小王'),'坐席小王');
ui.qualityState.filters.agents = [{...scope,agent_id:1,agent_name:'坐席1',agent_name_source:'chatwoot'}];
assert.equal(ui.qualityAgentLabel(scope,'1','Old name'),'坐席1','an explicit upstream name is evidence even if it resembles an old placeholder');
ui.qualityState.filters.agents = [{...scope,agent_id:1,agent_name:'#1',agent_name_source:'unobserved'}];
assert.equal(ui.qualityAgentLabel(scope,'1',null),fallback);
""",
    "agent_selector_and_export_confirmation_use_real_names_without_changing_ids": r"""
ui.state.token = 'session'; ui.state.view = 'quality'; ui.qualityState.ready = true;
ui.qualityState.filters.agents = [
 {source_key:'cw',account_id:1,agent_id:1,agent_name:'beec',agent_name_source:'chatwoot'},
 {source_key:'cw',account_id:1,agent_id:7,agent_name:'分配坐席7'}
];
ui.populateQualityFilters();
assert.equal(node('qualityAgent').innerHTML.includes('>beec · cw / 账号 1'),true);
assert.equal(node('qualityAgent').innerHTML.includes('Chatwoot 坐席 ID 7 · 名称未同步'),true);
assert.equal(node('qualityAgent').innerHTML.includes('分配坐席7'),false);
for (const [index,agentId] of ['1','7'].entries()) {
 ui.qualityState.applied = new URLSearchParams({source_key:'cw',account_id:'1',agent_id:agentId,agent_scope:'responsible'});
 const pending = ui.exportQuality('list');
 const query = new URL(requests[index].url,'http://localhost').searchParams;
 assert.equal(query.get('source_key'),'cw'); assert.equal(query.get('account_id'),'1'); assert.equal(query.get('agent_id'),agentId);
 assert.equal(confirmations[index].includes(`坐席：${agentId === '1' ? 'beec' : 'Chatwoot 坐席 ID 7 · 名称未同步'}`),true);
 assert.equal(confirmations[index].includes('本轮责任坐席（ICS分配）'),true);
 respond(index,'xlsx'); await pending;
}
""",
    "responsibility_remains_original_a_when_b_replies_and_chatwoot_reassigns": r"""
ui.state.token = 'session'; ui.state.view = 'quality'; ui.qualityState.ready = true;
ui.qualityState.mode = 'rounds';
const original = {round_id:'fixed',responsibility_basis:'ics_assignment',responsible_agent_id:'1',responsible_agent_name:'Original A',responsible_assigned_at:'2026-10-16T00:00:00Z',responsibility_event_id:'1001',responsibility_external_round_id:'round-fixed',responsibility_trigger_message_id:'700'};
for (const [index, replyName] of ['<Actual B>','Later C'].entries()) {
 const item = {...original,current_assignee_id:String(index + 2),agents:[{agent_id:String(index + 2),agent_name:replyName,replied:true,was_assigned:true,reply_count:1}],last_public_agent_id:String(index + 2),last_public_agent_name:replyName,last_public_reply_at:'2026-10-16T00:03:00Z',last_public_message_id:String(index + 701)};
 const list = ui.loadQualityList(); const listIndex = requests.length - 1;
 respond(listIndex, {items:[item],total:1,page:1}); await list;
 assert.equal(node('qualityList').innerHTML.includes('<td>Original A</td>'), true);
 assert.equal(node('qualityList').innerHTML.includes(replyName === '<Actual B>' ? '&lt;Actual B&gt;' : replyName), false);
 const content = `这是 ${replyName} 的原始回复：<内容> & 原文\n第二行`;
 const message = {message_id:String(index + 701),sender_id:String(index + 2),sender_role:'agent',sender_name:replyName,content,is_private:false,round_id:'fixed'};
 const detail = ui.openQualityDetail('round','fixed');
 respond(listIndex + 1, {round:item,messages:[message],total:1,page:1}); await detail;
 const meta = node('qualityDetailMeta').innerHTML;
 assert.equal(meta.includes('<span>本轮责任坐席：Original A</span>'), true);
 assert.equal(meta.includes('责任依据：本轮 ICS 原始分配'), true);
 const safeName = replyName === '<Actual B>' ? '&lt;Actual B&gt;' : replyName;
 assert.equal(meta.includes(`实际最后人工回复人（非责任依据）：${safeName}`), true);
 assert.equal(meta.includes(`实际最后人工回复消息 ID：${message.message_id}`), true);
 const timeline = node('qualityTimeline').innerHTML;
 assert.equal(timeline.includes(`<strong>${safeName}</strong>`), true);
 assert.equal(timeline.includes(`这是 ${safeName} 的原始回复：&lt;内容&gt; &amp; 原文\n第二行`), true);
 assert.equal(timeline.includes('Original A'), false); assert.equal(timeline.includes('<Actual B>'), false);
 assert.equal(ui.qualityState.messages[0].sender_name, replyName); assert.equal(ui.qualityState.messages[0].sender_id, message.sender_id);
 assert.equal(ui.qualityState.messages[0].content, content);
}
""",
    "all_agent_scopes_keep_query_and_export_parameters_with_clear_confirmation": r"""
ui.state.token = 'session'; ui.state.view = 'quality'; ui.qualityState.ready = true;
node('qualityAgent').value = JSON.stringify({source_key:'cw',account_id:'2',agent_id:'7'});
const labels = {responsible:'本轮责任坐席（ICS分配）',replied:'本轮全部回复坐席',assigned:'历史分配（非责任）',current:'会话当前分配（非责任）'};
assert.equal(node('qualityAgentScope').value, 'responsible');
for (const [scope, label] of Object.entries(labels)) {
 node('qualityAgentScope').value = scope;
 const requestIndex = requests.length;
 node('qualityFilterForm').handlers.submit({preventDefault() {}});
 const query = new URL(requests[requestIndex].url).searchParams;
 assert.equal(query.get('agent_scope'), scope); assert.equal(query.get('agent_id'), '7');
 assert.equal(query.get('source_key'), 'cw'); assert.equal(query.get('account_id'), '2');
 respond(requestIndex, {items:[],total:0,page:1}); await tick();
 const pending = ui.exportQuality('list');
 const exported = new URL(requests[requestIndex + 1].url,'http://localhost').searchParams;
 assert.equal(exported.get('agent_scope'), scope); assert.equal(exported.get('agent_id'), '7');
 assert.equal(confirmations.at(-1).includes(`坐席范围：${label}`), true);
 respond(requestIndex + 1, 'xlsx'); await pending;
}
assert.equal(downloads.length, 4);
""",
    "reset_clears_every_filter_and_keeps_panel_open_while_logout_restores_defaults": r"""
ui.state.token = 'session'; ui.state.view = 'quality'; ui.qualityState.ready = true;
for (const id of Object.keys(fields)) node(id).value = 'previous filter';
node('qualityAgentScope').value = 'assigned';
node('qualityAgent').value = JSON.stringify({source_key:'cw',account_id:'2',agent_id:'7'});
node('qualityInbox').value = JSON.stringify({source_key:'cw',account_id:'2',inbox_id:'3'});
ui.qualityState.applied = new URLSearchParams({keyword:'previous filter',agent_scope:'assigned'});
ui.qualityState.page = 4;
ui.setQualityFiltersCollapsed(true);
node('qualityReset').handlers.click();
for (const id of [...Object.keys(fields),'qualityInbox','qualityAgent']) assert.equal(node(id).value,'',id);
assert.equal(ui.collectQualityFilters().toString(),'');
assert.equal(ui.qualityState.applied.toString(),'');
assert.equal(ui.qualityState.page,1);
assert.equal(ui.qualityState.filtersCollapsed,false);
assert.equal(node('qualityFilterFields').classList.contains('hidden'),false);
assert.equal(node('qualityFilterSummary').classList.contains('hidden'),true);
assert.equal(node('qualityToggleFilters').attributes['aria-expanded'],'true');
assert.equal(node('qualityFilterFields').scrollTop,0);
const query = new URL(requests[0].url).searchParams;
assert.equal(query.get('page'),'1');
query.delete('page'); query.delete('page_size');
assert.equal(query.toString(),'','reset queries without any stale filters');
respond(0, {items:[],total:0,page:1}); await tick();
node('qualityReset').handlers.click();
assert.equal(ui.qualityState.filtersCollapsed,false,'repeated reset must keep the filters visible');
assert.equal(ui.collectQualityFilters().toString(),'');
respond(1, {items:[],total:0,page:1}); await tick();
node('qualityAgentScope').value = 'current'; ui.logout(false);
assert.equal(node('qualityAgentScope').value, 'responsible');
assert.equal(ui.collectQualityFilters().get('agent_scope'), 'responsible');
""",
    "late_list_response_cannot_replace_new_query": r"""
ui.state.token = 'session'; ui.state.view = 'quality'; ui.qualityState.ready = true;
ui.qualityState.mode = 'rounds';
const older = ui.loadQualityList();
ui.qualityState.applied = new URLSearchParams({keyword:'new'});
const newer = ui.loadQualityList();
assert.equal(requests[0].options.signal.aborted, true);
respond(1, {items:[{round_id:'new',customer_name:'NEW',source_key:'cw',account_id:'1',round_no:1}],total:1,page:1});
await newer;
respond(0, {items:[{round_id:'old',customer_name:'OLD'}],total:1,page:1});
await older;
assert.equal(node('qualityList').innerHTML.includes('NEW'), true);
assert.equal(node('qualityList').innerHTML.includes('OLD'), false);
""",
    "navigation_and_logout_cancel_and_clear_private_data": r"""
ui.state.token = 'session'; ui.state.view = 'quality'; ui.qualityState.ready = true;
node('qualityTimeline').innerHTML = 'PRIVATE MESSAGE'; node('qualityVersionList').innerHTML = 'PRIVATE VERSION';
const pending = ui.loadQualityList();
ui.stopQuality(); ui.state.view = 'channel';
respond(0, {items:[{customer_name:'PRIVATE LATE'}],total:1}); await pending;
assert.equal(node('qualityList').innerHTML.includes('PRIVATE'), false);
assert.equal(node('qualityTimeline').innerHTML, ''); assert.equal(node('qualityVersionList').innerHTML, '');
assert.equal(ui.qualityState.controllers.size, 0);
node('qualityCustomer').value = 'SECRET USER'; node('qualityAgent').innerHTML = '<option value="a">SECRET AGENT</option>';
ui.logout(false);
assert.equal(node('qualityCustomer').value, ''); assert.equal(node('qualityAgent').innerHTML.includes('SECRET'), false);
assert.equal(ui.qualityState.items.length, 0); assert.equal(ui.qualityState.messages.length, 0);
assert.equal(node('qualityFilters').disabled, true);
""",
    "stale_401_does_not_logout_new_session": r"""
ui.state.token = 'old'; ui.state.view = 'quality';
const pending = ui.qualityRequest('list','/rounds').catch(error => error.name);
ui.stopQuality(true); ui.state.token = 'new'; ui.state.username = 'new user';
respond(0, {}, 401);
assert.equal(await pending, 'AbortError'); assert.equal(ui.state.token, 'new');
assert.equal(ui.state.username, 'new user');
const current = ui.qualityRequest('status','/status').catch(error => error.name);
respond(1, {}, 401); await current;
assert.equal(ui.state.token, ''); assert.equal(node('qualityTimeline').innerHTML, '');
""",
    "complete_user_history_does_not_inherit_filters": r"""
ui.state.token = 'session'; ui.state.view = 'quality'; ui.qualityState.ready = true;
ui.qualityState.applied = new URLSearchParams({keyword:'secret',channel_key:'xingliao',inbox_id:'5',agent_id:'7'});
const pending = ui.openQualityDetail('customer','customer/2');
const url = new URL(requests[0].url);
assert.equal(url.pathname.endsWith('/customers/customer%2F2/history'), true);
assert.equal(url.searchParams.has('keyword'), false); assert.equal(url.searchParams.has('channel_key'), false);
assert.equal(url.searchParams.has('agent_id'), false);
assert.equal(url.searchParams.has('inbox_id'), false);
respond(0, {customer:{display_name:'<USER>',source_key:'cw',account_id:'2'},messages:[{message_id:'m',content:'unassigned',sender_role:'contact',round_id:null}],total:31,page:1});
await pending;
assert.equal(node('qualityTimeline').innerHTML.includes('未归轮'), true);
assert.equal(node('qualityDetailNext').disabled, false);
assert.equal(node('qualityDetailTitle').textContent, '<USER> · 完整历史');
""",
    "versions_are_scoped_paginated_and_escaped": r"""
ui.state.token = 'session'; ui.state.view = 'quality'; ui.qualityState.ready = true;
ui.qualityState.detail = {type:'round',id:'r'};
const pending = ui.openQualityVersions({message_id:'m/1',source_key:'cw-two',account_id:'2',latest_snapshot_id:2});
const url = new URL(requests[0].url);
assert.equal(url.searchParams.get('source_key'), 'cw-two'); assert.equal(url.searchParams.get('account_id'), '2');
assert.equal(url.searchParams.get('page_size'), '30');
respond(0, {items:[{snapshot_id:2,event_id:5,snapshot_kind:'updated',content:'<img src=evil>',sender_name:'<AGENT>',sender_role:'agent'}],total:61,page:1});
await pending;
assert.equal(node('qualityVersionList').innerHTML.includes('<img'), false);
assert.equal(node('qualityVersionList').innerHTML.includes('编辑时观察'), true);
assert.equal(node('qualityVersionsNext').disabled, false);
assert.equal(node('qualityVersionList').innerHTML.includes('下载原始事件'), true);
ui.qualityState.versions.page = 2;
const next = ui.loadQualityVersions(); assert.equal(new URL(requests[1].url).searchParams.get('page'), '2');
respond(1, {items:[],total:61,page:2}); await next;
assert.equal(node('qualityVersionsPrev').disabled, false);
""",
    "disabled_archive_is_local_to_archive_page": r"""
ui.state.token = 'session'; ui.state.view = 'quality';
node('connectionStatus').textContent = 'original service healthy'; node('qualityFilters').disabled = true;
const pending = ui.loadQuality(); respond(0, {enabled:false,ready:false,status:'disabled'}); await pending;
assert.equal(requests.length, 1); assert.equal(node('qualityFilters').disabled, true);
assert.equal(node('qualityStatusTag').textContent, '未启用');
assert.equal(node('connectionStatus').textContent, 'original service healthy');
assert.equal(ui.state.token, 'session');
""",
    "late_raw_event_download_is_cancelled": r"""
ui.state.token = 'session'; ui.state.view = 'quality';
let readBlob = false;
const button = new Element('download');
const pending = ui.downloadQualityEvent({event_id:5},{source_key:'cw-two',account_id:'2'},button);
assert.equal(new URL(requests[0].url,'http://localhost').searchParams.get('account_id'), '2');
ui.stopQuality(); ui.state.view = 'channel';
requests[0].resolve({status:200,ok:true,blob:async () => {readBlob=true; return {};}});
await pending;
assert.equal(readBlob, false); assert.equal(button.disabled, false);
assert.equal(ui.qualityState.controllers.size, 0);
""",
    "ratings_show_only_actual_scores_and_feedback_in_list_and_detail": r"""
ui.state.token = 'session'; ui.state.view = 'quality'; ui.qualityState.ready = true;
ui.qualityState.mode = 'rounds';
const items = [
 {round_id:'not-invited',rating_status:'not_invited',rating:null},
 {round_id:'pending',rating_status:'pending',rating:null,rating_feedback:'must not show'},
 {round_id:'unknown',rating_status:'unknown'},
 {round_id:'rated',rating_status:'rated',rating:5,rating_feedback:'<script>feedback</script>\n第二行'},
 {round_id:'score-only',rating_status:'rated',rating:1,rating_feedback:null}
];
const pending = ui.loadQualityList(); respond(0, {items,total:items.length,page:1}); await pending;
const html = node('qualityList').innerHTML;
assert.equal((html.match(/<td>—<\/td>/g) || []).length, 3);
assert.equal(html.includes('5 分'), true); assert.equal(html.includes('1 分'), true);
assert.equal(html.includes('&lt;script&gt;feedback&lt;/script&gt;\n第二行'), true);
assert.equal(html.includes('<script>'), false); assert.equal(html.includes('must not show'), false);
assert.equal(/未邀约|未评价|待评价|评价状态未知|无文字反馈/.test(html), false);
for (const [index, item] of items.entries()) {
 const detail = ui.openQualityDetail('round',item.round_id);
 respond(index + 1, {round:item,messages:[],total:0,page:1}); await detail;
 const meta = node('qualityDetailMeta').innerHTML;
 assert.equal(meta.includes('服务评价：'), index >= 3);
 assert.equal(meta.includes('评价反馈：'), index === 3);
 assert.equal(meta.includes('must not show'), false);
 assert.equal(/未邀约|未评价|待评价|评价状态未知|无文字反馈/.test(meta), false);
 if (index === 3) {
  assert.equal(meta.includes('服务评价：5 分'), true);
  assert.equal(meta.includes('&lt;script&gt;feedback&lt;/script&gt;\n第二行'), true);
  assert.equal(meta.includes('<script>'), false);
 }
}
node('qualityRatingStatus').value = 'rated'; node('qualityRating').value = '5';
const params = ui.collectQualityFilters();
assert.equal(params.get('rating_status'), 'rated'); assert.equal(params.get('rating'), '5');
""",
    "excel_cancel_confirmation_has_no_requests_downloads_or_busy_state": r"""
ui.state.token = 'session'; ui.state.view = 'quality'; ui.qualityState.ready = true;
ui.qualityState.applied = new URLSearchParams({keyword:'applied search'});
for (const mode of ['rounds','customers']) {
 ui.qualityState.mode = mode; confirmationAnswers.push(false);
 await node('qualityExportList').handlers.click();
 assert.equal(requests.length, 0); assert.equal(downloads.length, 0);
 assert.equal(ui.qualityState.exportJob, null); assert.equal(ui.qualityState.controllers.size, 0);
 const prompt = confirmations.at(-1);
 assert.equal(prompt.includes('当前已应用筛选'), true); assert.equal(prompt.includes('关键词：applied search'), true);
 assert.equal(prompt.includes('全部分页'), true);
 assert.equal(prompt.includes(mode === 'customers' ? '跨会话的完整历史' : '每轮完整消息'), true);
 if (mode === 'customers') assert.equal(prompt.includes('用户历史不受列表时间、关键词等筛选限制'), true);
}
for (const type of ['customer','round']) {
 ui.qualityState.detail = {type,id:'selected',item:{display_name:'User',conversation_id:10,round_no:2}};
 ui.updateQualityExportButtons(); confirmationAnswers.push(false);
 await node('qualityExportDetail').handlers.click();
 assert.equal(requests.length, 0); assert.equal(downloads.length, 0);
 assert.equal(ui.qualityState.exportJob, null); assert.equal(ui.qualityState.controllers.size, 0);
 const prompt = confirmations.at(-1);
 assert.equal(prompt.includes('全部分页'), true); assert.equal(prompt.includes('不受列表筛选限制'), true);
 assert.equal(prompt.includes(type === 'customer' ? '用户“User”' : '会话 10 · 第 2 轮'), true);
 assert.equal(node('qualityExportDetail').disabled, false);
}
assert.equal(confirmations.length, 4);
const accepted = ui.exportQuality('detail'); assert.equal(requests.length, 1);
respond(0, 'xlsx'); await accepted; assert.equal(downloads.length, 1);
""",
    "excel_export_uses_applied_filters_without_pagination": r"""
ui.state.token = 'session'; ui.state.view = 'quality'; ui.qualityState.ready = true;
ui.qualityState.mode = 'customers'; ui.qualityState.page = 3;
ui.qualityState.applied = new URLSearchParams({keyword:'applied search',source_key:'cw',account_id:'2',page:'3',page_size:'30'});
node('qualityKeyword').value = 'not applied';
ui.updateQualityExportButtons(); assert.equal(node('qualityExportList').disabled, false);
assert.ok(node('qualityExportList').title.includes('筛选选中用户完整历史'));
const pending = ui.exportQuality('list');
assert.equal(confirmations.length, 1);
assert.equal(confirmations[0].includes('当前已应用筛选'), true);
assert.equal(confirmations[0].includes('关键词：applied search'), true);
assert.equal(confirmations[0].includes('not applied'), false);
assert.equal(confirmations[0].includes('全部分页'), true);
assert.equal(confirmations[0].includes('跨会话的完整历史'), true);
assert.equal(confirmations[0].includes('用户历史不受列表时间、关键词等筛选限制'), true);
const url = new URL(requests[0].url, 'http://localhost');
assert.equal(url.pathname, '/admin/api/quality/export.xlsx');
assert.equal(url.searchParams.get('mode'), 'customers'); assert.equal(url.searchParams.get('keyword'), 'applied search');
assert.equal(url.searchParams.get('account_id'), '2');
assert.equal(url.searchParams.has('page'), false); assert.equal(url.searchParams.has('page_size'), false);
assert.equal(requests[0].options.headers.Authorization, 'Bearer session');
assert.equal(node('qualityExportList').disabled, true); assert.equal(node('qualityExportDetail').disabled, true);
assert.equal(node('qualityExportList').textContent, '正在导出...');
respond(0, 'xlsx bytes', 200, {'Content-Disposition':"attachment; filename*=UTF-8''%E5%BD%92%E6%A1%A3.xlsx"}); await pending;
assert.equal(downloads.length, 1); assert.equal(downloads[0].filename, '归档.xlsx');
assert.equal(node('qualityExportList').disabled, false); assert.equal(ui.qualityState.exportJob, null);
assert.equal(node('qualityExportList').textContent, '导出筛选结果（Excel）');
""",
    "excel_detail_exports_complete_history_or_round_without_filters": r"""
ui.state.token = 'session'; ui.state.view = 'quality'; ui.qualityState.ready = true;
ui.qualityState.mode = 'rounds';
ui.qualityState.applied = new URLSearchParams({keyword:'secret',page:'3'});
ui.qualityState.detail = {type:'customer',id:'c/2',item:{display_name:'User'},page:3};
ui.updateQualityExportButtons();
assert.ok(node('qualityExportList').title.includes('所选轮次完整消息'));
assert.equal(node('qualityExportDetail').textContent, '下载完整历史（Excel）');
assert.equal(node('qualityExportDetail').disabled, false);
const history = ui.exportQuality('detail');
assert.equal(confirmations[0].includes('用户“User”'), true);
assert.equal(confirmations[0].includes('跨会话的完整历史'), true);
assert.equal(confirmations[0].includes('不受列表筛选限制'), true);
let url = new URL(requests[0].url, 'http://localhost');
assert.equal(url.pathname, '/admin/api/quality/customers/c%2F2/export.xlsx'); assert.equal(url.search, '');
respond(0, 'xlsx bytes'); await history; assert.equal(downloads[0].filename, '对话归档.xlsx');
ui.qualityState.detail = {type:'round',id:'r1',item:{},page:2};
ui.updateQualityExportButtons(); assert.equal(node('qualityExportDetail').textContent, '导出本轮（Excel）');
const round = ui.exportQuality('detail');
assert.equal(confirmations[1].includes('本轮全部分页的完整消息'), true);
assert.equal(confirmations[1].includes('不受列表筛选限制'), true);
url = new URL(requests[1].url, 'http://localhost');
assert.equal(url.pathname, '/admin/api/quality/rounds/r1/export.xlsx'); assert.equal(url.search, '');
respond(1, 'xlsx bytes'); await round; assert.equal(downloads.length, 2);
ui.qualityState.ready = false; ui.updateQualityExportButtons();
assert.equal(node('qualityExportList').disabled, true); assert.equal(node('qualityExportDetail').disabled, true);
await ui.exportQuality('detail'); assert.equal(requests.length, 2); assert.equal(confirmations.length, 2);
""",
    "excel_busy_and_loading_guards_prevent_repeat_confirmation_and_requests": r"""
ui.state.token = 'session'; ui.state.view = 'quality'; ui.qualityState.ready = true;
ui.qualityState.detail = {type:'round',id:'r',item:{}};
ui.qualityState.listLoading = true; await ui.exportQuality('list');
ui.qualityState.listLoading = false; ui.qualityState.detailLoading = true; await ui.exportQuality('detail');
ui.qualityState.detailLoading = false;
assert.equal(confirmations.length, 0); assert.equal(requests.length, 0);
const pending = ui.exportQuality('list');
assert.equal(node('qualityExportList').disabled, true); assert.equal(node('qualityExportDetail').disabled, true);
await ui.exportQuality('list'); await ui.exportQuality('detail');
assert.equal(confirmations.length, 1); assert.equal(requests.length, 1);
respond(0, {detail:'已有导出任务正在运行，请稍后重试。'}, 429); await pending;
assert.equal(downloads.length, 0); assert.equal(node('toast').textContent, '已有导出任务正在运行，请稍后重试。');
assert.equal(ui.qualityState.exportJob, null); assert.equal(ui.qualityState.controllers.size, 0);
assert.equal(node('qualityExportList').disabled, false); assert.equal(node('qualityExportDetail').disabled, false);
const retry = ui.exportQuality('detail'); assert.equal(confirmations.length, 2); assert.equal(requests.length, 2);
respond(1, 'xlsx'); await retry; assert.equal(downloads.length, 1);
""",
    "excel_error_json_and_invalid_body_show_error_without_download": r"""
ui.state.token = 'session'; ui.state.view = 'quality'; ui.qualityState.ready = true;
const pending = ui.exportQuality('list');
respond(0, {detail:'匹配记录过多，请缩小筛选范围。'}, 422); await pending;
assert.equal(downloads.length, 0); assert.equal(node('toast').textContent, '匹配记录过多，请缩小筛选范围。');
assert.equal(node('qualityExportList').disabled, false);
const invalid = ui.exportQuality('list');
requests[1].resolve({status:503,json:async () => {throw new Error('not json');}}); await invalid;
assert.equal(downloads.length, 0); assert.equal(node('toast').textContent, '导出失败，请稍后重试。');
const blobError = ui.exportQuality('list');
requests[2].resolve({status:200,headers:new Headers(),blob:async () => {throw new Error('读取导出文件失败');}}); await blobError;
assert.equal(downloads.length, 0); assert.equal(node('toast').textContent, '读取导出文件失败');
assert.equal(node('qualityExportList').disabled, false); assert.equal(ui.qualityState.controllers.size, 0);
""",
    "excel_navigation_cancels_even_after_blob_read_started": r"""
ui.state.token = 'session'; ui.state.view = 'quality'; ui.qualityState.ready = true;
let completeBlob;
const pending = ui.exportQuality('list');
requests[0].resolve({status:200,headers:new Headers(),blob:() => new Promise(resolve => {completeBlob=resolve;})});
await tick(); assert.equal(typeof completeBlob, 'function');
ui.stopQuality(); ui.state.view = 'channel';
assert.equal(requests[0].options.signal.aborted, true);
completeBlob('xlsx bytes'); await pending;
assert.equal(downloads.length, 0); assert.equal(node('qualityExportList').disabled, true);
assert.equal(ui.qualityState.controllers.size, 0); assert.equal(ui.qualityState.exportJob, null);
""",
    "excel_context_changes_cancel_old_download_and_keep_new_busy": r"""
ui.state.token = 'session'; ui.state.view = 'quality'; ui.qualityState.ready = true;
ui.qualityState.mode = 'rounds';
const old = ui.exportQuality('list');
ui.changeQualityMode('customers');
assert.equal(requests[0].options.signal.aborted, true);
respond(1, {items:[],total:0,page:1}); await tick();
const newer = ui.exportQuality('list'); assert.equal(node('qualityExportList').disabled, true);
respond(0, 'OLD'); await old;
assert.equal(downloads.length, 0); assert.equal(node('qualityExportList').disabled, true);
respond(2, 'NEW'); await newer; assert.equal(downloads.length, 1);
const filtered = ui.exportQuality('list'); node('qualityKeyword').value = 'changed'; ui.applyQualityFilters();
assert.equal(requests[3].options.signal.aborted, true);
respond(4, {items:[],total:0,page:1}); await tick(); respond(3,'OLD FILTER'); await filtered;
assert.equal(downloads.length, 1);
ui.qualityState.detail = {type:'round',id:'r1',item:{}};
const detail = ui.exportQuality('detail'); ui.closeQualityDetail();
assert.equal(requests[5].options.signal.aborted, true); respond(5,'CLOSED DETAIL'); await detail;
assert.equal(downloads.length, 1); assert.equal(node('qualityExportDetail').classList.contains('hidden'), true);
const loggedOut = ui.exportQuality('list'); ui.logout(false); respond(6,'OLD LOGIN'); await loggedOut;
assert.equal(downloads.length, 1); assert.equal(node('qualityExportList').disabled, true);
""",
    "excel_filename_fallback_rejects_wrong_extension": r"""
assert.equal(ui.qualityExportFilename('attachment; filename="report.xlsx"'), 'report.xlsx');
assert.equal(ui.qualityExportFilename('attachment; filename="../report.xlsx"'), '.._report.xlsx');
assert.equal(ui.qualityExportFilename('attachment; filename="script.html"'), '对话归档.xlsx');
assert.equal(ui.qualityExportFilename("attachment; filename*=UTF-8''%INVALID"), '对话归档.xlsx');
""",
}


def test_page_has_all_referenced_elements_and_unique_ids() -> None:
    class IdParser(HTMLParser):
        def __init__(self) -> None:
            super().__init__()
            self.ids: list[str] = []

        def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
            self.ids.extend(value for name, value in attrs if name == "id" and value is not None)

    html = ADMIN_PAGE.read_text(encoding="utf-8")
    parser = IdParser()
    parser.feed(html)
    assert len(parser.ids) == len(set(parser.ids))
    references = set(re.findall(r'\$\("((?:quality|presence)[^"]+)"\)', html))
    assert references <= set(parser.ids), references - set(parser.ids)


def test_reception_choices_remain_and_removed_filters_have_no_controls() -> None:
    html = ADMIN_PAGE.read_text(encoding="utf-8")
    reception = re.search(r'<select id="qualityReception" name="reception_type">(.*?)</select>', html)
    assert reception is not None
    assert re.findall(r'<option value="([^"]*)">([^<]*)</option>', reception.group(1)) == [
        ("", "全部分类"), ("ai_only", "纯 AI 接待"), ("human", "人工介入")
    ]
    for removed in ("qualitySource", "qualityChannel", "qualityRoundStatus", "qualityAttachment"):
        assert f'id="{removed}"' not in html
    assert "更多筛选" not in html and "quality-advanced" not in html


def test_archive_has_no_unlinked_rating_mode_or_unanswered_rating_filters() -> None:
    html = ADMIN_PAGE.read_text(encoding="utf-8")
    assert "unlinked" not in html.lower()
    assert "qualityUnlinkedTab" not in html
    assert "待关联评价" not in html
    assert "data-quality-rating-event" not in html
    rating = re.search(r'<select id="qualityRatingStatus" name="rating_status">(.*?)</select>', html)
    assert rating is not None
    assert re.findall(r'<option value="([^"]*)">([^<]*)</option>', rating.group(1)) == [
        ("", "全部评价状态"), ("rated", "已评价")
    ]


def test_agent_scope_defaults_to_round_responsibility_and_labels_other_scopes() -> None:
    html = ADMIN_PAGE.read_text(encoding="utf-8")
    scope = re.search(r'<select id="qualityAgentScope" name="agent_scope">(.*?)</select>', html)
    assert scope is not None
    assert re.findall(r'<option value="([^"]*)"(?: selected)?>([^<]*)</option>', scope.group(1)) == [
        ("", "请选择坐席范围"),
        ("responsible", "本轮责任坐席（ICS分配）"), ("replied", "本轮全部回复坐席"),
        ("assigned", "历史分配（非责任）"), ("current", "会话当前分配（非责任）")
    ]
    assert '<option value="responsible" selected>' in scope.group(1)
    assert "responsible_reply_at" not in html
    assert "responsible_message_id" not in html
    assert "本轮责任坐席（最后人工回复）" not in html


@pytest.mark.skipif(NODE is None, reason="Node.js is needed to verify the browser JavaScript")
@pytest.mark.parametrize("case", CASES)
def test_archive_browser_behavior(case: str) -> None:
    html = ADMIN_PAGE.read_text(encoding="utf-8")
    match = re.search(r"<script>(.*?)</script>", html, flags=re.DOTALL)
    assert match is not None
    program = (
        HARNESS
        + "\nvm.runInContext("
        + json.dumps(match.group(1) + EXPORTS)
        + ", context);\nconst ui = context.ui;\n"
        + "(async () => {\n"
        + CASES[case]
        + "\n})().catch(error => {console.error(error); process.exitCode = 1;});\n"
    )
    result = subprocess.run(
        [NODE, "-"], input=program, text=True, encoding="utf-8", capture_output=True, timeout=20
    )
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize("control,name", [("qualityAiReviewStatus", "ai_review_status"),
                                         ("qualityHumanReviewStatus", "human_review_status")])
def test_review_filter_controls_submit_supported_verdicts(control, name):
    html = ADMIN_PAGE.read_text(encoding="utf-8")
    select = re.search(r'<select id="' + control + r'" name="' + name + r'">(.*?)</select>', html)
    assert select is not None
    assert re.findall(r'<option value="([^"]*)">([^<]*)</option>', select.group(1)) == [
        ("", "全部结果"), ("passed", "合格"), ("failed", "不合格"), ("pending", "待定")
    ]
