#!/usr/bin/env python3
"""Pack the preapproved binder papers with Sonnet, through the pipeline's own path.

Operator ruling (2026-09-07): binder papers are preapproved -- OCR, then pack,
no curate verdict -- and "Sonnet can perform the packing step". This driver
changes ONE thing about how a paper is packed: the model call. Everything else
is the production code, untouched:

  curation_actions._pack_only_raw   raw curator doc -> section-bounded windows
                                    (18k target / 25k cap) -> production pack
                                    prompt (curator/pack_data + key registry) ->
                                    two attempts with gate feedback -> shape
                                    repair -> per-window grounding gates ->
                                    merge -> whole-document gates
  curation_actions.action_curate_book_result
                                    envelope (required_fields_check), dataset
                                    JSON, key-registry fold, papers.jsonl row

The model call is `_curate_turn -> effects.run_inference`; SonnetEffects
overrides run_inference to run `claude -p --model claude-sonnet-5` headless
with the prompt on stdin (prompts run to 100 KB; argv would not hold them),
no tools, no session persistence. provenance.model is stamped through the
domain route the pipeline already reads (_provenance_model), so artifacts say
`claude-sonnet-5`, not the local server's config.

Papers pack concurrently (--concurrency, default 4 claude processes);
BOOKINGS are serialised behind one lock because the key registry is a
read-modify-write file.

  .venv/bin/python dev/pack_binder_sonnet.py --dry-run
  .venv/bin/python dev/pack_binder_sonnet.py --keys doi_10.1080_14786440908637137
  .venv/bin/python dev/pack_binder_sonnet.py --concurrency 4 --log ~/tmp/binder_pack.jsonl
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent.actions import curation_actions as ca  # noqa: E402
from agent.actions.scholarly_actions import read_databank  # noqa: E402
from agent.effects.local import LocalEffects  # noqa: E402
from agent.effects.protocol import InferenceResult  # noqa: E402
from agent.models import FlowMeta, StepInput  # noqa: E402

CORPUS = os.path.expanduser("~/corpora/ouroboros-spectra")
MODEL = os.environ.get("BINDER_PACK_MODEL", "claude-sonnet-5")
CLAUDE = os.environ.get("CLAUDE_BIN", "claude")
SYSTEM = (
    "You are a data-packing function inside a corpus pipeline. Never use tools, "
    "never ask questions, never add commentary. Output exactly what the prompt "
    "asks for: one fenced JSON object."
)
CALL_TIMEOUT_S = 1200


class SonnetEffects(LocalEffects):
    """LocalEffects for files + databank; inference goes to headless Sonnet."""

    def __init__(self, working_dir: str) -> None:
        super().__init__(working_dir)
        # _provenance_model reads these: artifacts record the packing model.
        self._inference_domain = "sonnet"
        self._llmvp_domains = {"sonnet": {"model": MODEL}}
        self.calls = 0
        self.call_seconds = 0.0

    def _call(self, prompt: str) -> tuple[str, str]:
        cmd = [
            CLAUDE,
            "-p",
            "--model",
            MODEL,
            "--output-format",
            "text",
            "--tools",
            "",
            "--no-session-persistence",
            "--append-system-prompt",
            SYSTEM,
        ]
        try:
            proc = subprocess.run(
                cmd,
                input=prompt,
                capture_output=True,
                text=True,
                timeout=CALL_TIMEOUT_S,
                cwd="/tmp",
            )
        except subprocess.TimeoutExpired:
            return "", f"claude -p timed out after {CALL_TIMEOUT_S}s"
        if proc.returncode != 0:
            return (
                proc.stdout or "",
                f"claude -p exit {proc.returncode}: {(proc.stderr or '')[-300:]}",
            )
        return proc.stdout or "", ""

    async def run_inference(  # type: ignore[override]
        self, prompt: str, config_overrides=None, static_prefix=None, flow_key=None
    ) -> InferenceResult:
        t0 = time.monotonic()
        text, err = await asyncio.to_thread(self._call, prompt)
        if err or not text.strip():
            # one retry: transient CLI/API hiccups are not a verdict
            text, err = await asyncio.to_thread(self._call, prompt)
        self.calls += 1
        self.call_seconds += time.monotonic() - t0
        # RAW CAPTURE for diagnosis (new-format-family lesson: keep the raw
        # stream, or "model never answered" and "gate dropped it" look alike).
        cap_dir = os.environ.get("BINDER_PACK_CAPTURE", "")
        if cap_dir:
            os.makedirs(cap_dir, exist_ok=True)
            stamp = datetime.now(timezone.utc).strftime("%H%M%S_%f")
            with open(
                os.path.join(cap_dir, f"call_{stamp}.json"), "w", encoding="utf-8"
            ) as fh:
                json.dump(
                    {
                        "prompt_head": prompt[:600],
                        "prompt_tail": prompt[-3000:],
                        "prompt_chars": len(prompt),
                        "seconds": round(time.monotonic() - t0, 1),
                        "error": err,
                        "response": text,
                    },
                    fh,
                    ensure_ascii=False,
                )
        if err and not text.strip():
            return InferenceResult(
                text="", tokens_generated=0, finished=False, error=err
            )
        return InferenceResult(
            text=text,
            tokens_generated=len(text.split()),
            finished=True,
            prompt_tokens=len(prompt) // 4,
            generated_tokens=len(text.split()),
        )

    async def end_inference_session(self, session_id: str) -> bool:  # no server
        return True

    async def purge_inference_snapshot(self, key: str) -> bool:  # no server
        return True


async def pack_one(
    eff: SonnetEffects, key: str, rec: dict, book_lock: asyncio.Lock, log_path: str
) -> dict:
    t0 = time.monotonic()
    out: dict = {"paper_key": key, "started": datetime.now(timezone.utc).isoformat()}
    try:
        state = await ca._pack_only_raw(eff, key, rec)
    except ca._CurateTransportFault as e:
        out.update(
            status="transport_fault",
            reason=str(e)[:300],
            seconds=round(time.monotonic() - t0),
        )
        _log(log_path, out)
        return out
    pack = state.get("pack") or {}
    q = pack.get("quality") or {}
    out.update(
        status=pack.get("status"),
        reason=(pack.get("reason") or "")[:300],
        windows=q.get("windows"),
        windows_passed=q.get("windows_passed"),
        grounding_rate=q.get("grounding_rate"),
        numeric_leaves=q.get("numeric_leaves"),
        keys=len(pack.get("data") or {}),
        attempts=pack.get("attempts"),
    )
    async with book_lock:
        res = await ca.action_curate_book_result(
            StepInput(
                context={"curate_state": state},
                effects=eff,
                meta=FlowMeta(flow_name="binder_pack_sonnet", step_id="book_result"),
            )
        )
    out.update(booked=res.observations[:200], seconds=round(time.monotonic() - t0))
    _log(log_path, out)
    return out


def _log(path: str, row: dict) -> None:
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, ensure_ascii=False) + "\n")


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--keys", nargs="*", default=None)
    ap.add_argument("--concurrency", type=int, default=4)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument(
        "--log", default=os.path.expanduser("~/tmp/binder_pack_sonnet.jsonl")
    )
    args = ap.parse_args()

    eff = SonnetEffects(CORPUS)
    db = await read_databank(eff)
    todo = []
    for k, r in sorted(db.items()):
        if r.get("curation_method") != "binder_preapproved" and not (
            r.get("binder") and r.get("review_status") == "accepted"
        ):
            continue
        if r.get("pack_status") == "packed":
            continue
        if args.keys and k not in args.keys:
            continue
        if not r.get("md_path"):
            continue
        todo.append((k, r))
    print(
        f"{len(todo)} paper(s) to pack with {MODEL}; concurrency {args.concurrency}; log {args.log}"
    )
    for k, r in todo:
        print(f"  {r.get('extraction_status'):18s} {k}")
    if args.dry_run or not todo:
        return 0

    sem = asyncio.Semaphore(args.concurrency)
    lock = asyncio.Lock()

    async def guarded(k, r):
        async with sem:
            res = await pack_one(eff, k, r, lock, args.log)
            print(
                f"  [{datetime.now().strftime('%H:%M:%S')}] {res.get('status'):16s} "
                f"win={res.get('windows_passed')}/{res.get('windows')} ground={res.get('grounding_rate')} "
                f"keys={res.get('keys')} {res.get('seconds')}s {k[:56]} {res.get('reason','')[:80]}",
                flush=True,
            )
            return res

    results = await asyncio.gather(*(guarded(k, r) for k, r in todo))
    packed = sum(1 for r in results if r.get("status") == "packed")
    print(
        f"\nDONE: packed {packed}/{len(results)}; claude calls {eff.calls}, {eff.call_seconds/60:.1f} call-min"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
