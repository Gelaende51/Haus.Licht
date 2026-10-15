#!/usr/bin/env python3
"""hauslicht: entry point; the code lives in the haus/ package (see haus/cli.py)."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from haus.cli import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
