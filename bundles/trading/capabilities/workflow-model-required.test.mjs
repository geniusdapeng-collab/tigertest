import assert from "node:assert/strict";
import { test } from "node:test";
import { REQUIRED_PROFILE, runtimeEnvironment, validateRequest, validateEnvelope, invokeWorkflow, exitCode } from "./workflow-client.mjs";
import { REQUEST, RUN_ID, result, runnerFixture } from "../../../scripts/tiger-agent-test-support.mjs";

const stages = ["clean.llm_semantic", "tech.sentiment", "tech.risk", "sector.narrative", "decision.debate"];
function modelReceipt() {
  const receipt = result("receipt");
  receipt.summary.disclosures.model_mode = "enabled";
  receipt.summary.disclosures.model_required = true;
  receipt.summary.model = { provider: "deepseek", model: "deepseek-flash", release: "DeepSeek-V4.1-Flash-1017",
    required: true, response_ids: stages.map((_, index) => `fixture-response-${index}`), call_count: 5,
    required_stages: [...stages], stage_counts: Object.fromEntries(stages.map(stage => [stage, 1])), search_count: 14,
    search_mode: "provider_native_search", all_required_completed: true };
  receipt.summary.action_summary = "传输夹具：等待，不代表真实研究。";
  receipt.summary.execution_decisions = [];
  return receipt;
}

function marketReceipt(action = "watch") {
  const receipt = modelReceipt();
  receipt.summary.as_of.market_date = "2026-10-26";
  receipt.summary.as_of.benchmark_last_bar = "2026-10-22";
  receipt.summary.execution_decisions = [{ symbol: "AAA", action,
    action_summary: "测试夹具：等待条件满足", why_this: "已观察行情", why_now: "公司新闻不足，仍须观察",
    entry_condition: "全部条件通过后重新研究", invalidation: "价格结构失效时取消", risk_note: "公司新闻缺失且存在价格风险",
    market_evidence: [{ id: "md_AAA_20261023_" + "a".repeat(24), symbol: "AAA", as_of: "2026-10-23",
      close: 123.45, series_sha256: "a".repeat(64) }],
    target_weight: action === "buy" ? .12 : 0, entry_price: action === "buy" ? 120 : null,
    stop_price: action === "buy" ? 115 : null, shares: action === "buy" ? 100 : null,
    risk_usd: action === "buy" ? 500 : null, time_stop_days: action === "buy" ? 5 : null,
    valid_for: "2026-10-27T16:00:00-04:00", reference_capital_usd: 100000,
    sources: action === "buy" ? [{ id: "ds_" + "b".repeat(24), title: "TEST_FIXTURE 原生文章",
      url: "https://www.reuters.com/fixture", published_at: null }] : [] }];
  return receipt;
}

test("required profile is explicit and cannot take provider, credential or model overrides", () => {
  assert.equal(validateRequest("run", { ...REQUEST, profile: REQUIRED_PROFILE }).profile, REQUIRED_PROFILE);
  assert.equal(validateRequest("describe", { profile: REQUIRED_PROFILE }).profile, REQUIRED_PROFILE);
  for (const key of ["LLM_API_KEY", "TIGER_SEC_CONTACT", "model", "provider", "model_mode", "search", "env"]) {
    assert.throws(() => validateRequest("run", { ...REQUEST, profile: REQUIRED_PROFILE, [key]: "override" }), { code: "INVALID_REQUEST" });
  }
});

test("trusted required startup keeps model settings and SEC contact while stripping unrelated secrets", () => {
  const env = { HOME: "/fixture-home", PATH: "/bin", LLM_BACKEND: "api", LLM_BASE_URL: "https://api.deepseek.com",
    LLM_MODEL: "deepseek-flash", LLM_API_KEY: "fixture-only", TIGER_SEC_CONTACT: "research@example.test",
    OPENAI_API_KEY: "other-key", TIGER_PNL_API_TOKEN: "pnl-key", PYTHONPATH: "/untrusted" };
  assert.equal(runtimeEnvironment(env).LLM_API_KEY, undefined);
  assert.equal(runtimeEnvironment(env).TIGER_SEC_CONTACT, undefined);
  const required = runtimeEnvironment(env, true);
  assert.equal(required.LLM_API_KEY, "fixture-only");
  assert.equal(required.LLM_MODEL, "deepseek-flash");
  assert.equal(required.TIGER_SEC_CONTACT, "research@example.test");
  assert.equal(required.OPENAI_API_KEY, undefined);
  assert.equal(required.TIGER_PNL_API_TOKEN, undefined);
  assert.equal(required.PYTHONPATH, undefined);
});

test("trusted runtime policy can reach readonly inspection without forwarding model credentials or accepting API paths", async t => {
  const fixture = await runnerFixture(t);
  const key = "TIGER_AGENT_RUNTIME_POLICY_FILE";
  const previous = process.env[key];
  process.env[key] = "/fixture-config/runtime-5400.json";
  t.after(() => { if (previous === undefined) delete process.env[key]; else process.env[key] = previous; });
  const inspection = runtimeEnvironment({ [key]: process.env[key], LLM_API_KEY: "fixture-model-key" });
  assert.equal(inspection[key], process.env[key]);
  assert.equal(inspection.LLM_API_KEY, undefined);
  await invokeWorkflow("doctor", {}, fixture.options);
  await invokeWorkflow("describe", { profile: REQUIRED_PROFILE }, fixture.options);
  assert.deepEqual((await fixture.calls()).map(call => call.envNames.includes(key)), [true, true]);
  for (const field of ["runtime_policy", "runtime_policy_file", "runtime_input", key]) {
    assert.throws(() => validateRequest("run", { ...REQUEST, [field]: "/untrusted-policy.json" }), { code: "INVALID_REQUEST" });
  }
});

test("required startup environment is selected by the fixed profile and run operation", async t => {
  const fixture = await runnerFixture(t);
  const settings = { LLM_BACKEND: "api", LLM_BASE_URL: "https://api.deepseek.com",
    LLM_MODEL: "deepseek-flash", LLM_API_KEY: "fixture-only", TIGER_SEC_CONTACT: "research@example.test" };
  const old = Object.fromEntries(Object.keys(settings).map(key => [key, process.env[key]]));
  Object.assign(process.env, settings);
  t.after(() => { for (const [key, value] of Object.entries(old)) { if (value === undefined) delete process.env[key]; else process.env[key] = value; } });
  await invokeWorkflow("run", { ...REQUEST, profile: REQUIRED_PROFILE }, fixture.options);
  await invokeWorkflow("run", REQUEST, fixture.options);
  await invokeWorkflow("describe", { profile: REQUIRED_PROFILE }, fixture.options);
  const calls = await fixture.calls();
  assert.equal(calls[0].envNames.includes("LLM_API_KEY"), true);
  assert.equal(calls[1].envNames.includes("LLM_API_KEY"), false);
  assert.equal(calls[2].envNames.includes("LLM_API_KEY"), false);
  assert.deepEqual(calls.map(call => call.envNames.includes("TIGER_SEC_CONTACT")), [true, false, false]);
});

test("required receipt accepts only completed, exact fixed model and complete unique stage proof", () => {
  const receipt = modelReceipt();
  assert.equal(validateEnvelope(receipt, "receipt", { run_id: RUN_ID }), receipt);
  for (const mutate of [
    row => { row.status = "completed_degraded"; },
    row => { delete row.summary.model; },
    row => { row.summary.model.model = "deepseek-reasoner"; },
    row => { row.summary.model.all_required_completed = false; },
    row => { row.summary.model.response_ids[1] = row.summary.model.response_ids[0]; },
    row => { row.summary.model.stage_counts[stages[0]] = 0; },
    row => { row.summary.model.required_stages.reverse(); },
    row => { row.summary.model.search_count = 0; },
    row => { row.summary.disclosures.model_required = false; },
    row => { delete row.summary.action_summary; },
    row => { delete row.summary.execution_decisions; },
  ]) {
    const invalid = modelReceipt(); mutate(invalid);
    assert.throws(() => validateEnvelope(invalid, "receipt", { run_id: RUN_ID }), { code: "RESULT_INVALID" });
  }
  assert.equal(exitCode({ operation_ok: false, error: { code: "MODEL_REQUIRED_FAILED" } }), 8);
});

test("legacy disabled receipts keep their historical completed_degraded contract", () => {
  const legacy = result("receipt", { status: "completed_degraded" });
  assert.equal(validateEnvelope(legacy, "receipt", { run_id: RUN_ID }), legacy);
});

test("market-only WATCH and market-plus-article BUY preserve distinct observed dates", () => {
  for (const action of ["watch", "buy"]) {
    const receipt = marketReceipt(action);
    assert.equal(validateEnvelope(receipt, "receipt", { run_id: RUN_ID }), receipt);
    assert.equal(receipt.summary.execution_decisions[0].market_evidence[0].as_of, "2026-10-23");
  }
});

for (const [name, mutate] of [
  ["missing market", row => { delete row.market_evidence; }],
  ["empty market", row => { row.market_evidence = []; }],
  ["duplicate market", row => { row.market_evidence.push(structuredClone(row.market_evidence[0])); }],
  ["unknown market key", row => { row.market_evidence[0].provider = "invented"; }],
  ["missing series hash", row => { delete row.market_evidence[0].series_sha256; }],
  ["other stock snapshot", row => { row.market_evidence[0].symbol = "BBB"; }],
  ["arbitrary market id", row => { row.market_evidence[0].id = "md_AAA_20261023_" + "b".repeat(24); }],
  ["id date mismatch", row => { row.market_evidence[0].as_of = "2026-10-22"; }],
  ["future market date", row => { row.market_evidence[0].as_of = "2026-10-27"; row.market_evidence[0].id = "md_AAA_20261027_" + "a".repeat(24); }],
  ["impossible market date", row => { row.market_evidence[0].as_of = "2026-02-30"; row.market_evidence[0].id = "md_AAA_20260230_" + "a".repeat(24); }],
  ["year-zero market date", row => { row.market_evidence[0].as_of = "0000-10-23"; row.market_evidence[0].id = "md_AAA_00001023_" + "a".repeat(24); }],
  ["zero market price", row => { row.market_evidence[0].close = 0; }],
  ["negative market price", row => { row.market_evidence[0].close = -1; }],
  ["nonfinite market price", row => { row.market_evidence[0].close = Infinity; }],
  ["boolean market price", row => { row.market_evidence[0].close = true; }],
  ["string market price", row => { row.market_evidence[0].close = "123.45"; }],
  ["zero series hash", row => { row.market_evidence[0].series_sha256 = "0".repeat(64); }],
  ["uppercase series hash", row => { row.market_evidence[0].series_sha256 = "A".repeat(64); }],
  ["market pretending article", row => { row.sources = [{ id: row.market_evidence[0].id, title: "fake", url: "https://www.reuters.com/fixture", published_at: null }]; }],
  ["buy lacks article", row => { row.action = "buy"; }],
  ["unknown instruction key", row => { row.provider = "invented"; }],
  ["missing qualitative reason", row => { delete row.why_this; }],
  ["html qualitative reason", row => { row.why_this = "<p>unsafe</p>"; }],
  ["blank qualitative reason", row => { row.why_now = " "; }],
  ["watch allocation", row => { row.target_weight = .1; }],
  ["watch numeric entry", row => { row.entry_price = 123; }],
  ["watch time stop", row => { row.time_stop_days = 5; }],
  ["zero reference capital", row => { row.reference_capital_usd = 0; }],
  ["invalid plan expiry", row => { row.valid_for = "not-a-date"; }],
  ["permissive date plan expiry", row => { row.valid_for = "42"; }],
  ["impossible plan expiry", row => { row.valid_for = "2027-02-30T16:00:00-05:00"; }],
  ["nonfuture plan expiry", row => { row.valid_for = "2026-10-26T16:00:00-04:00"; }],
  ["sell extension", row => { row.action = "sell"; }],
  ["hold extension", row => { row.action = "hold"; }],
]) {
  test(`required receipt rejects ${name}`, () => {
    const receipt = marketReceipt(); mutate(receipt.summary.execution_decisions[0]);
    assert.throws(() => validateEnvelope(receipt, "receipt", { run_id: RUN_ID }), { code: "RESULT_INVALID" });
  });
}

test("duplicate decisions or native article IDs and over-eight articles are rejected", () => {
  const duplicateDecision = marketReceipt();
  duplicateDecision.summary.execution_decisions.push(structuredClone(duplicateDecision.summary.execution_decisions[0]));
  assert.throws(() => validateEnvelope(duplicateDecision, "receipt", { run_id: RUN_ID }), { code: "RESULT_INVALID" });
  const duplicateArticle = marketReceipt("buy");
  duplicateArticle.summary.execution_decisions[0].sources.push(structuredClone(duplicateArticle.summary.execution_decisions[0].sources[0]));
  assert.throws(() => validateEnvelope(duplicateArticle, "receipt", { run_id: RUN_ID }), { code: "RESULT_INVALID" });
  const overLimit = marketReceipt("buy");
  overLimit.summary.execution_decisions[0].sources = Array.from({ length: 9 }, (_, index) => ({
    ...overLimit.summary.execution_decisions[0].sources[0], id: "ds_" + index.toString(16).padStart(24, "0") }));
  assert.throws(() => validateEnvelope(overLimit, "receipt", { run_id: RUN_ID }), { code: "RESULT_INVALID" });
});

test("required assessment date rejects year zero even when no candidates exist", () => {
  const receipt = modelReceipt();
  receipt.summary.as_of.market_date = "0000-10-26";
  assert.throws(() => validateEnvelope(receipt, "receipt", { run_id: RUN_ID }), { code: "RESULT_INVALID" });
});

test("buy instructions require positive bounded facts from the approved plan", () => {
  for (const [key, value] of [["target_weight", 0], ["entry_price", null], ["stop_price", 0],
    ["stop_price", 121], ["shares", 1.5], ["shares", 0], ["risk_usd", 0], ["time_stop_days", 1.5]]) {
    const receipt = marketReceipt("buy"); receipt.summary.execution_decisions[0][key] = value;
    assert.throws(() => validateEnvelope(receipt, "receipt", { run_id: RUN_ID }), { code: "RESULT_INVALID" });
  }
});
