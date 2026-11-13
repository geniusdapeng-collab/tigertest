import assert from "node:assert/strict";
import { mkdtemp, mkdir, readFile, writeFile, realpath, symlink, link, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";
import { pathToFileURL } from "node:url";
import { test } from "node:test";
import * as actual from "./repo.mjs";
import { invokeCapability, loadCatalog } from "../../../scripts/agent-capabilities.mjs";

const root = resolve(import.meta.dirname, "../../..");
async function fixture(t) {
  const dir = await mkdtemp(join(tmpdir(), "tiger-repo-"));
  t.after(() => rm(dir, { recursive: true, force: true }));
  async function file(path, text) { await mkdir(join(dir, path, ".."), { recursive: true }); await writeFile(join(dir, path), text); }
  await file("bundles/trading/capabilities/repo.mjs", await readFile(new URL("./repo.mjs", import.meta.url), "utf8"));
  await file("product.manifest.json", JSON.stringify({ productId: "workroom-tiger", defaultBundle: "trading", demoWorkspaceSlug: "fixture" }));
  await file("governance/bundles/trading/bundle.json", JSON.stringify({ version: "fixture-version" }));
  await file("governance/bundles/trading/fences/sample.yml", "- rule_id: FIXTURE-PAPER\n");
  await file("governance/bundles/trading/skills/read.md", "# Fixture\n");
  await file("governance/packages/runtime/dsh-gate/package.json", JSON.stringify({ dependencies: { "@deepseek-ai/dsh": "fixture-pin" } }));
  await file("trading_system/__init__.py", "");
  await file("scripts/workloom-agent-mcp.mjs", ""); await file("scripts/tiger-agent-mcp.mjs", "");
  await file("docs/a.md", "# Actual fixture title\n"); await file("docs/b.md", "# Second\n");
  await mkdir(join(dir, "governance/node_modules"), { recursive: true });
  const module = await import(pathToFileURL(join(dir, "bundles/trading/capabilities/repo.mjs")).href);
  return { dir, file, module };
}

test("R02: actual engine and governance roots are reported with the real trading manifest", () => {
  const status = actual.repoStatus({});
  assert.equal(status.productId, "workloom-ai-tigertest"); assert.equal(status.engineRoot, root); assert.equal(status.governanceRoot, join(root, "governance"));
  const bundle = actual.bundleSummary({}); assert.equal(bundle.found, true); assert.equal(bundle.location, "governance/bundles/trading");
  assert.ok(bundle.counts.skills > 0); assert.ok(bundle.counts.fences > 0);
  const readiness = actual.repoReadiness({}); assert.equal(readiness.ready, true);
  assert.equal(readiness.items.some((item) => /keys-file|ARK/u.test(item.id + item.detail)), false);
});

test("R02/P04: fixture proves governance paths and no user-secret file open", async (t) => {
  const { dir, module } = await fixture(t);
  // A directory is deliberately not a readable env file. The provider must never open it.
  const prior = process.env.WORKLOOM_KEYS_FILE;
  process.env.WORKLOOM_KEYS_FILE = dir;
  t.after(() => { if (prior === undefined) delete process.env.WORKLOOM_KEYS_FILE; else process.env.WORKLOOM_KEYS_FILE = prior; });
  const bundle = module.bundleSummary({}); assert.equal(bundle.version, "fixture-version"); assert.equal(bundle.counts.skills, 1);
  assert.deepEqual(bundle.fenceRuleSample, ["FIXTURE-PAPER"]);
  const readiness = module.repoReadiness({}); assert.equal(readiness.ready, true);
  assert.match(readiness.items.find((item) => item.id === "dsh-gate").detail, /fixture-pin/u);
  assert.equal(readiness.items.some((item) => /secret|keys-file|ARK/u.test(JSON.stringify(item))), false);
  assert.equal(module.repoStatus({}).governanceRoot, join(await realpath(dir), "governance"));
});

test("P04/B05: document symlink and hardlink escapes are rejected before content is returned", async (t) => {
  for (const kind of ["symlink", "hardlink"]) {
    const { dir, module } = await fixture(t);
    const outside = await mkdtemp(join(tmpdir(), "tiger-canary-")); t.after(() => rm(outside, { recursive: true, force: true }));
    const target = join(outside, "canary.md"); await writeFile(target, "# CANARY MUST NOT BE RETURNED\n");
    await (kind === "symlink" ? symlink(target, join(dir, "docs/c.md")) : link(target, join(dir, "docs/c.md")));
    assert.throws(() => module.repoDocs({}), (error) => error.code === "UNSAFE_PATH" && !error.message.includes("CANARY"));
    assert.equal(await readFile(target, "utf8"), "# CANARY MUST NOT BE RETURNED\n");
  }
});

test("B01/B05: malformed input, missing layouts and linked parent directories fail safely", async (t) => {
  const { dir, module } = await fixture(t);
  for (const limit of [0, 61, true, "2", null]) assert.throws(() => module.repoDocs({ limit }));
  assert.deepEqual(module.repoDocs({ limit: 1 }).docs, [{ file: "docs/a.md", title: "Actual fixture title" }]);
  assert.throws(() => module.repoReadiness({ root: "/tmp" }));
  await rm(join(dir, "governance/bundles/trading"), { recursive: true }); assert.equal(module.bundleSummary({}).found, false);
  await rm(join(dir, "docs"), { recursive: true }); await symlink(tmpdir(), join(dir, "docs"));
  assert.throws(() => module.repoDocs({}), { code: "UNSAFE_PATH" });
});

test("R01/R03: old readonly catalog remains valid and still forbids local execute", async (t) => {
  const catalog = await loadCatalog(root);
  assert.equal(catalog.entries.size, 12);
  const output = await invokeCapability(catalog, "trading.repo.status", {});
  assert.equal(output.status, "succeeded"); assert.equal(output.result.productId, "workloom-ai-tigertest");
  const descriptor = catalog.entries.get("trading.repo.status");
  const { dir, file } = await fixture(t);
  await file("bundles/trading/agent-capabilities.json", JSON.stringify({ schemaVersion: "workloom.agent-capabilities/v1", bundleId: "trading",
    capabilities: [{ ...descriptor, id: "trading.test.execute", operation: "execute", inputSchema: {
      type: "object", properties: { idempotencyKey: { type: "string" } }, required: ["idempotencyKey"], additionalProperties: false } }] }));
  await assert.rejects(loadCatalog(dir), (error) => error.code === "CATALOG_INVALID" && /local|本地/iu.test(error.message));
});
