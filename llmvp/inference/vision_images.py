"""Image intake for the vision endpoint: base64 data URI or local path.

TWO TRANSPORTS, ON PURPose. A base64 data URI is the OpenAI shape and survives
a caller on another machine (which matters the day llmvp moves to the CUDA
rig). A local path costs nothing for the case we actually have today — figures
already sitting in `databank/figures/` — where base64 would mean copying a
1.5MB PNG into ~2MB of JSON per request.

THE PATH BRANCH IS A SECURITY BOUNDARY, NOT A CONVENIENCE. An endpoint that
reads an arbitrary caller-supplied path is an arbitrary file read for anything
that can reach the port. So paths are refused unless `model.vision_image_roots`
lists a directory, and the check is done on the FULLY RESOLVED path so that
`..` traversal and symlinks that escape the root are both rejected — checking
the string before resolving would catch neither.
"""

from __future__ import annotations

import base64
import binascii
import os
from pathlib import Path


class ImageIntakeError(ValueError):
    """Rejected image — message is safe to return to the caller."""


_DATA_URI_PREFIX = "data:"


def _decode_data_uri(url: str) -> bytes:
    if "," not in url:
        raise ImageIntakeError("malformed data URI: no comma separator")
    header, _, payload = url.partition(",")
    if "base64" not in header:
        raise ImageIntakeError("only base64 data URIs are supported")
    try:
        return base64.b64decode(payload, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ImageIntakeError(f"invalid base64 payload: {exc}") from exc


def _read_allowed_path(raw: str, roots: list[str]) -> bytes:
    if not roots:
        raise ImageIntakeError(
            "image paths are disabled: set model.vision_image_roots to allow them"
        )
    try:
        # strict=False so a missing file yields our error, not an OSError.
        target = Path(raw).expanduser().resolve(strict=False)
    except (OSError, RuntimeError) as exc:  # RuntimeError = symlink loop
        raise ImageIntakeError(f"cannot resolve path: {exc}") from exc

    for root in roots:
        try:
            root_p = Path(root).expanduser().resolve(strict=False)
        except (OSError, RuntimeError):
            continue
        # is_relative_to on RESOLVED paths — this is what rejects both `..`
        # traversal and a symlink pointing outside the root.
        if target == root_p or target.is_relative_to(root_p):
            break
    else:
        raise ImageIntakeError("path is outside every configured image root")

    if not target.is_file():
        raise ImageIntakeError("path is not a file")
    return target.read_bytes()


def resolve_image_part(part: dict, roots: list[str], max_bytes: int) -> bytes:
    """One content part → image bytes.

    Accepts the OpenAI shape::

        {"type": "image_url", "image_url": {"url": "data:image/png;base64,..."}}

    and a local-path shape::

        {"type": "image_path", "path": "/abs/or/~/path.png"}

    An `image_url` whose url is NOT a data URI is refused rather than fetched:
    making the server fetch caller-supplied URLs is an SSRF primitive, and
    nothing in Ouroboros needs it.
    """
    kind = (part or {}).get("type") or ""
    if kind == "image_path":
        data = _read_allowed_path(str(part.get("path") or ""), roots)
    elif kind == "image_url":
        url = str(((part or {}).get("image_url") or {}).get("url") or "")
        if not url:
            raise ImageIntakeError("image_url part has no url")
        if url.startswith(_DATA_URI_PREFIX):
            data = _decode_data_uri(url)
        elif os.path.isabs(url) or url.startswith("~"):
            # Tolerate a bare path in the url field — a natural mistake, and
            # it still goes through the allowlist.
            data = _read_allowed_path(url, roots)
        else:
            raise ImageIntakeError(
                "remote image URLs are not fetched; pass a data URI or a path "
                "under model.vision_image_roots"
            )
    else:
        raise ImageIntakeError(f"unsupported image part type: {kind!r}")

    if not data:
        raise ImageIntakeError("image is empty")
    if len(data) > max_bytes:
        raise ImageIntakeError(
            f"image is {len(data)} bytes, over the "
            f"{max_bytes}-byte model.vision_max_image_bytes limit"
        )
    return data


def to_data_uri(data: bytes, mime: str = "image/png") -> str:
    """Bytes → data URI, the shape MTMDChatHandler consumes."""
    return f"data:{mime};base64,{base64.b64encode(data).decode()}"
