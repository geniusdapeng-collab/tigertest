/** Deterministic subprocess transport fixtures. They are never business acceptance evidence. */
import { spawn } from "node:child_process";
import { chmod, mkdtemp, readFile, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";
import { setTimeout as delay } from "node:timers/promises";

export const ROOT = resolve(import.meta.dirname, "..");
export const RUN_ID = "run-0123456789abcdef0123456789abcdef";
export const REQUEST = Object.freeze({ schema_version: "tiger.agent-request/v1", profile: "us-public-paper-v1",
  paper_account: "public-usd", idempotency_key: "test-only-key-0001" });

export function result(operation, changes = {}) {
  const business = ["run", "resume", "supplement", "cancel", "status", "receipt", "verify"].includes(operation);
  return { schema_version: "tiger.agent-result/v1", operation, operation_ok: true,
    run_id: business ? RUN_ID : null,
    status: ["run", "supplement"].includes(operation) ? "accepted"
      : operation === "resume" ? "running" : operation === "cancel" ? "cancelled" : business ? "completed" : "ready",
    execution_mode: business ? "paper" : null, data: {},
    summary: operation === "receipt" ? { picks: [], positions: [], pending: [], cash_usd: 100000, equity_usd: 100000,
      market_value_usd: 0, as_of: { market_date: "2026-10-20", market_timezone: "America/New_York",
        admitted_at_utc: "2026-10-20T12:00:00+00:00", benchmark_last_bar: "2026-10-19", freshness: {} },
      disclosures: { fixture: true, synthetic: false, execution_mode: "paper", model_mode: "disabled" } } : null,
    verification: ["receipt", "verify"].includes(operation) ? { ok: true, receipt_sha256: "a".repeat(64), evidence_count: 1, checks: [] } : null,
    limitations: ["Transport fixture only; not business evidence."], evidence_refs: [], error: null, ...changes };
}

export async function runnerFixture(t, body = "send(result(operation));") {
  const dir = await mkdtemp(join(tmpdir(), "tiger-node-"));
  t.after(() => rm(dir, { recursive: true, force: true }));
  const callsFile = join(dir, "calls.jsonl");
  const python = join(dir, "trusted-python-fixture");
  await writeFile(python, `#!${process.execPath}\nimport fs from 'node:fs';
import {spawn} from 'node:child_process';
const args=process.argv.slice(2); const operation=args.at(-1); const dir=${JSON.stringify(dir)};
const RUN_ID=${JSON.stringify(RUN_ID)}; const result=${result.toString()};
let text='';for await(const chunk of process.stdin)text+=chunk;const input=JSON.parse(text);
fs.appendFileSync(${JSON.stringify(callsFile)}, JSON.stringify({args,input,operation,cwd:process.cwd(),envNames:Object.keys(process.env),pid:process.pid})+'\\n');
function send(value,code=0){process.stdout.write(JSON.stringify(value)+'\\n',()=>{process.exitCode=code;});}
${body}\n`);
  await chmod(python, 0o700);
  return { dir, python, callsFile, stateRoot: join(dir, "state"),
    options: { python, stateRoot: join(dir, "state"), mode: "paper" },
    calls: async () => { try { return (await readFile(callsFile, "utf8")).trim().split("\n").filter(Boolean).map(JSON.parse); }
      catch (error) { if (error.code === "ENOENT") return []; throw error; } } };
}

export function collectProcess(script, args = [], options = {}) {
  const child = spawn(process.execPath, [resolve(ROOT, script), ...args], { cwd: ROOT, stdio: ["pipe", "pipe", "pipe"], ...options });
  let stdout = ""; let stderr = "";
  child.stdout.on("data", (chunk) => { stdout += chunk; });
  child.stderr.on("data", (chunk) => { stderr += chunk; });
  const closed = new Promise((done, reject) => {
    child.once("error", reject);
    child.once("close", (code, signal) => done({ code, signal, stdout, stderr }));
  });
  return { child, closed };
}

export async function eventually(fn, { timeout = 5000, label = "condition" } = {}) {
  const deadline = Date.now() + timeout;
  do {
    const value = await fn();
    if (value) return value;
    await delay(10);
  } while (Date.now() < deadline);
  throw new Error(`Timed out waiting for ${label}`);
}

export function mcpProcess(args = [], options = {}) {
  const proc = collectProcess("scripts/tiger-agent-mcp.mjs", args, options);
  let buffer = "";
  const messages = [];
  proc.child.stdout.setEncoding("utf8");
  proc.child.stdout.on("data", (chunk) => {
    buffer += chunk;
    let end;
    while ((end = buffer.indexOf("\n")) >= 0) {
      const line = buffer.slice(0, end); buffer = buffer.slice(end + 1);
      if (line.trim()) messages.push(JSON.parse(line));
    }
  });
  const send = (message) => proc.child.stdin.write(JSON.stringify(message) + "\n");
  return { ...proc, messages, send,
    response: (id, count = 1) => eventually(() => messages.filter((message) => message.id === id).length >= count
      && messages.filter((message) => message.id === id)[count - 1], { label: `MCP response ${id}` }),
    async initialize() {
      send({ jsonrpc: "2.0", id: "init", method: "initialize", params: {
        protocolVersion: "2025-11-25", capabilities: {}, clientInfo: { name: "tiger-test", version: "1" } } });
      const response = await this.response("init");
      send({ jsonrpc: "2.0", method: "notifications/initialized" });
      return response;
    },
    async stop() { proc.child.stdin.end(); return proc.closed; },
  };
}
