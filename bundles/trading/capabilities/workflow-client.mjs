/** Tiger's fixed Python protocol. No business stages or account writes live in Node. */
import { spawn } from "node:child_process";
import { existsSync, readFileSync } from "node:fs";
import { open } from "node:fs/promises";
import { isAbsolute, resolve } from "node:path";

export const ENGINE_ROOT = resolve(import.meta.dirname, "../../..");
export const PROFILE = "us-public-paper-v1";
export const REQUIRED_PROFILE = "us-public-model-required-paper-v1";
export const INTELLIGENCE_PROFILE = "us-public-intelligence-paper-v2";
export const OPERATIONS = Object.freeze(["doctor", "describe", "run", "status", "receipt", "verify"]);
export const LIMITS = Object.freeze({ inputBytes: 65536, outputBytes: 1024 * 1024, stderrBytes: 65536,
  frameBytes: 131072, requestMs: 60000, waitMs: 2460000, pollMs: 1000, depth: 64, concurrent: 4,
  terminateMs: 250, cleanupMs: 2000 });
const RUN_ID = /^run-[0-9a-f]{32}$/u;
const TERMINAL_OK = new Set(["completed", "completed_degraded"]);
const ENV_NAMES = ["PATH", "HOME", "LANG", "LC_ALL", "LC_CTYPE", "TZ", "TMPDIR", "TMP", "TEMP",
  "SSL_CERT_FILE", "SSL_CERT_DIR", "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE",
  "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY", "http_proxy", "https_proxy", "all_proxy", "no_proxy"];
// Client launchers forward these names only; values stay in the trusted process environment.
export const MODEL_ENV_NAMES = Object.freeze(["LLM_BACKEND", "LLM_BASE_URL", "LLM_MODEL", "LLM_API_KEY", "TIGER_SEC_CONTACT",
  "TIGER_INTEL_BRAVE_API_KEY", "TIGER_INTEL_TAVILY_API_KEY", "TIGER_INTEL_EXA_API_KEY",
  "TIGER_INTEL_FRED_API_KEY", "TIGER_INTEL_SEC_USER_AGENT",
  "TIGER_INTEL_REGISTRY_FILE", "TIGER_INTEL_ARCHIVE_ROOT", "TIGER_AGENT_RUNTIME_POLICY_FILE"]);
const EXIT_CODES = new Map([
  ...["INVALID_REQUEST", "INPUT_TOO_LARGE"].map((s) => [s, 2]),
  ...["READ_ONLY", "LIVE_FORBIDDEN", "UNAUTHORIZED"].map((s) => [s, 3]),
  ...["IDEMPOTENCY_CONFLICT", "ACCOUNT_BUSY", "ACCOUNT_VERSION_CONFLICT"].map((s) => [s, 5]),
  ...["DATA_UNAVAILABLE", "DATA_STALE", "SOURCE_SYNTHETIC", "CURRENCY_BLOCKED"].map((s) => [s, 6]),
  ...["RUNNER_FAILED", "RESULT_INVALID", "EVIDENCE_MISSING", "EVIDENCE_HASH_MISMATCH", "ARTIFACT_WRITE_FAILED", "RUN_NOT_FOUND"].map((s) => [s, 7]),
  ...["STEP_ORDER_VIOLATION", "STEP_FAILED", "RUN_INCOMPLETE", "MODEL_REQUIRED_FAILED"].map((s) => [s, 8]),
  ["TIMEOUT", 9], ["INTERRUPTED", 9], ["CANCELLED", 130],
]);

export class WorkflowError extends Error {
  constructor(code, message, { runId = null, cause } = {}) {
    super(message, { cause });
    this.name = "WorkflowError";
    this.code = code;
    this.runId = RUN_ID.test(runId ?? "") ? runId : null;
  }
}
export function plain(value) {
  return value !== null && typeof value === "object" && !Array.isArray(value)
    && [Object.prototype, null].includes(Object.getPrototypeOf(value));
}
function invalid(message = "Input does not match the fixed Tiger request schema.") {
  return new WorkflowError("INVALID_REQUEST", message);
}

/** Parse before JSON.stringify can discard duplicate (including escaped) object keys. */
export function parseJson(source, maxBytes = LIMITS.inputBytes) {
  const bytes = Buffer.isBuffer(source) ? source : Buffer.from(source, "utf8");
  if (bytes.length > maxBytes) throw new WorkflowError("INPUT_TOO_LARGE", "JSON exceeds the byte limit.");
  let text;
  try { text = new TextDecoder("utf-8", { fatal: true }).decode(bytes); }
  catch (cause) { throw new WorkflowError("INVALID_REQUEST", "Input must be valid UTF-8 JSON.", { cause }); }
  let cursor = 0;
  let nodes = 0;
  function space() { while (/[\t\n\r ]/u.test(text[cursor] ?? "x")) cursor += 1; }
  function string() {
    const start = cursor++;
    let escaped = false;
    for (; cursor < text.length; cursor += 1) {
      const char = text[cursor];
      if (!escaped && char === '"') {
        cursor += 1;
        try { return JSON.parse(text.slice(start, cursor)); }
        catch { throw invalid("Malformed JSON string."); }
      }
      if (!escaped && char === "\\") escaped = true;
      else escaped = false;
    }
    throw invalid("Unterminated JSON string.");
  }
  function value(depth) {
    if (depth > LIMITS.depth || ++nodes > 100000) throw invalid("JSON nesting or element limit exceeded.");
    space();
    if (text[cursor] === '"') return string();
    if (text[cursor] === "{") {
      cursor += 1;
      const result = {};
      const keys = new Set();
      space();
      if (text[cursor] === "}") { cursor += 1; return result; }
      for (;;) {
        space();
        if (text[cursor] !== '"') throw invalid("JSON object keys must be strings.");
        const key = string();
        if (keys.has(key)) throw invalid("Duplicate JSON object key.");
        keys.add(key);
        space();
        if (text[cursor++] !== ":") throw invalid("Malformed JSON object.");
        Object.defineProperty(result, key, { value: value(depth + 1), enumerable: true, configurable: true, writable: true });
        space();
        const separator = text[cursor++];
        if (separator === "}") return result;
        if (separator !== ",") throw invalid("Malformed JSON object.");
      }
    }
    if (text[cursor] === "[") {
      cursor += 1;
      const result = [];
      space();
      if (text[cursor] === "]") { cursor += 1; return result; }
      for (;;) {
        result.push(value(depth + 1));
        space();
        const separator = text[cursor++];
        if (separator === "]") return result;
        if (separator !== ",") throw invalid("Malformed JSON array.");
      }
    }
    for (const [literal, result] of [["true", true], ["false", false], ["null", null]]) {
      if (text.startsWith(literal, cursor)) { cursor += literal.length; return result; }
    }
    const match = /^-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)?/u.exec(text.slice(cursor));
    if (!match) throw invalid("Input is not valid JSON.");
    cursor += match[0].length;
    const number = Number(match[0]);
    if (!Number.isFinite(number)) throw invalid("JSON numbers must be finite.");
    return number;
  }
  const result = value(0);
  space();
  if (cursor !== text.length) throw invalid("Input must contain exactly one JSON value.");
  return result;
}

const SCHEMA_KEYS = new Set(["$schema", "$id", "$defs", "title", "description", "type", "properties", "required",
  "additionalProperties", "items", "enum", "const", "pattern", "minLength", "maxLength", "minimum", "maximum", "minItems", "maxItems"]);
const TYPES = new Set(["object", "array", "string", "number", "integer", "boolean", "null"]);
/** Fail closed if a reviewed schema adds a keyword this validator does not implement. */
export function validateSchemaDefinition(schema, depth = 0) {
  if (!plain(schema) || depth > LIMITS.depth) throw invalid("Invalid schema definition.");
  for (const key of Object.keys(schema)) if (!SCHEMA_KEYS.has(key)) throw invalid("Unsupported schema keyword.");
  for (const key of ["$schema", "$id", "title", "description"]) {
    if (key in schema && typeof schema[key] !== "string") throw invalid("Invalid schema metadata.");
  }
  if (schema.type !== undefined) {
    const types = Array.isArray(schema.type) ? schema.type : [schema.type];
    if (!types.length || types.some((type) => !TYPES.has(type)) || new Set(types).size !== types.length) throw invalid("Invalid schema type.");
  }
  for (const key of ["properties", "$defs"]) if (schema[key] !== undefined) {
    if (!plain(schema[key])) throw invalid("Invalid schema properties.");
    for (const child of Object.values(schema[key])) validateSchemaDefinition(child, depth + 1);
  }
  if (schema.required !== undefined && (!Array.isArray(schema.required)
    || schema.required.some((key) => typeof key !== "string" || !Object.hasOwn(schema.properties ?? {}, key))
    || new Set(schema.required).size !== schema.required.length)) throw invalid("Invalid schema required list.");
  if (schema.additionalProperties !== undefined && typeof schema.additionalProperties !== "boolean") throw invalid("Unsupported additionalProperties schema.");
  if (schema.items !== undefined) validateSchemaDefinition(schema.items, depth + 1);
  if (schema.enum !== undefined && (!Array.isArray(schema.enum) || !schema.enum.length
    || schema.enum.some((v) => v !== null && !["string", "boolean", "number"].includes(typeof v)))) throw invalid("Invalid scalar enum.");
  if ("const" in schema && schema.const !== null && !["string", "boolean", "number"].includes(typeof schema.const)) throw invalid("Unsupported non-scalar const.");
  if (schema.pattern !== undefined) {
    if (typeof schema.pattern !== "string" || schema.pattern.length > 1024) throw invalid("Invalid schema pattern.");
    try { new RegExp(schema.pattern, "u"); } catch { throw invalid("Invalid schema pattern."); }
  }
  for (const key of ["minLength", "maxLength", "minItems", "maxItems"]) {
    if (schema[key] !== undefined && (!Number.isSafeInteger(schema[key]) || schema[key] < 0)) throw invalid("Invalid schema bound.");
  }
  for (const key of ["minimum", "maximum"]) if (schema[key] !== undefined && !Number.isFinite(schema[key])) throw invalid("Invalid schema numeric bound.");
  return schema;
}

function matchesType(value, type) {
  return type === "null" ? value === null : type === "object" ? plain(value)
    : type === "array" ? Array.isArray(value) : type === "integer" ? Number.isSafeInteger(value)
      : type === "number" ? typeof value === "number" && Number.isFinite(value) : typeof value === type;
}
function validateValue(value, schema) {
  if (schema.type !== undefined && !(Array.isArray(schema.type) ? schema.type : [schema.type]).some((t) => matchesType(value, t))) throw invalid();
  if (schema.enum && !schema.enum.includes(value)) throw invalid();
  if ("const" in schema && value !== schema.const) throw invalid();
  if (typeof value === "number" && (!Number.isFinite(value) || (schema.minimum !== undefined && value < schema.minimum)
    || (schema.maximum !== undefined && value > schema.maximum))) throw invalid();
  if (typeof value === "string" && ((schema.minLength !== undefined && [...value].length < schema.minLength)
    || (schema.maxLength !== undefined && [...value].length > schema.maxLength)
    || (schema.pattern !== undefined && !new RegExp(schema.pattern, "u").test(value)))) throw invalid();
  if (Array.isArray(value)) {
    if ((schema.minItems !== undefined && value.length < schema.minItems) || (schema.maxItems !== undefined && value.length > schema.maxItems)) throw invalid();
    if (schema.items) for (const item of value) validateValue(item, schema.items);
  } else if (plain(value)) {
    for (const key of schema.required ?? []) if (!Object.hasOwn(value, key)) throw invalid();
    for (const [key, child] of Object.entries(value)) {
      if (Object.hasOwn(schema.properties ?? {}, key)) validateValue(child, schema.properties[key]);
      else if (schema.additionalProperties === false) throw invalid();
    }
  }
  return value;
}
function deepFreeze(value) {
  if (value && typeof value === "object") { Object.freeze(value); for (const child of Object.values(value)) deepFreeze(child); }
  return value;
}
export const RESULT_SCHEMA = deepFreeze(validateSchemaDefinition(parseJson(
  readFileSync(resolve(import.meta.dirname, "../agent-workflow.schema.json")), 262144)));
export const REQUEST_SCHEMAS = deepFreeze(Object.fromEntries(OPERATIONS.map((operation) =>
  [operation, RESULT_SCHEMA.$defs[`${operation}_request`]])));

export function validateRequest(operation, input) {
  if (!OPERATIONS.includes(operation)) throw invalid("Unknown Tiger operation.");
  // Reparse callers' objects as well, to reject NaN/Infinity/undefined rather than normalize them away.
  function finite(value, depth = 0) {
    if (depth > LIMITS.depth || value === undefined || typeof value === "function" || typeof value === "symbol"
      || typeof value === "bigint" || (typeof value === "number" && !Number.isFinite(value))) throw invalid();
    if (value && typeof value === "object") for (const child of Object.values(value)) finite(child, depth + 1);
  }
  finite(input);
  validateValue(input, REQUEST_SCHEMAS[operation]);
  if (Buffer.byteLength(JSON.stringify(input)) > LIMITS.inputBytes) throw new WorkflowError("INPUT_TOO_LARGE", "Request exceeds the byte limit.");
  return input;
}

export function safeEvidencePath(value) {
  return typeof value === "string" && value.length > 0 && value.length <= 1024 && !/[\\\x00-\x1f%]/u.test(value)
    && !isAbsolute(value) && !/^[A-Za-z][A-Za-z0-9+.-]*:/u.test(value)
    && value.split("/").every((part) => part !== "" && part !== "." && part !== "..");
}

function validMarketDate(value) {
  if (typeof value !== "string" || value.startsWith("0000-") || !/^[0-9]{4}-[0-9]{2}-[0-9]{2}$/u.test(value)) return false;
  const milliseconds = Date.parse(value + "T00:00:00Z");
  return Number.isFinite(milliseconds) && new Date(milliseconds).toISOString().slice(0, 10) === value;
}

function validateModelInstructions(summary) {
  const researchDate = summary.as_of.market_date;
  if (!validMarketDate(researchDate)) throw invalid();
  const symbols = new Set();
  for (const row of summary.execution_decisions) {
    const market = row.market_evidence[0];
    const plan = ["entry_price", "stop_price", "shares", "risk_usd"];
    if (symbols.has(row.symbol) || market.symbol !== row.symbol || market.close <= 0
      || !validMarketDate(market.as_of) || market.as_of > researchDate
      || market.id !== `md_${row.symbol}_${market.as_of.replaceAll("-", "")}_${market.series_sha256.slice(0, 24)}`
      || row.reference_capital_usd <= 0 || !validMarketDate(row.valid_for.slice(0, 10))
      || row.valid_for.slice(0, 10) <= researchDate || !Number.isFinite(Date.parse(row.valid_for))
      || row.action === "buy" && (row.sources.length === 0 || row.target_weight <= 0
        || plan.some(key => row[key] === null || row[key] <= 0) || row.stop_price >= row.entry_price)
      || row.action === "watch" && (row.target_weight !== 0
        || [...plan, "time_stop_days"].some(key => row[key] !== null))
      || ["action_summary", "why_this", "why_now", "entry_condition", "invalidation", "risk_note"].some(
        key => !row[key].trim() || /[<>\x00-\x1f\x7f-\x9f\u200b-\u200f\u202a-\u202e\u2066-\u2069]/u.test(row[key]))
      || new Set(row.sources.map(source => source.id)).size !== row.sources.length) throw invalid();
    symbols.add(row.symbol);
  }
}

export function validateEnvelope(envelope, operation, input = {}) {
  try {
    validateValue(envelope, RESULT_SCHEMA);
    if (envelope.operation !== operation) throw invalid();
    if (input.run_id && envelope.run_id !== input.run_id) throw invalid();
    if (envelope.operation_ok !== (envelope.error === null)) throw invalid();
    if (envelope.error && envelope.error.run_id !== envelope.run_id) throw invalid();
    if (envelope.error?.details_ref !== null && envelope.error?.details_ref !== undefined && !safeEvidencePath(envelope.error.details_ref)) throw invalid();
    for (const ref of envelope.evidence_refs) if (!safeEvidencePath(ref.path)) throw invalid();
    if (["run", "status", "doctor", "describe"].includes(operation)
      && (envelope.summary !== null || envelope.verification !== null)) throw invalid();
    if (operation === "run" && envelope.operation_ok
      && (!["accepted", "running"].includes(envelope.status) || !envelope.run_id || envelope.execution_mode !== "paper")) throw invalid();
    if (envelope.summary !== null && (operation !== "receipt" || !envelope.operation_ok
      || !TERMINAL_OK.has(envelope.status) || envelope.verification?.ok !== true)) throw invalid();
    if (operation === "receipt" && envelope.operation_ok
      && (!TERMINAL_OK.has(envelope.status) || envelope.summary === null || envelope.verification?.ok !== true
        || !envelope.verification.receipt_sha256)) throw invalid();
    if (["receipt", "verify"].includes(operation) && envelope.operation_ok
      && (!envelope.run_id || envelope.execution_mode !== "paper" || !TERMINAL_OK.has(envelope.status)
        || envelope.verification?.ok !== true || !envelope.verification.receipt_sha256)) throw invalid();
    if (operation === "verify" && envelope.summary !== null) throw invalid();
    if (envelope.summary?.disclosures?.model_mode === "enabled") {
      const model = envelope.summary.model;
      const stages = ["clean.llm_semantic", "tech.sentiment", "tech.risk", "sector.narrative", "decision.debate"];
      if (envelope.status !== "completed" || envelope.summary.disclosures.model_required !== true
        || !plain(model) || model.required_stages.join("|") !== stages.join("|")
        || new Set(model.response_ids).size !== model.call_count || model.response_ids.length !== model.call_count
        || Object.keys(model.stage_counts).sort().join("|") !== [...stages].sort().join("|")
        || stages.some(stage => !Number.isSafeInteger(model.stage_counts[stage]) || model.stage_counts[stage] < 1)
        || Object.values(model.stage_counts).reduce((sum, count) => sum + count, 0) !== model.call_count
        || typeof envelope.summary.action_summary !== "string" || !envelope.summary.action_summary.trim()
        || !Array.isArray(envelope.summary.execution_decisions)) throw invalid();
      if (model.search_mode === "provider_native_search" && model.search_count !== 14) throw invalid();
      if (model.search_mode === "intelligence_source_registry") {
        const intelligence = envelope.summary.disclosures.intelligence;
        if (intelligence?.contract !== "tiger.intelligence-acquisition.v2"
          || model.acquisition_contract !== intelligence.contract
          || model.bundle_id !== intelligence.bundle?.bundle_id
          || !/^bundle_[0-9a-f]{32}$/u.test(model.bundle_id ?? "")
          || !/^[0-9a-f]{64}$/u.test(intelligence.bundle?.manifest_sha256 ?? "")
          || !Number.isSafeInteger(model.source_requests) || model.source_requests < 0
          || model.source_requests !== intelligence.bundle?.usage?.requests
          || !Number.isSafeInteger(model.fetch_count) || model.fetch_count < 0) throw invalid();
      }
      validateModelInstructions(envelope.summary);
    }
  } catch (cause) { throw new WorkflowError("RESULT_INVALID", "Python returned an invalid or unverified result envelope.", { runId: input.run_id, cause }); }
  return envelope;
}

export function sanitizeMessage(message, env = process.env) {
  let result = typeof message === "string" ? message : "The operation failed.";
  for (const [name, value] of Object.entries(env)) {
    if (/KEY|TOKEN|SECRET|PASSWORD|CREDENTIAL/iu.test(name) && typeof value === "string" && value.length >= 4) result = result.split(value).join("[REDACTED]");
  }
  result = result.replace(/Bearer\s+[^\s,;]+/giu, "Bearer [REDACTED]")
    .replace(/https?:\/\/[^\s<>]+/giu, "[URL omitted]")
    .replace(/\b(?:token|password|api[_-]?key|secret)\s*[:=]\s*[^\s,;]+/giu, "credential=[REDACTED]")
    .replace(/[\x00-\x1f\x7f]/gu, " ");
  if (/Traceback \(most recent call last\)/u.test(result)) result = "Python reported an error. Inspect the controlled run evidence.";
  return result.slice(0, 512).trim() || "The operation failed.";
}
export function failureEnvelope(operation, error, runId = null) {
  const known = error instanceof WorkflowError;
  const code = known ? error.code : "RUNNER_FAILED";
  const id = RUN_ID.test(runId ?? "") ? runId : known ? error.runId : null;
  const interrupted = ["CANCELLED", "TIMEOUT", "INTERRUPTED"].includes(code);
  return {
    schema_version: "tiger.agent-result/v1", operation: OPERATIONS.includes(operation) ? operation : null,
    operation_ok: false, run_id: id,
    status: interrupted ? "interrupted" : code === "RUN_NOT_FOUND" ? "not_found" : code === "READ_ONLY" ? "blocked" : "invalid",
    execution_mode: id ? "paper" : null, data: {}, summary: null, verification: null,
    limitations: interrupted ? [id ? "Observation stopped. Query the existing run_id; accepted workers continue independently."
      : "Acceptance may have occurred before the response was lost. Recover using the identical original request and idempotency key; do not generate a new key."] : [],
    evidence_refs: [], error: { code, message: sanitizeMessage(known ? error.message : "The runner could not complete this operation."),
      run_id: id, stage: null, retryable: false, details_ref: null },
  };
}
export function exitCode(envelope) { return envelope.operation_ok ? 0 : EXIT_CODES.get(envelope.error?.code) ?? 7; }

export function runtimeEnvironment(env = process.env, modelRequired = false) {
  const result = Object.fromEntries(ENV_NAMES.filter((key) => typeof env[key] === "string").map((key) => [key, env[key]]));
  // Read-only doctor/describe may inspect this non-secret trusted config;
  // model credentials still cross only the explicit required-model boundary.
  if (typeof env.TIGER_AGENT_RUNTIME_POLICY_FILE === "string") result.TIGER_AGENT_RUNTIME_POLICY_FILE = env.TIGER_AGENT_RUNTIME_POLICY_FILE;
  if (modelRequired) for (const key of MODEL_ENV_NAMES) {
    if (typeof env[key] === "string") result[key] = env[key];
  }
  return { ...result, PYTHONNOUSERSITE: "1", PYTHONDONTWRITEBYTECODE: "1", PYTHONUNBUFFERED: "1", PYTHONUTF8: "1" };
}
export function startupOptions(options = {}) {
  for (const key of Object.keys(options)) if (!["mode", "python", "stateRoot"].includes(key)) throw invalid("Unknown startup option.");
  const mode = options.mode ?? "readonly";
  const python = options.python ?? (existsSync(resolve(ENGINE_ROOT, ".venv/bin/python")) ? resolve(ENGINE_ROOT, ".venv/bin/python") : "python3");
  const stateRoot = options.stateRoot ?? resolve(ENGINE_ROOT, ".tiger-agent");
  if (!["readonly", "paper"].includes(mode)) throw invalid("Access mode must be readonly or paper.");
  if (typeof python !== "string" || !python || /[\x00-\x1f]/u.test(python) || python.startsWith("-")) throw invalid("Invalid trusted Python executable.");
  if (typeof stateRoot !== "string" || !isAbsolute(stateRoot) || /[\x00-\x1f]/u.test(stateRoot)) throw invalid("state-root must be an absolute directory.");
  if (!["darwin", "linux"].includes(process.platform)) throw invalid("Tiger agent currently supports macOS and Linux.");
  return Object.freeze({ mode, python, stateRoot: resolve(stateRoot) });
}

/** No env/argv/root override comes from a business request. Accepted detached workers are owned by Python. */
export async function invokeWorkflow(operation, input, options = {}, { signal, timeoutMs = LIMITS.requestMs } = {}) {
  let config;
  try {
    config = startupOptions(options);
    validateRequest(operation, input);
    if (operation === "run" && config.mode !== "paper") throw new WorkflowError("READ_ONLY", "Run is unavailable in readonly deployment.");
    if (signal?.aborted) throw new WorkflowError("CANCELLED", "Request observation was cancelled.", { runId: input.run_id });
  } catch (error) { return failureEnvelope(operation, error, input?.run_id); }
  const args = ["-B", "-m", "trading_system.agent_runner", "--mode", config.mode, "--state-root", config.stateRoot, operation];
  try {
    const output = await new Promise((resolveResult, rejectResult) => {
      let child;
      let failure = null;
      let stdoutBytes = 0;
      let stderrBytes = 0;
      const chunks = [];
      let forcedTimer;
      let cleanupTimer;
      let requestTimer;
      let settled = false;
      function kill(groupSignal) {
        if (!child?.pid) return;
        try { process.kill(-child.pid, groupSignal); }
        catch (cause) { if (cause.code !== "ESRCH") failure ??= new WorkflowError("RUNNER_FAILED", "Unable to terminate the request process group.", { cause }); }
      }
      function finish(error, value) {
        if (settled) return;
        settled = true;
        clearTimeout(requestTimer); clearTimeout(forcedTimer); clearTimeout(cleanupTimer);
        signal?.removeEventListener("abort", onAbort);
        if (error) rejectResult(error); else resolveResult(value);
      }
      function stop(error) {
        if (settled || failure) return;
        failure = error;
        kill("SIGTERM");
        forcedTimer = setTimeout(() => kill("SIGKILL"), LIMITS.terminateMs);
        cleanupTimer = setTimeout(() => {
          kill("SIGKILL");
          child.stdin.destroy(); child.stdout.destroy(); child.stderr.destroy();
          finish(new WorkflowError("RUNNER_FAILED", "Request process cleanup exceeded its deadline.", { runId: input.run_id }));
        }, LIMITS.cleanupMs);
      }
      function onAbort() { stop(new WorkflowError("CANCELLED", "Request observation was cancelled.", { runId: input.run_id })); }
      try { child = spawn(config.python, args, { cwd: ENGINE_ROOT, env: runtimeEnvironment(process.env, operation === "run" && [REQUIRED_PROFILE, INTELLIGENCE_PROFILE].includes(input.profile)), detached: true, stdio: ["pipe", "pipe", "pipe"] }); }
      catch (cause) { finish(new WorkflowError("RUNNER_FAILED", "Unable to start the configured Python executable.", { runId: input.run_id, cause })); return; }
      child.on("error", (cause) => finish(new WorkflowError("RUNNER_FAILED", "Unable to start the configured Python executable.", { runId: input.run_id, cause })));
      child.stdout.on("data", (chunk) => {
        stdoutBytes += chunk.length;
        if (stdoutBytes > LIMITS.outputBytes) stop(new WorkflowError("RESULT_INVALID", "Runner output exceeds the response limit; no partial result is accepted.", { runId: input.run_id }));
        else if (!failure) chunks.push(chunk);
      });
      child.stderr.on("data", (chunk) => {
        stderrBytes += chunk.length;
        if (stderrBytes > LIMITS.stderrBytes) stop(new WorkflowError("RUNNER_FAILED", "Runner diagnostics exceed the byte limit.", { runId: input.run_id }));
      });
      child.stdin.on("error", (cause) => {
        if (cause.code !== "EPIPE") stop(new WorkflowError("RUNNER_FAILED", "Unable to deliver the request to Python.", { runId: input.run_id, cause }));
      });
      child.on("close", (code, exitSignal) => {
        // A closed request owns no continuing work, even if validation fails below.
        // Accepted Python workers own separate sessions and are outside this group.
        kill("SIGKILL");
        finish(failure, { bytes: Buffer.concat(chunks), code, signal: exitSignal });
      });
      requestTimer = setTimeout(() => stop(new WorkflowError("TIMEOUT", "The request exceeded its observation deadline.", { runId: input.run_id })), timeoutMs);
      signal?.addEventListener("abort", onAbort, { once: true });
      child.stdin.end(JSON.stringify(input) + "\n");
      if (signal?.aborted) onAbort();
    });
    if (output.signal || output.code === null) throw new WorkflowError("RUNNER_FAILED", "Python exited without a normal exit status.", { runId: input.run_id });
    let envelope;
    try { envelope = validateEnvelope(parseJson(output.bytes, LIMITS.outputBytes), operation, input); }
    catch (cause) { throw new WorkflowError("RESULT_INVALID", "Python did not return one valid, verified protocol envelope.", { runId: input.run_id, cause }); }
    if (output.code !== exitCode(envelope)) throw new WorkflowError("RUNNER_FAILED", "Python exit status contradicts its operation result.", { runId: envelope.run_id });
    if (envelope.error) envelope.error.message = sanitizeMessage(envelope.error.message);
    return envelope;
  } catch (error) { return failureEnvelope(operation, error, input.run_id); }
}

/** Bounded file/stdin reading, including a byte check before parsing or buffering a whole file. */
export async function readRequestInput(file, stream = process.stdin, { signal } = {}) {
  let handle;
  let input = stream;
  const chunks = [];
  let bytes = 0;
  const onAbort = () => input.destroy(new WorkflowError("CANCELLED", "Input reading was cancelled."));
  try {
    if (file !== "-") {
      handle = await open(file, "r");
      const stat = await handle.stat();
      if (!stat.isFile()) throw invalid("Input must be a regular JSON file.");
      if (stat.size > LIMITS.inputBytes) throw new WorkflowError("INPUT_TOO_LARGE", "Input file exceeds the byte limit.");
      input = handle.createReadStream({ autoClose: false, highWaterMark: 16384 });
    }
    signal?.addEventListener("abort", onAbort, { once: true });
    if (signal?.aborted) throw new WorkflowError("CANCELLED", "Input reading was cancelled.");
    for await (const chunk of input) {
      const part = Buffer.isBuffer(chunk) ? chunk : Buffer.from(chunk);
      bytes += part.length;
      if (bytes > LIMITS.inputBytes) throw new WorkflowError("INPUT_TOO_LARGE", "Input exceeds the byte limit.");
      chunks.push(part);
    }
    return parseJson(Buffer.concat(chunks));
  } catch (error) {
    if (error instanceof WorkflowError) throw error;
    throw invalid("Unable to read a valid JSON request.");
  } finally {
    signal?.removeEventListener("abort", onAbort);
    if (handle) await handle.close();
  }
}

export function abortableDelay(ms, signal) {
  return new Promise((done, reject) => {
    if (signal?.aborted) { reject(new WorkflowError("CANCELLED", "Observation was cancelled.")); return; }
    const cancel = () => { clearTimeout(timer); reject(new WorkflowError("CANCELLED", "Observation was cancelled.")); };
    const timer = setTimeout(() => { signal?.removeEventListener("abort", cancel); done(); }, ms);
    signal?.addEventListener("abort", cancel, { once: true });
  });
}

export async function waitForReceipt(runId, options, { signal, waitMs = LIMITS.waitMs, pollMs = LIMITS.pollMs,
  invoke = invokeWorkflow } = {}) {
  const deadline = Date.now() + waitMs;
  try {
    validateRequest("status", { run_id: runId });
    for (;;) {
      if (signal?.aborted) throw new WorkflowError("CANCELLED", "Observation was cancelled.");
      const remaining = deadline - Date.now();
      if (remaining <= 0) throw new WorkflowError("TIMEOUT", "Waiting for the existing run exceeded the observation deadline.");
      const current = await invoke("status", { run_id: runId }, options, { signal, timeoutMs: Math.min(remaining, LIMITS.requestMs) });
      if (!current.operation_ok) return current;
      if (TERMINAL_OK.has(current.status)) {
        const receipt = await invoke("receipt", { run_id: runId }, options, { signal, timeoutMs: Math.min(Math.max(1, deadline - Date.now()), LIMITS.requestMs) });
        return receipt;
      }
      if (!["accepted", "running"].includes(current.status)) {
        const failed = failureEnvelope("run", new WorkflowError("RUN_INCOMPLETE", "The existing run did not complete. Query its status; do not submit a replacement key."), runId);
        failed.status = ["failed", "blocked", "interrupted"].includes(current.status) ? current.status : "invalid";
        failed.data = { observed_status: current.status };
        return failed;
      }
      await abortableDelay(Math.min(pollMs, Math.max(1, deadline - Date.now())), signal);
    }
  } catch (error) { return failureEnvelope("run", error, runId); }
}
