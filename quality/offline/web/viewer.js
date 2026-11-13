(function (root) {
  'use strict';
  const lower = v => String(v ?? '').toLocaleLowerCase();
  // 兼容 ISO 8601、旧版留档的“日期 空格 时间”格式及 6 位微秒；无时区时按北京时间理解。
  function time(v) {
    if (!v) return NaN;
    let text = String(v).trim().replace(' ', 'T').replace(/(\.\d{3})\d+/, '$1');
    if (/^\d{4}-\d\d-\d\d$/.test(text)) text += 'T00:00:00';
    if (!/([zZ]|[+-]\d\d:?\d\d)$/.test(text)) text += '+08:00';
    return Date.parse(text);
  }
  const has = v => String(v ?? '').trim() !== '';
  const includes = (values, query) => values.some(v => lower(v).includes(lower(String(query).trim())));
  // 目录级筛选：只用目录中的轮次信息，不读取聊天正文。
  function matches(e, c, f) {
    c = c || {};
    if (has(f.customer) && !includes([c.name, c.id, c.contact_id, c.h5_user_id], f.customer)) return false;
    if (has(f.conversation) && String(e.conversation_id) !== String(f.conversation).trim()) return false;
    const started = e.round_id ? e.round_started_at : e.first_at;
    if (has(f.start) && !(time(started) >= time(f.start))) return false;
    if (has(f.end) && !(time(started) < time(f.end))) return false;
    if (has(f.reception)) {
      const type = e.reception_type === 'human_involved' ? 'human' : e.reception_type;
      if (type !== f.reception) return false;
    }
    if (has(f.ai) && (e.ai_review_status || (e.round_id ? 'pending' : '')) !== f.ai) return false;
    if (has(f.human) && (!['human', 'human_involved'].includes(e.reception_type) || (e.human_review_status || 'pending') !== f.human)) return false;
    if (f.rating === 'rated' && !e.rated) return false;
    if (f.rating === 'low' && !(e.rating != null && e.rating < 4)) return false;
    if (has(f.assisted) && (!e.round_id || e.assisted !== (f.assisted === 'true'))) return false;
    if (has(f.lost) && (!e.round_id || e.is_lost !== (f.lost === 'true'))) return false;
    return true;
  }
  // 正文级筛选：关键词匹配正文或附件文件名，发送人匹配名称或 ID。
  function messageMatches(r, f) {
    if (has(f.keyword) && !includes([r.content, ...(r.attachments || []).map(a => a.filename)], f.keyword)) return false;
    if (has(f.sender) && !includes([r.sender_name, r.sender_id], f.sender)) return false;
    return true;
  }
  // 关键词和发送人组成同一条消息条件；“无附件”还要排除满足该条件的任何带附件消息。
  function roundMatches(rows, f) {
    let hasP = false, hasPWithAttachment = false, hasKind = false;
    for (const r of rows) {
      if (!messageMatches(r, f)) continue;
      hasP = true;
      const attachments = r.attachments || [];
      if (attachments.length) hasPWithAttachment = true;
      if (attachments.some(a => f.media === 'image' ? a.kind === 'image'
        : f.media === 'video' ? ['video', 'audio'].includes(a.kind)
        : f.media === 'unavailable' && a.status === 'unavailable')) hasKind = true;
    }
    const hasCondition = has(f.keyword) || has(f.sender);
    if (f.media === 'none') return (!hasCondition || hasP) && !hasPWithAttachment;
    if (f.media === 'any') return hasPWithAttachment;
    if (has(f.media)) return hasKind;
    return !hasCondition || hasP;
  }
  const needsMessages = f => has(f.keyword) || has(f.sender) || has(f.media);
  const labels = {contact:'客户',agent:'客服',bot:'机器人',system:'系统',unknown:'未知',passed:'合格',failed:'不合格',pending:'待定'};
  const receptions = {ai_only:'纯 AI 接待',human:'人工介入',human_involved:'人工介入'};
  const statuses = {active:'未结束',resolved:'已解决',idle_boundary:'超时分轮',unknown:'待确认'};
  // 轮次编号只在单个会话内递增；用户第 N 次咨询按该用户全部轮次的开始时间计算。
  function roundTitle(r) {
    if (!r.round_id) return `会话 ${r.conversation_id ?? '未知'} · 未归轮消息`;
    return [r.customer_round_seq ? `用户第 ${r.customer_round_seq} 次咨询` : '', `会话 ${r.conversation_id ?? '未知'}`,
      `会话内第 ${r.round_no ?? '未知'} 轮`].filter(Boolean).join(' · ');
  }
  function ratingReviewText(review) {
    return ({valid:'已复核标记有效',invalid:'已复核标记无效'})[review?.status] || '未复核';
  }
  // 消息属于目录中的同一轮；仅匹配选中邀请，保留其他历史评价卡的原始内容。
  function ratingMessageReviewText(message, entry) {
    const review = entry?.rating_review;
    const invitation = review?.selected_rating?.invitation_message_id;
    if (!message.visitor_rating?.rating || invitation == null || !entry.round_id || String(review.round_id) !== String(entry.round_id) || String(message.message_id) !== String(invitation)) return '';
    return ratingReviewText(review);
  }
  function ratingText(rating) {
    if (!rating) return '';
    if (!rating.rating) return '访客评价：待评价';
    const score = Math.max(0, Math.min(5, Number(rating.rating) || 0));
    return `访客评价：${'★'.repeat(score)}${'☆'.repeat(5 - score)} ${score} 分${rating.feedback ? ' · ' + rating.feedback : ''}`;
  }
  // 指标文字、时长格式和说明与归档页面卡片保持一致。
  function qualityDuration(value) {
    if (value === null || value === undefined || value === '' || !Number.isFinite(Number(value)) || Number(value) < 0) return '—';
    if (Number(value) > 0 && Number(value) < 1) return '不足1秒';
    let seconds = Math.round(Number(value));
    const parts = [];
    for (const [unit, label] of [[86400, '天'], [3600, '小时'], [60, '分'], [1, '秒']]) {
      const count = Math.floor(seconds / unit);
      if (count) parts.push(`${count}${label}`);
      seconds %= unit;
    }
    return parts.join('') || '0秒';
  }
  const qualityRoleLabel = role => ({contact:'客户', customer:'客户', agent:'真人客服', bot:'机器人', system:'系统', unknown:'身份待确认'}[role] || '身份待确认');
  function serviceCells(round) {
    if (!round.round_id) return [];
    const metrics = round.service_metrics || {};
    const firstLabels = {not_handed_off:'未转人工', missing_handoff_time:'时间待确认', unknown_time:'时间待确认', awaiting_agent:'待人工回复', over_limit:'超时不计', unassigned:'无ICS分配，不计', assisted:'协助接待，不计', not_counted:'无有效首响'};
    const first = metrics.first_response_status === 'recorded' ? qualityDuration(metrics.first_response_seconds) : firstLabels[metrics.first_response_status] || '待确认';
    const last = metrics.last_message_by_agent === true ? '是' : metrics.last_message_by_agent === false ? `否 · ${qualityRoleLabel(metrics.last_message_sender_role)}` : '待确认';
    const rating = round.rated && round.rating != null ? `${round.rating} 分` : '未评价';
    const feedback = round.rated ? String(round.rating_feedback || '').trim() : '';
    const counts = [metrics.human_phase_message_count, metrics.human_phase_customer_count, metrics.human_phase_agent_count];
    const messages = counts.every(value => value !== null && value !== undefined) ? `${counts[0]}（${counts[1]}：${counts[2]}）` : '待确认';
    const multiSeat = metrics.seat_timing_excluded === true;
    const multiSeatHint = multiSeat ? `；本轮有 ICS 分配坐席以外的坐席公开回复（如交班时同事协助，共 ${metrics.responding_seat_count} 位坐席回复），本轮整轮不计入任何坐席的统计考核（询盘、回复、对话量、评分、质检、响应时长），仅保留发配量（咨询状态总时长也不计）；下一轮照常统计` : '';
    const agentLabel = (id, name) => name || (has(id) ? `Chatwoot 坐席 ID ${id} · 名称未同步` : 'Chatwoot 坐席名称未同步');
    const who = round.assisted ? `${agentLabel(round.assigned_agent_id, round.assigned_agent_name)} · 协助接待，不计考核`
      : round.responsibility_basis === 'assignment_unobserved' ? '未记录 ICS 分配'
      : round.responsibility_basis !== 'ics_assignment' || !has(round.responsible_agent_id) ? '待确认'
      : agentLabel(round.responsible_agent_id, round.responsible_agent_name);
    return [
      ['考核坐席', who, '考核坐席（ICS 分配）'],
      ['转人工首响', first, `与统计报表同一算法：转人工成功后客户须先发言，从客户这段连续发言的最后一条有效消息（分段发送以最后一段为准）到坐席随后首次有效公开回复；转人工指令不作起点，客户发言前坐席主动发的消息不算首响；只计 ICS 分配坐席单独接待的轮次；首响超过600秒不计入统计，600秒保留，后续回复不替代首响${multiSeatHint}。`],
      ['人工平均响应', multiSeat ? '协助接待，不计' : qualityDuration(metrics.average_response_seconds), `与统计报表同一算法，基于 ${metrics.response_sample_count ?? 0} 次有效已完成响应：首段为首响，之后每段从客户连续发言的最后一条有效消息到下一条有效人工回复；统计报表按响应次数加权汇总；单次响应超过600秒不计入时长及样本数，600秒保留${multiSeatHint}。`],
      ['最后一句为人工', last, '按本轮最后一条公开聊天消息判断，忽略内部备注、系统活动和评价卡'],
      ['本轮总时长', qualityDuration(metrics.duration_seconds), '从本轮开始至解决时间；未解决或超时分轮按最后一条聊天消息计时'],
      ['访客评价', feedback ? `${rating} · ${feedback}` : rating, feedback ? `${rating} · ${feedback}` : rating],
      ['人工对话数', messages, '总消息数（客户消息数：人工客服消息数），仅计人工接待阶段的公开消息' + (metrics.human_phase_basis === 'first_agent_reply' ? '；未记录转人工时间，从首条人工回复起统计' : '')],
    ];
  }
  function countEntries(items) {
    const rounds = new Set();
    let unrounded = 0;
    for (const e of items) {
      if (e.round_id) rounds.add(e.round_id);
      else unrounded += e.messages || 0;
    }
    return `${rounds.size.toLocaleString()} 轮对话 · 未归轮消息 ${unrounded.toLocaleString()} 条`;
  }
  const api = {qualityDuration, serviceCells, countEntries, matches, messageMatches, roundMatches, needsMessages, time, roundTitle, ratingText, ratingReviewText, ratingMessageReviewText};
  if (typeof module !== 'undefined' && module.exports) {module.exports = api; return;}

  const archive = root.ICSArchive = {...api};
  const filterEngine = root.ICSFilters;
  const $ = id => document.getElementById(id);
  const escape = v => String(v ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const zone = {timeZone: 'Asia/Shanghai', hour12: false};
  const formatTime = v => { const at = time(v); return Number.isNaN(at) ? '时间未知' : new Date(at).toLocaleString('zh-CN', zone); };
  const formatClock = v => { const at = time(v); return Number.isNaN(at) ? '' : new Date(at).toLocaleTimeString('zh-CN', zone); };
  const formatDay = v => { const at = time(v); return Number.isNaN(at) ? '时间未知' : new Date(at).toLocaleDateString('zh-CN', {...zone, year:'numeric', month:'long', day:'numeric', weekday:'short'}); };
  const formatBytes = n => n >= 1048576 ? (n / 1048576).toFixed(1) + ' MB' : n >= 1024 ? Math.round(n / 1024) + ' KB' : n + ' B';
  const kinds = {image:'图片', video:'视频', audio:'音频', pdf:'PDF 文件', file:'文件', unknown:'附件'};
  const IMAGE_TYPES = ['image/png','image/jpeg','image/gif','image/webp','image/avif'];
  const PAGE = 50, GROUP_CACHE = 40;
  let entries = [], customers = [], view = [], filters = {}, page = 1, current = -1, version = 0;
  const groups = new Map();
  let historyHits = new Set();
  const messageKey = (e, m) => JSON.stringify([String(e.source_key ?? customers[e.customer]?.source_key), String(e.account_id ?? customers[e.customer]?.account_id), String(m.message_id)]);

  // 每轮对话的消息存放在不执行的 JSON 数据块中，点击时才解析，并只缓存最近查看的若干轮。
  function messages(entry) {
    if (groups.has(entry.index)) {const rows = groups.get(entry.index); groups.delete(entry.index); groups.set(entry.index, rows); return searchRows(entry, rows);}
    let rows = [];
    for (let part = 0; part < (entry.parts || 1); part++) {
      const node = document.getElementById(`g${entry.index}.${part}`);
      if (!node) throw new Error('留档数据缺失，文件可能未完整下载。');
      rows = rows.concat(JSON.parse(node.textContent));
    }
    groups.set(entry.index, rows);
    if (groups.size > GROUP_CACHE) groups.delete(groups.keys().next().value);
    return searchRows(entry, rows);
  }
  function searchRows(entry, rows) {
    if (archive.catalog?.meta?.version >= 3) for (const row of rows) row.search_contents = historyHits.has(messageKey(entry,row)) ? [filters.keyword] : [];
    return rows;
  }
  const customerName = c => c.name || c.id || '未命名用户';
  // 视频、音频点击时才从内嵌 base64 分块解码成本地 Blob；切换轮次时释放，避免内存累积。
  const blobs = new Map();
  function mediaUrl(id) {
    if (blobs.has(id)) return blobs.get(id);
    const node = document.getElementById('m' + id);
    if (!node) throw new Error('附件数据缺失，文件可能未完整下载。');
    let type = node.dataset.type || 'application/octet-stream';
    if (type === 'video/quicktime') type = 'video/mp4';  // 手机 .mov 多为 H.264，按 MP4 交给浏览器播放
    const text = node.textContent.trim(), parts = [], STEP = 4 * 1024 * 1024;  // 4 的整数倍
    for (let at = 0; at < text.length; at += STEP) {
      const binary = atob(text.slice(at, at + STEP)), bytes = new Uint8Array(binary.length);
      for (let i = 0; i < binary.length; i++) bytes[i] = binary.charCodeAt(i);
      parts.push(bytes);
    }
    const url = URL.createObjectURL(new Blob(parts, {type}));
    blobs.set(id, url);
    return url;
  }
  function releaseMedia() {
    for (const url of blobs.values()) URL.revokeObjectURL(url);
    blobs.clear();
  }
  function warn(box, text) {
    if (box.querySelector('.warn')) return;
    box.appendChild(Object.assign(document.createElement('small'), {className: 'warn', textContent: text}));
  }
  function play(button) {
    const box = button.closest('[data-player]');
    let url;
    try {url = mediaUrl(button.dataset.play);} catch (error) {warn(box, error.message); return;}
    const player = document.createElement(box.classList.contains('video') ? 'video' : 'audio');
    player.controls = true;
    player.preload = 'metadata';
    player.addEventListener('error', () => warn(box, '浏览器不支持该编码，可点“保存原文件”后用本地播放器打开。'));
    player.src = url;
    button.replaceWith(player);
    const started = player.play && player.play();
    if (started && started.catch) started.catch(() => {});
  }
  function save(button) {
    const box = button.closest('[data-player]');
    let url;
    try {url = mediaUrl(button.dataset.save);} catch (error) {warn(box, error.message); return;}
    const link = Object.assign(document.createElement('a'), {href: url, download: box.dataset.name || '附件'});
    document.body.appendChild(link);
    link.click();
    link.remove();
  }
  const customerIds = c => [c.contact_id ? `联系人 ${c.contact_id}` : '', c.h5_user_id ? `H5 ID ${c.h5_user_id}` : ''].filter(Boolean).join(' · ');
  function shortTitle(e) {
    if (!e.round_id) return `会话 ${e.conversation_id ?? '未知'} · 未归轮消息`;
    return [e.customer_round_seq ? `第 ${e.customer_round_seq} 次咨询` : '', `会话 ${e.conversation_id ?? '未知'} 第 ${e.round_no ?? '?'} 轮`].filter(Boolean).join(' · ');
  }
  function tags(e) {
    const out = [`<span class="tag">${e.messages} 条</span>`];
    if (e.reception_type) out.push(`<span class="tag ${e.reception_type === 'ai_only' ? 'ai' : 'human'}">${escape(receptions[e.reception_type] || '待确认')}</span>`);
    if (e.round_id) {
      for (const [kind, status] of [['AI', e.ai_review_status], ['人工', e.human_review_status]]) {
        if (kind === '人工' && !['human', 'human_involved'].includes(e.reception_type)) continue;
        if (['passed', 'failed'].includes(status)) out.push(`<span class="tag ${status}" title="${kind}质检：${labels[status]}">${kind} ${labels[status]}</span>`);
      }
      if (e.assisted) out.push('<span class="tag human">协助接待</span>');
      if (e.is_lost) out.push('<span class="tag failed">流单</span>');
    }
    if (e.rating_review) out.push(`<span class="tag rating-review ${e.rating_review.status === 'invalid' ? 'failed' : ''}">${escape(ratingReviewText(e.rating_review))}</span>`);
    if (e.rating != null) out.push(`<span class="tag ${e.rating < 4 ? 'failed' : 'passed'}">★ ${escape(e.rating)}</span>`);
    if (e.images) out.push(`<span class="tag">图片 ${e.images}</span>`);
    if (e.videos) out.push(`<span class="tag">视频/音频 ${e.videos}</span>`);
    if (e.unavailable) out.push(`<span class="tag failed">失效附件 ${e.unavailable}</span>`);
    return out.join('');
  }
  function renderToc() {
    const start = (page - 1) * PAGE, items = view.slice(start, start + PAGE);
    const grouped = (filters.sort || 'customer') === 'customer';
    let last = null, html = '';
    items.forEach((e, k) => {
      const c = customers[e.customer] || {};
      if (grouped && e.customer !== last) {
        const ids = customerIds(c);
        html += `<li class="who"><strong>${escape(customerName(c))}</strong>${ids ? `<small>${escape(ids)}</small>` : ''}</li>`;
        last = e.customer;
      }
      const label = grouped ? shortTitle(e) : `${customerName(c)} · ${shortTitle(e)}`;
      html += `<li><button type="button" class="entry${start + k === current ? ' active' : ''}" data-at="${start + k}">`
        + `<span class="line"><strong>${escape(label)}</strong><time>${escape(formatTime(e.round_started_at || e.first_at))}</time></span>`
        + `<span class="preview">${escape(e.preview || '（无客户文字消息）')}</span><span class="tags">${tags(e)}</span></button></li>`;
    });
    $('toc').innerHTML = html || '<li class="empty">没有符合条件的轮次</li>';
    const pages = Math.max(1, Math.ceil(view.length / PAGE));
    $('pageinfo').textContent = `第 ${page} / ${pages} 页`;
    $('prev').disabled = page <= 1;
    $('next').disabled = page >= pages;
  }
  function highlight(text, keyword) {
    text = String(text ?? '');
    keyword = String(keyword ?? '').trim();
    if (!keyword) return escape(text);
    const fold=filterEngine?filterEngine.lower:lower;
    const haystack = fold(text), needle = fold(keyword);
    let at = 0, out = '', found;
    while ((found = haystack.indexOf(needle, at)) !== -1) {
      out += escape(text.slice(at, found)) + '<mark>' + escape(text.slice(found, found + keyword.length)) + '</mark>';
      at = found + keyword.length;
    }
    return out + escape(text.slice(at));
  }
  function attachment(a) {
    const kind = kinds[a.kind] || '附件', name = escape(a.filename || kind);
    const size = a.byte_count ? ' · ' + formatBytes(a.byte_count) : '';
    if (a.status === 'embedded' && Number.isInteger(a.media)) {
      if (a.kind === 'video' || a.kind === 'audio') {
        // 视频不在打开本轮时解码，点击播放才转成本地 Blob，避免一轮多个视频时占满内存。
        return `<div class="player ${a.kind}" data-player="${a.media}" data-name="${name}">`
          + `<button type="button" class="play" data-play="${a.media}">▶ 播放${kind}</button>`
          + `<div class="info"><strong>${name}</strong><small>${escape(kind + size)} · 已内嵌</small>`
          + `<button type="button" class="save" data-save="${a.media}">保存原文件</button></div></div>`;
      }
      return `<figure class="image"><img class="thumb" data-media="${a.media}" alt="${name}"><figcaption>${name}</figcaption></figure>`;
    }
    if (a.status === 'blocked') {
      // 非图片/音视频：生成时已去掉地址，这里也不渲染任何链接。
      return `<div class="file bad"><span class="icon">拦截</span><div><strong>${name}</strong><small>${escape(a.reason || '非图片/视频文件，为防范病毒已禁止打开')}</small></div></div>`;
    }
    const link = a.source_url && /^https?:\/\//i.test(a.source_url)
      ? `<a href="${escape(a.source_url)}" target="_blank" rel="noopener noreferrer">打开原始链接</a>` : '';
    if (a.status === 'linked') {
      return `<div class="file"><span class="icon">${escape(kind.slice(0, 2))}</span><div><strong>${name}</strong><small>${escape(kind + size)} · ${escape(a.reason || '未内嵌')}；源端到期后链接失效</small>${link}</div></div>`;
    }
    return `<div class="file bad"><span class="icon">失效</span><div><strong>${name}</strong><small>附件已失效${a.error ? '（' + escape(a.error) + '）' : ''}</small>${link}</div></div>`;
  }
  function bubble(r, keyword, entry) {
    const role = r.is_private ? 'private' : r.sender_role === 'contact' ? 'contact' : ['agent', 'bot'].includes(r.sender_role) ? r.sender_role : 'system';
    const attachments = (r.attachments || []).map(attachment).join('');
    const rating = r.visitor_rating ? `<div class="rating${r.visitor_rating.rating && r.visitor_rating.rating < 4 ? ' low' : ''}">${escape(ratingText(r.visitor_rating))}${ratingMessageReviewText(r, entry) ? `<span class="rating-review"> · ${escape(ratingMessageReviewText(r, entry))}</span>` : ""}</div>` : '';
    if (role === 'system' && !attachments && !rating) {
      return `<div class="sys"><span>${highlight(r.content, keyword)}</span><time>${escape(formatClock(r.sent_at))}</time></div>`;
    }
    const name = r.sender_name || labels[r.sender_role] || '未知';
    const flags = [r.is_private ? '内部备注' : '', r.is_deleted ? '已删除或撤回' : ''].filter(Boolean)
      .map(t => `<span class="flag">${t}</span>`).join('');
    const text = has(r.content) ? `<div class="text">${highlight(r.content, keyword)}</div>` : '';
    return `<div class="msg ${role === 'contact' ? 'left' : role === 'system' ? 'center' : 'right'} ${role}${r.is_deleted ? ' deleted' : ''}">`
      + `<div class="avatar" aria-hidden="true">${escape(Array.from(String(name).trim())[0] || '?')}</div>`
      + `<div class="body"><div class="meta"><strong>${escape(name)}</strong><span>${escape(labels[r.sender_role] || '')}</span><time>${escape(formatClock(r.sent_at))}</time>${flags}</div>`
      + `<div class="bubble">${text}${attachments}${rating}</div></div></div>`;
  }
  function renderChat(e) {
    const chat = $('chat'), c = customers[e.customer] || {};
    releaseMedia();
    let rows;
    try {rows = messages(e); if(e.matched_message_ids) rows=rows.filter(row=>e.matched_message_ids.includes(String(row.message_id)));} catch (error) {chat.innerHTML = `<div class="placeholder">${escape(error.message)}</div>`; return;}
    const meta = [];
    if (e.round_status) meta.push(statuses[e.round_status] || '待确认');
    meta.push(`${formatTime(e.first_at || e.round_started_at)} 至 ${formatTime(e.last_at)}`);
    const ids = customerIds(c);
    let day = null, thread = '';
    for (const r of rows) {
      const d = formatDay(r.sent_at);
      if (d !== day) {thread += `<div class="day"><span>${escape(d)}</span></div>`; day = d;}
      const historical = filters.keyword && historyHits.has(messageKey(e,r)) && !filterEngine.includes(r.content,filters.keyword);
      thread += (historical ? '<p class="history-hit">关键词命中历史版本</p>' : '') + bubble(r, historical ? '' : filters.keyword, e);
    }
    chat.innerHTML = `<div class="chat-head"><button type="button" class="back" id="back">← 目录</button>`
      + `<div class="title"><h2>${escape(customerName(c))}${ids ? `<small>${escape(ids)}</small>` : ''}</h2>`
      + `<p>${escape(roundTitle(e))} · ${escape(meta.join(' · '))}</p><div class="tags">${tags(e)}</div>`
      + `<div class="service-metrics">${serviceCells(e).map(([label, value, hint]) => `<div class="service-metric" title="${escape(hint)}"><span>${escape(label)}</span><strong>${escape(value)}</strong></div>`).join('')}</div>`
      + (e.rating_review ? `<p class="rating-review">快照复核：${escape(ratingReviewText(e.rating_review))}${e.rating_review.reviewed_at ? ' · '+escape(e.rating_review.reviewer || '管理员')+' · '+escape(formatTime(e.rating_review.reviewed_at)) : ''}</p>` : '')
      + (e.review_remark ? `<p class="remark">质检备注：${escape(e.review_remark)}</p>` : '')
      + `</div><div class="steps"><button type="button" id="prevRound"${current <= 0 ? ' disabled' : ''}>上一轮</button>`
      + `<button type="button" id="nextRound"${current >= view.length - 1 ? ' disabled' : ''}>下一轮</button></div></div>`
      + `<div class="thread" id="thread"><div class="thread-content">${thread || '<div class="placeholder">本轮没有消息</div>'}</div></div>`;
    chat.querySelectorAll('img[data-media]').forEach(img => {
      const node = document.getElementById('m' + img.dataset.media);
      if (node && IMAGE_TYPES.includes(node.dataset.type)) img.src = `data:${node.dataset.type};base64,${node.textContent.trim()}`;
      else img.replaceWith(Object.assign(document.createElement('span'), {className: 'missing', textContent: '图片数据缺失'}));
    });
    $('back').addEventListener('click', () => document.body.classList.remove('reading'));
    $('prevRound').addEventListener('click', () => openAt(current - 1));
    $('nextRound').addEventListener('click', () => openAt(current + 1));
    const hit = chat.querySelector('mark');
    if (hit && hit.scrollIntoView) hit.scrollIntoView({block: 'center'});
    else $('thread').scrollTop = 0;
  }
  function openAt(at) {
    if (at < 0 || at >= view.length) return;
    current = at;
    const target = Math.floor(at / PAGE) + 1;
    if (target !== page) page = target;
    renderToc();
    document.body.classList.add('reading');
    renderChat(view[at]);
    const active = $('toc').querySelector('.entry.active');
    if (active && active.scrollIntoView) active.scrollIntoView({block: 'nearest'});
  }
  const pause = () => new Promise(resolve => setTimeout(resolve, 0));
  async function apply() {
    const mine = ++version, f = filters;
    if(filterEngine){
      try {
        const normalized=filterEngine.normalize(f,archive.catalog?.meta?.filter_contract||{});
        const hits=new Set(),meta=archive.catalog?.meta||{};
        if(normalized.keyword){
          let previous='',tail='';
          const query=filterEngine.lower(normalized.keyword);
          for(let i=0;i<(meta.search_version_parts||0);i++){
            if(mine!==version)return;
            const node=$('v'+i); if(!node)throw Error('历史正文数据缺失，请重新下载完整文件。');
            const chunk=JSON.parse(node.textContent),key=JSON.stringify([String(chunk.source_key),String(chunk.account_id),String(chunk.message_id)]),versionKey=key+':'+chunk.version;
            if(previous!==versionKey)tail='';
            const text=tail+filterEngine.lower(chunk.content);
            if(text.includes(query))hits.add(key);
            tail=query.length>1?text.slice(-(query.length-1)):'';previous=versionKey;
            if(i%32===31){$('status').textContent=`正在搜索历史正文 ${i+1} / ${meta.search_version_parts}…`;await pause();}
          }
        }
        if(mine!==version)return;
        historyHits=hits;
        const selected=[];
        const localConditions=Object.entries(f).some(([key,value])=>has(value)&&!['sort','agent_scope','time_field','rating_op'].includes(key)&&!(key==='message_scope'&&value==='all'));
        for(let i=0;i<entries.length;i++){
          if(mine!==version)return;
          const entry=entries[i],customer=customers[entry.customer]||{};
          if(!localConditions)selected.push(entry);
          else if(filterEngine.matchesEntry(entry,customer,normalized)){
            const rows=messages(entry),state=filterEngine.beginMessages();
            for(let n=0;n<rows.length;n++){
              filterEngine.observeMessage(state,entry,rows[n],normalized);
              if(n%256===255){await pause();if(mine!==version)return;}
            }
            const matched=filterEngine.finishMessages(state,entry,rows,normalized);
            if(matched&&(entry.round_id||matched.length))selected.push(entry.round_id?entry:{...entry,messages:matched.length,matched_message_ids:matched.map(m=>String(m.message_id))});
          }
          if(i%32===31){$('status').textContent=`正在筛选 ${i+1} / ${entries.length}…`;await pause();}
        }
        if(mine!==version)return;
        const at=e=>time(e.round_started_at||e.first_at)||0;
        if(f.sort==='newest')selected.sort((a,b)=>at(b)-at(a));
        if(f.sort==='oldest')selected.sort((a,b)=>at(a)-at(b));
        view=selected;page=1;current=-1;
        $('status').textContent=`共 ${countEntries(view)} · ${new Set(view.map(e=>e.customer)).size} 位用户`;
        renderToc();
        if(view.length&&(!root.matchMedia||root.matchMedia('(min-width: 901px)').matches))openAt(0);
        else if(!view.length){releaseMedia();$('chat').innerHTML='<div class="placeholder">没有符合条件的对话</div>';}
      }catch(error){if(mine===version)$('status').textContent=error.message;}
      return;
    }
    let list = entries.filter(e => matches(e, customers[e.customer], f));
    if (needsMessages(f)) {
      const found = [];
      for (let i = 0; i < list.length; i++) {
        if (mine !== version) return;
        try {if (roundMatches(messages(list[i]), f)) found.push(list[i]);}
        catch (error) {$('status').textContent = error.message; return;}
        if (i % 200 === 199) {$('status').textContent = `正在搜索聊天内容 ${i + 1} / ${list.length} 轮…`; await pause();}
      }
      if (mine !== version) return;
      list = found;
    }
    const at = e => time(e.round_started_at || e.first_at) || 0;
    if (f.sort === 'newest') list.sort((a, b) => at(b) - at(a));
    if (f.sort === 'oldest') list.sort((a, b) => at(a) - at(b));
    view = list; page = 1; current = -1;
    const filtered = Object.entries(f).some(([k, v]) => k !== 'sort' && has(v));
    $('status').textContent = `共 ${countEntries(view)}` + (filtered ? `（全部 ${countEntries(entries)}）` : '');
    renderToc();
    const wide = !root.matchMedia || root.matchMedia('(min-width: 901px)').matches;
    if (view.length && wide) openAt(0);
    else if (!view.length) $('chat').innerHTML = '<div class="placeholder">没有符合条件的对话</div>';
  }
  const exportLockTime = m => m.as_of || m.scope?.as_of || m.scope?.filters?.as_of;
  function exportSummary(m) {
    const scope = m.scope || {}, snapshot = formatTime(m.snapshot_at || m.exported_at);
    if (['rounds', 'customers'].includes(scope.mode)) {
      const lockedAt = exportLockTime(m);
      const messageTime = has(lockedAt) ? `消息时间截至 ${formatTime(lockedAt)}（锁定）` : '未锁定消息时间';
      return `导出范围：当前筛选（${m.summary || scope.summary || '未提供筛选摘要'}）· ${messageTime} · 内容、评分与质检取自生成时刻 ${snapshot} 的记录`;
    }
    if (scope.mode === 'month') return `导出范围：${scope.month || ''} · 按结束时间归月的完整轮次＋当月未归轮消息 · 生成时刻 ${snapshot}。跨月轮次导出整轮（含上月消息）；未结束的轮次按导出时最后一条消息归属；发送时间未知的未归轮消息只在全部历史留档中。`;
    return `导出范围：全部历史 · 生成时刻 ${snapshot}`;
  }
  function prepareFilters(form,meta){
    const labels={ai_only:'纯 AI 接待',human:'人工介入',passed:'合格',failed:'不合格',pending:'待定',ever:'曾被拉黑的用户',active:'当前拉黑中的用户',responsible:'考核坐席（与统计报表一致）',replied:'本轮全部回复坐席',assigned:'历史分配（非责任）',current:'会话当前分配（导出快照）',started_at:'轮次开始时间',message_at:'消息时间',resolved_at:'解决时间',rating_at:'评价时间',handoff_at:'转人工时间',reply_at:'人工回复时间',assigned_at:'ICS 成功分配时间',rated:'已评价',eq:'等于',gt:'大于',lt:'小于',handoff:'转人工',refused:'转人工拒接',effective_human:'有效人工',lost:'流单',dialogue:'有效对话',high_quality:'高质量对话',all:'全部消息',contact:'客户消息',agent:'真人客服公开回复',bot:'机器人消息',private:'私密消息 / 内部备注',true:'仅流单（转人工后未回复）'};
    const contract=meta.filter_contract||{},parameters=contract.parameters||{};
    for(const select of form.querySelectorAll('[data-param]')){
      const field=parameters[select.dataset.param]||{};
      select.innerHTML='<option value="">不限</option>'+(field.options||[]).map(value=>`<option value="${escape(value)}"${value===field.default?' selected':''}>${escape(labels[value]||value)}</option>`).join('');
    }
    for(const select of form.querySelectorAll('[name$="_hour"], [name$="_minute"]')){
      const max=select.name.endsWith('_hour')?24:60;
      select.innerHTML=Array.from({length:max},(_,i)=>`<option value="${String(i).padStart(2,'0')}">${String(i).padStart(2,'0')} ${max===24?'时':'分'}</option>`).join('');
    }
    let options={};try{options=JSON.parse($(meta.options_block||'filter-options')?.textContent||'{}');}catch(error){$('status').textContent='选项字典缺失。';}
    for(const [name,key,id] of [['agent_selection','agents','agent_id'],['inbox_selection','inboxes','inbox_id']]){
      const select=form.elements.namedItem(name);if(!select)continue;
      const fixed=contract.fixed_context||{};
      select.innerHTML='<option value="">全部</option>'+(options[key]||[]).filter(o=>(!has(fixed.source_key)||String(o.source_key)===String(fixed.source_key))&&(!has(fixed.account_id)||String(o.account_id)===String(fixed.account_id))).map(o=>`<option value="${escape(JSON.stringify(o))}">${escape(o[id])} · ${escape(o.source_key)} / 账号 ${escape(o.account_id)}</option>`).join('');
    }
    const fixed=contract.fixed_context||{};
    const text=Object.entries(fixed).filter(([,v])=>v!=null).map(([k,v])=>`${({source_key:'来源',account_id:'账号',channel_key:'业务渠道',metric:'统计指标',metric_seat:'记入坐席',as_of:'消息截止',has_attachment:'固定附件条件',status:'固定轮次状态',start:'统计开始',end:'统计结束'})[k]||k}：${v}`).join('；');
    if(text)$('exportScope').textContent+=' · 固定上下文：'+text+'（以导出快照为准）';
    for(const button of form.querySelectorAll('[data-days]'))button.addEventListener('click',()=>{
      const day=new Date(Date.now()+8*3600000).toISOString().slice(0,10),days=Number(button.dataset.days),end=new Date(day+'T00:00:00Z');
      if(!days)end.setUTCDate(end.getUTCDate()-1);
      const start=new Date(end);start.setUTCDate(start.getUTCDate()-Math.max(days-1,0));
      form.elements.namedItem('start_date').value=start.toISOString().slice(0,10);form.elements.namedItem('end_date').value=end.toISOString().slice(0,10);
      for(const detail of form.querySelectorAll('fieldset details'))detail.open=false;
    });
  }
  function collectFilters(form){
    if(!filterEngine || !form.elements)return Object.fromEntries(new FormData(form));
    const raw=Object.fromEntries(new FormData(form)),f={};
    for(const [key,value] of Object.entries(raw))if(value&&!key.includes('_selection')&&!/_(date|hour|minute)$/.test(key)&&key!=='attribution')f[key]=value;
    for(const key of ['conversation_id','round_no'])if(f[key]&&!/^[1-9][0-9]*$/.test(f[key]))throw Error('会话 ID 和轮次编号必须为正整数。');
    if(f.round_no&&BigInt(f.round_no)>2147483647n||f.conversation_id&&BigInt(f.conversation_id)>9223372036854775807n)throw Error('会话 ID 或轮次编号超出允许范围。');
    let identity;
    for(const [key,id] of [['inbox_selection','inbox_id'],['agent_selection','agent_id']])if(raw[key]){
      const selected=JSON.parse(raw[key]);
      if(identity&&(identity.source_key!==selected.source_key||identity.account_id!==selected.account_id))throw Error('收件箱与坐席的来源 / 账号不一致，请重新选择。');
      identity=selected;f.source_key=selected.source_key;f.account_id=selected.account_id;f[id]=selected[id];
    }
    if(raw.attribution)f[raw.attribution]=true;
    for(const edge of ['start','end'])if(raw[edge+'_date']){
      const select=form.elements.namedItem(edge+'_hour'),open=select.closest('details').open;
      const clock=open?`${raw[edge+'_hour']||'00'}:${raw[edge+'_minute']||'00'}:${edge==='end'?'59':'00'}`:edge==='end'?'23:59:59':'00:00:00';
      const date=raw[edge+'_date']+'T'+clock+'+08:00';
      f[edge]=edge==='end'?new Date(Date.parse(date)+1000).toISOString():date;
    }
    filterEngine.normalize(f,archive.catalog?.meta?.filter_contract||{});
    return f;
  }
  function init() {
    let catalog;
    try {catalog = JSON.parse($('catalog').textContent);}
    catch (error) {$('summary').textContent = '留档目录缺失，文件可能未完整下载。'; return;}
    archive.catalog = catalog;
    entries = catalog.entries || []; customers = catalog.customers || [];
    const m = catalog.meta || {};
    $('summary').textContent = `${(m.customers || 0).toLocaleString()} 位用户 · ${(m.rounds || 0).toLocaleString()} 轮对话 · 未归轮消息 ${(m.unrounded_messages || 0).toLocaleString()} 条 · ${(m.messages || 0).toLocaleString()} 条消息`
      + ` · 内嵌图片 ${m.images || 0} · 内嵌视频/音频 ${m.videos || 0} · 仅链接附件 ${m.linked_attachments || 0} · 安全拦截 ${m.blocked_attachments || 0} · 失效附件 ${m.unavailable_attachments || 0} · 导出于 ${formatTime(m.exported_at)}`;
    $('exportScope').textContent = exportSummary(m);
    if (['rounds', 'customers'].includes(m.scope?.mode)) $('exportScope').title = (has(exportLockTime(m)) ? '按锁定时刻限定消息时间范围' : '未锁定消息时间') + '，使用生成时的当前记录；不回溯消息编辑前的版本，补录或修订可能改变结果。';
    const form = $('filters');
    if(filterEngine && form.querySelectorAll && form.elements) prepareFilters(form,m);
    form.addEventListener('submit', e => {e.preventDefault();try{filters = collectFilters(form);apply();}catch(error){$('status').textContent=error.message;}});
    form.addEventListener('reset', () => setTimeout(() => {filters = {}; apply();}, 0));
    form.addEventListener('change', e => {if (e.target && e.target.tagName === 'SELECT') {try{filters = collectFilters(form);apply();}catch(error){$('status').textContent=error.message;}}});
    $('toc').addEventListener('click', e => {
      const button = e.target.closest && e.target.closest('button[data-at]');
      if (button) openAt(Number(button.dataset.at));
    });
    $('prev').addEventListener('click', () => {if (page > 1) {page--; renderToc();}});
    $('next').addEventListener('click', () => {if (page * PAGE < view.length) {page++; renderToc();}});
    const box = $('lightbox'), big = $('lightboxImage');
    $('chat').addEventListener('click', e => {
      const target = e.target;
      const playButton = target.closest && target.closest('button[data-play]');
      if (playButton) {play(playButton); return;}
      const saveButton = target.closest && target.closest('button[data-save]');
      if (saveButton) {save(saveButton); return;}
      if (target.classList && target.classList.contains('thumb') && target.src) {big.src = target.src; big.alt = target.alt; box.hidden = false;}
    });
    box.addEventListener('click', () => {box.hidden = true; big.removeAttribute('src');});
    document.addEventListener('keydown', e => {
      if (e.key === 'Escape' && !box.hidden) {box.hidden = true; big.removeAttribute('src'); return;}
      if (/^(INPUT|SELECT|TEXTAREA|VIDEO|AUDIO)$/.test((e.target && e.target.tagName) || '')) return;
      if (e.key === 'ArrowDown' || e.key === 'j') {e.preventDefault(); openAt(current + 1);}
      if (e.key === 'ArrowUp' || e.key === 'k') {e.preventDefault(); openAt(current - 1);}
    });
    apply();
  }
  archive.start = init;
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init);
  else init();
})(typeof window === 'undefined' ? globalThis : window);
