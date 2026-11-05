import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { chmod, link, mkdir, mkdtemp, readFile, realpath, rm, symlink, writeFile } from "node:fs/promises";
import fs, { existsSync } from "node:fs";
import { createRequire, syncBuiltinESMExports } from "node:module";
import { homedir, tmpdir } from "node:os";
import { join, resolve } from "node:path";
import { test } from "node:test";
import { parseOptions, prepareConfiguration, writeConfiguration } from "./tiger-agent-config.mjs";
import { MODEL_ENV_NAMES } from "../bundles/trading/capabilities/workflow-client.mjs";

const root = resolve(import.meta.dirname, "..");
const python = process.env.TIGER_TEST_PYTHON ?? join(root, ".venv/bin/python");
const BEGIN = "# BEGIN TIGER AGENT MCP v1";
const END = "# END TIGER AGENT MCP v1";
const require = createRequire(new URL("../governance/package.json", import.meta.url));
async function fixture(t) {
  const temporary = await mkdtemp(join(tmpdir(), "tiger config 中文 "));
  const project = await realpath(temporary);
  t.after(() => rm(project, { recursive: true, force: true }));
  const file = async (name, text) => { const path = join(project, name); await mkdir(join(path, ".."), { recursive: true }); await writeFile(path, text); return path; };
  await file("product.manifest.json", JSON.stringify({ productId: "workroom-tiger" }));
  await file("scripts/tiger-agent-mcp.mjs", "// Configuration target fixture only.\n");
  return { project, file, options: { project, python, client: "codex" } };
}
function withoutBlock(text) { return text.replace(/^# BEGIN TIGER AGENT MCP v1[\s\S]*?^# END TIGER AGENT MCP v1/gmu, "<owned>"); }

test("R04: Codex preserves unrelated bytes, supports spaces/Unicode, is idempotent and changes only owned access mode", async (t) => {
  const f = await fixture(t);
  const original = '# User comment 中文\nmodel = "unchanged-model"\n\n[mcp_servers.existing]\ncommand = "existing command"\nargs = ["a b", "中文"]\n';
  await f.file(".codex/config.toml", original);
  const prepared = prepareConfiguration(f.options);
  assert.equal(prepared.mode, "readonly"); assert.ok(prepared.proposed.startsWith(original));
  assert.equal(writeConfiguration(prepared).durability, "synced");
  const again = prepareConfiguration(f.options);
  assert.equal(again.changed, false); assert.deepEqual(writeConfiguration(again), { changed: false, matches: true });
  const paper = prepareConfiguration({ ...f.options, mode: "paper" });
  assert.equal(withoutBlock(paper.proposed), withoutBlock(again.proposed));
  assert.match(paper.proposed, /"--mode",\s*"paper"/u); writeConfiguration(paper);
  const readonly = prepareConfiguration(f.options); writeConfiguration(readonly);
  assert.equal(await readFile(readonly.file, "utf8"), again.proposed);
});

test("R04: DSH block and flow lists preserve plugins and owned separators across repeated generation", async (t) => {
  const originals = ["", "[]\n", '# original\n- id: other\n  name: other-plugin\n  config: {value: "中文"}\n',
    '[{id: other, name: other-plugin}]\n', '[{id: other, name: other-plugin}, # existing separator\n]\n'];
  for (const original of originals) {
    const f = await fixture(t); const options = { ...f.options, client: "dsh" };
    await f.file(".dsh/tiger-mcp.yaml", original);
    const prepared = prepareConfiguration(options); writeConfiguration(prepared);
    const again = prepareConfiguration(options); assert.equal(again.changed, false, original); writeConfiguration(again);
    const paper = prepareConfiguration({ ...options, mode: "paper" });
    assert.equal(withoutBlock(paper.proposed), withoutBlock(prepared.proposed)); writeConfiguration(paper);
    assert.equal(prepareConfiguration({ ...options, mode: "paper" }).changed, false);
  }
});

test("P04/R04: DSH !!js text stays inert and byte-identical while unrelated alias data is preserved", async (t) => {
  const f = await fixture(t);
  const canary = join(f.project, "must-not-exist");
  const original = `# Dynamic DSH plugin values are never evaluated by this generator.\n- id: other\n  name: example\n  config:\n    payload: !!js >-\n      require('node:fs').writeFileSync(${JSON.stringify(canary)}, 'executed')\n    first: &data {message: unchanged}\n    second: *data\n`;
  await f.file(".dsh/tiger-mcp.yaml", original);
  const prepared = prepareConfiguration({ ...f.options, client: "dsh" });
  assert.ok(prepared.proposed.startsWith(original)); writeConfiguration(prepared);
  assert.equal(existsSync(canary), false);
  assert.equal(prepareConfiguration({ ...f.options, client: "dsh" }).changed, false);
});

test("R04: malformed inputs, conflicting keys, unrelated managed content and duplicate markers leave bytes unchanged", async (t) => {
  const cases = [
    ["codex", '[invalid\n'], ["codex", '[mcp_servers.tiger-workflow]\ncommand="other"\n'],
    ["codex", `${BEGIN}\n[mcp_servers.tiger-workflow]\ncommand="old"\n[unrelated]\nkeep=true\n${END}\n`],
    ["codex", `${BEGIN}\n${BEGIN}\n${END}\n`], ["codex", `${END}\n${BEGIN}\n`],
    ["dsh", 'plugins: []\n'], ["dsh", '- id: duplicate\n  name: a\n- id: duplicate\n  name: b\n'],
    ["dsh", '- id: mcp-tiger-workflow\n  name: another\n'],
    ["dsh", '- id: existing\n  name: "@deepseek-ai/dsh-mcp-client"\n  config: {serverName: tiger-workflow}\n'],
    ["dsh", `${BEGIN}\n- id: mcp-tiger-workflow\n  name: old\n- id: unrelated\n  name: other\n${END}\n`],
  ];
  for (const [client, original] of cases) {
    const f = await fixture(t); const name = client === "codex" ? ".codex/config.toml" : ".dsh/tiger-mcp.yaml";
    const target = await f.file(name, original);
    assert.throws(() => prepareConfiguration({ ...f.options, client }), { code: "INVALID_REQUEST" });
    assert.equal(await readFile(target, "utf8"), original);
  }
});

test("P04/B05/R04: traversal, symlink and hardlink configurations and user-global project roots are rejected", async (t) => {
  const f = await fixture(t);
  for (const config of ["../outside.toml", "/tmp/outside.toml", "x/../outside.toml", "x//a.toml", "x\\..\\outside.toml"]) {
    assert.throws(() => prepareConfiguration({ ...f.options, config }), { code: "INVALID_REQUEST" });
  }
  assert.throws(() => prepareConfiguration({ ...f.options, project: homedir() }), { code: "INVALID_REQUEST" });
  const outside = await mkdtemp(join(tmpdir(), "tiger-config-canary-")); t.after(() => rm(outside, { recursive: true, force: true }));
  const canary = join(outside, "canary.toml"); await writeFile(canary, '# must remain unchanged\n');
  for (const kind of ["symlink", "hardlink", "parent"]) {
    const name = kind === "parent" ? "linked/canary.toml" : `${kind}.toml`;
    if (kind === "parent") await symlink(outside, join(f.project, "linked"));
    else await (kind === "symlink" ? symlink(canary, join(f.project, name)) : link(canary, join(f.project, name)));
    assert.throws(() => prepareConfiguration({ ...f.options, config: name }), { code: "INVALID_REQUEST" });
  }
  assert.equal(await readFile(canary, "utf8"), '# must remain unchanged\n');
});

test("R04: --check creates no directory, concurrent changes and existing write lock prevent replacement", async (t) => {
  const f = await fixture(t);
  const check = prepareConfiguration({ ...f.options, check: true });
  assert.deepEqual(writeConfiguration(check), { changed: false, matches: false });
  assert.equal(existsSync(join(f.project, ".codex")), false);
  const prepared = prepareConfiguration(f.options); await f.file(".codex/config.toml", '# changed after preparation\n');
  assert.throws(() => writeConfiguration(prepared), { code: "IDEMPOTENCY_CONFLICT" });
  const ready = prepareConfiguration(f.options); await f.file(".codex/.tiger-agent-config.lock", "existing");
  assert.throws(() => writeConfiguration(ready), { code: "IDEMPOTENCY_CONFLICT" });
  assert.equal(await readFile(ready.file, "utf8"), '# changed after preparation\n');
  await rm(join(f.project, ".codex/.tiger-agent-config.lock"));
  writeConfiguration(ready);
  assert.equal(existsSync(join(f.project, ".codex/.tiger-agent-config.lock")), false);
});

test("B01/R04: invalid startup/Python selection fails before writing and older validator responses are rejected", async (t) => {
  const f = await fixture(t);
  for (const extra of [{ mode: "live" }, { python: "/no/such/python" }, { python: "--unsafe" }, { stateRoot: "relative" }, { unknown: true }]) {
    assert.throws(() => prepareConfiguration({ ...f.options, ...extra }), { code: "INVALID_REQUEST" });
  }
  const old = await f.file("old-python", `#!${process.execPath}\nprocess.stdout.write(JSON.stringify({python:'/trusted/python',version:[3,10,99],value:{}}));\n`);
  await chmod(old, 0o700);
  assert.throws(() => prepareConfiguration({ ...f.options, python: old }), { code: "INVALID_REQUEST" });
  assert.equal(existsSync(join(f.project, ".codex")), false);
  for (const args of [["--client", "codex", "--client", "dsh"], ["--python"], ["--global"], ["--check", "extra"]]) assert.throws(() => parseOptions(args));
});

test("R04: generator CLI reports real validation, explicit paper and host attachment honestly", async (t) => {
  const f = await fixture(t);
  const script = join(root, "scripts/tiger-agent-config.mjs");
  const args = [script, "--client", "codex", "--project", f.project, "--python", python];
  const checked = spawnSync(process.execPath, [...args, "--check"], { encoding: "utf8" });
  assert.equal(checked.status, 2); assert.equal(JSON.parse(checked.stdout).host_attached, false);
  assert.equal(existsSync(join(f.project, ".codex")), false);
  const generated = spawnSync(process.execPath, [...args, "--mode", "paper"], { encoding: "utf8" });
  assert.equal(generated.status, 0, generated.stdout); assert.equal(generated.stderr, "");
  const result = JSON.parse(generated.stdout); assert.equal(result.ok, true); assert.equal(result.host_attached, false);
  assert.equal(result.mode, "paper"); assert.ok(result.python_version[1] >= 11);
  const matching = spawnSync(process.execPath, [...args, "--mode", "paper", "--check"], { encoding: "utf8" });
  assert.equal(matching.status, 0); assert.equal(JSON.parse(matching.stdout).changed, false);
});

test("R04: write faults preserve prior bytes before rename and disclose visible commits after rename", async (t) => {
  // Official builtin live-binding API keeps fault injection confined to this test's synchronous commit.
  // https://nodejs.org/api/module.html#modulesyncbuiltinesmexports
  for (const fault of ["file-sync", "rename", "directory-sync", "lock-cleanup"]) {
    const f = await fixture(t); const original = '# prior configuration\n';
    await f.file(".codex/config.toml", original);
    const prepared = prepareConfiguration(f.options);
    const saved = { fsyncSync: fs.fsyncSync, renameSync: fs.renameSync, unlinkSync: fs.unlinkSync };
    let syncs = 0;
    const failed = () => { const error = new Error("Injected configuration I/O fault"); error.code = "EIO"; throw error; };
    fs.fsyncSync = (...args) => { syncs += 1; if ((fault === "file-sync" && syncs === 1) || (fault === "directory-sync" && syncs === 2)) failed(); return saved.fsyncSync(...args); };
    fs.renameSync = (...args) => { if (fault === "rename") failed(); return saved.renameSync(...args); };
    fs.unlinkSync = (...args) => { if (fault === "lock-cleanup" && String(args[0]).endsWith(".tiger-agent-config.lock")) failed(); return saved.unlinkSync(...args); };
    syncBuiltinESMExports();
    let outcome;
    try {
      if (["file-sync", "rename"].includes(fault)) assert.throws(() => writeConfiguration(prepared), { code: "EIO" });
      else outcome = writeConfiguration(prepared);
    } finally { Object.assign(fs, saved); syncBuiltinESMExports(); }
    if (["file-sync", "rename"].includes(fault)) assert.equal(await readFile(prepared.file, "utf8"), original);
    else {
      assert.equal(await readFile(prepared.file, "utf8"), prepared.proposed); assert.equal(outcome.changed, true); assert.equal(outcome.matches, true);
      if (fault === "directory-sync") assert.equal(outcome.durability, "unknown");
      else assert.equal(outcome.cleanup, "incomplete");
      assert.match(outcome.warning, /visible|replaced/u);
    }
    assert.equal(fs.readdirSync(join(f.project, ".codex")).some((name) => name.endsWith(".tmp")), false);
    assert.equal(existsSync(join(f.project, ".codex/.tiger-agent-config.lock")), fault === "lock-cleanup");
  }
});

test("NP002/R04: changes during temporary-file sync are rechecked before publication", async (t) => {
  for (const change of ["edit", "replace-same-bytes", "create", "delete", "permissions"]) await t.test(change, async (scenario) => {
    const f = await fixture(scenario); const original = '# original user configuration\n';
    const target = join(f.project, ".codex/config.toml");
    if (change !== "create") await f.file(".codex/config.toml", original);
    const prepared = prepareConfiguration(f.options);
    const saved = fs.fsyncSync;
    let syncs = 0;
    let external;
    fs.fsyncSync = (...args) => {
      syncs += 1;
      if (syncs === 1) {
        if (change === "edit" || change === "create") fs.writeFileSync(target, '# concurrent user save\n');
        else if (change === "replace-same-bytes") {
          fs.writeFileSync(`${target}.user-save`, original); fs.renameSync(`${target}.user-save`, target);
        } else if (change === "delete") fs.unlinkSync(target);
        else fs.chmodSync(target, 0o400);
        external = existsSync(target) ? { text: fs.readFileSync(target, "utf8"), ino: fs.statSync(target).ino,
          mode: fs.statSync(target).mode & 0o777 } : null;
      }
      return saved(...args);
    };
    syncBuiltinESMExports();
    try { assert.throws(() => writeConfiguration(prepared), { code: "IDEMPOTENCY_CONFLICT" }); }
    finally { fs.fsyncSync = saved; syncBuiltinESMExports(); }
    const after = existsSync(target) ? { text: fs.readFileSync(target, "utf8"), ino: fs.statSync(target).ino,
      mode: fs.statSync(target).mode & 0o777 } : null;
    assert.deepEqual(after, external);
    assert.equal(fs.readdirSync(join(f.project, ".codex")).some((name) => name.endsWith(".tmp") || name.endsWith(".lock")), false);
  });
});

test("Codex paper forwards only shared environment names without persisting values; readonly removes them", async (t) => {
  const f = await fixture(t);
  const sentinel = "private-process-value-not-for-config";
  const prior = process.env.LLM_API_KEY;
  process.env.LLM_API_KEY = sentinel;
  try {
    const readonly = prepareConfiguration(f.options);
    assert.doesNotMatch(readonly.proposed, /env_vars/u);
    writeConfiguration(readonly);
    const paper = prepareConfiguration({ ...f.options, mode: "paper" });
    const row = paper.proposed.split("\n").find(line => line.startsWith("env_vars = "));
    assert.deepEqual(JSON.parse(row.slice("env_vars = ".length)), [...MODEL_ENV_NAMES]);
    assert.equal(paper.proposed.includes(sentinel), false);
    writeConfiguration(paper);
    assert.equal(prepareConfiguration({ ...f.options, mode: "paper" }).changed, false);
    const back = prepareConfiguration(f.options);
    assert.equal(back.proposed, readonly.proposed);
  } finally {
    if (prior === undefined) delete process.env.LLM_API_KEY; else process.env.LLM_API_KEY = prior;
  }
});

function qoderOwned(text) {
  const body = text.slice(text.indexOf("// BEGIN TIGER AGENT MCP v1") + "// BEGIN TIGER AGENT MCP v1".length,
    text.indexOf("// END TIGER AGENT MCP v1")).trim().replace(/^,\s*/u, "");
  return JSON.parse(`{${body}}`)["tiger-workflow"];
}
test("Qoder CLI preserves comments, unrelated settings and Unicode while updating only its owned server", async (t) => {
  const originals = ["", "// empty settings 😀\n", "{}\n", '\ufeff{"language":"zh-CN"}\r\n', '// CR-only comment\r{"language":"zh-CN"}\r', '{"language":"zh-CN"}\n',
    '{"language":"zh-CN", /* trailing separator */}\n',
    '{\n  // keep 😀 and exact user formatting\n  "mcpServers": {\n    "other": {"command":"other", "args":["https://example.invalid/a//b", "/* not a comment */", "escaped \\\" quote"]}\n  },\n  "tools": {"exclude": ["unrelated"]}\n}\n',
    '{"mcpServers":{"other":{"args":["//", "/*", "ends in comma,"],},/* retain */},"model":{"name":"unchanged"},}\n'];
  for (const original of originals) await t.test(JSON.stringify(original.slice(0, 40)), async (scenario) => {
    const f = await fixture(scenario); const options = { ...f.options, client: "qoder" };
    await f.file(".qoder/settings.local.json", original);
    const prepared = prepareConfiguration(options);
    if (original.startsWith("\ufeff")) assert.ok(prepared.proposed.startsWith("\ufeff"));
    assert.equal(prepared.mode, "readonly");
    assert.equal(prepared.file, join(f.project, ".qoder/settings.local.json"));
    const server = qoderOwned(prepared.proposed);
    // Use the already-locked TypeScript JSONC parser as an independent syntax/semantic check.
    const ts = require("typescript-governance");
    const external = ts.parseConfigFileTextToJson("settings.json", prepared.proposed);
    assert.equal(external.error, undefined);
    assert.deepEqual(external.config.mcpServers["tiger-workflow"], server);
    const beforeObject = ts.parseConfigFileTextToJson("settings.json", original).config ?? {};
    const afterObject = structuredClone(external.config);
    delete afterObject.mcpServers["tiger-workflow"];
    if (!Object.keys(afterObject.mcpServers).length) delete afterObject.mcpServers;
    if (beforeObject.mcpServers && !Object.keys(beforeObject.mcpServers).length) delete beforeObject.mcpServers;
    assert.deepEqual(afterObject, beforeObject);
    assert.equal(server.type, "stdio"); assert.equal(server.command, process.execPath);
    assert.deepEqual(server.args, [join(f.project, "scripts/tiger-agent-mcp.mjs"), "--python", python,
      "--state-root", join(f.project, ".tiger-agent"), "--mode", "readonly"]);
    assert.equal(server.cwd, f.project); assert.equal(server.timeout, 65000);
    assert.equal(Object.hasOwn(server, "trust"), false); assert.equal(Object.hasOwn(server, "alwaysAllow"), false);
    assert.equal(Object.hasOwn(server, "env"), false);
    writeConfiguration(prepared);
    const again = prepareConfiguration(options);
    assert.equal(again.changed, false);
    const paper = prepareConfiguration({ ...options, mode: "paper" });
    assert.equal(qoderOwned(paper.proposed).args.at(-1), "paper");
    const replace = text => text.replace(/^[\t ]*\/\/ BEGIN TIGER AGENT MCP v1[\s\S]*?^[\t ]*\/\/ END TIGER AGENT MCP v1/gmu, "<owned>");
    assert.equal(replace(paper.proposed), replace(prepared.proposed));
    writeConfiguration(paper);
    assert.equal(prepareConfiguration({ ...options, mode: "paper" }).changed, false);
    assert.equal(prepareConfiguration(options).proposed, prepared.proposed);
    // Every original byte remains in its original order; only one insertion was made.
    const insertion = prepared.proposed.indexOf("// BEGIN TIGER AGENT MCP v1");
    assert.ok(insertion >= 0);
    for (const keep of ['"other"', '"language"', '"tools"', '"model"', "// keep 😀 and exact user formatting", "/* not a comment */"]) {
      if (original.includes(keep)) assert.ok(prepared.proposed.includes(keep));
    }
  });
});

test("Qoder CLI refuses malformed JSONC, duplicate keys, unowned servers and markers outside their object", async (t) => {
  const start = "// BEGIN TIGER AGENT MCP v1"; const end = "// END TIGER AGENT MCP v1";
  const invalid = ["[]", "null", '{"mcpServers":[]}', '{"a":1,"a":2}', '{"mcpServers":{},"mcp\\u0053ervers":{}}',
    '{"mcpServers":{"tiger-workflow":{"command":"unowned"}}}',
    '{"mcpServers":{"same":{},"same":{}}}', '{"unterminated":"value}', '{/* comment never closed',
    `{\n${start}\n"tiger-workflow":{},\n${end}\n"mcpServers":{"tiger-workflow":{}}\n}`,
    `{"mcpServers":{\n${start}\n"tiger-workflow":{},"other":{}\n${end}\n}}`,
    `{"mcpServers":{\n${start}\n${start}\n"tiger-workflow":{}\n${end}\n}}`,
    `{"mcpServers":{\n${end}\n"tiger-workflow":{}\n${start}\n}}`,
  ];
  for (const original of invalid) {
    const f = await fixture(t); const target = await f.file(".qoder/settings.local.json", original);
    assert.throws(() => prepareConfiguration({ ...f.options, client: "qoder" }), { code: "INVALID_REQUEST" });
    assert.equal(await readFile(target, "utf8"), original);
  }
});

test("Qoder CLI --check is read-only and generation reports configuration separately from host attachment", async (t) => {
  const f = await fixture(t); const script = join(root, "scripts/tiger-agent-config.mjs");
  const args = [script, "--client", "qoder", "--project", f.project, "--python", python];
  const check = spawnSync(process.execPath, [...args, "--check"], { encoding: "utf8" });
  assert.equal(check.status, 2, check.stdout); assert.equal(existsSync(join(f.project, ".qoder")), false);
  const generated = spawnSync(process.execPath, [...args, "--mode", "paper"], { encoding: "utf8",
    env: { ...process.env, LLM_API_KEY: "private-process-value-not-for-config" } });
  assert.equal(generated.status, 0, generated.stdout); assert.equal(generated.stderr, "");
  const result = JSON.parse(generated.stdout);
  assert.equal(result.ok, true); assert.equal(result.host_attached, false); assert.equal(result.check, false);
  assert.equal(result.environment_requirements.values_saved, false);
  assert.equal(result.credentials_verified, false);
  assert.deepEqual(result.environment_requirements.names, [...MODEL_ENV_NAMES]);
  assert.match(result.next, /Qoder CLI/u); assert.match(result.next, /No user-global/u);
  assert.equal(generated.stdout.includes("private-process-value-not-for-config"), false);
  assert.equal((await readFile(result.config, "utf8")).includes("private-process-value-not-for-config"), false);
  const checked = spawnSync(process.execPath, [...args, "--mode", "paper", "--check"], { encoding: "utf8" });
  assert.equal(checked.status, 0, checked.stdout); assert.equal(JSON.parse(checked.stdout).changed, false);
  const readonly = prepareConfiguration({ ...f.options, client: "qoder" }); writeConfiguration(readonly);
  const output = spawnSync(process.execPath, [...args, "--check"], { encoding: "utf8" });
  assert.equal(JSON.parse(output.stdout).environment_requirements, null);
});

test("Qoder CLI accepts an explicit project settings path and rejects races without replacing user bytes", async (t) => {
  const f = await fixture(t);
  const original = '{\n"tools": {"exclude": ["unrelated"]}\n}\n';
  const target = await f.file(".qoder/settings.json", original);
  const options = { ...f.options, client: "qoder", config: ".qoder/settings.json" };
  const prepared = prepareConfiguration(options);
  await writeFile(target, "// concurrent edit\n" + original);
  assert.throws(() => writeConfiguration(prepared), { code: "IDEMPOTENCY_CONFLICT" });
  assert.equal(await readFile(target, "utf8"), "// concurrent edit\n" + original);
  const ready = prepareConfiguration(options); writeConfiguration(ready);
  assert.ok((await readFile(target, "utf8")).startsWith("// concurrent edit\n"));
  assert.equal(existsSync(join(f.project, ".qoder/settings.local.json")), false);
  const outside = await mkdtemp(join(tmpdir(), "qoder-outside-")); t.after(() => rm(outside, { recursive: true, force: true }));
  await symlink(outside, join(f.project, "linked"));
  assert.throws(() => prepareConfiguration({ ...options, config: "linked/settings.json" }), { code: "INVALID_REQUEST" });
});

test("Qoder CLI rejects oversized and invalid UTF-8 files before replacement", async (t) => {
  for (const original of [Buffer.from([0x7b, 0xff, 0x7d]), Buffer.from('"' + "x".repeat(1024 * 1024) + '"')]) {
    const f = await fixture(t); const target = await f.file(".qoder/settings.local.json", original);
    assert.throws(() => prepareConfiguration({ ...f.options, client: "qoder" }), { code: "INVALID_REQUEST" });
    assert.deepEqual(await readFile(target), original);
  }
  const f = await fixture(t);
  const original = '{"large":"' + "x".repeat(1024 * 1024 - 200) + '"}';
  const target = await f.file(".qoder/settings.local.json", original);
  assert.throws(() => prepareConfiguration({ ...f.options, client: "qoder" }), { code: "INVALID_REQUEST" });
  assert.equal(await readFile(target, "utf8"), original);
});
