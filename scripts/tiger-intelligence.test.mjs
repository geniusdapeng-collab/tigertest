import assert from "node:assert/strict";
import { mkdtemp, rm, stat } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { spawnSync } from "node:child_process";
import test from "node:test";
import { acquisitionEnvironment, parseArgs } from "./tiger-intelligence.mjs";

test("trusted startup options stay fixed and bounded", () => {
  const config = parseArgs(["monitor", "--archive-root", "/tmp/tiger-intelligence", "--mode", "acquire",
    "--cycles", "3", "--wall-seconds", "300", "--enable-source", "brave"]);
  assert.equal(config.cycles, 3);
  assert.equal(config.wall, 300);
  assert.deepEqual(config.sources, ["brave"]);
  assert.equal(config.file, "-");
  for (const argv of [
    ["sync", "--archive-root", "relative"], ["live", "--archive-root", "/tmp/archive"],
    ["sync", "--archive-root", "/tmp/archive", "--mode", "paper"],
    ["monitor", "--archive-root", "/tmp/archive", "--cycles", "13"],
    ["monitor", "--archive-root", "/tmp/archive", "--wall-seconds", "59"],
    ["monitor", "--archive-root", "/tmp/archive", "--enable-source", "arbitrary"],
    ["monitor", "--archive-root", "/tmp/archive", "--env", "unsafe"],
    ["sync", "--archive-root", "/tmp/archive", "--cycles", "2"],
    ["sync", "--archive-root", "/tmp/archive", "--mode", "readonly", "--mode", "acquire"],
  ]) assert.throws(() => parseArgs(argv), /INVALID_STARTUP/u);
});

test("readonly environment carries no acquisition keys and acquire has an allowlist", () => {
  const environment = { PATH: process.env.PATH, LLM_API_KEY: "fixture-key", TIGER_INTEL_EXA_API_KEY: "fixture-exa",
    GITHUB_TOKEN: "unrelated-fixture-token", LLM_MODEL: "unrelated-model", PYTHONPATH: "/injected/module" };
  const readonly = acquisitionEnvironment("readonly", environment);
  assert.equal(readonly.LLM_API_KEY, undefined);
  assert.equal(readonly.TIGER_INTEL_EXA_API_KEY, undefined);
  const acquire = acquisitionEnvironment("acquire", environment);
  assert.equal(acquire.LLM_API_KEY, "fixture-key");
  assert.equal(acquire.TIGER_INTEL_EXA_API_KEY, "fixture-exa");
  assert.equal(acquire.GITHUB_TOKEN, undefined);
  assert.equal(acquire.LLM_MODEL, undefined);
  assert.equal(acquire.PYTHONPATH, undefined);
});

test("readonly rejects before reading a nonexistent request or creating an archive", async () => {
  const folder = await mkdtemp(join(tmpdir(), "tiger-intelligence-node-"));
  try {
    const result = spawnSync(process.execPath, ["scripts/tiger-intelligence.mjs", "sync", "--archive-root", join(folder, "archive"),
      "--input-file", join(folder, "missing.json")], { encoding: "utf8", timeout: 10000 });
    assert.equal(result.status, 3, result.stderr);
    assert.equal(JSON.parse(result.stdout).error.code, "READ_ONLY");
    await assert.rejects(stat(join(folder, "archive")), { code: "ENOENT" });
  } finally { await rm(folder, { recursive: true, force: true }); }
});

test("malformed request cannot start an acquisition subprocess", async () => {
  const result = spawnSync(process.execPath, ["scripts/tiger-intelligence.mjs", "sync", "--mode", "acquire",
    "--archive-root", "/tmp/unused-tiger-intelligence-test", "--python", "/nonexistent/python"],
  { input: '{"a":1,"a":2}', encoding: "utf8", timeout: 10000 });
  assert.equal(result.status, 2);
  assert.equal(JSON.parse(result.stdout).error.code, "INVALID_REQUEST");
});

test("real Python readonly evidence lookup returns fixed safe error without making an archive", async (context) => {
  const python = process.env.TIGER_TEST_PYTHON;
  if (!python) { context.skip("Set TIGER_TEST_PYTHON to run the real Python subprocess"); return; }
  const folder = await mkdtemp(join(tmpdir(), "tiger-intelligence-python-"));
  try {
    const result = spawnSync(process.execPath, ["scripts/tiger-intelligence.mjs", "status", "--archive-root", join(folder, "archive"),
      "--python", python], { input: '{"job_id":"intel_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"}', encoding: "utf8", timeout: 10000 });
    assert.equal(result.status, 7, result.stderr);
    assert.equal(JSON.parse(result.stdout).error.code, "EVIDENCE_NOT_FOUND");
    await assert.rejects(stat(join(folder, "archive")), { code: "ENOENT" });
  } finally { await rm(folder, { recursive: true, force: true }); }
});
