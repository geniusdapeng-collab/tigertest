#!/usr/bin/env node
/** Generate reviewed project-only client configuration; never write a user-global config. */
import { spawnSync } from "node:child_process";
import { createRequire } from "node:module";
import { accessSync, closeSync, constants, existsSync, fchmodSync, fstatSync, fsyncSync, lstatSync,
  mkdirSync, openSync, readFileSync, realpathSync, renameSync, unlinkSync, writeFileSync } from "node:fs";
import { homedir } from "node:os";
import { dirname, isAbsolute, join, relative, resolve, sep } from "node:path";
import { randomUUID } from "node:crypto";
import { isDeepStrictEqual } from "node:util";
import { pathToFileURL } from "node:url";
import { ENGINE_ROOT, MODEL_ENV_NAMES, WorkflowError, parseJson, plain, runtimeEnvironment, sanitizeMessage } from "../bundles/trading/capabilities/workflow-client.mjs";

const require = createRequire(new URL("../governance/package.json", import.meta.url));
const SERVER = "tiger-workflow";
const PLUGIN = "mcp-tiger-workflow";
const BEGIN = "# BEGIN TIGER AGENT MCP v1";
const END = "# END TIGER AGENT MCP v1";
const JSON_BEGIN = "// BEGIN TIGER AGENT MCP v1";
const JSON_END = "// END TIGER AGENT MCP v1";
const MAX_CONFIG_BYTES = 1024 * 1024;
const PYTHON_PARSE = `import sys,json
if sys.version_info < (3,11): raise SystemExit(3)
import tomllib
source=sys.stdin.buffer.read(1048577)
if len(source)>1048576: raise SystemExit(2)
try:
 value=tomllib.loads(source.decode('utf-8'))
except (ValueError,UnicodeError):
 raise SystemExit(2)
print(json.dumps({'python':sys.executable,'version':list(sys.version_info[:3]),'value':value},ensure_ascii=False,default=str,allow_nan=False))
`;

function fail(message, code = "INVALID_REQUEST") { throw new WorkflowError(code, message); }
function isWithin(base, path) { const rel = relative(base, path); return rel !== "" && rel !== ".." && !rel.startsWith(`..${sep}`) && !isAbsolute(rel); }
function noLinks(base, path) {
  if (!isWithin(base, path)) fail("Configuration path must stay inside the selected project.");
  let current = base;
  for (const part of relative(base, path).split(sep)) {
    current = join(current, part);
    let stat;
    try { stat = lstatSync(current); } catch (error) { if (error.code === "ENOENT") break; throw error; }
    if (stat.isSymbolicLink()) fail("Configuration path contains a symbolic link.");
  }
}
function readConfig(project, file) {
  noLinks(project, file);
  if (!existsSync(file)) return { text: "", exists: false, mode: 0o600, ino: null, dev: null };
  const fd = openSync(file, constants.O_RDONLY | constants.O_NOFOLLOW);
  try {
    const stat = fstatSync(fd);
    if (!stat.isFile() || stat.nlink !== 1) fail("Configuration must be an independent regular file.");
    if (stat.size > MAX_CONFIG_BYTES) fail("Configuration file exceeds the byte limit.");
    const data = readFileSync(fd);
    if (data.length > MAX_CONFIG_BYTES) fail("Configuration file exceeds the byte limit.");
    let text;
    try { text = new TextDecoder("utf-8", { fatal: true, ignoreBOM: true }).decode(data); } catch { fail("Configuration is not valid UTF-8."); }
    noLinks(project, file);
    return { text, exists: true, mode: stat.mode & 0o777, ino: stat.ino, dev: stat.dev };
  } finally { closeSync(fd); }
}
function sameConfig(left, right) {
  return left.text === right.text && left.exists === right.exists && left.ino === right.ino
    && left.dev === right.dev && left.mode === right.mode;
}
function selectedPython(project, explicit) {
  const candidate = explicit ?? (existsSync(join(project, ".venv/bin/python")) ? join(project, ".venv/bin/python") : "python3");
  if (typeof candidate !== "string" || !candidate || /[\x00-\x1f]/u.test(candidate) || candidate.startsWith("-")) fail("Invalid Python executable.");
  const choices = isAbsolute(candidate) ? [candidate] : candidate.includes(sep) ? [resolve(project, candidate)]
    : (process.env.PATH ?? "").split(sep === "/" ? ":" : ";").filter(isAbsolute).map((path) => join(path, candidate));
  for (const path of choices) {
    try { accessSync(path, constants.X_OK); return path; } catch (error) { if (!["ENOENT", "EACCES", "ENOTDIR"].includes(error.code)) throw error; }
  }
  fail("Configured Python executable is unavailable. Supply --python with Python 3.11 or newer.");
}
function toml(text, python, project) {
  const parsed = spawnSync(python, ["-I", "-B", "-c", PYTHON_PARSE], { cwd: project, env: runtimeEnvironment(),
    input: text, encoding: "utf8", timeout: 10000, maxBuffer: 4 * MAX_CONFIG_BYTES, windowsHide: true });
  if (parsed.error || parsed.status !== 0) fail("Python 3.11+ with tomllib and a valid TOML document are required; configuration was not written.");
  let result;
  try { result = parseJson(parsed.stdout, 4 * MAX_CONFIG_BYTES); } catch { fail("Python configuration validation returned an invalid response."); }
  if (!plain(result) || !plain(result.value) || !isAbsolute(result.python ?? "") || !Array.isArray(result.version)
    || result.version[0] !== 3 || result.version[1] < 11) fail("Python 3.11+ is required.");
  return result;
}
function yaml(text) {
  let parser;
  try { parser = require("yaml"); } catch { fail("Install the existing governance dependencies before generating a DSH configuration."); }
  // No !!js resolver is registered. Other plugins' tagged scalar text stays inert and byte-for-byte unchanged.
  const doc = parser.parseDocument(text, { version: "1.2", schema: "core", customTags: [], resolveKnownTags: false,
    uniqueKeys: true, stringKeys: true, merge: false, prettyErrors: false, logLevel: "silent" });
  if (doc.errors.length) fail("DSH configuration must be one valid YAML plugin-list document.");
  let value;
  try { value = doc.toJS({ maxAliasCount: 100 }); } catch { fail("DSH configuration contains invalid or excessive aliases."); }
  if (value === null) value = [];
  if (!Array.isArray(value) || value.some((entry) => !plain(entry))) fail("DSH configuration must be a top-level plugin list, not a complete unrelated client configuration.");
  const ids = new Set(); const servers = new Set();
  for (const entry of value) {
    if (typeof entry.id !== "string" || !entry.id || ids.has(entry.id)) fail("DSH plugin IDs must be present and unique.");
    ids.add(entry.id);
    if (entry.name === "@deepseek-ai/dsh-mcp-client") {
      const name = entry.config?.serverName;
      if (typeof name !== "string" || !/^[A-Za-z0-9_-]{1,32}$/u.test(name) || servers.has(name)) fail("DSH MCP server names must be valid and unique.");
      servers.add(name);
    }
  }
  return { value, doc };
}
function blockRange(text, json = false) {
  const starts = [...text.matchAll(json ? /^[\t ]*\/\/ BEGIN TIGER AGENT MCP v1\r?$/gmu : /^# BEGIN TIGER AGENT MCP v1\r?$/gmu)];
  const ends = [...text.matchAll(json ? /^[\t ]*\/\/ END TIGER AGENT MCP v1\r?$/gmu : /^# END TIGER AGENT MCP v1\r?$/gmu)];
  if (!starts.length && !ends.length) return null;
  if (starts.length !== 1 || ends.length !== 1 || starts[0].index >= ends[0].index) fail("Managed configuration markers are missing, duplicated or out of order.");
  return { start: starts[0].index, end: ends[0].index + ends[0][0].length,
    body: text.slice(starts[0].index + starts[0][0].length, ends[0].index) };
}
function withoutCodex(value) {
  const copy = structuredClone(value);
  if (plain(copy.mcp_servers)) { delete copy.mcp_servers[SERVER]; if (!Object.keys(copy.mcp_servers).length) delete copy.mcp_servers; }
  return copy;
}
function withoutQoder(value) {
  const copy = structuredClone(value);
  if (plain(copy.mcpServers)) { delete copy.mcpServers[SERVER]; if (!Object.keys(copy.mcpServers).length) delete copy.mcpServers; }
  return copy;
}

/** Preserve offsets and all original bytes; only the owned member is ever regenerated. */
function jsonc(text) {
  // Offsets below are UTF-16 string indices, including surrogate pairs in user comments.
  // split("") keeps those offsets aligned with slice(), unlike code-point iteration.
  const clean = text.split("");
  let index = 0;
  const stringEnd = (start) => {
    let escaped = false;
    for (let cursor = start + 1; cursor < text.length; cursor += 1) {
      const character = text[cursor];
      if (!escaped && character === '"') return cursor + 1;
      if (!escaped && character === "\\") escaped = true; else escaped = false;
    }
    fail("Qoder configuration contains an unterminated string.");
  };
  while (index < text.length) {
    if (text[index] === '"') { index = stringEnd(index); continue; }
    if (text.startsWith("//", index) || text.startsWith("/*", index)) {
      const line = text[index + 1] === "/";
      const lineEnd = line ? text.slice(index + 2).search(/[\n\r]/u) : -1;
      const found = line ? lineEnd < 0 ? -1 : index + 2 + lineEnd : text.indexOf("*/", index + 2);
      if (!line && found < 0) fail("Qoder configuration contains an unterminated comment.");
      const end = found < 0 ? text.length : line ? found : found + 2;
      for (; index < end; index += 1) if (!["\n", "\r"].includes(text[index])) clean[index] = " ";
      continue;
    }
    index += 1;
  }
  let stripped = clean.join("");
  const commentsClean = stripped;
  // JSONC trailing separators are whitespace for strict duplicate-aware JSON validation.
  for (index = 0; index < stripped.length; index += 1) {
    if (stripped[index] === '"') { index = stringEnd(index) - 1; continue; }
    if (stripped[index] !== ",") continue;
    let next = index + 1;
    while (/\s/u.test(stripped[next] ?? "x")) next += 1;
    if (["}", "]"].includes(stripped[next])) clean[index] = " ";
  }
  stripped = clean.join("");
  const empty = !stripped.trim();
  let value;
  try { value = empty ? {} : parseJson(stripped, MAX_CONFIG_BYTES); }
  catch { fail("Qoder configuration must be valid JSONC without duplicate keys."); }
  if (!plain(value) || value.mcpServers !== undefined && !plain(value.mcpServers)) fail("Qoder configuration must be an object with an mcpServers object.");
  return { value, clean: stripped, commentsClean, empty };
}
function objectRange(source, property) {
  const space = (offset) => { while (/\s/u.test(source[offset] ?? "x")) offset += 1; return offset; };
  const endOfString = (start) => {
    let escaped = false;
    for (let index = start + 1; index < source.length; index += 1) {
      if (!escaped && source[index] === '"') return index + 1;
      if (!escaped && source[index] === "\\") escaped = true; else escaped = false;
    }
    fail("Invalid Qoder configuration string.");
  };
  const endOfValue = (start) => {
    if (source[start] === '"') return endOfString(start);
    if (["{", "["].includes(source[start])) {
      let depth = 0;
      for (let index = start; index < source.length; index += 1) {
        if (source[index] === '"') { index = endOfString(index) - 1; continue; }
        if (["{", "["].includes(source[index])) depth += 1;
        else if (["}", "]"].includes(source[index]) && --depth === 0) return index + 1;
      }
      fail("Invalid Qoder configuration object.");
    }
    let index = start;
    while (index < source.length && !/[,}\]\s]/u.test(source[index])) index += 1;
    return index;
  };
  const rootStart = space(0);
  if (property === undefined) return { start: rootStart, end: endOfValue(rootStart) };
  let cursor = space(rootStart + 1);
  while (source[cursor] !== "}") {
    const keyEnd = endOfString(cursor);
    const key = JSON.parse(source.slice(cursor, keyEnd));
    const start = space(space(keyEnd) + 1);
    const end = endOfValue(start);
    if (key === property) return { start, end };
    cursor = space(end);
    if (source[cursor] === ",") cursor = space(cursor + 1);
  }
  return null;
}
function qoderConfiguration(text, range, server) {
  const original = jsonc(text);
  const entries = original.value.mcpServers ?? {};
  if (Object.hasOwn(entries, SERVER) !== Boolean(range)) fail("Existing Tiger Qoder configuration is not owned by this generator; resolve the conflict explicitly.");
  const bounds = original.empty ? null : objectRange(original.clean, "mcpServers");
  const member = `${JSON.stringify(SERVER)}: ${JSON.stringify(server, null, 2)}`;
  let proposed;
  if (range) {
    if (!bounds || range.start <= bounds.start || range.end >= bounds.end) fail("Managed Qoder block is outside mcpServers.");
    const body = range.body.trim();
    const owned = jsonc(`{${body.replace(/^,\s*/u, "")}}`).value;
    if (Object.keys(owned).length !== 1 || !Object.hasOwn(owned, SERVER)) fail("Managed Qoder block contains unrelated configuration.");
    proposed = appendBlock(text, [JSON_BEGIN, `${body.startsWith(",") ? ", " : ""}${member}`, JSON_END].join("\n"), range);
  } else if (bounds) {
    const prefix = Object.keys(entries).length && !original.commentsClean.slice(bounds.start + 1, bounds.end - 1).trim().endsWith(",") ? ", " : "";
    const block = [JSON_BEGIN, `${prefix}${member}`, JSON_END].join("\n");
    proposed = text.slice(0, bounds.end - 1) + "\n" + block + "\n" + text.slice(bounds.end - 1);
  } else {
    const block = [JSON_BEGIN, member, JSON_END].join("\n");
    const inserted = `${JSON.stringify("mcpServers")}: {\n${block}\n}`;
    if (original.empty) proposed = text + (text && !text.endsWith("\n") ? "\n" : "") + `{\n${inserted}\n}\n`;
    else {
      const root = objectRange(original.clean);
      let previous = root.end - 2;
      while (/\s/u.test(original.commentsClean[previous] ?? "x")) previous -= 1;
      const prefix = Object.keys(original.value).length && text[previous] !== "," ? ", " : "";
      proposed = text.slice(0, root.end - 1) + "\n" + prefix + inserted + "\n" + text.slice(root.end - 1);
    }
  }
  const after = jsonc(proposed).value;
  if (!isDeepStrictEqual(withoutQoder(original.value), withoutQoder(after)) || !isDeepStrictEqual(after.mcpServers?.[SERVER], server)) fail("Generated JSONC would alter unrelated configuration.");
  return proposed;
}
function appendBlock(text, block, range) {
  if (range) return text.slice(0, range.start) + block + text.slice(range.end);
  return text + (text && !text.endsWith("\n") ? "\n" : "") + (text ? "\n" : "") + block + "\n";
}

export function prepareConfiguration(options = {}) {
  for (const name of Object.keys(options)) if (!["client", "project", "python", "stateRoot", "mode", "config", "check"].includes(name)) fail("Unknown generator option.");
  if (!["codex", "dsh", "qoder"].includes(options.client)) fail("Choose --client codex, --client dsh or --client qoder (Qoder CLI).");
  const requested = resolve(options.project ?? ENGINE_ROOT);
  if (!existsSync(requested) || lstatSync(requested).isSymbolicLink() || !lstatSync(requested).isDirectory()) fail("Project must be an existing, non-symlink Tiger repository directory.");
  const project = realpathSync(requested);
  if (project === realpathSync(homedir())) fail("User-global configuration is outside this generator's scope.");
  const entry = join(project, "scripts/tiger-agent-mcp.mjs");
  const manifest = readConfig(project, join(project, "product.manifest.json"));
  if (!manifest.exists || parseJson(manifest.text).productId !== "workroom-tiger" || !readConfig(project, entry).exists) fail("Selected project is not a Tiger repository with the reviewed MCP entry.");
  const mode = options.mode ?? "readonly";
  if (!["readonly", "paper"].includes(mode)) fail("Access mode must be readonly or paper.");
  const stateRoot = options.stateRoot ?? join(project, ".tiger-agent");
  if (!isAbsolute(stateRoot) || /[\x00-\x1f]/u.test(stateRoot)) fail("state-root must be an absolute directory.");
  const relativeConfig = options.config ?? ({ codex: ".codex/config.toml", dsh: ".dsh/tiger-mcp.yaml", qoder: ".qoder/settings.local.json" })[options.client];
  if (typeof relativeConfig !== "string" || !relativeConfig || isAbsolute(relativeConfig)
    || relativeConfig.split(/[\\/]/u).some((part) => ["", ".", ".."].includes(part))) fail("--config must be a project-relative file path without traversal.");
  const file = resolve(project, relativeConfig);
  noLinks(project, file);
  const before = readConfig(project, file);
  const range = blockRange(before.text, options.client === "qoder");
  const python = selectedPython(project, options.python);
  const probe = toml("", python, project);
  const server = { command: process.execPath, args: [entry, "--python", probe.python, "--state-root", resolve(stateRoot), "--mode", mode], cwd: project,
    startup_timeout_sec: 15, tool_timeout_sec: 65,
    ...(options.client === "codex" && mode === "paper" ? { env_vars: [...MODEL_ENV_NAMES] } : {}) };
  let proposed;
  if (options.client === "codex") {
    const original = toml(before.text, python, project).value;
    if (original.mcp_servers !== undefined && !plain(original.mcp_servers)) fail("Existing mcp_servers must be a TOML table.");
    if (Object.hasOwn(original.mcp_servers ?? {}, SERVER) !== Boolean(range)) fail("Existing Tiger MCP configuration is not owned by this generator; resolve the conflict explicitly.");
    if (range) {
      const owned = toml(range.body, python, project).value;
      if (Object.keys(owned).length !== 1 || !plain(owned.mcp_servers) || Object.keys(owned.mcp_servers).length !== 1
        || !Object.hasOwn(owned.mcp_servers, SERVER)) fail("Managed block contains unrelated configuration.");
    }
    const block = [BEGIN, `[mcp_servers.${SERVER}]`, ...Object.entries(server).map(([key, value]) => `${key} = ${JSON.stringify(value)}`), END].join("\n");
    proposed = appendBlock(before.text, block, range);
    const after = toml(proposed, python, project).value;
    if (!isDeepStrictEqual(withoutCodex(original), withoutCodex(after)) || !isDeepStrictEqual(after.mcp_servers?.[SERVER], server)) fail("Generated TOML would alter unrelated configuration.");
  } else if (options.client === "qoder") {
    proposed = qoderConfiguration(before.text, range, { type: "stdio", command: server.command, args: server.args, cwd: project, timeout: 65000 });
  } else {
    const original = yaml(before.text);
    const ownedItems = original.value.filter((item) => item.id === PLUGIN);
    if (ownedItems.length !== Number(Boolean(range)) || original.value.some((item) => item.id !== PLUGIN && item.config?.serverName === SERVER)) fail("Existing Tiger DSH plugin conflicts with the managed entry.");
    const plugin = { id: PLUGIN, name: "@deepseek-ai/dsh-mcp-client", config: { serverName: SERVER, transport: "stdio", command: server.command,
      args: server.args, cwd: project, toolCallTimeoutMs: 65000, failOnStartupError: true } };
    const flow = original.doc.contents?.flow === true;
    if (range) {
      let ownedBody = range.body.trim();
      if (flow) ownedBody = `[${ownedBody.replace(/^,\s*/u, "")}]`;
      const owned = yaml(ownedBody).value;
      if (owned.length !== 1 || owned[0].id !== PLUGIN) fail("Managed DSH block contains unrelated plugins.");
    }
    const others = original.value.filter((item) => item.id !== PLUGIN);
    if (flow) {
      const ownedSeparator = range ? range.body.trim().startsWith(",") : others.length > 0;
      const block = [BEGIN, `${ownedSeparator ? ", " : ""}${JSON.stringify(plugin)}`, END].join("\n");
      if (range) proposed = appendBlock(before.text, block, range);
      else {
        const closing = original.doc.contents.range[1] - 1;
        if (before.text[closing] !== "]") fail("Unsupported YAML flow-list boundary.");
        // A trailing separator belongs to the existing list. Do not insert a second one.
        const last = original.doc.contents.items.at(-1);
        const tail = last ? before.text.slice(last.range[1], closing).replace(/#[^\n]*/gu, "").trim() : "";
        const inserted = tail === "," ? block.replace(/\n, /u, "\n") : block;
        proposed = before.text.slice(0, closing) + "\n" + inserted + "\n" + before.text.slice(closing);
      }
    } else {
      const block = [BEGIN, `- id: ${JSON.stringify(plugin.id)}`, `  name: ${JSON.stringify(plugin.name)}`, "  config:",
        ...Object.entries(plugin.config).map(([key, value]) => `    ${key}: ${JSON.stringify(value)}`), END].join("\n");
      proposed = appendBlock(before.text, block, range);
    }
    const after = yaml(proposed).value;
    if (!isDeepStrictEqual(others, after.filter((item) => item.id !== PLUGIN))
      || !isDeepStrictEqual(after.find((item) => item.id === PLUGIN), plugin)) fail("Generated YAML would alter unrelated configuration.");
  }
  if (Buffer.byteLength(proposed) > MAX_CONFIG_BYTES) fail("Generated configuration exceeds the byte limit.");
  return { project, file, before, proposed, client: options.client, mode, python: probe.python, pythonVersion: probe.version,
    check: options.check === true, changed: proposed !== before.text };
}

export function writeConfiguration(prepared) {
  if (prepared.check || !prepared.changed) return { changed: false, matches: !prepared.changed };
  noLinks(prepared.project, prepared.file);
  const current = readConfig(prepared.project, prepared.file);
  if (!sameConfig(current, prepared.before)) fail("Configuration changed during preparation; nothing was written.", "IDEMPOTENCY_CONFLICT");
  mkdirSync(dirname(prepared.file), { recursive: true, mode: 0o700 });
  noLinks(prepared.project, prepared.file);
  const lock = join(dirname(prepared.file), ".tiger-agent-config.lock");
  let lockFd;
  try { lockFd = openSync(lock, "wx", 0o600); }
  catch (error) { if (error.code === "EEXIST") fail("Another configuration write is active or left a lock; nothing was replaced.", "IDEMPOTENCY_CONFLICT"); throw error; }
  const temporary = join(dirname(prepared.file), `.tiger-agent-config-${randomUUID()}.tmp`);
  let committed = false;
  let tempFd;
  let outcome;
  let operationError;
  const cleanupErrors = [];
  try {
    const underLock = readConfig(prepared.project, prepared.file);
    if (!sameConfig(underLock, current)) fail("Configuration changed before commit; nothing was replaced.", "IDEMPOTENCY_CONFLICT");
    tempFd = openSync(temporary, "wx", 0o600);
    writeFileSync(tempFd, prepared.proposed, "utf8");
    fchmodSync(tempFd, prepared.before.mode);
    fsyncSync(tempFd); closeSync(tempFd); tempFd = undefined;
    // Other editors do not share our lock. Recheck after the potentially slow sync.
    // This detects intervening saves; it is not an OS-level compare-and-swap rename.
    const beforePublish = readConfig(prepared.project, prepared.file);
    if (!sameConfig(beforePublish, underLock)) fail("Configuration changed during file sync; nothing was replaced.", "IDEMPOTENCY_CONFLICT");
    renameSync(temporary, prepared.file); committed = true;
    const directory = openSync(dirname(prepared.file), constants.O_RDONLY);
    try { fsyncSync(directory); } finally { closeSync(directory); }
    outcome = { changed: true, matches: true, durability: "synced" };
  } catch (error) {
    if (committed) {
      let matches = false;
      try { matches = readConfig(prepared.project, prepared.file).text === prepared.proposed; }
      catch (readError) { cleanupErrors.push(readError); }
      outcome = { changed: true, matches, durability: "unknown", warning: matches
        ? "Configuration replacement is visible; directory durability could not be confirmed."
        : "Configuration was replaced, but its current content could not be confirmed. Inspect the existing file; do not treat this as an unwritten retry." };
    } else operationError = error;
  } finally {
    for (const cleanup of [() => { if (tempFd !== undefined) closeSync(tempFd); },
      () => { if (!committed && existsSync(temporary)) unlinkSync(temporary); }, () => closeSync(lockFd), () => unlinkSync(lock)]) {
      try { cleanup(); } catch (error) { cleanupErrors.push(error); }
    }
  }
  if (operationError) throw operationError;
  if (cleanupErrors.length) outcome = { ...outcome, cleanup: "incomplete", warning:
    "Configuration was replaced; file or writer-lock cleanup could not be fully confirmed. Inspect the existing file and lock before another write." };
  return outcome;
}

export function parseOptions(argv) {
  const mapping = { "--client": "client", "--project": "project", "--python": "python", "--state-root": "stateRoot", "--mode": "mode", "--config": "config", "--check": "check" };
  const options = {};
  for (let index = 0; index < argv.length; index += 1) {
    const key = mapping[argv[index]];
    if (!key || Object.hasOwn(options, key)) fail("Unknown or duplicate generator option.");
    if (key === "check") options.check = true;
    else { const value = argv[++index]; if (!value || value.startsWith("--")) fail("Missing generator option value."); options[key] = value; }
  }
  return options;
}
export function main(argv = process.argv.slice(2)) {
  try {
    const prepared = prepareConfiguration(parseOptions(argv));
    const write = writeConfiguration(prepared);
    process.stdout.write(JSON.stringify({ schema_version: "tiger.agent-config/v1", ok: write.matches, client: prepared.client,
      project: prepared.project, config: prepared.file, mode: prepared.mode, python: prepared.python, python_version: prepared.pythonVersion,
      check: prepared.check, ...write, host_attached: false,
      credentials_verified: false,
      environment_requirements: prepared.mode === "paper" ? { source: "trusted host process environment", names: [...MODEL_ENV_NAMES],
        required_for_model: ["LLM_BACKEND", "LLM_BASE_URL", "LLM_MODEL", "LLM_API_KEY"], values_saved: false,
        validation_scope: "Configuration syntax and Python version only; no credentials or provider requests were tested." } : null,
      next: prepared.client === "codex" ? "Use this trusted project configuration in a new or explicitly refreshed Codex connection; verify tools in the actual host."
        : prepared.client === "qoder" ? "Qoder CLI: trust this project and reconnect using normal host controls; verify tools and doctor. For Qoder desktop, import a reviewed server through its connector UI. No user-global configuration was edited."
        : "Include this project plugin-list overlay in the selected DSH deployment configuration, then verify the actual host. No global configuration was edited." }) + "\n");
    process.exitCode = write.matches ? 0 : 2;
  } catch (error) {
    process.stdout.write(JSON.stringify({ schema_version: "tiger.agent-config/v1", ok: false,
      error: { code: error instanceof WorkflowError ? error.code : "CONFIG_WRITE_FAILED",
        message: sanitizeMessage(error instanceof WorkflowError ? error.message : "Unable to read or atomically write the project configuration.") } }) + "\n");
    process.exitCode = 2;
  }
}
if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) main();
