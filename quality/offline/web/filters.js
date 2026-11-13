(function(root){
  'use strict';
  const has=v=>v!==null&&v!==undefined&&String(v)!=='';
  let casefoldMap=Object.fromEntries(Array.from({length:26},(_,i)=>[i+65,String.fromCharCode(i+97)]));
  function configureCasefold(map){casefoldMap=map;}
  // 映射来自导出数据库，未变化的标量保持原值；不回退浏览器自己的 Unicode 大小写表。
  const lower=v=>Array.from(String(v??''),c=>casefoldMap[c.codePointAt(0)]??c).join('');
  const includes=(v,q)=>lower(v).includes(lower(q));
  function time(value){
    if(!has(value))return NaN;
    let s=String(value).replace(' ','T');
    if(/^\d{4}-\d\d-\d\d$/.test(s))s+='T00:00:00';
    if(!/([zZ]|[+-]\d\d:?\d\d)$/.test(s))s+='+08:00';
    const milliseconds=Date.parse(s);
    if(!Number.isFinite(milliseconds))return NaN;
    const fraction=s.match(/\.(\d+)(?=[zZ]|[+-]\d\d:?\d\d$)/);
    return BigInt(milliseconds)*1000n+BigInt((fraction?.[1]||'').padEnd(6,'0').slice(3,6)||'0');
  }
  function normalize(input={},context={}){
    if(context.casefold_map)configureCasefold(context.casefold_map);
    const f={...input};
    for(const k of ['customer_query','keyword'])if(has(f[k]))f[k]=String(f[k]).trim();
    const fixed=context.fixed_context||context;
    for(const k of ['source_key','account_id','channel_key','as_of','has_attachment','status'])if(has(fixed[k]))f[k]=fixed[k];
    for(const k of ['has_attachment','assisted','unassigned','lost'])if(has(f[k]))f[k]=f[k]===true||f[k]==='true';
    if(f.lost===false)delete f.lost;
    if((has(f.agent_id)||has(f.inbox_id))&&(!has(f.source_key)||!has(f.account_id)))throw Error('筛选坐席或收件箱需要同时指定来源和账号。');
    for(const key of ['account_id','inbox_id','conversation_id','round_no'])if(has(f[key])){
      if(!/^[1-9][0-9]*$/.test(String(f[key]))||BigInt(f[key])>(key==='round_no'?2147483647n:9223372036854775807n))throw Error('身份和轮次编号必须是范围内的正整数。');
      if(key!=='round_no')f[key]=String(f[key]);
    }
    f.time_field=f.time_field||'started_at';f.message_scope=f.message_scope||'all';f.agent_scope=f.agent_scope||'responsible';f.rating_op=has(f.rating)?f.rating_op||'eq':'eq';
    if(has(f.start)&&typeof time(f.start)!=='bigint'||has(f.end)&&typeof time(f.end)!=='bigint')throw Error('请输入有效日期和时刻。');
    if(has(f.start)&&has(f.end)&&time(f.start)>=time(f.end))throw Error('结束时间必须晚于开始时间。');
    return f;
  }
  const equal=(a,b)=>String(a)===String(b);
  const windowMatch=(v,f)=>(!has(f.start)||time(v)>=time(f.start))&&(!has(f.end)||time(v)<time(f.end));
  function unroundedAllowed(f){
    return !['round_no','reception_type','rating_status','rating','status','ai_review_status','human_review_status','lost','metric','outcome','assisted','unassigned','dialogue'].some(k=>has(f[k]))
      &&!(has(f.agent_id)&&f.agent_scope!=='current')&&!(f.time_field!=='message_at'&&(has(f.start)||has(f.end)));
  }
  function matchesEntry(e,c,f){
    for(const k of ['source_key','account_id'])if(has(f[k])&&!equal(e[k]??c[k],f[k]))return false;
    if(has(f.customer_query)&&!([c.name,c.contact_id,c.h5_user_id,c.business_user_id].some(v=>includes(v,f.customer_query))||equal(c.id,f.customer_query)))return false;
    if(has(f.blacklist)&&!c['blacklist_'+f.blacklist])return false;
    if(has(f.conversation_id)&&!equal(e.conversation_id,f.conversation_id))return false;
    if(!e.round_id)return unroundedAllowed(f);
    for(const [p,k] of [['inbox_id','inbox_id'],['round_no','round_no'],['reception_type','reception_type'],['rating_status','round_rating_status'],['status','round_status']])if(has(f[p])&&!equal(e[k],f[p]))return false;
    if(has(f.channel_key)&&(f.channel_key==='unmapped'?e.channel_key!=null:!equal(e.channel_key,f.channel_key)))return false;
    if(has(f.as_of)&&!(time(e.round_started_at)<time(f.as_of)))return false;
    if(has(f.rating)){
      const score=e.round_rating??e.rating;
      if(score==null)return false;
      if(f.rating_op!=='eq'&&(e.round_rating_status??(e.rated?'rated':null))!=='rated')return false;
      if(!(f.rating_op==='gt'?Number(score)>Number(f.rating):f.rating_op==='lt'?Number(score)<Number(f.rating):Number(score)===Number(f.rating)))return false;
    }
    for(const k of ['ai_review_status','human_review_status'])if(has(f[k])){
      if(k==='human_review_status'&&e.reception_type!=='human')return false;
      if((e[k]??'pending')!==f[k])return false;
    }
    if(f.lost&&!e.is_lost)return false;
    if(has(f.assisted)&&e.assisted!==f.assisted)return false;
    if(has(f.unassigned)&&(e.assigned_agent_id==null)!==f.unassigned)return false;
    if(has(f.dialogue)&&!(e.visitor_messages>(f.dialogue==='high_quality'?5:0)))return false;
    const outcomes={ai_only:'is_ai_only',handoff:'is_handoff',refused:'busy_refused',effective_human:'human_replied',lost:'is_lost'};
    if(has(f.outcome)&&!e[outcomes[f.outcome]])return false;
    if(has(f.agent_id)){
      const values=f.agent_scope==='responsible'?[e.responsible_agent_id]:f.agent_scope==='current'?[e.current_assignee_id]:e[f.agent_scope==='replied'?'replied_agent_ids':'assigned_agent_ids']||[];
      if(!values.some(v=>v!=null&&equal(v,f.agent_id)))return false;
    }
    if(has(f.start)||has(f.end)){
      if(['message_at','reply_at'].includes(f.time_field)){
        if(has(f.end)&&!(time(e.round_started_at)<time(f.end)))return false;
        if(f.time_field==='reply_at'&&has(f.start)&&!(time(e.last_message_at)>=time(f.start)))return false;
      }else{
        const key={started_at:'round_started_at',resolved_at:'resolved_at',rating_at:'rating_at',handoff_at:'filter_handoff_at',assigned_at:'assigned_at'}[f.time_field];
        if(!windowMatch(key==='resolved_at'?(e.resolved_at??e.closed_at):e[key],f))return false;
      }
    }
    return true;
  }
  function messageMatch(m,f,e){
    if(has(f.as_of)&&m.sent_at!=null&&!(time(m.sent_at)<time(f.as_of)))return false;
    if(has(f.keyword)&&!(m.search_contents??[m.content]).some(v=>includes(v,f.keyword)))return false;
    if(f.message_scope==='agent'&&!m.is_human_reply)return false;
    if(f.message_scope==='private'&&!m.is_private)return false;
    if(['contact','bot'].includes(f.message_scope)&&(m.sender_role!==f.message_scope||m.is_private))return false;
    if(f.time_field==='message_at'&&!windowMatch(m.sent_at,f))return false;
    if(!e.round_id){
      for(const k of ['inbox_id','current_assignee_id']){const p=k==='current_assignee_id'?'agent_id':k;if(has(f[p])&&!equal(m[k],f[p]))return false;}
      if(has(f.channel_key)&&(f.channel_key==='unmapped'?m.channel_key!=null:!equal(m.channel_key,f.channel_key)))return false;
    }
    return true;
  }
  function matchingMessages(e,rows,f){
    const state=beginMessages();for(const row of rows)observeMessage(state,e,row,f);
    return finishMessages(state,e,rows,f);
  }
  function beginMessages(){return {reply:false,matched:[]};}
  function observeMessage(state,e,m,f){
    if(m.is_human_reply&&windowMatch(m.sent_at,f)&&(!has(f.as_of)||time(m.sent_at)<time(f.as_of)))state.reply=true;
    if(messageMatch(m,f,e))state.matched.push(m);
  }
  function finishMessages(state,e,rows,f){
    if(f.time_field==='reply_at'&&(has(f.start)||has(f.end))&&!state.reply)return null;
    const matched=state.matched;
    const attachment=m=>m.has_original_attachment??Boolean(m.attachments?.length);
    if(!e.round_id)return matched.filter(m=>!has(f.has_attachment)||attachment(m)===f.has_attachment);
    const requires=has(f.keyword)||f.message_scope!=='all'||(f.time_field==='message_at'&&(has(f.start)||has(f.end)))||(f.has_attachment===false&&has(f.as_of));
    if(f.has_attachment===true)return matched.some(attachment)?rows:null;
    if(f.has_attachment===false&&matched.some(attachment))return null;
    return !requires||matched.length?rows:null;
  }
  function select(entries,customers,getMessages,input={},context={}){
    const f=normalize(input,context),found=[],rounds=new Set(),messages=[],people=new Set();
    for(const entry of entries){
      const customer=customers[entry.customer??0]||{};
      if(!matchesEntry(entry,customer,f))continue;
      const matched=matchingMessages(entry,getMessages(entry),f);
      if(!matched||!entry.round_id&&!matched.length)continue;
      const e=entry.round_id?entry:{...entry,messages:matched.length,matched_message_ids:matched.map(m=>String(m.message_id))};
      found.push(e);people.add(String(customer.id));
      if(e.round_id)rounds.add(String(e.round_id));else for(const m of matched)messages.push([String(entry.source_key??customer.source_key),String(entry.account_id??customer.account_id),String(m.message_id)]);
    }
    return {entries:found,round_ids:[...rounds],message_ids:messages,customer_ids:[...people]};
  }
  const api={normalize,time,lower,includes,configureCasefold,matchesEntry,messageMatch,matchingMessages,select,beginMessages,observeMessage,finishMessages};
  if(typeof module!=='undefined'&&module.exports)module.exports=api;else root.ICSFilters=api;
})(typeof window==='undefined'?globalThis:window);
