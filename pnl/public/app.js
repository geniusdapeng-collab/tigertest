"use strict";

(() => {
  const USD = new Intl.NumberFormat("en-US", { style: "currency", currency: "USD", minimumFractionDigits: 2, maximumFractionDigits: 2 });
  const MARKET_PRICE = new Intl.NumberFormat("en-US", { style: "currency", currency: "USD", minimumFractionDigits: 2, maximumFractionDigits: 6 });
  const NUMBER = new Intl.NumberFormat("en-US", { maximumFractionDigits: 6 });
  const PERCENT = new Intl.NumberFormat("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  const DATE_TIME = new Intl.DateTimeFormat("zh-CN", { timeZone: "Asia/Shanghai", year: "numeric", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", hourCycle: "h23" });
  const DATE = new Intl.DateTimeFormat("en-CA", { timeZone: "Asia/Shanghai", year: "numeric", month: "2-digit", day: "2-digit" });
  const NEW_YORK_DATE = new Intl.DateTimeFormat("en-CA", { timeZone: "America/New_York", year: "numeric", month: "2-digit", day: "2-digit" });
  const SVG_NS = "http://www.w3.org/2000/svg";
  const ACTION_NAMES = { buy: "买入指令", sell: "卖出指令", hold: "继续持有", watch: "暂不买入" };
  const REQUIRED_MODEL_STAGES = ["clean.llm_semantic", "tech.sentiment", "tech.risk", "sector.narrative", "decision.debate"];
  let dashboard = null;
  let range = "all";
  let historyExpanded = false;
  let requestPending = false;
  let lastAttemptAt = 0;
  let selectedStock = null;

  const byId = (id) => document.getElementById(id);
  const finite = (value) => typeof value === "number" && Number.isFinite(value);
  const money = (value) => finite(value) ? USD.format(value) : "—";
  const number = (value) => finite(value) ? NUMBER.format(value) : "—";
  const percent = (value, signed = false) => finite(value) ? `${signed && value > 0 ? "+" : ""}${PERCENT.format(Object.is(value, -0) ? 0 : value)}%` : "—";
  const validDate = (value) => typeof value === "string" && Number.isFinite(Date.parse(value));
  const dateTime = (value) => validDate(value) ? `${DATE_TIME.format(new Date(value))}（北京时间）` : "截至时间未提供";
  const dateOnly = (value) => validDate(value) ? DATE.format(new Date(value)) : "—";

  function element(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined) node.textContent = String(text);
    return node;
  }

  function svgElement(tag, attributes = {}, text) {
    const node = document.createElementNS(SVG_NS, tag);
    for (const [key, value] of Object.entries(attributes)) node.setAttribute(key, String(value));
    if (text !== undefined) node.textContent = String(text);
    return node;
  }

  function setMetric(id, text, numericValue) {
    const node = byId(id);
    node.textContent = text;
    node.classList.toggle("is-positive", finite(numericValue) && numericValue > 0);
    node.classList.toggle("is-negative", finite(numericValue) && numericValue < 0);
  }

  function emptyRow(targetId, columns, message) {
    const row = element("tr");
    const cell = element("td", "table-empty");
    cell.colSpan = columns;
    cell.append(element("span", "table-empty-message", message));
    row.append(cell);
    byId(targetId).replaceChildren(row);
  }

  function validateDashboard(data) {
    if (!data || data.schemaVersion !== 1 || !["awaiting_first_record", "active"].includes(data.status)) throw new Error("INVALID_SCHEMA");
    if (!["history", "records"].every((key) => Array.isArray(data[key]))) throw new Error("INVALID_HISTORY");
    if (data.research !== undefined) {
      if (!data.research || !Array.isArray(data.research.history)) throw new Error("INVALID_RESEARCH");
      for (const research of [...data.research.history, ...(data.research.latest === null ? [] : [data.research.latest])]) {
        if (!research || !validDate(research.asOf) || !validDate(research.dataAsOf) || research.executionMode !== "paper" || !["disabled", "enabled", "passthrough"].includes(research.modelMode) || !["completed", "completed_degraded"].includes(research.status) || !finite(research.universeSize) || !Array.isArray(research.decisions)) throw new Error("INVALID_RESEARCH_RECORD");
        validateDecisions(research.decisions);
      }
    }
    if (data.status === "awaiting_first_record") {
      if (data.latest !== null || data.baseline !== null || data.metrics !== null || data.history.length) throw new Error("INVALID_EMPTY_STATE");
      return data;
    }
    const latest = data.latest;
    if (!latest || !validDate(latest.asOf) || !latest.account || latest.account.currency !== "USD" || !finite(latest.account.nav) || latest.account.nav <= 0 || !finite(latest.account.cash)) throw new Error("INVALID_ACCOUNT");
    if (!data.baseline || !validDate(data.baseline.date) || !finite(data.baseline.nav) || data.baseline.nav <= 0 || !data.metrics || !["returnPct", "drawdownPct", "recordCount"].every((key) => finite(data.metrics[key]))) throw new Error("INVALID_METRICS");
    for (const key of ["positions", "trades", "decisions"]) if (!Array.isArray(latest[key])) throw new Error("INVALID_ENTRIES");
    for (const row of data.history) if (!validDate(row.asOf) || !["nav", "returnPct", "drawdownPct"].every((key) => finite(row[key]))) throw new Error("INVALID_HISTORY_ENTRY");
    for (const row of latest.positions) if (typeof row.symbol !== "string" || !["quantity", "avgCost", "price", "marketValue", "weight", "unrealizedPnl"].every((key) => finite(row[key])) || row.weight < 0 || row.weight > 1) throw new Error("INVALID_POSITION");
    for (const row of latest.trades) if (typeof row.symbol !== "string" || !["buy", "sell"].includes(row.side) || !validDate(row.executedAt) || !["quantity", "price", "fees"].every((key) => finite(row[key]))) throw new Error("INVALID_TRADE");
    validateDecisions(latest.decisions);
    return data;
  }

  function validateDecisions(decisions) {
    for (const row of decisions) if (typeof row.symbol !== "string" || !Object.hasOwn(ACTION_NAMES, row.action) || !validDate(row.decidedAt) || typeof row.thesis !== "string" || !finite(row.targetWeight) || row.targetWeight < 0 || row.targetWeight > 1) throw new Error("INVALID_DECISION");
  }

  const plainText = (value) => typeof value === "string" && value.trim().length > 0;
  const positiveInteger = (value) => Number.isSafeInteger(value) && value > 0;
  function sourceUrl(value) {
    try {
      const url = new URL(value);
      return ["https:", "http:"].includes(url.protocol) && !url.username && !url.password && !url.hash
        && ![...url.searchParams.keys()].some((key) => /token|key|signature|authorization|credential|secret|password/i.test(key.replace(/[_-]/g, ""))) ? url.href : null;
    } catch { return null; }
  }

  function calendarDate(value) {
    if (typeof value !== "string" || !/^\d{4}-\d{2}-\d{2}$/.test(value) || value.startsWith("0000-")) return false;
    const stamp = Date.parse(`${value}T00:00:00Z`);
    return Number.isFinite(stamp) && new Date(stamp).toISOString().slice(0, 10) === value;
  }

  function marketEvidenceValid(record, decision) {
    if (!validDate(record.asOf) || !Array.isArray(decision.market_evidence) || decision.market_evidence.length !== 1) return false;
    const snapshot = decision.market_evidence[0];
    const fields = ["id", "symbol", "as_of", "close", "series_sha256"];
    if (!snapshot || Array.isArray(snapshot) || typeof snapshot !== "object"
      || Object.keys(snapshot).length !== fields.length || !fields.every((key) => Object.hasOwn(snapshot, key))
      || snapshot.symbol !== decision.symbol || !calendarDate(snapshot.as_of)
      || !finite(snapshot.close) || snapshot.close <= 0
      || typeof snapshot.series_sha256 !== "string" || !/^[a-f0-9]{64}$/.test(snapshot.series_sha256) || /^0+$/.test(snapshot.series_sha256)) return false;
    const parts = Object.fromEntries(NEW_YORK_DATE.formatToParts(new Date(record.asOf)).map(({ type, value }) => [type, value]));
    const assessmentDate = `${parts.year}-${parts.month}-${parts.day}`;
    const expectedId = `md_${snapshot.symbol}_${snapshot.as_of.replaceAll("-", "")}_${snapshot.series_sha256.slice(0, 24)}`;
    return snapshot.as_of <= assessmentDate && snapshot.id === expectedId;
  }

  function modelQualified(record) {
    const model = record?.model;
    return record?.modelMode === "enabled" && record.status === "completed"
      && model?.provider === "deepseek" && model.model === "deepseek-flash" && model.release === "DeepSeek-V4.1-Flash-1017"
      && model.required === true && model.all_required_completed === true && positiveInteger(model.call_count)
      && Array.isArray(model.response_ids) && model.response_ids.length === model.call_count && model.response_ids.every(plainText)
      && new Set(model.response_ids).size === model.call_count
      && Array.isArray(model.required_stages) && model.required_stages.length === REQUIRED_MODEL_STAGES.length
      && new Set(model.required_stages).size === model.required_stages.length
      && model.stage_counts && Object.keys(model.stage_counts).length === REQUIRED_MODEL_STAGES.length
      && REQUIRED_MODEL_STAGES.every((stage) => model.required_stages.includes(stage) && positiveInteger(model.stage_counts[stage]))
      && Object.values(model.stage_counts).reduce((sum, count) => sum + count, 0) === model.call_count
      && model.search_mode === "provider_native_search" && model.search_count === 14;
  }

  function structuredDecisionsValid(record) {
    if (!plainText(record?.summary) || !Array.isArray(record.executionDecisions)) return false;
    const seen = new Set();
    let totalWeight = 0;
    for (const decision of record.executionDecisions) {
      if (!decision || typeof decision.symbol !== "string" || !/^[A-Z0-9][A-Z0-9.^=-]{0,19}$/.test(decision.symbol)
        || seen.has(decision.symbol) || !["buy", "watch"].includes(decision.action)
        || !finite(decision.target_weight) || decision.target_weight < 0 || decision.target_weight > 1
        || !["action_summary", "why_this", "why_now", "entry_condition", "invalidation", "risk_note", "valid_for"].every((key) => plainText(decision[key]))
        || !["entry_price", "stop_price", "shares", "risk_usd", "time_stop_days", "reference_capital_usd"].every((key) => decision[key] === null || (finite(decision[key]) && decision[key] >= 0))
        || !marketEvidenceValid(record, decision)
        || !Array.isArray(decision.sources) || decision.sources.length > 8
        || new Set(decision.sources.map((source) => source?.id)).size !== decision.sources.length
        || decision.sources.some((source) => !source || !plainText(source.id) || source.id.startsWith("md_") || !plainText(source.title) || !sourceUrl(source.url))) return false;
      if (decision.action === "watch" && (decision.target_weight !== 0
        || ["entry_price", "stop_price", "shares", "risk_usd", "time_stop_days"].some((key) => decision[key] !== null))) return false;
      if (decision.action === "buy" && (!decision.sources.length || !(decision.target_weight > 0) || !(decision.entry_price > 0)
        || !(decision.stop_price > 0) || !(decision.stop_price < decision.entry_price)
        || !positiveInteger(decision.shares) || !(decision.risk_usd > 0) || !(decision.reference_capital_usd > 0))) return false;
      seen.add(decision.symbol);
      totalWeight += decision.target_weight;
    }
    return totalWeight <= 1.000001;
  }

  function expiryTimestamp(value) {
    // Only an explicit ISO expiry can be interpreted here. Session descriptions
    // remain verbatim; the display never derives an expiry from a trading phrase.
    return typeof value === "string" && /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,3})?(?:Z|[+-]\d{2}:\d{2})$/.test(value)
      && validDate(value) ? Date.parse(value) : null;
  }
  const expiredAt = (value) => expiryTimestamp(value) !== null && expiryTimestamp(value) < Date.now();

  function renderChartState(title, description, loading = false) {
    const container = byId("performance-chart");
    container.classList.toggle("has-chart", false);
    const state = element("div", "chart-state");
    state.append(element(loading ? "span" : "div", loading ? "loading-ring" : "chart-empty-symbol", loading ? undefined : "∅"));
    state.firstChild.setAttribute("aria-hidden", "true");
    state.append(element("p", "", title));
    if (description) state.append(element("span", "", description));
    container.replaceChildren(state);
    container.setAttribute("aria-label", `${title}。${description || ""}`);
  }

  function renderAwaiting(error = false) {
    for (const id of ["metric-nav", "metric-return", "metric-drawdown"]) setMetric(id, "—");
    byId("metric-days").replaceChildren(document.createTextNode("—"), element("small", "", "天"));
    byId("nav-note").textContent = "来自真实账户快照";
    byId("return-note").textContent = "自首笔真实记录起";
    byId("drawdown-note").textContent = "记录期内净值高点至低点";
    byId("days-note").textContent = "第一天，从真实开始";
    byId("baseline-date").textContent = error ? "基准日期暂不可用" : "基准日期待建立";
    byId("stock-weight").textContent = "—";
    byId("cash-weight").textContent = "—";
    byId("position-count").textContent = error ? "持仓数据暂不可用" : "持仓记录待入账";
    byId("allocation-bar").replaceChildren();
    byId("allocation-bar").setAttribute("aria-label", "资产配置暂无数据");
    byId("history-toggle").hidden = true;
    document.querySelectorAll("[data-range]").forEach((button) => { button.disabled = true; });
    byId("account-status").textContent = error ? "账簿暂不可用" : "待首笔真实记录";
    byId("account-status").className = `state-label${error ? " is-error" : ""}`;
    byId("as-of").textContent = error ? "当前无法获取账户数据" : "真实记录入账后开始计时";
    byId("chart-subtitle").textContent = "每一个数据点，对应一笔真实账户记录。";
    renderChartState(error ? "账簿暂时未能读取" : "首笔真实账户记录待入账", error ? "连接恢复后重新读取，请稍后重试。" : "基准尚未建立，业绩从第一笔真实记录开始。" );
    emptyRow("history-body", 5, error ? "历史账页暂不可用" : "第一张账页，等待真实记录。");
    emptyRow("positions-body", 6, error ? "持仓数据暂不可用" : "首笔真实账户快照入账后公开持仓。");
    byId("history-count").textContent = error ? "/ —" : "/ 0 笔";
    byId("positions-as-of").textContent = error ? "当前无法获取账户快照" : "截至时间待更新";
    byId("decisions-list").replaceChildren(element("p", "journal-empty", error ? "决策记录暂不可用。" : "暂无已公开的研究决策。"));
    byId("trades-list").replaceChildren(element("p", "journal-empty", error ? "成交记录暂不可用。" : "暂无已入账的真实成交。"));
  }

  function renderActive(data) {
    const { latest, metrics, baseline } = data;
    const start = Date.parse(`${dateOnly(baseline.date)}T00:00:00Z`);
    const end = Date.parse(`${dateOnly(latest.asOf)}T00:00:00Z`);
    const days = Math.max(1, Math.floor((end - start) / 86400000) + 1);
    byId("account-status").textContent = data.history.length === 1 ? "真实账户 · 基准已建立" : "真实账户 · 持续记账";
    byId("account-status").className = "state-label is-active";
    byId("as-of").textContent = `截至 ${dateTime(latest.asOf)}`;
    setMetric("metric-nav", money(latest.account.nav));
    setMetric("metric-return", percent(metrics.returnPct, true), metrics.returnPct);
    const maxDrawdown = data.history.reduce((maximum, record) => Math.min(maximum, record.drawdownPct), 0);
    setMetric("metric-drawdown", percent(maxDrawdown), maxDrawdown);
    byId("metric-days").replaceChildren(document.createTextNode(number(days)), element("small", "", "天"));
    byId("days-note").textContent = `始于 ${dateOnly(baseline.date)}`;
    byId("nav-note").textContent = `账户记账币种 · ${latest.account.currency}`;
    byId("return-note").textContent = data.history.length === 1 ? "首日建立基准，收益从此累计" : "已剔除记录期内净出入金影响";
    byId("drawdown-note").textContent = data.history.length === 1 ? "首日基准，尚无跨日回撤" : "逐期收益净值的最大回落";
    byId("baseline-date").textContent = `基准 ${dateOnly(baseline.date)} · ${money(baseline.nav)}`;
    byId("history-count").textContent = `/ ${number(metrics.recordCount)} 笔`;
    byId("positions-as-of").textContent = dateTime(latest.asOf);
    byId("position-count").textContent = latest.positions.length ? `${latest.positions.length} 只股票持仓` : "当前账户未持有股票";
    byId("methodology-text").textContent = "首笔真实账户记录建立基准。相邻快照先从本期净资产中扣除本期净入金，计算单期收益，再逐期复合；资金流采用期末口径，不把入金算成盈利。";
    byId("methodology-detail").textContent = "收益包含已记录的费用；最大回撤按剔除资金流影响后的收益净值计算。样本不足时不展示年化收益或胜率。";
    renderAllocation(latest);
    renderPositions(latest.positions);
    const seenTrades = new Set();
    const reportedTrades = [];
    for (const record of data.records.length ? data.records : [latest]) {
      for (const trade of record.trades || []) {
        if (!seenTrades.has(trade.id)) { seenTrades.add(trade.id); reportedTrades.push(trade); }
      }
    }
    renderTrades(reportedTrades);
    renderHistory();
    renderChart();
    document.querySelectorAll("[data-range]").forEach((button) => { button.disabled = false; });
  }

  function renderAllocation(latest) {
    const positionValue = latest.positions.reduce((total, row) => total + row.marketValue, 0);
    const stockWeight = positionValue / latest.account.nav;
    const cashWeight = latest.account.cash / latest.account.nav;
    byId("stock-weight").textContent = percent(stockWeight * 100);
    byId("cash-weight").textContent = percent(cashWeight * 100);
    const bar = byId("allocation-bar");
    const svg = svgElement("svg", { viewBox: "0 0 1000 5", preserveAspectRatio: "none", "aria-hidden": "true" });
    bar.replaceChildren(svg);
    bar.setAttribute("aria-label", `股票持仓 ${percent(stockWeight * 100)}，现金 ${percent(cashWeight * 100)}`);
    let offset = 0;
    for (const [weight, color] of [[stockWeight, "#9e8526"], [cashWeight, "#abb59d"]]) {
      if (weight <= 0) continue;
      const segmentWidth = Math.max(0, Math.min(weight, 1)) * 1000;
      svg.append(svgElement("rect", { x: offset, y: 0, width: segmentWidth, height: 5, fill: color }));
      offset += segmentWidth;
    }
  }

  function renderPositions(positions) {
    if (!positions.length) {
      emptyRow("positions-body", 6, "当前账户未持有股票。现金同样是账户配置的一部分。" );
      return;
    }
    const fragment = document.createDocumentFragment();
    for (const item of [...positions].sort((a, b) => b.weight - a.weight)) {
      const row = element("tr");
      const asset = element("td");
      asset.append(element("span", "asset-symbol", item.symbol), element("span", "asset-name", item.name || item.symbol));
      const price = element("td", "numeric", money(item.price));
      price.append(element("span", "price-subvalue", `成本 ${money(item.avgCost)}`));
      const weightCell = element("td", "numeric");
      const weight = element("span", "weight-cell");
      const track = svgElement("svg", { class: "weight-track", viewBox: "0 0 42 3", preserveAspectRatio: "none" });
      track.setAttribute("aria-hidden", "true");
      track.append(svgElement("rect", { width: Math.min(42, Math.max(0, item.weight * 42)), height: 3, fill: "#9e8526" }));
      weight.append(track, document.createTextNode(percent(item.weight * 100)));
      weightCell.append(weight);
      row.append(asset, element("td", "numeric", number(item.quantity)), price, element("td", "numeric", money(item.marketValue)), weightCell, element("td", `numeric${item.unrealizedPnl > 0 ? " is-positive" : item.unrealizedPnl < 0 ? " is-negative" : ""}`, `${item.unrealizedPnl > 0 ? "+" : ""}${money(item.unrealizedPnl)}`));
      fragment.append(row);
    }
    byId("positions-body").replaceChildren(fragment);
  }

  function renderHistory() {
    const history = [...dashboard.history].sort((a, b) => Date.parse(b.asOf) - Date.parse(a.asOf));
    const rows = historyExpanded ? history : history.slice(0, 7);
    const dayCounts = new Map();
    for (const record of history) { const day = dateOnly(record.asOf); dayCounts.set(day, (dayCounts.get(day) || 0) + 1); }
    const fragment = document.createDocumentFragment();
    for (const item of rows) {
      const row = element("tr");
      const date = element("td", "table-date", dateOnly(item.asOf));
      const sameDayCount = dayCounts.get(dateOnly(item.asOf));
      if (sameDayCount > 1) date.append(element("small", "", `${DATE_TIME.format(new Date(item.asOf)).split(" ").pop()} 更新`));
      const status = element("td", "record-column");
      status.append(element("span", "record-label", item === history[history.length - 1] ? "首笔基准" : "账户记录"));
      row.append(date, element("td", "numeric", money(item.nav)), element("td", `numeric${item.returnPct > 0 ? " is-positive" : item.returnPct < 0 ? " is-negative" : ""}`, percent(item.returnPct, true)), element("td", "numeric", percent(item.drawdownPct)), status);
      fragment.append(row);
    }
    byId("history-body").replaceChildren(fragment);
    const toggle = byId("history-toggle");
    toggle.hidden = history.length <= 7;
    toggle.textContent = historyExpanded ? "收起历史记录 ↑" : `展开全部 ${history.length} 笔记录 ↓`;
    toggle.setAttribute("aria-expanded", String(historyExpanded));
  }

  function renderResearch(data) {
    const research = data.research?.latest;
    const stamp = byId("research-stamp");
    const qualified = modelQualified(research) && structuredDecisionsValid(research);
    renderHistoricalResearch(data);
    renderDesk(data, qualified ? research : null);
    if (!qualified) {
      renderInstructionState(research ? "unqualified" : "pending",
        research ? "等待满足要求的模型指令" : "等待新一轮模型指令",
        research ? "已有运行记录未满足本轮必需模型与完整指令的核验条件，已收进历史记录。当前不据此发出新的落单指令。" : "实际模型完成分析并通过核验后，交易指令将在这里公开。没有完整产出时不以规则结果替代。" );
      return;
    }

    const status = byId("instruction-status");
    const allExpired = research.executionDecisions.length > 0 && research.executionDecisions.every((item) => expiredAt(item.valid_for));
    status.textContent = allExpired ? "历史指令 · 已过有效期" : "必需模型与搜索 · 已核验";
    status.className = `instruction-status ${allExpired ? "is-pending" : "is-verified"}`;
    const proof = byId("model-proof");
    proof.hidden = false;
    proof.replaceChildren();
    const identity = element("div", "model-identity");
    identity.append(element("span", "model-proof-label", "本轮实际模型"), element("strong", "", "DeepSeek"), element("span", "model-release", research.model.release), element("span", "model-api-name", research.model.model));
    const facts = element("div", "model-proof-facts");
    for (const [value, label] of [[research.model.call_count, "次真实模型调用"], [research.model.required_stages.length, "个必需模型阶段完成"], [research.model.search_count, "次模型原生搜索"]]) {
      const fact = element("div");
      fact.append(element("strong", "", number(value)), element("span", "", label));
      facts.append(fact);
    }
    proof.append(identity, facts);
    stamp.hidden = false;
    stamp.replaceChildren(element("span", "", `研究生成 ${dateTime(research.asOf)}`), element("span", "", `行情截至 ${dateOnly(research.dataAsOf)} · 本轮扫描 ${number(research.universeSize)} 只标的`), element("span", "research-disclosure", "研究计算：paper 环境 · 真实执行与账户独立记账"));
    const summary = byId("instruction-summary");
    summary.hidden = false;
    summary.replaceChildren(element("p", "instruction-summary-label", allExpired ? "当时的总体指令" : "本轮总体指令"), element("p", "instruction-summary-text", research.summary));
    if (allExpired) summary.append(element("p", "instruction-summary-note", "以下指令已过其明确有效期，保留当时的判断用于复核；等待新的已核验模型指令。"));
    renderQuicklist(research);
    renderExecutionDecisions(research);
  }

  function renderInstructionState(kind, title, description) {
    const status = byId("instruction-status");
    status.textContent = kind === "error" ? "本轮指令暂不可核验" : kind === "unqualified" ? "历史运行不作本轮指令" : "等待必需模型完成";
    status.className = `instruction-status ${kind === "error" ? "is-error" : "is-pending"}`;
    for (const id of ["model-proof", "research-stamp", "instruction-summary"]) {
      byId(id).hidden = true;
      byId(id).replaceChildren();
    }
    const empty = element("div", `instruction-empty${kind === "error" ? " is-error" : ""}`);
    empty.append(element("strong", "", title), element("p", "", description));
    byId("decisions-list").replaceChildren(empty);
    byId("stock-selector").hidden = true;
    byId("stock-selector").replaceChildren();
    byId("decision-quicklist").replaceChildren(element("p", "instruction-empty", description));
    renderResearchMetrics(null);
  }

  function addInstructionReason(container, label, value, className = "") {
    const section = element("div", `instruction-reason${className ? ` ${className}` : ""}`);
    section.append(element("h5", "", label), element("p", "", value));
    container.append(section);
  }

  function renderExecutionDecisions(research) {
    const list = byId("decisions-list");
    if (!research.executionDecisions.length) {
      const empty = element("div", "instruction-empty");
      empty.append(element("strong", "", "本轮没有新增单票落单指令"), element("p", "", "执行团队按上方总体指令行动。账户已有持仓与已回报成交，分别查看持仓记录和实际执行回报。"));
      list.replaceChildren(empty);
      byId("stock-selector").hidden = true;
      byId("stock-selector").replaceChildren();
      return;
    }
    const fragment = document.createDocumentFragment();
    for (const [index, item] of research.executionDecisions.entries()) {
      const expired = expiredAt(item.valid_for);
      const article = element("article", `instruction-entry${expired ? " is-expired" : ""}`);
      article.id = `stock-panel-${index}`;
      article.setAttribute("role", "tabpanel");
      article.setAttribute("aria-labelledby", `stock-tab-${index}`);
      article.setAttribute("tabindex", "0");
      article.dataset.panelGroup = "stock";
      article.dataset.panel = item.symbol;
      const header = element("div", "instruction-entry-head");
      const identity = element("div", "instruction-symbol");
      identity.append(element("span", `action-badge action-${item.action}`, ACTION_NAMES[item.action]), element("h4", "", item.symbol));
      header.append(identity, element("span", "instruction-target", `目标仓位 ${percent(item.target_weight * 100)}`));
      article.append(header, element("p", "instruction-headline", item.action_summary));
      if (expired) article.append(element("p", "instruction-expired-note", "已过明确有效期 · 等待新指令"));

      const reasons = element("div", "instruction-reasons");
      addInstructionReason(reasons, "为什么选它", item.why_this);
      addInstructionReason(reasons, "为什么现在", item.why_now);
      article.append(reasons);

      const market = item.market_evidence[0];
      const provenance = element("div", "instruction-market-evidence");
      provenance.append(element("h5", "", "行情依据"), element("p", "instruction-market-facts", `${market.symbol} · ${market.as_of} 交易日 · 收盘 ${MARKET_PRICE.format(market.close)}`));
      provenance.append(element("p", "instruction-market-note", "模型本轮引用的已冻结行情快照，非实时价格。"));
      provenance.append(element("p", "instruction-news-note", item.sources.length ? `公司新闻引用 ${item.sources.length} 篇 · 可展开下方来源查看` : "本条未引用公司新闻"));
      article.append(provenance);

      const metrics = element("dl", `instruction-metrics${item.action === "watch" ? " is-observation" : ""}`);
      for (const [label, value] of [["入场参考", money(item.entry_price)], ["认错线 / 止损", money(item.stop_price)], ["计划股数", item.shares === null ? "—" : `${number(item.shares)} 股`]]) {
        const metric = element("div");
        metric.append(element("dt", "", label), element("dd", "", value));
        metrics.append(metric);
      }
      article.append(metrics);
      addInstructionReason(article, "执行办法", item.entry_condition, "instruction-execution");
      addInstructionReason(article, "何时撤销 / 离场", item.invalidation, "instruction-invalidation");
      addInstructionReason(article, "主要风险", item.risk_note, "instruction-risk");

      const validity = element("div", "instruction-validity");
      validity.append(element("span", "", "有效范围"), element("p", "", expiryTimestamp(item.valid_for) === null ? item.valid_for : `截至 ${dateTime(item.valid_for)}`));
      article.append(validity);
      const evidence = element("details", "instruction-evidence");
      const evidenceSummary = element("summary", "", "查看来源与核验记录");
      evidence.append(evidenceSummary);
      const numeric = element("p", "instruction-numeric-note", `计划风险 ${money(item.risk_usd)} · 计划资金基准 ${money(item.reference_capital_usd)} · 时间纪律 ${item.time_stop_days === null ? "—" : `${number(item.time_stop_days)} 个交易日`}`);
      evidence.append(numeric);
      if (item.action === "watch") evidence.append(element("p", "instruction-numeric-note", "本项为观察指令；未提供下单价格或股数的字段保持空白。0% 是目标配置，不代替实际持仓。"));
      else evidence.append(element("p", "instruction-numeric-note", "以上为已核验计划值，成交以团队执行回报为准；跳空、滑点与费用可能使实际损失超过计划风险。"));
      if (item.sources.length) {
        const sources = element("ol", "instruction-sources");
        for (const source of item.sources) {
          const row = element("li");
          const link = element("a", "", source.title);
          link.href = sourceUrl(source.url); link.target = "_blank"; link.rel = "noopener noreferrer";
          row.append(link, element("span", "", validDate(source.published_at) ? ` · ${dateOnly(source.published_at)}` : " · 发布日期未提供"));
          sources.append(row);
        }
        evidence.append(sources);
      }
      const meta = element("div", "instruction-identifiers");
      meta.append(element("span", "", `行情快照 ${market.id}`), element("span", "", `完整行情序列 SHA256 ${market.series_sha256}`), element("span", "", `运行 ${research.runId}`), element("span", "", `原始有效范围 ${item.valid_for}`), element("span", "", `原始回执 SHA256 ${research.receiptHash}`));
      evidence.append(meta);
      article.append(evidence);
      fragment.append(article);
    }
    list.replaceChildren(fragment);
    renderStockSelector(research.executionDecisions);
  }

  function renderHistoricalResearch(data, qualifiedCurrent = null, connectionFailed = false) {
    const history = [...(data.research?.history || [])];
    if (data.research?.latest && !history.some((item) => item.runId === data.research.latest.runId)) history.unshift(data.research.latest);
    const rows = history.filter(Boolean);
    const container = byId("legacy-research");
    container.hidden = rows.length === 0;
    byId("research-history-empty").hidden = rows.length > 0;
    byId("legacy-research-title").textContent = `历史指令与运行记录（${rows.length}）`;
    const body = byId("legacy-research-body");
    body.replaceChildren();
    for (const record of rows) {
      const entry = element("details", "historical-entry");
      const qualification = modelQualified(record) && structuredDecisionsValid(record);
      const label = connectionFailed && record.runId === data.research?.latest?.runId ? "上次读取记录 · 当前连接未核验" : qualification ? "历史模型指令 · 以当时有效范围为准" : "历史运行 · 未满足模型必需条件";
      entry.append(element("summary", "", `${dateTime(record.asOf)} · ${label}`));
      entry.append(element("p", "historical-summary", typeof record.summary === "string" ? record.summary : "原记录未提供总体说明。"));
      if (!qualification) entry.append(element("p", "historical-qualification", record.modelMode === "disabled" ? "这条历史运行没有启用模型，不作为本轮交易指令。" : "这条历史运行没有完整的必需模型执行凭据，不作为本轮交易指令。"));
      for (const decision of record.decisions || []) {
        const note = element("div", "historical-decision");
        note.append(element("strong", "", `${decision.symbol} · 当时目标 ${percent(decision.targetWeight * 100)}`), element("p", "", decision.thesis));
        entry.append(note);
      }
      const metadata = element("div", "instruction-identifiers");
      metadata.append(element("span", "", `运行 ${record.runId || "未提供"}`), element("span", "", `行情截至 ${dateOnly(record.dataAsOf)}`));
      entry.append(metadata);
      body.append(entry);
    }
  }

  function renderTrades(trades) {
    const list = byId("trades-list");
    if (!trades.length) {
      list.replaceChildren(element("p", "journal-empty", "截至账户快照，暂无已入账的真实成交。新的团队执行回报待更新。"));
      return;
    }
    const fragment = document.createDocumentFragment();
    for (const item of [...trades].sort((a, b) => Date.parse(b.executedAt) - Date.parse(a.executedAt))) {
      const article = element("article", "trade-entry");
      const side = element("span", `trade-side${item.side === "sell" ? " sell" : ""}`, item.side === "buy" ? "买" : "卖");
      const main = element("div", "trade-main");
      const time = element("time", "", dateTime(item.executedAt));
      time.dateTime = item.executedAt;
      const detail = element("small");
      detail.append(time);
      main.append(element("strong", "", item.symbol), detail);
      const value = element("div", "trade-value", `${number(item.quantity)} 股 × ${money(item.price)}`);
      value.append(element("small", "", `费用 ${money(item.fees)}`));
      article.append(side, main, value);
      fragment.append(article);
    }
    list.replaceChildren(fragment);
  }

  function renderChart() {
    let history = [...dashboard.history].sort((a, b) => Date.parse(a.asOf) - Date.parse(b.asOf));
    if (range !== "all" && history.length) {
      const threshold = Date.parse(history[history.length - 1].asOf) - (Number(range) - 1) * 86400000;
      history = history.filter((row) => Date.parse(row.asOf) >= threshold);
    }
    document.querySelectorAll("[data-range]").forEach((button) => {
      const selected = button.dataset.range === range;
      button.classList.toggle("is-active", selected);
      button.setAttribute("aria-pressed", String(selected));
    });
    if (!history.length) {
      renderChartState("该时段暂无账户记录", "调整时间范围查看已公开的账页。" );
      return;
    }
    const container = byId("performance-chart");
    const mobile = window.matchMedia("(max-width: 740px)").matches;
    const width = mobile ? Math.max(270, container.clientWidth || 300) : 1100;
    const height = mobile ? 235 : 270;
    const padding = { top: 30, right: mobile ? 12 : 24, bottom: 34, left: mobile ? 40 : 57 };
    const plotWidth = width - padding.left - padding.right;
    const plotHeight = height - padding.top - padding.bottom;
    const svg = svgElement("svg", { class: "chart-svg", viewBox: `0 0 ${width} ${height}`, preserveAspectRatio: "none", "aria-hidden": "true" });
    let min = history.reduce((value, point) => Math.min(value, point.returnPct), 0);
    let max = history.reduce((value, point) => Math.max(value, point.returnPct), 0);
    const spread = max - min;
    const margin = spread < .02 ? 1 : spread * .22;
    min -= margin;
    max += margin;
    const y = (value) => padding.top + (max - value) / (max - min) * plotHeight;
    const startTime = Date.parse(history[0].asOf);
    const endTime = Date.parse(history[history.length - 1].asOf);
    const x = (value) => history.length === 1 || endTime === startTime ? padding.left + plotWidth / 2 : padding.left + (Date.parse(value) - startTime) / (endTime - startTime) * plotWidth;
    for (let i = 0; i < 4; i += 1) {
      const value = max - (max - min) * i / 3;
      const position = y(value);
      svg.append(svgElement("line", { x1: padding.left, x2: width - padding.right, y1: position, y2: position, stroke: "#e9ece1", "stroke-width": 1, "vector-effect": "non-scaling-stroke" }));
      svg.append(svgElement("text", { x: padding.left - 12, y: position + 3, class: "chart-label", "text-anchor": "end" }, `${value.toFixed(1)}%`));
    }
    svg.append(svgElement("line", { x1: padding.left, x2: width - padding.right, y1: y(0), y2: y(0), stroke: "#c6cebb", "stroke-dasharray": "3 5", "stroke-width": 1, "vector-effect": "non-scaling-stroke" }));
    const tooltip = element("div", "chart-tooltip");
    tooltip.setAttribute("aria-hidden", "true");
    if (history.length > 1) {
      const path = history.map((point, index) => `${index === 0 ? "M" : "L"}${x(point.asOf).toFixed(2)},${y(point.returnPct).toFixed(2)}`).join(" ");
      svg.append(svgElement("path", { d: path, fill: "none", stroke: "#9e8526", "stroke-width": 2.3, "stroke-linecap": "round", "stroke-linejoin": "round", "vector-effect": "non-scaling-stroke" }));
    }
    for (const point of history) {
      const dot = svgElement("g");
      dot.append(svgElement("circle", { cx: x(point.asOf), cy: y(point.returnPct), r: history.length === 1 ? 6 : 3.5, fill: "#9e8526", stroke: "#fffefa", "stroke-width": 2, "vector-effect": "non-scaling-stroke" }));
      dot.append(svgElement("circle", { cx: x(point.asOf), cy: y(point.returnPct), r: 14, fill: "transparent" }));
      const label = `${dateOnly(point.asOf)} · 净资产 ${money(point.nav)} · 累计收益 ${percent(point.returnPct, true)}`;
      dot.append(svgElement("title", {}, label));
      dot.addEventListener("pointerenter", () => { tooltip.textContent = label; });
      dot.addEventListener("pointerleave", () => { tooltip.textContent = ""; });
      svg.append(dot);
    }
    if (history.length === 1) {
      const point = history[0];
      svg.append(svgElement("text", { x: x(point.asOf), y: y(point.returnPct) - 24, class: "chart-point-label", "text-anchor": "middle" }, dashboard.history.length === 1 ? "第 1 天 · 基准建立" : "该时段 1 笔记录"));
      svg.append(svgElement("text", { x: x(point.asOf), y: y(point.returnPct) + 29, class: "chart-point-note", "text-anchor": "middle" }, "长期结果，从这一点开始。"));
    }
    const labelIndexes = history.length === 1 ? [0] : [0, ...(history.length > 3 ? [Math.floor((history.length - 1) / 2)] : []), history.length - 1];
    for (const index of labelIndexes) {
      const point = history[index];
      svg.append(svgElement("text", { x: x(point.asOf), y: height - 8, class: "chart-label", "text-anchor": history.length === 1 ? "middle" : index === 0 ? "start" : index === history.length - 1 ? "end" : "middle" }, dateOnly(point.asOf)));
    }
    container.replaceChildren(svg, tooltip);
    container.classList.add("has-chart");
    container.setAttribute("aria-label", history.length === 1 ? `${dateOnly(history[0].asOf)}，${dashboard.history.length === 1 ? "首笔真实记录建立基准" : "该时段唯一记录"}，账户净资产 ${money(history[0].nav)}，累计收益 ${percent(history[0].returnPct)}。详细数据见下方每日账页。` : `${dateOnly(history[0].asOf)} 至 ${dateOnly(history[history.length - 1].asOf)}，共 ${history.length} 笔真实记录，最新累计收益 ${percent(history[history.length - 1].returnPct, true)}。详细数据见下方每日账页。`);
    byId("chart-subtitle").textContent = dashboard.history.length === 1 ? "首日只有一个真实数据点，持续记账后形成收益曲线。" : `按已公开账户快照连接，共 ${history.length} 笔记录；并非实时行情。`;
  }

  function showConnectionNotice(message) {
    const notice = byId("connection-notice");
    const button = element("button", "retry-button", "重新读取");
    button.type = "button";
    button.addEventListener("click", () => { void loadDashboard(); });
    notice.replaceChildren(element("span", "", message), button);
    notice.hidden = false;
  }

  async function loadDashboard() {
    if (requestPending) return;
    requestPending = true;
    byId("refresh-dashboard").disabled = true;
    byId("refresh-dashboard").textContent = "读取中…";
    lastAttemptAt = Date.now();
    byId("performance").setAttribute("aria-busy", "true");
    const retry = document.querySelector(".retry-button");
    if (retry) { retry.disabled = true; retry.textContent = "读取中…"; }
    const controller = new AbortController();
    const timeout = window.setTimeout(() => controller.abort(), 12000);
    try {
      const response = await fetch("/api/public/dashboard", { method: "GET", headers: { Accept: "application/json" }, cache: "no-store", credentials: "same-origin", signal: controller.signal });
      if (!response.ok) throw new Error(`HTTP_${response.status}`);
      const data = validateDashboard(await response.json());
      dashboard = data;
      byId("connection-notice").hidden = true;
      if (data.status === "active") renderActive(data); else renderAwaiting();
      renderResearch(data);
      byId("last-read").textContent = `最近读取 ${dateTime(new Date().toISOString())}`;
    } catch (error) {
      if (!dashboard) renderAwaiting(true);
      renderInstructionState("error", "当前无法核验最新模型指令", "本轮未取得完整的最新记录，当前不展示可执行的新指令。请重新读取；历史记录的原生成时间和有效范围保持不变。" );
      if (dashboard) renderHistoricalResearch(dashboard, null, true);
      renderDesk(dashboard, null, true);
      byId("last-read").textContent = "本轮读取失败 · 可刷新重试";
      showConnectionNotice(dashboard?.status === "active" ? "最新账簿暂时无法读取，当前保留上次成功读取的记录，请以标注的截至时间为准。" : "账户数据暂时无法读取。页面不展示未经核实的业绩，请重新读取。" );
    } finally {
      window.clearTimeout(timeout);
      requestPending = false;
      byId("refresh-dashboard").disabled = false;
      byId("refresh-dashboard").replaceChildren(document.createTextNode("刷新账簿 "), element("span", "", "↻"));
      byId("performance").setAttribute("aria-busy", "false");
    }
  }

  function renderResearchMetrics(research) {
    const plans = research?.executionDecisions || [];
    const rows = [
      ["本轮扫描", research ? `${number(research.universeSize)} 只` : "—", "来自本轮研究的扫描记录"],
      ["买入计划", research ? `${plans.filter((row) => row.action === "buy").length} 只` : "—", "研究计划，不是实际成交"],
      ["观察标的", research ? `${plans.filter((row) => row.action === "watch").length} 只` : "—", "条件不足时，保持观察"],
      ["行情日期", research ? research.dataAsOf.slice(0, 10) : "—", "模型引用的数据，非实时行情"],
    ];
    byId("research-metrics").replaceChildren(...rows.map(([label, value, note]) => {
      const card = element("div");
      card.append(element("span", "", label), element("strong", "", value), element("small", "", note));
      return card;
    }));
  }

  function renderDesk(data, research, connectionFailed = false) {
    byId("hero-account-time").textContent = data?.latest ? dateTime(data.latest.asOf) : "待首笔真实记录";
    byId("hero-research-time").textContent = data?.research?.latest ? dateTime(data.research.latest.asOf) : "待已核验研究";
    const days = data?.latest ? Math.max(0, Math.floor((Date.now() - Date.parse(data.latest.asOf)) / 86400000)) : 0;
    const accountNote = days >= 2 ? `真实账户距今 ${days} 天未新增记录，数值保持原快照。` : "真实账户以最后一笔已入账快照为准。";
    byId("hero-data-note").textContent = `${accountNote} 研究使用 paper 核算；计划价格与仓位不代表实际成交。`;
    if (connectionFailed) {
      byId("hero-status").textContent = "读取失败 · 待重试";
      byId("hero-summary").textContent = "本轮未能核验最新研究，暂不展示可执行的新指令。已读账户与历史记录保留原截至时间。";
      return;
    }
    renderResearchMetrics(research);
    if (!research) {
      byId("hero-status").textContent = "等待完整研究";
      byId("hero-summary").textContent = "暂时没有满足必需模型与证据条件的研究。本轮不据此给出新的交易指令，可查看历史记录与交易方法。";
      return;
    }
    const plans = research.executionDecisions;
    const allExpired = plans.length > 0 && plans.every((row) => expiredAt(row.valid_for));
    const ageDays = Math.max(0, Math.floor((Date.now() - Date.parse(research.asOf)) / 86400000));
    byId("hero-status").textContent = allExpired ? "历史研究 · 已过有效期" : ageDays >= 2 ? "最近研究 · 请核对有效期" : "模型研究 · 已核验";
    byId("hero-summary").textContent = research.summary;
    if (allExpired) byId("hero-data-note").textContent = `研究指令已过明确有效期，只作复核。${accountNote} 等待新一轮完整研究；真实执行与研究分别记账。`;
  }

  function renderQuicklist(research) {
    const list = byId("decision-quicklist");
    if (!research.executionDecisions.length) {
      list.replaceChildren(element("p", "instruction-empty", "本轮没有新增单票计划。请按总体研究理解当前动作；真实持仓与已回报成交另见账户栏目。"));
      return;
    }
    list.replaceChildren(...research.executionDecisions.map((item) => {
      const row = element("article", "quick-row");
      const identity = element("div");
      identity.append(element("h4", "quick-symbol", item.symbol), element("span", `action-badge action-${item.action}`, ACTION_NAMES[item.action]));
      const body = element("div", "quick-summary");
      body.append(element("p", "", item.action_summary), element("small", "", `行情 ${item.market_evidence[0].as_of} · 目标 ${percent(item.target_weight * 100)}${expiredAt(item.valid_for) ? " · 已过有效期" : " · 请核对有效范围"}`));
      const button = element("button", "", expiredAt(item.valid_for) ? "复核详情 →" : "查看详情 →");
      button.type = "button";
      button.setAttribute("aria-label", `查看 ${item.symbol} 的完整研究`);
      button.addEventListener("click", () => {
        activateTab("research", "stocks");
        activateTab("stock", item.symbol, true);
      });
      row.append(identity, body, button);
      return row;
    }));
  }

  function activateTab(group, key, focus = false) {
    const tabs = [...document.querySelectorAll(`[data-tab-group="${group}"]`)];
    const target = tabs.find((tab) => tab.dataset.tab === key);
    if (!target) return;
    for (const tab of tabs) {
      const selected = tab === target;
      tab.setAttribute("aria-selected", String(selected));
      tab.tabIndex = selected ? 0 : -1;
    }
    for (const panel of document.querySelectorAll(`[data-panel-group="${group}"]`)) panel.hidden = panel.dataset.panel !== key;
    if (group === "stock") selectedStock = key;
    if (group === "main" && key === "account" && dashboard?.status === "active") window.requestAnimationFrame(renderChart);
    if (focus) target.focus();
  }

  function bindTab(tab) {
    tab.addEventListener("click", () => {
      activateTab(tab.dataset.tabGroup, tab.dataset.tab);
      if (tab.dataset.tabGroup === "main") {
        window.history.replaceState(null, "", `#${tab.dataset.tab}`);
      }
    });
    tab.addEventListener("keydown", (event) => {
      const tabs = [...document.querySelectorAll(`[data-tab-group="${tab.dataset.tabGroup}"]`)];
      const index = tabs.indexOf(tab);
      let next;
      if (event.key === "ArrowRight") next = (index + 1) % tabs.length;
      else if (event.key === "ArrowLeft") next = (index - 1 + tabs.length) % tabs.length;
      else if (event.key === "Home") next = 0;
      else if (event.key === "End") next = tabs.length - 1;
      else return;
      event.preventDefault();
      activateTab(tab.dataset.tabGroup, tabs[next].dataset.tab, true);
      if (tab.dataset.tabGroup === "main") window.history.replaceState(null, "", `#${tabs[next].dataset.tab}`);
    });
  }

  function renderStockSelector(decisions) {
    const selector = byId("stock-selector");
    selector.replaceChildren(...decisions.map((item, index) => {
      const button = element("button", "", item.symbol);
      button.type = "button";
      button.id = `stock-tab-${index}`;
      button.setAttribute("role", "tab");
      button.setAttribute("aria-controls", `stock-panel-${index}`);
      button.dataset.tabGroup = "stock";
      button.dataset.tab = item.symbol;
      bindTab(button);
      return button;
    }));
    selector.hidden = !decisions.length;
    activateTab("stock", decisions.some((item) => item.symbol === selectedStock) ? selectedStock : decisions[0]?.symbol);
  }

  function openHash() {
    const hash = window.location.hash.slice(1);
    const main = ["research", "account", "method"].includes(hash) ? hash
      : ["performance", "positions", "executions"].includes(hash) ? "account"
      : ["whitepaper", "principles", "methodology"].includes(hash) ? "method" : null;
    if (!main) return;
    activateTab("main", main);
    const target = byId(hash) || byId("workspace");
    window.requestAnimationFrame(() => target.scrollIntoView({ block: "start" }));
  }

  document.querySelectorAll("[data-tab-group]").forEach(bindTab);
  document.querySelectorAll("[data-open-research]").forEach((button) => button.addEventListener("click", () => activateTab("research", button.dataset.openResearch, true)));
  byId("refresh-dashboard").addEventListener("click", () => { void loadDashboard(); });
  window.addEventListener("hashchange", openHash);
  openHash();

  byId("copyright-year").textContent = String(new Date().getFullYear());
  byId("history-toggle").addEventListener("click", () => { historyExpanded = !historyExpanded; renderHistory(); });
  document.querySelectorAll("[data-range]").forEach((button) => {
    button.addEventListener("click", () => { range = button.dataset.range; renderChart(); });
  });
  window.addEventListener("online", () => { void loadDashboard(); });
  document.addEventListener("visibilitychange", () => { if (!document.hidden && Date.now() - lastAttemptAt >= 300000) void loadDashboard(); });
  window.setInterval(() => { if (!document.hidden) void loadDashboard(); }, 300000);
  let resizeTimer;
  window.addEventListener("resize", () => {
    window.clearTimeout(resizeTimer);
    resizeTimer = window.setTimeout(() => { if (dashboard?.status === "active") renderChart(); }, 150);
  });
  void loadDashboard();
})();
