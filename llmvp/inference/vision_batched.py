"""Batched vision: multimodal prefixes installed into the shared text engine.

The dedicated vision path holds one private single-seq context per request
and pays a ~1.2 GiB compute buffer per context — a second context does not
fit a 24 GB card (three ggml aborts, 2026-08-26), so vision throughput was
pinned to one stream. This module is the other half of the answer the
batched TEXT engine already gave: many sequences, one context, shared
weight reads.

The pipeline (probe-verified 2026-08-26, probe_vision_kv_integrity.py —
neighbour seqs stay byte-identical through an install):

  render (family renderer, NOT the GGUF Jinja the handler uses)
    -> mtmd_tokenize (direct; add_special keyed to THE SEAT, never the
       fork's context-emptiness heuristic, and never through
       _process_mtmd_prompt whose hybrid path clears the whole context)
    -> [text1 | image | text2] split
    -> text1 + image installed PRE-admission via engine control ops
       (image: encode OFF the decode thread under a lock, embeddings
       copied out of mtmd scratch, then fed as ≤n_batch embd sub-batches
       — or one atomic mtmd_helper_decode_image_chunk for models that
       need non-causal attention or M-RoPE)
    -> text2 becomes the stream's ordinary prompt_tokens, so pressure
       rollback can never cross an image row.

Negative media pseudo-ids never leave this module; the engine's
MEDIA_SENTINEL is what lands in slot.input_ids for image rows.
"""

from __future__ import annotations

import ctypes
import logging
import os
import threading
from dataclasses import dataclass, field
from typing import Any, List, Optional

log = logging.getLogger(__name__)


class VisionInstallError(RuntimeError):
    """The batched install could not complete; the caller falls back to
    the dedicated vision-pool path for this request."""


@dataclass
class MediaChunk:
    """One image chunk from mtmd_tokenize, copied so it outlives the
    container; free() must be called exactly once."""

    ptr: Any
    n_tokens: int
    needs_atomic: bool
    _freed: bool = field(default=False, repr=False)

    def free(self) -> None:
        if not self._freed:
            from llama_cpp import mtmd_cpp as M

            M.mtmd_input_chunk_free(self.ptr)
            self._freed = True


@dataclass
class SplitPrompt:
    """The three-way split. text2 is the generation tail and is always
    non-empty for a well-formed prompt (it ends with the generation
    head); segments in `pre` alternate text token lists and MediaChunks
    in prompt order."""

    pre: List[Any]  # List[int] segments and MediaChunk entries
    text2: List[int]

    def free(self) -> None:
        for seg in self.pre:
            if isinstance(seg, MediaChunk):
                seg.free()


class MtmdEncoder:
    """Process-wide mtmd context + encode lock.

    The mtmd context binds the MODEL (not a llama_context), so one
    encoder serves every seat of the shared batched context. Encodes are
    serialized by `lock` (the helper APIs are not thread-safe against
    themselves) but run OFF the engine's decode thread — only the short
    embedding decode enters a control op."""

    def __init__(
        self,
        mmproj_path: str,
        n_threads: int = 4,
        projector_device: Optional[str] = None,
        use_gpu: bool = True,
    ) -> None:
        self.mmproj_path = mmproj_path
        self.n_threads = n_threads
        self.projector_device = projector_device
        self.use_gpu = use_gpu
        self.lock = threading.Lock()
        self._ctx: Any = None
        self._marker: Optional[str] = None
        self._init_lock = threading.Lock()

    def ensure(self, llama_model: Any) -> None:
        """Idempotent lazy init. `llama_model` is the fork's LlamaModel
        (weights owner). MTMD_BACKEND_DEVICE is consulted by clip.cpp at
        init via getenv, so the env dance here is per-init placement —
        the same convention the dedicated path uses."""
        if self._ctx is not None:
            return
        with self._init_lock:
            if self._ctx is not None:
                return
            from llama_cpp import mtmd_cpp as M

            params = M.mtmd_context_params_default()
            params.use_gpu = self.use_gpu
            params.print_timings = False
            params.n_threads = self.n_threads
            params.warmup = True
            prev = os.environ.get("MTMD_BACKEND_DEVICE")
            try:
                if self.projector_device and self.use_gpu:
                    os.environ["MTMD_BACKEND_DEVICE"] = str(self.projector_device)
                ctx = M.mtmd_init_from_file(
                    self.mmproj_path.encode(), llama_model.model, params
                )
            finally:
                if self.projector_device and self.use_gpu:
                    if prev is None:
                        os.environ.pop("MTMD_BACKEND_DEVICE", None)
                    else:
                        os.environ["MTMD_BACKEND_DEVICE"] = prev
            if not ctx:
                raise VisionInstallError(
                    f"mtmd_init_from_file failed for {self.mmproj_path}"
                )
            self._ctx = ctx
            self._marker = M.mtmd_default_marker().decode()
            log.info(
                "👁  batched-vision mtmd context ready (projector=%s)",
                self.projector_device or "default",
            )

    @property
    def marker(self) -> str:
        assert self._marker is not None, "ensure() first"
        return self._marker

    def split_prompt(
        self, prompt_text: str, images: List[bytes], add_special: bool
    ) -> SplitPrompt:
        """mtmd_tokenize the marker-bearing prompt against the images and
        return the three-way split. Chunks are copied out of the
        container; the caller owns SplitPrompt.free()."""
        from llama_cpp import mtmd_cpp as M

        assert self._ctx is not None, "ensure() first"
        bitmaps = []
        wrappers = []
        try:
            for data in images:
                buf = (ctypes.c_uint8 * len(data)).from_buffer_copy(data)
                w = M.mtmd_helper_bitmap_init_from_buf(self._ctx, buf, len(data), False)
                if not w.bitmap:
                    raise VisionInstallError("bitmap init failed (bad image?)")
                wrappers.append(w)
                bitmaps.append(w.bitmap)

            chunks = M.mtmd_input_chunks_init()
            it = M.mtmd_input_text()
            enc = prompt_text.encode("utf-8")
            it.text = ctypes.c_char_p(enc)
            it.text_len = len(enc)
            it.add_special = bool(add_special)
            it.parse_special = True
            arr = (M.mtmd_bitmap_p_ctypes * len(bitmaps))(*bitmaps) if bitmaps else None
            rc = M.mtmd_tokenize(self._ctx, chunks, ctypes.byref(it), arr, len(bitmaps))
            if rc != 0:
                raise VisionInstallError(f"mtmd_tokenize rc={rc}")

            segments: List[Any] = []
            n = int(M.mtmd_input_chunks_size(chunks))
            for i in range(n):
                ch = M.mtmd_input_chunks_get(chunks, i)
                ctype = int(M.mtmd_input_chunk_get_type(ch))
                if ctype == 0:  # text
                    cnt = ctypes.c_size_t(0)
                    p = M.mtmd_input_chunk_get_tokens_text(ch, ctypes.byref(cnt))
                    segments.append([int(p[k]) for k in range(int(cnt.value))])
                elif ctype == 1:  # image
                    needs_atomic = bool(
                        M.mtmd_decode_use_non_causal(self._ctx, ch)
                    ) or bool(M.mtmd_decode_use_mrope(self._ctx))
                    segments.append(
                        MediaChunk(
                            ptr=M.mtmd_input_chunk_copy(ch),
                            n_tokens=int(M.mtmd_input_chunk_get_n_tokens(ch)),
                            needs_atomic=needs_atomic,
                        )
                    )
                else:
                    raise VisionInstallError(f"unsupported chunk type {ctype}")
            M.mtmd_input_chunks_free(chunks)
        finally:
            # BITMAPS ARE CALLER-OWNED. tokenize copies what it needs into
            # the chunks; the raw decoded RGB (1-8 MB per figure) stays
            # ours to free — the fork's own handler frees right after
            # tokenize. The first version of this comment claimed mtmd
            # freed them "with the chunks": WRONG, and the resulting leak
            # grew the server's C heap to 46 GB over ~12,900 requests
            # (2026-08-28, characterized via smaps: [heap] 46.4 GB /
            # 776 segments, ~2 GB per 90 min at ~550 figs/h).
            for w in wrappers:
                try:
                    if w.bitmap:
                        M.mtmd_bitmap_free(w.bitmap)
                except Exception:  # noqa: BLE001 — never break intake
                    log.exception("bitmap free failed")

        if not segments:
            raise VisionInstallError("tokenize produced no chunks")
        # Split: everything through the LAST media chunk is the install
        # prefix; the trailing text segment is the stream's prompt.
        last_media = max(
            (i for i, s in enumerate(segments) if isinstance(s, MediaChunk)),
            default=-1,
        )
        if last_media < 0:
            raise VisionInstallError("no media chunk in prompt")
        pre = segments[: last_media + 1]
        tail_segs = segments[last_media + 1 :]
        text2: List[int] = []
        for seg in tail_segs:
            if isinstance(seg, MediaChunk):  # unreachable by construction
                seg.free()
                raise VisionInstallError("media after last media chunk")
            text2.extend(seg)
        if not text2:
            for seg in pre:
                if isinstance(seg, MediaChunk):
                    seg.free()
            raise VisionInstallError(
                "prompt has no text after the final image — the generation "
                "head must follow the media marker"
            )
        return SplitPrompt(pre=pre, text2=text2)

    def encode(self, chunk: MediaChunk, n_embd_inp: int) -> Any:
        """CLIP forward for one media chunk; returns a float32 numpy COPY
        (mtmd scratch is clobbered by the next encode)."""
        import numpy as np

        from llama_cpp import mtmd_cpp as M

        with self.lock:
            rc = M.mtmd_encode_chunk(self._ctx, chunk.ptr)
            if rc != 0:
                raise VisionInstallError(f"mtmd_encode_chunk rc={rc}")
            src = M.mtmd_get_output_embd(self._ctx)
            return np.ctypeslib.as_array(
                src, shape=(chunk.n_tokens * n_embd_inp,)
            ).copy()

    def decode_image_atomic(
        self,
        lctx: Any,
        chunk: MediaChunk,
        embd: Any,
        n_past: int,
        seq_id: int,
        n_batch: int,
    ) -> int:
        """The atomic helper path (non-causal / M-RoPE models): one call,
        internal batching, returns new_n_past. DECODE-THREAD ONLY."""
        from llama_cpp import mtmd_cpp as M

        newp = ctypes.c_int32(0)
        rc = M.mtmd_helper_decode_image_chunk(
            self._ctx,
            lctx.ctx,
            chunk.ptr,
            embd.ctypes.data_as(ctypes.POINTER(ctypes.c_float)),
            n_past,
            seq_id,
            n_batch,
            ctypes.byref(newp),
            ctypes.cast(None, M.mtmd_helper_post_decode_callback),
            None,
        )
        if rc != 0:
            raise VisionInstallError(f"decode_image_chunk rc={rc}")
        return int(newp.value)


class MtmdEncoderPool:
    """K independent mtmd contexts, LEASED one per request.

    WHY A POOL. An encode is a ViT+projector forward writing the mtmd
    context's own output scratch — one context runs one forward at a time,
    and with a single process-wide encoder that made encode the SERIAL
    stage of batched vision. Measured 2026-08-29 (dev/OCR_LANE doc): a
    paddle crop pays a constant 57-73 ms minimum-grid encode (~38% of the
    request), and the batched serving cell held quality but stayed
    throughput-FLAT against the pool because every stream queued on this
    one lock while decode multiplexed underneath. Each pool member costs a
    full projector upload (~1 GB for paddle's mmproj) — VRAM buys encoder
    concurrency, the same trade the old per-instance vision pool made, but
    here it buys ONLY the encode stage; KV stays in the one batched
    context. The preflight governor counts size × mmproj.

    THE LEASE COVERS tokenize + encode + install, one request at a time
    per member. A media chunk carries its preprocessing from tokenize, so
    creating and encoding it on the SAME context removes every
    cross-context question; the atomic install then reads the leased
    context's flags from the decode thread, which is the same overlap
    (install on ctx concurrent with another encode) production has always
    run. At size 1 this degrades to today's single-encoder behaviour with
    one improvement: split_prompt is now inside the lease, closing the
    previously-unserialized concurrent-tokenize window on a shared ctx.

    asyncio-native on purpose: leases are awaited in request coroutines
    (never the decode thread), so a queue.Queue would block the loop.
    """

    def __init__(
        self,
        mmproj_path: str,
        size: int = 1,
        n_threads: int = 4,
        projector_device: Optional[str] = None,
        use_gpu: bool = True,
    ) -> None:
        self.size = max(1, int(size or 1))
        self._encoders: List[MtmdEncoder] = [
            MtmdEncoder(
                mmproj_path=mmproj_path,
                n_threads=n_threads,
                projector_device=projector_device,
                use_gpu=use_gpu,
            )
            for _ in range(self.size)
        ]
        self._q: Any = None  # asyncio.Queue, built in ensure() (needs a loop)
        self._ensure_lock: Any = None

    @property
    def marker(self) -> str:
        return self._encoders[0].marker

    async def ensure(self, llama_model: Any) -> None:
        """Idempotent lazy init of every member (blocking C loads run in
        the default executor). ~0.5-1 s per member, paid once."""
        import asyncio

        if self._ensure_lock is None:
            self._ensure_lock = asyncio.Lock()
        async with self._ensure_lock:
            if self._q is not None:
                return
            loop = asyncio.get_running_loop()
            for enc in self._encoders:
                await loop.run_in_executor(None, enc.ensure, llama_model)
            q: Any = asyncio.Queue()
            for enc in self._encoders:
                q.put_nowait(enc)
            self._q = q
            log.info(
                "👁  batched-vision encoder pool ready: %d context(s)",
                self.size,
            )

    def lease(self):
        """Async context manager yielding one member for the request's
        tokenize+encode+install window. Released on every exit path — a
        leaked lease silently shrinks the pool until vision deadlocks,
        the same failure shape acquire_vision_instance guards against."""
        import contextlib

        assert self._q is not None, "ensure() first"

        @contextlib.asynccontextmanager
        async def _cm():
            enc = await self._q.get()
            try:
                yield enc
            finally:
                self._q.put_nowait(enc)

        return _cm()


def render_vision_prompt(
    family: str,
    system_text: str,
    user_text: str,
    marker: str,
    n_images: int,
    reasoning: Optional[str] = None,
) -> str:
    """Render through the repo's OWN family renderer — the same machinery
    as the text path — rather than the GGUF Jinja template the dedicated
    handler resolves. `user_text` carries `marker` once per image (the
    caller inserts them at part positions)."""
    from formats.registry import load_schema
    from formats.renderer import FormatRenderer

    if user_text.count(marker) != n_images:
        raise VisionInstallError(
            f"marker count {user_text.count(marker)} != images {n_images}"
        )
    schema = load_schema(family)
    r = FormatRenderer(schema)
    parts = []
    if system_text:
        parts.append(r.render_system(system_text))
    parts.append(r.render_user(user_text))
    gen = r.render_generation_prompt(reasoning)
    # FORCE THE CONTENT CHANNEL for channel-thinking families (muse):
    # the bare generation head lets the model open ` to=self` and spend
    # the whole budget reasoning — the first live batched request leaked
    # exactly that. The dedicated pool path never had the problem only
    # because the GGUF template it renders with closes the channel
    # itself. Reconstructed from the same schema fields vision_text's
    # _channel_heads uses, so the string matches byte for byte. The
    # reasoning dial is structurally inert on this path as a result —
    # figure description is content work.
    th = getattr(schema, "thinking", None)
    if (
        th is not None
        and getattr(th, "style", "") == "channel"
        and th.channel_token
        and th.content_channel
        and not gen.rstrip().endswith(schema.tokens.msg_content)
    ):
        gen = gen + f"{th.channel_token}{th.content_channel}{schema.tokens.msg_content}"
    parts.append(gen)
    return "".join(parts)


async def install_multimodal_prefix(
    engine: Any,
    encoder: MtmdEncoder,
    slot: Any,
    split: SplitPrompt,
    n_embd_inp: int,
    n_batch: int,
) -> None:
    """Install text1 + image chunks onto the seat via engine control ops.

    Encode runs on THIS (event-loop-adjacent) thread pool under the
    encoder lock; only the decodes enter control ops. Positions are
    verified after every image: for the embd path new position MUST equal
    old + chunk tokens (guaranteed — we assign positions ourselves); for
    the atomic path a divergence (M-RoPE cell law) raises rather than
    silently corrupting occupancy — such models need the cell-debt
    machinery before they may ride the batched path."""
    import asyncio

    loop = asyncio.get_running_loop()

    def _preflight():
        # SEAT/KV AGREEMENT GUARD (2026-08-26 19:15 incident): two installs
        # hit a seq holding 2,784 stale KV positions while slot.n_tokens
        # said 0 — llama_decode refused ("positions must be consecutive")
        # and the requests fell back to the pool. Root cause not yet
        # pinned (prepare_seat rm-alls on acquire, so SOMETHING re-
        # populated or never cleared the seq); this guard closes the
        # whole class: verify the seq's KV agrees with the slot before
        # writing row one, scrub loudly if not.
        ctx = engine._llama._ctx
        mx = ctx.memory_seq_pos_max(slot.seq)
        expect = slot.n_tokens - 1
        if mx != expect:
            log.warning(
                "vision install preflight: seq %d KV pos_max=%d but slot "
                "expects %d — scrubbing stale KV (see 2026-08-26 19:15 "
                "race in dev/BATCHED_VISION doc)",
                slot.seq,
                mx,
                expect,
            )
            ctx.memory_seq_rm(slot.seq, 0, -1)
            slot.n_tokens = 0
            slot.input_ids = []
            slot.has_media = False
            slot.cell_debt = 0

    await asyncio.wrap_future(engine.control(_preflight))
    for seg in split.pre:
        if isinstance(seg, MediaChunk):
            embd = await loop.run_in_executor(None, encoder.encode, seg, n_embd_inp)
            if seg.needs_atomic:
                before = slot.n_tokens

                def _atomic(seg=seg, embd=embd, before=before):
                    new_past = encoder.decode_image_atomic(
                        engine._llama._ctx,
                        seg,
                        embd,
                        before,
                        slot.seq,
                        n_batch,
                    )
                    # POSITION AUTHORITY IS new_past (the original plan's
                    # R3). Under M-RoPE the chunk's n_tokens CELLS advance
                    # n_past by only max(t,h,w) POSITIONS — paddle: a full
                    # page is 1,240 cells moving n_past by 40. input_ids
                    # stays position-dense (every rollback/purge removes by
                    # position), so it gains exactly `delta` sentinels; the
                    # remaining cells are booked as cell_debt, which
                    # _occupancy adds so admission sees the physical cache.
                    # Muse advances 1:1 (delta == n_tokens): debt 0,
                    # sentinels n_tokens — byte-identical to the previous
                    # behaviour, one code path.
                    delta = new_past - before
                    if delta <= 0 or delta > seg.n_tokens:
                        raise VisionInstallError(
                            f"M-RoPE position law violated: +{seg.n_tokens} "
                            f"cells moved n_past by {delta} (expected "
                            "1..n_tokens) — refusing the install"
                        )
                    slot.input_ids.extend([engine.MEDIA_SENTINEL] * delta)
                    slot.cell_debt += seg.n_tokens - delta
                    slot.n_tokens = new_past
                    slot.has_media = True

                await asyncio.wrap_future(engine.control(_atomic))
            else:
                await asyncio.wrap_future(
                    engine.control(
                        lambda seg=seg, embd=embd: engine.eval_embd_on_slot(
                            slot, embd, seg.n_tokens, n_embd_inp
                        )
                    )
                )
        else:
            toks = list(seg)
            for i in range(0, len(toks), n_batch):
                part = toks[i : i + n_batch]
                await asyncio.wrap_future(
                    engine.control(
                        lambda part=part: engine.eval_tokens_on_slot(slot, part)
                    )
                )
