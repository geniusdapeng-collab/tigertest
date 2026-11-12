export const round = value => Math.round((value + Number.EPSILON) * 1e8) / 1e8;
export function publicRecord(entry) {
  const r = entry.record;
  return {
    sequence: entry.sequence, hash: entry.hash, asOf: r.asOf,
    account: { currency: r.account.currency, nav: r.account.nav, cash: r.account.cash },
    positions: r.positions.map(p => ({ symbol: p.symbol, name: p.name, quantity: p.quantity, avgCost: p.avgCost, price: p.price, marketValue: p.marketValue, weight: round(p.marketValue / r.account.nav), unrealizedPnl: round(p.marketValue - p.quantity * p.avgCost) })),
    trades: r.trades.map(t => ({ id: t.id, executedAt: t.executedAt, symbol: t.symbol, side: t.side, quantity: t.quantity, price: t.price, fees: t.fees })),
    decisions: r.decisions.map(d => ({ id: d.id, symbol: d.symbol, action: d.action, targetWeight: d.targetWeight, thesis: d.thesis, decidedAt: d.decidedAt, runId: d.runId })),
  };
}
export function publicResearch(entry) {
  const r = entry.record;
  const output = { schemaVersion: r.schemaVersion, sequence: entry.sequence, hash: entry.hash, asOf: r.asOf, runId: r.runId, receiptHash: r.receiptHash, dataAsOf: r.dataAsOf, executionMode: r.executionMode, modelMode: r.modelMode, status: r.status, universeSize: r.universeSize, summary: r.summary, decisions: r.decisions.map(d => ({ id: d.id, symbol: d.symbol, action: d.action, targetWeight: d.targetWeight, thesis: d.thesis, decidedAt: d.decidedAt, runId: d.runId })) };
  if (r.schemaVersion === 2) {
    const m = r.model;
    output.model = { provider: m.provider, model: m.model, release: m.release, required: m.required, response_ids: [...m.response_ids], call_count: m.call_count, required_stages: [...m.required_stages], stage_counts: { ...m.stage_counts }, search_count: m.search_count, search_mode: m.search_mode, all_required_completed: m.all_required_completed };
    output.executionDecisions = r.executionDecisions.map(d => ({ symbol: d.symbol, action: d.action, target_weight: d.target_weight, action_summary: d.action_summary, why_this: d.why_this, why_now: d.why_now, entry_condition: d.entry_condition, invalidation: d.invalidation, risk_note: d.risk_note, sources: d.sources.map(s => ({ id: s.id, title: s.title, url: s.url, published_at: s.published_at })), market_evidence: d.market_evidence.map(m => ({ id: m.id, symbol: m.symbol, as_of: m.as_of, close: m.close, series_sha256: m.series_sha256 })), entry_price: d.entry_price, stop_price: d.stop_price, shares: d.shares, risk_usd: d.risk_usd, time_stop_days: d.time_stop_days, valid_for: d.valid_for, reference_capital_usd: d.reference_capital_usd }));
  }
  return output;
}
export function dashboard(entries, now = new Date().toISOString(), researchEntries = []) {
  const result = { schemaVersion: 1, status: entries.length ? 'active' : 'awaiting_first_record', generatedAt: now, baseline: null, latest: null, metrics: null, history: [], records: [], research: { latest: researchEntries.length ? publicResearch(researchEntries.at(-1)) : null, history: researchEntries.slice().reverse().map(publicResearch) } };
  if (!entries.length) return result;
  let unitValue = 1; let peak = 1;
  for (let i = 0; i < entries.length; i++) {
    const r = entries[i].record;
    if (i) unitValue *= (r.account.nav - r.netExternalFlow) / entries[i - 1].record.account.nav;
    peak = Math.max(peak, unitValue);
    result.history.push({ asOf: r.asOf, nav: r.account.nav, returnPct: round((unitValue - 1) * 100), drawdownPct: round((unitValue / peak - 1) * 100) });
  }
  const latest = publicRecord(entries.at(-1)); const last = result.history.at(-1);
  result.baseline = { date: entries[0].record.asOf, nav: entries[0].record.account.nav };
  result.latest = latest;
  result.metrics = { returnPct: last.returnPct, drawdownPct: last.drawdownPct, unrealizedPnl: round(latest.positions.reduce((sum, p) => sum + p.unrealizedPnl, 0)), positionCount: latest.positions.length, recordCount: entries.length, netExternalFlow: round(entries.slice(1).reduce((sum, entry) => sum + entry.record.netExternalFlow, 0)) };
  result.records = entries.slice(-20).reverse().map(publicRecord);
  return result;
}
