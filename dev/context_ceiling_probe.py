#!/usr/bin/env python3
"""MOVED — the context-ceiling probe is part of LLMVP now.

    llmvp/.venv/bin/python api/main.py --probe-context step37-flash-196b-a11
    llmvp/.venv/bin/python api/main.py --probe-context active
    llmvp/.venv/bin/python api/main.py --probe-context a,b,c --probe-no-write

It lives at `llmvp/core/context_probe.py` because it is not a one-off
experiment: it measures the number the KV preflight otherwise has to guess, and
it writes that number back as `probe_verified_n_ctx`. A guard and the instrument
that calibrates it belong in the same package — a dev/ script drifts from the
code it corrects, which is exactly how a six-week-old note about the runtime
version stayed the most authoritative-looking source in the repo.

This shim exists so the old invocation says where things went rather than
failing with an import error.
"""

import sys

print(__doc__, file=sys.stderr)
sys.exit(2)
