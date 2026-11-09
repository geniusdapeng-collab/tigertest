#!/usr/bin/env node
/** Tiger CLI: one request, one envelope; business execution belongs to the strict Python runner. */
import { pathToFileURL } from "node:url";
import {
  LIMITS, OPERATIONS, PROFILE, WorkflowError, exitCode, failureEnvelope, invokeWorkflow,
  readRequestInput, startupOptions, waitForReceipt,
} from "../bundles/trading/capabilities/workflow-client.mjs";

export function parseArgs(argv) {
  const operation = argv[0];
  if (!OPERATIONS.includes(operation)) throw new WorkflowError("INVALID_REQUEST", "Use doctor, describe, run, resume, supplement, cancel, status, receipt or verify.");
  const flags = new Map();
  const allowed = new Set(["--python", "--state-root", "--mode", "--input-file", "--wait", "--wait-timeout-seconds"]);
  for (let index = 1; index < argv.length; index += 1) {
    const flag = argv[index];
    if (!allowed.has(flag) || flags.has(flag)) throw new WorkflowError("INVALID_REQUEST", "Unknown or duplicate command option.");
    if (flag === "--wait") flags.set(flag, true);
    else {
      const value = argv[++index];
      if (!value || value.startsWith("--")) throw new WorkflowError("INVALID_REQUEST", "Missing command option value.");
      flags.set(flag, value);
    }
  }
  const wait = flags.has("--wait");
  if ((wait || flags.has("--wait-timeout-seconds"))
    && !["run", "resume", "supplement"].includes(operation)) throw new WorkflowError("INVALID_REQUEST", "Only run, resume and supplement support --wait.");
  if (flags.has("--wait-timeout-seconds") && !wait) throw new WorkflowError("INVALID_REQUEST", "A wait deadline requires --wait.");
  let waitMs = LIMITS.waitMs;
  if (flags.has("--wait-timeout-seconds")) {
    const seconds = flags.get("--wait-timeout-seconds");
    if (!/^[1-9][0-9]{0,3}$/u.test(seconds) || Number(seconds) > 3600) throw new WorkflowError("INVALID_REQUEST", "Wait deadline must be 1 to 3600 seconds.");
    waitMs = Number(seconds) * 1000;
  }
  const options = startupOptions({ ...(flags.has("--python") ? { python: flags.get("--python") } : {}),
    ...(flags.has("--state-root") ? { stateRoot: flags.get("--state-root") } : {}),
    ...(flags.has("--mode") ? { mode: flags.get("--mode") } : {}) });
  return { operation, options, file: flags.get("--input-file"), wait, waitMs };
}

export async function main(argv = process.argv.slice(2)) {
  const controller = new AbortController();
  let writable = true;
  const abort = () => controller.abort();
  const brokenOutput = () => { writable = false; controller.abort(); };
  process.once("SIGINT", abort);
  process.once("SIGTERM", abort);
  process.stdout.on("error", brokenOutput);
  let envelope;
  let operation = OPERATIONS.includes(argv[0]) ? argv[0] : null;
  let knownRun = null;
  try {
    const parsed = parseArgs(argv);
    operation = parsed.operation;
    // Readonly rejects run before even opening an operator-supplied request file.
    if (["run", "resume", "supplement", "cancel"].includes(operation)
      && parsed.options.mode === "readonly") throw new WorkflowError("READ_ONLY", "Paper mutations are unavailable in readonly deployment.");
    const input = parsed.file ? await readRequestInput(parsed.file, process.stdin, { signal: controller.signal })
      : operation === "doctor" ? {} : operation === "describe" ? { profile: PROFILE } : {};
    envelope = await invokeWorkflow(operation, input, parsed.options, { signal: controller.signal });
    knownRun = envelope.run_id;
    if (parsed.wait && envelope.operation_ok) envelope = await waitForReceipt(knownRun, parsed.options,
      { operation: parsed.operation, signal: controller.signal, waitMs: parsed.waitMs });
  } catch (error) { envelope = failureEnvelope(operation, error, knownRun); }
  try {
    if (writable) await new Promise((done, reject) => process.stdout.write(JSON.stringify(envelope) + "\n", (error) => error ? reject(error) : done()));
  } catch { writable = false; controller.abort(); }
  finally {
    process.removeListener("SIGINT", abort);
    process.removeListener("SIGTERM", abort);
    if (writable) process.stdout.removeListener("error", brokenOutput);
  }
  process.exitCode = writable ? exitCode(envelope) : 130;
  return envelope;
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) await main();
