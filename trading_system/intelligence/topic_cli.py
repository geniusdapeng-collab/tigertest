"""Typed topic CLI and detached, resumable worker entry point."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import fcntl
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import sys

from .contracts import IntelligenceError, aware, canonical, identifier, safe_value
from .topic import JOB_ID, TopicResearch, TopicStore, load_registry, trusted_policy
from ..agent_evidence import atomic_json, read_json

OPERATIONS = ("create", "research", "watch", "status", "report", "evidence", "health", "tick")
RESULT_SCHEMA = "tiger.topic-operation/v1"


def validate_request(operation, value):
    if operation not in OPERATIONS or not isinstance(value, dict):
        raise IntelligenceError("INVALID_REQUEST")
    fields = {
        "create": ({"question", "entity_ids", "horizon", "material_policy", "idempotency_key"}, {"question", "idempotency_key"}),
        "research": ({"topic_id", "version", "idempotency_key"}, {"topic_id", "version", "idempotency_key"}),
        "watch": ({"topic_id", "version", "source_ids", "cadence_preset", "idempotency_key"}, {"topic_id", "version", "source_ids", "cadence_preset", "idempotency_key"}),
        "status": ({"job_id", "watch_id"}, set()), "report": ({"job_id"}, {"job_id"}),
        "evidence": ({"snapshot_id", "document_id"}, {"snapshot_id"}),
        "health": (set(), set()), "tick": (set(), set()),
    }
    allowed, required = fields[operation]
    if set(value) - allowed or required - value.keys() or safe_value(value) != value:
        raise IntelligenceError("INVALID_REQUEST")
    if operation == "status" and len(value) != 1:
        raise IntelligenceError("INVALID_REQUEST")
    for key in ("idempotency_key", "topic_id", "watch_id", "snapshot_id", "document_id"):
        if key in value:
            identifier(value[key])
    if "job_id" in value and (not isinstance(value["job_id"], str) or not JOB_ID.fullmatch(value["job_id"])):
        raise IntelligenceError("INVALID_REQUEST")
    if "version" in value and (type(value["version"]) is not int or value["version"] < 1):
        raise IntelligenceError("INVALID_REQUEST")
    return value


def envelope(operation, data=None, error=None):
    return {"schema": RESULT_SCHEMA, "operation": operation, "operation_ok": error is None,
            "data": {} if data is None else data, "error": None if error is None else {"code": error},
            "limitations": ["Topic research does not execute a paper account or broker order.",
                "accepted/running is not completed; report independently verifies evidence and model receipts.",
                "partial reports retain missing dimensions and cannot count as strict paper completion."]}


def _lock_file(root, name):
    import stat
    path = root / name
    try:
        descriptor = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_uid != os.geteuid():
            os.close(descriptor)
            raise IntelligenceError("ACCESS_DENIED", "Unsafe topic queue lock")
        return descriptor
    except OSError:
        raise IntelligenceError("ARCHIVE_UNAVAILABLE", "Cannot open topic queue lock") from None


def _deployment_tenant(root, tenant):
    """One trusted tenant per research deployment; different tenants use roots."""
    path = root / "research-deployment.json"
    value = {"schema": "tiger.topic-deployment/v1", "tenant": tenant}
    if path.exists() or path.is_symlink():
        if read_json(root, path.name) != value:
            raise IntelligenceError("TENANT_ROOT_CONFLICT", "Use a separate trusted research root for another tenant")
    else:
        atomic_json(root, path.name, value, exclusive=True)


def admit_research_deployment(service):
    descriptor = _lock_file(service.root, "queue-launch.lock")
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        _deployment_tenant(service.root, service.tenant)
    finally:
        os.close(descriptor)


def spawn_worker(service, job):
    launch = _lock_file(service.root, "queue-launch.lock")
    slot = None
    try:
        fcntl.flock(launch, fcntl.LOCK_EX)
        _deployment_tenant(service.root, service.tenant)
        job = service.store.job(job["job_id"])
        if job["state"] in {"completed", "partial"}:
            return
        if job["state"] == "running":
            active = _lock_file(service.store.directory(job["job_id"]), "worker.lock")
            try:
                try:
                    fcntl.flock(active, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    return
            finally:
                os.close(active)
        # A resumed original identity joins the same durable queue. Earlier
        # failures and dispatched/unknown attempts remain in its event ledger.
        if job["state"] != "accepted":
            service.store.update(job["job_id"], state="accepted", phase="queued_resume")
        slot = _lock_file(service.root, "research-worker.lock")
        try:
            fcntl.flock(slot, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return
        with service.store.connection() as db:
            first = db.execute("SELECT job_id FROM research_jobs WHERE tenant=? "
                "AND json_extract(record,'$.state')='accepted' "
                "ORDER BY json_extract(record,'$.created_at'),job_id LIMIT 1", (service.tenant,)).fetchone()
        first_id = first["job_id"] if first else job["job_id"]
        subprocess.Popen([sys.executable, "-B", "-m", "trading_system.intelligence.topic_cli", "_worker",
            "--state-root", str(service.root), "--archive-root", str(service.archive_root),
            "--mode", "research", "--tenant", service.tenant, "--job-id", first_id, "--slot-fd", str(slot)],
            cwd=Path(__file__).resolve().parents[2], env=dict(os.environ), stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, close_fds=True,
            pass_fds=(slot,), start_new_session=True)
    except OSError:
        raise IntelligenceError("WORKER_START_FAILED", "Research remains queued under its original identity") from None
    finally:
        if slot is not None:
            os.close(slot)
        os.close(launch)


def execute(operation, request, *, state_root, archive_root, mode="readonly", tenant="public", policy=None,
            service_factory=TopicResearch, start_worker=spawn_worker):
    admitted_at = datetime.now(timezone.utc)
    request = validate_request(operation, request)
    if operation in {"create", "watch", "tick"} and mode not in {"acquire", "research"} or operation == "research" and mode != "research":
        raise IntelligenceError("MODE_DENIED")
    # Inspection cannot accidentally create stores by selecting a write mode.
    effective_mode = mode if operation in {"create", "watch", "tick", "research"} else "readonly"
    service = service_factory(state_root, archive_root, mode=effective_mode, tenant=tenant, policy=policy,
                              admitted_at=admitted_at)
    if operation == "create":
        data = service.create(request)
    elif operation == "research":
        admit_research_deployment(service)
        data = service.submit(request)
        start_worker(service, data)
        data = service.store.job(data["job_id"])
    elif operation == "watch":
        data = service.monitor.create_watch(**request)
    elif operation == "status":
        data = service.store.job(request["job_id"]) if "job_id" in request else service.monitor.status(request["watch_id"])
        if "job_id" in request:
            if data["state"] == "running":
                lock_path = service.store.directory(request["job_id"]) / "worker.lock"
                if not lock_path.is_file():
                    data["state"] = "interrupted"
                    data["worker_observation"] = "worker_lock_missing; resume_original_request"
                else:
                  with lock_path.open("r") as lock:
                    try:
                        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    except BlockingIOError:
                        data["worker_observation"] = "active"
                    else:
                        data["state"] = "interrupted"
                        data["worker_observation"] = "worker_lock_not_held; resume_original_request"
            data = {k: v for k, v in data.items() if k not in {"policy", "topic"}}
            data["model_attempts"] = service.store.attempts(request["job_id"])
    elif operation == "report":
        data = service.report_view(request["job_id"])
    elif operation == "evidence":
        from .archive import Archive
        archive = Archive(archive_root, tenant=tenant, readonly=True)
        snapshot = archive.snapshot(request["snapshot_id"])
        data = {"snapshot": snapshot}
        if "document_id" in request:
            matches = [d for d in archive.snapshot_documents(request["snapshot_id"]) if d["id"] == request["document_id"]]
            if len(matches) != 1:
                raise IntelligenceError("EVIDENCE_NOT_FOUND")
            data["document"] = matches[0]
    elif operation == "health":
        observed = service.monitor.status() if service.monitor.database.is_file() else {"state": "not_initialized", "sources": [service.catalog.connection(key) for key in service.registry.source_ids(enabled_only=False)]}
        data = {"catalog": service.catalog.manifest(), "observations": observed}
    else:
        from .monitoring import MonitoringWorker
        data = MonitoringWorker(service.monitor, lambda _source=None: service._service()).tick()
    return envelope(operation, data=data)


def _timeout(_number, _frame):
    raise IntelligenceError("WALL_DEADLINE")


def _interrupt(_number, _frame):
    raise KeyboardInterrupt


def worker(args):
    # Start the hard watchdog before loading any archive, catalog or model.
    signal.signal(signal.SIGALRM, _timeout)
    signal.signal(signal.SIGTERM, _interrupt)
    signal.setitimer(signal.ITIMER_REAL, 60)
    root = Path(args.state_root)
    slot = getattr(args, "slot_fd", None)
    try:
        if type(slot) is not int or slot < 3:
            raise IntelligenceError("INVALID_STARTUP", "A detached research worker requires its admitted slot")
        expected = (root / "research-worker.lock").stat(follow_symlinks=False)
        actual = os.fstat(slot)
        if ((expected.st_dev, expected.st_ino) != (actual.st_dev, actual.st_ino)
                or actual.st_nlink != 1 or actual.st_uid != os.geteuid()):
            raise IntelligenceError("INVALID_STARTUP", "Research slot identity differs")
        fcntl.flock(slot, fcntl.LOCK_EX | fcntl.LOCK_NB)
        store = TopicStore(args.state_root, tenant=args.tenant, readonly=True)
        job_id = args.job_id
        while job_id is not None:
            job = store.job(job_id)
            remaining = (aware(job["deadline"]) - datetime.now(timezone.utc)).total_seconds()
            if remaining <= 0:
                TopicStore(args.state_root, tenant=args.tenant).update(job_id, state="failed", phase="stopped", error={"code": "WALL_DEADLINE"})
            else:
                signal.setitimer(signal.ITIMER_REAL, remaining)
                service = TopicResearch(args.state_root, args.archive_root, mode="research", tenant=args.tenant,
                                        policy=job["policy"])
                service.run(job_id)
            signal.setitimer(signal.ITIMER_REAL, 0)
            launch = _lock_file(root, "queue-launch.lock")
            try:
                fcntl.flock(launch, fcntl.LOCK_EX)
                _deployment_tenant(root, args.tenant)
                with store.connection() as db:
                    next_job = db.execute("SELECT job_id FROM research_jobs WHERE tenant=? "
                        "AND json_extract(record,'$.state')='accepted' "
                        "ORDER BY json_extract(record,'$.created_at'),job_id LIMIT 1", (args.tenant,)).fetchone()
                job_id = next_job["job_id"] if next_job else None
                if job_id is None:
                    # Empty-check and slot release share the launch mutex with
                    # submitters; accepted work cannot miss its wakeup.
                    os.close(slot)
                    slot = None
            finally:
                os.close(launch)
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        if slot is not None:
            os.close(slot)
    return 0


def exit_code(result):
    if result["operation_ok"]:
        return 0
    code = result["error"]["code"]
    return 3 if code in {"MODE_DENIED", "ACCESS_DENIED", "POLICY_DENIED"} else 5 if code in {"IDEMPOTENCY_CONFLICT", "QUEUE_FULL"} else 2 if code in {"INVALID_REQUEST", "INVALID_STARTUP", "INPUT_TOO_LARGE", "CONTRACT_INVALID"} else 9 if code == "WALL_DEADLINE" else 7


def main(argv=None):
    parser = argparse.ArgumentParser(description="Bounded read-only topic research; no account actions")
    parser.add_argument("operation", choices=(*OPERATIONS, "_worker"))
    parser.add_argument("--state-root", required=True)
    parser.add_argument("--archive-root", required=True)
    parser.add_argument("--mode", choices=("readonly", "acquire", "research"), default="readonly")
    parser.add_argument("--tenant", default="public")
    parser.add_argument("--policy-file")
    parser.add_argument("--job-id")
    parser.add_argument("--slot-fd", type=int)
    args = parser.parse_args(argv)
    if not Path(args.state_root).is_absolute() or not Path(args.archive_root).is_absolute():
        parser.error("State and archive roots must be absolute")
    if args.operation == "_worker":
        if args.mode != "research" or args.job_id is None or args.slot_fd is None:
            return 3
        return worker(args)
    try:
        from ..intelligence_cli import read_request
        if args.operation in {"create", "watch", "tick"} and args.mode == "readonly" or args.operation == "research" and args.mode != "research":
            raise IntelligenceError("MODE_DENIED")
        result = execute(args.operation, read_request(sys.stdin.buffer), state_root=args.state_root,
                         archive_root=args.archive_root, mode=args.mode, tenant=args.tenant, policy=trusted_policy(args.policy_file))
    except IntelligenceError as error:
        result = envelope(args.operation, error=error.code)
    except KeyboardInterrupt:
        result = envelope(args.operation, error="INTERRUPTED")
    except Exception:
        result = envelope(args.operation, error="INTERNAL_ERROR")
    secrets = tuple(v for k, v in os.environ.items() if re.search(r"KEY|TOKEN|SECRET|PASSWORD|CREDENTIAL", k, re.I) and len(v) >= 4)
    payload = canonical(safe_value(result, secrets))
    if len(payload) > 8 * 1024 * 1024:
        result = envelope(args.operation, error="OUTPUT_TOO_LARGE")
        payload = canonical(result)
    try:
        sys.stdout.buffer.write(payload + b"\n")
    except BrokenPipeError:
        return 130
    return exit_code(result)


if __name__ == "__main__":
    raise SystemExit(main())
