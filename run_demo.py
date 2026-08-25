#!/usr/bin/env python3
"""Workspace-local launcher that does not require installing the package."""

import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent
SRC_ROOT = PROJECT_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from open_pit_agent.cli import main


if __name__ == "__main__":
    main()

