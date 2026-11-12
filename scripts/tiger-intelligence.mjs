#!/usr/bin/env node
/** Registered-source acquisition only. No free-form request command or URL. */
import { spawn } from "node:child_process";
import { existsSync } from "node:fs";
import { isAbsolute, resolve } from "node:path";
import { pathToFileURL } from "node:url";
import { parseJson, readRequestInput, runtimeEnvironment } from "../bundles/trading/capabilities/workflow-client.mjs";

export const ROOT = resolve(import.meta.dirname, "..");
const OPS = new Set(["sync", "monitor", "status", "evidence"]);
const SOURCES = new Set(["web_page", "deepseek", "brave", "tavily", "exa", "sec_directory", "sec_current", "sec_submissions", "fed_policy", "bls_calendar", "fred", "cboe_vix"]);
const KEYS = ["LLM_API_KEY", "TIGER_INTEL_BRAVE_API_KEY", "TIGER_INTEL_TAVILY_API_KEY", "TIGER_INTEL_EXA_API_KEY", "TIGER_INTEL_FRED_API_KEY", "TIGER_INTEL_SEC_USER_AGENT"];
const RESULT_BYTES = 8 * 1024 * 1024;

class CommandError extends Error {
  constructor(code) { super(code); this.code = code; }
}
function invalid() { throw new CommandError("INVALID_STARTUP"); }
function number(value, low, high) {
  if (!/^[1-9][0-9]*$/u.test(value ?? "") || Number(value) < low || Number(value) > high) invalid();
  return Number(value);
}
export function parseArgs(argv) {
  const operation = argv[0];
  if (!OPS.has(operation)) invalid();
  const flags = new Map();
  const sources = [];
  const allowed = new Set(["--python", "--archive-root", "--mode", "--input-file", "--enable-source", "--cycles", "--interval-seconds", "--wall-seconds"]);
  for (let index = 1; index < argv.length; index += 1) {
    const flag = argv[index];
    const value = argv[++index];
    if (!allowed.has(flag) || !value || value.startsWith("--") || /[\x00-\x1f]/u.test(value)) invalid();
    if (flag === "--enable-source") {
      if (!SOURCES.has(value) || sources.includes(value)) invalid();
      sources.push(value);
    } else {
      if (flags.has(flag)) invalid();
      flags.set(flag, value);
    }
  }
  const mode = flags.get("--mode") ?? "readonly";
  const archiveRoot = flags.get("--archive-root");
  const python = flags.get("--python") ?? (existsSync(resolve(ROOT, ".venv/bin/python")) ? resolve(ROOT, ".venv/bin/python") : "python3");
  if (!["readonly", "acquire"].includes(mode) || !archiveRoot || !isAbsolute(archiveRoot) || python.startsWith("-")) invalid();
  const cycles = number(flags.get("--cycles") ?? "1", 1, 12);
  const interval = number(flags.get("--interval-seconds") ?? "30", 1, 300);
  const wall = number(flags.get("--wall-seconds") ?? "300", 60, 3600);
  if (operation !== "monitor" && cycles !== 1) invalid();
  return { operation, mode, archiveRoot: resolve(archiveRoot), python, cycles, interval, wall, sources,
    file: flags.get("--input-file") ?? "-" };
}

export function failure(operation, code) {
  return { schema: "tiger.intelligence-operation/v1", operation: OPS.has(operation) ? operation : null,
    operation_ok: false, data: {}, error: { code }, limitations: [] };
}
export function exitCode(result) {
  if (result.operation_ok) return 0;
  if (["READ_ONLY", "ACCESS_DENIED", "POLICY_DENIED"].includes(result.error?.code)) return 3;
  if (["INVALID_REQUEST", "INVALID_STARTUP", "INPUT_TOO_LARGE", "BAD_AS_OF"].includes(result.error?.code)) return 2;
  return 7;
}

export function acquisitionEnvironment(mode, environment = process.env) {
  const result = runtimeEnvironment(environment, false);
  if (mode === "acquire") for (const name of KEYS) if (typeof environment[name] === "string") result[name] = environment[name];
  return result;
}

export async function invoke(config, input, { signal, spawnImpl = spawn } = {}) {
  if (["sync", "monitor"].includes(config.operation) && config.mode !== "acquire") throw new CommandError("READ_ONLY");
  if (signal?.aborted) throw new CommandError("INTERRUPTED");
  const args = ["-B", "-m", "trading_system.intelligence_cli", config.operation,
    "--archive-root", config.archiveRoot, "--mode", config.mode, "--cycles", String(config.cycles),
    "--interval-seconds", String(config.interval), "--wall-seconds", String(config.wall),
    ...config.sources.flatMap((value) => ["--enable-source", value])];
  return await new Promise((resolveResult, rejectResult) => {
    let child;
    let failed;
    let timer;
    let forceTimer;
    let cleanupTimer;
    let done = false;
    let outputBytes = 0;
    let diagnosticBytes = 0;
    const chunks = [];
    function kill(kind) {
      if (!child?.pid) return;
      try { process.kill(-child.pid, kind); } catch (error) { if (error.code !== "ESRCH") failed ??= new CommandError("RUNNER_FAILED"); }
    }
    function finish(error, value) {
      if (done) return;
      done = true;
      clearTimeout(timer); clearTimeout(forceTimer); clearTimeout(cleanupTimer);
      signal?.removeEventListener("abort", abort);
      if (error) rejectResult(error); else resolveResult(value);
    }
    function stop(code) {
      if (failed || done) return;
      failed = new CommandError(code);
      kill("SIGTERM");
      forceTimer = setTimeout(() => kill("SIGKILL"), 250);
      cleanupTimer = setTimeout(() => {
        kill("SIGKILL"); child.stdin.destroy(); child.stdout.destroy(); child.stderr.destroy();
        finish(failed);
      }, 2000);
    }
    const abort = () => stop("INTERRUPTED");
    try {
      child = spawnImpl(config.python, args, { cwd: ROOT, shell: false, detached: true,
        env: acquisitionEnvironment(["sync", "monitor"].includes(config.operation) ? config.mode : "readonly"),
        stdio: ["pipe", "pipe", "pipe"] });
    } catch { finish(new CommandError("RUNNER_FAILED")); return; }
    child.on("error", () => finish(new CommandError("RUNNER_FAILED")));
    child.stdout.on("data", (chunk) => {
      outputBytes += chunk.length;
      if (outputBytes > RESULT_BYTES) stop("OUTPUT_TOO_LARGE");
      else if (!failed) chunks.push(chunk);
    });
    child.stderr.on("data", (chunk) => {
      diagnosticBytes += chunk.length;
      if (diagnosticBytes > 65536) stop("RUNNER_FAILED");
    });
    child.stdin.on("error", (error) => { if (error.code !== "EPIPE") stop("RUNNER_FAILED"); });
    child.on("close", (code, exitSignal) => {
      kill("SIGKILL");
      if (failed) { finish(failed); return; }
      try {
        const result = parseJson(Buffer.concat(chunks), RESULT_BYTES);
        if (result?.schema !== "tiger.intelligence-operation/v1" || result.operation !== config.operation
          || typeof result.operation_ok !== "boolean" || !result.data || typeof result.data !== "object" || Array.isArray(result.data)
          || !Array.isArray(result.limitations) || result.limitations.some(x => typeof x !== "string")
          || result.operation_ok !== (result.error === null) || !result.operation_ok && !/^[A-Z][A-Z0-9_]{1,79}$/u.test(result.error?.code ?? "")
          || exitSignal || code !== exitCode(result)) throw new CommandError("RESULT_INVALID");
        finish(null, result);
      } catch { finish(new CommandError("RESULT_INVALID")); }
    });
    timer = setTimeout(() => stop("WALL_DEADLINE"), config.wall * 1000 + 2000);
    signal?.addEventListener("abort", abort, { once: true });
    child.stdin.end(JSON.stringify(input) + "\n");
    if (signal?.aborted) abort();
  });
}

export async function main(argv = process.argv.slice(2)) {
  const controller = new AbortController();
  const abort = () => controller.abort();
  process.once("SIGINT", abort); process.once("SIGTERM", abort);
  let writable = true;
  const broken = () => { writable = false; controller.abort(); };
  process.stdout.on("error", broken);
  let result;
  let operation = OPS.has(argv[0]) ? argv[0] : null;
  try {
    const config = parseArgs(argv);
    operation = config.operation;
    if (["sync", "monitor"].includes(operation) && config.mode !== "acquire") throw new CommandError("READ_ONLY");
    const input = await readRequestInput(config.file, process.stdin, { signal: controller.signal });
    result = await invoke(config, input, { signal: controller.signal });
  } catch (error) {
    result = failure(operation, error instanceof CommandError ? error.code :
      ["INVALID_REQUEST", "INPUT_TOO_LARGE"].includes(error?.code) ? error.code : "RUNNER_FAILED");
  }
  try {
    if (writable) await new Promise((done, reject) => process.stdout.write(JSON.stringify(result) + "\n", error => error ? reject(error) : done()));
  } catch { writable = false; }
  finally { process.removeListener("SIGINT", abort); process.removeListener("SIGTERM", abort); process.stdout.removeListener("error", broken); }
  process.exitCode = writable ? exitCode(result) : 130;
  return result;
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) await main();
