"""Shared filesystem anchors for the agent package.

One canonical repo-root resolver — previously re-derived (identically) in
three action modules that launch repo-local sidecar tools.
"""

import os

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def repo_root() -> str:
    """Absolute path of the repository root (the dir containing agent/)."""
    return _REPO_ROOT
