"""Make this folder importable so its tests run from anywhere.

rock_olmo is a PROJECT RESULT, not part of Ouroboros: it reads the
scraper's output and the mineral reference layer and emits training
records. It lives under dev/ for that reason, which means its modules
are not on the repo's import path and its tests are not picked up by
the root `pytest tests/` run. Both are deliberate — see README.md for
how to run them.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
