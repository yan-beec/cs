(function(root){
  'use strict';
  root.ICSOfflineArchives={create({api,getToken,getFilters,toast}){
    const $=id=>document.getElementById(id);
    const panel=$('offlinePanel'), list=$('offlineJobs'), base='/admin/api/quality/offline';
    const escape=v=>String(v??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
    let timer=null,active=false,busy=false,epoch=0,downloaded=2;
    $('offlineMonth').value=new Date().toLocaleDateString('sv-SE',{timeZone:'Asia/Shanghai'}).slice(0,7);
    const update=()=>{
      const mode=$('offlineMode').value;
      $('offlineMonth').disabled=mode!=='month';
    };
    function render(items){
      const labels={queued:'等待生成',running:'正在生成',cancelling:'正在取消',cancelled:'已取消',completed:'已完成，待下载',failed:'失败',downloaded:'已下载（服务器端已删除）',expired:'未下载已过期（服务器端已删除）'};
      const stages={extracting:'提取消息',media:'整理消息与图片',finishing:'写入页面',packing:'生成 ZIP'};
      const at=s=>new Date(s*1000).toLocaleString('zh-CN',{timeZone:'Asia/Shanghai',hour12:false});
      list.innerHTML=items.length ? items.map(j=>{
        const p=j.progress || {}, scope=j.request.mode==='month' ? j.request.month : j.request.mode==='all' ? '全部历史' : '当前筛选';
        const name=j.filename || scope;
        const again=j.status==='downloaded' && j.available;
        const label=again ? '已下载，可重新下载' : labels[j.status] || escape(j.status);
        const removal=(j.status==='completed' || again) && j.delete_at ? `<p class="hint">服务器端文件将于 ${escape(at(j.delete_at))} 后删除${again?'，请确认已保存好下载的文件':''}。</p>` : '';
        // 双包：HTML 只含文字；附件 ZIP 解压到 HTML 所在文件夹后，页面里才能看到图片和视频。
        const usage=(j.status==='completed' || again) && j.media_size && j.media_filename ? `<p class="hint">图片、视频在附件 ZIP 里：两个文件都下载后放进同一个文件夹，把“${escape(j.media_filename)}”解压到当前文件夹（HTML 旁边出现“${escape(j.media_folder||'')}”文件夹），再打开 HTML 即可查看。</p>` : '';
        const downloadable=j.status==='completed' || again;
        const busyJob=['queued','running','cancelling'].includes(j.status);
        const actions=[
          downloadable?`<button class="button primary" type="button" data-offline-download="${j.id}" data-offline-part="html">${again?'重新下载 HTML':'下载聊天记录 HTML'}</button>`:'',
          downloadable && j.media_size?`<button class="button primary" type="button" data-offline-download="${j.id}" data-offline-part="media">${again?'重新下载附件 ZIP':'下载附件 ZIP'}（${(j.media_size/1024/1024).toFixed(1)} MB）</button>`:'',
          downloadable && j.media_size?`<button class="button primary" type="button" data-offline-both="${j.id}">同时下载</button>`:'',
          ['queued','running'].includes(j.status)?`<button class="button secondary" type="button" data-offline-cancel="${j.id}">取消任务</button>`:'',
          // 生成中的任务须先取消；其余状态都可删除记录，服务器端仍有文件的一并删除。
          busyJob?'':`<button class="button danger" type="button" data-offline-delete="${j.id}" data-offline-name="${escape(name)}" data-offline-files="${downloadable?'1':''}">删除记录</button>`,
        ].join('');
        return `<div data-offline-item="${j.id}" style="padding:12px 0;border-top:1px solid var(--line)"><strong>${escape(name)} · ${label}</strong><p class="hint">${escape(at(j.created))}${stages[p.stage]?' · '+stages[p.stage]:''} · 对话 ${p.rounds || 0} 轮 · 未归轮消息 ${p.unrounded_messages || 0} 条 · 消息 ${p.messages || 0} · 已处理 ${p.processed || 0} · 内嵌图片 ${p.attachments || 0} · 内嵌视频/音频 ${p.videos || 0} · 仅链接 ${p.linked_attachments || 0} · 安全拦截 ${p.blocked_attachments || 0} · 失效 ${p.unavailable_attachments || 0}${j.size?' · 文件 '+(j.size/1024/1024).toFixed(1)+' MB'+(j.media_size?'（其中附件 ZIP '+(j.media_size/1024/1024).toFixed(1)+' MB）':''):''}</p>${removal}${usage}${j.error?`<p class="message error">${escape(j.error)}</p>`:''}${actions?`<div class="toolbar-actions" style="margin-top:6px">${actions}</div>`:''}</div>`;
      }).join('') : '<p class="hint">尚无留档任务。</p>';
      // 先换下载票据（短时签名地址），再用 <a download> 让浏览器直接流式下载，不经内存 Blob。
      const ticket=async(id,part)=>{
        const r=await api(`${base}/${id}/ticket${part==='media'?'?part=media':''}`,{method:'POST'});
        const url=new URL(r.url,root.location.origin);
        if(url.origin!==root.location.origin || !url.pathname.startsWith(base+'/')) throw new Error('下载地址无效');
        return url.href;
      };
      const save=href=>{const anchor=document.createElement('a');anchor.href=href;anchor.download='';anchor.rel='noreferrer';document.body.appendChild(anchor);anchor.click();anchor.remove();};
      list.querySelectorAll('[data-offline-download]').forEach(button=>button.onclick=async()=>{
        button.disabled=true;
        try{
          const href=await ticket(button.dataset.offlineDownload,button.dataset.offlinePart);
          if(!active || !getToken()) return;
          save(href);
        }catch(error){toast(error.message,true);}finally{button.disabled=false;}
      });
      // 同时下载：两张票据都拿到后再依次触发，间隔一下，避免浏览器把第二个下载吞掉。
      list.querySelectorAll('[data-offline-both]').forEach(button=>button.onclick=async()=>{
        button.disabled=true;
        try{
          const id=button.dataset.offlineBoth;
          const html=await ticket(id,'html'), media=await ticket(id,'media');
          if(!active || !getToken()) return;
          save(html);
          await new Promise(resolve=>setTimeout(resolve,800));
          if(!active || !getToken()) return;
          save(media);
          toast('已开始下载 HTML 和附件 ZIP；如浏览器询问“下载多个文件”，请选择允许。');
        }catch(error){toast(error.message,true);}finally{button.disabled=false;}
      });
      list.querySelectorAll('[data-offline-cancel]').forEach(button=>button.onclick=async()=>{
        try{await api(`${base}/${button.dataset.offlineCancel}`,{method:'DELETE'});await refresh();}catch(error){toast(error.message,true);}
      });
      list.querySelectorAll('[data-offline-delete]').forEach(button=>button.onclick=async()=>{
        const id=button.dataset.offlineDelete, name=button.dataset.offlineName || '该留档';
        const files=button.dataset.offlineFiles ? '\n\n服务器端的 HTML 和附件 ZIP 会一并删除，之后无法再下载；请先确认已保存好。' : '';
        if(!root.confirm(`确认删除留档记录“${name}”？${files}`))return;
        button.disabled=true;
        try{
          await api(`${base}/${id}/record`,{method:'DELETE'});
          const row=button.closest && button.closest('[data-offline-item]');
          if(row)row.remove();
          toast('留档记录已删除。');
          await refresh();
        }catch(error){toast(error.message,true);button.disabled=false;}
      });
    }
    async function refresh(){
      if(!active || busy || !getToken()) return;
      busy=true; const token=getToken(),generation=epoch;
      try{const r=await api(base);if(active && token===getToken() && generation===epoch){downloaded=r.downloaded_hours || 2;render(r.items);}}
      catch(error){if(active && token===getToken() && generation===epoch) list.textContent=error.message;}
      finally{busy=false;}
    }
    const toggle=$('offlineToggle');
    const show=open=>{
      panel.classList.toggle('hidden',!open);
      toggle.textContent=open?'收起留档':'离线 HTML 留档';
      if(toggle.setAttribute)toggle.setAttribute('aria-expanded',String(open));
      if(open){update();refresh();}
    };
    toggle.onclick=()=>show(panel.classList.contains('hidden'));
    $('offlineClose').onclick=()=>show(false);
    $('offlineMode').onchange=update;
    const note=$('offlineEstimate');
    const setNote=(text,error)=>{if(!note)return;note.textContent=text||'';note.className=text?(error?'message error':'hint'):'hint';};
    const num=v=>Number(v||0).toLocaleString('zh-CN');
    const mb=v=>(Number(v||0)/1024/1024).toFixed(1)+' MB';
    $('offlineCreate').onclick=async()=>{
      const mode=$('offlineMode').value,month=$('offlineMonth').value;
      // 导出命中轮次；客户视图还包含符合页面规则的未归轮消息。
      const body={mode:mode==='current'?'rounds':mode};
      if(mode==='month'){if(!month){toast('请选择留档月份。',true);return;}body.month=month;}
      let selection=null;
      try{selection=mode==='current'?getFilters():null;}catch(error){toast('读取当前筛选失败，请重新查询归档后再试。',true);return;}
      if(selection){body.summary=selection.summary||'';body.view=selection.mode==='customers'?'customers':'rounds';}
      const q=selection?'?'+String(selection.query):'';
      const summary=mode==='month'?`${month} 按结束时间归月的完整轮次＋当月未归轮消息。按轮次结束时间归属月份，跨月轮次导出整轮（含上月消息）；未结束的轮次按导出时最后一条消息归属，建议月份结束后留档。发送时间未知的未归轮消息只在全部历史留档中。`:mode==='all'?'全部历史归档':`当前筛选（${(selection.summary||'全部归档').replace(/\n/g,' · ')}）`;
      const button=$('offlineCreate'),label=button.textContent;
      button.disabled=true;button.textContent='正在预估数量…';setNote('正在统计导出范围内的消息数量，数据多时可能需要几十秒…');
      let estimate;
      try{
        estimate=await api(base+'/estimate'+q,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
      }catch(error){
        // 预估失败时不继续生成：无法判断规模就不冒险。
        setNote(`无法预估导出数量：${error.message}`,true);toast(error.message,true);
        button.disabled=false;button.textContent=label;return;
      }
      button.textContent=label;
      try{
        if(!active || !getToken() || !estimate) return;
        const counts=estimate.messages==null?'':`消息 ${num(estimate.messages)} 条 · 对话 ${num(estimate.rounds)} 轮 · 含未归轮消息 ${num(estimate.unrounded_messages)} 条 · 客户 ${num(estimate.customers)} 位 · 附件 ${num(estimate.attachments)} 个${estimate.attachment_bytes?'（约 '+mb(estimate.attachment_bytes)+'）':''}${estimate.estimated_bytes?' · 预计页面约 '+mb(estimate.estimated_bytes):''}${estimate.estimated_media_bytes?' · 附件 ZIP 约 '+mb(estimate.estimated_media_bytes):''}`;
        if(estimate.level==='empty'){setNote('该范围内没有可导出的消息，请调整范围或筛选条件。',true);toast('该范围内没有可导出的消息。',true);return;}
        if(estimate.level==='block'){
          setNote(`${counts?counts+'。':''}${estimate.reason}`,true);
          toast('导出范围过大，已阻止生成，请缩小范围后分批导出。',true);return;
        }
        const warn=estimate.level==='warn';
        setNote(counts+(warn?'。数据量较大：'+(estimate.warnings||[]).join('；'):''),warn);
        const risk=warn
          ? `\n\n【数据量较大】${(estimate.warnings||[]).join('；')}。\n生成时间会很长，可能因超时或超出资源上限而失败，下载后的页面也可能打开缓慢。建议缩小时间范围或增加筛选条件分批导出。`
          : '';
        const message=`确认生成离线留档？\n\n范围：${summary}\n预估：${counts}${risk}\n\n提醒：聊天记录过多会导致导出失败（超时、超出内存或文件上限）。导出在独立的低优先级进程中运行，失败只会让本任务标记为“失败”并清理临时文件，不影响客服系统；失败后请缩小范围重新导出。\n\n生成两个文件：聊天记录 HTML（文字与目录）＋附件 ZIP（图片、视频和音频原文件，单个或总量超限、磁盘预算不足的保留原始链接）。下载后放进同一个文件夹，把附件 ZIP 解压到当前文件夹即可在 HTML 里查看附件。后台生成可能需要较长时间，期间可离开页面。下载完成后服务器端再保留 ${downloaded} 小时供重新下载，之后删除，请妥善保存。`;
        if(!root.confirm(message))return;
        if(warn && !root.confirm(`再次确认：本次导出约 ${num(estimate.messages)} 条消息、${num(estimate.rounds)} 轮对话，数据量较大，仍要继续生成吗？`))return;
        button.disabled=true;
        await api(base+q,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
        toast('已开始后台留档，可离开页面，稍后回来下载。');await refresh();
      }catch(error){toast(error.message,true);}finally{button.disabled=false;button.textContent=label;}
    };
    update();
    return {
      start(){active=true;epoch++;if(timer===null)timer=setInterval(()=>{if(!panel.classList.contains('hidden'))refresh();},3000);if(!panel.classList.contains('hidden'))refresh();},
      stop(){active=false;epoch++;if(timer!==null)clearInterval(timer);timer=null;list.innerHTML='';},
    };
  }};
})(window);
