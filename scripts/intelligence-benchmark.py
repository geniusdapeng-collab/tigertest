#!/usr/bin/env python3
"""Repository-local wrapper for the offline intelligence benchmark."""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from trading_system.intelligence_benchmark import main

if __name__ == "__main__":
    raise SystemExit(main())
