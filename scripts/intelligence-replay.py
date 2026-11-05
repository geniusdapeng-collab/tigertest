#!/usr/bin/env python3
"""Diagnose a frozen native run offline; never fetch or regenerate model output."""
from pathlib import Path
import argparse
import json
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from trading_system.agent_evidence import EvidenceInvalid, atomic_json, read_json
from trading_system.intelligence_replay import replay_native_run


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-root", type=Path, required=True)
    parser.add_argument("--annotations", type=Path)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    try:
        annotations = read_json(args.annotations.parent, args.annotations.name) if args.annotations else None
        report = replay_native_run(args.run_root.resolve(), annotations)
        out = args.out.resolve()
        out.parent.mkdir(parents=True, exist_ok=True)
        atomic_json(out.parent, out.name, report, exclusive=True)
    except (EvidenceInvalid, OSError, ValueError, KeyError, TypeError):
        print(json.dumps({"ok": False, "code": "REPLAY_INVALID", "network_requests": 0}))
        return 2
    print(json.dumps({"ok": True, "document_count": report["document_count"],
                      "production_acceptance": "unverified", "network_requests": 0}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
