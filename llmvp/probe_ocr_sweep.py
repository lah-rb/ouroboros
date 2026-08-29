#!/usr/bin/env python3
"""Drive the Phase 0 OCR cells: restart the server per geometry, run the real
tool, keep the wire trace and the tool's own quality verdict per cell.

CELLS. Each is (vision_pool_size, vision_n_ctx, vl_parallel). The KV ladder is
ISO-MEMORY: vision_n_ctx is per CONTEXT and the preflight multiplies by
vision_pool_size, so (2 x 32768), (4 x 16384) and (8 x 8192) all cost the same
vision KV. Width is therefore free if quality holds at the narrower window —
which is exactly what the numeric/span recall columns are here to check, since
the 2026-08-12 measurement that blessed `-c 8192` per slot was taken on the
subprocess path, not this one.

vl_parallel 1 is not a serving proposal — it is the UNCONTENDED control. The
production fan-out is 4-wide against a pool of 2, so almost every recorded
request queues, and a stage-split fit on those rows would charge queue wait to
the fixed term. One cell at vl_parallel 1 buys rows with no queue in them.

QUALITY IS PART OF THE MEASUREMENT, NOT A FOLLOW-UP. A narrower KV that
silently truncates would look like a throughput win in the wall column alone.

RUN (from repo root, mission stopped, nothing else on 8008):
  llmvp/.venv/bin/python llmvp/probe_ocr_sweep.py
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import time
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LLMVP = os.path.join(ROOT, "llmvp")
CFG = os.path.join(LLMVP, "configs", "experiments", "paddle-ocr-stage-probe.yaml")
SCRATCH = (
    "/tmp/claude-1000/-home-lah-rb-Repos-ouroboros/"
    "89c0814e-2bf4-43de-a225-ceaa353d642d/scratchpad"
)
OUTDIR = os.path.join(LLMVP, "probe_out")
RESULTS = os.path.join(OUTDIR, "ocr_sweep.jsonl")

# (pool, n_ctx, vl_parallel, papers, pages, label)
#
# The throughput cells share ONE workload (6 papers x 4 pages) so their wall
# columns are comparable. The control does not enter that comparison — it
# exists only to supply queue-free rows for the stage-split fit — so it runs a
# smaller slice; at vl_parallel 1 the same workload would take 3-4x longer and
# buy nothing the fit needs.
CELLS = [
    (2, 32768, 1, 3, 4, "uncontended-control"),
    (2, 32768, 4, 6, 4, "production-today"),
    (4, 16384, 4, 6, 4, "width4-isomem"),
    (8, 8192, 4, 6, 4, "width8-isomem"),
]


def write_cfg(pool: int, n_ctx: int) -> None:
    src = open(CFG).read()
    out, seen_model = [], False
    for line in src.splitlines():
        if line.startswith("model:"):
            seen_model = True
        if seen_model and line.strip().startswith("vision_n_ctx:"):
            out.append(f"  vision_n_ctx: {n_ctx}")
            continue
        if seen_model and line.strip().startswith("vision_pool_size:"):
            out.append(f"  vision_pool_size: {pool}")
            continue
        out.append(line)
    tmp = CFG + ".tmp"
    with open(tmp, "w") as fh:
        fh.write("\n".join(out) + "\n")
    os.replace(tmp, CFG)  # atomic: never edit a file a live process may read


def server_pids() -> list[int]:
    # "[a]pi/main.py" so the pattern cannot match this pgrep's own argv. The
    # bare form has cost two self-kills in one session; the bracket is cheap.
    r = subprocess.run(
        ["pgrep", "-f", "[a]pi/main.py"], capture_output=True, text=True
    )
    return [int(x) for x in r.stdout.split() if x.strip().isdigit()]


def stop_server() -> None:
    pids = server_pids()
    for p in pids:
        try:
            os.kill(p, signal.SIGTERM)
        except ProcessLookupError:
            pass
    for _ in range(60):
        if not server_pids():
            return
        time.sleep(1)
    raise RuntimeError(f"server would not stop: {server_pids()}")


def start_server() -> str:
    log = os.path.join(SCRATCH, f"srv_{int(time.time())}.log")
    env = dict(os.environ)
    home = os.path.expanduser("~")
    env["LD_LIBRARY_PATH"] = (
        f"{home}/cuda-libs/nvidia/cublas/lib:{home}/cuda-libs/nvidia/cuda_runtime/lib"
    )
    fh = open(log, "w")
    subprocess.Popen(
        [os.path.join(LLMVP, ".venv", "bin", "python"), "api/main.py"],
        cwd=LLMVP, env=env, stdout=fh, stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL, start_new_session=True,
    )
    for _ in range(180):
        time.sleep(2)
        try:
            txt = open(log).read()
        except OSError:
            continue
        if "Uvicorn running" in txt:
            return log
        if "Traceback" in txt or "CUDA error" in txt:
            raise RuntimeError(f"server failed to start — see {log}")
    raise RuntimeError(f"server start timed out — see {log}")


def warm() -> None:
    """Build the LAZY vision pool before timing anything.

    _build_vision_pool runs on first vision request, so without this the first
    cell's first crop pays for N context allocations and the trace records it
    as a very slow request.
    """
    man = json.load(open(os.path.join(SCRATCH, "ocr_probe", "manifest.json")))
    import base64

    b = base64.b64encode(open(man["pages"][0]["path"], "rb").read()).decode()
    body = json.dumps({
        "max_tokens": 8, "temperature": 0.8,
        "messages": [{"role": "user", "content": [
            {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b}"}},
            {"type": "text", "text": "Transcribe all text in this image."}]}],
    }).encode()
    req = urllib.request.Request(
        "http://127.0.0.1:8008/v1/vision", data=body,
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=600) as r:
        r.read()


def wire_lines(wire: str) -> int:
    """Lines already in the wire log.

    THE DRIVER MUST NOT UNLINK THIS FILE. The proxy opened it once at startup
    and holds the handle; os.remove only drops the name, so the proxy keeps
    appending to a now-anonymous inode and every per-cell copy comes out
    empty. (Recoverable live from /proc/<proxy-pid>/fd/N — which is how the
    2026-08-29 run's traces were saved — but only while the proxy is alive.)
    Slice by offset instead.
    """
    try:
        with open(wire) as fh:
            return sum(1 for _ in fh)
    except OSError:
        return 0


def run_tool(vl_parallel: int, papers: int, pages: int,
             wire: str) -> tuple[float, list[dict], str]:
    man = json.load(open(os.path.join(SCRATCH, "ocr_probe", "manifest.json")))
    wd = "/home/lah-rb/corpora/ouroboros-spectra"
    db = os.path.join(SCRATCH, "scratch_databank")
    shutil.rmtree(db, ignore_errors=True)
    os.makedirs(db, exist_ok=True)
    sel = man["papers"][:papers]
    keys = [p["key"] for p in sel]
    pdfs = [os.path.join(wd, p["pdf"]) for p in sel]
    cmd = [
        os.path.join(ROOT, "tools/pdf_extract/.venv/bin/python"),
        os.path.join(ROOT, "tools/pdf_extract/extract_batch.py"),
        "--pdfs", *pdfs, "--keys", *keys,
        "--databank-dir", db, "--vl-backend", "llmvp",
        "--page-range", f"0:{pages}", "--vl-parallel", str(vl_parallel),
    ]
    env = dict(os.environ)
    env["OUROBOROS_LLMVP_URL"] = "http://127.0.0.1:8010"
    env["OUROBOROS_LLMVP_VL_MODEL"] = "paddle-ocr-stage-probe"
    t0 = time.time()
    r = subprocess.run(cmd, cwd=ROOT, env=env, capture_output=True,
                       text=True, timeout=7200)
    wall = time.time() - t0
    reports = []
    for line in (r.stdout or "").splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            j = json.loads(line)
        except Exception:  # noqa: BLE001
            continue
        if isinstance(j, dict) and j.get("paper_key"):
            reports.append(j)
    return wall, reports, (r.stdout or "")[-3000:]


def main() -> int:
    os.makedirs(OUTDIR, exist_ok=True)
    wire = os.path.join(OUTDIR, "ocr_wire.jsonl")
    sink = open(RESULTS, "a")
    for pool, n_ctx, par, papers, pages, label in CELLS:
        print(f"\n=== cell {label}: pool={pool} n_ctx={n_ctx} "
              f"vl_parallel={par} papers={papers} pages={pages} ===", flush=True)
        stop_server()
        write_cfg(pool, n_ctx)
        log = start_server()
        warm()
        start_line = wire_lines(wire)
        wall, reports, tail = run_tool(par, papers, pages, wire)
        keep = os.path.join(OUTDIR, f"ocr_wire_{label}.jsonl")
        try:
            with open(wire) as src, open(keep, "w") as dst:
                for i, line in enumerate(src):
                    if i >= start_line:
                        dst.write(line)
        except OSError:
            logger_note = f"could not slice {wire}"
            print(logger_note, flush=True)
        # The tool reports RATES per paper, not raw hit/total, so aggregate
        # them weighted by verified_pages — an unweighted mean would let an
        # 8-page paper and a 1-page paper vote equally on corpus quality.
        vw = [(int(r.get("verified_pages") or 0), r) for r in reports]
        wsum = sum(w for w, _ in vw) or 1

        def wavg(field: str):
            vals = [(w, r.get(field)) for w, r in vw
                    if w and isinstance(r.get(field), (int, float))]
            if not vals:
                return None
            return round(sum(w * v for w, v in vals) / sum(w for w, _ in vals), 4)

        pages = sum(int(r.get("verified_pages") or 0)
                    + int(r.get("unverified_pages") or 0) for r in reports)
        row = {
            "label": label, "pool": pool, "n_ctx": n_ctx, "vl_parallel": par,
            "wall_s": round(wall, 1), "papers": len(reports), "pages": pages,
            "verified_pages": wsum,
            "pages_per_min": round(pages / (wall / 60.0), 2) if wall else 0,
            "numeric_recall": wavg("numeric_match_rate"),
            "span_recall": wavg("span_pass_rate"),
            # Repetition run-length separates real damage from furniture; the
            # numeric rate alone is uncorrelated with blind quality.
            "max_repeat_words": max(
                [int(r.get("max_repeat_words") or 0) for r in reports] or [0]),
            "table_token_leak": sum(
                int(r.get("table_token_leak") or 0) for r in reports),
            "errors": [r.get("error") for r in reports if r.get("error")][:5],
            "server_log": log, "wire": keep,
        }
        print(json.dumps(row), flush=True)
        sink.write(json.dumps(row) + "\n")
        sink.flush()
    sink.close()
    print(f"\nwrote {RESULTS}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
