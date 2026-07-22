"""Long-cycle detection + partial-generation capture.

The RepetitionGuard's horizon is 8-token cycles; the live qwen menu
runaway looped at paragraph scale (130k tokens, 43 watchdog cancels,
text discarded). detect_long_cycle measures the distinct-chunk ratio of
the text tail; dump_capture preserves the evidence.
"""

from __future__ import annotations

import json
from pathlib import Path

from inference.runaway_capture import (
    CHECK_INTERVAL,
    WINDOW_BYTES,
    detect_long_cycle,
    dump_capture,
)


def test_short_output_never_trips():
    assert detect_long_cycle(b'{"choice": "engine.py"}') is None


def test_paragraph_loop_trips():
    # The live failure shape: a short answer re-emitted forever.
    lap = b'```json\n{"choice": "engine.py"}\n```\nThe best file to fix is:\n'
    text = lap * (WINDOW_BYTES // len(lap) + 10)
    reason = detect_long_cycle(text)
    assert reason is not None
    assert "long-cycle repetition" in reason


def test_paragraph_orbit_trips_second_tier():
    # The July 22 conclude-turn failure shape (qwen3-next, live specimen):
    # an ~8-paragraph / ~3.2KB deliberation cycle repeated ~47x near-
    # verbatim. Period sits far above the 8KB tier's ~1KB horizon (its
    # ratio plateaued at 0.29); the 32KB tier catches it.
    # Eight mutually distinct paragraphs (the real loop had 108 unique
    # paragraphs — plenty of within-cycle variety, so the 8KB window sees
    # a high distinct ratio) repeated as one long cycle.
    paras = [
        (
            "Paragraph %d analysis: " % i
            + " ".join(f"tok{i}_{j}" for j in range(48))
            + "\n\n"
        ).encode()
        for i in range(8)
    ]
    cycle = b"".join(paras)
    assert 2500 < len(cycle) < 4500  # paragraph-scale period, >1KB horizon
    text = cycle * (32768 // len(cycle) + 6)
    reason = detect_long_cycle(text)
    assert reason is not None
    assert "32768B" in reason  # caught by the second tier, not the 8KB one


def test_varied_prose_beyond_second_tier_does_not_trip():
    # Legitimate long varied output filling the 32KB window — every
    # block distinct content on a shared template (world-file shape).
    rooms = [
        (
            f"room_{i}:\n  name: Chamber {i}\n  description: A room numbered {i} "
            f"with its own distinct furnishings and lore.\n  exits:\n    north: room_{i + 1}\n"
        ).encode()
        for i in range(400)
    ]
    text = b"".join(rooms)
    assert len(text) > 32768
    assert detect_long_cycle(text) is None


def test_varied_code_does_not_trip():
    # Legitimate long output: every line distinct (simulates a real file).
    lines = [
        f"def handler_{i}(arg_{i}):\n    return process(arg_{i}, mode={i})\n".encode()
        for i in range(600)
    ]
    text = b"".join(lines)
    assert len(text) > WINDOW_BYTES
    assert detect_long_cycle(text) is None


def test_repetitive_but_varied_yaml_does_not_trip():
    # Structurally similar blocks with distinct content — the world-file
    # shape that must never be mistaken for degeneracy.
    rooms = [
        (
            f"room_{i}:\n  name: Chamber {i}\n  description: A room numbered {i} "
            f"with its own distinct furnishings and lore.\n  exits:\n    north: room_{i + 1}\n"
        ).encode()
        for i in range(200)
    ]
    text = b"".join(rooms)
    assert len(text) > WINDOW_BYTES
    assert detect_long_cycle(text) is None


def test_window_must_fill_before_detection():
    lap = b"same line repeated\n"
    text = lap * ((WINDOW_BYTES // 2) // len(lap))  # half a window
    assert detect_long_cycle(text) is None


def test_dump_capture_writes_record(tmp_path: Path):
    text = b"loop " * 5000
    path = dump_capture(
        tmp_path,
        "long-cycle repetition: test",
        text,
        tokens_generated=12345,
        meta={"temperature": 0.24},
    )
    assert path is not None
    rec = json.loads(Path(path).read_text())
    assert rec["tokens_generated"] == 12345
    assert rec["meta"]["temperature"] == 0.24
    assert rec["text"].endswith("loop ")
    assert rec["bytes_total"] == len(text)


def test_dump_capture_never_raises_on_bad_dir():
    assert (
        dump_capture("/dev/null/not-a-dir", "r", b"x", 1) is None
    )  # swallowed, returns None


def test_check_interval_sane():
    # The loop hooks fire on completion_tokens % CHECK_INTERVAL == 0;
    # guard against someone setting it to 0 and dividing by zero.
    assert CHECK_INTERVAL >= 256
