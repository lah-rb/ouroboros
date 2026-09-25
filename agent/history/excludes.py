"""What the history store never looks at inside a workspace.

One list, shared by the workspace snapshotter and ``LocalEffects.list_directory``
so a directory the agent cannot see is also a directory the history never
versions. Virtual environments, caches, build output, version control and the
agent's own state are never project source, and they run to thousands of
files. The store's own ``.agent/history`` sits inside ``.agent``.
"""

from __future__ import annotations

import fnmatch

# Directory NAMES pruned wherever they appear in the tree. Entries may be
# fnmatch patterns (``*.egg-info`` — the old literal entry in
# LocalEffects._EXCLUDE_DIRS could never match anything).
EXCLUDED_DIR_PATTERNS: tuple[str, ...] = (
    ".venv",
    "venv",
    "env",
    ".env",
    "__pycache__",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".hypothesis",
    ".ipynb_checkpoints",
    "node_modules",
    ".git",
    ".agent",
    "dist",
    "build",
    ".eggs",
    "*.egg-info",
    ".tox",
    ".nox",
)

# File NAMES the snapshotter skips: bytecode, editor litter, and files whose
# whole purpose is to hold secrets — a prompt may quote them, the tree must
# not archive them.
EXCLUDED_FILE_PATTERNS: tuple[str, ...] = (
    "*.pyc",
    "*.pyo",
    ".DS_Store",
    "*.swp",
    "*.pem",
    "*.key",
    ".env",
    ".env.*",
)

# Files recorded as a STUB (size + sha256) rather than by content, whatever
# their size: downloaded papers, images, archives, model weights, databases.
# The tree still says they were there and what they hashed to; a rollback
# cannot recreate them and says so. Versioning a scraper's PDF databank as
# blobs would duplicate the whole databank inside repo.git.
STUB_FILE_PATTERNS: tuple[str, ...] = (
    "*.pdf",
    "*.png",
    "*.jpg",
    "*.jpeg",
    "*.gif",
    "*.webp",
    "*.zip",
    "*.gz",
    "*.tgz",
    "*.tar",
    "*.7z",
    "*.parquet",
    "*.sqlite",
    "*.sqlite3",
    "*.db",
    "*.bin",
    "*.pt",
    "*.pth",
    "*.safetensors",
    "*.gguf",
    "*.onnx",
    "*.whl",
    "*.so",
    "*.dylib",
    "*.mp3",
    "*.wav",
    "*.mp4",
    "*.mov",
)

# The one part of ``.agent`` the tree DOES carry: the mission state, so a
# rollback restores the goals and notes as they stood at that turn.
AGENT_STATE_FILES: tuple[str, ...] = (".agent/mission.json", ".agent/env.json")


def is_excluded_dir(name: str) -> bool:
    return any(fnmatch.fnmatchcase(name, pat) for pat in EXCLUDED_DIR_PATTERNS)


def is_excluded_file(name: str) -> bool:
    return any(fnmatch.fnmatchcase(name, pat) for pat in EXCLUDED_FILE_PATTERNS)


def is_stub_file(name: str) -> bool:
    return any(fnmatch.fnmatchcase(name, pat) for pat in STUB_FILE_PATTERNS)
