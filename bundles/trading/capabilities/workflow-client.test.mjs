import assert from "node:assert/strict";
import { existsSync } from "node:fs";
import { readFile, writeFile } from "node:fs/promises";
import { join } from "node:path";
import { Readable } from "node:stream";
import { test } from "node:test";
import {
  LIMITS, RESULT_SCHEMA, WorkflowError, exitCode, invokeWorkflow, parseJson, readRequestInput,
  runtimeEnvironment, safeEvidencePath, startupOptions, validateEnvelope, validateRequest, validateSchemaDefinition, waitForReceipt,
} from "./workflow-client.mjs";
import { REQUEST, ROOT, RUN_ID, eventually, result, runnerFixture } from "../../../scripts/tiger-agent-test-support.mjs";

test("B01/B02: exact operation schemas reject missing, unknown and wrongly typed fields", () => {
  for (const input of [null, [], "x", 0, true, {}, { ...REQUEST, mode: "paper" }, { ...REQUEST, profile: null },
    { ...REQUEST, idempotency_key: false }, { ...REQUEST, idempotency_key: "short" }, { ...REQUEST, idempotency_key: "a".repeat(97) }]) {
    assert.throws(() => validateRequest("run", input), { code: "INVALID_REQUEST" });
  }
  for (const key of ["a".repeat(8), "a".repeat(96), "key._-123"]) assert.equal(validateRequest("run", { ...REQUEST, idempotency_key: key }).idempotency_key, key);
  assert.throws(() => validateRequest("doctor", new Date()), { code: "INVALID_REQUEST" });
  for (const forbidden of [NaN, Infinity, -Infinity, undefined, 1n]) assert.throws(() => validateRequest("doctor", { x: forbidden }), { code: "INVALID_REQUEST" });
  assert.equal(Object.keys(RESULT_SCHEMA.properties).length, 12);
});

test("public resume and supplement schemas are exact and readonly-safe", async (t) => {
  const resume = { run_id: RUN_ID };
  const supplement = { schema_version: "tiger.research-supplement-request/v1",
    parent_run_id: RUN_ID, symbols: ["NVDA", "BRK.B"], idempotency_key: "supplement-key-0001" };
  assert.deepEqual(validateRequest("resume", resume), resume);
  assert.deepEqual(validateRequest("supplement", supplement), supplement);
  for (const value of [
    { ...resume, profile: "us-public-intelligence-paper-v2" },
    { ...supplement, symbols: [] },
    { ...supplement, symbols: ["NVDA", "NVDA"] },
    { ...supplement, symbols: ["nvda"] },
    { ...supplement, profile: "us-public-intelligence-paper-v2" },
  ]) assert.throws(() => validateRequest(value.symbols ? "supplement" : "resume", value), { code: "INVALID_REQUEST" });
  const fixture = await runnerFixture(t);
  for (const [operation, input] of [["resume", resume], ["supplement", supplement]]) {
    const denied = await invokeWorkflow(operation, input, { ...fixture.options, mode: "readonly" });
    assert.equal(denied.error.code, "READ_ONLY");
  }
  assert.deepEqual(await fixture.calls(), []);
  assert.equal(validateEnvelope(result("resume"), "resume").status, "running");
  assert.equal(validateEnvelope(result("supplement"), "supplement").status, "accepted");
});

test("B02: duplicate decoded keys, malformed JSON, depth, nonfinite numbers and invalid UTF-8 are rejected", () => {
  for (const raw of ['{"profile":1,"profile":2}', '{"a":{"mode":1,"mo\\u0064e":2}}', '{"arguments":{},"arguments":{}}',
    "NaN", "Infinity", "-Infinity", "1e400", "01", "1.", "{}{}", "[1,]", '{"a":1,}', " ", '"\\x20"', '"a\n"', "[".repeat(66) + "0" + "]".repeat(66)]) {
    assert.throws(() => parseJson(raw), { code: "INVALID_REQUEST" }, raw);
  }
  assert.throws(() => parseJson(Buffer.from([0xff, 0xfe])), { code: "INVALID_REQUEST" });
  assert.deepEqual(parseJson('{"a":{"key":1},"b":{"key":2},"literal":"{\\\"key\\\":1,\\\"key\\\":2}","中文":"好"}'),
    { a: { key: 1 }, b: { key: 2 }, literal: '{"key":1,"key":2}', 中文: "好" });
  const proto = parseJson('{"__proto__":{"polluted":true}}');
  assert.equal(Object.hasOwn(proto, "__proto__"), true);
  assert.equal({}.polluted, undefined);
});

test("B03: stream and oversized-file limits apply before parsing the entire input", async (t) => {
  const fixture = await runnerFixture(t);
  let reads = 0;
  const stream = Readable.from((async function* () { for (let i = 0; i < 1000; i += 1) { reads += 1; yield Buffer.alloc(1024, 32); } })());
  await assert.rejects(readRequestInput("-", stream), { code: "INPUT_TOO_LARGE" });
  assert.ok(reads < 70);
  let byteReads = 0;
  const byteStream = Readable.from((async function* () {
    while (byteReads <= LIMITS.inputBytes + 2) { byteReads += 1; yield Buffer.from(" "); }
  })());
  await assert.rejects(readRequestInput("-", byteStream), { code: "INPUT_TOO_LARGE" });
  assert.ok(byteReads <= LIMITS.inputBytes + 3);
  const file = join(fixture.dir, "large.json");
  await writeFile(file, Buffer.alloc(LIMITS.inputBytes + 1, 32));
  await assert.rejects(readRequestInput(file), { code: "INPUT_TOO_LARGE" });
  const valid = join(fixture.dir, "input.json"); await writeFile(valid, JSON.stringify(REQUEST));
  assert.deepEqual(await readRequestInput(valid), REQUEST);
  assert.deepEqual(await readRequestInput("-", Readable.from([Buffer.from('{"profile":"us-public-paper-v1"}')])) , { profile: "us-public-paper-v1" });
});

test("B05: run IDs and evidence references cannot select absolute, encoded or parent paths", () => {
  for (const run_id of ["../outside", "/etc/passwd", "run-a/b", "run-%2e%2e", "run-" + "A".repeat(32)]) assert.throws(() => validateRequest("receipt", { run_id }), { code: "INVALID_REQUEST" });
  for (const path of ["../canary", "/tmp/canary", "x/../canary", "x//y", "x\\y", "file:secret", "x/%2e%2e/y"]) {
    assert.equal(safeEvidencePath(path), false);
    assert.throws(() => validateEnvelope(result("receipt", { evidence_refs: [{ path, sha256: "b".repeat(64), bytes: 1, schema: "fixture/v1" }] }), "receipt", { run_id: RUN_ID }), { code: "RESULT_INVALID" });
  }
  assert.equal(safeEvidencePath("artifacts/结果.json"), true);
  assert.throws(() => validateEnvelope(result("status", { run_id: "run-" + "f".repeat(32) }), "status", { run_id: RUN_ID }), { code: "RESULT_INVALID" });
});

test("B06: unsupported schema keywords fail closed; implemented const is enforced", () => {
  for (const key of ["oneOf", "$ref", "allOf", "format", "patternProperties"]) assert.throws(() => validateSchemaDefinition({ type: "object", [key]: [] }), { code: "INVALID_REQUEST" });
  assert.throws(() => validateSchemaDefinition({ type: "object", additionalProperties: {} }), { code: "INVALID_REQUEST" });
  assert.throws(() => validateSchemaDefinition({ type: "object", required: ["absent"] }), { code: "INVALID_REQUEST" });
  assert.throws(() => validateRequest("describe", { profile: "other" }), { code: "INVALID_REQUEST" });
  assert.equal(validateSchemaDefinition(RESULT_SCHEMA), RESULT_SCHEMA);
});

test("P01/P02/P03: readonly and forbidden fields never spawn or create the state directory", async (t) => {
  const fixture = await runnerFixture(t);
  const denied = await invokeWorkflow("run", REQUEST, { ...fixture.options, mode: "readonly" });
  assert.equal(denied.error.code, "READ_ONLY"); assert.equal(exitCode(denied), 3);
  for (const key of ["mode", "root", "env", "module", "command", "argv", "live", "broker", "tune", "approve", "skip", "steps", "override", "verified", "completed_steps"]) {
    const rejected = await invokeWorkflow("run", { ...REQUEST, [key]: "attempt" }, fixture.options);
    assert.equal(rejected.error.code, "INVALID_REQUEST", key);
  }
  assert.deepEqual(await fixture.calls(), []); assert.equal(existsSync(fixture.stateRoot), false);
});

test("P04: fixed argv, code cwd and allowlisted environment do not inherit credentials or Python hooks", async (t) => {
  const fixture = await runnerFixture(t);
  const env = runtimeEnvironment({ PATH: "/bin", HOME: "/home/test", OPENAI_API_KEY: "fake", PYTHONPATH: "/untrusted", NODE_OPTIONS: "--require bad", WORKLOOM_KEYS_FILE: "/secret", HTTPS_PROXY: "http://127.0.0.1:7890" });
  assert.equal(env.HOME, "/home/test"); assert.equal(env.HTTPS_PROXY, "http://127.0.0.1:7890");
  assert.equal(env.OPENAI_API_KEY, undefined); assert.equal(env.PYTHONPATH, undefined); assert.equal(env.NODE_OPTIONS, undefined); assert.equal(env.WORKLOOM_KEYS_FILE, undefined);
  const response = await invokeWorkflow("doctor", {}, fixture.options);
  assert.equal(response.operation_ok, true);
  const [call] = await fixture.calls();
  assert.deepEqual(call.args, ["-B", "-m", "trading_system.agent_runner", "--mode", "paper", "--state-root", fixture.stateRoot, "doctor"]);
  assert.equal(call.cwd, ROOT);
  assert.equal(call.envNames.some((key) => /KEY|TOKEN|SECRET|PASSWORD|CREDENTIAL/u.test(key)), false);
  assert.equal(existsSync(fixture.stateRoot), false);
});

test("N06: accepted/recovered identity and status are distinct from verified receipt", async (t) => {
  const fixture = await runnerFixture(t, "send(result(operation, operation==='run'?{data:{idempotent_replay:true,observed_status:'completed'}}:{}));");
  const accepted = await invokeWorkflow("run", REQUEST, fixture.options);
  assert.equal(accepted.status, "accepted"); assert.equal(accepted.summary, null); assert.equal(accepted.verification, null);
  const receipt = await waitForReceipt(accepted.run_id, fixture.options);
  assert.equal(receipt.operation, "receipt"); assert.equal(receipt.verification.ok, true);
  assert.deepEqual((await fixture.calls()).map((call) => call.operation), ["run", "status", "receipt"]);
  for (const operation of ["run", "status", "doctor", "describe"]) assert.throws(() => validateEnvelope(result(operation, { summary: {} }), operation), { code: "RESULT_INVALID" });
  assert.throws(() => validateEnvelope(result("receipt", { verification: null }), "receipt"), { code: "RESULT_INVALID" });
});

test("E04/E05: exit0 fake business failure and nonzero success cannot become success", async (t) => {
  const fixtures = [
    ["process.stdout.write('finished successfully\\n');", "RESULT_INVALID"],
    ["send(result(operation,{status:'failed'}));", "RESULT_INVALID"],
    ["send(result(operation),7);", "RUNNER_FAILED"],
    ["send({...result(operation),extra:true});", "RESULT_INVALID"],
    ["send(result(operation,{operation:'status'}));", "RESULT_INVALID"],
    ["process.exitCode=0;", "RESULT_INVALID"],
  ];
  for (const [body, expected] of fixtures) {
    const fixture = await runnerFixture(t, body);
    const response = await invokeWorkflow("run", REQUEST, fixture.options);
    assert.equal(response.operation_ok, false); assert.equal(response.error.code, expected);
  }
});

test("E04: empty or mistyped summaries and unbound verify success cannot become completion", () => {
  const valid = result("receipt");
  for (const summary of [{}, { ...valid.summary, cash_usd: true }, { ...valid.summary, cash_usd: NaN },
    { ...valid.summary, positions: {} }, { ...valid.summary, as_of: "2026-10-20" },
    { ...valid.summary, as_of: {} }, { ...valid.summary, disclosures: { ...valid.summary.disclosures, synthetic: true } }]) {
    assert.throws(() => validateEnvelope({ ...valid, summary }, "receipt"), { code: "RESULT_INVALID" });
  }
  for (const changes of [{ run_id: null }, { execution_mode: null }, { status: "running" },
    { verification: { ...valid.verification, receipt_sha256: null } }, { summary: valid.summary }]) {
    assert.throws(() => validateEnvelope(result("verify", changes), "verify"), { code: "RESULT_INVALID" });
  }
  assert.equal(validateEnvelope(valid, "receipt"), valid);
});

test("Status query may report failed business state successfully, but wait and run do not", async (t) => {
  const fixture = await runnerFixture(t, "send(result(operation,{status:'failed',data:{observed_status:'failed'}}));");
  const status = await invokeWorkflow("status", { run_id: RUN_ID }, fixture.options);
  assert.equal(status.operation_ok, true); assert.equal(exitCode(status), 0); assert.equal(status.status, "failed");
  const wait = await waitForReceipt(RUN_ID, fixture.options);
  assert.equal(wait.operation_ok, false); assert.equal(wait.error.code, "RUN_INCOMPLETE"); assert.equal(wait.run_id, RUN_ID);
});

test("E11/B08: stdout/stderr floods and spawn ENOENT return bounded failures, never partial success", async (t) => {
  for (const [body, expected] of [
    ["process.stdout.write('x'.repeat(1100000));setInterval(()=>{},1000);", "RESULT_INVALID"],
    ["process.stderr.write('x'.repeat(70000));setInterval(()=>{},1000);", "RUNNER_FAILED"],
  ]) {
    const fixture = await runnerFixture(t, body);
    const response = await invokeWorkflow("doctor", {}, fixture.options);
    assert.equal(response.error.code, expected); assert.ok(JSON.stringify(response).length < 2048);
  }
  const missing = await invokeWorkflow("doctor", {}, { python: "/no-such-tiger-python", stateRoot: "/tmp/not-created-by-node" });
  assert.equal(missing.error.code, "RUNNER_FAILED");
});

test("B08: large valid evidence-reference lists remain complete and over-limit valid JSON is rejected whole", async (t) => {
  const large = await runnerFixture(t, "send(result(operation,{evidence_refs:Array.from({length:1500},(_,i)=>({path:'evidence/'+i+'.json',sha256:'b'.repeat(64),bytes:100,schema:'fixture/v1'}))}));");
  const receipt = await invokeWorkflow("receipt", { run_id: RUN_ID }, large.options);
  assert.equal(receipt.operation_ok, true); assert.equal(receipt.evidence_refs.length, 1500);
  assert.equal(receipt.evidence_refs.at(-1).path, "evidence/1499.json");
  const tooLarge = await runnerFixture(t, "send(result(operation,{data:{payload:'x'.repeat(1100000)}}));");
  const failed = await invokeWorkflow("status", { run_id: RUN_ID }, tooLarge.options);
  assert.equal(failed.operation_ok, false); assert.equal(failed.error.code, "RESULT_INVALID");
  assert.equal(failed.summary, null); assert.deepEqual(failed.data, {});
});

test("E12: domain errors preserve stable codes and redact credentials/URLs; invalid shapes fail closed", async (t) => {
  const fixture = await runnerFixture(t, "send(result(operation,{operation_ok:false,status:'failed',error:{code:'STEP_FAILED',message:'Bearer fake-token https://example.invalid/?secret=hidden',run_id:RUN_ID,stage:'fixture',retryable:false,details_ref:'errors/fixture.json'}}),8);");
  const response = await invokeWorkflow("run", REQUEST, fixture.options);
  assert.equal(response.error.code, "STEP_FAILED"); assert.equal(exitCode(response), 8);
  assert.doesNotMatch(response.error.message, /fake-token|example\.invalid|hidden/u);
  for (const code of [42, {}, "x".repeat(100)]) assert.throws(() => validateEnvelope(result("run", { operation_ok: false, status: "failed",
    error: { code, message: "bad", run_id: RUN_ID, stage: null, retryable: false, details_ref: null } }), "run"), { code: "RESULT_INVALID" });
  assert.throws(() => validateEnvelope(result("run", { operation_ok: false, status: "failed", error: { code: "STEP_FAILED", message: "x".repeat(513), run_id: RUN_ID, stage: null, retryable: false, details_ref: null } }), "run"), { code: "RESULT_INVALID" });
});

test("E06: timeout and cancellation clean request process groups and retain known run identity", async (t) => {
  const fixture = await runnerFixture(t, "process.on('SIGTERM',()=>{});setInterval(()=>{},1000);");
  const timeout = await invokeWorkflow("status", { run_id: RUN_ID }, fixture.options, { timeoutMs: 80 });
  assert.equal(timeout.error.code, "TIMEOUT"); assert.equal(timeout.run_id, RUN_ID);
  const firstCalls = (await fixture.calls()).length;
  const controller = new AbortController();
  t.after(() => controller.abort());
  const cancelled = invokeWorkflow("status", { run_id: RUN_ID }, fixture.options, { signal: controller.signal });
  await eventually(async () => (await fixture.calls()).length > firstCalls);
  controller.abort();
  const value = await cancelled;
  assert.equal(value.error.code, "CANCELLED"); assert.equal(value.run_id, RUN_ID);
  for (const { pid } of await fixture.calls()) assert.throws(() => process.kill(pid, 0), { code: "ESRCH" });
});

test("E06: TERM-exiting request does not leave a TERM-ignoring same-group helper", async (t) => {
  const fixture = await runnerFixture(t, `
const child=spawn(process.execPath,['-e',"process.on('SIGTERM',()=>{});setInterval(()=>{},1000)"],{stdio:'ignore'});
fs.writeFileSync(dir+'/helper.pid',String(child.pid));process.on('SIGTERM',()=>process.exit(0));setInterval(()=>{},1000);`);
  const controller = new AbortController();
  const pending = invokeWorkflow("doctor", {}, fixture.options, { signal: controller.signal });
  const file = join(fixture.dir, "helper.pid");
  await eventually(() => existsSync(file));
  const helperPid = Number(await readFile(file, "utf8"));
  controller.abort();
  const response = await pending; assert.equal(response.error.code, "CANCELLED");
  await eventually(() => { try { process.kill(helperPid, 0); return false; } catch (error) { return error.code === "ESRCH"; } }, { label: "helper termination" });
});

test("NP001/E06: completed request groups are cleaned even when result validation fails after close", async (t) => {
  for (const [name, output, expected] of [
    ["invalid JSON", "process.stdout.write('invalid protocol output\\n');", "RESULT_INVALID"],
    ["empty output", "", "RESULT_INVALID"],
    ["contradictory exit", "send(result(operation),7);", "RUNNER_FAILED"],
    ["valid success", "send(result(operation));", null],
  ]) await t.test(name, async (scenario) => {
    const fixture = await runnerFixture(scenario, `
const helper=spawn(process.execPath,['-e',"process.on('SIGTERM',()=>{});setInterval(()=>{},1000)"],{stdio:'ignore'});
helper.unref();fs.writeFileSync(dir+'/helper.pid',String(helper.pid));
${output}`);
    const response = await invokeWorkflow("doctor", {}, fixture.options);
    const helperPid = Number(await readFile(join(fixture.dir, "helper.pid"), "utf8"));
    scenario.after(() => { try { process.kill(helperPid, "SIGKILL"); } catch (error) { if (error.code !== "ESRCH") throw error; } });
    assert.equal(response.error?.code ?? null, expected);
    await eventually(() => { try { process.kill(helperPid, 0); return false; } catch (error) { return error.code === "ESRCH"; } },
      { timeout: 1500, label: "post-close helper termination" });
  });
});

test("NP001/E06: request cleanup preserves a worker that owns a separate session", async (t) => {
  for (const output of ["send(result(operation));", "process.stdout.write('response lost\\n');"]) {
    const fixture = await runnerFixture(t, `
const worker=spawn(process.execPath,['-e',"setInterval(()=>{},1000)"],{detached:true,stdio:'ignore'});
worker.unref();fs.writeFileSync(dir+'/worker.pid',String(worker.pid));
${output}`);
    const response = await invokeWorkflow("run", REQUEST, fixture.options);
    const workerPid = Number(await readFile(join(fixture.dir, "worker.pid"), "utf8"));
    t.after(() => { try { process.kill(-workerPid, "SIGKILL"); } catch (error) { if (error.code !== "ESRCH") throw error; } });
    assert.equal(response.operation_ok, output.startsWith("send"));
    assert.doesNotThrow(() => process.kill(workerPid, 0));
  }
});

test("wait cancellation/timeout never resubmits and keeps original run_id", async () => {
  const calls = [];
  const invoke = async (op) => { calls.push(op); return result(op, { status: "running" }); };
  const value = await waitForReceipt(RUN_ID, {}, { invoke, pollMs: 1, waitMs: 10 });
  assert.equal(value.error.code, "TIMEOUT"); assert.equal(value.run_id, RUN_ID);
  assert.ok(calls.length > 0); assert.ok(calls.every((op) => op === "status"));
});

test("startup overrides are constrained to trusted Python, absolute state root and access mode", () => {
  for (const options of [{ mode: "live" }, { stateRoot: "relative" }, { python: "--malicious" }, { env: {} }, { root: "/tmp" }]) assert.throws(() => startupOptions(options), { code: "INVALID_REQUEST" });
});
