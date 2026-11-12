import assert from "node:assert/strict";
import { existsSync } from "node:fs";
import { writeFile } from "node:fs/promises";
import { join } from "node:path";
import { test } from "node:test";
import { REQUEST, RUN_ID, collectProcess, eventually, runnerFixture } from "./tiger-agent-test-support.mjs";
import { parseArgs } from "./tiger-agent.mjs";

function launch(fixture, operation, extra = []) {
  return collectProcess("scripts/tiger-agent.mjs", [operation, "--python", fixture.python,
    "--state-root", fixture.stateRoot, "--mode", "paper", ...extra]);
}
function envelope(output, expectedExit) {
  assert.equal(output.code, expectedExit, output.stderr);
  assert.equal(output.stdout.trim().split("\n").length, 1);
  return JSON.parse(output.stdout);
}

test("CLI doctor/describe use fixed defaults and emit exactly one JSON envelope", async (t) => {
  const fixture = await runnerFixture(t);
  for (const operation of ["doctor", "describe"]) {
    const proc = launch(fixture, operation); proc.child.stdin.end();
    const value = envelope(await proc.closed, 0);
    assert.equal(value.operation, operation); assert.equal(value.summary, null);
  }
  assert.deepEqual((await fixture.calls()).map((call) => call.input), [{}, { profile: "us-tech-intelligence-paper-v3" }]);
  assert.equal(existsSync(fixture.stateRoot), false);
});

test("CLI P01: readonly rejects every paper mutation before opening input or spawning Python", async (t) => {
  const fixture = await runnerFixture(t);
  for (const operation of ["run", "resume", "supplement", "cancel"]) {
    const proc = collectProcess("scripts/tiger-agent.mjs", [operation, "--python", fixture.python,
      "--state-root", fixture.stateRoot, "--input-file", join(fixture.dir, "absent.json")]);
    proc.child.stdin.end();
    assert.equal(envelope(await proc.closed, 3).error.code, "READ_ONLY");
  }
  assert.deepEqual(await fixture.calls(), []); assert.equal(existsSync(fixture.stateRoot), false);
});

test("CLI resume observes the original run and supplement waits on its new identity", async (t) => {
  const fixture = await runnerFixture(t);
  const resume = launch(fixture, "resume", ["--input-file", "-"]);
  resume.child.stdin.end(JSON.stringify({ run_id: RUN_ID }));
  const resumed = envelope(await resume.closed, 0);
  assert.equal(resumed.operation, "resume"); assert.equal(resumed.status, "running");
  assert.equal(resumed.run_id, RUN_ID);
  const supplement = launch(fixture, "supplement", ["--input-file", "-", "--wait"]);
  supplement.child.stdin.end(JSON.stringify({
    schema_version: "tiger.research-supplement-request/v1",
    parent_run_id: RUN_ID,
    symbols: ["NVDA"],
    idempotency_key: "supplement-cli-0001",
  }));
  const completed = envelope(await supplement.closed, 0);
  assert.equal(completed.operation, "receipt"); assert.equal(completed.verification.ok, true);
  assert.deepEqual((await fixture.calls()).map((call) => call.operation),
    ["resume", "supplement", "status", "receipt"]);
});

test("CLI cancel forwards only the original run identity", async (t) => {
  const fixture = await runnerFixture(t);
  const proc = launch(fixture, "cancel", ["--input-file", "-"]);
  proc.child.stdin.end(JSON.stringify({ run_id: RUN_ID }));
  const value = envelope(await proc.closed, 0);
  assert.equal(value.operation, "cancel");
  assert.equal(value.status, "cancelled");
  assert.equal(value.run_id, RUN_ID);
  assert.deepEqual((await fixture.calls()).map((call) => [call.operation, call.input]),
    [["cancel", { run_id: RUN_ID }]]);
});

test("CLI N06: run --wait submits once, polls original identity, then returns verified receipt", async (t) => {
  const fixture = await runnerFixture(t);
  const proc = launch(fixture, "run", ["--input-file", "-", "--wait"]);
  proc.child.stdin.end(JSON.stringify(REQUEST));
  const value = envelope(await proc.closed, 0);
  assert.equal(value.operation, "receipt"); assert.equal(value.run_id, RUN_ID); assert.equal(value.verification.ok, true);
  assert.deepEqual((await fixture.calls()).map((call) => call.operation), ["run", "status", "receipt"]);
});

test("CLI B01/P02/P03: malformed/duplicate/unknown request data and arbitrary flags fail before runner", async (t) => {
  const fixture = await runnerFixture(t);
  for (const raw of ['null', '{}', '{"profile":1,"profile":2}', JSON.stringify({ ...REQUEST, skip: ["risk"] }), '1e400']) {
    const proc = launch(fixture, "run", ["--input-file", "-"]); proc.child.stdin.end(raw);
    assert.equal(envelope(await proc.closed, 2).error.code, "INVALID_REQUEST");
  }
  for (const args of [["doctor", "--env", "a=b"], ["status", "--wait"], ["run", "--wait-timeout-seconds", "4"],
    ["doctor", "--mode", "paper", "--mode", "readonly"], ["run", "--mode", "live"], ["submit"]]) {
    const proc = collectProcess("scripts/tiger-agent.mjs", args); proc.child.stdin.end();
    assert.equal(envelope(await proc.closed, 2).error.code, "INVALID_REQUEST");
  }
  assert.deepEqual(await fixture.calls(), []);
});

test("CLI E04/E05: valid JSON alone or exit status alone cannot establish success", async (t) => {
  for (const [body, error] of [["send(result(operation),7);", "RUNNER_FAILED"],
    ["send(result(operation,{status:'failed'}));", "RESULT_INVALID"], ["process.stdout.write('All done!');", "RESULT_INVALID"]]) {
    const fixture = await runnerFixture(t, body);
    const proc = launch(fixture, "run", ["--input-file", "-"]); proc.child.stdin.end(JSON.stringify(REQUEST));
    assert.equal(envelope(await proc.closed, 7).error.code, error);
  }
});

test("CLI E06: cancelling wait keeps accepted identity, kills only query and never resubmits", async (t) => {
  const fixture = await runnerFixture(t, "if(operation==='run')send(result(operation));else{process.on('SIGTERM',()=>{});setInterval(()=>{},1000);}");
  const proc = launch(fixture, "run", ["--input-file", "-", "--wait"]);
  t.after(() => proc.child.kill("SIGKILL"));
  proc.child.stdin.end(JSON.stringify(REQUEST));
  await eventually(async () => (await fixture.calls()).some((call) => call.operation === "status"));
  proc.child.kill("SIGINT");
  const value = envelope(await proc.closed, 130);
  assert.equal(value.error.code, "CANCELLED"); assert.equal(value.run_id, RUN_ID);
  assert.match(value.limitations.join(" "), /existing run_id/u);
  assert.equal((await fixture.calls()).filter((call) => call.operation === "run").length, 1);
  for (const call of await fixture.calls()) assert.throws(() => process.kill(call.pid, 0), { code: "ESRCH" });
});

test("CLI wait timeout retains accepted identity without a replacement key", async (t) => {
  const fixture = await runnerFixture(t, "send(result(operation,operation==='status'?{status:'running'}:{}));");
  const proc = launch(fixture, "run", ["--input-file", "-", "--wait", "--wait-timeout-seconds", "1"]);
  proc.child.stdin.end(JSON.stringify(REQUEST));
  const value = envelope(await proc.closed, 9);
  assert.equal(value.error.code, "TIMEOUT"); assert.equal(value.run_id, RUN_ID);
  assert.equal((await fixture.calls()).filter((call) => call.operation === "run").length, 1);
});

test("CLI input files with spaces/nonASCII work and failed status is only a query success", async (t) => {
  const fixture = await runnerFixture(t, "send(result(operation,{status:'failed'}));");
  const file = join(fixture.dir, "输入 request.json"); await writeFile(file, JSON.stringify({ run_id: RUN_ID }));
  const proc = launch(fixture, "status", ["--input-file", file]); proc.child.stdin.end();
  const value = envelope(await proc.closed, 0); assert.equal(value.status, "failed"); assert.equal(value.summary, null);
});

test("CLI wait covers the business deadline plus bounded receipt proof", async (t) => {
  assert.equal(parseArgs(["run", "--wait", "--wait-timeout-seconds", "14400"]).waitMs, 14_400_000);
  assert.throws(() => parseArgs(["run", "--wait", "--wait-timeout-seconds", "14401"]),
    (error) => error.code === "INVALID_REQUEST");
});

test("CLI reads ordinary stdin without --input-file and forwards status card pagination", async (t) => {
  const fixture = await runnerFixture(t, "send(result(operation,{status:'failed'}));");
  const proc = launch(fixture, "status", ["--detail", "cards", "--limit", "20"]);
  proc.child.stdin.end(JSON.stringify({ run_id: RUN_ID }));
  const value = envelope(await proc.closed, 0);
  assert.equal(value.status, "failed");
  assert.deepEqual((await fixture.calls()).map((call) => [call.operation, call.input]), [
    ["status", { run_id: RUN_ID, detail: "cards", limit: 20 }],
  ]);
});
