/* ICS reporting UI. Native SVG and browser APIs; no chart library or CDN. */
(() => {
  "use strict";
  const metrics = [
    ["incoming", "进客量", "#2f6bff"], ["ai_only", "纯AI接待量", "#16a085"],
    ["handoff", "转人工量", "#8854d0"], ["rejected", "转人工拒接量", "#e18422"],
    ["effective_human", "有效人工量", "#db5479"], ["low_rating", "低评分数（低于4分）", "#ba3428"]
  ];
  const timeHelp = {
    incoming: "按咨询轮次开始时间统计新询盘，同一客户再次咨询可形成新轮次；随左侧渠道筛选更新。",
    ai_only: "AI接待、未出现人工繁忙拒接提示且没有公开人工回复的轮次数，包含进行中的轮次。",
    handoff: "进入或请求人工接待、出现人工繁忙拒接提示或已有公开人工回复的轮次数，包含继承人工接管状态的新轮次。",
    rejected: "观察到系统‘当前人工坐席繁忙’提示的轮次数；同轮多次提示只计一次，后来接通仍保留。未回复不直接视为拒接。",
    effective_human: "至少有一条公开人工服务回复的咨询轮次数，排除私密备注、系统消息和评价卡。",
    low_rating: "当前关联本轮的评分为1至3分的轮次数；未评分及未关联的评价不计入，已排除复核无效评价。",
    channel: "该渠道的新咨询轮次数，按轮次开始时间归入时段；无法识别渠道的记录归入‘未映射渠道’。"
  };
  const agentHelp = {
    seats: "所选周期内的监控坐席、状态记录、询盘主责坐席、已确认分配询盘、存在响应时长、咨询时长或已归属流单的坐席；来源、账号和坐席ID共同确定身份。",
    inquiries: "区间内有公开人工回复的询盘轮次数，同一轮只计一次。每轮只归属主责坐席（ICS已确认分配的坐席；没有ICS分配记录的轮次不计入任何坐席，单列为“无ICS分配轮次”）。同一轮有主责以外的坐席公开回复（如交班时同事协助，无论主责是否回复）时，该轮不计入任何坐席的任何考核（含咨询状态总时长），仅保留发配量；下一轮照常统计。",
    total_inquiries: "各坐席在所选周期内主责询盘量之和；同事协助过的轮次不计入。",
    replies: "所选区间内该坐席在自己主责询盘中的公开人工回复逻辑消息数（同事协助过的轮次整轮不计），编辑版本不重复计数；排除私密备注、系统消息、机器人回复和评价卡。",
    peak_online: "所选周期内，单个有采样分钟观测到的在线坐席人数最大值；仅覆盖保留期内的数据。",
    observed_minutes: "所选周期内至少有一条成功状态采样的分钟数；不包含缺采样、过期和未来分钟。",
    coverage: "实际坐席状态样本数÷按历史监控成员计算的应采样数×100%，最高显示100%；只统计状态保留期内的已完成分钟。",
    online: "按日展示每分钟采样中状态为online的坐席人数；按月、年及自定义时间段展示对应时段内有采样分钟的平均在线人数。缺采样保留空缺，在线不代表仍有接待容量。",
    first_response: "按转人工发生时间筛选对话。转人工成功后用户须先发言：从用户这一段连续发言的最后一条有效消息（分段发送以最后一段为准）到随后坐席首次有效公开回复计时，只计主责坐席，再对有效对话求平均。转人工指令本身不作计时起点；用户未发言前坐席主动发出的消息（如欢迎语）不算首响。首响超过600秒不计入统计，600秒保留，后续回复不替代首响。转人工后用户未发言、无人回复或时间不明时不计。使用截至查询时已完成的回复，可跨筛选区间结束时间。转人工后有主责以外坐席公开回复（如同事协助）的对话不计入任何坐席的首响。",
    average_response: "按转人工发生时间筛选对话。首段为首响，之后每段从用户连续发言的最后一条有效消息到下一条有效人工回复计时；单次响应超过600秒不计入时长及样本数，600秒保留。按响应次数加权汇总：所选范围内各轮响应总时长相加 ÷ 响应总次数，不先按对话求平均。未回复尾段、连续人工回复、私密消息、评价卡、空白和已删除消息不计；回复可跨筛选区间结束时间。转人工后有主责以外坐席公开回复（如同事协助）的对话不计入任何坐席的响应时长。",
    average_rating: "该坐席在区间内主责询盘当前关联的1至5分评价平均值，同一轮只计一次，未评分不作为0分，已排除复核无效评价。",
    low_rating: "该坐席在区间内主责询盘当前关联评分为1至3分的轮次数，同一坐席同一轮只计一次，已排除复核无效评价。",
    lost_inquiries: "转人工发生在所选区间、截至查询时仍无任何公开人工回复的轮次数，包含仍在等待的询盘；只归属已确认的ICS责任坐席。",
    consultation_seconds: "每轮从转人工成功时刻到本次对话“解决”：把转人工成功、之后的每条有效消息（客户或任何坐席的公开消息，不论谁发）和解决依次排成时间线，相邻两点间隔超过10分钟（恰好10分钟仍连续）即判为两次咨询，中间空档不计；每次从空档后第一条消息起，到空档前最后一条消息止，最后一次以“解决”为终点（解决距上一条消息超过10分钟时以上一条消息为终点）。尚未解决的轮次暂截至最后一条有效消息，不延长到当前时间。归入ICS分配坐席；交班等有同事协助回复的轮次不计入（避免跨班次时段），转人工后始终没有人工回复的流单轮次也不计入。再合并同一坐席并行接待的重叠时段，只累计落在筛选区间内的部分。未观察到转人工的轮次从第一条人工回复起算；有效消息时间缺失的轮次排除。",
    dialogues: "该坐席在区间内主责询盘中，访客转人工后发过有效消息的轮次数，同轮去重。有效消息为非空文字或附件，排除触发转人工的消息、私密消息、评价卡和已删除消息；转接时间不明时不计。访客消息查阅完整轮次至查询时。",
    high_quality_dialogues: "上述对话量中，访客转人工后的有效消息严格超过5条（至少6条）的轮次数；按同一有效消息规则及完整轮次统计，同一坐席同一轮只计一次。",
    assigned_inquiries: "按成功分配时间统计所选区间内，ICS已确认分配给该坐席的询盘轮次数，同一轮只计一次；未回复也计入。仅请求转人工或繁忙拒接但未成功分配、分配归属冲突、缺少实际分配时间的记录不计入，不按当前分配人或实际回复人推测。",
    online_minutes: "在状态保留期内，该坐席被采样为online的分钟数；没有任何状态样本时显示‘—’，不补算为0或离线。",
    busy_minutes: "在状态保留期内，该坐席被采样为busy的分钟数；没有任何状态样本时显示‘—’。",
    unassigned_inquiries: "区间内有公开人工回复、但没有 ICS 已确认分配坐席的轮次数（Chatwoot 手动分配、沿用上一轮人工接待、分配记录冲突等）。这些轮次不计入任何坐席，单列在此，点击可在对话归档中查看。",
    assisted_inquiries: "区间内有公开人工回复、且 ICS 分配坐席以外的坐席在转人工后有效公开回复过的轮次数（如交班时同事协助）。整轮不计入任何坐席的考核（含咨询状态总时长），只保留发配量。",
    bucket: "该时段内有公开人工回复的主责询盘数，同一轮去重；同轮跨时段回复会在各时段各计一次，整个周期的合计再次去重。"
  };
  const qualityHelp = {
    ...agentHelp,
    failed_inquiries: "该坐席在筛选区间内已回复的询盘中，当前人工质检结论为‘不合格’的轮次数，同一坐席同一轮只计一次；待质检及合格不计入。",
    total_failed_inquiries: "汇总全部坐席已回复询盘中，当前人工质检结论为‘不合格’的数量，不受坐席分页影响；只计各轮主责坐席，有同事协助回复的轮次整轮不计。",
    human_failure_rate: "人工质检不合格量÷回复询盘总量×100%。分母包含待质检轮次；没有回复询盘时显示‘—’。概况按所有坐席汇总计算；有同事协助回复的轮次整轮不计入分子和分母。",
    ai_total_rounds: "按轮次开始时间，统计所选区间内全部已采集、已解析的对话轮次数，包含已转人工及待质检轮次，同一客户再次咨询可形成新轮。",
    ai_handoff: "所有对话轮中，已请求或进入人工接待、出现繁忙拒接提示或已有公开人工回复的轮次数，包含继承人工接管状态的新轮。",
    ai_ai_only: "所有对话轮中，已有公开有效AI回复且仍为纯AI接待、没有转人工或公开人工回复的轮次数；一轮多条AI消息只计一次。",
    ai_handoff_failed: "出现系统‘当前人工坐席繁忙’拒接提示的对话轮数，同轮多次拒接只计一次；后来接通仍保留此次失败记录。等待中或未回复不推定为失败。",
    ai_failed_rounds: "筛选区间内全部对话轮中，当前AI质检结论为‘不合格’的轮次数，包含后来已转人工的轮次；人工质检结论不影响此项。",
    ai_failure_rate: "AI质检不合格量÷所有对话轮数×100%，分母包含待质检轮次；没有对话轮时显示‘—’。两类质检均读取当前最新结论，复核会更新历史统计。"
  };
  const userHelp = {
    total_assignments: "所选区间内 ICS 成功分配坐席（会话转入人工接待）的次数，按成功分配时间归入区间，与坐席细化“发配量”合计（含无坐席归属部分）同一口径。纯AI、坐席繁忙或黑名单拒接、排队、仍在请求中、Chatwoot 网页端手动分配或沿用上一轮人工接待（无 ICS 分配记录）的都不计。",
    users_total: "区间内至少成功转人工 1 次的用户数。用户 = 来源 + 账号 + Chatwoot 联系人；同一联系人在多个会话中的成功转人工合并计算。",
    ranked_assignments: "排行前 100 名用户的成功转人工次数之和；用户不足 100 且没有无法识别联系人的记录时，等于成功转人工总次数。",
    ranked_share: "前 100 名次数合计 ÷ 成功转人工总次数 × 100%。",
    unidentified_assignments: "成功转人工但无法识别联系人的次数：计入总次数，不参与排行。",
    rank: "按成功转人工次数从高到低；次数相同依次按最近一次成功转人工时间（晚者在前）、联系人 ID 排序，每次查询顺序一致。只取前 100 名，每页 20 名。",
    user: "Chatwoot 联系人名称、联系人 ID 与该用户在区间内成功转人工的渠道。",
    assignments: "该用户在区间内的成功转人工次数：一轮最多一次 ICS 成功分配，同一轮内多次发送“转人工”只计 1 次；按 ICS 成功分配时间归入区间。点击数字在对话归档中按客户打开该用户（时间依据“ICS 成功分配时间”，只显示这些轮次）。",
    daily_count: "直接读取排队功能的日计数：查询时刻之前近24小时内成功转人工的次数，24小时前的边界不计入。按账号和联系人合并所有会话、渠道，不随报表日期或渠道筛选改变；排队关闭时仍累计，从排队计数功能部署后开始记录。— 表示计数暂不可用或该来源未接入。",
    weekly_count: "直接读取排队功能的周计数：北京时间最近一个周五09:00至查询时刻成功转人工的次数，每周五09:00切换新周期。按账号和联系人合并所有会话、渠道，不随报表日期或渠道筛选改变；排队关闭时仍累计，从排队计数功能部署后开始记录。显示已成功次数，不加本次请求的1次。— 表示计数暂不可用或该来源未接入。",
    first_assigned_at: "区间内该用户第一次成功转人工（ICS 成功分配）的时间，北京时间到秒。",
    last_assigned_at: "区间内该用户最近一次成功转人工（ICS 成功分配）的时间，北京时间到秒；也是同次数时的排序依据。",
    blacklist: "该用户当前的黑名单状态（R6）：拉黑中 / 曾被拉黑。拉黑后发转人工被拒接不计入次数，拉黑前的成功转人工照常计入。",
    trace: "展开该用户区间内每一次成功转人工：分配时间、会话与轮次、分配坐席；条数与次数一致，点击可跳到对话归档对应轮次。"
  };
  const aiFields = [["total_rounds", "所有对话轮数"], ["handoff", "转人工量"], ["ai_only", "纯AI回复量"], ["handoff_failed", "转人工失败量"], ["failed_rounds", "不合格量"], ["failure_rate", "不合格率"]];
  const beijingDay = () => new Intl.DateTimeFormat("sv-SE", {timeZone: "Asia/Shanghai", year: "numeric", month: "2-digit", day: "2-digit"}).format(new Date());
  const reportModes = [["day", "日"], ["week", "周"], ["month", "月"], ["year", "年"], ["custom", "自定义"]];
  const weekRange = day => {
    // 以北京时间日期计算周一至周日；使用 UTC 运算，避免浏览器时区及夏令时影响。
    const start = new Date(`${day}T00:00:00Z`);
    start.setUTCDate(start.getUTCDate() - (start.getUTCDay() + 6) % 7);
    const end = new Date(start);
    end.setUTCDate(end.getUTCDate() + 6);
    // 首尾周只保留系统支持的 2000 至 2099 年内日期。
    return {start: start.toISOString().slice(0, 10) < "2000-01-01" ? "2000-01-01" : start.toISOString().slice(0, 10),
      end: end.toISOString().slice(0, 10) > "2099-12-31" ? "2099-12-31" : end.toISOString().slice(0, 10)};
  };
  const number = value => value === null || value === undefined ? "—" : Number(value).toLocaleString("zh-CN", {maximumFractionDigits: 2});
  const percent = value => value === null || value === undefined ? "—" : `${number(value)}%`;
  const duration = value => {
    if (value === null || value === undefined) return "—";
    const seconds = Math.round(Number(value) * 100) / 100;
    const hours = Math.floor(seconds / 3600), minutes = Math.floor(seconds % 3600 / 60);
    return `${hours ? `${number(hours)}小时` : ""}${minutes ? `${minutes}分` : ""}${number(Number((seconds % 60).toFixed(2)))}秒`;
  };

    const parsePageTarget = (raw, totalPages) => {
      const text = String(raw ?? "").trim(), last = Math.max(1, totalPages);
      if (!/^-?[0-9]+$/.test(text)) return {ok:false, page:null, adjusted:false, message:"请输入整数页码"};
      const digits = text.replace(/^-/, "").replace(/^0+/, "") || "0";
      if (text.startsWith("-") || digits === "0") return {ok:true, page:1, adjusted:true, message:"已调整到第 1 页"};
      const limit = String(last), above = digits.length > limit.length || digits.length === limit.length && digits > limit;
      return {ok:true, page:above ? last : Number(digits), adjusted:above, message:above ? `已调整到第 ${last} 页` : ""};
    };

  function create({api, escapeHtml: esc, getToken, isActive, download, openArchive}) {
    const $ = id => document.getElementById(id);
    // 报表上的每个计数/时长格子都可点击：带上同一指标、统计窗口、坐席（或渠道）和报表生成时刻，
    // 在对话归档中列出的正是该格子计入的轮次（同一份指标定义，见 quality/processing/stat_metrics.py）。
    const drills = [];
    const drill = (text, value, spec) => {
      if (!openArchive || !spec || !(Number(value) > 0)) return text;
      drills.push(spec);
      return `<button class="report-archive-link" type="button" data-drill="${drills.length - 1}" title="在对话归档中查看这些轮次（${esc(spec.label)}）">${text}</button>`;
    };
    const seatOf = a => ({source_key: a.source_key, account_id: a.account_id, agent_id: a.agent_id, agent_name: a.agent_name});
    const bucketWindow = (r, i) => ({start: r.buckets[i].start || r.buckets[i].key, end: r.buckets[i].end || r.buckets[i + 1]?.key || r.end});
    const period = r => r.refined ? `${r.start_label} 至 ${r.end_label}` : r.start_date === r.end_date ? r.start_date : `${r.start_date} 至 ${r.end_date}`;
    const spec = (r, metric, label, extra = {}) => ({metric, label, start: r.start, end: r.end, as_of: r.as_of,
      period: period(r), ...extra});
    const openArchiveFromTable = event => {
      if (event.target.closest?.("[data-trace]")?.dataset?.trace) return toggleTrace(event);
      const button = event.target.closest?.("[data-drill]");
      const r = model.result;
      if (!button || !openArchive || !r) return;
      const item = drills[Number(button.dataset.drill)];
      if (item) openArchive({...item, count: Number(item.count ?? 0)});
    };
    const helpLabel = (label, key) => {
      const help = model.kind === "users" ? userHelp[key] : model.kind === "quality" ? qualityHelp[key] : model.kind === "time" ? timeHelp[key.startsWith("channel:") ? "channel" : key] : agentHelp[key];
      return `${esc(label)}<span class="report-help" tabindex="0" role="img" aria-label="${esc(`${label}：${help}`)}" title="${esc(help)}">?</span>`;
    };
    const today = beijingDay();
    const model = {kind: "time", mode: "day", date: today, startDate: today, endDate: today, page: 1, controller: null,
      startTime: "", endTime: "", timeOpen: false, traces: new Map(),
      loading: false, completedPage: 1, pageFailed: false, epoch: 0, loadTimer: null, result: null, applied: null, exportController: null, optionsToken: null, options: {channels: []}, hidden: new Set(), mounted: false};

    const mount = () => {
      if (model.mounted) return;
      model.mounted = true;
      $("statisticsPage").innerHTML = `
        <aside class="panel report-sidebar" aria-label="筛选与统计概况"><form id="reportFilters" class="report-filters">
          <h3 class="report-sidebar-title">统计筛选</h3>
          <div class="report-field report-period-field"><span id="reportModeLabel">统计周期</span><div class="report-period-buttons" role="group" aria-labelledby="reportModeLabel">${reportModes.map(([mode, label]) => `<button id="reportMode-${mode}" class="report-period-button" data-mode="${mode}" type="button" aria-pressed="${mode === model.mode}">${label}</button>`).join("")}</div></div>
          <label id="reportDateField" class="report-field"><span id="reportDateLabel">选择日期</span><input id="reportDate" type="date" min="2000-01-01" max="2099-12-31" required aria-labelledby="reportDateLabel" aria-describedby="reportWeekRange"><span id="reportWeekRange" class="report-week-range hidden" aria-live="polite"></span></label>
          <label id="reportStartField" class="report-field hidden">起始日期<input id="reportStartDate" type="date" min="2000-01-01" max="2099-12-31" required disabled aria-label="统计起始日期"></label>
          <label id="reportEndField" class="report-field hidden">结束日期<input id="reportEndDate" type="date" min="2000-01-01" max="2099-12-31" required disabled aria-label="统计结束日期"></label>
          <div id="reportTimeField" class="report-field report-time hidden" role="group" aria-label="指定时刻"><button id="reportTimeToggle" class="report-time-toggle" type="button" aria-expanded="false" aria-controls="reportTimeDetail">指定时刻 ▸</button>
            <div id="reportTimeDetail" class="report-time-detail hidden"><span>开始</span><select id="reportStartHour" aria-label="开始时刻：时"></select><select id="reportStartMinute" aria-label="开始时刻：分"></select><span>结束（含）</span><select id="reportEndHour" aria-label="结束时刻：时"></select><select id="reportEndMinute" aria-label="结束时刻：分"></select></div></div>
          <label id="reportChannelField" class="report-field">渠道<select id="reportChannel"><option value="">全部渠道</option></select></label>
          <div class="report-filter-actions"><button id="reportRefresh" class="button primary" type="submit">查询统计</button><button id="reportReset" class="button secondary" type="button">重置</button><button id="reportExport" class="button secondary report-export" type="button" disabled>导出 Excel</button></div>
          <span id="reportZone" class="report-zone">北京时间 · 时段开始含、结束不含</span>
        </form>
        <section class="report-overview" aria-label="统计概况"><h3 class="report-sidebar-title">统计概况</h3><div id="reportCards" class="report-cards"></div></section>
        <p id="reportStatus" class="report-status" role="status" aria-live="polite"></p>
        <details class="report-rules"><summary>数据与统计口径说明</summary><div id="reportNotice" class="report-note"></div><div id="reportRules"></div></details>
        </aside>
        <div class="report-main">
        <section id="reportChartPanel" class="panel report-chart-panel" aria-label="统计趋势"><div class="panel-header"><div class="panel-heading"><h3 id="reportChartTitle">询盘趋势</h3><p id="reportChartSubtitle">按咨询开始时间归入对应时段</p></div></div>
          <div class="report-legend"><button id="reportSelectAll" class="button secondary report-select-all" type="button" aria-label="全选或取消全部趋势指标" disabled>全选</button><div id="reportLegend" class="report-legend-items"></div></div><div class="report-chart-scroll"><div id="reportChart" class="report-chart"></div></div><p id="reportChartDetail" class="report-chart-detail">悬停或点击时间点查看数值；勾选图例切换指标。</p>
        </section>
        <div id="reportTables" class="report-tables">
        <section class="panel report-data-panel"><div class="panel-header"><div class="panel-heading"><h3 id="reportTableTitle">时段数据</h3><p id="reportTableSubtitle">折线图对应的完整指标明细</p></div></div><div id="reportTable" class="table-wrap report-table"></div>
          <div id="reportQualityPagination" class="report-pagination hidden"><span id="reportQualityPageInfo"></span><div><button id="reportQualityFirst" type="button" class="button secondary">首页</button><button id="reportQualityPrev" type="button" class="button secondary">上一页</button><button id="reportQualityNext" type="button" class="button secondary">下一页</button><button id="reportQualityLast" type="button" class="button secondary">末页</button><span style="display:inline-flex;align-items:center;gap:4px;font-size:11px;color:var(--muted)"><input id="reportQualityJumpInput" type="text" inputmode="numeric" style="width:52px;padding:3px 6px;text-align:center;font-size:12px" aria-label="跳转到页码" placeholder="页码"><button id="reportQualityJump" type="button" class="button secondary">跳转</button></span></div></div>
        </section>
        <section id="reportAgentPanel" class="panel report-data-panel hidden"><div class="panel-header"><div class="panel-heading"><h3 id="reportAgentTitle">坐席回复询盘</h3><p id="reportAgentSubtitle">按实际回复时间归入时段；同一坐席在同一时段回复同一轮询盘仅计一次</p></div></div>
          <div id="reportAgents" class="table-wrap report-table"></div><div id="reportPagination" class="report-pagination"><span id="reportPageInfo"></span><div><button id="reportFirst" type="button" class="button secondary">首页</button><button id="reportPrev" type="button" class="button secondary">上一页</button><button id="reportNext" type="button" class="button secondary">下一页</button><button id="reportLast" type="button" class="button secondary">末页</button><span style="display:inline-flex;align-items:center;gap:4px;font-size:11px;color:var(--muted)"><input id="reportJumpInput" type="text" inputmode="numeric" style="width:52px;padding:3px 6px;text-align:center;font-size:12px" aria-label="跳转到页码" placeholder="页码"><button id="reportJump" type="button" class="button secondary">跳转</button></span></div></div>
        </section></div></div>`;
      updateDateInput();
      $("reportFilters").addEventListener("submit", event => {event.preventDefault(); model.page = 1; load();});
      $("reportReset").addEventListener("click", reset);
      $("reportExport").addEventListener("click", exportReport);
      const two = n => Array.from({length: n}, (_, i) => String(i).padStart(2, "0"));
      for (const id of ["reportStartHour", "reportEndHour"]) $(id).innerHTML = two(24).map(h => `<option value="${h}">${h} 时</option>`).join("");
      for (const id of ["reportStartMinute", "reportEndMinute"]) $(id).innerHTML = two(60).map(m => `<option value="${m}">${m} 分</option>`).join("");
      $("reportStartHour").value = $("reportStartMinute").value = "00";
      $("reportEndHour").value = "23"; $("reportEndMinute").value = "59";
      $("reportTimeToggle").addEventListener("click", () => {model.timeOpen = !model.timeOpen; updateTimeDetail(); model.page = 1; scheduleLoad();});
      for (const id of ["reportStartHour", "reportStartMinute", "reportEndHour", "reportEndMinute"]) $(id).addEventListener("change", () => {model.page = 1; scheduleLoad();});
      for (const [mode] of reportModes) $(`reportMode-${mode}`).addEventListener("click", () => {
        if (model.mode === mode) return;
        model.mode = mode; model.date = beijingDay(); updateDateInput(); model.page = 1; scheduleLoad();
      });
      for (const id of ["reportDate", "reportStartDate", "reportEndDate", "reportChannel"]) $(id).addEventListener("change", () => {model.page = 1; scheduleLoad();});
      $("reportLegend").addEventListener("change", event => {
        const key = event.target.dataset.series;
        if (!key) return;
        event.target.checked ? model.hidden.delete(key) : model.hidden.add(key);
        renderChart();
      });
      $("reportSelectAll").addEventListener("click", () => {
        if (!model.result) return;
        const all = series(), allSelected = all.every(([key]) => !model.hidden.has(key));
        for (const [key] of all) allSelected ? model.hidden.add(key) : model.hidden.delete(key);
        renderChart();
      });
      $("reportChart").addEventListener("click", event => showPoint(event));
      for (const id of ["reportTable", "reportAgents", "reportCards", "reportTableSubtitle"]) $(id).addEventListener("click", openArchiveFromTable);
      $("reportChart").addEventListener("focusin", event => showPoint(event));
      for (const prefix of ["report", "reportQuality"]) {
        const total = () => model.result?.total || 0;
        const pageSize = () => model.result?.page_size || 20;
        const pages = () => Math.max(1, Math.ceil(total() / pageSize()));
        for (const [suffix, target] of [["First", () => 1], ["Prev", () => model.page - 1], ["Next", () => model.page + 1], ["Last", pages]]) {
          $(`${prefix}${suffix}`).addEventListener("click", () => goToPage(String(target()), prefix));
        }
        $(`${prefix}Jump`).addEventListener("click", () => goToPage($(`${prefix}JumpInput`).value, prefix));
        $(`${prefix}JumpInput`).addEventListener("keydown", event => {
          if (event.key !== "Enter") return;
          event.preventDefault();
          if (!event.repeat && !event.isComposing && event.keyCode !== 229) goToPage($(`${prefix}JumpInput`).value, prefix);
        });
      }
      let resizeTimer;
      window.addEventListener("resize", () => {
        clearTimeout(resizeTimer);
        resizeTimer = setTimeout(() => {if (model.result && !["quality", "users"].includes(model.kind) && isActive()) renderChart();}, 100);
      });
    };

    function updatePagination() {
      const r = model.result, pages = Math.max(1, Math.ceil((r?.total || 0) / (r?.page_size || 20)));
      for (const prefix of ["report", "reportQuality"]) {
        for (const suffix of ["First", "Prev", "Next", "Last", "Jump", "JumpInput"]) {
          $(prefix + suffix).disabled = model.loading || !r || pages <= 1 || (["First","Prev"].includes(suffix) && model.page <= 1) || (["Next","Last"].includes(suffix) && model.page >= pages);
        }
      }
    }
    function goToPage(raw, prefix) {
      if (model.loading || !model.result) return;
      const pages = Math.max(1, Math.ceil(model.result.total / model.result.page_size)), target = parsePageTarget(raw, pages);
      if (!target.ok) {$(prefix + "PageInfo").textContent = target.message; return;}
      $(prefix + "JumpInput").value = String(target.page);
      model.pageAdjustment = target.message;
      if (target.message) $(prefix + "PageInfo").textContent = target.message;
      if (pages <= 1 || target.page === model.page && !model.pageFailed) return;
      model.page = target.page; load();
    }
    function reset() {
      model.mode = "day"; model.date = beijingDay(); model.page = 1; model.hidden.clear();
      model.startDate = model.endDate = model.date;
      model.timeOpen = false; $("reportStartHour").value = $("reportStartMinute").value = "00";
      $("reportEndHour").value = "23"; $("reportEndMinute").value = "59";
      $("reportChannel").value = "";
      updateDateInput(); load();
    }

    function readDate() {
      if (model.mode === "custom") {
        model.startDate = $("reportStartDate").value;
        model.endDate = $("reportEndDate").value;
        return;
      }
      const value = $("reportDate").value;
      if (!value) return;
      const mode = $("reportDate").type;
      model.date = mode === "number" ? `${value}-01-01` : mode === "month" ? `${value}-01` : value;
      if (model.mode === "week") {
        const range = weekRange(model.date);
        $("reportWeekRange").textContent = `${range.start} 至 ${range.end}`;
      }
    }
    function updateDateInput() {
      const input = $("reportDate"), day = model.date;
      const custom = model.mode === "custom", daily = model.mode === "day" || model.mode === "week";
      for (const [mode] of reportModes) $(`reportMode-${mode}`).setAttribute("aria-pressed", String(mode === model.mode));
      $("reportWeekRange").classList.toggle("hidden", model.mode !== "week");
      $("reportDateField").classList.toggle("hidden", custom);
      $("reportStartField").classList.toggle("hidden", !custom);
      $("reportEndField").classList.toggle("hidden", !custom);
      input.disabled = custom;
      $("reportStartDate").disabled = $("reportEndDate").disabled = !custom;
      $("reportStartDate").value = model.startDate;
      $("reportEndDate").value = model.endDate;
      $("reportEndDate").setCustomValidity("");
      $("reportZone").textContent = custom ? "北京时间 · 包含起止日期全天；按日汇总" : "北京时间 · 时段开始含、结束不含";
      updateTimeDetail();
      if (custom) return;
      input.type = daily ? "date" : model.mode === "month" ? "month" : "number";
      input.min = daily ? "2000-01-01" : model.mode === "month" ? "2000-01" : "2000";
      input.max = daily ? "2099-12-31" : model.mode === "month" ? "2099-12" : "2099";
      input.value = daily ? day : model.mode === "month" ? day.slice(0, 7) : day.slice(0, 4);
      $("reportDateLabel").textContent = model.mode === "week" ? "选择周内日期" : model.mode === "day" ? "选择日期" : model.mode === "month" ? "选择月份" : "选择年份";
      readDate();
    }
    // R10 二级时刻：所有报表页的按日 / 自定义都提供；不展开即全天（开始 00:00:00，结束 23:59:59 含）。
    // 时间统计 / 坐席细化 / 质检统计的回复类指标按小时预聚合，只能选到整点：分固定为开始 00、结束 59。
    const timeAllowed = () => ["day", "custom"].includes(model.mode);
    const hourOnly = () => model.kind !== "users";
    function updateTimeDetail() {
      const allowed = timeAllowed(), open = allowed && model.timeOpen, hours = hourOnly();
      $("reportTimeField").classList.toggle("hidden", !allowed);
      $("reportTimeDetail").classList.toggle("hidden", !open);
      $("reportTimeToggle").setAttribute("aria-expanded", String(open));
      $("reportTimeToggle").textContent = open ? "指定时刻 ▾（收起即全天）" : "指定时刻 ▸";
      if (hours) {$("reportStartMinute").value = "00"; $("reportEndMinute").value = "59";}
      $("reportStartMinute").disabled = $("reportEndMinute").disabled = hours;
      $("reportStartMinute").title = $("reportEndMinute").title = hours ? "该报表按整点统计，分钟固定" : "";
      if (open) $("reportZone").textContent = hours ? "北京时间 · 按整点统计：开始时刻整点起，至结束时刻该小时末（含）" : "北京时间 · 开始时刻起，至结束时刻该分钟末（含）";
      else if (model.mode === "week") $("reportZone").textContent = "北京时间 · 周一至周日，按日汇总";
      else if (model.kind === "users") $("reportZone").textContent = "北京时间 · 起止日期全天（00:00:00 至 23:59:59，含）";
      else $("reportZone").textContent = model.mode === "custom" ? "北京时间 · 包含起止日期全天；按日汇总" : "北京时间 · 时段开始含、结束不含";
    }
    function stop(reset = false) {
      clearTimeout(model.loadTimer); model.loadTimer = null;
      model.epoch++; model.loading = false;
      model.controller?.abort(); model.controller = null;
      model.exportController?.abort(); model.exportController = null; model.applied = null;
      if (model.mounted) {$("reportExport").disabled = true; $("reportExport").textContent = "导出 Excel";}
      if (reset) {
        model.result = null; model.optionsToken = null; model.options = {channels: []};
        if (model.mounted) {
          $("reportChannel").innerHTML = '<option value="">全部渠道</option>';
          clear();
        }
      }
    }
    function scheduleLoad() {
      // 条件改变时立即作废旧结果，只延迟新请求的发送。
      stop(); readDate(); model.result = null; model.traces.clear();
      clear("正在准备查询…");
      $("reportStatus").classList.remove("error");
      $("reportStatus").textContent = "正在准备查询…";
      $("reportRefresh").disabled = false;
      model.loadTimer = setTimeout(() => {
        model.loadTimer = null;
        if (isActive() && getToken()) load();
      }, 400);
    }
    function clear(message = "正在读取统计数据…") {
      for (const id of ["reportChart", "reportTable", "reportAgents"]) $(id).innerHTML = `<div class="report-empty">${esc(message)}</div>`;
      $("reportCards").innerHTML = ""; $("reportLegend").innerHTML = "";
      $("reportSelectAll").disabled = true;
      $("reportNotice").textContent = ""; $("reportPageInfo").textContent = "";
      $("reportPrev").disabled = $("reportNext").disabled = true;
      $("reportQualityPrev").disabled = $("reportQualityNext").disabled = true;
      for (const prefix of ["report", "reportQuality"]) for (const suffix of ["First", "Last", "Jump", "JumpInput"]) $(prefix + suffix).disabled = true;
      $("reportQualityPageInfo").textContent = ""; $("reportRules").textContent = "";
      $("reportChartDetail").textContent = "悬停或点击时间点查看数值；勾选图例切换指标。";
    }
    async function loadOptions(signal, epoch, token) {
      if (model.optionsToken === token) return;
      try {
        const options = await api("/admin/api/statistics/filters", {signal});
        if (epoch !== model.epoch || token !== getToken() || !isActive()) return;
        model.options = options; model.optionsToken = token;
        $("reportChannel").innerHTML = '<option value="">全部渠道</option>' + options.channels.map(c => `<option value="${esc(c.key)}">${esc(c.name)}</option>`).join("");
        if (model.result && !model.pageFailed) {render(); updatePagination();}
      } catch (_) { /* Report endpoint still provides readable data if the directory fails. */ }
    }
    async function load() {
      const startDate = $("reportStartDate").value, endDate = $("reportEndDate").value;
      let dateError = "";
      if (model.mode === "custom" && startDate && endDate) {
        if (endDate < startDate) dateError = "结束日期不能早于起始日期。";
        // 使用 UTC 日期差计算包含起止两天的跨度，避免浏览器当地夏令时影响。
        else if ((Date.parse(endDate + "T00:00:00Z") - Date.parse(startDate + "T00:00:00Z")) / 86400000 + 1 > 366)
          dateError = "自定义时间段最多 366 天（含起止日期），请缩小范围。";
      }
      $("reportEndDate").setCustomValidity(dateError);
      if (!$("reportFilters").reportValidity()) {stop(); $("reportRefresh").disabled = false; return;}
      const previousResult = model.result, successfulPage = model.completedPage;
      readDate(); stop(); model.result = null;
      const epoch = model.epoch, token = getToken(), controller = new AbortController();
      model.controller = controller;
      // 周筛选复用起止日期查询，使报表、导出与归档下钻使用同一个统计区间。
      const params = new URLSearchParams({mode: model.mode === "week" ? "custom" : model.mode});
      if (model.mode === "week") {
        const range = weekRange(model.date);
        params.set("start_date", range.start); params.set("end_date", range.end);
      } else if (model.mode === "custom") {
        params.set("start_date", model.startDate); params.set("end_date", model.endDate);
      } else params.set("date", model.date);
      if (["time", "users"].includes(model.kind) && $("reportChannel").value) params.set("channel_key", $("reportChannel").value);
      if (timeAllowed() && model.timeOpen) {
        params.set("start_time", `${$("reportStartHour").value}:${$("reportStartMinute").value}`);
        params.set("end_time", `${$("reportEndHour").value}:${$("reportEndMinute").value}`);
        if ((model.mode === "day" || model.startDate === model.endDate) && params.get("end_time") < params.get("start_time")) {
          clear("结束时刻早于开始时刻"); $("reportStatus").textContent = "结束时刻不能早于开始时刻。"; $("reportStatus").classList.add("error"); return;
        }
      }
      if (model.kind !== "time") {params.set("page", model.page); params.set("page_size", 20);}
      model.loading = true; updatePagination();
      model.traces.clear();
      clear(); $("reportStatus").classList.remove("error"); $("reportStatus").textContent = "正在读取统计数据…";
      $("reportRefresh").disabled = true;
      const optionsTask = loadOptions(controller.signal, epoch, token);
      try {
        let result;
        for (let correction = 0; correction < 2; correction++) {
          result = await api(`/admin/api/statistics/${model.kind}?${params}`, {signal: controller.signal});
          if (epoch !== model.epoch || token !== getToken() || !isActive()) return;
          if (model.kind === "time") break;
          const last = Math.max(1, Math.ceil(Number(result.total || 0) / (result.page_size || 20)));
          if (Number(result.page || params.get("page")) <= last) break;
          if (correction === 1) throw new Error("页数已变化，请重试");
          params.set("page", last);
        }
        if (epoch !== model.epoch || token !== getToken() || !isActive()) return;
        if (model.kind !== "time") {model.page = Math.min(Number(result.page || params.get("page")), Math.max(1, Math.ceil((result.total || 0) / (result.page_size || 20)))); result.page = model.page;}
        model.completedPage = model.page; model.pageFailed = false;
        model.result = result; model.applied = new URLSearchParams(params); render();
        $("reportExport").disabled = false;
      } catch (error) {
        if (error.name === "AbortError" || epoch !== model.epoch || token !== getToken() || !isActive()) return;
        model.result = previousResult; model.page = successfulPage; model.pageFailed = true;
        clear("暂无可显示的数据");
        if (previousResult) for (const prefix of ["report", "reportQuality"]) $(prefix + "PageInfo").textContent = `第 ${successfulPage} / ${Math.max(1, Math.ceil(previousResult.total / previousResult.page_size))} 页 · 查询未完成`;
        $("reportStatus").textContent = error.message; $("reportStatus").classList.add("error");
      } finally {
        if (epoch === model.epoch) {model.loading = false; $("reportRefresh").disabled = false; updatePagination(); if (model.pageAdjustment) {for (const prefix of ["report", "reportQuality"]) $(prefix + "PageInfo").textContent += ` · ${model.pageAdjustment}`; model.pageAdjustment = "";}}
      }
      await optionsTask;
    }
    async function exportReport() {
      if (!model.result || !model.applied || model.exportController || !isActive()) return;
      const controller = new AbortController(), epoch = model.epoch, token = getToken(), kind = model.kind;
      const params = new URLSearchParams(model.applied), result = model.result;
      params.delete("page"); params.delete("page_size");
      model.exportController = controller;
      $("reportExport").disabled = true; $("reportExport").textContent = "正在导出…";
      const current = () => !controller.signal.aborted && epoch === model.epoch && token === getToken() && isActive();
      try {
        const blob = await api(`/admin/api/statistics/${kind}/export.xlsx?${params}`, {signal: controller.signal, responseType: "blob"});
        if (!current()) return;
        const title = {time: "时间统计", agents: "坐席细化", quality: "质检统计", users: "用户统计"}[kind];
        download(blob, `${title}_${result.start_date || result.date}_${result.end_date || result.date}.xlsx`);
      } catch (error) {
        if (error.name !== "AbortError" && current()) {
          $("reportStatus").textContent = error.message || "导出失败，请稍后重试。";
          $("reportStatus").classList.add("error");
        }
      } finally {
        if (model.exportController === controller) {
          model.exportController = null;
          $("reportExport").disabled = !model.applied; $("reportExport").textContent = "导出 Excel";
        }
      }
    }
    function start(kind) {
      mount(); model.kind = kind; model.page = 1;
      const tableOnly = kind === "quality" || kind === "users";
      $("statisticsPage").classList.toggle("quality-report", tableOnly);
      $("reportChartPanel").classList.toggle("hidden", tableOnly);
      $("reportQualityPagination").classList.toggle("hidden", !tableOnly);
      $("reportPagination").classList.toggle("hidden", kind !== "agents");
      $("reportChannelField").classList.toggle("hidden", !["time", "users"].includes(kind));
      $("reportAgentPanel").classList.toggle("hidden", kind === "time" || kind === "users");
      updateTimeDetail();
      $("reportTables").classList.toggle("with-agents", kind === "agents");
      $("reportTables").classList.toggle("with-quality", kind === "quality");
      $("reportAgentTitle").textContent = kind === "quality" ? "AI质检报表" : "坐席回复询盘";
      $("reportAgentSubtitle").textContent = kind === "quality" ? "按轮次开始时间统计全部对话；使用最新AI质检结论" : "按实际回复时间归入时段；同一坐席在同一时段回复同一轮询盘仅计一次";
      $("reportChartTitle").textContent = kind === "time" ? "询盘趋势" : "可用客服量（在线）";
      $("reportChartSubtitle").textContent = kind === "time" ? "按咨询开始时间归入对应时段；数据随左侧渠道筛选更新" : "按分钟观测在线状态；缺采样处断开显示";
      $("reportTableTitle").textContent = kind === "users" ? "用户成功转人工排行（前 100 名）" : kind === "quality" ? "人工质检表" : kind === "time" ? "时段数据" : "客服回复质量";
      $("reportTableSubtitle").textContent = kind === "users" ? "按 ICS 成功分配时间统计；点击次数打开对话归档，点“溯源”查看每一次成功转人工" : kind === "quality" ? "按坐席统计已回复询盘，使用最新人工质检结论" : kind === "time" ? "折线图对应的完整指标明细" : "首响与响应按转人工时间筛选，其余询盘按实际回复时间；与下表共用分页";
      load();
    }
    const channelName = key => model.options.channels.find(c => c.key === key)?.name || (key === "unmapped" ? "未映射渠道" : key);
    function series() {
      if (model.kind === "agents") return [["online", model.mode === "day" ? "每分钟在线人数" : "平均在线人数", "#2f6bff"]];
      return metrics;
    }
    const getValue = (row, key) => key.startsWith("channel:") ? row.channels?.[key.slice(8)] || 0 : row[key];
    function chartData() {
      const result = model.result;
      if (model.kind === "agents") return result.presence_points;
      return result.rows.map((row, i) => ({...row, label: result.buckets[i].label, future: result.buckets[i].future}));
    }
    function showPoint(event) {
      const target = event.target.closest?.("[data-point]");
      if (!target || !model.result) return;
      const row = chartData()[Number(target.dataset.point)];
      if (row) $("reportChartDetail").textContent = `${row.label} · ${row.future ? "未来时段" : series().filter(s => !model.hidden.has(s[0])).map(s => `${s[1]} ${number(getValue(row, s[0]))}`).join("　")}`;
    }
    function renderChart() {
      if (!model.result) return;
      const all = series(), data = chartData(), chosen = all.filter(s => !model.hidden.has(s[0]));
      $("reportSelectAll").disabled = false;
      $("reportLegend").innerHTML = all.map(([key, name, color]) => `<label style="--series-color:${color}"><input type="checkbox" data-series="${esc(key)}" ${model.hidden.has(key) ? "" : "checked"}><i></i>${helpLabel(name, key)}</label>`).join("");
      if (!chosen.length) {$("reportChart").innerHTML = '<div class="report-empty">请勾选至少一个指标。</div>'; return;}
      const width = Math.max(640, $("reportChart").clientWidth || 1000);
      const height = Math.max(140, $("reportChart").clientHeight || 200);
      const custom = model.result.mode === "custom";
      const left = custom ? 45 : 40, right = custom ? 45 : 20, top = 16, bottom = 30;
      const plotWidth = width - left - right, plotHeight = height - top - bottom;
      let peak = 0;
      data.forEach(row => {if (!row.future) chosen.forEach(s => {peak = Math.max(peak, Number(getValue(row, s[0])) || 0);});});
      const step = Math.max(1, Math.ceil(peak / 4)), ceiling = step * 4;
      const x = i => left + i * plotWidth / Math.max(1, data.length - 1), y = value => top + plotHeight * (1 - value / ceiling);
      let svg = `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 ${width} ${height}" role="img" aria-label="${esc($("reportChartTitle").textContent)}"><title>${esc($("reportChartTitle").textContent)}</title>`;
      for (let i = 0; i <= 4; i++) svg += `<line x1="${left}" y1="${y(i * step)}" x2="${width - right}" y2="${y(i * step)}" stroke="#e7eaf0"/><text x="${left - 12}" y="${y(i * step) + 4}" text-anchor="end">${number(i * step)}</text>`;
      data.forEach((row, i) => {
        const stride = custom ? Math.max(1, Math.ceil(data.length / Math.max(2, Math.floor(plotWidth / 90)))) : data.length > 100 ? 120 : data.length > 24 ? 2 : 1;
        if (i % stride === 0 || i === data.length - 1) svg += `<text x="${x(i)}" y="${height - 18}" text-anchor="middle">${esc(row.label)}</text>`;
      });
      chosen.forEach(([key, name, color]) => {
        let path = "", previous = false;
        data.forEach((row, i) => {
          const value = row.future ? null : getValue(row, key);
          if (value === null || value === undefined) {previous = false; return;}
          path += `${previous ? "L" : "M"}${x(i).toFixed(2)},${y(value).toFixed(2)} `; previous = true;
          if (data.length <= 31 || (data[i - 1]?.[key] == null && data[i + 1]?.[key] == null)) svg += `<circle cx="${x(i)}" cy="${y(value)}" r="3" fill="${color}"/>`;
        });
        svg += `<path d="${path}" fill="none" stroke="${color}" stroke-width="2" stroke-linejoin="round"><title>${esc(name)}</title></path>`;
      });
      data.forEach((row, i) => {
        const hitWidth = plotWidth / Math.max(1, data.length - 1);
        const tooltip = `${row.label} · ${row.future ? "未来时段" : chosen.map(s => `${s[1]} ${number(getValue(row, s[0]))}`).join(" / ")}`;
        svg += `<rect class="report-hit" data-point="${i}" x="${x(i) - hitWidth / 2}" y="${top}" width="${hitWidth}" height="${plotHeight}" fill="transparent" ${data.length <= 31 || i % 60 === 0 ? 'tabindex="0"' : ""} aria-label="${esc(tooltip)}"><title>${esc(tooltip)}</title></rect>`;
      });
      svg += "</svg>";
      $("reportChart").innerHTML = svg;
    }
    function cards(items) {
      $("reportCards").innerHTML = items.map(([label, value, key, target]) => `<div class="report-card"><span class="report-card-label">${helpLabel(label, key)}</span><strong>${target ? drill(number(value), value, target) : number(value)}</strong></div>`).join("");
    }
    function renderTime() {
      const r = model.result;
      const channels = new Set([...model.options.channels.map(c => c.key), ...Object.keys(r.channels || {})]);
      const fields = [...metrics, ...[...channels].map(key => [`channel:${key}`, `${channelName(key)}进客量`])];
      drills.length = 0;
      const timeMetric = {incoming: "incoming", ai_only: "ai_only", handoff: "handoff", rejected: "rejected", effective_human: "effective_human", low_rating: "low_rating_rounds"};
      const appliedChannel = model.applied?.get("channel_key") || null;
      const timeSpec = (key, label, window, count) => {
        const channel = key.startsWith("channel:") ? key.slice(8) : appliedChannel;
        return spec(r, key.startsWith("channel:") ? "incoming" : timeMetric[key], label, {...window, count, channel});
      };
      const timeCell = (row, key, label, window) => drill(number(getValue(row, key)), getValue(row, key), timeSpec(key, label, window, getValue(row, key)));
      cards(metrics.map(([key, title]) => [title, r.totals[key], key, timeSpec(key, title, {}, r.totals[key])]));
      $("reportTable").innerHTML = `<table><thead><tr><th>时间区间</th>${fields.map(s => `<th>${helpLabel(s[1], s[0])}</th>`).join("")}</tr></thead><tbody>${r.rows.map((row, i) => `<tr${r.buckets[i].future ? ' class="report-future"' : ""}><td>${esc(r.buckets[i].label)}${r.buckets[i].partial ? " · 进行中" : ""}</td>${fields.map(s => `<td>${r.buckets[i].future ? "—" : timeCell(row, s[0], `${r.buckets[i].label} · ${s[1]}`, bucketWindow(r, i))}</td>`).join("")}</tr>`).join("")}</tbody><tfoot><tr><th>合计</th>${fields.map(s => `<td>${timeCell({...r.totals, channels: r.channels}, s[0], s[1], {})}</td>`).join("")}</tr></tfoot></table>`;
      $("reportNotice").innerHTML = `<p>所有指标按咨询轮次开始时间统计，同一客户再次咨询可形成新轮次。正在进行的轮次，其接待分类和评分可能继续变化。</p>${r.processing.pending_jobs ? `<p>还有 ${number(r.processing.pending_jobs)} 条采集任务待处理，当前报表可能尚未包含最新数据。</p>` : ""}`;
      $("reportRules").innerHTML = "<p>自定义时间段最多 366 天（含起止日期），按天汇总；页面查询、用户溯源与 Excel 导出使用同一上限。</p><p>进客量：新咨询轮次数。纯AI接待量：尚未转人工且没有公开人工回复的AI轮次数，包含进行中的轮次。转人工量：已请求或进入人工接待的轮次数，包含继承人工接待状态的轮次和繁忙拒接轮次。有效人工量：至少有一条公开人工服务回复的轮次数，不含私密备注、系统消息和评价卡。</p><p>转人工拒接量：出现系统“当前人工坐席繁忙”提示的轮次数，同轮多次提示计一次；后续成功接待仍保留此次拒接记录。没有回复、等待中、静音提示均不推定为拒接。现有采集未提供独立的坐席主动拒接事件。</p><p>低评分数：当前已关联本轮的1至3分评价轮次数；未评价和未关联评价不计入。各渠道进客量相加等于进客总量，无法识别渠道的记录归入未映射渠道。</p>";
    }
    function renderAgents() {
      const r = model.result, summary = r.presence_summary;
      drills.length = 0;
      cards([["参与统计坐席", r.total, "seats"], ["回复询盘（按坐席去重）", r.total_inquiries, "total_inquiries", spec(r, "inquiries", "回复询盘", {count: r.total_inquiries, seat: {agent_id: "any"}})], ["公开回复消息数", r.total_replies, "replies", spec(r, "replies", "有公开回复的询盘", {count: r.total_replies, seat: {agent_id: "any"}})],
        ["无ICS分配轮次", r.unassigned_inquiries, "unassigned_inquiries", spec(r, "unassigned_inquiries", "无ICS分配轮次", {count: r.unassigned_inquiries})],
        ["协助轮次（不计考核）", r.assisted_inquiries, "assisted_inquiries", spec(r, "assisted_inquiries", "协助轮次", {count: r.assisted_inquiries})],
        ["在线人数峰值", summary.peak_online, "peak_online"], ["有采样的分钟数", summary.observed_minutes, "observed_minutes"], ["状态采样覆盖率（%）", summary.expected_samples ? Math.min(100, summary.samples / summary.expected_samples * 100) : null, "coverage"]]);
      const seatCell = (a, metric, label, text, value) => drill(text, value, spec(r, metric, label, {seat: seatOf(a), count: value}));
      const seatName = a => `<span class="report-seat-name">${esc(a.agent_name || `坐席 #${a.agent_id}`)}</span><span class="report-seat-id">${esc(a.source_key)} / ${esc(a.account_id)} / #${esc(a.agent_id)}</span>`;
      const qualityFields = [["首响时长", "first_response"], ["平均响应时长", "average_response"], ["对话量", "dialogues"], ["高质量对话量", "high_quality_dialogues"], ["发配量", "assigned_inquiries"], ["回复询盘总量", "inquiries"], ["平均评分", "average_rating"], ["低分数（低于4分）", "low_rating"], ["流单数（转人工后未回）", "lost_inquiries"], ["咨询状态总时长", "consultation_seconds"]];
      $("reportTable").innerHTML = r.agents.length ? `<table><thead><tr><th>坐席</th>${qualityFields.map(([label, key]) => `<th>${helpLabel(label, key)}</th>`).join("")}</tr></thead><tbody>${r.agents.map(a => `<tr><td>${seatName(a)}</td><td title="有效首响对话 ${number(a.first_response_samples)} 通">${seatCell(a, "first_response_count", "有首响的对话", duration(a.first_response_seconds), a.first_response_samples)}</td><td title="有效响应对话 ${number(a.response_rounds)} 通，已完成响应 ${number(a.response_samples)} 次；按响应次数加权汇总">${seatCell(a, "average_response_count", "有平均响应的对话", duration(a.average_response_seconds), a.response_rounds)}</td><td>${seatCell(a, "dialogues", "对话量", number(a.dialogues), a.dialogues)}</td><td>${seatCell(a, "high_quality_dialogues", "高质量对话量", number(a.high_quality_dialogues), a.high_quality_dialogues)}</td><td>${seatCell(a, "assigned_inquiries", "发配量", number(a.assigned_inquiries), a.assigned_inquiries)}</td><td>${seatCell(a, "inquiries", "回复询盘", number(a.inquiries), a.inquiries)}</td><td title="已评分询盘 ${number(a.rated_inquiries)} 条">${seatCell(a, "rated_inquiries", "已评分询盘", number(a.average_rating), a.rated_inquiries)}</td><td>${seatCell(a, "low_rating", "低分（低于4分）", number(a.low_rating), a.low_rating)}</td><td>${seatCell(a, "lost_inquiries", "流单", number(a.lost_inquiries), a.lost_inquiries)}</td><td>${seatCell(a, "consultation_seconds", "计入咨询时长的轮次", duration(a.consultation_seconds), a.consultation_seconds)}</td></tr>`).join("")}</tbody></table>` : '<div class="report-empty">当前时间范围没有坐席记录、公开人工回复、成功分配或已归属流单。</div>';
      $("reportTableSubtitle").innerHTML = `时长按转人工时间筛选并按对话平均；其余询盘按实际回复时间；与下表共用分页；点击数字可在对话归档中查看对应轮次${r.unassigned_lost_inquiries ? `。另有 ${drill(number(r.unassigned_lost_inquiries), r.unassigned_lost_inquiries, spec(r, "lost_inquiries", "无ICS分配的流单", {seat: {agent_id: "none"}, count: r.unassigned_lost_inquiries}))} 条无 ICS 分配坐席的流单` : ""}`;
      $("reportAgents").innerHTML = r.agents.length ? `<table><thead><tr><th>坐席</th>${[["回复询盘合计", "inquiries"], ["公开回复数", "replies"], ["在线分钟", "online_minutes"], ["忙碌分钟", "busy_minutes"]].map(([label, key]) => `<th>${helpLabel(label, key)}</th>`).join("")}${r.buckets.map(b => `<th>${helpLabel(b.label, "bucket")}</th>`).join("")}</tr></thead><tbody>${r.agents.map(a => {
        const sampled = a.online_minutes + a.busy_minutes + a.offline_minutes + a.unknown_minutes;
        return `<tr><td>${seatName(a)}</td><td>${seatCell(a, "inquiries", "回复询盘", number(a.inquiries), a.inquiries)}</td><td>${seatCell(a, "replies", "有公开回复的询盘", number(a.replies), a.replies)}</td><td>${sampled ? number(a.online_minutes) : "—"}</td><td>${sampled ? number(a.busy_minutes) : "—"}</td>${r.buckets.map((b, i) => `<td>${b.future ? "—" : drill(number(a.buckets[b.key] || 0), a.buckets[b.key] || 0, spec(r, "inquiries", `${b.label} · 回复询盘`, {...bucketWindow(r, i), seat: seatOf(a), count: a.buckets[b.key] || 0}))}</td>`).join("")}</tr>`;
      }).join("")}</tbody></table>` : '<div class="report-empty">当前时间范围没有坐席记录或公开人工回复。</div>';
      const pages = Math.max(1, Math.ceil(r.total / r.page_size));
      $("reportPageInfo").textContent = `第 ${r.page} / ${pages} 页 · 共 ${number(r.total)} 位坐席 · 每页 ${r.page_size} 位`;
      $("reportPrev").disabled = r.page <= 1; $("reportNext").disabled = r.page >= pages;
      $("reportFirst").disabled = r.page <= 1; $("reportLast").disabled = r.page >= pages;
      $("reportJump").disabled = pages <= 1; $("reportJumpInput").disabled = model.loading || pages <= 1;
      $("reportNotice").innerHTML = `<p>可用客服量以采样时状态为“在线”的人数展示。按日展示每分钟人数；按月、年展示对应日、月内有采样分钟的平均在线人数，自定义时间段按天展示平均在线人数。</p><p>状态历史保留45天，当前可用起点为 ${esc(new Date(r.retention_cutoff_at).toLocaleString("zh-CN", {timeZone: "Asia/Shanghai", hour12: false}))}。未监控、缺采样和已过期不补为离线或零；覆盖不足时，观测人数可能偏低。询盘归档不受45天保留期影响。</p>`;
      $("reportRules").innerHTML = `<p>自定义时间段最多 366 天（含起止日期），按天汇总；页面查询、用户溯源与 Excel 导出使用同一上限。</p><p>在线人数根据每分钟成功采样中的online状态计数，不表示当时仍有剩余接待容量。未收到状态样本的分钟保持空缺，当天尚未结束的分钟不参与统计。</p><p>首响时长：${esc(agentHelp.first_response)}</p><p>平均响应时长：${esc(agentHelp.average_response)} 有同事协助回复的对话整轮不计；转人工或有效消息时间不明的轮次排除时长计算。</p><p>回复询盘数按区间内实际公开回复的坐席和咨询轮次去重。跨时段回复同一轮时，合计可能小于各时段相加。平均评分与低分数基于这些已回复询盘的当前关联评价，同一坐席同一轮只计一次，未评分不参与平均；有同事协助回复的轮次整轮不计入任何坐席，仅保留发配量；该轮解决后的下一轮照常统计。首响和响应使用转人工时间范围，时长样本数可以与回复询盘总量不同。</p><p>流单按区间内转人工时间统计，截至查询时没有任何公开人工回复（含尚在等待的进行中询盘），只归入已确认的ICS责任坐席；后续已由其他坐席回复的轮次不计流单，无法确认责任坐席的数量单独提示。转人工时间优先使用请求时间，再使用已确认的ICS分配时间；继承人工接待的轮次使用新轮开始时间。</p><p>坐席名单包含监控坐席、实际回复人、所选转人工对话中存在后续回复的坐席和存在已归属流单的责任坐席。两张坐席明细使用同一分页，坐席ID相同但来源或账号不同的记录独立统计。状态采样时长仅覆盖保留期内可观察到的分钟，回复质量不受45天状态保留期影响。</p>`;
    }
    function renderQuality() {
      const r = model.result;
      drills.length = 0;
      const aiMetric = {total_rounds: "incoming", handoff: "handoff", ai_only: "ai_replied_only", handoff_failed: "rejected", failed_rounds: "ai_failed", failure_rate: "ai_failed"};
      cards([["人工回复询盘总量", r.total_inquiries, "total_inquiries", spec(r, "inquiries", "回复询盘", {count: r.total_inquiries, seat: {agent_id: "any"}})], ["人工质检不合格量", r.total_failed_inquiries, "total_failed_inquiries", spec(r, "failed_inquiries", "人工质检不合格", {count: r.total_failed_inquiries, seat: {agent_id: "any"}})], ["人工不合格率（%）", r.human_failure_rate, "human_failure_rate"],
        ["所有对话轮数", r.ai.total_rounds, "ai_total_rounds", spec(r, "incoming", "所有对话轮", {count: r.ai.total_rounds})], ["AI质检不合格量", r.ai.failed_rounds, "ai_failed_rounds", spec(r, "ai_failed", "AI质检不合格", {count: r.ai.failed_rounds})], ["AI不合格率（%）", r.ai.failure_rate, "ai_failure_rate"]]);
      const qualityMetric = {assigned_inquiries: "assigned_inquiries", dialogues: "dialogues", high_quality_dialogues: "high_quality_dialogues", inquiries: "inquiries", failed_inquiries: "failed_inquiries", failure_rate: "failed_inquiries"};
      const fields = [["assigned_inquiries", "发配量"], ["dialogues", "对话量"], ["high_quality_dialogues", "高质量对话量"], ["inquiries", "回复询盘总量"], ["failed_inquiries", "质检不合格量"], ["failure_rate", "不合格率"]];
      const seatName = a => `<span class="report-seat-name">${esc(a.agent_name || `坐席 #${a.agent_id}`)}</span><span class="report-seat-id">${esc(a.source_key)} / ${esc(a.account_id)} / #${esc(a.agent_id)}</span>`;
      $("reportTable").innerHTML = r.agents.length ? `<table><thead><tr><th>坐席</th>${fields.map(([key, label]) => `<th>${helpLabel(label, key === "failure_rate" ? "human_failure_rate" : key)}</th>`).join("")}</tr></thead><tbody>${r.agents.map(a => `<tr><td>${seatName(a)}</td>${fields.map(([key, label]) => {
        const count = key === "failure_rate" ? a.failed_inquiries : a[key];
        return `<td>${drill(key === "failure_rate" ? percent(a[key]) : number(a[key]), count, spec(r, qualityMetric[key], label, {seat: seatOf(a), count}))}</td>`;
      }).join("")}</tr>`).join("")}</tbody></table>` : '<div class="report-empty">当前时间范围没有坐席记录或询盘。</div>';
      const selected = periodLabel(r);
      $("reportAgents").innerHTML = `<table><thead><tr><th>统计周期</th>${aiFields.map(([key, label]) => `<th>${helpLabel(label, `ai_${key}`)}</th>`).join("")}</tr></thead><tbody><tr><td>${esc(selected)}</td>${aiFields.map(([key, label]) => {
        const count = key === "failure_rate" ? r.ai.failed_rounds : r.ai[key];
        return `<td>${drill(key === "failure_rate" ? percent(r.ai[key]) : number(r.ai[key]), count, spec(r, aiMetric[key], label, {count}))}</td>`;
      }).join("")}</tr></tbody></table>`;
      const pages = Math.max(1, Math.ceil(r.total / r.page_size));
      $("reportQualityPageInfo").textContent = `第 ${r.page} / ${pages} 页 · 共 ${number(r.total)} 位坐席 · 每页 ${r.page_size} 位`;
      $("reportQualityPrev").disabled = r.page <= 1; $("reportQualityNext").disabled = r.page >= pages;
      $("reportQualityFirst").disabled = r.page <= 1; $("reportQualityLast").disabled = r.page >= pages;
      $("reportQualityJump").disabled = pages <= 1; $("reportQualityJumpInput").disabled = model.loading || pages <= 1;
      $("reportNotice").innerHTML = "<p>人工与AI质检结论独立，使用当前最新保存结果；复核后历史报表随之更新。待质检轮次保留在分母中，不计为不合格；分母为0时不合格率显示‘—’。</p>";
      $("reportRules").innerHTML = `<p>自定义时间段最多 366 天（含起止日期），按天汇总；页面查询、用户溯源与 Excel 导出使用同一上限。</p><p>人工表沿用坐席细化的发配量、对话量、高质量对话量和回复询盘总量。发配量按成功分配时间统计，其余三项按区间内实际回复询盘统计，每轮只归属主责坐席；有主责以外坐席公开回复（如交班时同事协助）的轮次整轮不计入任何坐席，仅保留发配量。</p><p>人工质检不合格量：${esc(qualityHelp.failed_inquiries)}</p><p>不合格率：${esc(qualityHelp.human_failure_rate)}</p>${aiFields.map(([key, label]) => `<p>${esc(label)}：${esc(qualityHelp[`ai_${key}`])}</p>`).join("")}`;
    }
    const beijingSeconds = value => value ? new Date(new Date(value).getTime() + 8 * 3600 * 1000).toISOString().slice(0, 19).replace("T", " ") : "—";
    const blacklistTag = status => status === "active" ? '<span class="report-tag red">拉黑中</span>' : status === "past" ? '<span class="report-tag">曾被拉黑</span>' : "";
    const userKey = u => `${u.source_key}|${u.account_id}|${u.contact_id}`;
    const userOf = u => ({source_key: u.source_key, account_id: u.account_id, contact_id: u.contact_id, customer_id: u.customer_id, display_name: u.display_name});
    const userRange = r => `${r.start_label} 至 ${r.end_label}`;
    function renderUsers() {
      const r = model.result;
      drills.length = 0;
      const channel = model.applied?.get("channel_key") || null;
      cards([["成功转人工总次数", r.total_assignments, "total_assignments"], ["有成功转人工的用户数", r.users_total, "users_total"],
        ["前 100 名次数合计", r.ranked_assignments, "ranked_assignments"], ["占总次数（%）", r.ranked_share, "ranked_share"],
        ...(r.unidentified_assignments ? [["无法识别联系人（不参与排行）", r.unidentified_assignments, "unidentified_assignments"]] : [])]);
      const fields = [["名次", "rank"], ["用户", "user"], ["成功转人工次数", "assignments"], ["近24小时计数", "daily_count"], ["当前周次计数", "weekly_count"], ["区间内首次", "first_assigned_at"], ["最近一次", "last_assigned_at"], ["黑名单", "blacklist"], ["溯源明细", "trace"]];
      const row = u => {
        const key = userKey(u), trace = model.traces.get(key);
        const name = `<span class="report-seat-name">${esc(u.display_name || `联系人 #${u.contact_id}`)}</span><span class="report-seat-id">联系人 ${esc(u.contact_id)} · ${esc(u.channels.map(channelName).join("、"))}</span>`;
        const count = drill(number(u.assignments), u.assignments, spec(r, "assigned_inquiries", "成功转人工", {user: userOf(u), channel, count: u.assignments, period: userRange(r)}));
        const button = `<button class="button tertiary report-trace" type="button" data-trace="${esc(key)}" aria-expanded="${trace ? "true" : "false"}">${trace ? "收起" : "溯源"}</button>`;
        return `<tr><td>${number(u.rank)}</td><td class="report-user">${name}</td><td>${count}</td><td>${number(u.daily_count)}</td><td>${number(u.weekly_count)}</td><td>${esc(beijingSeconds(u.first_assigned_at))}</td><td>${esc(beijingSeconds(u.last_assigned_at))}</td><td>${blacklistTag(u.blacklist_status)}</td><td>${button}</td></tr>${trace ? `<tr class="report-trace-row"><td colspan="${fields.length}">${traceTable(u, trace)}</td></tr>` : ""}`;
      };
      $("reportTable").innerHTML = r.users.length ? `<table class="report-user-table"><thead><tr>${fields.map(([label, key]) => `<th>${helpLabel(label, key)}</th>`).join("")}</tr></thead><tbody>${r.users.map(row).join("")}</tbody></table>`
        : `<div class="report-empty">${r.before_earliest ? `该时间之前无成功转人工记录${r.earliest_assigned_at ? `（最早一条 ICS 成功分配记录：${esc(beijingSeconds(r.earliest_assigned_at))}）` : "（尚无任何 ICS 成功分配记录，请确认已开启分配同步 ARCHIVE_ASSIGNMENT_SYNC_ENABLED）"}` : "所选范围内没有成功转人工的用户。"}</div>`;
      const pages = Math.max(1, Math.ceil(r.total / r.page_size));
      $("reportQualityPageInfo").textContent = `第 ${r.page} / ${pages} 页 · 前 ${number(r.total)} 名 · 每页 ${r.page_size} 名`;
      $("reportQualityPrev").disabled = r.page <= 1; $("reportQualityNext").disabled = r.page >= pages;
      $("reportQualityFirst").disabled = r.page <= 1; $("reportQualityLast").disabled = r.page >= pages;
      $("reportQualityJump").disabled = pages <= 1; $("reportQualityJumpInput").disabled = model.loading || pages <= 1;
      $("reportNotice").innerHTML = `<p>统计区间：${esc(userRange(r))}（北京时间，含起止）。</p>${r.before_earliest && r.users.length ? `<p>所选区间早于最早一条 ICS 成功分配记录（${esc(beijingSeconds(r.earliest_assigned_at))}），该时间之前无成功转人工记录。</p>` : ""}`;
      if (r.queue_counts?.available) $("reportNotice").innerHTML += `<p>排队计数截至 ${esc(beijingSeconds(r.queue_counts.as_of))}；当前周次从 ${esc(beijingSeconds(r.queue_counts.week_start))} 起算（北京时间）。</p>`;
      else $("reportNotice").innerHTML += '<p>排队计数暂不可用，近24小时计数和当前周次计数显示为“—”。</p>';
      $("reportRules").innerHTML = `<p>自定义时间段最多 366 天（含起止日期），按天汇总；页面查询、用户溯源与 Excel 导出使用同一上限。</p><p>成功转人工次数：${esc(userHelp.assignments)}</p><p>成功转人工总次数：${esc(userHelp.total_assignments)}</p><p>排序：${esc(userHelp.rank)}</p><p>近24小时计数：${esc(userHelp.daily_count)}</p><p>当前周次计数：${esc(userHelp.weekly_count)}</p><p>数据来源：排行、成功转人工次数和溯源来自归档中已同步的 ICS 成功分配记录；近24小时计数和当前周次计数直接读取排队计数。分配同步或排队计数启用之前的历史分别不追补。</p>`;
    }
    function traceTable(u, trace) {
      if (trace === "loading") return '<div class="report-empty">正在读取明细…</div>';
      if (trace.error) return `<div class="report-empty">${esc(trace.error)}</div>`;
      const r = model.result, channel = model.applied?.get("channel_key") || null;
      const rows = trace.items.map((d, i) => `<tr><td>${i + 1}</td><td>${esc(beijingSeconds(d.assigned_at))}</td><td>${drill(`会话 ${esc(d.conversation_id)} · 第 ${esc(d.round_no)} 轮`, 1, spec(r, "assigned_inquiries", `第 ${i + 1} 次成功转人工`, {user: userOf(u), round: {conversation_id: d.conversation_id, round_no: d.round_no, round_id: d.round_id}, channel, count: 1, period: userRange(r)}))}</td><td>${esc(d.agent_name || `坐席 #${d.agent_id}`)}<span class="report-seat-id">#${esc(d.agent_id)}</span></td><td>${esc(channelName(d.channel_key || "unmapped"))}</td></tr>`).join("");
      const mismatch = trace.items.length !== u.assignments ? `<p class="report-trace-note">明细 ${trace.items.length} 条与排行 ${u.assignments} 次不一致：报表生成后分配记录有变化，请重新查询。</p>` : "";
      return `${mismatch}<table class="report-trace-table"><thead><tr><th>#</th><th>成功分配时间</th><th>会话 / 轮次</th><th>分配坐席</th><th>渠道</th></tr></thead><tbody>${rows}</tbody></table>`;
    }
    async function toggleTrace(event) {
      const button = event.target.closest?.("[data-trace]");
      const r = model.result;
      if (!button || !r || model.kind !== "users") return;
      const key = button.dataset.trace, user = r.users.find(u => userKey(u) === key);
      if (!user) return;
      if (model.traces.has(key)) {model.traces.delete(key); renderUsers(); return;}
      model.traces.set(key, "loading"); renderUsers();
      const params = new URLSearchParams(model.applied);
      params.delete("page"); params.delete("page_size");
      params.set("source_key", user.source_key); params.set("account_id", user.account_id); params.set("contact_id", user.contact_id);
      const epoch = model.epoch, token = getToken();
      try {
        const detail = await api(`/admin/api/statistics/users/detail?${params}`, {signal: model.controller?.signal});
        if (epoch !== model.epoch || token !== getToken() || !isActive() || !model.traces.has(key)) return;
        model.traces.set(key, detail);
      } catch (error) {
        if (error.name === "AbortError" || epoch !== model.epoch) return;
        model.traces.set(key, {error: error.message || "明细读取失败，请稍后重试。"});
      }
      if (model.result === r) renderUsers();
    }
    function periodLabel(r) {
      const mode = r.mode || model.mode;
      if (r.refined) return `${r.start_label} 至 ${r.end_label}`;
      return mode === "custom" ? `${r.start_date} 至 ${r.end_date}` : mode === "day" ? r.date : mode === "month" ? r.date.slice(0, 7) : r.date.slice(0, 4);
    }
    function render() {
      const r = model.result;
      if (!r) return;
      const selected = periodLabel(r);
      $("reportStatus").textContent = `${model.kind === "users" ? `${r.start_label} 至 ${r.end_label}` : selected} · ${{time: "时间统计", agents: "坐席细化", quality: "质检统计", users: "用户统计"}[model.kind]} · 更新于 ${new Date(r.generated_at).toLocaleTimeString("zh-CN", {timeZone: "Asia/Shanghai", hour12: false})}（北京时间）`;
      if (model.kind === "quality") {renderQuality(); return;}
      if (model.kind === "users") {renderUsers(); return;}
      model.kind === "time" ? renderTime() : renderAgents();
      if (model.kind === "agents") $("reportRules").innerHTML += `<p>咨询状态总时长：${esc(agentHelp.consultation_seconds)} 跨日的咨询时段按筛选窗口截取。客户隔很久才再次发言、坐席未结束会话时，空档不再计入忙碌时长。</p><p>对话量：${esc(agentHelp.dialogues)}</p><p>高质量对话量：${esc(agentHelp.high_quality_dialogues)}</p><p>发配量：${esc(agentHelp.assigned_inquiries)}</p>`;
      renderChart();
    }
    return {start, stop, refresh: load};
  }
  // 字段说明同时写入导出 Excel 的“口径说明”表（statistics/glossary.py），测试比对两边一致。
  window.ICSReports = {create, help: {time: timeHelp, agents: agentHelp, quality: qualityHelp, users: userHelp}};
})();
