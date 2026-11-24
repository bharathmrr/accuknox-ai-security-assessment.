"""Make ``assignment-1-python`` importable (its folder name contains a hyphen)."""

import sys
from pathlib import Path

ASSIGNMENT_1 = Path(__file__).resolve().parent.parent / "assignment-1-python"
sys.path.insert(0, str(ASSIGNMENT_1))
