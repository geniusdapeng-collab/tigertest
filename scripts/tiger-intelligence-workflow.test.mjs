import assert from "node:assert/strict";
import { test } from "node:test";
import { CURRENT_TECH_INTELLIGENCE_PROFILE, INTELLIGENCE_PROFILE, TECH_INTELLIGENCE_PROFILE,
  validateEnvelope, validateRequest, runtimeEnvironment,
  invokeWorkflow } from "../bundles/trading/capabilities/workflow-client.mjs";
import { INSTRUCTIONS, toolDefinitions } from "./tiger-agent-mcp.mjs";
import { REQUEST, RUN_ID, result, runnerFixture } from "./tiger-agent-test-support.mjs";

const stages = ["clean.llm_semantic", "tech.sentiment", "tech.risk", "sector.narrative", "decision.debate"];
function receipt() {
  const row = result("receipt");
  row.summary.disclosures.model_mode = "enabled";
  row.summary.disclosures.model_required = true;
  row.summary.disclosures.intelligence = { contract: "tiger.intelligence-acquisition.v2",
    bundle: { bundle_id: "bundle_" + "a".repeat(32), manifest_sha256: "b".repeat(64),
      usage: { requests: 27 }, state: "partial", missing_required: ["cell_fixture"] } };
  row.summary.model = { provider: "deepseek", model: "deepseek-flash", release: "DeepSeek-V4.1-Flash-1017",
    required: true, response_ids: stages.map((_, index) => `fixture-model-${index}`), call_count: 5,
    required_stages: stages, stage_counts: Object.fromEntries(stages.map(stage => [stage, 1])),
    search_count: 7, fetch_count: 0, search_mode: "intelligence_source_registry", all_required_completed: true,
    acquisition_contract: "tiger.intelligence-acquisition.v2", bundle_id: "bundle_" + "a".repeat(32), source_requests: 27 };
  row.summary.action_summary = "离线协议夹具：研究有缺口，等待核验。";
  row.summary.execution_decisions = [];
  return row;
}

test("v4 tech profile reaches CLI/MCP schema; v3 remains replay-compatible", () => {
  assert.equal(validateRequest("run", { ...REQUEST, profile: INTELLIGENCE_PROFILE }).profile, INTELLIGENCE_PROFILE);
  assert.equal(validateRequest("describe", { profile: TECH_INTELLIGENCE_PROFILE }).profile, TECH_INTELLIGENCE_PROFILE);
  assert.ok(INSTRUCTIONS.includes(CURRENT_TECH_INTELLIGENCE_PROFILE));
  const tool = toolDefinitions("paper").find(row => row.name === "tiger_workflow_run");
  assert.ok(tool.inputSchema.properties.profile.enum.includes(INTELLIGENCE_PROFILE));
  assert.ok(tool.inputSchema.properties.profile.enum.includes(TECH_INTELLIGENCE_PROFILE));
  for (const field of ["sources", "registry", "archive_root", "frontier", "model", "skip", "limits", "env"]) {
    assert.throws(() => validateRequest("run", { ...REQUEST, profile: INTELLIGENCE_PROFILE, [field]: {} }), { code: "INVALID_REQUEST" });
  }
});

test("v2 receipt permits partial research and variable source counts with complete mandatory model proof", () => {
  const row = receipt();
  assert.equal(validateEnvelope(row, "receipt", { run_id: RUN_ID }), row);
});

function receiptWithProofDiagnostics() {
  const row = receipt();
  const usage = { prompt_tokens: 20, completion_tokens: 10, total_tokens: 30 };
  const observation = (stage, index) => ({
    stage, call_id: index.toString(16).padStart(32, "0"), ordinal: 1,
    request_sha256: "a".repeat(64),
    observation_ref: { path: `snapshots/model-${index}.json`, schema: "fixture-observation", bytes: 240, sha256: "b".repeat(64) },
    response_id: `fixture-model-${index}`, diagnostic: null, usage,
  });
  row.summary.model.chat_accounting = {
    accounting_scope: "chat_completions_only", source_accounting: "not_certified_by_this_report",
    other_model_io_count: 7, native_count: 6, observed_attempts_count: 6, known_native_attempts: 5,
    known_usage: { prompt_tokens: 100, completion_tokens: 50, total_tokens: 150 },
    unknown_attempts: 1, observation_state: "usage_unknown", requires_reconciliation: true, format_reanswers: 0,
    observed_attempts: [...stages.map(observation), {
      ...observation(stages[0], 5), response_id: null, diagnostic: { code: "MODEL_TIMEOUT" }, usage: null,
    }],
  };
  row.summary.model.research_work = {
    quarantined_attempts: [{
      attempt_id: "attempt-" + "1".repeat(64), unit_id: "unit-fixture", scope: "model",
      affected_targets: ["global:macro"], usage_state: "unknown", reservation_state: "held",
      reason: "MODEL_TIMEOUT", event_sha256: "c".repeat(64),
    }],
    quarantined_targets: ["global:macro"],
  };
  return row;
}

test("verified paper receipt transports native accounting and quarantine disclosures", async (t) => {
  const expected = receiptWithProofDiagnostics();
  const fixture = await runnerFixture(t, `send(${JSON.stringify(expected)});`);
  const receipt = await invokeWorkflow("receipt", { run_id: RUN_ID }, { ...fixture.options, mode: "readonly" });
  assert.deepEqual(receipt, expected);
});

for (const [name, change] of [
  ["an unrecognized model member", model => { model.unrecognized = {}; }],
  ["accounting without required fields", model => { model.chat_accounting = {}; }],
  ["unstructured observations", model => { model.chat_accounting.observed_attempts = ["not-an-observation"]; }],
  ["a malformed quarantine reservation", model => { model.research_work.quarantined_attempts[0].reservation_state = 7; }],
  ["quarantine targets that are not identifiers", model => { model.research_work.quarantined_targets = [false]; }],
]) {
  test(`receipt rejects ${name}`, () => {
    const row = receiptWithProofDiagnostics();
    change(row.summary.model);
    assert.throws(() => validateEnvelope(row, "receipt", { run_id: RUN_ID }), { code: "RESULT_INVALID" });
  });
}

for (const [name, change] of [
  ["no acquisition", row => { delete row.summary.disclosures.intelligence; }],
  ["missing bundle", row => { delete row.summary.disclosures.intelligence.bundle; }],
  ["different frozen bundle", row => { row.summary.model.bundle_id = "bundle_" + "c".repeat(32); }],
  ["unbound source usage", row => { row.summary.model.source_requests += 1; }],
  ["no source usage", row => { delete row.summary.model.source_requests; }],
  ["missing acquisition contract", row => { delete row.summary.model.acquisition_contract; }],
  ["arbitrary manifest", row => { row.summary.disclosures.intelligence.bundle.manifest_sha256 = "unverified"; }],
  ["absent required model stage", row => { row.summary.model.stage_counts["tech.risk"] = 0; }],
  ["degraded model result", row => { row.status = "completed_degraded"; }],
  ["unknown model override", row => { row.summary.model.model = "another-model"; }],
]) {
  test(`v2 receipt rejects ${name}`, () => {
    const row = receipt(); change(row);
    assert.throws(() => validateEnvelope(row, "receipt", { run_id: RUN_ID }), { code: "RESULT_INVALID" });
  });
}

test("new trusted source configuration is inherited only by model-required run", async t => {
  const fixture = await runnerFixture(t);
  const settings = { LLM_API_KEY: "fixture-key", TIGER_INTEL_REGISTRY_FILE: "/fixture/registry.json",
    TIGER_INTEL_ARCHIVE_ROOT: "/fixture/archive", TIGER_INTEL_BRAVE_API_KEY: "fixture-brave",
    TIGER_INTEL_SEC_USER_AGENT: "Fixture research contact@example.test" };
  const before = Object.fromEntries(Object.keys(settings).map(key => [key, process.env[key]]));
  Object.assign(process.env, settings);
  t.after(() => { for (const [key, value] of Object.entries(before)) {
    if (value === undefined) delete process.env[key]; else process.env[key] = value;
  } });
  await invokeWorkflow("run", { ...REQUEST, profile: INTELLIGENCE_PROFILE }, fixture.options);
  await invokeWorkflow("run", { ...REQUEST, profile: TECH_INTELLIGENCE_PROFILE }, fixture.options);
  await invokeWorkflow("run", REQUEST, fixture.options);
  await invokeWorkflow("describe", { profile: INTELLIGENCE_PROFILE }, fixture.options);
  const calls = await fixture.calls();
  for (const key of Object.keys(settings)) {
    assert.deepEqual(calls.map(row => row.envNames.includes(key)), [true, true, false, false]);
    assert.equal(runtimeEnvironment(settings)[key], undefined);
  }
});

test("v2 readonly rejects before invoking Python or reading source credentials", async t => {
  const fixture = await runnerFixture(t);
  const row = await invokeWorkflow("run", { ...REQUEST, profile: INTELLIGENCE_PROFILE }, { ...fixture.options, mode: "readonly" });
  assert.equal(row.error.code, "READ_ONLY");
  assert.deepEqual(await fixture.calls(), []);
});
