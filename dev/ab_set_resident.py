#!/usr/bin/env python3
"""Toggle resident_seq_cache in the gpt-oss config (resident A/B driver helper).

Text-level edit so the file's comments survive. Removes any existing
resident_seq_cache line, then for "true" inserts it after the model name line.
The committed config has no such line (= legacy save_state / full_replay path),
so `git checkout` is the clean reset. Mirrors ab_set_replay.py.

Usage: ab_set_resident.py <true|false>
"""

import pathlib
import sys

CFG = pathlib.Path(__file__).resolve().parent.parent / (
    "llmvp/configs/gpt-oss-120b-a5.yaml"
)


def main() -> None:
    val = sys.argv[1]
    lines = CFG.read_text().splitlines()
    out = [ln for ln in lines if "resident_seq_cache" not in ln]
    if val == "true":
        for i, ln in enumerate(out):
            if ln.strip().startswith("name:") and "gpt-oss" in ln:
                out.insert(i + 1, "  resident_seq_cache: true")
                break
    CFG.write_text("\n".join(out) + "\n")
    print(f"resident_seq_cache -> {val}")


if __name__ == "__main__":
    main()
