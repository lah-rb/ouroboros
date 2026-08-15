"""A committed mission file must name a corpus without naming a machine.

`missions/*.yaml` used to carry `working_dir: "/Users/lah-rb/corpora/..."`.
That string is stamped into mission.json at creation and handed to every
action that builds an absolute path for a subprocess, so carrying the corpus
to a second machine pointed the OCR lane at a directory that did not exist
there — 99 papers booked `extract_failed` by a toolchain that never opened
one of them.

The tilde is the fix, and it only works if something expands it:
`os.path.realpath` does not. An interactive shell expands
`--working-dir ~/corpora/x` before argv ever sees it, which is exactly why
this is easy to get wrong — the CLI path looks fine while the YAML path,
quoted and never shell-touched, silently resolves to a directory named `~`
inside the repo.
"""

from __future__ import annotations

import os

from ouroboros import _resolve_working_dir


def test_a_tilde_from_yaml_resolves_to_the_home_directory():
    resolved = _resolve_working_dir("~/corpora/ouroboros-spectra")
    assert resolved == os.path.realpath(
        os.path.join(os.path.expanduser("~"), "corpora/ouroboros-spectra")
    )
    # The failure this guards against is a literal '~' directory under cwd.
    assert "~" not in resolved


def test_an_absolute_path_is_unchanged(tmp_path):
    assert _resolve_working_dir(str(tmp_path)) == os.path.realpath(str(tmp_path))


def test_no_value_falls_back_to_cwd():
    assert _resolve_working_dir(None) == os.path.realpath(os.getcwd())


def test_every_committed_mission_names_a_corpus_without_a_machine():
    """The policy itself, not just the mechanism.

    A reviewer can add a mission YAML with an absolute home path and every
    other test still passes. This is the one that notices.
    """
    root = os.path.join(os.path.dirname(os.path.dirname(__file__)), "missions")
    offenders = []
    for name in sorted(os.listdir(root)):
        if not name.endswith((".yaml", ".yml")):
            continue
        text = open(os.path.join(root, name), encoding="utf-8").read()
        for lineno, line in enumerate(text.splitlines(), 1):
            if "/Users/" in line or "/home/" in line:
                offenders.append(f"{name}:{lineno}: {line.strip()}")
    assert (
        not offenders
    ), "absolute home paths in committed mission files:\n" + "\n".join(offenders)
