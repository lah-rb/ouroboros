"""env.json command sanitization (2026-08-03, the title-match root cause).

The validation env is model-proposed and was persisted verbatim; a config
that named bare `python` on a python3-only macOS made every syntax/import
gate fail with FileNotFoundError — ten cycles of misdiagnosis, then an
environment-assert reified into parser.py's module frame. The sanitizer
resolves interpreters to sys.executable and drops checks whose tool does
not exist, at WRITE time.
"""

from __future__ import annotations

import sys
from unittest.mock import patch

from agent.actions.pipeline_actions import _sanitize_env_commands


def _which_macos_like(tok):
    """A PATH with python3 and uv but NO bare python and NO ruff."""
    return {"python3": "/usr/bin/python3", "uv": "/opt/uv"}.get(tok)


def test_bare_python_rewritten_to_sys_executable():
    cfg = {
        "py": {
            "syntax": [
                "python",
                "-c",
                "import py_compile; py_compile.compile('{file}', doraise=True)",
            ],
            "import": ["python3", "-c", "import {module}"],
        }
    }
    with patch("shutil.which", side_effect=_which_macos_like):
        notes = _sanitize_env_commands(cfg)
    assert cfg["py"]["syntax"][0] == sys.executable
    assert cfg["py"]["syntax"][1:] == [
        "-c",
        "import py_compile; py_compile.compile('{file}', doraise=True)",
    ]
    # python3 exists on this fake PATH — untouched
    assert cfg["py"]["import"][0] == "python3"
    assert any("rewrote to sys.executable" in n for n in notes)


def test_missing_tool_entry_dropped_loudly():
    cfg = {
        "py": {
            "lint": ["ruff", "check", "{file}"],
            "formatter": ["ruff", "format", "-q", "{file}"],
            "install_command": ["uv", "pip", "install", "-r", "pyproject.toml"],
        }
    }
    with patch("shutil.which", side_effect=_which_macos_like):
        notes = _sanitize_env_commands(cfg)
    assert "lint" not in cfg["py"]
    assert "formatter" not in cfg["py"]
    assert cfg["py"]["install_command"][0] == "uv"  # exists — kept
    assert sum("dropped the check entry" in n for n in notes) == 2


def test_non_command_values_untouched():
    cfg = {
        "interactive_prompt": "> ",
        "py": {"syntax": ["python3", "-m", "py_compile", "{file}"], "note": "hi"},
        "weird": ["not", "a", "block"],
    }
    with patch("shutil.which", side_effect=_which_macos_like):
        notes = _sanitize_env_commands(cfg)
    assert cfg["interactive_prompt"] == "> "
    assert cfg["py"]["note"] == "hi"
    assert cfg["weird"] == ["not", "a", "block"]
    assert notes == []


def test_all_present_is_a_noop():
    cfg = {
        "py": {
            "syntax": ["python", "-m", "py_compile", "{file}"],
            "lint": ["ruff", "check"],
        }
    }
    with patch("shutil.which", return_value="/bin/tool"):
        notes = _sanitize_env_commands(cfg)
    assert (
        cfg["py"]["syntax"][0] == "python"
    )  # exists on this fake PATH — kept verbatim
    assert "lint" in cfg["py"]
    assert notes == []
