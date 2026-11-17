"""MC-002/MC-009 runtime profile and reproducible installation contracts."""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
PREFLIGHT = ROOT / "scripts/runtime_preflight.py"


def executable(path: Path, text: str) -> Path:
    path.write_text(text, encoding="utf-8")
    path.chmod(0o755)
    return path


def invoke(*arguments: str):
    return subprocess.run(
        [sys.executable, str(PREFLIGHT), *arguments],
        cwd=ROOT, capture_output=True, text=True, timeout=30,
    )


def test_backend_profile_accepts_python_311_without_node(tmp_path):
    result = invoke("--profile", "backend", "--python-bin", sys.executable)
    assert result.returncode == 0, result.stdout + result.stderr
    value = json.loads(result.stdout)
    assert value["schema"] == "tiger.install-baseline/v1"
    assert value["ready"] is True and value["profile"] == "backend"
    assert value["python"]["version"][0] == sys.version_info.major
    assert value["node"] is None


@pytest.mark.parametrize("version", [(3, 10, 16), (3, 9, 6)])
def test_python_310_and_older_fail_before_setup_or_doctor(tmp_path, version):
    fake = executable(
        tmp_path / "python-old",
        "#!/bin/sh\nprintf '[%d,%d,%d]\\n'\n" % version,
    )
    result = invoke("--profile", "backend", "--python-bin", str(fake))
    assert result.returncode == 2
    assert json.loads(result.stdout)["error"]["code"] == "RUNTIME_NOT_READY"


def test_full_profile_requires_node_and_has_no_machine_specific_fallback():
    missing = invoke("--profile", "full", "--python-bin", sys.executable)
    assert missing.returncode == 2
    assert "Node 24" in json.loads(missing.stdout)["error"]["message"]
    doctor = (ROOT / "scripts/doctor.sh").read_text(encoding="utf-8")
    setup = (ROOT / "scripts/setup.sh").read_text(encoding="utf-8")
    assert "/Users/mac" not in doctor and "/Users/mac" not in setup
    assert ".tiger-runtime/node/bin/node" in doctor
    assert "--require-hashes" in setup
    assert "--frozen-lockfile" in setup
    assert "TIGER_PNPM_BIN" in setup
    assert "doctor.sh || true" not in setup


def test_full_profile_accepts_python_312_and_node_24(tmp_path):
    fake_node = executable(tmp_path / "node", "#!/bin/sh\nprintf 'v24.22.1\\n'\n")
    fake_pnpm = executable(tmp_path / "pnpm", "#!/bin/sh\nprintf '10.14.0\\n'\n")
    result = invoke(
        "--profile", "full", "--python-bin", sys.executable,
        "--node-bin", str(fake_node), "--pnpm-bin", str(fake_pnpm),
        "--out", str(tmp_path / "baseline.json"),
    )
    assert result.returncode == 0, result.stdout + result.stderr
    value = json.loads(result.stdout)
    assert value["ready"] is True and value["node"]["version"] == [24, 22, 1]
    assert value["pnpm"]["version"] == [10, 14, 0]
    assert json.loads((tmp_path / "baseline.json").read_text()) == value


def test_requirements_lock_is_pinned_hashed_and_installable():
    lock = (ROOT / "requirements.lock").read_text(encoding="utf-8")
    assert "--index-url https://pypi.org/simple" in lock
    assert "--hash=sha256:" in lock
    packages = re.findall(r"^([A-Za-z0-9_.-]+)==([^ ;\\]+)", lock, re.MULTILINE)
    assert len(packages) >= 40
    assert all(version and re.fullmatch(r"[0-9][A-Za-z0-9.+!-]*", version)
               for _, version in packages)
    result = subprocess.run(
        [sys.executable, "-m", "pip", "install", "--dry-run", "--ignore-installed",
         "--require-hashes", "-r", str(ROOT / "requirements.lock")],
        cwd=ROOT, capture_output=True, text=True, timeout=120,
    )
    assert result.returncode == 0, result.stdout + result.stderr
