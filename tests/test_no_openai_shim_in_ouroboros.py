"""Nothing in Ouroboros speaks LLMVP's OpenAI shim. Mechanised.

The shim (`app.openai_shim`, mounted at /v1) is OPTIONAL and exists for
external harnesses that only know OpenAI's REST shape (Terminus, Harbor,
tau-bench). Every Ouroboros consumer — the OCR tool, figtext, vl_inspect, the
digitizer, preocr triage — speaks GraphQL, and the corpus fleet runs with the
shim OFF so a retired route cannot be used silently. This test is the guard
that keeps it that way: a URL construction that targets a /v1/ route anywhere
in the agent, tools or dev trees fails the suite. Prose mentions (docstrings
explaining what was retired) are not URL constructions and pass.

llmvp/ is excluded on purpose: the shim's own implementation, tests and
benchmarks live there and are allowed to speak it. A PRIVATE server the tool
spawns itself (fig_review / vl_inspect's mlx_vlm.server fallback) speaks its
own OpenAI API and is not the shim; such a line is exempt only when it carries
the literal marker below — an allowlist you can grep, not a looser regex.
"""

from __future__ import annotations

import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCAN = ("agent", "tools", "dev", "adapters", "mcp_servers", "ouroboros.py")
SKIP_DIRS = {".venv", "venv", "__pycache__", "site-packages", "node_modules", "archive"}
#: The one sanctioned exemption, spelled exactly so it can be grepped.
ALLOW_MARKER = "not the LLMVP shim"

# A /v1/ route inside something that builds a URL: an http(s) literal, an
# f-string interpolation, or a `.rstrip('/')` join. Docstring prose that merely
# names the retired route does not match.
_URL_CONSTRUCTION = re.compile(
    r"""(https?://[^\s"']*?/v1/(vision|chat/completions|completions|models)\b"""
    r"""|\{[^}]+\}/v1/(vision|chat/completions|completions|models)\b"""
    r"""|rstrip\(['"]/['"]\)\}?/v1/)"""
)


def _py_files():
    for top in SCAN:
        path = os.path.join(ROOT, top)
        if os.path.isfile(path):
            yield path
            continue
        for dirpath, dirnames, filenames in os.walk(path):
            dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
            for fn in filenames:
                if fn.endswith(".py"):
                    yield os.path.join(dirpath, fn)


def test_no_ouroboros_code_builds_an_openai_shim_url():
    offenders = []
    for path in _py_files():
        with open(path, encoding="utf-8", errors="ignore") as fh:
            for lineno, line in enumerate(fh, 1):
                if ALLOW_MARKER in line:
                    continue
                if _URL_CONSTRUCTION.search(line):
                    offenders.append(
                        f"{os.path.relpath(path, ROOT)}:{lineno}: {line.strip()}"
                    )
    assert not offenders, "OpenAI-shim URL(s) built in Ouroboros code:\n" + "\n".join(
        offenders
    )


def test_the_guard_recognises_a_shim_url_when_it_sees_one():
    """A guard that matches nothing proves nothing."""
    assert _URL_CONSTRUCTION.search('f"{url}/v1/vision"')
    assert _URL_CONSTRUCTION.search('URL = "http://127.0.0.1:8008/v1/vision"')
    assert _URL_CONSTRUCTION.search("f\"{_LLMVP_URL.rstrip('/')}/v1/\"")
    assert not _URL_CONSTRUCTION.search("# the preflight used to ask /v1/models")


def test_the_exemption_is_only_for_marked_private_server_lines():
    """The marker exists for spawned servers; count them so a new one is a
    deliberate addition, not drift."""
    marked = []
    for path in _py_files():
        with open(path, encoding="utf-8", errors="ignore") as fh:
            for line in fh:
                if ALLOW_MARKER in line and _URL_CONSTRUCTION.search(line):
                    marked.append(os.path.relpath(path, ROOT))
    assert sorted(marked) == [
        "tools/fig_review/fig_review.py",
        "tools/fig_review/vl_inspect.py",
    ], marked
