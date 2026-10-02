#!/usr/bin/env python3
"""Remove or correct the truly incorrect values in booked packs with Sonnet 5.5.

WHY. The 2026-10-02 audit of values the grounding gate flags but tolerates
(1,337 of 209,421; 4 reviewers on a 120-value sample) put ~22% of them --
~0.14% of all packed values -- in the truly incorrect classes: FABRICATED,
MISATTRIBUTED, WRONG UNIT CONVERSION. Operator ruling 2026-10-02: Sonnet 5.5
cleans up exactly those values ("the 0.14% deemed truly incorrect"), nothing
else. The 0.14% is an estimate from a sample, so the incorrect values must
first be FOUND among the values the gate still flags; every other class --
correct in another notation, correct conversion, approximate (figure
estimate, derived, rounded, textbook constant), label -- is left untouched.

WHAT SONNET DOES. Per pack, headless `claude -p --model claude-sonnet-5-5` with
READ-ONLY tools (Read, Grep) in a work dir holding the paper's curator doc
(paper.txt), the pack (pack.json) and the suspects (suspects.json: every value
the current gate flags in that pack, with the audit reviewer's note where one
exists). For each suspect it returns a VERDICT and an op: keep; or, only for
the three incorrect verdicts, remove (or replace, when the paper states the
right value for that slot).

WHAT THIS DRIVER CHECKS. An edit is applied only for an incorrect verdict and
only at a suspect's own path or the list entry holding it -- no sweep of
other values. A replacement is applied only when its value grounds in the
paper or its evidence quote is found there; otherwise the slot is removed,
never kept wrong. Nothing is added and no key is renamed. The patched pack faces the production gates and is booked only
when it carries no masked placeholder and no new type mismatch. Booking writes
the dataset envelope first (provenance.cleanup names the model and the
counts), then a fresh full papers row (pack_quality.pack_cleanup).

    .venv/bin/python dev/clean_packs_sonnet.py select
    .venv/bin/python dev/clean_packs_sonnet.py run --keys K1,K2          # dry run
    .venv/bin/python dev/clean_packs_sonnet.py run --apply --concurrency 3
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent.actions import curation_actions as ca  # noqa: E402
from agent.actions.scholarly_actions import append_records, read_databank  # noqa: E402
from agent.effects.local import LocalEffects  # noqa: E402

CORPUS = os.path.expanduser("~/corpora/ouroboros-spectra")
WORK = Path(os.path.expanduser(os.environ.get("CLEAN_WORK", "~/tmp/pack_cleanup")))
AUDIT = WORK / "audit"  # review_result_*.json + review_batch_*.json from the audit
MODEL = os.environ.get("CLEAN_MODEL", "claude-sonnet-5-5")
CLAUDE = os.environ.get("CLAUDE_BIN", "claude")
CALL_TIMEOUT_S = 1800
LIMIT_SIGNS = ("session limit", "usage limit", "rate limit", "limit reached")

PROMPT = """You are checking suspect values in one data pack that an extraction model built from a scientific paper. Files in the current directory:
- paper.txt: the paper's full text exactly as the extraction saw it (markdown; tables are HTML; figure descriptions are inlined as "[FIGURE ... — VLM reading]: ..." and are approximate readings).
- pack.json: the extracted data, key -> value.
- suspects.json: values an automatic check could not find printed in paper.txt, each with its path in pack.json and, where an earlier reviewer looked, that reviewer's note.

This cleanup corrects ONLY values that are truly incorrect. For EVERY suspect, search paper.txt (use Grep; read the surrounding passage or table row) and give one verdict:
- correct: the paper states this value, possibly in another notation (power of ten, decimal comma, thousands separator, OCR-garbled) or in another unit correctly converted.
- approximate: read off a figure, computed from printed numbers (mean, midpoint, sum), rounded, or a standard textbook value. Approximate is NOT incorrect: keep it.
- label: an identifier, sample number, index or code rather than a measurement: keep it.
- fabricated: not in the paper, not derivable from it, and not a standard value; or contradicted by the paper; or copied from somewhere else (e.g. a registry example).
- misattributed: the number is in the paper but belongs to another quantity, sample, row or column.
- wrong_conversion: a unit conversion with the wrong factor or direction.
Ops: keep for correct, approximate and label. For fabricated: remove. For misattributed and wrong_conversion: replace when the paper states the right value for this slot (give it in the unit the key name says, with the quote), otherwise remove. To remove a whole list entry whose only substance was the incorrect value, give the entry's path (e.g. samples[64]); otherwise give the suspect's own path. Touch nothing else.

Output ONLY one fenced JSON object, nothing after it:
```json
{"ops": [{"path": "...", "verdict": "correct|approximate|label|fabricated|misattributed|wrong_conversion", "op": "keep|replace|remove", "value": <replace only>, "evidence": "<verbatim quote from paper.txt, max 160 chars; empty when nothing can be quoted>", "reason": "<one line>"}]}
```"""

INCORRECT = frozenset({"fabricated", "misattributed", "wrong_conversion"})


# ── paths ─────────────────────────────────────────────────────────────


def _parts(path: str) -> list:
    out: list = []
    for m in re.finditer(r"\[(\d+)\]|([^.\[\]]+)", path):
        out.append(int(m.group(1)) if m.group(1) is not None else m.group(2))
    return out


def _resolve(data, path: str):
    cur = data
    for p in _parts(path):
        if isinstance(p, int):
            if not isinstance(cur, list) or p >= len(cur):
                raise KeyError(path)
        elif not isinstance(cur, dict) or p not in cur:
            raise KeyError(path)
        cur = cur[p]
    return cur


def _set(data, path: str, value) -> None:
    parts = _parts(path)
    parent = _resolve(data, _join(parts[:-1])) if len(parts) > 1 else data
    parent[parts[-1]] = value


def _join(parts: list) -> str:
    s = ""
    for p in parts:
        s += f"[{p}]" if isinstance(p, int) else (f".{p}" if s else p)
    return s


def _in_scope(path: str, suspects: set[str]) -> bool:
    """A suspect's own path, or the list entry that holds it."""
    return path in suspects or (
        path.endswith("]")
        and any(s.startswith(path + ".") or s.startswith(path + "[") for s in suspects)
    )


_GONE = object()
_EMPTIED = object()


def _sweep(node):
    """(node without _GONE marks, emptied?) -- a container whose every member
    was removed is itself removed; one that was empty to begin with stays."""
    if isinstance(node, dict):
        kept = {}
        for k, v in node.items():
            if v is _GONE:
                continue
            v2, emptied = _sweep(v)
            if not emptied:
                kept[k] = v2
        return kept, bool(node) and not kept
    if isinstance(node, list):
        kept = []
        for v in node:
            if v is _GONE:
                continue
            v2, emptied = _sweep(v)
            if not emptied:
                kept.append(v2)
        return kept, bool(node) and not kept
    return node, False


def apply_ops(
    data: dict, ops: list[dict], doc: str, suspects: set[str]
) -> tuple[dict, list[dict]]:
    """(patched copy, log). Edits only for an incorrect verdict at a suspect's
    path (or its list entry). Replacements first; removals deepest/last-index
    first so earlier indices stay valid; containers emptied by a removal go."""
    out = json.loads(json.dumps(data))
    log: list[dict] = []
    removes: list[list] = []
    doc_n = ca._norm(doc)
    for op in ops:
        path, kind = str(op.get("path") or ""), str(op.get("op") or "")
        verdict = str(op.get("verdict") or "").strip().lower()
        try:
            _resolve(out, path)
        except KeyError:
            log.append({**op, "result": "rejected: path does not resolve"})
            continue
        if kind == "keep" or verdict not in INCORRECT:
            log.append(
                {
                    **op,
                    "result": (
                        "kept" if kind == "keep" else "kept: verdict is not incorrect"
                    ),
                }
            )
            continue
        if not _in_scope(path, suspects):
            log.append({**op, "result": "rejected: not a suspect's path"})
            continue
        if kind == "replace" and "value" in op:
            ev = ca._norm(str(op.get("evidence") or ""))
            grounded = ca.grounding_check({"v": op["value"]}, doc)["passed"]
            if grounded or (len(ev) >= 8 and ev in doc_n):
                _set(out, path, op["value"])
                log.append(
                    {**op, "result": "replaced" + ("" if grounded else " (evidence)")}
                )
            else:
                removes.append(_parts(path))
                log.append({**op, "result": "removed: replacement unsupported"})
        elif kind in ("remove", "replace"):
            removes.append(_parts(path))
            log.append({**op, "result": "removed"})
        else:
            log.append({**op, "result": f"rejected: unknown op {kind!r}"})
    # MARK, then sweep once: popping in place shifts the indices of later
    # removals in the same list, and pruning an emptied entry mid-loop did too.
    for parts in removes:
        try:
            _set(out, _join(parts), _GONE)
        except (KeyError, IndexError, TypeError):
            continue
    out, _ = _sweep(out)
    return out, log


# ── selection ─────────────────────────────────────────────────────────


async def cmd_select(a) -> int:
    """Every booked pack with a value the CURRENT gate flags; each flagged
    leaf is a suspect, with the audit reviewer's note where there is one."""
    fx = LocalEffects(CORPUS)
    bank = await read_databank(fx)
    reg = json.loads((await fx.read_file(ca.KEY_REGISTRY_PATH)).content)
    top = sorted(reg.items(), key=lambda kv: -int(kv[1].get("count") or 0))[
        : ca.REGISTRY_PROMPT_TOP_N
    ]
    ex_nums = {
        k: set(re.findall(r"\d+(?:\.\d+)?", str(e.get("exemplar") or "")))
        for k, e in top
    }
    notes: dict[tuple, str] = {}
    for f in sorted(AUDIT.glob("review_result_*.json")):
        batch = {
            i["id"]: i
            for i in json.loads((AUDIT / f.name.replace("result", "batch")).read_text())
        }
        for r in json.loads(f.read_text()):
            it = batch.get(r["id"])
            if it:
                notes[(it["paper_key"], it["path"])] = (
                    f"reviewer: {r['category']} -- {r['note']}"
                )
    targets = []
    for key, rec in sorted(bank.items()):
        if (
            rec.get("pack_status") != "packed"
            or rec.get("review_status") != "accepted"
            or not rec.get("dataset_path")
        ):
            continue
        try:
            data = (
                json.loads((Path(CORPUS) / rec["dataset_path"]).read_text()).get("data")
                or {}
            )
        except (OSError, ValueError):
            continue
        doc = await ca._raw_curator_doc(fx, key)
        if not doc or not data:
            continue
        g = ca.grounding_check(data, doc, cap=None)
        sus, seen = [], set()
        for u in g["ungrounded"]:
            if u["path"] in seen:
                continue
            seen.add(u["path"])
            why = notes.get((key, u["path"]), "")
            if u["token"].lstrip("-") in ex_nums.get(
                re.split(r"[.\[]", u["path"])[0], set()
            ):
                why = (
                    (why + "; " if why else "")
                    + "equals the value the registry used to show as this key's example"
                )
            sus.append(
                {
                    "path": u["path"],
                    "token": u["token"],
                    "value": ca_value(data, u["path"]),
                    "note": why,
                }
            )
        if sus:
            targets.append({"paper_key": key, "suspects": sus})
    WORK.mkdir(parents=True, exist_ok=True)
    (WORK / "targets.json").write_text(
        json.dumps(targets, indent=1, ensure_ascii=False)
    )
    print(
        f"{len(targets)} packs, {sum(len(t['suspects']) for t in targets)} suspects -> {WORK / 'targets.json'}"
    )
    return 0


def ca_value(data, path):
    try:
        return _resolve(data, path)
    except KeyError:
        return None


# ── run ───────────────────────────────────────────────────────────────


def _call(cwd: Path) -> tuple[str, str]:
    cmd = [
        CLAUDE,
        "-p",
        "--model",
        MODEL,
        "--tools",
        "Read,Grep",
        "--no-session-persistence",
        "--output-format",
        "json",
        PROMPT,
    ]
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=CALL_TIMEOUT_S, cwd=str(cwd)
        )
    except subprocess.TimeoutExpired:
        return "", f"timeout after {CALL_TIMEOUT_S}s"
    try:
        d = json.loads(proc.stdout)
    except ValueError:
        return (
            proc.stdout or "",
            f"exit {proc.returncode}: {(proc.stderr or '')[-300:]}",
        )
    text = str(d.get("result") or "")
    if (
        d.get("is_error")
        or any(s in text.lower() for s in LIMIT_SIGNS)
        and "ops" not in text
    ):
        return text, f"provider: {text[:200]}"
    return text, ""


def _parse_ops(text: str) -> dict | None:
    from agent.llm_json import parse_llm_json

    got = parse_llm_json(text)
    return got if isinstance(got, dict) and isinstance(got.get("ops"), list) else None


async def clean_one(fx, t: dict, *, apply: bool, lock: asyncio.Lock) -> dict:
    key = t["paper_key"]
    rec = (await read_databank(fx)).get(key) or {}
    env_path = Path(CORPUS) / rec["dataset_path"]
    envelope = json.loads(env_path.read_text())
    data = envelope.get("data") or {}
    doc = await ca._raw_curator_doc(fx, key)
    # A key ending in "." (doi_10.7907_0st8-5h98.) reads as a Windows path
    # pattern to Claude Code's permission check, which refused every read.
    wd = WORK / key.rstrip(". ")
    wd.mkdir(parents=True, exist_ok=True)
    (wd / "paper.txt").write_text(doc)
    (wd / "pack.json").write_text(json.dumps(data, indent=1, ensure_ascii=False))
    (wd / "suspects.json").write_text(
        json.dumps(t["suspects"], indent=1, ensure_ascii=False)
    )
    t0 = time.monotonic()
    text, err = await asyncio.to_thread(_call, wd)
    if err or _parse_ops(text) is None:
        text, err = await asyncio.to_thread(_call, wd)  # one retry
    (wd / "sonnet.txt").write_text(text)
    parsed = _parse_ops(text)
    if err or parsed is None:
        return {
            "paper_key": key,
            "outcome": f"no ops: {err or 'unparsed'}"[:240],
            "limited": err.startswith("provider"),
        }
    patched, log = apply_ops(
        data, parsed["ops"], doc, {x["path"] for x in t["suspects"]}
    )
    (wd / "patched.json").write_text(json.dumps(patched, indent=1, ensure_ascii=False))
    (wd / "log.json").write_text(json.dumps(log, indent=1, ensure_ascii=False))
    registry = await ca._load_registry(fx)
    before = ca._run_pack_gates(data, doc, registry)
    after = ca._run_pack_gates(patched, doc, registry)
    counts = {
        k: sum(1 for x in log if x["result"].startswith(k))
        for k in ("kept", "replaced", "removed", "rejected")
    }
    summary = {
        "paper_key": key,
        "ops": counts,
        "grounding": [
            before["grounding"]["grounding_rate"],
            after["grounding"]["grounding_rate"],
        ],
        "leaves": [
            before["grounding"]["numeric_leaves"],
            after["grounding"]["numeric_leaves"],
        ],
        "seconds": round(time.monotonic() - t0),
        "notes": str(parsed.get("notes") or "")[:200],
    }
    bad = (
        not patched
        or ca.placeholder_leaves(patched)
        or len(after["registry"]["type_mismatches"])
        > len(before["registry"]["type_mismatches"])
    )
    if bad:
        return {
            **summary,
            "outcome": "not booked: patched pack failed a structural check",
        }
    if not apply or patched == data:
        return {**summary, "outcome": "dry run" if not apply else "no change"}
    async with lock:
        stamp = datetime.now(timezone.utc).isoformat()
        cleanup = {
            "model": MODEL,
            "at": stamp,
            **counts,
            "scope": "flagged values judged fabricated, misattributed or wrongly converted",
        }
        envelope["data"] = patched
        envelope["provenance"] = {
            **(envelope.get("provenance") or {}),
            "cleanup": list((envelope.get("provenance") or {}).get("cleanup") or [])
            + [cleanup],
        }
        env_path.write_text(json.dumps(envelope, indent=1, ensure_ascii=False))
        rec = dict((await read_databank(fx)).get(key) or rec)  # fresh, full row
        g = after["grounding"]
        rec["pack_quality"] = {
            **(rec.get("pack_quality") or {}),
            "grounding_rate": g["grounding_rate"],
            "numeric_leaves": g["numeric_leaves"],
            "ungrounded": g["ungrounded"],
            "pack_cleanup": cleanup,
        }
        rec["updated_at"] = stamp
        await append_records(fx, [rec])
    return {**summary, "outcome": "booked"}


async def cmd_run(a) -> int:
    targets = json.loads((WORK / "targets.json").read_text())
    if a.keys:
        want = set(a.keys.split(","))
        targets = [t for t in targets if t["paper_key"] in want]
    done = set()
    logf = WORK / "run.jsonl"
    if a.apply and logf.exists():
        done = {
            json.loads(x)["paper_key"]
            for x in logf.read_text().splitlines()
            if '"booked"' in x or '"no change"' in x
        }
    targets = [t for t in targets if t["paper_key"] not in done][: a.limit or None]
    fx = LocalEffects(CORPUS)
    sem, lock = asyncio.Semaphore(a.concurrency), asyncio.Lock()
    stop = asyncio.Event()

    async def one(t):
        if stop.is_set():
            return
        async with sem:
            if stop.is_set():
                return
            try:
                r = await clean_one(fx, t, apply=a.apply, lock=lock)
            except Exception as e:  # noqa: BLE001 -- one pack never stops the run
                r = {
                    "paper_key": t["paper_key"],
                    "outcome": f"error: {type(e).__name__}: {e}"[:240],
                }
            if r.get("limited"):
                stop.set()
            with open(logf, "a", encoding="utf-8") as fh:
                fh.write(json.dumps({**r, "apply": a.apply}, ensure_ascii=False) + "\n")
            print(json.dumps(r, ensure_ascii=False), flush=True)

    print(
        f"{len(targets)} pack(s) to clean ({'APPLY' if a.apply else 'dry run'}, {MODEL})",
        flush=True,
    )
    await asyncio.gather(*(one(t) for t in targets))
    if stop.is_set():
        print("stopped: the provider reported a limit", flush=True)
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("select")
    r = sub.add_parser("run")
    r.add_argument("--keys", default="")
    r.add_argument("--limit", type=int, default=0)
    r.add_argument("--concurrency", type=int, default=2)
    r.add_argument("--apply", action="store_true")
    a = ap.parse_args()
    return asyncio.run(cmd_select(a) if a.cmd == "select" else cmd_run(a))


if __name__ == "__main__":
    raise SystemExit(main())
