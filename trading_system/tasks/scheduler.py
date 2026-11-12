"""One owner coordinates a bounded pool; workers never submit or wait on it."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, wait as wait_futures, FIRST_COMPLETED
from threading import Event, Lock, get_ident
import time

from .contracts import UnitResult, UnitSpec, WorkUnitError, detached, timestamp_number, identifiers
from .context import UnitContext


_SAFE_HANDLER_ERROR_CODES = (
    "DOCUMENT_GLOBAL_REVIEW_MISMATCH",
    "DOCUMENT_REVIEW_FAILED",
    "WORK_ANNOTATION_INVALID",
    "WORK_DECISION_INPUT_CHANGED",
    "WORK_OUTPUT_SCHEMA_MISMATCH",
    "WORK_SEGMENT_ANNOTATION_INVALID",
    "WORK_SEGMENT_OFFSET_INVALID",
    "WORK_SEGMENTS_REQUIRED",
    "WORK_SPAN_BINDING_INVALID",
)
_STORE_BUSY_RETRY_SECONDS = 5.0
_CLAIM_CANDIDATE_BATCH = 128


def _handler_error_code(error):
    """Keep only a closed, value-free validation code from handler failures."""
    if isinstance(error, WorkUnitError):
        return error.code
    if isinstance(error, ValueError):
        message = str(error)
        for code in _SAFE_HANDLER_ERROR_CODES:
            if message == code or message.startswith(code + ":"):
                return code
    return "UNIT_HANDLER_FAILED"


class Coordinator:
    def __init__(self, store, *, max_workers=1, client_factory=None, lease_seconds=60,
                 resource_capacity=None, resource_cost=None, unit_ids=None):
        if type(max_workers) is not int or not 1 <= max_workers <= 64:
            raise WorkUnitError("CONTRACT_INVALID", "Worker count must be bounded between one and 64")
        self.store, self.max_workers, self.client_factory = store, max_workers, client_factory
        self.unit_ids = None if unit_ids is None else identifiers(unit_ids, "coordinator scope")
        if self.unit_ids is not None:
            if not self.unit_ids:
                raise WorkUnitError("CONTRACT_INVALID", "Coordinator scope cannot be empty")
            self._scope_states = store.scope_states(self.unit_ids)
        else:
            self._scope_states = None
        self.lease_seconds = timestamp_number(lease_seconds)
        if not self.lease_seconds:
            raise WorkUnitError("CONTRACT_INVALID", "Lease duration must be positive")
        if resource_capacity is None:
            if resource_cost is not None:
                raise WorkUnitError("CONTRACT_INVALID", "Resource cost requires explicit capacities")
            resource_capacity = {}
        if (type(resource_capacity) is not dict or any(type(key) is not str or not key for key in resource_capacity)
                or any(type(value) is not int or value <= 0 for value in resource_capacity.values())
                or resource_capacity and not callable(resource_cost)):
            raise WorkUnitError("CONTRACT_INVALID", "Resource capacities require positive integers and a cost callback")
        self.resource_capacity, self.resource_cost = detached(resource_capacity), resource_cost
        self._resource_used = {name: 0 for name in resource_capacity}
        self._resource_lock = Lock()
        self._owner, self._closed = get_ident(), False
        self._executor = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="tiger-unit")
        self._futures = {}
        self._clients, self._client_lock = [], Lock()
        self._stop_observing = Event()
        self._handlers, self._validators = {}, {}
        self._summary_cache, self._summary_at = None, 0.0

    def _check_owner(self):
        if get_ident() != self._owner:
            raise WorkUnitError("COORDINATOR_OWNER_MISMATCH", "Only the coordinator owner may schedule or wait")

    def _register_client(self, client):
        with self._client_lock:
            if any(existing is client for existing in self._clients):
                raise WorkUnitError("CLIENT_CONTEXT_SHARED", "Mutable client is shared across unit contexts")
            # Retain references until close, preventing object-id reuse.
            self._clients.append(client)

    def _release_resources(self, cost):
        with self._resource_lock:
            for name, amount in cost.items():
                self._resource_used[name] -= amount

    def _reserve_resources(self, cost):
        with self._resource_lock:
            if any(self._resource_used[name] + amount > self.resource_capacity[name] for name, amount in cost.items()):
                return False
            for name, amount in cost.items():
                self._resource_used[name] += amount
        return True

    def _mark_state(self, unit_id, state):
        if self._scope_states is not None and unit_id in self._scope_states:
            self._scope_states[unit_id] = state

    def _refresh_unit(self, unit_id):
        row = self.store.unit(unit_id)
        self._mark_state(unit_id, row["state"])
        return row

    def _retry_store_busy(self, operation):
        deadline = time.monotonic() + _STORE_BUSY_RETRY_SECONDS
        delay = 0.01
        while True:
            try:
                return operation()
            except WorkUnitError as error:
                if error.code != "STORE_BUSY" or time.monotonic() >= deadline:
                    raise
                time.sleep(min(delay, max(0, deadline - time.monotonic())))
                delay = min(delay * 2, 0.25)

    def _status(self, *, final=False):
        if final:
            value = self.store.status()
        else:
            now = time.monotonic()
            if self._summary_cache is None or now - self._summary_at >= 0.25:
                self._summary_cache = self.store.compact_status()
                self._summary_at = now
            value = dict(self._summary_cache)
        if self.unit_ids is None:
            value["scope_state"] = value["graph_state"]
        else:
            states = set(self._scope_states.values())
            value["scope_state"] = ("completed" if states == {"validated"} else
                "failed" if states & {"failed", "unknown"} else "cancelled" if "cancelled" in states else
                "running" if "running" in states else "ready")
            if final:
                value["units"] = [row for row in value["units"] if row["unit_id"] in self.unit_ids]
        with self._resource_lock:
            value["resources"] = {"capacity": dict(self.resource_capacity), "used": dict(self._resource_used)}
        return value

    def _execute(self, lease, handler, validator, cost):
        try:
            try:
                context = UnitContext(self.store, lease, client_factory=self.client_factory,
                                      register_client=self._register_client)
                value = handler(context)
                if isinstance(value, UnitResult):
                    context.publish_result(value.value, validator=validator, dispositions=value.dispositions)
                else:
                    context.publish_result(value, validator=validator)
            except BaseException as error:
                code = _handler_error_code(error)
                try:
                    self.store.fail(lease, code, retryable=code in {
                        "STORE_BUSY", "RESOURCE_COST_INVALID",
                    })
                except WorkUnitError as persistence:
                    if persistence.code not in {"LEASE_FENCED", "TASK_CANCELLED"}:
                        raise
                if not isinstance(error, Exception):
                    raise
        finally:
            self._release_resources(cost)

    def _next_lease(self):
        if not self.resource_capacity and self.unit_ids is None:
            owner = "coordinator-" + str(self._owner)
            lease = self._retry_store_busy(
                lambda: self.store.claim_ready(owner, lease_seconds=self.lease_seconds))
            if lease is not None:
                self._mark_state(lease.unit_id, "running")
            return lease, {}
        owner = "coordinator-" + str(self._owner)
        after_rowid = 0
        while True:
            rows = self._retry_store_busy(
                lambda: self.store.ready_candidates(
                    after_rowid=after_rowid, limit=_CLAIM_CANDIDATE_BATCH))
            if not rows:
                return None, {}
            for row in rows:
                after_rowid = row["_rowid"]
                if self.unit_ids is not None and row["unit_id"] not in self.unit_ids:
                    continue
                code = None
                try:
                    cost = (detached(self.resource_cost(UnitSpec.from_dict(row["spec"])))
                            if self.resource_capacity else {})
                    if (type(cost) is not dict or not set(cost) <= set(self.resource_capacity)
                            or any(type(amount) is not int or amount < 0 for amount in cost.values())):
                        raise ValueError
                    cost = {name: cost.get(name, 0) for name in self.resource_capacity}
                    if any(amount > self.resource_capacity[name] for name, amount in cost.items()):
                        code = "RESOURCE_EXCEEDS_CAPACITY"
                except Exception as error:
                    cost = {}
                    code = ("STORE_BUSY" if isinstance(error, WorkUnitError)
                            and error.code == "STORE_BUSY" else "RESOURCE_COST_INVALID")
                if code is None and not self._reserve_resources(cost):
                    continue
                lease = None
                try:
                    lease = self._retry_store_busy(lambda: self.store.claim(
                        row["unit_id"], owner, lease_seconds=self.lease_seconds))
                    if lease is None:
                        continue
                    self._mark_state(lease.unit_id, "running")
                    if code is not None:
                        self.store.fail(lease, code, retryable=code == "STORE_BUSY")
                        self._mark_state(lease.unit_id, "failed")
                        continue
                    return lease, cost
                finally:
                    if code is None and lease is None:
                        self._release_resources(cost)
        return None, {}

    def tick(self, handlers, *, validators):
        self._check_owner()
        if self._closed:
            raise WorkUnitError("COORDINATOR_CLOSED", "Coordinator is closed")
        if type(handlers) is not dict or type(validators) is not dict:
            raise WorkUnitError("CONTRACT_INVALID", "Handlers and validators require explicit kind maps")
        self._handlers, self._validators = handlers, validators
        for future in list(self._futures):
            if future.done():
                future.result()
                self._refresh_unit(self._futures[future].unit_id)
                del self._futures[future]
        for future, lease in list(self._futures.items()):
            if lease.until - self.store._now() < self.lease_seconds / 2:
                try:
                    self._futures[future] = self.store.heartbeat(lease, lease_seconds=self.lease_seconds)
                except WorkUnitError as error:
                    if error.code not in {"LEASE_FENCED", "TASK_CANCELLED", "TASK_DEADLINE"}:
                        raise
        recovered = self.store.recover_expired()
        for unit_id in (*recovered["ready"], *recovered["unknown"]):
            self._refresh_unit(unit_id)
        if self._stop_observing.is_set():
            return self._status()
        while len(self._futures) < self.max_workers:
            lease, cost = self._next_lease()
            if lease is None:
                break
            kind = self.store.unit(lease.unit_id)["spec"]["kind"]
            handler, validator = handlers.get(kind), validators.get(kind)
            if not callable(handler) or not callable(validator):
                try:
                    self.store.fail(lease, "UNIT_HANDLER_MISSING", retryable=False)
                    self._mark_state(lease.unit_id, "failed")
                finally:
                    self._release_resources(cost)
                continue
            try:
                future = self._executor.submit(self._execute, lease, handler, validator, cost)
            except BaseException:
                self._release_resources(cost)
                self.store.fail(lease, "UNIT_SUBMISSION_FAILED", retryable=True)
                raise
            self._futures[future] = lease
        return self._status()

    def wait(self, *, timeout=None):
        self._check_owner()
        if timeout is not None and (type(timeout) not in {int, float} or timeout < 0):
            raise WorkUnitError("CONTRACT_INVALID", "Invalid coordinator wait timeout")
        if self._futures:
            wait_futures(tuple(self._futures), timeout=timeout, return_when=FIRST_COMPLETED)
        return self._status()

    def run(self, handlers, *, validators, timeout=None):
        self._check_owner()
        if timeout is not None:
            timestamp_number(timeout)
        deadline = None if timeout is None else time.monotonic() + timeout
        while True:
            status = self.tick(handlers, validators=validators)
            if status["scope_state"] in {"completed", "cancelled"} or status["cancel_requested"] or not self._futures:
                return self._status(final=True)
            if self._stop_observing.is_set() or deadline is not None and time.monotonic() >= deadline:
                return status
            self.wait(timeout=min(0.05, max(0, deadline - time.monotonic())) if deadline is not None else 0.05)

    def stop_observing(self):
        self._check_owner()
        self._stop_observing.set()

    def close(self, *, wait=True, timeout=None):
        self._check_owner()
        if not self._closed:
            self._closed = True
            self._executor.shutdown(wait=wait, cancel_futures=False)
            if wait:
                if timeout is None:
                    for future in self._futures:
                        future.result()
                    self._futures.clear()
                else:
                    wait_futures(tuple(self._futures), timeout=timeout,
                                 return_when=FIRST_COMPLETED)
                    return {"closed": True,
                            "pending_units": [
                                {"future_pending": not future.done(),
                                 "unit_id": lease.unit_id, "epoch": lease.epoch}
                                for future, lease in self._futures.items()
                                if not future.done()
                            ]}
        return {"closed": True, "pending_units": []}
