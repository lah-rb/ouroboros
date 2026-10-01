"""Wire format between extract_batch.py and layout_server.py (the remote layout model).

The PaddleOCR-VL pipeline calls its layout model with page images (numpy uint8 arrays)
and keyword options, and reads back LayoutAnalysisResult dicts whose boxes carry numpy
polygon points. Everything crosses the wire as JSON:

- images: zlib-compressed raw pixels with shape and dtype. Bit-exact, so the remote model
  sees the same pixels the local one would; no PNG or JPEG round trip.
- values: JSON plus tagged forms for what JSON loses. Dicts with non-string keys (layout
  thresholds are keyed by class id), tuples, numpy arrays (with dtype) and numpy scalars.

numpy only; no paddle, no OpenCV, so the agent's test venv can import it.
"""

from __future__ import annotations

import base64
import zlib

import numpy as np


def encode_image(arr: np.ndarray) -> dict:
    arr = np.ascontiguousarray(arr)
    return {
        "zraw": base64.b64encode(zlib.compress(arr.tobytes(), 1)).decode("ascii"),
        "shape": list(arr.shape),
        "dtype": str(arr.dtype),
    }


def decode_image(d: dict) -> np.ndarray:
    raw = zlib.decompress(base64.b64decode(d["zraw"]))
    return np.frombuffer(raw, dtype=np.dtype(d["dtype"])).reshape(d["shape"]).copy()


def encode_value(v):
    if isinstance(v, np.ndarray):
        return {"__nd__": v.tolist(), "dtype": str(v.dtype)}
    if isinstance(v, np.generic):
        return v.item()
    if isinstance(v, dict):
        if all(isinstance(k, str) for k in v):
            return {k: encode_value(x) for k, x in v.items()}
        return {"__dict__": [[encode_value(k), encode_value(x)] for k, x in v.items()]}
    if isinstance(v, tuple):
        return {"__tuple__": [encode_value(x) for x in v]}
    if isinstance(v, list):
        return [encode_value(x) for x in v]
    return v


def decode_value(v):
    if isinstance(v, dict):
        if "__nd__" in v:
            return np.asarray(v["__nd__"], dtype=np.dtype(v["dtype"]))
        if "__dict__" in v:
            return {decode_value(k): decode_value(x) for k, x in v["__dict__"]}
        if "__tuple__" in v:
            return tuple(decode_value(x) for x in v["__tuple__"])
        return {k: decode_value(x) for k, x in v.items()}
    if isinstance(v, list):
        return [decode_value(x) for x in v]
    return v
