import { createHash } from 'node:crypto';

export class PnlError extends Error {
  constructor(status, code, message) { super(message); this.status = status; this.code = code; }
}

export const fail = (message, code = 'INVALID_RECORD') => { throw new PnlError(422, code, message); };
export const digest = value => createHash('sha256').update(typeof value === 'string' ? value : canonical(value)).digest('hex');
export function canonical(value) {
  if (Array.isArray(value)) return `[${value.map(canonical).join(',')}]`;
  if (value !== null && typeof value === 'object') return `{${Object.keys(value).sort().map(key => `${JSON.stringify(key)}:${canonical(value[key])}`).join(',')}}`;
  return JSON.stringify(value);
}
function shape(value, keys, path) {
  if (!value || Array.isArray(value) || typeof value !== 'object' || Object.getPrototypeOf(value) !== Object.prototype) fail(`${path} must be an object`);
  if (Object.keys(value).length !== keys.length || keys.some(key => !Object.hasOwn(value, key))) fail(`${path} has missing or unsupported fields`);
}
function text(value, path, max = 200, pattern) {
  if (typeof value !== 'string' || !value.length || value.length > max || value.trim() !== value || /[\u0000-\u001f\u007f<>]/u.test(value) || (pattern && !pattern.test(value))) fail(`${path} must be valid plain text`);
}
function number(value, path, min = 0, max = 1e15) {
  if (typeof value !== 'number' || !Number.isFinite(value) || value < min || value > max) fail(`${path} is outside its numeric range`);
}
function time(value, path) {
  text(value, path, 40, /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,3})?(?:Z|[+-]\d{2}:\d{2})$/);
  if (!Number.isFinite(Date.parse(value))) fail(`${path} must be an ISO timestamp with timezone`);
  // Date.parse normalizes impossible calendar dates; compare the literal date separately.
  const day = new Date(`${value.slice(0, 10)}T00:00:00Z`);
  if (day.toISOString().slice(0, 10) !== value.slice(0, 10)) fail(`${path} has an invalid calendar date`);
}
function list(value, path, max) { if (!Array.isArray(value) || value.length > max) fail(`${path} must be an array of at most ${max} items`); }
function unique(items, key, path) { if (new Set(items.map(item => item[key])).size !== items.length) fail(`${path} has duplicate ${key}`); }
const symbol = (value, path) => text(value, path, 20, /^[A-Z0-9][A-Z0-9.^=-]*$/);
export const validKey = value => typeof value === 'string' && /^[A-Za-z0-9][A-Za-z0-9_.:-]{7,127}$/.test(value);
const near = (a, b) => Math.abs(a - b) <= Math.max(0.02, Math.abs(b) * 1e-8);

export const MODEL_PROFILE = 'us-public-model-required-paper-v1';
export const REQUIRED_MODEL_STAGES = Object.freeze(['clean.llm_semantic', 'tech.sentiment', 'tech.risk', 'sector.narrative', 'decision.debate']);
export function validateModelEvidence(model) {
  shape(model, ['provider', 'model', 'release', 'required', 'response_ids', 'call_count', 'required_stages', 'stage_counts', 'search_count', 'search_mode', 'all_required_completed'], 'model');
  if (model.provider !== 'deepseek' || model.model !== 'deepseek-flash' || model.release !== 'DeepSeek-V4.1-Flash-1017' || model.required !== true || model.all_required_completed !== true || model.search_mode !== 'provider_native_search') fail('A completed required DeepSeek model assessment is required', 'MODEL_REQUIRED');
  list(model.required_stages, 'model.required_stages', REQUIRED_MODEL_STAGES.length);
  if (model.required_stages.length !== REQUIRED_MODEL_STAGES.length || new Set(model.required_stages).size !== REQUIRED_MODEL_STAGES.length || REQUIRED_MODEL_STAGES.some(stage => !model.required_stages.includes(stage))) fail('Every required semantic stage must be completed by the model', 'MODEL_REQUIRED');
  shape(model.stage_counts, REQUIRED_MODEL_STAGES, 'model.stage_counts');
  for (const stage of REQUIRED_MODEL_STAGES) {
    number(model.stage_counts[stage], `model.stage_counts.${stage}`, 1, 100);
    if (!Number.isSafeInteger(model.stage_counts[stage])) fail('Model stage counts must be integers', 'MODEL_REQUIRED');
  }
  number(model.call_count, 'model.call_count', REQUIRED_MODEL_STAGES.length, 500);
  number(model.search_count, 'model.search_count', 14, 14);
  if (!Number.isSafeInteger(model.call_count) || !Number.isSafeInteger(model.search_count) || model.call_count !== Object.values(model.stage_counts).reduce((sum, count) => sum + count, 0)) fail('Model response counts must reconcile with completed stages', 'MODEL_REQUIRED');
  list(model.response_ids, 'model.response_ids', 500);
  if (model.response_ids.length !== model.call_count || new Set(model.response_ids).size !== model.call_count) fail('Every completed model response requires a unique provider identity', 'MODEL_REQUIRED');
  for (const id of model.response_ids) text(id, 'model.response_id', 160, /^[A-Za-z0-9][A-Za-z0-9_.:-]*$/);
  return model;
}
function validateSources(sources) {
  list(sources, 'instruction.sources', 8);
  for (const source of sources) {
    shape(source, ['id', 'title', 'url', 'published_at'], 'instruction.source');
    text(source.id, 'instruction.source.id', 100); text(source.title, 'instruction.source.title', 500); text(source.url, 'instruction.source.url', 2048);
    if (source.id.startsWith('md_')) fail('Market evidence cannot be presented as a news article');
    let url;
    try { url = new URL(source.url); } catch { fail('Source URL is invalid'); }
    if (!['https:', 'http:'].includes(url.protocol) || !url.hostname || url.username || url.password || url.hash || [...url.searchParams.keys()].some(key => /token|key|signature|authorization|credential|secret|password/i.test(key.replace(/[_-]/g, '')))) fail('Source URLs must be public links without credentials');
    if (source.published_at !== null) {
      text(source.published_at, 'instruction.source.published_at', 80);
      if (!Number.isFinite(Date.parse(source.published_at))) fail('Source publication time is invalid');
    }
  }
  unique(sources, 'id', 'instruction.sources');
}
function validateMarketEvidence(evidence, ownSymbol, assessmentDate) {
  list(evidence, 'instruction.market_evidence', 1);
  if (evidence.length !== 1) fail('Every instruction requires its own frozen stock market snapshot');
  const snapshot = evidence[0];
  shape(snapshot, ['id', 'symbol', 'as_of', 'close', 'series_sha256'], 'instruction.market_evidence');
  symbol(snapshot.symbol, 'instruction.market_evidence.symbol');
  if (snapshot.symbol !== ownSymbol) fail('Market snapshot must match the instruction symbol');
  text(snapshot.as_of, 'instruction.market_evidence.as_of', 10, /^\d{4}-\d{2}-\d{2}$/);
  if (snapshot.as_of.startsWith('0000-')) fail('Market snapshot requires a valid calendar year');
  time(`${snapshot.as_of}T00:00:00Z`, 'instruction.market_evidence.as_of');
  if (snapshot.as_of > assessmentDate) fail('Market snapshot cannot be after the New York assessment date');
  number(snapshot.close, 'instruction.market_evidence.close', Number.MIN_VALUE, Number.MAX_VALUE);
  text(snapshot.series_sha256, 'instruction.market_evidence.series_sha256', 64, /^[a-f0-9]{64}$/);
  if (/^0+$/.test(snapshot.series_sha256)) fail('Market series hash cannot be a placeholder');
  const expectedId = `md_${snapshot.symbol}_${snapshot.as_of.replaceAll('-', '')}_${snapshot.series_sha256.slice(0, 24)}`;
  if (snapshot.id !== expectedId) fail('Market evidence ID must bind its symbol, date and frozen series hash');
}
function validateExecutionDecisions(instructions, decisions, asOf) {
  list(instructions, 'executionDecisions', 500);
  if (instructions.length !== decisions.length) fail('Model instructions must match the published decision set');
  const parts = Object.fromEntries(new Intl.DateTimeFormat('en-US', { timeZone: 'America/New_York', year: 'numeric', month: '2-digit', day: '2-digit' }).formatToParts(new Date(asOf)).map(({ type, value }) => [type, value]));
  const assessmentDate = `${parts.year}-${parts.month}-${parts.day}`;
  for (const item of instructions) {
    shape(item, ['symbol', 'action', 'target_weight', 'action_summary', 'why_this', 'why_now', 'entry_condition', 'invalidation', 'risk_note', 'sources', 'market_evidence', 'entry_price', 'stop_price', 'shares', 'risk_usd', 'time_stop_days', 'valid_for', 'reference_capital_usd'], 'executionDecision');
    symbol(item.symbol, 'executionDecision.symbol');
    if (!['buy', 'watch'].includes(item.action)) fail('Required-model instructions support buy or watch only');
    const decision = decisions.find(row => row.symbol === item.symbol);
    if (!decision || decision.action !== item.action || decision.targetWeight !== item.target_weight) fail('Execution instruction differs from the published action or allocation');
    for (const key of ['action_summary', 'why_this', 'why_now', 'entry_condition', 'invalidation', 'risk_note']) text(item[key], `executionDecision.${key}`, 2000);
    time(item.valid_for, 'executionDecision.valid_for');
    validateSources(item.sources);
    validateMarketEvidence(item.market_evidence, item.symbol, assessmentDate);
    for (const key of ['entry_price', 'stop_price', 'shares', 'risk_usd', 'time_stop_days', 'reference_capital_usd']) {
      if (item[key] !== null) number(item[key], `executionDecision.${key}`, Number.MIN_VALUE);
    }
    if ((item.shares !== null && !Number.isSafeInteger(item.shares)) || (item.time_stop_days !== null && !Number.isSafeInteger(item.time_stop_days))) fail('Shares and time stop must be whole numbers');
    if (item.action === 'buy' && (!item.sources.length || item.target_weight <= 0 || item.entry_price === null || item.stop_price === null || item.shares === null || item.risk_usd === null || item.reference_capital_usd === null || item.stop_price >= item.entry_price)) fail('Buy instructions need news evidence and verified entry, stop, size and risk facts');
    if (item.action === 'watch' && (item.target_weight !== 0 || ['entry_price', 'stop_price', 'shares', 'risk_usd', 'time_stop_days'].some(key => item[key] !== null))) fail('Watch instructions require a zero target and no executable numeric plan');
  }
  unique(instructions, 'symbol', 'executionDecisions');
}

function validateDecisions(decisions, asOf) {
  list(decisions, 'decisions', 500);
  for (const d of decisions) {
    shape(d, ['id', 'symbol', 'action', 'targetWeight', 'thesis', 'decidedAt', 'runId'], 'decision');
    text(d.id, 'decision.id', 128); symbol(d.symbol, 'decision.symbol');
    if (!['buy', 'sell', 'hold', 'watch'].includes(d.action)) fail('decision.action is invalid');
    number(d.targetWeight, 'decision.targetWeight', 0, 1); text(d.thesis, 'decision.thesis', 2000);
    time(d.decidedAt, 'decision.decidedAt'); text(d.runId, 'decision.runId', 128);
    if (Date.parse(d.decidedAt) > Date.parse(asOf)) fail('decision.decidedAt must not be after asOf');
  }
  unique(decisions, 'id', 'decisions');
  if (decisions.reduce((sum, d) => sum + d.targetWeight, 0) > 1.000001) fail('decision target weights must sum to at most 1');
}

export function validateRecord(record, now = Date.now()) {
  shape(record, ['schemaVersion', 'idempotencyKey', 'asOf', 'account', 'positions', 'trades', 'decisions', 'netExternalFlow', 'provenance'], 'record');
  if (record.schemaVersion !== 1) fail('schemaVersion must be 1');
  if (!validKey(record.idempotencyKey)) fail('idempotencyKey must be 8-128 safe characters');
  time(record.asOf, 'asOf');
  if (Date.parse(record.asOf) > now + 5 * 60_000) fail('asOf cannot be in the future');
  shape(record.account, ['label', 'currency', 'nav', 'cash'], 'account');
  text(record.account.label, 'account.label', 80);
  if (record.account.currency !== 'USD') fail('Only USD accounts are supported');
  number(record.account.nav, 'account.nav', 0.01);
  number(record.account.cash, 'account.cash');
  number(record.netExternalFlow, 'netExternalFlow', -1e15);
  list(record.positions, 'positions', 500);
  for (const p of record.positions) {
    shape(p, ['symbol', 'name', 'quantity', 'avgCost', 'price', 'marketValue'], 'position');
    symbol(p.symbol, 'position.symbol'); text(p.name, 'position.name', 120);
    number(p.quantity, 'position.quantity', Number.MIN_VALUE, 1e12);
    number(p.avgCost, 'position.avgCost'); number(p.price, 'position.price', Number.MIN_VALUE);
    number(p.marketValue, 'position.marketValue', Number.MIN_VALUE);
    if (!near(p.marketValue, p.quantity * p.price)) fail('position.marketValue must equal quantity × price');
  }
  unique(record.positions, 'symbol', 'positions');
  if (!near(record.account.nav, record.account.cash + record.positions.reduce((sum, p) => sum + p.marketValue, 0))) fail('account.nav must equal cash plus position market values');
  list(record.trades, 'trades', 2000);
  for (const trade of record.trades) {
    shape(trade, ['id', 'executedAt', 'symbol', 'side', 'quantity', 'price', 'fees'], 'trade');
    text(trade.id, 'trade.id', 128); time(trade.executedAt, 'trade.executedAt'); symbol(trade.symbol, 'trade.symbol');
    if (!['buy', 'sell'].includes(trade.side)) fail('trade.side must be buy or sell');
    number(trade.quantity, 'trade.quantity', Number.MIN_VALUE, 1e12); number(trade.price, 'trade.price', Number.MIN_VALUE); number(trade.fees, 'trade.fees');
    if (Date.parse(trade.executedAt) > Date.parse(record.asOf)) fail('trade.executedAt must not be after asOf');
  }
  unique(record.trades, 'id', 'trades');
  validateDecisions(record.decisions, record.asOf);
  shape(record.provenance, ['kind', 'evidenceSha256', 'sourceRef', 'attestedBy'], 'provenance');
  if (!['broker_statement', 'operator_attested'].includes(record.provenance.kind)) fail('Only real account evidence is accepted', 'REAL_EVIDENCE_REQUIRED');
  text(record.provenance.evidenceSha256, 'provenance.evidenceSha256', 64, /^[a-f0-9]{64}$/);
  if (/^0+$/.test(record.provenance.evidenceSha256)) fail('Evidence hash cannot be a placeholder', 'REAL_EVIDENCE_REQUIRED');
  text(record.provenance.sourceRef, 'provenance.sourceRef', 200); text(record.provenance.attestedBy, 'provenance.attestedBy', 120);
  if (/(?:^|[\s/_.-])(?:paper|sim|simulated|simulation|mock|demo|synthetic)(?:$|[\s/_.-])/i.test(record.provenance.sourceRef)) fail('Paper or simulated sources cannot be published as real accounts', 'REAL_EVIDENCE_REQUIRED');
  return record;
}

export function validateResearch(record, now = Date.now()) {
  const fields = ['schemaVersion', 'idempotencyKey', 'asOf', 'runId', 'receiptHash', 'dataAsOf', 'executionMode', 'modelMode', 'status', 'universeSize', 'summary', 'decisions'];
  shape(record, record?.schemaVersion === 2 ? [...fields, 'model', 'executionDecisions'] : fields, 'research');
  if (![1, 2].includes(record.schemaVersion)) fail('Research schemaVersion must be 1 or 2');
  if (!validKey(record.idempotencyKey)) fail('idempotencyKey must be 8-128 safe characters');
  time(record.asOf, 'asOf');
  if (Date.parse(record.asOf) > now + 5 * 60_000) fail('asOf cannot be in the future');
  text(record.runId, 'runId', 128); text(record.receiptHash, 'receiptHash', 64, /^[a-f0-9]{64}$/);
  if (/^0+$/.test(record.receiptHash)) fail('receiptHash cannot be a placeholder');
  if (typeof record.dataAsOf === 'string' && /^\d{4}-\d{2}-\d{2}$/.test(record.dataAsOf)) time(`${record.dataAsOf}T00:00:00Z`, 'dataAsOf');
  else time(record.dataAsOf, 'dataAsOf');
  if (Date.parse(record.dataAsOf) > Date.parse(record.asOf)) fail('dataAsOf cannot be after asOf');
  if (record.executionMode !== 'paper') fail('Research must disclose the controlled paper workflow');
  if (!['disabled', 'enabled', 'passthrough'].includes(record.modelMode)) fail('modelMode is invalid');
  if (!['completed', 'completed_degraded'].includes(record.status)) fail('Only independently verified completed research may be published', 'RESEARCH_NOT_COMPLETED');
  number(record.universeSize, 'universeSize', 0, 100000);
  if (!Number.isInteger(record.universeSize)) fail('universeSize must be an integer');
  text(record.summary, 'summary', 10000); validateDecisions(record.decisions, record.asOf);
  if (record.decisions.some(d => d.runId !== record.runId)) fail('Research decisions must refer to this verified runId');
  if (record.schemaVersion === 2) {
    if (record.modelMode !== 'enabled' || record.status !== 'completed') fail('Only fully completed required models can produce execution instructions', 'MODEL_REQUIRED');
    validateModelEvidence(record.model);
    validateExecutionDecisions(record.executionDecisions, record.decisions, record.asOf);
  }
  return record;
}
