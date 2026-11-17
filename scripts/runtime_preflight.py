#!/usr/bin/env python3
"""Deterministic backend/full runtime preflight used by setup and doctor."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import subprocess
import sys

BASELINE_SCHEMA = "tiger.install-baseline/v1"
DEPENDENCIES = (
    "pandas", "numpy", "yfinance", "pytest", "requests", "curl_cffi",
    "PyYAML", "tokenizers", "pypdf", "scrapling",
)


def _run(command: list[str], *, timeout: int = 10) -> str:
    try:
        result = subprocess.run(
            command, check=True, capture_output=True, text=True, timeout=timeout,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise RuntimeError(f"runtime probe failed: {command[0]}") from exc
    return result.stdout.strip()


def python_identity(path: str) -> dict:
    output = _run([
        path, "-I", "-B", "-c",
        "import json,sys;print(json.dumps([sys.version_info.major,sys.version_info.minor,sys.version_info.micro]))",
    ])
    try:
        version = json.loads(output)
    except (ValueError, TypeError) as exc:
        raise RuntimeError("Python version probe returned invalid JSON") from exc
    if (not isinstance(version, list) or len(version) != 3
            or any(type(value) is not int for value in version)
            or tuple(version) < (3, 11, 0)):
        raise RuntimeError("Python 3.11 or newer is required")
    return {"path": str(Path(path).resolve()), "version": version}


def node_identity(path: str) -> dict:
    output = _run([path, "--version"])
    text = output.removeprefix("v")
    parts = text.split(".")
    if (len(parts) != 3 or any(not part.isdigit() for part in parts)
            or tuple(map(int, parts)) < (24, 0, 0)):
        raise RuntimeError("Node 24 or newer is required for the full profile")
    return {"path": str(Path(path).resolve()), "version": list(map(int, parts))}


def pnpm_identity(path: str) -> dict:
    output = _run([path, "--version"])
    parts = output.split(".")
    if (len(parts) != 3 or any(not part.isdigit() for part in parts)
            or tuple(map(int, parts)) != (10, 14, 0)):
        raise RuntimeError("pnpm 10.14.0 is required for the governance lock")
    return {"path": str(Path(path).resolve()), "version": list(map(int, parts))}


def dependency_versions(python: str) -> dict:
    names = json.dumps(DEPENDENCIES)
    script = (
        "import importlib.metadata,json,sys;"
        f"names=json.loads({names!r});"
        "out={};\n"
        "for name in names:\n"
        " try: out[name]=importlib.metadata.version(name)\n"
        " except importlib.metadata.PackageNotFoundError: out[name]=None\n"
        "print(json.dumps(out,sort_keys=True))"
    )
    output = _run([python, "-I", "-B", "-c", script])
    value = json.loads(output)
    if not isinstance(value, dict):
        raise RuntimeError("dependency probe returned invalid JSON")
    return value


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--profile", choices=("backend", "full"), required=True)
    parser.add_argument("--python-bin", required=True)
    parser.add_argument("--node-bin")
    parser.add_argument("--pnpm-bin")
    parser.add_argument("--dependencies", action="store_true")
    parser.add_argument("--out")
    args = parser.parse_args(argv)
    try:
        if args.profile == "full" and (not args.node_bin or not args.pnpm_bin):
            raise RuntimeError("Node 24+ and pnpm 10.14.0 are required for the full profile")
        value = {
            "schema": BASELINE_SCHEMA,
            "profile": args.profile,
            "ready": True,
            "python": python_identity(args.python_bin),
            "node": node_identity(args.node_bin) if args.profile == "full" else None,
            "pnpm": pnpm_identity(args.pnpm_bin) if args.profile == "full" else None,
            "dependencies": dependency_versions(args.python_bin) if args.dependencies else None,
        }
        encoded = json.dumps(value, ensure_ascii=False, sort_keys=True) + "\n"
        if args.out:
            Path(args.out).write_text(encoded, encoding="utf-8")
        sys.stdout.write(encoded)
        return 0
    except (OSError, RuntimeError, ValueError, TypeError, json.JSONDecodeError) as exc:
        value = {
            "schema": BASELINE_SCHEMA,
            "profile": args.profile,
            "ready": False,
            "error": {"code": "RUNTIME_NOT_READY", "message": str(exc)},
        }
        encoded = json.dumps(value, ensure_ascii=False, sort_keys=True) + "\n"
        if args.out:
            try:
                Path(args.out).write_text(encoded, encoding="utf-8")
            except OSError:
                pass
        sys.stdout.write(encoded)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
