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
const defaults = {qualityAgentScope: 'responsible', qualityTimeField: 'started_at', qualityMessageScope: 'all'};
const decode = value => value.replace(/&quot;/g, '"').replace(/&#039;/g, "'").replace(/&lt;/g, '<').replace(/&gt;/g, '>').replace(/&amp;/g, '&');
class Element {
  constructor(id) {
    this.id = id; this._html = ''; this.value = defaults[id] || ''; this.textContent = ''; this.disabled = false;
    this.dataset = {}; this.handlers = {}; this.attributes = {}; this.style = {};
    const classes = new Set();
    this.classList = {add: name => classes.add(name), remove: name => classes.delete(name), contains: name => classes.has(name), toggle(name, value) {value ? classes.add(name) : classes.delete(name);}};
  }
  set innerHTML(value) {this._html = value; if (['qualitySource', 'qualityAgent', 'qualityInbox', 'qualityChannel'].includes(this.id)) this.value = this.options[0]?.value || '';}
  get innerHTML() {return this._html;}
  get options() {return [...this._html.matchAll(/<option value="([^"]*)"/g)].map(match => ({value: decode(match[1])}));}
  addEventListener(name, handler) {this.handlers[name] = handler;}
  setAttribute(name, value) {this.attributes[name] = value;}
  removeAttribute(name) {delete this.attributes[name];}
  querySelectorAll() {return [];}
  scrollIntoView() {}
  reset() {for (const [id, node] of nodes) if (id.startsWith('quality')) node.value = defaults[id] || '';}
}
const node = id => {if (!nodes.has(id)) nodes.set(id, new Element(id)); return nodes.get(id);};
const fields = {
 qualityCustomer: 'customer_query', qualityKeyword: 'keyword', qualityChannel: 'channel_key',
 qualityReception: 'reception_type', qualityAgentScope: 'agent_scope', qualityConversation: 'conversation_id',
 qualityRoundNo: 'round_no', qualityRoundStatus: 'status', qualityTimeField: 'time_field', qualityStart: 'start',
 qualityEnd: 'end', qualityRatingStatus: 'rating_status', qualityRating: 'rating', qualityMessageScope: 'message_scope',
 qualityAttachment: 'has_attachment'
};
class BrowserFormData {
  constructor() {this.values = Object.entries(fields).map(([id, name]) => [name, node(id).value]);}
  [Symbol.iterator]() {return this.values[Symbol.iterator]();}
}
const requests = [], downloads = [], confirmations = [], confirmationAnswers = [];
class BrowserURL extends URL {
  static createObjectURL() {return 'blob:archive-preview';}
  static revokeObjectURL() {}
}
const context = vm.createContext({
 document: {getElementById: node, querySelectorAll: () => [], title: '', body: {appendChild() {}},
   createElement() {return {href:'',download:'',click() {downloads.push({url:this.href,filename:this.download});},remove() {}};}},
 window: {location: {origin: 'http://localhost:3001', hash: '#quality'}, addEventListener() {},
   confirm(message) {confirmations.push(message); return confirmationAnswers.length ? confirmationAnswers.shift() : true;}},
 localStorage: {getItem() {return null;}, setItem() {}, removeItem() {}},
 history: {replaceState() {}}, URL: BrowserURL, URLSearchParams, Headers, AbortController, DOMException, Intl, Date,
 FormData: BrowserFormData, setTimeout() {}, clearTimeout() {}, setInterval() {}, clearInterval() {},
 fetch: (url, options) => new Promise((resolve, reject) => requests.push({url, options, resolve, reject})),
 console
});
const respond = (index, payload, status = 200, headers = {}) => requests[index].resolve({status, ok: status >= 200 && status < 300, headers:new Headers(headers), json: async () => payload, blob: async () => ({payload})});
const tick = () => new Promise(resolve => setImmediate(resolve));
"""

EXPORTS = """
globalThis.ui = {state, qualityState, collectQualityFilters, populateQualityFilters, qualityTime,
 qualitySafeLink, qualityAttachments, renderQualityTimeline, qualitySnapshotLabel, loadQualityList,
 openQualityDetail, loadQualityDetail, openQualityVersions, loadQualityVersions, stopQuality, logout,
 qualityRequest, loadQuality, downloadQualityEvent, changeQualityMode, closeQualityDetail, applyQualityFilters,
 exportQuality, cancelQualityExport, updateQualityExportButtons, qualityExportFilename};
"""

CASES = {
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
node('qualitySource').value = JSON.stringify({source_key:'cw-two',account_id:'2'});
ui.populateQualityFilters();
assert.equal(node('qualityAgent').innerHTML.includes('>A ·'), false);
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
    "inbox_and_agent_choices_keep_the_visible_source_scope": r"""
ui.qualityState.filters = {sources:[],channels:[{key:'business',name:'ICS业务'}],agents:[
 {source_key:'cw-one',account_id:1,agent_id:'7',agent_name:'Agent One'},
 {source_key:'cw-two',account_id:2,agent_id:'7',agent_name:'<Agent Two>'}
],inboxes:[
 {source_key:'cw-one',account_id:1,inbox_id:5,name:'Inbox One',channel_key:'business'},
 {source_key:'cw-two',account_id:2,inbox_id:5,name:'<Inbox Two>',channel_key:null}
]};
ui.populateQualityFilters();
assert.equal(node('qualitySource').options.length, 3, 'directory-only accounts must be selectable');
assert.equal(node('qualityInbox').innerHTML.includes('&lt;Inbox Two&gt;'), true);
node('qualityInbox').value = JSON.stringify({source_key:'cw-two',account_id:'2',inbox_id:'5'});
node('qualityInbox').handlers.change();
assert.deepEqual(JSON.parse(node('qualitySource').value), {source_key:'cw-two',account_id:'2'});
assert.equal(node('qualityAgent').innerHTML.includes('Agent One'), false);
assert.equal(node('qualityAgent').innerHTML.includes('&lt;Agent Two&gt;'), true);
assert.equal(node('qualityChannel').value, '', 'a Chatwoot inbox must not silently select an ICS channel');
node('qualityAgent').value = JSON.stringify({source_key:'cw-two',account_id:'2',agent_id:'7'});
node('qualityAgent').handlers.change();
let params = ui.collectQualityFilters();
assert.equal(params.get('inbox_id'), '5'); assert.equal(params.get('agent_id'), '7');
assert.equal(params.get('source_key'), 'cw-two'); assert.equal(params.get('account_id'), '2');
node('qualitySource').value = JSON.stringify({source_key:'cw-one',account_id:'1'});
node('qualitySource').handlers.change();
assert.equal(node('qualityInbox').value, ''); assert.equal(node('qualityAgent').value, '');
assert.equal(node('qualityInbox').innerHTML.includes('Inbox One'), true);
assert.equal(node('qualityInbox').innerHTML.includes('Inbox Two'), false);
node('qualitySource').value = ''; node('qualitySource').handlers.change();
node('qualityAgent').value = JSON.stringify({source_key:'cw-one',account_id:'1',agent_id:'7'});
node('qualityAgent').handlers.change();
assert.equal(JSON.parse(node('qualitySource').value).source_key, 'cw-one');
node('qualitySource').value = ''; node('qualitySource').handlers.change();
params = ui.collectQualityFilters();
assert.equal(params.has('source_key'), false); assert.equal(params.has('account_id'), false);
assert.equal(params.has('agent_id'), false); assert.equal(params.has('inbox_id'), false);
""",
    "conflicting_inbox_and_agent_scopes_never_silently_override_source": r"""
ui.state.token = 'session'; ui.state.view = 'quality'; ui.qualityState.ready = true;
node('qualitySource').value = JSON.stringify({source_key:'cw-one',account_id:'1'});
node('qualityInbox').value = JSON.stringify({source_key:'cw-two',account_id:'2',inbox_id:'5'});
node('qualityAgent').value = JSON.stringify({source_key:'cw-one',account_id:'1',agent_id:'7'});
assert.throws(() => ui.collectQualityFilters(), /来源.*账号不一致/);
ui.applyQualityFilters(); assert.equal(requests.length, 0);
assert.equal(node('qualityListSummary').textContent.includes('不一致'), true);
node('qualitySource').value = ''; node('qualityInbox').value = '';
assert.equal(ui.collectQualityFilters().get('source_key'), 'cw-one');
""",
    "round_inbox_and_range_filters_match_list_and_excel_parameters": r"""
ui.state.token = 'session'; ui.state.view = 'quality'; ui.qualityState.ready = true;
node('qualitySource').value = JSON.stringify({source_key:'cw',account_id:'2'});
node('qualityInbox').value = JSON.stringify({source_key:'cw',account_id:'2',inbox_id:'5'});
node('qualityAgent').value = JSON.stringify({source_key:'cw',account_id:'2',agent_id:'7'});
node('qualityChannel').value = 'business'; node('qualityConversation').value = '123';
node('qualityRoundNo').value = '2'; node('qualityRoundStatus').value = 'resolved';
node('qualityReception').value = 'human'; node('qualityMessageScope').value = 'private';
node('qualityTimeField').value = 'message_at'; node('qualityAttachment').value = 'false';
node('qualityStart').value = '2026-10-16T08:00'; node('qualityEnd').value = '2026-10-17T00:00';
for (const scope of ['responsible','replied','assigned','current']) {
 node('qualityAgentScope').value = scope; assert.equal(ui.collectQualityFilters().get('agent_scope'), scope);
}
node('qualityFilterForm').handlers.submit({preventDefault() {}});
const list = new URL(requests[0].url).searchParams;
const expected = {source_key:'cw',account_id:'2',inbox_id:'5',agent_id:'7',agent_scope:'current',
 channel_key:'business',conversation_id:'123',round_no:'2',status:'resolved',reception_type:'human',
 message_scope:'private',time_field:'message_at',has_attachment:'false',
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
node('qualityRoundStatus').value = 'active';
for (const reception of ['ai_only','human']) {
 node('qualityReception').value = reception;
 const params = ui.collectQualityFilters();
 assert.equal(params.get('reception_type'), reception);
 assert.equal(params.get('status'), 'active', 'lifecycle and reception filters must remain independent');
}
""",
    "round_responsibility_uses_only_original_ics_assignment_evidence": r"""
ui.state.token = 'session'; ui.state.view = 'quality'; ui.qualityState.ready = true;
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
assert.deepEqual(responsibleCells, ['&lt;Original A&gt;','坐席 3','未记录 ICS 分配','待确认','待确认','待确认','&lt;Original A&gt;']);
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
    "responsibility_remains_original_a_when_b_replies_and_chatwoot_reassigns": r"""
ui.state.token = 'session'; ui.state.view = 'quality'; ui.qualityState.ready = true;
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
    "reset_and_logout_restore_responsible_scope_default": r"""
ui.state.token = 'session'; ui.state.view = 'quality'; ui.qualityState.ready = true;
node('qualityAgentScope').value = 'assigned';
node('qualityAgent').value = JSON.stringify({source_key:'cw',account_id:'2',agent_id:'7'});
node('qualityReset').handlers.click();
assert.equal(node('qualityAgentScope').value, 'responsible');
const query = new URL(requests[0].url).searchParams;
assert.equal(query.get('agent_scope'), 'responsible'); assert.equal(query.has('agent_id'), false);
respond(0, {items:[],total:0,page:1}); await tick();
node('qualityAgentScope').value = 'current'; ui.logout(false);
assert.equal(node('qualityAgentScope').value, 'responsible');
assert.equal(ui.collectQualityFilters().get('agent_scope'), 'responsible');
""",
    "late_list_response_cannot_replace_new_query": r"""
ui.state.token = 'session'; ui.state.view = 'quality'; ui.qualityState.ready = true;
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
    references = set(re.findall(r'\$\("(quality[^"]+)"\)', html))
    assert references <= set(parser.ids), references - set(parser.ids)


def test_reception_and_lifecycle_filters_keep_separate_choices() -> None:
    html = ADMIN_PAGE.read_text(encoding="utf-8")
    reception = re.search(r'<select id="qualityReception" name="reception_type">(.*?)</select>', html)
    lifecycle = re.search(r'<select id="qualityRoundStatus" name="status">(.*?)</select>', html)
    assert reception is not None and lifecycle is not None
    assert re.findall(r'<option value="([^"]*)">([^<]*)</option>', reception.group(1)) == [
        ("", "全部分类"), ("ai_only", "纯 AI 接待"), ("human", "人工介入")
    ]
    assert re.findall(r'<option value="([^"]*)">([^<]*)</option>', lifecycle.group(1)) == [
        ("", "全部状态"), ("active", "未结束"), ("resolved", "已解决"),
        ("idle_boundary", "超时分轮"), ("unknown", "待确认")
    ]


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
    assert re.findall(r'<option value="([^"]*)">([^<]*)</option>', scope.group(1)) == [
        ("responsible", "本轮责任坐席（ICS分配）"), ("replied", "本轮全部回复坐席"),
        ("assigned", "历史分配（非责任）"), ("current", "会话当前分配（非责任）")
    ]
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
