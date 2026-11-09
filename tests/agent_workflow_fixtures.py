"""Offline source fixtures for exercising the real worker process boundary.

These fixtures never represent market analysis. They are installed only into a
pytest temporary source tree, and their artifacts explicitly say TEST_FIXTURE.
The public JSON interface cannot select this source or a replacement engine.
"""
from pathlib import Path
import shutil
import textwrap
import time

from trading_system.agent_runner import observe_run
from trading_system.agent_runtime import SOURCE_ROOT, executable_files

REQUEST = {
    "schema_version": "tiger.agent-request/v1", "profile": "us-public-paper-v1",
    "paper_account": "public-usd", "idempotency_key": "fixture-key-0001",
}


def fixture_source(destination: Path, mode="success") -> Path:
    destination.mkdir()
    for name in executable_files(SOURCE_ROOT):
        target = destination / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(SOURCE_ROOT / name, target)
    source = '''
        """TEST_FIXTURE: no live market requests or investment conclusions."""
        import json
        import os
        from pathlib import Path
        import signal
        import subprocess
        import sys
        import time
        from types import SimpleNamespace
        from trading_system.agent_evidence import atomic_json
        from trading_system.calendar import prev_trading_day
        from trading_system.redline.execution import daily_stage_contract
        MODE = __MODE__

        def _daily(args, provider, *, execution):
            if MODE == "fds":
                locks = [execution.store.root / "run.lock",
                         execution.store.root.parent.parent / "account.lock"]
                inodes = {(p.stat().st_dev, p.stat().st_ino) for p in locks}
                inherited = []
                for fd in range(256):
                    try:
                        info = os.fstat(fd)
                    except OSError:
                        continue
                    if (info.st_dev, info.st_ino) in inodes:
                        inherited.append(fd)
                atomic_json(execution.paths.work_dir, "descriptor-observation.json",
                            {"fixture": "TEST_FIXTURE", "inherited_lock_fds": inherited})
            if MODE == "descendant":
                child = subprocess.Popen(
                    [sys.executable, "-c", "import time; time.sleep(60)"],
                    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL, close_fds=True)
                atomic_json(execution.paths.work_dir, "descendant-observation.json",
                            {"fixture": "TEST_FIXTURE", "pid": child.pid, "pgid": os.getpgrp()})
            if MODE == "stop":
                os.kill(os.getpid(), signal.SIGSTOP)
            if MODE == "hang":
                time.sleep(60)
            if MODE == "flood":
                for _ in range(1800):
                    os.write(1, b"x" * 8192 + b"\\n")
            if MODE == "fail":
                raise RuntimeError("TEST_FIXTURE failure before business completion")
            reports = execution.paths.reports_dir
            day = execution.run_date.isoformat()
            compact = day.replace("-", "")
            sim = json.loads((reports / "sim_portfolio.json").read_text())
            sim["equity_curve"].append({"date": day, "equity": 100000.0})
            sim["ops_log"].append({"date": day, "equity": 100000.0,
                                   "fixture": "TEST_FIXTURE"})
            atomic_json(reports, "sim_portfolio.json", sim)
            result = {
                "trade_date": day, "provider": "yahoo", "picks": [], "action": "AVOID",
                "watchlist": [], "market_view": "TEST_FIXTURE",
                "raw": {
                    "fixture": "TEST_FIXTURE", "synthetic": False,
                    "account_currency": "USD",
                    "currency_policy": {"quote_currency": "USD"},
                    "accounting": {"status": "updated", "execution_blocked": False},
                    "freshness": {"benchmark_last_bar": prev_trading_day(execution.run_date).isoformat(),
                                  "benchmark_lag_days": 0},
                    "source_lineage": [["TEST_FIXTURE", "yahoo"]],
                    "data_coverage": "1/1", "data_issues": [],
                    "scan_stats": {"universe_size": 1, "data_ok": 1},
                    "redline": [{"step": "review.daily", "status": "executed"}],
                },
            }
            atomic_json(reports, f"result_{compact}.json", result)
            (reports / f"日报_{compact}.md").write_text(
                f"# TEST_FIXTURE {day}\\n| review.daily | ✅ | 0 | test |\\n")
            (reports / f"复盘_{compact}.md").write_text(
                f"# 复盘纪要 — {day}\\nTEST_FIXTURE\\n## 一、日度归因\\n"
                "## 二、违规六条检测\\n")
            (reports / f"日报_{compact}.html").write_text(
                f"<!doctype html><html><head><title>TEST_FIXTURE {day}</title></head>"
                "<body><table><tr><td>review.daily</td><td>已执行</td></tr>"
                "</table></body></html>")
            for spec in daily_stage_contract():
                with execution.step(spec["name"], {"fixture": "TEST_FIXTURE"}) as scope:
                    if spec["driver"] == "llm":
                        scope.passthrough("TEST_FIXTURE no model")
                    scope.output({"fixture": "TEST_FIXTURE", "stage": spec["name"]})
            return SimpleNamespace(fixture="TEST_FIXTURE")
    '''
    (destination / "main.py").write_text(
        textwrap.dedent(source).replace("__MODE__", repr(mode)), encoding="utf-8")
    return destination


def wait_terminal(state: Path, run_id: str, seconds=20):
    deadline = time.monotonic() + seconds
    latest = None
    while time.monotonic() < deadline:
        latest = observe_run(state, run_id)
        if (latest["status"] in {"completed", "completed_degraded", "failed", "blocked", "interrupted", "cancelled"}
                and not latest["active"]):
            return latest
        time.sleep(0.05)
    raise AssertionError(f"TEST_FIXTURE worker did not settle: {latest}")


def wait_engine(state: Path, run_id: str, seconds=10):
    path = state / "runs" / run_id / "processes.json"
    deadline = time.monotonic() + seconds
    import json
    while time.monotonic() < deadline:
        if path.exists():
            value = json.loads(path.read_bytes())
            if "engine_pid" in value:
                return value
        time.sleep(0.02)
    raise AssertionError("TEST_FIXTURE engine did not start")
