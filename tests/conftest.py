"""Make ``import cashdesk`` work from a plain checkout.

The repository root (the Hermes plugin folder) is put first on ``sys.path`` so
the engine package is importable without installing anything.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
