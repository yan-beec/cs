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
    low_rating: "当前关联本轮的评分为1至3分的轮次数；未评分及未关联的评价不计入。",
    channel: "该渠道的新咨询轮次数，按轮次开始时间归入时段；无法识别渠道的记录归入‘未映射渠道’。"
  };
  const agentHelp = {
    seats: "所选周期内的监控坐席、状态记录、实际回复人、已确认分配询盘、存在响应时长、咨询时长或已归属流单的坐席；来源、账号和坐席ID共同确定身份。",
    inquiries: "区间内有实际公开人工回复的询盘轮次数，同一坐席同一轮只计一次；不同坐席参与同一轮分别计入各自数据。",
    total_inquiries: "各坐席在所选周期内回复的询盘量之和；同一轮多人回复时，每位回复坐席各计一次。",
    replies: "所选区间内的公开人工回复逻辑消息数，编辑版本不重复计数；排除私密备注、系统消息、机器人回复和评价卡。",
    peak_online: "所选周期内，单个有采样分钟观测到的在线坐席人数最大值；仅覆盖保留期内的数据。",
    observed_minutes: "所选周期内至少有一条成功状态采样的分钟数；不包含缺采样、过期和未来分钟。",
    coverage: "实际坐席状态样本数÷按历史监控成员计算的应采样数×100%，最高显示100%；只统计状态保留期内的已完成分钟。",
    online: "按日展示每分钟采样中状态为online的坐席人数；按月、年及自定义时间段展示对应时段内有采样分钟的平均在线人数。缺采样保留空缺，在线不代表仍有接待容量。",
    first_response: "按转人工发生时间筛选对话，从转人工后用户第一条有效消息到随后首次有效人工回复计时，归入该回复坐席，再对有效对话求平均。触发转人工的指令不计；转人工后未发言、未回复或时间不明时不计。使用截至查询时已完成的回复，可跨筛选区间结束时间。",
    average_response: "按转人工发生时间筛选对话。首段响应使用首响时长，后续每段从用户连续发言的最后一条有效消息到下一条有效人工回复计时；先算每通对话中该坐席的平均响应，再对各通对话等权平均。未回复尾段、连续人工回复、私密消息、评价卡、空白和已删除消息不计；回复可跨筛选区间结束时间。",
    average_rating: "该坐席在区间内已回复询盘当前关联的1至5分评价平均值，同一轮只计一次，未评分不作为0分。",
    low_rating: "该坐席在区间内已回复询盘当前关联评分为1至3分的轮次数，同一坐席同一轮只计一次。",
    lost_inquiries: "转人工发生在所选区间、截至查询时仍无任何公开人工回复的轮次数，包含仍在等待的询盘；只归属已确认的ICS责任坐席。",
    consultation_seconds: "每轮取该坐席首次至最后一次公开回复的时间区间，再合并同一坐席所有重叠时段，只累计落在筛选区间内的部分。单次回复计0秒，不延长到当前时间；缺少回复时间的轮次排除。",
    dialogues: "该坐席在区间内已回复的询盘中，访客转人工后发过有效消息的轮次数，同轮去重。有效消息为非空文字或附件，排除触发转人工的消息、私密消息、评价卡和已删除消息；转接时间不明时不计。访客消息查阅完整轮次至查询时。",
    high_quality_dialogues: "上述对话量中，访客转人工后的有效消息严格超过5条（至少6条）的轮次数；按同一有效消息规则及完整轮次统计，同一坐席同一轮只计一次。",
    assigned_inquiries: "按成功分配时间统计所选区间内，ICS已确认分配给该坐席的询盘轮次数，同一轮只计一次；未回复也计入。仅请求转人工或繁忙拒接但未成功分配、分配归属冲突、缺少实际分配时间的记录不计入，不按当前分配人或实际回复人推测。",
    online_minutes: "在状态保留期内，该坐席被采样为online的分钟数；没有任何状态样本时显示‘—’，不补算为0或离线。",
    busy_minutes: "在状态保留期内，该坐席被采样为busy的分钟数；没有任何状态样本时显示‘—’。",
    bucket: "该时段内实际公开回复过的询盘数，同一坐席同一轮去重；同轮跨时段回复会在各时段各计一次，整个周期的合计再次去重。"
  };
  const qualityHelp = {
    ...agentHelp,
    failed_inquiries: "该坐席在筛选区间内已回复的询盘中，当前人工质检结论为‘不合格’的轮次数，同一坐席同一轮只计一次；待质检及合格不计入。",
    total_failed_inquiries: "汇总全部坐席已回复询盘中，当前人工质检结论为‘不合格’的数量，不受坐席分页影响；同一轮多人回复时，各坐席分别计一次。",
    human_failure_rate: "人工质检不合格量÷回复询盘总量×100%。分母包含待质检轮次；没有回复询盘时显示‘—’。概况按所有坐席汇总计算，多人回复同一轮各计一次。",
    ai_total_rounds: "按轮次开始时间，统计所选区间内全部已采集、已解析的对话轮次数，包含已转人工及待质检轮次，同一客户再次咨询可形成新轮。",
    ai_handoff: "所有对话轮中，已请求或进入人工接待、出现繁忙拒接提示或已有公开人工回复的轮次数，包含继承人工接管状态的新轮。",
    ai_ai_only: "所有对话轮中，已有公开有效AI回复且仍为纯AI接待、没有转人工或公开人工回复的轮次数；一轮多条AI消息只计一次。",
    ai_handoff_failed: "出现系统‘当前人工坐席繁忙’拒接提示的对话轮数，同轮多次拒接只计一次；后来接通仍保留此次失败记录。等待中或未回复不推定为失败。",
    ai_failed_rounds: "筛选区间内全部对话轮中，当前AI质检结论为‘不合格’的轮次数，包含后来已转人工的轮次；人工质检结论不影响此项。",
    ai_failure_rate: "AI质检不合格量÷所有对话轮数×100%，分母包含待质检轮次；没有对话轮时显示‘—’。两类质检均读取当前最新结论，复核会更新历史统计。"
  };
  const aiFields = [["total_rounds", "所有对话轮数"], ["handoff", "转人工量"], ["ai_only", "纯AI回复量"], ["handoff_failed", "转人工失败量"], ["failed_rounds", "不合格量"], ["failure_rate", "不合格率"]];
  const beijingDay = () => new Intl.DateTimeFormat("sv-SE", {timeZone: "Asia/Shanghai", year: "numeric", month: "2-digit", day: "2-digit"}).format(new Date());
  const number = value => value === null || value === undefined ? "—" : Number(value).toLocaleString("zh-CN", {maximumFractionDigits: 2});
  const percent = value => value === null || value === undefined ? "—" : `${number(value)}%`;
  const duration = value => {
    if (value === null || value === undefined) return "—";
    const seconds = Math.round(Number(value) * 100) / 100;
    const hours = Math.floor(seconds / 3600), minutes = Math.floor(seconds % 3600 / 60);
    return `${hours ? `${number(hours)}小时` : ""}${minutes ? `${minutes}分` : ""}${number(Number((seconds % 60).toFixed(2)))}秒`;
  };

  function create({api, escapeHtml: esc, getToken, isActive, download}) {
    const $ = id => document.getElementById(id);
    const helpLabel = (label, key) => {
      const help = model.kind === "quality" ? qualityHelp[key] : model.kind === "time" ? timeHelp[key.startsWith("channel:") ? "channel" : key] : agentHelp[key];
      return `${esc(label)}<span class="report-help" tabindex="0" role="img" aria-label="${esc(`${label}：${help}`)}" title="${esc(help)}">?</span>`;
    };
    const today = beijingDay();
    const model = {kind: "time", mode: "day", date: today, startDate: today, endDate: today, page: 1, controller: null,
      epoch: 0, result: null, applied: null, exportController: null, optionsToken: null, options: {channels: []}, hidden: new Set(), mounted: false};

    const mount = () => {
      if (model.mounted) return;
      model.mounted = true;
      $("statisticsPage").innerHTML = `
        <aside class="panel report-sidebar" aria-label="筛选与统计概况"><form id="reportFilters" class="report-filters">
          <h3 class="report-sidebar-title">统计筛选</h3>
          <label class="report-field">统计周期<select id="reportMode"><option value="day">按日 · 每小时</option><option value="month">按月 · 每日</option><option value="year">按年 · 每月</option><option value="custom">自定义 · 每日</option></select></label>
          <label id="reportDateField" class="report-field"><span id="reportDateLabel">选择日期</span><input id="reportDate" type="date" min="2000-01-01" max="2099-12-31" required aria-label="统计日期"></label>
          <label id="reportStartField" class="report-field hidden">起始日期<input id="reportStartDate" type="date" min="2000-01-01" max="2099-12-31" required disabled aria-label="统计起始日期"></label>
          <label id="reportEndField" class="report-field hidden">结束日期<input id="reportEndDate" type="date" min="2000-01-01" max="2099-12-31" required disabled aria-label="统计结束日期"></label>
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
          <div id="reportQualityPagination" class="report-pagination hidden"><span id="reportQualityPageInfo"></span><div><button id="reportQualityPrev" type="button" class="button secondary">上一页</button><button id="reportQualityNext" type="button" class="button secondary">下一页</button></div></div>
        </section>
        <section id="reportAgentPanel" class="panel report-data-panel hidden"><div class="panel-header"><div class="panel-heading"><h3 id="reportAgentTitle">坐席回复询盘</h3><p id="reportAgentSubtitle">按实际回复时间归入时段；同一坐席在同一时段回复同一轮询盘仅计一次</p></div></div>
          <div id="reportAgents" class="table-wrap report-table"></div><div id="reportPagination" class="report-pagination"><span id="reportPageInfo"></span><div><button id="reportPrev" type="button" class="button secondary">上一页</button><button id="reportNext" type="button" class="button secondary">下一页</button></div></div>
        </section></div></div>`;
      $("reportMode").value = model.mode;
      updateDateInput();
      $("reportFilters").addEventListener("submit", event => {event.preventDefault(); model.page = 1; load();});
      $("reportReset").addEventListener("click", reset);
      $("reportExport").addEventListener("click", exportReport);
      $("reportMode").addEventListener("change", () => {
        model.mode = $("reportMode").value; model.date = beijingDay(); updateDateInput(); model.page = 1; load();
      });
      for (const id of ["reportDate", "reportStartDate", "reportEndDate", "reportChannel"]) $(id).addEventListener("change", () => {model.page = 1; load();});
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
      $("reportChart").addEventListener("focusin", event => showPoint(event));
      for (const prefix of ["report", "reportQuality"]) {
        $(`${prefix}Prev`).addEventListener("click", () => {if (model.page > 1) {model.page--; load();}});
        $(`${prefix}Next`).addEventListener("click", () => {if (model.result && model.page * model.result.page_size < model.result.total) {model.page++; load();}});
      }
      let resizeTimer;
      window.addEventListener("resize", () => {
        clearTimeout(resizeTimer);
        resizeTimer = setTimeout(() => {if (model.result && model.kind !== "quality" && isActive()) renderChart();}, 100);
      });
    };

    function reset() {
      model.mode = "day"; model.date = beijingDay(); model.page = 1; model.hidden.clear();
      model.startDate = model.endDate = model.date;
      $("reportMode").value = "day";
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
    }
    function updateDateInput() {
      const input = $("reportDate"), day = model.date;
      const custom = model.mode === "custom";
      $("reportDateField").classList.toggle("hidden", custom);
      $("reportStartField").classList.toggle("hidden", !custom);
      $("reportEndField").classList.toggle("hidden", !custom);
      input.disabled = custom;
      $("reportStartDate").disabled = $("reportEndDate").disabled = !custom;
      $("reportStartDate").value = model.startDate;
      $("reportEndDate").value = model.endDate;
      $("reportEndDate").setCustomValidity("");
      $("reportZone").textContent = custom ? "北京时间 · 包含起止日期全天；按日汇总" : "北京时间 · 时段开始含、结束不含";
      if (custom) return;
      input.type = model.mode === "day" ? "date" : model.mode === "month" ? "month" : "number";
      input.min = model.mode === "day" ? "2000-01-01" : model.mode === "month" ? "2000-01" : "2000";
      input.max = model.mode === "day" ? "2099-12-31" : model.mode === "month" ? "2099-12" : "2099";
      input.value = model.mode === "day" ? day : model.mode === "month" ? day.slice(0, 7) : day.slice(0, 4);
      $("reportDateLabel").textContent = model.mode === "day" ? "选择日期" : model.mode === "month" ? "选择月份" : "选择年份";
    }
    function stop(reset = false) {
      model.epoch++;
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
    function clear(message = "正在读取统计数据…") {
      for (const id of ["reportChart", "reportTable", "reportAgents"]) $(id).innerHTML = `<div class="report-empty">${esc(message)}</div>`;
      $("reportCards").innerHTML = ""; $("reportLegend").innerHTML = "";
      $("reportSelectAll").disabled = true;
      $("reportNotice").textContent = ""; $("reportPageInfo").textContent = "";
      $("reportPrev").disabled = $("reportNext").disabled = true;
      $("reportQualityPrev").disabled = $("reportQualityNext").disabled = true;
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
        if (model.result) render();
      } catch (_) { /* Report endpoint still provides readable data if the directory fails. */ }
    }
    async function load() {
      const startDate = $("reportStartDate").value, endDate = $("reportEndDate").value;
      $("reportEndDate").setCustomValidity(model.mode === "custom" && startDate && endDate && endDate < startDate ? "结束日期不能早于起始日期。" : "");
      if (!$("reportFilters").reportValidity()) {stop(); $("reportRefresh").disabled = false; return;}
      readDate(); stop(); model.result = null;
      const epoch = model.epoch, token = getToken(), controller = new AbortController();
      model.controller = controller;
      const params = new URLSearchParams({mode: model.mode});
      if (model.mode === "custom") {
        params.set("start_date", model.startDate); params.set("end_date", model.endDate);
      } else params.set("date", model.date);
      if (model.kind === "time" && $("reportChannel").value) params.set("channel_key", $("reportChannel").value);
      if (model.kind !== "time") {params.set("page", model.page); params.set("page_size", 20);}
      clear(); $("reportStatus").classList.remove("error"); $("reportStatus").textContent = "正在读取统计数据…";
      $("reportRefresh").disabled = true;
      const optionsTask = loadOptions(controller.signal, epoch, token);
      try {
        const result = await api(`/admin/api/statistics/${model.kind}?${params}`, {signal: controller.signal});
        if (epoch !== model.epoch || token !== getToken() || !isActive()) return;
        model.result = result; model.applied = new URLSearchParams(params); render();
        $("reportExport").disabled = false;
      } catch (error) {
        if (error.name === "AbortError" || epoch !== model.epoch || token !== getToken() || !isActive()) return;
        clear("暂无可显示的数据"); $("reportStatus").textContent = error.message; $("reportStatus").classList.add("error");
      } finally {
        if (epoch === model.epoch) $("reportRefresh").disabled = false;
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
        const title = {time: "时间统计", agents: "坐席细化", quality: "质检统计"}[kind];
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
      $("statisticsPage").classList.toggle("quality-report", kind === "quality");
      $("reportChartPanel").classList.toggle("hidden", kind === "quality");
      $("reportQualityPagination").classList.toggle("hidden", kind !== "quality");
      $("reportPagination").classList.toggle("hidden", kind !== "agents");
      $("reportChannelField").classList.toggle("hidden", kind !== "time");
      $("reportAgentPanel").classList.toggle("hidden", kind === "time");
      $("reportTables").classList.toggle("with-agents", kind === "agents");
      $("reportTables").classList.toggle("with-quality", kind === "quality");
      $("reportAgentTitle").textContent = kind === "quality" ? "AI质检报表" : "坐席回复询盘";
      $("reportAgentSubtitle").textContent = kind === "quality" ? "按轮次开始时间统计全部对话；使用最新AI质检结论" : "按实际回复时间归入时段；同一坐席在同一时段回复同一轮询盘仅计一次";
      $("reportChartTitle").textContent = kind === "time" ? "询盘趋势" : "可用客服量（在线）";
      $("reportChartSubtitle").textContent = kind === "time" ? "按咨询开始时间归入对应时段；数据随左侧渠道筛选更新" : "按分钟观测在线状态；缺采样处断开显示";
      $("reportTableTitle").textContent = kind === "quality" ? "人工质检表" : kind === "time" ? "时段数据" : "客服回复质量";
      $("reportTableSubtitle").textContent = kind === "quality" ? "按坐席统计已回复询盘，使用最新人工质检结论" : kind === "time" ? "折线图对应的完整指标明细" : "首响与响应按转人工时间筛选，其余询盘按实际回复时间；与下表共用分页";
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
      $("reportCards").innerHTML = items.map(([label, value, key]) => `<div class="report-card"><span class="report-card-label">${helpLabel(label, key)}</span><strong>${number(value)}</strong></div>`).join("");
    }
    function renderTime() {
      const r = model.result;
      const channels = new Set([...model.options.channels.map(c => c.key), ...Object.keys(r.channels || {})]);
      const fields = [...metrics, ...[...channels].map(key => [`channel:${key}`, `${channelName(key)}进客量`])];
      cards(metrics.map(([key, title]) => [title, r.totals[key], key]));
      $("reportTable").innerHTML = `<table><thead><tr><th>时间区间</th>${fields.map(s => `<th>${helpLabel(s[1], s[0])}</th>`).join("")}</tr></thead><tbody>${r.rows.map((row, i) => `<tr${r.buckets[i].future ? ' class="report-future"' : ""}><td>${esc(r.buckets[i].label)}${r.buckets[i].partial ? " · 进行中" : ""}</td>${fields.map(s => `<td>${r.buckets[i].future ? "—" : number(getValue(row, s[0]))}</td>`).join("")}</tr>`).join("")}</tbody><tfoot><tr><th>合计</th>${fields.map(s => `<td>${number(getValue({...r.totals, channels: r.channels}, s[0]))}</td>`).join("")}</tr></tfoot></table>`;
      $("reportNotice").innerHTML = `<p>所有指标按咨询轮次开始时间统计，同一客户再次咨询可形成新轮次。正在进行的轮次，其接待分类和评分可能继续变化。</p>${r.processing.pending_jobs ? `<p>还有 ${number(r.processing.pending_jobs)} 条采集任务待处理，当前报表可能尚未包含最新数据。</p>` : ""}`;
      $("reportRules").innerHTML = "<p>进客量：新咨询轮次数。纯AI接待量：尚未转人工且没有公开人工回复的AI轮次数，包含进行中的轮次。转人工量：已请求或进入人工接待的轮次数，包含继承人工接待状态的轮次和繁忙拒接轮次。有效人工量：至少有一条公开人工服务回复的轮次数，不含私密备注、系统消息和评价卡。</p><p>转人工拒接量：出现系统“当前人工坐席繁忙”提示的轮次数，同轮多次提示计一次；后续成功接待仍保留此次拒接记录。没有回复、等待中、静音提示均不推定为拒接。现有采集未提供独立的坐席主动拒接事件。</p><p>低评分数：当前已关联本轮的1至3分评价轮次数；未评价和未关联评价不计入。各渠道进客量相加等于进客总量，无法识别渠道的记录归入未映射渠道。</p>";
    }
    function renderAgents() {
      const r = model.result, summary = r.presence_summary;
      cards([["参与统计坐席", r.total, "seats"], ["回复询盘（按坐席去重）", r.total_inquiries, "total_inquiries"], ["公开回复消息数", r.total_replies, "replies"], ["在线人数峰值", summary.peak_online, "peak_online"], ["有采样的分钟数", summary.observed_minutes, "observed_minutes"], ["状态采样覆盖率（%）", summary.expected_samples ? Math.min(100, summary.samples / summary.expected_samples * 100) : null, "coverage"]]);
      const seatName = a => `<span class="report-seat-name">${esc(a.agent_name || `坐席 #${a.agent_id}`)}</span><span class="report-seat-id">${esc(a.source_key)} / ${esc(a.account_id)} / #${esc(a.agent_id)}</span>`;
      const qualityFields = [["首响时长", "first_response"], ["平均响应时长", "average_response"], ["对话量", "dialogues"], ["高质量对话量", "high_quality_dialogues"], ["发配量", "assigned_inquiries"], ["回复询盘总量", "inquiries"], ["平均评分", "average_rating"], ["低分数（低于4分）", "low_rating"], ["流单数（转人工后未回）", "lost_inquiries"], ["咨询状态总时长", "consultation_seconds"]];
      $("reportTable").innerHTML = r.agents.length ? `<table><thead><tr><th>坐席</th>${qualityFields.map(([label, key]) => `<th>${helpLabel(label, key)}</th>`).join("")}</tr></thead><tbody>${r.agents.map(a => `<tr><td>${seatName(a)}</td><td title="有效首响对话 ${number(a.first_response_samples)} 通">${duration(a.first_response_seconds)}</td><td title="有效响应对话 ${number(a.response_rounds)} 通，已完成响应 ${number(a.response_samples)} 次；先按对话平均，再等权汇总">${duration(a.average_response_seconds)}</td><td>${number(a.dialogues)}</td><td>${number(a.high_quality_dialogues)}</td><td>${number(a.assigned_inquiries)}</td><td>${number(a.inquiries)}</td><td title="已评分询盘 ${number(a.rated_inquiries)} 条">${number(a.average_rating)}</td><td>${number(a.low_rating)}</td><td>${number(a.lost_inquiries)}</td><td>${duration(a.consultation_seconds)}</td></tr>`).join("")}</tbody></table>` : '<div class="report-empty">当前时间范围没有坐席记录、公开人工回复、成功分配或已归属流单。</div>';
      $("reportTableSubtitle").textContent = `时长按转人工时间筛选并按对话平均；其余询盘按实际回复时间；与下表共用分页${r.unassigned_lost_inquiries ? `。另有 ${number(r.unassigned_lost_inquiries)} 条未归属坐席的流单` : ""}`;
      $("reportAgents").innerHTML = r.agents.length ? `<table><thead><tr><th>坐席</th>${[["回复询盘合计", "inquiries"], ["公开回复数", "replies"], ["在线分钟", "online_minutes"], ["忙碌分钟", "busy_minutes"]].map(([label, key]) => `<th>${helpLabel(label, key)}</th>`).join("")}${r.buckets.map(b => `<th>${helpLabel(b.label, "bucket")}</th>`).join("")}</tr></thead><tbody>${r.agents.map(a => {
        const sampled = a.online_minutes + a.busy_minutes + a.offline_minutes + a.unknown_minutes;
        return `<tr><td>${seatName(a)}</td><td>${number(a.inquiries)}</td><td>${number(a.replies)}</td><td>${sampled ? number(a.online_minutes) : "—"}</td><td>${sampled ? number(a.busy_minutes) : "—"}</td>${r.buckets.map(b => `<td>${b.future ? "—" : number(a.buckets[b.key] || 0)}</td>`).join("")}</tr>`;
      }).join("")}</tbody></table>` : '<div class="report-empty">当前时间范围没有坐席记录或公开人工回复。</div>';
      const pages = Math.max(1, Math.ceil(r.total / r.page_size));
      $("reportPageInfo").textContent = `第 ${r.page} / ${pages} 页 · 共 ${number(r.total)} 位坐席 · 每页 ${r.page_size} 位`;
      $("reportPrev").disabled = r.page <= 1; $("reportNext").disabled = r.page >= pages;
      $("reportNotice").innerHTML = `<p>可用客服量以采样时状态为“在线”的人数展示。按日展示每分钟人数；按月、年展示对应日、月内有采样分钟的平均在线人数，自定义时间段按天展示平均在线人数。</p><p>状态历史保留45天，当前可用起点为 ${esc(new Date(r.retention_cutoff_at).toLocaleString("zh-CN", {timeZone: "Asia/Shanghai", hour12: false}))}。未监控、缺采样和已过期不补为离线或零；覆盖不足时，观测人数可能偏低。询盘归档不受45天保留期影响。</p>`;
      $("reportRules").innerHTML = `<p>在线人数根据每分钟成功采样中的online状态计数，不表示当时仍有剩余接待容量。未收到状态样本的分钟保持空缺，当天尚未结束的分钟不参与统计。</p><p>首响时长：${esc(agentHelp.first_response)}</p><p>平均响应时长：${esc(agentHelp.average_response)} 同一通对话涉及多位坐席时，各自只计算自己完成的响应；转人工或有效消息时间不明的轮次排除时长计算。</p><p>回复询盘数按区间内实际公开回复的坐席和咨询轮次去重。跨时段回复同一轮时，合计可能小于各时段相加。平均评分与低分数基于这些已回复询盘的当前关联评价，同一坐席同一轮只计一次，未评分不参与平均；多人参与同一轮时分别计入各自数据。首响和响应使用转人工时间范围，时长样本数可以与回复询盘总量不同。</p><p>流单按区间内转人工时间统计，截至查询时没有任何公开人工回复（含尚在等待的进行中询盘），只归入已确认的ICS责任坐席；后续已由其他坐席回复的轮次不计流单，无法确认责任坐席的数量单独提示。转人工时间优先使用请求时间，再使用已确认的ICS分配时间；继承人工接待的轮次使用新轮开始时间。</p><p>坐席名单包含监控坐席、实际回复人、所选转人工对话中存在后续回复的坐席和存在已归属流单的责任坐席。两张坐席明细使用同一分页，坐席ID相同但来源或账号不同的记录独立统计。状态采样时长仅覆盖保留期内可观察到的分钟，回复质量不受45天状态保留期影响。</p>`;
    }
    function renderQuality() {
      const r = model.result;
      cards([["人工回复询盘总量", r.total_inquiries, "total_inquiries"], ["人工质检不合格量", r.total_failed_inquiries, "total_failed_inquiries"], ["人工不合格率（%）", r.human_failure_rate, "human_failure_rate"],
        ["所有对话轮数", r.ai.total_rounds, "ai_total_rounds"], ["AI质检不合格量", r.ai.failed_rounds, "ai_failed_rounds"], ["AI不合格率（%）", r.ai.failure_rate, "ai_failure_rate"]]);
      const fields = [["assigned_inquiries", "发配量"], ["dialogues", "对话量"], ["high_quality_dialogues", "高质量对话量"], ["inquiries", "回复询盘总量"], ["failed_inquiries", "质检不合格量"], ["failure_rate", "不合格率"]];
      const seatName = a => `<span class="report-seat-name">${esc(a.agent_name || `坐席 #${a.agent_id}`)}</span><span class="report-seat-id">${esc(a.source_key)} / ${esc(a.account_id)} / #${esc(a.agent_id)}</span>`;
      $("reportTable").innerHTML = r.agents.length ? `<table><thead><tr><th>坐席</th>${fields.map(([key, label]) => `<th>${helpLabel(label, key === "failure_rate" ? "human_failure_rate" : key)}</th>`).join("")}</tr></thead><tbody>${r.agents.map(a => `<tr><td>${seatName(a)}</td>${fields.map(([key]) => `<td>${key === "failure_rate" ? percent(a[key]) : number(a[key])}</td>`).join("")}</tr>`).join("")}</tbody></table>` : '<div class="report-empty">当前时间范围没有坐席记录或询盘。</div>';
      const selected = periodLabel(r);
      $("reportAgents").innerHTML = `<table><thead><tr><th>统计周期</th>${aiFields.map(([key, label]) => `<th>${helpLabel(label, `ai_${key}`)}</th>`).join("")}</tr></thead><tbody><tr><td>${esc(selected)}</td>${aiFields.map(([key]) => `<td>${key === "failure_rate" ? percent(r.ai[key]) : number(r.ai[key])}</td>`).join("")}</tr></tbody></table>`;
      const pages = Math.max(1, Math.ceil(r.total / r.page_size));
      $("reportQualityPageInfo").textContent = `第 ${r.page} / ${pages} 页 · 共 ${number(r.total)} 位坐席 · 每页 ${r.page_size} 位`;
      $("reportQualityPrev").disabled = r.page <= 1; $("reportQualityNext").disabled = r.page >= pages;
      $("reportNotice").innerHTML = "<p>人工与AI质检结论独立，使用当前最新保存结果；复核后历史报表随之更新。待质检轮次保留在分母中，不计为不合格；分母为0时不合格率显示‘—’。</p>";
      $("reportRules").innerHTML = `<p>人工表沿用坐席细化的发配量、对话量、高质量对话量和回复询盘总量。发配量按成功分配时间统计，其余三项按区间内实际回复询盘统计；多人回复同一轮时分别计入各自数据。</p><p>人工质检不合格量：${esc(qualityHelp.failed_inquiries)}</p><p>不合格率：${esc(qualityHelp.human_failure_rate)}</p>${aiFields.map(([key, label]) => `<p>${esc(label)}：${esc(qualityHelp[`ai_${key}`])}</p>`).join("")}`;
    }
    function periodLabel(r) {
      const mode = r.mode || model.mode;
      return mode === "custom" ? `${r.start_date} 至 ${r.end_date}` : mode === "day" ? r.date : mode === "month" ? r.date.slice(0, 7) : r.date.slice(0, 4);
    }
    function render() {
      const r = model.result;
      if (!r) return;
      const selected = periodLabel(r);
      $("reportStatus").textContent = `${selected} · ${{time: "时间统计", agents: "坐席细化", quality: "质检统计"}[model.kind]} · 更新于 ${new Date(r.generated_at).toLocaleTimeString("zh-CN", {timeZone: "Asia/Shanghai", hour12: false})}（北京时间）`;
      if (model.kind === "quality") {renderQuality(); return;}
      model.kind === "time" ? renderTime() : renderAgents();
      if (model.kind === "agents") $("reportRules").innerHTML += `<p>咨询状态总时长：${esc(agentHelp.consultation_seconds)} 跨日区间按筛选窗口截取，即使当天没有回复落点，也保留重合时长。</p><p>对话量：${esc(agentHelp.dialogues)}</p><p>高质量对话量：${esc(agentHelp.high_quality_dialogues)}</p><p>发配量：${esc(agentHelp.assigned_inquiries)}</p>`;
      renderChart();
    }
    return {start, stop, refresh: load};
  }
  window.ICSReports = {create};
})();
