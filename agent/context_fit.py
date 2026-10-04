"""Whole if it fits, otherwise an index to drill into — one rule for every read.

Operator ruling (2026-09-26). Anything a model turn READS — a source file, a
data file, a command's output, a slice of one — is shown WHOLE when it is
within a share of the serving model's window AND fits in the context still
free:

    whole  iff  tokens <= SHARE * window   and   tokens <= window - used

Otherwise the model gets an INDEX of the content (a code file's symbol
outline, a data file's addressable entries, a text's line ranges) and drills
into the part it needs. Never a silent cut. The operator's cases, on a 10k
window: 2.4k with 5k used is whole (under 2.5k, fits the 5k free); 2.4k with
8k used falls back (only 2k free); 3k with 2k used falls back (over the 2.5k
share even though it would fit).

``used`` is what the context already holds PLUS the output reserve for the
turn that will read it: a session's occupancy after its last turn, or the rest
of a one-shot prompt.

It replaces the fixed budgets that did this job badly: the interact world map
at 4,000 chars ("starting_room … (4 more items)"), escalation reads at 6,000,
rewrite's 24 KB gate, the trace fallback's 4,000, data_patch's 8,000, the
connected-data block's 1,200. The window comes from LLMVP (``health.nCtxSeq``,
the per-sequence window — never config), sizes from the serving model's own
tokenizer (``token_count``), with the fitted chars x 13/40 estimate only when
the server will not say.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)

# The share of the window one read may take. A quarter leaves room for the
# prompt around it, the answer, and the reads that follow it in a session.
SHARE = 0.25
# Output reserve when a turn sets no max_tokens. The largest verdict of the
# 2026-09-25 run was 3,366 tokens (reasoning high; p90 1,546); 8k leaves a
# longer thinker room without starving the evidence.
OUTPUT_RESERVE = 8192
# The window assumed when the server does not report nCtxSeq: the smallest any
# configured text model serves (hy3, muse). Too small costs evidence; too
# large loses the turn.
UNKNOWN_WINDOW = 32768


@dataclass(frozen=True)
class Fit:
    """One sizing decision, with everything a log line or a note needs."""

    whole: bool
    tokens: int
    window: int
    used: int
    how: str  # "exact" (server tokenizer) | "estimated" (chars x 13/40)
    window_reported: bool

    @property
    def share_limit(self) -> int:
        return int(self.window * SHARE)

    @property
    def free(self) -> int:
        return max(0, self.window - self.used)

    @property
    def reason(self) -> str:
        if self.whole:
            return "fits"
        if self.tokens > self.share_limit:
            return "over the share"
        return "over the free context"

    def describe(self) -> str:
        return (
            f"{self.tokens} tok ({self.how}) vs share {self.share_limit} and free "
            f"{self.free} of a {self.window} window "
            f"({'reported' if self.window_reported else 'assumed'}): {self.reason}"
        )


def decide(tokens: int, *, window: int, used: int) -> bool:
    """The rule itself: within the share AND within what is free."""
    return tokens <= int(window * SHARE) and tokens <= window - used


# The window LLMVP last reported to any fit in this process — for the
# synchronous callers (prompt formatters) that have no effects handle and
# cannot ask. It changes only when the served model does.
_last_window: int | None = None


async def serving_window(effects: Any) -> tuple[int, bool]:
    """(window, reported). The per-sequence window LLMVP reports, else the
    smallest window any configured text model serves."""
    global _last_window
    health = getattr(effects, "cache_health", None)
    if health is not None:
        try:
            n = int((await health()).get("nCtxSeq") or 0)
        except Exception:  # noqa: BLE001 — sizing never fails a step
            n = 0
        if n > 0:
            _last_window = n
            return n, True
    return UNKNOWN_WINDOW, False


def known_window() -> int:
    """The window last reported in this process, else the assumed one."""
    return _last_window or UNKNOWN_WINDOW


def share_chars(window: int | None = None) -> int:
    """One read's share of the window in CHARS (tokens x 40/13) — for a
    synchronous caller that cannot measure. It applies the share half of
    the rule; the free-context half is the step's ``fit`` map, which
    measures the rendered prompt."""
    return int(SHARE * (window or known_window()) * 40 / 13)


def estimate_tokens(text: str) -> int:
    """The fitted fallback when the server will not count: chars x 13/40."""
    return (len(text or "") * 13) // 40


async def measure(effects: Any, texts: list[str]) -> tuple[list[int], str]:
    """Token counts from the serving model's own tokenizer (with the
    chat-template margin), or the estimate when the server will not say."""
    from agent.scheduler.capacity_model import TOKENIZE_MARGIN

    counter = getattr(effects, "token_count", None)
    counts: list[int] = []
    if counter is not None:
        try:
            counts = await counter(list(texts))
        except Exception:  # noqa: BLE001
            counts = []
    if len(counts) == len(texts):
        return [int(c * TOKENIZE_MARGIN) for c in counts], "exact"
    return [estimate_tokens(t) for t in texts], "estimated"


async def session_used(effects: Any, session_id: str) -> int:
    """What a memoryful session already holds (prompt + generated after its
    last turn), or 0 when the effects cannot say."""
    getter = getattr(effects, "session_tokens", None)
    if getter is None or not session_id:
        return 0
    try:
        return int(getter(session_id) or 0)
    except Exception:  # noqa: BLE001
        return 0


async def fit(
    effects: Any,
    content: str,
    *,
    used: int = 0,
    reserve: int = OUTPUT_RESERVE,
    window: int | None = None,
) -> Fit:
    """Size ``content`` for a turn whose context already holds ``used``
    tokens; ``reserve`` is added to ``used`` for the answer — at most a
    quarter of the window, so with the share a read and its answer never
    take more than half of it (a fixed 8k reserve would leave an 8k window
    nothing)."""
    reported = True
    if window is None:
        window, reported = await serving_window(effects)
    (tokens,), how = await measure(effects, [content or ""])
    total_used = max(0, int(used)) + min(max(0, int(reserve)), int(window * SHARE))
    return Fit(
        whole=decide(tokens, window=window, used=total_used),
        tokens=tokens,
        window=window,
        used=total_used,
        how=how,
        window_reported=reported,
    )


# ── Indexes: what a model drills into when the whole does not fit ─────


def code_index(path: str, content: str) -> str:
    """A code file's symbol outline (signatures, docstrings) — the model then
    reads one symbol by name. Empty when no symbols can be extracted."""
    try:
        from agent.actions.ast_actions import _build_symbol_table
        from agent.formatters import _format_file_outline

        table = _build_symbol_table(path, content)
        return _format_file_outline({"symbol_table": table, "target_file": path}, {})
    except Exception:  # noqa: BLE001 — an index is a fallback, never a failure
        logger.debug("code index failed for %s", path, exc_info=True)
        return ""


def text_index(content: str, *, lines_per_range: int = 200) -> str:
    """A plain text's line ranges (and markdown headings when it has them) —
    the model then reads a range. Used for output and files without symbols."""
    lines = (content or "").splitlines()
    n = len(lines)
    out = [f"{n} lines."]
    heads = [
        (i + 1, ln.strip())
        for i, ln in enumerate(lines)
        if ln.lstrip().startswith("#") and ln.strip("# ").strip()
    ]
    if heads and len(heads) < n:
        out.append("Headings (line: text):")
        out.extend(f"  {i}: {h}" for i, h in heads)
    out.append("Line ranges:")
    for start in range(1, n + 1, lines_per_range):
        end = min(n, start + lines_per_range - 1)
        first = lines[start - 1].strip()
        out.append(f"  {start}-{end}  starts: {first}")
    return "\n".join(out)


def read_lines(content: str, start: int, end: int) -> str:
    """Lines ``start``..``end`` (1-based, inclusive), each numbered."""
    lines = (content or "").splitlines()
    start = max(1, int(start))
    end = min(len(lines), int(end))
    return "\n".join(f"{i}: {lines[i - 1]}" for i in range(start, end + 1))


_LABEL_KEYS = ("id", "name", "title", "key", "label")


def _label(value: Any) -> str:
    if isinstance(value, dict):
        return " ".join(
            f"{k}={value[k]!r}"
            for k in _LABEL_KEYS
            if k in value and isinstance(value[k], (str, int, float))
        )
    return ""


def _describe(value: Any) -> str:
    if isinstance(value, dict):
        return f"map, {len(value)} keys"
    if isinstance(value, list):
        return f"list, {len(value)} entries"
    return repr(value)


def data_index(path: str, content: str, pointer: str = "") -> str:
    """The addressable children of the data at ``pointer`` (RFC 6901, "" = the
    whole file) in any supported format (JSON, YAML, TOML): each child's
    pointer, its shape, and its id/name when it has one — the model then reads
    one child by pointer. Entry ids are kept (dropping a room id once caused
    hallucinated "missing id" rewrites)."""
    from agent.data_ops import Document, detect_fmt, resolve_pointer

    try:
        doc = Document.read(content, detect_fmt(path, content))
    except Exception as e:  # noqa: BLE001
        return f"({path} does not parse: {e})"
    node = doc.root
    if pointer:
        try:
            parent, key, exists = resolve_pointer(doc.root, pointer)
        except Exception as e:  # noqa: BLE001
            return f"(no data at {pointer}: {e})"
        if parent is None:
            node = doc.root
        elif not exists:
            return f"(no data at {pointer})"
        else:
            node = parent[key]
    items: list[tuple[str, Any]]
    if isinstance(node, dict):
        items = [(str(k), v) for k, v in node.items()]
    elif isinstance(node, list):
        items = [(str(i), v) for i, v in enumerate(node)]
    else:
        return f"{pointer or '/'} = {node!r}"
    base = pointer.rstrip("/")
    out = [f"{path}{(':' + pointer) if pointer else ''} — {_describe(node)}:"]
    for key, value in items:
        esc = key.replace("~", "~0").replace("/", "~1")
        ptr = f"{base}/{esc}"
        label = _label(value)
        shape = _describe(value)
        if not isinstance(value, (dict, list)):
            out.append(f"  {ptr} = {shape}")
        else:
            out.append(f"  {ptr}  ({shape}){('  ' + label) if label else ''}")
    return "\n".join(out)


def read_data(path: str, content: str, pointer: str) -> tuple[str, bool]:
    """(text, found): the subtree at ``pointer`` re-serialized in the file's
    own format, or — when the pointer does not resolve — the index of its
    deepest existing ancestor, so a wrong guess is also a map."""
    from agent.data_ops import Document, detect_fmt

    try:
        doc = Document.read(content, detect_fmt(path, content))
    except Exception as e:  # noqa: BLE001
        return f"({path} does not parse: {e})", False
    sl = doc.slice(pointer)
    if sl.value is not None or (pointer in ("", "/") and doc.root is not None):
        return (sl.text if pointer not in ("", "/") else content), True
    parts = [p for p in pointer.split("/") if p != ""]
    while parts:
        parts.pop()
        anc = "/" + "/".join(parts) if parts else ""
        if anc == "" or doc.slice(anc).value is not None:
            return (
                f"(no data at {pointer} — the nearest existing level is below)\n"
                + data_index(path, content, anc)
            ), False
    return data_index(path, content, ""), False


def index_for(path: str, content: str) -> str:
    """The right index for a file: data pointers for data files, a symbol
    outline for code, else line ranges."""
    from agent import languages

    ext = path.rsplit(".", 1)[-1].lower() if "." in path else ""
    if languages.is_data(ext):
        return data_index(path, content, "")
    outline = code_index(path, content)
    return outline or text_index(content)


# ── Reading one part of a file, and sizing tool output ─────────────────


def read_part(path: str, content: str, target: str) -> tuple[str, bool]:
    """(text, found) for one part of a file: ``A-B`` (lines, 1-based,
    inclusive), ``/pointer`` (a data entry; the slash may be omitted for a
    data file), else a code symbol (``Class.method``). When the part is not
    there, the text is the file's index — a wrong guess is also a map."""
    import re

    from agent import languages

    t = (target or "").strip()
    m = re.fullmatch(r"(\d+)\s*-\s*(\d+)", t)
    if m:
        a, b = int(m.group(1)), int(m.group(2))
        n = len((content or "").splitlines())
        if 1 <= a <= b and a <= n:
            return read_lines(content, a, b), True
        return (
            f"(no lines {t} in {path}: it has {n} lines)\n" + text_index(content),
            False,
        )
    ext = path.rsplit(".", 1)[-1].lower() if "." in path else ""
    if t.startswith("/") or languages.is_data(ext):
        return read_data(path, content, t if t.startswith("/") else "/" + t)
    try:
        from agent.actions.ast_actions import _build_symbol_table
        from agent.actions.trace_actions import _find_symbol

        sym = _find_symbol(t, _build_symbol_table(path, content))
    except Exception:  # noqa: BLE001
        sym = None
    if sym and sym.get("body"):
        return str(sym["body"]), True
    return (
        f"(no symbol {t!r} in {path} — its index:)\n" + index_for(path, content),
        False,
    )


_PART_FORMS = (
    "`{p}:<Symbol>` (code), `{p}:/pointer` (data) or `{p}:<first>-<last>` (lines)"
)


async def read_file_view(effects: Any, ref: str, *, used: int) -> tuple[str, str]:
    """(observation, error) for a tool's ``read_file`` of ``path`` or one part
    of it (``path:<part>``, see read_part). The file or part WHOLE when it
    fits the session; otherwise the file's index and how to read one part."""
    path, _, target = (ref or "").partition(":")
    path, target = path.strip(), target.strip()
    if not path:
        return "", "read_file needs a path argument."
    try:
        fc = await effects.read_file(path)
    except Exception as e:  # noqa: BLE001
        return "", f"could not read {path}: {e}"
    if not getattr(fc, "exists", False):
        return "", f"{path} does not exist."
    content = getattr(fc, "content", "") or ""
    label = f"{path}:{target}" if target else path
    text = content
    if target:
        text, found = read_part(path, content, target)
        if not found:
            return "", text
    f = await fit(effects, text, used=used)
    if f.whole:
        return f"Observation (read {label}):\n```\n{text}\n```", ""
    return (
        f"Observation (read {label} — too large to show whole here: "
        f"{f.describe()}). Read one part with read_file "
        f"{_PART_FORMS.format(p=path)}. Its index:\n{index_for(path, content)}"
    ), ""


async def output_view(
    effects: Any,
    text: str,
    *,
    used: int,
    save_path: str,
    label: str,
    how: str = "read_file",
) -> str:
    """Tool output or evidence WHOLE when it fits; otherwise saved in full at
    ``save_path`` and shown as its line index, each range readable with
    ``<how> <save_path>:<first>-<last>`` (``how`` names the reader's verb:
    ``read_file`` for a tool loop, ``trace`` in a diagnosis). Replaces fixed
    head cuts that kept the first 4,000 chars and lost the error at the end."""
    text = text or ""
    f = await fit(effects, text, used=used)
    if f.whole:
        return text
    saved = False
    writer = getattr(effects, "write_file", None)
    if writer is not None:
        try:
            await writer(save_path, text)
            saved = True
        except Exception:  # noqa: BLE001 — the index still stands without it
            logger.debug("could not save %s to %s", label, save_path, exc_info=True)
    where = (
        f" The full text is saved: read a range with {how} "
        f"`{save_path}:<first>-<last>`."
        if saved
        else ""
    )
    return (
        f"({label} is too large to show whole here: {f.describe()}.{where})\n"
        + text_index(text)
    )


def name_list(paths: list[str], budget_chars: int) -> str:
    """Every path by name when the names fit ``budget_chars``; otherwise a
    directory rollup ("data/raw/ (98,000 files)"), coarser until it fits —
    so a 100k-file tree is still accounted for, never dropped and never a
    wall of names in the prompt."""
    joined = ", ".join(paths)
    if len(joined) <= budget_chars:
        return joined
    rolled = ""
    for depth in (2, 1):
        counts: dict[str, int] = {}
        for p in paths:
            parts = str(p).split("/")
            keep = min(depth, len(parts) - 1)
            key = "/".join(parts[:keep]) + "/" if keep else "./"
            counts[key] = counts.get(key, 0) + 1
        rolled = ", ".join(
            f"{d} ({n:,} file{'s' if n != 1 else ''})"
            for d, n in sorted(counts.items())
        )
        if len(rolled) <= budget_chars:
            break
    return "by directory: " + rolled


# ── The tail fit: evidence for a one-shot reader ───────────────────────

FIT_MARKER = (
    "[… the first {omitted:,} characters of this text are not shown — "
    "it was fitted to the model's {window:,}-token window; the most recent "
    "part follows …]"
)


def tail_on_a_line(text: str, keep_chars: int) -> str:
    """The last ``keep_chars`` of ``text``, starting at a line boundary."""
    if keep_chars <= 0:
        return ""
    if keep_chars >= len(text):
        return text
    tail = text[-keep_chars:]
    nl = tail.find("\n")
    return tail[nl + 1 :] if 0 <= nl < len(tail) - 1 else tail


async def tail_view(
    effects: Any,
    content: str,
    *,
    used: int,
    reserve: int = OUTPUT_RESERVE,
    window: int | None = None,
    label: str = "text",
    step_name: str = "",
    window_how: str = "",
) -> str:
    """Evidence for a reader that cannot read more (a one-shot judge, a
    localizer): the content itself when it fits beside ``used`` tokens of
    prompt and the output reserve — not held to the per-read share, it IS
    the evidence — else its most recent part from a line boundary under a
    marker saying how much is not shown. Returns ``content`` (the same
    object) when whole, so a caller can tell."""
    if window is None:
        window, reported = await serving_window(effects)
        window_how = window_how or ("reported" if reported else "assumed")
    (content_tok,), how = await measure(effects, [content])
    marker_tok = (len(FIT_MARKER) * 13) // 40 + 16
    budget = window - used - reserve
    if content_tok <= budget:
        logger.info(
            "fit %s: %s whole — %d tok beside a %d-tok prompt and %d "
            "reserve in a %d window (%s, %s counts)",
            step_name,
            label,
            content_tok,
            used,
            reserve,
            window,
            window_how,
            how,
        )
        return content
    budget -= marker_tok
    kept = content
    for _ in range(3):
        keep_chars = int(len(kept) * max(budget, 0) / max(content_tok, 1))
        kept = tail_on_a_line(kept, keep_chars)
        if not kept:
            break
        (content_tok,), how = await measure(effects, [kept])
        if content_tok <= budget:
            break
    marker = FIT_MARKER.format(omitted=len(content) - len(kept), window=window)
    (logger.info if kept else logger.warning)(
        "fit %s: kept the last %d of %d chars of %s — a %d-tok prompt and "
        "%d reserve in a %d window (%s, %s counts)",
        step_name,
        len(kept),
        len(content),
        label,
        used,
        reserve,
        window,
        window_how,
        how,
    )
    return f"{marker}\n{kept}" if kept else marker


def data_overview(path: str, content: str) -> str:
    """Two levels of a data file's index: the top level, then the members of
    each top-level collection (each with its pointer and id/name) — enough to
    name any entry without reading the file whole."""
    from agent.data_ops import Document, detect_fmt

    out = [data_index(path, content, "")]
    try:
        doc = Document.read(content, detect_fmt(path, content))
    except Exception:  # noqa: BLE001 — data_index already said it does not parse
        return out[0]
    root = doc.root
    if isinstance(root, dict):
        for key, value in root.items():
            if isinstance(value, (dict, list)) and value:
                esc = str(key).replace("~", "~0").replace("/", "~1")
                out.append(data_index(path, content, f"/{esc}"))
    return "\n\n".join(out)
