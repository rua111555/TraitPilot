#!/usr/bin/env python3
"""TraitPilot 入口：python agent/run.py ...（或 cd agent && python -m skill_agent）"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from skill_agent.cli import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
