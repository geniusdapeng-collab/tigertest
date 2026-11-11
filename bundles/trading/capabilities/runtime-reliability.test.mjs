import assert from "node:assert/strict";
import { test } from "node:test";
import { waitForReceipt } from "./workflow-client.mjs";
import { RUN_ID, result } from "../../../scripts/tiger-agent-test-support.mjs";

test("run wait retries a transient status timeout without resubmitting", async () => {
  const calls = [];
  let statuses = 0;
  const invoke = async (operation) => {
    calls.push(operation);
    if (operation === "status" && statuses++ === 0) {
      return {
        ...result("status"),
        operation_ok: false,
        status: "interrupted",
        error: {
          code: "TIMEOUT", message: "status observation exceeded 60 seconds",
          run_id: RUN_ID, stage: null, retryable: true, details_ref: null,
        },
      };
    }
    if (operation === "status") return result("status", { status: "completed" });
    if (operation === "receipt") return result("receipt");
    throw new Error(`unexpected operation ${operation}`);
  };

  const value = await waitForReceipt(RUN_ID, {}, { invoke, pollMs: 1, waitMs: 1000 });
  assert.equal(value.operation, "receipt");
  assert.equal(value.operation_ok, true);
  assert.deepEqual(calls, ["status", "status", "receipt"]);
});

test("run wait still stops immediately for a permanent request error", async () => {
  const calls = [];
  const failure = {
    ...result("status"),
    operation_ok: false,
    status: "invalid",
    error: {
      code: "INVALID_REQUEST", message: "fixed request differs",
      run_id: RUN_ID, stage: null, retryable: false, details_ref: null,
    },
  };
  const value = await waitForReceipt(RUN_ID, {}, {
    pollMs: 1,
    waitMs: 1000,
    invoke: async (operation) => {
      calls.push(operation);
      return failure;
    },
  });
  assert.equal(value.error.code, "INVALID_REQUEST");
  assert.deepEqual(calls, ["status"]);
});
