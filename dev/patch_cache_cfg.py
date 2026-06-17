#!/usr/bin/env python3
"""Idempotently set a model field in an llmvp config (line-based, format-safe).

Usage: patch_cache_cfg.py <config.yaml> <field> <value>
Replaces the field's value if present in the model section, else inserts it right
after `flash_attention:`. Used to enable swa_full/kv_unified/flow_kv_cache and to
toggle flow_kv_cache off per-model when a stability check trips.
"""
import re
import sys

cfg, field, value = sys.argv[1], sys.argv[2], sys.argv[3]
lines = open(cfg).read().splitlines()
out, done = [], False
for ln in lines:
    m = re.match(rf"^(\s*){re.escape(field)}:\s*\S", ln)
    if m and not done:
        out.append(f"{m.group(1)}{field}: {value}")
        done = True
    else:
        out.append(ln)
if not done:
    for i, ln in enumerate(out):
        if re.match(r"^\s*flash_attention:", ln):
            indent = re.match(r"^(\s*)", ln).group(1)
            out.insert(i + 1, f"{indent}{field}: {value}")
            done = True
            break
open(cfg, "w").write("\n".join(out) + "\n")
print(f"{'set' if done else 'FAILED to set'} {field}={value} in {cfg}")
