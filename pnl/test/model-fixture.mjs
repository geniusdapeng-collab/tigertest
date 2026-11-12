import { digest } from '../lib/schema.mjs';

// Isolated, explicitly synthetic contract fixtures. Never use as publication input.
export const MODEL_PROFILE = 'us-public-model-required-paper-v1';
export const MODEL_STAGES = ['clean.llm_semantic', 'tech.sentiment', 'tech.risk', 'sector.narrative', 'decision.debate'];
export function modelFixture() {
  return {
    provider: 'deepseek', model: 'deepseek-flash', release: 'DeepSeek-V4.1-Flash-1017', required: true,
    response_ids: MODEL_STAGES.map((_, index) => `test-only-model-response-${index}`),
    call_count: MODEL_STAGES.length, required_stages: [...MODEL_STAGES],
    stage_counts: Object.fromEntries(MODEL_STAGES.map(stage => [stage, 1])),
    search_count: 14, search_mode: 'provider_native_search', all_required_completed: true,
  };
}
export function instructionFixture({ symbol = 'AAPL', action = 'watch', ...overrides } = {}) {
  const seriesHash = digest(`SYNTHETIC UNIT TEST SERIES ONLY ${symbol}`);
  return {
    symbol, action, target_weight: action === 'buy' ? 0.1 : 0,
    action_summary: '测试夹具：先等待，不构成真实交易指令。',
    why_this: '测试夹具：业务与板块证据需要继续确认。',
    why_now: '测试夹具：尚未出现可执行的入场条件。',
    entry_condition: '测试夹具：只有新模型决策和风险校验同时通过后才执行。',
    invalidation: '测试夹具：条件未成立则不执行；过期后重新评估。',
    risk_note: '测试夹具：新闻可能变化，止损成交价无法保证。',
    sources: [{ id: `ds_${digest('SYNTHETIC UNIT TEST SOURCE ONLY').slice(0, 24)}`, title: 'TEST SOURCE ONLY', url: 'https://example.com/test-source', published_at: '2026-01-02T12:00:00Z' }],
    market_evidence: [{ id: `md_${symbol}_20260102_${seriesHash.slice(0, 24)}`, symbol, as_of: '2026-01-02', close: 100, series_sha256: seriesHash }],
    entry_price: action === 'buy' ? 100 : null, stop_price: action === 'buy' ? 95 : null,
    shares: action === 'buy' ? 100 : null, risk_usd: action === 'buy' ? 500 : null,
    time_stop_days: action === 'buy' ? 10 : null,
    valid_for: '2026-01-05T16:00:00-05:00',
    reference_capital_usd: 100000,
    ...overrides,
  };
}
export function modelResearchFixture(overrides = {}) {
  const runId = overrides.runId ?? 'unit-test-model-research-run';
  const asOf = overrides.asOf ?? '2026-01-03T21:00:00Z';
  const details = overrides.executionDecisions ?? [instructionFixture()];
  return {
    schemaVersion: 2, idempotencyKey: 'model-required-unit-test-0001', asOf, runId,
    receiptHash: digest('UNIT TEST ONLY MODEL RECEIPT - NEVER REAL MODEL EVIDENCE'),
    dataAsOf: '2026-01-02', executionMode: 'paper', modelMode: 'enabled', status: 'completed', universeSize: 10,
    summary: '测试夹具：当前等待有效条件，保持现金；没有真实模型或实际成交。',
    decisions: details.map((item, index) => ({ id: `${runId}-${index}`, symbol: item.symbol, action: item.action, targetWeight: item.target_weight, thesis: item.action_summary, decidedAt: asOf, runId })),
    model: modelFixture(), executionDecisions: details,
    ...overrides,
  };
}
