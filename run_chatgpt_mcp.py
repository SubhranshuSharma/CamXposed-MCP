#!/usr/bin/env python3
"""Backward-compatible entry point; use `python run.py` for new installs."""
from run import main

if __name__ == "__main__":
    raise SystemExit(main())
