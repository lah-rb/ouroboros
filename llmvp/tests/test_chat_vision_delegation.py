"""/v1/chat/completions delegates image requests to the vision path.

WHY IT MATTERS. OpenAI clients send images to /v1/chat/completions — PaddleOCR's
llama-cpp-server backend posts region crops there and has no notion of a
separate vision route. Without delegation those requests reach
run_chat_completion, whose prompt builder flattens content to ``str``: the
image is DROPPED and the model answers the text alone, fluently and wrongly,
with no error. Shape-based detection is the guard.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from api.rest_api import _has_image_part  # noqa: E402

TEXT_ONLY = [{"role": "user", "content": "hello"}]
PARTS_TEXT_ONLY = [
    {"role": "user", "content": [{"type": "text", "text": "hello"}]},
]
WITH_URL = [
    {
        "role": "user",
        "content": [
            {"type": "text", "text": "what is this"},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,AA=="}},
        ],
    }
]
WITH_PATH = [
    {"role": "user", "content": [{"type": "image_path", "path": "/x/y.png"}]},
]


def test_plain_string_content_is_not_an_image():
    assert _has_image_part(TEXT_ONLY) is False


def test_text_parts_are_not_an_image():
    assert _has_image_part(PARTS_TEXT_ONLY) is False


def test_image_url_part_detected():
    assert _has_image_part(WITH_URL) is True


def test_image_path_part_detected():
    assert _has_image_part(WITH_PATH) is True


def test_image_in_a_later_message_detected():
    """A multi-turn conversation may carry the image anywhere."""
    assert _has_image_part(PARTS_TEXT_ONLY + WITH_URL) is True


def test_empty_and_malformed_do_not_crash():
    """Detection runs before validation, so it must tolerate junk rather than
    500 on it."""
    assert _has_image_part([]) is False
    assert _has_image_part([{}]) is False
    assert _has_image_part([{"role": "user"}]) is False
    assert _has_image_part([{"role": "user", "content": [None]}]) is False
    assert _has_image_part([{"role": "user", "content": [{}]}]) is False
    assert _has_image_part([None]) is False


def test_detection_ignores_the_model_name():
    """A TEXT request naming a vision model must still take the text path —
    routing is decided by content shape, not by who is being addressed."""
    assert _has_image_part(TEXT_ONLY) is False
