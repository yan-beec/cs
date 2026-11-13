"""通过页面实际脚本验证留档范围、下载及退出后的响应隔离。"""
from pathlib import Path
import subprocess


def test_admin_scopes_jobs_and_streaming_download_without_blob():
    script = Path(__file__).parents[1] / "offline/web/admin.js"
    code = r'''
const fs=require('node:fs'),vm=require('node:vm'),assert=require('node:assert/strict');
const nodes=new Map(),requests=[],anchors=[],timers=new Set();
const node=id=>{
  if(!nodes.has(id)){
    const classes=new Set(id==='offlinePanel'?['hidden']:[]);
    nodes.set(id,{value:id==='offlineMode'?'month':'',innerHTML:'',textContent:'',disabled:false,
      classList:{contains:c=>classes.has(c),toggle:(c,force)=>{const on=force===undefined?!classes.has(c):force;on?classes.add(c):classes.delete(c);return on;}},
      querySelectorAll(selector){
        const match=this.innerHTML.match(/data-offline-download="([a-f0-9]+)"/);
        if(selector==='[data-offline-download]' && match){
          this.download={dataset:{offlineDownload:match[1]}};return [this.download];
        }
        const del=this.innerHTML.match(/data-offline-delete="([a-f0-9]+)" data-offline-name="([^"]*)" data-offline-files="([^"]*)"/);
        if(selector==='[data-offline-delete]' && del){
          this.remove={dataset:{offlineDelete:del[1],offlineName:del[2],offlineFiles:del[3]},disabled:false};return [this.remove];
        }
        return [];
      }});
  }
  return nodes.get(id);
};
let token='admin',items=[],pending=null,estimate={level:'ok',messages:120,rounds:8,customers:3,attachments:2,attachment_bytes:2048,estimated_bytes:90000,warnings:[]};const confirms=[],toasts=[];
const context={window:{location:{origin:'https://admin.example'},confirm:t=>{confirms.push(t);return true;}},document:{getElementById:node,
 createElement:()=>({click(){anchors.push(this.href)},remove(){}}),body:{appendChild(){}}},URL,Date,
 setInterval:fn=>{timers.add(fn);return fn},clearInterval:fn=>timers.delete(fn)};
vm.createContext(context);vm.runInContext(fs.readFileSync(process.argv[1],'utf8'),context);
const ui=context.window.ICSOfflineArchives.create({getToken:()=>token,getFilters:()=>({mode:'customers',summary:'关键词：hello',query:new URLSearchParams('keyword=hello&start=2026-10-01')}),toast:(t,e)=>toasts.push([t,!!e]),
 api:async(url,options={})=>{
  requests.push({url,...options});
  if(url.endsWith('/ticket'))return {url:'/admin/api/quality/offline/'+ 'a'.repeat(32)+'/download?ticket=signed'};
  if(url.includes('/estimate')){if(!estimate)throw new Error('归档库繁忙');return estimate;}
  if(options.method==='POST')return {id:'a'.repeat(32)};
  if(pending)return await pending;
  return {items,retention_days:7};
 }});
const tick=()=>new Promise(r=>setImmediate(r));
(async()=>{
 ui.start();node('offlineToggle').onclick();await tick();
 assert.equal(timers.size,1);
 assert(!node('offlinePanel').classList.contains('hidden'));assert.equal(node('offlineToggle').textContent,'收起留档');
 node('offlineClose').onclick();
 assert(node('offlinePanel').classList.contains('hidden'));assert.equal(node('offlineToggle').textContent,'离线 HTML 留档');
 node('offlineToggle').onclick();await tick();
 node('offlineMonth').value='2026-10';
 await node('offlineCreate').onclick();
 const creates=()=>requests.filter(r=>r.method==='POST'&&!r.url.includes('/estimate')&&!r.url.endsWith('/ticket'));
 let post=creates().at(-1);
 // 先预估再生成：预估请求与生成请求使用同一范围，确认框写明数量和失败提醒。
 assert(requests.some(r=>r.url==='/admin/api/quality/offline/estimate'));
 assert(confirms.at(-1).includes('消息 120 条') && confirms.at(-1).includes('导出失败') && confirms.at(-1).includes('不影响客服系统'));
 assert.equal(post.url,'/admin/api/quality/offline');
 assert.deepEqual(JSON.parse(post.body),{mode:'month',month:'2026-10'});
 assert(confirms.at(-1).includes('按结束时间归月的完整轮次＋当月未归轮消息'));
 assert(confirms.at(-1).includes('跨月轮次导出整轮（含上月消息）'));
 assert(confirms.at(-1).includes('未结束的轮次按导出时最后一条消息归属'));
 node('offlineMode').value='all';node('offlineMode').onchange();await node('offlineCreate').onclick();
 post=creates().at(-1);assert.deepEqual(JSON.parse(post.body),{mode:'all'});
 node('offlineMode').value='current';node('offlineMode').onchange();await node('offlineCreate').onclick();
 post=creates().at(-1);assert(post.url.includes('keyword=hello'));assert.equal(JSON.parse(post.body).mode,'rounds');
 assert(requests.findLast(r=>r.url.includes('/estimate')).url.includes('keyword=hello'));
 assert(confirms.at(-1).includes('关键词：hello'));
 assert.equal(JSON.parse(post.body).summary,'关键词：hello');
 assert.equal(JSON.parse(post.body).view,'customers');
 assert.deepEqual(JSON.parse(requests.findLast(r=>r.url.includes('/estimate')).body),JSON.parse(post.body));
 estimate={level:'ok',messages:4,rounds:0,customers:1,attachments:0,unrounded_messages:4};
 await node('offlineCreate').onclick();
 assert(confirms.at(-1).includes('含未归轮消息 4 条'));
 // 超过硬上限：不弹确认、不创建任务。
 let before=creates().length,asked=confirms.length;
 estimate={level:'block',messages:400000,rounds:9000,customers:10,attachments:0,reason:'超过单个离线页面上限'};
 await node('offlineCreate').onclick();
 assert.equal(creates().length,before);assert.equal(confirms.length,asked);assert(node('offlineEstimate').textContent.includes('超过单个离线页面上限'));
 assert.equal(node('offlineCreate').disabled,false);
 // 没有数据：不创建。
 estimate={level:'empty',messages:0,rounds:0,customers:0,attachments:0};
 await node('offlineCreate').onclick();assert.equal(creates().length,before);
 // 超过提醒线：两次确认后才创建。
 estimate={level:'warn',messages:40000,rounds:6000,customers:10,attachments:0,warnings:['消息 40,000 条，超过提醒线 30,000 条']};
 await node('offlineCreate').onclick();
 assert.equal(creates().length,before+1);assert(confirms.at(-2).includes('数据量较大'));assert(confirms.at(-1).includes('再次确认'));
 // 预估失败：不继续生成。
 estimate=null;
 await node('offlineCreate').onclick();assert.equal(creates().length,before+1);assert(node('offlineEstimate').textContent.includes('归档库繁忙'));
 estimate={level:'ok',messages:1,rounds:1,customers:1,attachments:0,warnings:[]};
 items=[{id:'a'.repeat(32),status:'completed',request:{mode:'all'},created:1,progress:{messages:2101,rounds:2,unrounded_messages:3},size:1024}];
 await [...timers][0]();await tick();
 assert(node('offlineJobs').innerHTML.includes('对话 2 轮 · 未归轮消息 3 条'));
 await node('offlineJobs').download.onclick();
 assert.equal(anchors.length,1);assert(anchors[0].includes('/download?ticket=signed'));
 items=[{id:'a'.repeat(32),status:'downloaded',available:true,delete_at:7201,request:{mode:'all'},created:1,progress:{},size:1024}];
 await [...timers][0]();await tick();
 assert(node('offlineJobs').innerHTML.includes('重新下载'));assert(node('offlineJobs').innerHTML.includes('后删除'));
 items=[{id:'a'.repeat(32),status:'downloaded',available:false,request:{mode:'all'},created:1,progress:{},size:1024}];
 await [...timers][0]();await tick();
 assert(!node('offlineJobs').innerHTML.includes('data-offline-download'));assert(node('offlineJobs').innerHTML.includes('服务器端已删除'));
 // 已结束的记录可删除：先确认，再调用删除接口；生成中的任务不显示删除按钮。
 await node('offlineJobs').remove.onclick();
 const removed=requests.findLast(r=>r.method==='DELETE');
 assert.equal(removed.url,'/admin/api/quality/offline/'+'a'.repeat(32)+'/record');
 assert(confirms.at(-1).includes('确认删除留档记录') && !confirms.at(-1).includes('一并删除'));
 items=[{id:'a'.repeat(32),status:'completed',available:true,request:{mode:'all'},created:1,progress:{},size:1024}];
 await [...timers][0]();await tick();
 await node('offlineJobs').remove.onclick();
 assert(confirms.at(-1).includes('一并删除'));
 items=[{id:'a'.repeat(32),status:'running',request:{mode:'all'},created:1,progress:{},size:0}];
 await [...timers][0]();await tick();
 assert(!node('offlineJobs').innerHTML.includes('data-offline-delete'));
 let finish;pending=new Promise(resolve=>finish=resolve);[...timers][0]();await tick();
 ui.stop();token='';finish({items});await tick();
 assert.equal(timers.size,0);assert.equal(node('offlineJobs').innerHTML,'');
})().catch(error=>{console.error(error);process.exitCode=1});
'''
    run = subprocess.run(["node", "-e", code, str(script.resolve())], capture_output=True, text=True)
    assert run.returncode == 0, run.stderr
