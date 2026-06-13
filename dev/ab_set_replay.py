#!/usr/bin/env python3
"""Toggle session_full_replay in the gpt-oss config (A/B driver helper).

Text-level edit so the file's comments survive. Removes any existing
session_full_replay line, then for "true" inserts it after the model
name line. The committed config has no such line (= save/load), so
`git checkout` is the clean reset.

Usage: ab_set_replay.py <true|false>
"""

import pathlib
import sys

CFG = pathlib.Path(__file__).resolve().parent.parent / (
    "llmvp/configs/gpt-oss-120b-a5.yaml"
)


def main() -> None:
    val = sys.argv[1]
    lines = CFG.read_text().splitlines()
    out = [ln for ln in lines if "session_full_replay" not in ln]
    if val == "true":
        for i, ln in enumerate(out):
            if ln.strip().startswith("name:") and "gpt-oss" in ln:
                out.insert(i + 1, "  session_full_replay: true")
                break
    CFG.write_text("\n".join(out) + "\n")
    print(f"session_full_replay -> {val}")


if __name__ == "__main__":
    main()
