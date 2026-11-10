#!/usr/bin/env python3
"""Nightwatch: find cars, people and lights in dark CCTV footage.

    ./nightwatch.py                 open the GUI
    ./nightwatch.py scan DIR -o OUT --compile
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from nw.cli import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
