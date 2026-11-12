#!/usr/bin/env node
/** Independent Tiger MCP transport. The generic WorkLoom local/read catalog stays unchanged. */
import { pathToFileURL } from "node:url";
import {
  LIMITS, OPERATIONS, REQUEST_SCHEMAS, RESULT_SCHEMA, WorkflowError, failureEnvelope, invokeWorkflow,
  parseJson, plain, startupOptions, validateSchemaDefinition,
} from "../bundles/trading/capabilities/workflow-client.mjs";

const VERSIONS = new Set(["2025-03-26", "2025-06-18", "2025-11-25"]);
const SERVER_VERSION = "1.0.0";
// Keep the first paragraph self-contained for clients that limit initialization instructions.
export const INSTRUCTIONS = [
  "New AI research uses us-tech-intelligence-paper-v3 with public-usd. Call doctor, then describe that profile. Reuse the original request, idempotency_key and run_id; never replace a key after losing a response. accepted/running status is not completion: poll status, then receipt and verify. Report success only for completed with a verified receipt. Cancellation stops observation, not an accepted worker; the explicit cancel operation requests scheduler cancellation. Paper only; no real broker orders.",
  "Tiger runs only the fixed public US paper workflow. Run, resume, supplement and scheduler cancel are hidden and rejected in readonly deployment. Resume uses only a stopped existing run_id and never refreshes its admission or deadline. Supplement creates a new full run for verified pending symbols. Access mode, Python executable and state-root are trusted startup settings, never tool arguments. Keep the trusted state root for the selected profile and recovery; use a separate state root when changing profiles. A new logical task gets a fresh idempotency key; retries retain the identical request. The intelligence profile requires real deepseek-flash responses for all five semantic stages, a trusted source registry, original fetches, per-target coverage and a frozen evidence bundle. Disabled, passthrough, rules replacement or missing evidence is failure, never completion. Legacy profiles retain historical semantics. No live, skip, tuning, score or approval controls exist.",
].join("\n\n");
const TITLES = {
  doctor: "老虎环境自检",
  describe: "查看研究流程与约束",
  run: "启动公开美股纸面研究",
  resume: "继续原研究身份",
  supplement: "补研已审定候选",
  cancel: "请求停止研究调度",
  status: "查询既有研究进度",
  receipt: "读取已核验研究回执",
  verify: "独立核验研究证据",
};
const DESCRIPTIONS = {
  doctor: "Check the fixed public-paper runner and dependencies. No market/model calls or account writes.",
  describe: "Describe the selected profile's ordered workflow, public-data policy, output evidence and completion requirements. For new AI research, pass {\"profile\":\"us-tech-intelligence-paper-v3\"} explicitly. No business worker starts.",
  run: "Accept one complete public US paper workflow in a trusted paper deployment. Pass exactly schema_version=\"tiger.agent-request/v1\", profile, paper_account=\"public-usd\" and idempotency_key. New AI research uses profile=\"us-tech-intelligence-paper-v3\". Preserve the identical request/key on retries and the returned run_id for status, then receipt and verify. accepted/running is not completion.",
  resume: "Recover one stopped intelligence-paper run under its original admission, source, policy, paper account and deadline. Pass only its existing run_id. Unknown provider/source attempts must reconcile first; a committed/completed run is observed without starting another worker.",
  supplement: "Create a new full intelligence-paper admission for symbols that are already pending_research in a verified parent receipt. Pass schema_version=\"tiger.research-supplement-request/v1\", parent_run_id, symbols and a new idempotency_key. This never expands the parent snapshot or reuses its trade approval.",
  cancel: "Request durable scheduler cancellation for an existing research run. Pass only its run_id. This does not cancel an accepted worker; unknown external attempts retain their reservation and require reconciliation. A cancelled scheduler run cannot be resumed.",
  status: "Read the existing run's state. A successful query may describe a failed business run and never supplies a completion summary.",
  receipt: "Independently verify immutable evidence and the account commit, then read the existing run's completion summary. Never execute or resume a stage.",
  verify: "Independently verify the existing run's immutable evidence, identities, hashes and account commit. Never execute or resume a stage.",
};
export function toolName(operation) {
  if (operation === "doctor") return "tiger_doctor";
  if (operation === "supplement") return "tiger_research_supplement";
  return `tiger_workflow_${operation}`;
}
export function toolDefinitions(mode = "readonly") {
  const outputSchema = structuredClone(RESULT_SCHEMA);
  delete outputSchema.$defs;
  const mutations = new Set(["run", "resume", "supplement", "cancel"]);
  const openWorldMutations = new Set(["run", "resume", "supplement"]);
  const tools = OPERATIONS.filter((operation) => mode === "paper" || !mutations.has(operation)).map((operation) => ({
    name: toolName(operation), title: TITLES[operation], description: DESCRIPTIONS[operation], inputSchema: REQUEST_SCHEMAS[operation], outputSchema,
    annotations: { readOnlyHint: !mutations.has(operation), destructiveHint: false, idempotentHint: true, openWorldHint: openWorldMutations.has(operation) },
  }));
  return validateToolDefinitions(tools);
}
export function validateToolDefinitions(tools) {
  const names = new Set();
  for (const tool of tools) {
    if (names.has(tool.name)) throw new WorkflowError("INVALID_REQUEST", "Duplicate MCP tool name.");
    names.add(tool.name);
    validateSchemaDefinition(tool.inputSchema); validateSchemaDefinition(tool.outputSchema);
  }
  return tools;
}

export function parseStartup(argv) {
  const mapped = { "--mode": "mode", "--python": "python", "--state-root": "stateRoot" };
  const options = {};
  for (let index = 0; index < argv.length; index += 1) {
    const key = mapped[argv[index]];
    const value = argv[++index];
    if (!key || Object.hasOwn(options, key) || !value || value.startsWith("--")) throw new WorkflowError("INVALID_REQUEST", "Invalid MCP startup options.");
    options[key] = value;
  }
  return startupOptions(options);
}

export function serve(options, { stdin = process.stdin, stdout = process.stdout, stderr = process.stderr,
  invoke = invokeWorkflow } = {}) {
  const config = startupOptions(options);
  const tools = toolDefinitions(config.mode);
  const byName = new Map(OPERATIONS.map((operation) => [toolName(operation), operation]));
  const inflight = new Map();
  let state = "uninitialized";
  let pending = Buffer.alloc(0);
  let outputBytes = 0;
  let shuttingDown = false;
  let outputClosed = false;
  let drainTimer;
  let exitCode = 0;
  let resolveClosed;
  const closed = new Promise((done) => { resolveClosed = done; });

  function send(message) {
    if (outputClosed) return;
    const line = JSON.stringify(message) + "\n";
    const bytes = Buffer.byteLength(line);
    if (outputBytes + bytes > 4 * LIMITS.outputBytes) { shutdown(7, "MCP output backpressure limit exceeded."); return; }
    outputBytes += bytes;
    try {
      stdout.write(line, (error) => {
        outputBytes -= bytes;
        if (error) { outputClosed = true; shutdown(130); }
        maybeClosed();
      });
    } catch { outputBytes -= bytes; outputClosed = true; shutdown(130); }
  }
  const reply = (id, result) => send({ jsonrpc: "2.0", id, result });
  const protocolError = (id, code, message) => send({ jsonrpc: "2.0", id, error: { code, message } });
  function maybeClosed() {
    if (!shuttingDown || inflight.size || (!outputClosed && outputBytes)) return;
    stdin.removeListener("data", data);
    stdin.removeListener("end", end);
    stdin.removeListener("error", inputError);
    // A peer may keep an oversized/incomplete stdin pipe open forever.
    stdin.destroy();
    // Keep the error listener through the queued stream error event after an EPIPE write callback.
    clearTimeout(drainTimer);
    resolveClosed(exitCode);
  }
  function shutdown(code = 0, diagnostic = null) {
    if (!shuttingDown) {
      shuttingDown = true;
      state = "closing";
      exitCode = code;
      pending = Buffer.alloc(0);
      stdin.pause();
      if (diagnostic) stderr.write(diagnostic + "\n");
      for (const request of inflight.values()) request.controller.abort();
      drainTimer = setTimeout(() => { outputClosed = true; stdout.destroy(); maybeClosed(); }, LIMITS.cleanupMs + 250);
    }
    maybeClosed();
  }
  function inputError() { shutdown(7, "MCP input stream failed."); }
  function outputError() { outputClosed = true; shutdown(130); }
  function validId(id) { return (typeof id === "string" && id.length > 0 && Buffer.byteLength(id) <= 128) || Number.isSafeInteger(id); }
  function emptyParams(params) { return params === undefined || (plain(params) && Object.keys(params).every((key) => key === "_meta")); }

  function handle(request) {
    if (shuttingDown) return;
    if (!plain(request) || request.jsonrpc !== "2.0" || typeof request.method !== "string"
      || Object.keys(request).some((key) => !["jsonrpc", "id", "method", "params"].includes(key))
      || (Object.hasOwn(request, "id") && !validId(request.id))
      || (request.params !== undefined && !plain(request.params))) {
      const id = plain(request) && validId(request.id) ? request.id : null;
      // A syntactically valid notification gets no reply, even when its params are invalid.
      if (!plain(request) || Object.hasOwn(request, "id") || typeof request.method !== "string") protocolError(id, -32600, "Invalid JSON-RPC request.");
      return;
    }
    const { method, params, id } = request;
    const notification = !Object.hasOwn(request, "id");
    // Standard transport metadata never becomes Python business arguments.
    if (params?._meta !== undefined && (!plain(params._meta)
      || (!notification && Object.hasOwn(params._meta, "progressToken")
        && typeof params._meta.progressToken !== "string" && typeof params._meta.progressToken !== "number"))) {
      if (!notification) protocolError(id, -32602, "Invalid protocol metadata.");
      return;
    }
    if (notification) {
      if (method === "notifications/initialized" && state === "awaiting_initialized" && emptyParams(params)) state = "ready";
      else if (method === "notifications/cancelled" && state === "ready" && plain(params) && validId(params.requestId)
        && Object.keys(params).every((key) => ["requestId", "reason", "_meta"].includes(key))
        && (params.reason === undefined || (typeof params.reason === "string" && params.reason.length <= 1024))) {
        inflight.get(params.requestId)?.controller.abort();
      }
      return;
    }
    if (inflight.has(id)) { protocolError(id, -32600, "Request ID is already in flight."); return; }
    if (method === "ping") {
      if (!emptyParams(params)) protocolError(id, -32602, "Ping does not accept arguments.");
      else reply(id, {});
      return;
    }
    if (method === "initialize") {
      if (state !== "uninitialized") { protocolError(id, -32600, "Server is already initialized."); return; }
      if (!plain(params) || typeof params.protocolVersion !== "string" || !/^\d{4}-\d{2}-\d{2}$/u.test(params.protocolVersion)
        || !plain(params.capabilities) || !plain(params.clientInfo) || typeof params.clientInfo.name !== "string" || !params.clientInfo.name.trim()
        || params.clientInfo.name.length > 256 || typeof params.clientInfo.version !== "string" || !params.clientInfo.version.trim()
        || params.clientInfo.version.length > 128) { protocolError(id, -32602, "Invalid initialization parameters."); return; }
      state = "awaiting_initialized";
      reply(id, { protocolVersion: VERSIONS.has(params.protocolVersion) ? params.protocolVersion : "2025-11-25",
        capabilities: { tools: { listChanged: false } }, serverInfo: { name: "tiger-agent", version: SERVER_VERSION }, instructions: INSTRUCTIONS });
      return;
    }
    if (!["tools/list", "tools/call"].includes(method)) { protocolError(id, -32601, "Method not found."); return; }
    if (state !== "ready") { protocolError(id, -32002, "Initialize and send notifications/initialized before using tools."); return; }
    if (method === "tools/list") {
      if (!emptyParams(params)) protocolError(id, -32602, "Tool list has no pagination cursor or arguments.");
      else reply(id, { tools });
      return;
    }
    if (!plain(params) || typeof params.name !== "string" || !byName.has(params.name)
      || Object.keys(params).some((key) => !["name", "arguments", "_meta"].includes(key))
      || (params._meta !== undefined && !plain(params._meta))) { protocolError(id, -32602, "Unknown tool or invalid tool parameters."); return; }
    if (inflight.size >= LIMITS.concurrent) { protocolError(id, -32000, "Too many in-flight requests."); return; }
    const operation = byName.get(params.name);
    const controller = new AbortController();
    inflight.set(id, { controller });
    Promise.resolve().then(() => invoke(operation, Object.hasOwn(params, "arguments") ? params.arguments : {}, config, { signal: controller.signal }))
      .catch((error) => failureEnvelope(operation, error))
      .then((envelope) => {
        reply(id, { content: [{ type: "text", text: JSON.stringify(envelope) }], structuredContent: envelope,
          isError: !envelope.operation_ok || (["run", "resume", "supplement"].includes(operation)
            && ["failed", "blocked", "interrupted"].includes(envelope.status)) });
      }).finally(() => { inflight.delete(id); maybeClosed(); });
  }

  function frame(bytes) {
    if (!bytes.toString("utf8").trim()) return;
    try { handle(parseJson(bytes, LIMITS.frameBytes)); }
    catch { protocolError(null, -32700, "Malformed UTF-8 JSON-RPC frame."); }
  }
  function data(chunk) {
    const bytes = Buffer.isBuffer(chunk) ? chunk : Buffer.from(chunk);
    let start = 0;
    while (start < bytes.length && !shuttingDown) {
      const newline = bytes.indexOf(10, start);
      const endOffset = newline < 0 ? bytes.length : newline;
      const part = bytes.subarray(start, endOffset);
      if (pending.length + part.length > LIMITS.frameBytes) {
        protocolError(null, -32600, "JSON-RPC frame exceeds the byte limit.");
        shutdown(2); return;
      }
      if (part.length) pending = pending.length ? Buffer.concat([pending, part]) : Buffer.from(part);
      if (newline < 0) return;
      const complete = pending;
      pending = Buffer.alloc(0);
      frame(complete);
      start = newline + 1;
    }
  }
  function end() {
    if (pending.length) protocolError(null, -32700, "Incomplete JSON-RPC frame at EOF.");
    shutdown(0);
  }
  stdin.on("data", data); stdin.once("end", end); stdin.once("error", inputError); stdout.on("error", outputError);
  stdout.once("close", () => stdout.removeListener("error", outputError));
  return { closed, shutdown, get state() { return state; } };
}

export async function main(argv = process.argv.slice(2)) {
  let server;
  try { server = serve(parseStartup(argv)); }
  catch {
    process.stderr.write("Tiger MCP startup configuration or schema is invalid.\n");
    process.exitCode = 2; return;
  }
  const onSignal = () => server.shutdown(130);
  process.once("SIGINT", onSignal); process.once("SIGTERM", onSignal);
  process.exitCode = await server.closed;
  process.removeListener("SIGINT", onSignal); process.removeListener("SIGTERM", onSignal);
}
if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) await main();
