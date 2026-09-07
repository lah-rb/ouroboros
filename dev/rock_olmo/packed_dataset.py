"""On-disk packed blocks for corpus v4 and the torch Dataset that reads them (rock venv).

FORMAT. A shard is `train-NNN.bin`: little-endian uint32 token ids, exactly
`seq` per block, plus `train-NNN.mask.bin`: one uint8 per token, 1 where the
token trains (labels = ids), 0 where it does not (labels = -100): padding in
both stages, and the PROMPT tokens in stage 2. `train-NNN.idx.json` maps every
block to the (doc_id, start, end) spans it was cut from, for provenance.
Validation sets are the same format under `val-<source>.bin`.

Reading is a memmap reshape; nothing is copied until a block is requested.
"""

from __future__ import annotations

import glob
import json
import os

import numpy as np

PAD_ID = 100277  # <|pad|>
EOS_ID = 100257  # <|endoftext|>
IGNORE = -100


class Shard:
    def __init__(self, path: str, seq: int):
        self.path = path
        self.seq = seq
        self.ids = np.memmap(path, dtype="<u4", mode="r").reshape(-1, seq)
        mpath = path[:-4] + ".mask.bin"
        self.mask = (
            np.memmap(mpath, dtype="u1", mode="r").reshape(-1, seq)
            if os.path.exists(mpath)
            else None
        )

    def __len__(self) -> int:
        return self.ids.shape[0]


def shards_for(directory: str, prefix: str, seq: int) -> list[Shard]:
    paths = sorted(
        p
        for p in glob.glob(os.path.join(directory, f"{prefix}*.bin"))
        if not p.endswith(".mask.bin")
    )
    return [Shard(p, seq) for p in paths]


def manifest(directory: str) -> dict:
    return json.load(open(os.path.join(directory, "manifest.json")))


try:  # torch is optional at import so the packager's tests can run without it
    import torch
    from torch.utils.data import Dataset

    class PackedDataset(Dataset):
        """Blocks from one or more shards as {input_ids, labels, attention_mask}."""

        def __init__(
            self, directory: str, prefix: str = "train", seq: int | None = None
        ):
            self.directory = directory
            m = manifest(directory)
            self.seq = seq or int(m["seq"])
            self.shards = shards_for(directory, prefix, self.seq)
            if not self.shards:
                raise FileNotFoundError(f"no {prefix}*.bin under {directory}")
            self._offsets = np.cumsum([0] + [len(s) for s in self.shards])

        def __len__(self) -> int:
            return int(self._offsets[-1])

        def __getitem__(self, i: int) -> dict:
            k = int(np.searchsorted(self._offsets, i, side="right") - 1)
            s = self.shards[k]
            row = i - int(self._offsets[k])
            ids = np.asarray(s.ids[row], dtype=np.int64)
            mask = (
                np.asarray(s.mask[row], dtype=np.int64)
                if s.mask is not None
                else (ids != PAD_ID).astype(np.int64)
            )
            labels = np.where(mask == 1, ids, IGNORE)
            return {
                "input_ids": torch.from_numpy(ids),
                "labels": torch.from_numpy(labels),
                "attention_mask": torch.from_numpy((ids != PAD_ID).astype(np.int64)),
            }

    def collate(batch: list[dict]) -> dict:
        return {k: torch.stack([b[k] for b in batch]) for k in batch[0]}

except ImportError:  # pragma: no cover
    PackedDataset = None  # type: ignore[assignment]
    collate = None  # type: ignore[assignment]
