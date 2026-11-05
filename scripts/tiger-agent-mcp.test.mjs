import assert from "node:assert/strict";
import { existsSync } from "node:fs";
import { test } from "node:test";
import { LIMITS, PROFILE, REQUEST_SCHEMAS, RESULT_SCHEMA } from "../bundles/trading/capabilities/workflow-client.mjs";
import { toolDefinitions, validateToolDefinitions } from "./tiger-agent-mcp.mjs";
import { REQUEST, RUN_ID, eventually, mcpProcess, runnerFixture } from "./tiger-agent-test-support.mjs";

function launch(t, fixture, mode = "paper") {
  const proc = mcpProcess(["--mode", mode, "--python", fixture.python, "--state-root", fixture.stateRoot]);
  t.after(() => { if (proc.child.exitCode === null) proc.child.kill("SIGTERM"); });
  return proc;
}
function call(proc, id, name, args) {
  proc.send({ jsonrpc: "2.0", id, method: "tools/call", params: { name, arguments: args } });
  return proc.response(id);
}
function toolEnvelope(response, failed = false) {
  assert.equal(response.error, undefined);
  assert.equal(response.result.isError, failed);
  assert.deepEqual(JSON.parse(response.result.content[0].text), response.result.structuredContent);
  return response.result.structuredContent;
}

test("MCP N05/P01: initialize/probe/list are side-effect free; readonly hides and rejects run", async (t) => {
  const fixture = await runnerFixture(t);
  for (let attempt = 0; attempt < 2; attempt += 1) {
    const proc = launch(t, fixture, "readonly");
    const initialized = await proc.initialize();
    assert.match(initialized.result.instructions.slice(0, 512), /accepted\/running status is not completion/u);
    proc.send({ jsonrpc: "2.0", id: "list", method: "tools/list", params: {} });
    const listed = (await proc.response("list")).result.tools;
    assert.equal(listed.length, 5); assert.equal(listed.some((tool) => tool.name === "tiger_workflow_run"), false);
    assert.ok(listed.every((tool) => tool.outputSchema.required.length === 12));
    const denied = toolEnvelope(await call(proc, "run", "tiger_workflow_run", REQUEST), true);
    assert.equal(denied.error.code, "READ_ONLY");
    assert.equal((await proc.stop()).code, 0);
  }
  assert.deepEqual(await fixture.calls(), []); assert.equal(existsSync(fixture.stateRoot), false);
});

test("MCP discovery: short instructions and tool metadata explain new research without changing execution contracts", async (t) => {
  const expectedOutput = structuredClone(RESULT_SCHEMA); delete expectedOutput.$defs;
  const expectedNames = ["tiger_doctor", "tiger_workflow_describe", "tiger_workflow_run",
    "tiger_workflow_status", "tiger_workflow_receipt", "tiger_workflow_verify"];
  assert.equal(PROFILE, "us-public-paper-v1", "The legacy CLI default remains a compatibility contract");
  for (const mode of ["readonly", "paper"]) await t.test(mode, async (scenario) => {
    const fixture = await runnerFixture(scenario); const proc = launch(scenario, fixture, mode);
    const initialized = await proc.initialize();
    const prefix = initialized.result.instructions.slice(0, 512);
    const paragraph = initialized.result.instructions.split("\n\n")[0];
    assert.ok(paragraph.length <= 512); assert.match(paragraph.trim(), /[.!?]$/u);
    assert.match(prefix, /us-public-intelligence-paper-v2/u); assert.match(prefix, /public-usd/u);
    assert.match(prefix, /idempotency_key/u); assert.match(prefix, /run_id/u);
    assert.match(prefix, /accepted\/running (?:status )?is not completion/u);
    assert.match(prefix, /receipt.*verify/u); assert.match(prefix, /completed.*verified receipt/u);
    assert.match(prefix, /Cancellation.*observation, not.*worker/u);
    assert.match(initialized.result.instructions, /Run is hidden and rejected in readonly deployment/u);
    proc.send({ jsonrpc: "2.0", id: "metadata", method: "tools/list" });
    const tools = (await proc.response("metadata")).result.tools;
    assert.deepEqual(tools.map((tool) => tool.name), expectedNames.filter((name) => mode === "paper" || name !== "tiger_workflow_run"));
    assert.equal(new Set(tools.map((tool) => tool.title)).size, tools.length);
    for (const tool of tools) {
      assert.equal(typeof tool.title, "string"); assert.ok(tool.title.trim().length > 0 && tool.title.length <= 64);
      const operation = tool.name === "tiger_doctor" ? "doctor" : tool.name.slice("tiger_workflow_".length);
      assert.deepEqual(tool.inputSchema, REQUEST_SCHEMAS[operation], "Discovery metadata cannot change accepted arguments");
      assert.deepEqual(tool.outputSchema, expectedOutput, "The result envelope remains unchanged");
      assert.deepEqual(tool.annotations, { readOnlyHint: operation !== "run", destructiveHint: false,
        idempotentHint: true, openWorldHint: operation === "run" });
    }
    assert.match(tools.find((tool) => tool.name === "tiger_workflow_describe").description,
      /\{"profile":"us-public-intelligence-paper-v2"\}/u);
    if (mode === "paper") {
      const description = tools.find((tool) => tool.name === "tiger_workflow_run").description;
      for (const field of REQUEST_SCHEMAS.run.required) assert.ok(description.includes(field), `Run explains ${field}`);
      for (const phrase of ["us-public-intelligence-paper-v2", "identical request/key", "run_id", "receipt and verify"]) {
        assert.ok(description.includes(phrase), `Run discovery retains ${phrase}`);
      }
    }
    assert.equal((await proc.stop()).code, 0);
    assert.deepEqual(await fixture.calls(), []); assert.equal(existsSync(fixture.stateRoot), false);
  });
});

test("MCP E01/E02: lifecycle requires initialized notification and rejects malformed/repeated initialize", async (t) => {
  const fixture = await runnerFixture(t); const proc = launch(t, fixture);
  assert.equal((await call(proc, 1, "tiger_doctor", {})).error.code, -32002);
  proc.send({ jsonrpc: "2.0", id: 2, method: "initialize", params: { protocolVersion: 3, clientInfo: {}, capabilities: {} } });
  assert.equal((await proc.response(2)).error.code, -32602);
  proc.send({ jsonrpc: "2.0", id: 3, method: "initialize", params: { protocolVersion: "2099-01-01", clientInfo: { name: "fixture", version: "1" }, capabilities: {} } });
  assert.equal((await proc.response(3)).result.protocolVersion, "2025-11-25");
  assert.equal((await call(proc, 4, "tiger_doctor", {})).error.code, -32002);
  proc.send({ jsonrpc: "2.0", method: "notifications/initialized" });
  proc.send({ jsonrpc: "2.0", id: 5, method: "initialize", params: {} });
  assert.equal((await proc.response(5)).error.code, -32600);
  await proc.stop(); assert.deepEqual(await fixture.calls(), []);
});

test("MCP E03/B04: JSON-RPC errors are protocol errors and notifications have no replies", async (t) => {
  const fixture = await runnerFixture(t); const proc = launch(t, fixture); await proc.initialize();
  proc.send({ jsonrpc: "2.0", id: 1, method: "unknown", params: {} });
  assert.equal((await proc.response(1)).error.code, -32601);
  assert.equal((await call(proc, 2, "missing_tool", {})).error.code, -32602);
  proc.send({ jsonrpc: "2.0", id: 3, method: "tools/call", params: [] });
  assert.equal((await proc.response(3)).error.code, -32600);
  proc.send({ jsonrpc: "2.0", id: true, method: "ping" });
  assert.equal((await proc.response(null)).error.code, -32600);
  proc.child.stdin.write('not JSON\n');
  assert.equal((await proc.response(null, 2)).error.code, -32700);
  proc.child.stdin.write('{"jsonrpc":"2.0","id":4,"method":"ping","method":"tools/call"}\n');
  assert.equal((await proc.response(null, 3)).error.code, -32700);
  proc.send({ jsonrpc: "2.0", method: "unknown", params: {} });
  proc.send({ jsonrpc: "2.0", method: "tools/call", params: [] });
  const closed = await proc.stop(); assert.equal(closed.code, 0);
  assert.equal(proc.messages.length, 7); assert.deepEqual(await fixture.calls(), []);
});

test("MCP B04: chunked UTF-8, several frames per chunk and blank lines preserve framing", async (t) => {
  const fixture = await runnerFixture(t); const proc = launch(t, fixture);
  const initial = Buffer.from(JSON.stringify({ jsonrpc: "2.0", id: "init", method: "initialize", params: {
    protocolVersion: "2025-11-25", capabilities: {}, clientInfo: { name: "中文客户端", version: "1" } } }) + "\n");
  const offset = initial.indexOf(Buffer.from("中")) + 1;
  proc.child.stdin.write(initial.subarray(0, offset)); proc.child.stdin.write(initial.subarray(offset));
  await proc.response("init");
  proc.child.stdin.write('\n \r\n{"jsonrpc":"2.0","method":"notifications/initialized"}\n{"jsonrpc":"2.0","id":1,"method":"ping"}\n{"jsonrpc":"2.0","id":2,"method":"tools/list"}\n');
  assert.deepEqual((await proc.response(1)).result, {});
  assert.equal((await proc.response(2)).result.tools.length, 6);
  await proc.stop(); assert.deepEqual(await fixture.calls(), []);
});

test("MCP B03/B04: oversized unterminated frame closes with bounded protocol output", async (t) => {
  const fixture = await runnerFixture(t); const proc = launch(t, fixture);
  proc.child.stdin.on("error", () => {});
  proc.child.stdin.write(Buffer.alloc(LIMITS.frameBytes + 1, 32));
  const closed = await proc.closed;
  assert.equal(closed.code, 2); assert.ok(closed.stdout.length < 1024);
  assert.equal(JSON.parse(closed.stdout).error.code, -32600); assert.deepEqual(await fixture.calls(), []);
});

test("MCP B06: duplicate tools and unsupported schemas cannot silently overwrite definitions", () => {
  const tools = toolDefinitions("paper"); assert.equal(new Set(tools.map((tool) => tool.name)).size, 6);
  assert.throws(() => validateToolDefinitions([...tools, tools[0]]), { code: "INVALID_REQUEST" });
  assert.throws(() => validateToolDefinitions([{ ...tools[0], inputSchema: { oneOf: [] } }]), { code: "INVALID_REQUEST" });
});

test("MCP B07/E06: duplicate IDs/overload do not spawn again; ping/cancellation stay responsive", async (t) => {
  const fixture = await runnerFixture(t, "process.on('SIGTERM',()=>{});setInterval(()=>{},1000);");
  const proc = launch(t, fixture); await proc.initialize();
  const request = (id) => ({ jsonrpc: "2.0", id, method: "tools/call", params: { name: "tiger_doctor", arguments: {} } });
  proc.send(request(1)); await eventually(async () => (await fixture.calls()).length === 1);
  proc.send(request(1)); assert.equal((await proc.response(1)).error.code, -32600);
  for (const id of [2, 3, 4]) proc.send(request(id));
  proc.send(request(5)); assert.equal((await proc.response(5)).error.code, -32000);
  proc.send({ jsonrpc: "2.0", id: "ping", method: "ping" }); assert.deepEqual((await proc.response("ping")).result, {});
  await eventually(async () => (await fixture.calls()).length === 4);
  for (const requestId of [1, 2, 3, 4]) proc.send({ jsonrpc: "2.0", method: "notifications/cancelled", params: { requestId, reason: "fixture cancellation" } });
  assert.equal(toolEnvelope(await proc.response(1, 2), true).error.code, "CANCELLED");
  for (const id of [2, 3, 4]) assert.equal(toolEnvelope(await proc.response(id), true).error.code, "CANCELLED");
  await proc.stop(); assert.equal((await fixture.calls()).length, 4);
  for (const call of await fixture.calls()) assert.throws(() => process.kill(call.pid, 0), { code: "ESRCH" });
});

test("MCP E04/E05/E12: tool error envelope agrees across text/structuredContent/isError", async (t) => {
  for (const body of ["send(result(operation),7);", "send(result(operation,{status:'failed'}));", "process.stdout.write('success');",
    "send(result(operation,{operation_ok:false,status:'failed',error:{code:{bad:true}}}));"]) {
    const fixture = await runnerFixture(t, body); const proc = launch(t, fixture); await proc.initialize();
    const value = toolEnvelope(await call(proc, 1, "tiger_workflow_run", REQUEST), true);
    assert.equal(value.operation_ok, false); assert.ok(["RESULT_INVALID", "RUNNER_FAILED"].includes(value.error.code));
    await proc.stop();
  }
});

test("MCP N06: accepted, failed status query, verified receipt and unknown arguments remain distinct", async (t) => {
  const fixture = await runnerFixture(t, "send(result(operation,operation==='status'?{status:'failed'}:{}));");
  const proc = launch(t, fixture); await proc.initialize();
  const accepted = toolEnvelope(await call(proc, 1, "tiger_workflow_run", REQUEST)); assert.equal(accepted.summary, null);
  const status = toolEnvelope(await call(proc, 2, "tiger_workflow_status", { run_id: RUN_ID })); assert.equal(status.status, "failed");
  const receipt = toolEnvelope(await call(proc, 3, "tiger_workflow_receipt", { run_id: RUN_ID })); assert.equal(receipt.verification.ok, true);
  const bad = toolEnvelope(await call(proc, 4, "tiger_doctor", null), true); assert.equal(bad.error.code, "INVALID_REQUEST");
  const injected = toolEnvelope(await call(proc, 5, "tiger_workflow_run", { ...REQUEST, mode: "paper", env: {} }), true); assert.equal(injected.error.code, "INVALID_REQUEST");
  await proc.stop(); assert.equal((await fixture.calls()).length, 3);
});

test("MCP E06: stdin EOF and SIGTERM abort and reap only in-flight query processes", async (t) => {
  for (const cause of ["eof", "signal"]) {
    const fixture = await runnerFixture(t, "process.on('SIGTERM',()=>{});setInterval(()=>{},1000);");
    const proc = launch(t, fixture); await proc.initialize();
    proc.send({ jsonrpc: "2.0", id: 1, method: "tools/call", params: { name: "tiger_workflow_status", arguments: { run_id: RUN_ID } } });
    await eventually(async () => (await fixture.calls()).length === 1);
    if (cause === "eof") proc.child.stdin.end(); else proc.child.kill("SIGTERM");
    const closed = await proc.closed; assert.equal(closed.code, cause === "eof" ? 0 : 130);
    const value = toolEnvelope(proc.messages.find((message) => message.id === 1), true); assert.equal(value.error.code, "CANCELLED"); assert.equal(value.run_id, RUN_ID);
    for (const call of await fixture.calls()) assert.throws(() => process.kill(call.pid, 0), { code: "ESRCH" });
  }
});

test("MCP E13: closing stdout while a call finishes shuts the server down without an uncaught error", async (t) => {
  const fixture = await runnerFixture(t, "setTimeout(()=>send(result(operation)),150);");
  const proc = launch(t, fixture); await proc.initialize();
  proc.send({ jsonrpc: "2.0", id: 1, method: "tools/call", params: { name: "tiger_doctor", arguments: {} } });
  await eventually(async () => (await fixture.calls()).length === 1);
  proc.child.stdout.destroy();
  const closed = await proc.closed; assert.equal(closed.code, 130, closed.stderr); assert.doesNotMatch(closed.stderr, /Unhandled|Error:/u);
});

test("NP003/E01: standard metadata on initialize and initialized reaches ready without business I/O", async (t) => {
  const fixture = await runnerFixture(t); const proc = launch(t, fixture);
  proc.send({ jsonrpc: "2.0", id: "init-meta", method: "initialize", params: {
    protocolVersion: "2025-11-25", capabilities: {}, clientInfo: { name: "fixture", version: "1" },
    _meta: { progressToken: "init-progress", "example.com/trace": "fixture" } } });
  assert.ok((await proc.response("init-meta")).result);
  proc.send({ jsonrpc: "2.0", method: "notifications/initialized", params: { _meta: { "example.com/trace": "fixture" } } });
  proc.send({ jsonrpc: "2.0", id: "list", method: "tools/list" });
  assert.equal((await proc.response("list")).result?.tools.length, 6);
  await proc.stop(); assert.deepEqual(await fixture.calls(), []);
});

test("NP003/B04: ping and tools/list accept standard request metadata", async (t) => {
  for (const method of ["ping", "tools/list"]) await t.test(method, async (scenario) => {
    const fixture = await runnerFixture(scenario); const proc = launch(scenario, fixture); await proc.initialize();
    proc.send({ jsonrpc: "2.0", id: "meta", method, params: { _meta: { progressToken: 7, "example.com/trace": "fixture" } } });
    const response = await proc.response("meta"); assert.equal(response.error, undefined);
    if (method === "ping") assert.deepEqual(response.result, {}); else assert.equal(response.result.tools.length, 6);
    await proc.stop(); assert.deepEqual(await fixture.calls(), []);
  });
});

test("NP003/E06: standard cancellation metadata aborts and reaps only the requested observer", async (t) => {
  const fixture = await runnerFixture(t, "process.on('SIGTERM',()=>{});setInterval(()=>{},1000);");
  const proc = launch(t, fixture); await proc.initialize();
  proc.send({ jsonrpc: "2.0", id: "pending", method: "tools/call", params: { name: "tiger_doctor", arguments: {},
    _meta: { progressToken: "work-progress", "example.com/trace": "fixture" } } });
  await eventually(async () => (await fixture.calls()).length === 1);
  for (const _meta of [null, [], "bad", 2]) {
    proc.send({ jsonrpc: "2.0", method: "notifications/cancelled", params: { requestId: "pending", _meta } });
    proc.send({ jsonrpc: "2.0", id: `ping-${JSON.stringify(_meta)}`, method: "ping" });
    await proc.response(`ping-${JSON.stringify(_meta)}`);
    assert.equal(proc.messages.some((message) => message.id === "pending"), false);
  }
  proc.send({ jsonrpc: "2.0", method: "notifications/cancelled", params: { requestId: "pending", reason: "fixture", _meta: { "example.com/trace": "fixture" } } });
  const response = await eventually(() => proc.messages.find((message) => message.id === "pending"), { timeout: 1500, label: "metadata cancellation" });
  assert.equal(toolEnvelope(response, true).error.code, "CANCELLED");
  await proc.stop();
  for (const entry of await fixture.calls()) assert.throws(() => process.kill(entry.pid, 0), { code: "ESRCH" });
});

test("NP003/B02: malformed metadata and business fields remain rejected without business I/O", async (t) => {
  const fixture = await runnerFixture(t); const proc = launch(t, fixture);
  for (const [_meta, index] of [null, [], "bad", 2, { progressToken: false }].map((value, index) => [value, index])) {
    proc.send({ jsonrpc: "2.0", id: `bad-init-${index}`, method: "initialize", params: {
      protocolVersion: "2025-11-25", capabilities: {}, clientInfo: { name: "fixture", version: "1" }, _meta } });
    assert.equal((await proc.response(`bad-init-${index}`)).error?.code, -32602);
  }
  proc.send({ jsonrpc: "2.0", id: "init", method: "initialize", params: {
    protocolVersion: "2025-11-25", capabilities: {}, clientInfo: { name: "fixture", version: "1" } } });
  assert.ok((await proc.response("init")).result);
  for (const [params, index] of [{ _meta: null }, { _meta: [] }, { _meta: "bad" }, { _meta: 2 }, { _meta: {}, mode: "paper" }].map((value, index) => [value, index])) {
    proc.send({ jsonrpc: "2.0", method: "notifications/initialized", params });
    proc.send({ jsonrpc: "2.0", id: `awaiting-${index}`, method: "tools/list" });
    assert.equal((await proc.response(`awaiting-${index}`)).error?.code, -32002);
  }
  proc.send({ jsonrpc: "2.0", method: "notifications/initialized" });
  for (const method of ["ping", "tools/list", "tools/call"]) {
    for (const [_meta, index] of [null, [], "bad", 2, { progressToken: false }].map((value, index) => [value, index])) {
      proc.send({ jsonrpc: "2.0", id: `${method}-${index}`, method, params: {
        ...(method === "tools/call" ? { name: "tiger_doctor", arguments: {} } : {}), _meta } });
      assert.equal((await proc.response(`${method}-${index}`)).error?.code, -32602);
    }
  }
  const rejected = toolEnvelope(await call(proc, "business-meta", "tiger_doctor", { _meta: {} }), true);
  assert.equal(rejected.error.code, "INVALID_REQUEST");
  await proc.stop(); assert.deepEqual(await fixture.calls(), []);
});
