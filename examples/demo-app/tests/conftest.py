"""Ensure the isolated demo workload is imported from its own directory."""

import sys
from pathlib import Path

DEMO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(DEMO_ROOT))
