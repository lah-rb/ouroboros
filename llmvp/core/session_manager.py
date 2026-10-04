"""Session Manager — memoryful inference sessions.

Each session pins a pool instance (or batched seat) for its lifetime. Two
turn-state mechanisms exist, selected by config: session_full_replay (the
safe default — re-prefill the token history from the pristine static
snapshot each turn) and resident_seq_cache (the production path — the
session appends to a live resident sequence, no re-prefill). The legacy
save_state/load_state per-turn snapshot splice was deleted 2026-07-30
(unreachable: config validation refuses session_full_replay: false; rap
sheet in dev/caching/CORPUS.md).

Sessions have a TTL. Expiry behavior:
- If an active subscription listener exists: push a SessionEvent.
- If no listener: silently release the instance.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, AsyncGenerator, Optional

from starlette.concurrency import run_in_threadpool

from core.config import strip_prior_reasoning_effective
from core.interaction_logger import log_interaction
from inference.tokenizer import get_cached_tokenizer, tokenize_segments, tokenize_text
from inference.repetition import DegenerateGenerationError
from formats.registry import get_renderer as _get_format_renderer

log = logging.getLogger("llm-mvp")

# Resident windowing reserves at most this much generation headroom when deciding
# whether a deep session must slide its window — NOT the full requested max_tokens.
# Session turns (esp. thinking models) often pass max_tokens ≈ n_ctx for headroom;
# reserving all of it would fire windowing every turn and gut the session memory.
# The backend caps the ACTUAL generation to the remaining context regardless, so
# this is just the minimum free headroom we keep before dropping the oldest turns.
#
# The reserve is additionally capped at n_ctx // 4 at the decision site: a flat
# 16384 against a 32k context is HALF the window, and it evicts on speculation.
# Measured (muse-glimmer structural session, 2026-08-15): windowing fired at
# pos 18.9k of 32.8k — 13.8k genuinely free, largest real generation in the
# session 3.5k — and the drop evicted the earlier files of a session walk, so
# every later file generated blind ("0 sibling(s) binding"). At n_ctx ≥ 64k
# the quarter-cap is ≥ 16384 and nothing changes.
_WINDOW_GEN_RESERVE = 16384


def _think_strip_enabled() -> bool:
    """Whether to strip prior-turn reasoning from session KV (Factor 4).

    On by default; set LLMVP_THINK_STRIP=0 to disable (e.g. A/B validation).
    """
    return os.environ.get("LLMVP_THINK_STRIP", "1") != "0"


def reasoning_span(
    family: str,
    thinking_enabled: bool,
    gen_tokens: list[int],
    gen_start_pos: int,
    tokenizer,
) -> tuple[int, str] | None:
    """Plan a truncate-and-replay reasoning strip for the just-finished turn.

    Returns ``(t0, replay_prefix)``: truncate the KV at position ``t0`` (dropping
    the turn's reasoning + raw answer) and re-eval ``replay_prefix`` + the clean
    answer there, leaving a canonical assistant turn with no reasoning. ``t0`` is
    chosen so the kept prefix (gen-prompt role framing) plus the replay yields the
    canonical form. Returns None when there's nothing to strip (non-thinking
    family/turn, or no reasoning marker — e.g. a truncated turn). Boundary
    markers are single special tokens thanks to canonical framing.

    ``gen_start_pos`` (P) is the KV position of the first generated token.
    """

    def sid(s: str) -> int | None:
        ids = tokenize_text(tokenizer, s, special=True)
        return ids[-1] if ids else None

    _s = _get_format_renderer(family).s
    _t = getattr(_s, "thinking", None)

    # INLINE-TAGS families — reasoning is delimited in-band by open/close
    # tags. Everything below is DERIVED from the format schema, which already
    # declares the one fact t0 depends on: whether this family's generation
    # prompt prefills the opener (`open_tag_prefill_when_enabled` — laguna
    # yes, gemma no) and whether a newline follows it (`open_tag_newline`).
    #
    # This branch was `family == "chatml"` plus `family == "gemma"` until
    # 2026-08-15, and both reduce to it exactly: chatml prefills `<think>\n`
    # so t0 backs over the opener; gemma prefills nothing so t0 IS the
    # generation start. Hardcoding the family list meant qwen/glm4/olmo/
    # hunyuan3/laguna/deepseek4 fell through to `return None` — their strip
    # flag would have been a silent no-op, the same hand-maintained-
    # vocabulary trap the featurizer hit at onboarding (reference.yaml 3c).
    if getattr(_t, "style", "") == "inline_tags":
        if not thinking_enabled:
            return None
        close_tag = getattr(_t, "close_tag", "") or ""
        open_tag = getattr(_t, "open_tag", "") or ""
        if not close_tag or sid(close_tag) not in gen_tokens:
            return None  # no close tag (no thinking, or truncated) → skip
        if open_tag and sid(open_tag) in gen_tokens:
            # The model SELF-emitted the opener — the gen-prompt did not
            # prefill it (a per-turn gate-closed level; gate_levels
            # families). Subtracting open_len would cut role framing out
            # of the KV; skip the strip for this rare turn instead.
            return None
        if not (open_tag and getattr(_t, "open_tag_prefill_when_enabled", True)):
            # Family never prefills (gemma): the role framing ends at the
            # generation start, so drop exactly the generated span.
            return (gen_start_pos, "")
        # Drop the injected opener (last tokens of the gen-prompt) + all
        # generated; keep the role framing. Replay = the clean answer.
        opener = open_tag + ("\n" if getattr(_t, "open_tag_newline", True) else "")
        open_len = len(tokenize_text(tokenizer, opener, special=True))
        t0 = gen_start_pos - open_len
        return (t0, "") if t0 >= 0 else None

    # Channel-style families — reasoning is a separate assistant message on a
    # reasoning channel/recipient (harmony's <|channel|>analysis, muse's
    # ` to=self`). Everything is derived from the format schema so a new
    # channel family costs no edits here: this branch was `family ==
    # "harmony"` until 2026-08-15, when muse-glimmer's strip turned out to be
    # a silent no-op — the same hand-maintained-vocabulary trap the
    # featurizer hit at onboarding (reference.yaml step 3c).
    if getattr(_t, "style", "") == "channel":
        _chan_tok = _s.thinking.channel_token
        _think_close = getattr(_s.tokens, "thinking_close", "") or ""
        if _think_close and _think_close != _s.tokens.msg_close:
            # A DISTINCT reasoning closer (muse: <|eom|> vs <|eot|>) is a
            # real special token and its presence IS the "this turn
            # reasoned" signal. The channel-count gate below is unusable
            # here: a plain-text recipient prefix like ` to=` ends in an id
            # (`=`) that generated code contains everywhere.
            if sid(_think_close) not in gen_tokens:
                return None
        else:
            # Shared closer (harmony's <|end|>): a reasoning turn reopens
            # the channel token at least twice (analysis + final).
            chan_id = sid(_chan_tok)
            if chan_id is None or sum(1 for t in gen_tokens if t == chan_id) < 2:
                return None  # no reasoning channel → nothing to strip
        # Keep the gen-prompt "<|start|>assistant"; drop ALL generated
        # channels, then replay the canonical content channel.
        return (
            gen_start_pos,
            f"{_chan_tok}{_s.thinking.content_channel}{_s.tokens.msg_content}",
        )

    return None


@dataclass
class TurnMark:
    """What it takes to take one session turn back (rewind_turn)."""

    turn_id: int
    pre_turn_pos: int  # KV length before the turn — the rollback boundary
    history_len: int  # len(token_history) before the turn (replay sessions)
    turn_count: int  # turn_count before the turn
    committed: bool = False
    dropped: bool = False  # rolled out at commit (an answerless turn)
    rewound: bool = False


@dataclass
class SessionState:
    """Internal state for an active memoryful session."""

    instance: Any  # Pinned pool instance
    last_assistant_text: str = ""  # Captured generation for next turn prefix
    ttl: int = 300
    listener: Optional[asyncio.Queue] = None  # For expiry event push
    created_at: float = field(default_factory=time.monotonic)
    last_turn_at: float = field(default_factory=time.monotonic)
    turn_count: int = 0
    # Turns currently generating. `last_turn_at` only advances when a turn
    # COMPLETES, so a turn that runs longer than the TTL leaves the idle clock
    # frozen at its start and the TTL monitor reaps the session out from under
    # its own live generation. Observed 2026-08-09 on hy3: a 1056s symbol
    # rewrite was expired at 600s with `turns=0` while still decoding; the
    # generation then finished into a dead session and the four queued symbols
    # after it all returned "Session not found" — a 5-symbol patch silently
    # became a 1-symbol patch. The orphan reaper below already refuses to
    # reclaim while GPU work is in flight; this is the same rule, per session.
    in_flight: int = 0
    # Full-replay mode (model.session_full_replay): the exact dynamic
    # token sequence of every completed turn (turn segments + generated
    # tokens), re-prefilled on top of the pristine static snapshot each
    # turn instead of save/load state surgery. A degenerate turn simply
    # never enters the history.
    token_history: list = field(default_factory=list)
    # Full-replay APPEND FAST PATH (2026-08-18). The replay stream is
    # append-only — turn N+1 is exactly turn N plus new tokens — so a session
    # whose KV still matches token_history can CONTINUE forward instead of
    # restoring the static snapshot and re-feeding everything. Measured on
    # qwen3.8 (memory_can_shift=False), dev/append_continuation_probe.py:
    # depth 8 → 6.3x fewer tokens fed, depth 24 → 17.4x (3.8x prefill wall),
    # needle recalled at both depths in both paths. `memory_can_shift` gates
    # REMOVAL and SHIFTING (seq_rm/seq_cp/windowing); continuing forward needs
    # neither, and the recurrent state after [0,N) is exactly what extends.
    # kv_dirty means the live KV may no longer match token_history — a
    # degenerate or abandoned turn left its span resident while the history
    # never took it. The AUTHORITY is the verified invariant (backend
    # session_kv_matches); the flag is the reason. Repair is a TURN ROLLBACK to
    # the last committed boundary where the model has one (hybrids restore the
    # boundary's recurrent checkpoint — inference/turn_checkpoint.py), else a
    # restore-and-replay. A bare n_tokens rewind is never a repair: on a hybrid
    # it leaves the recurrent state past the rewind and dies at the next decode.
    kv_dirty: bool = False
    # Resident session flow-fork (model.resident_session_flow_fork): an invariant
    # per-flow preamble ABOVE the global static, pinned + forked onto the live seq at
    # turn 0 so only the first user message prefills. static_base is the resident
    # head length to PRESERVE on windowing (flow_prefix_len when forked, else the
    # global static len).
    flow_key: Optional[str] = None
    flow_static_prefix: Optional[str] = None
    static_base: int = 0
    # Semi-permanent snapshot linkage. snapshot_key: this session TOOK a
    # snapshot (its live cells are shared with the pinned seq until the
    # session ends). snapshot_forked: this session STARTED from one. Either
    # way windowing is forbidden — memory_seq_add would shift shared cells
    # and corrupt the snapshot (see SessionSnapshotOverflow).
    snapshot_key: Optional[str] = None
    snapshot_forked: bool = False
    # Whether the LAST turn's generation prompt prefilled the think opener
    # (renderer.prefills_think_opener at that turn's reasoning level). The
    # FSM needs it to tell an unterminated think from an answer: the opener
    # is in the prompt, so the output stream can only show a closer — and a
    # turn cut off mid-thought shows none.
    last_prefilled_think: Optional[bool] = None
    # A resident purge the memory refused (see session_turn's degenerate
    # handler): the live seq still holds a span no turn committed, and there
    # is no sound way back. Further turns raise instead of decoding on it.
    kv_lost: bool = False
    # What happened to the LAST turn at commit (replay sessions with turn
    # rollback): its reasoning was stripped from the context, or — a turn that
    # produced no answer — it was rolled back out entirely.
    last_turn_stripped: bool = False
    last_turn_dropped: bool = False
    # Monotonic turn ids (every session_turn call, committed or not) and the
    # LAST turn's mark. A caller rewinds by id, so a blind "take back the last
    # turn" after a server-side drop can never erase the good turn before it.
    turn_seq: int = 0
    last_turn: Optional[TurnMark] = None


class SessionSnapshotOverflow(RuntimeError):
    """A snapshot-linked session outgrew the context window.

    Windowing is the normal deep-session escape hatch, but it position-
    shifts KV cells the session SHARES with a pinned snapshot seq —
    corrupting the snapshot. Snapshot-linked sessions are sized to fit
    (paper + passes ≪ n_ctx); overflow means the request is wrong, and
    the caller's correct move is a smaller ask or a fresh fork, never
    silent context loss.
    """


def _global_temperature_floor(requested: float, gen_cfg: Any) -> float:
    """Apply the per-model global temperature floor (any request kind).

    A refusal to sample below ``generation.temperature_floor``: requests
    under the floor are raised to it. Distinct from the session-depth
    floor below, which applies on top for deep turns.
    """
    floor = getattr(gen_cfg, "temperature_floor", None)
    if floor and requested < floor:
        return float(floor)
    return requested


def _effective_session_temperature(
    requested: float, turn_count: int, gen_cfg: Any
) -> float:
    """Apply the session temperature floor for deep turns.

    Deep multi-turn sessions are repetition attractors — low requested
    temperatures compound across accumulated KV until the sampler locks
    into a token cycle (live-observed at turn 5-6 even on models not
    otherwise predisposed; sparse MoEs hit it earliest). When the config
    declares ``session_temp_floor``, turns at depth >=
    ``session_temp_floor_after_turn`` (default 2 — the third turn
    onward) sample at no less than the floor. Shallow turns and plain
    completions keep the requested temperature untouched.
    """
    floor = getattr(gen_cfg, "session_temp_floor", None)
    if not floor:
        return requested
    after = getattr(gen_cfg, "session_temp_floor_after_turn", None)
    after = 2 if after is None else after
    if turn_count >= after and requested < floor:
        return float(floor)
    return requested


@dataclass
class SessionInfo:
    """Returned when a session is created."""

    session_id: str
    instance_index: int
    ttl_seconds: int


@dataclass
class SessionEvent:
    """Push notification for session lifecycle events."""

    session_id: str
    event_type: str  # "expired" | "closed" | "error"
    message: str


def _generate_session_id() -> str:
    return uuid.uuid4().hex[:16]


class SessionManager:
    """Manages memoryful inference sessions.

    Each session pins a pool instance and maintains a per-turn
    KV cache state snapshot.
    """

    def __init__(self, backend: Any):
        self._backend = backend
        # Context auto-refresh straggler hook: the backend force-expires
        # sessions that outlive the refresh drain window through the normal
        # expiry path (listener event + end_session), not by yanking seats.
        setattr(backend, "_session_expirer", self.expire_all_sessions)
        self._sessions: dict[str, SessionState] = {}
        self._expiry_tasks: dict[str, asyncio.Task] = {}
        self._turn_transition_cache: str | None = None
        # Crash-proof backstop for the per-session TTL monitors (see _orphan_reaper).
        # Lazily started on the first session; reclaims a pinned instance even if a
        # monitor task dies, so a standard client kill (pkill) cannot leak it.
        self._orphan_reaper_task: Optional[asyncio.Task] = None

    @property
    def active_session_count(self) -> int:
        return len(self._sessions)

    def get_session_ids(self) -> list[str]:
        return list(self._sessions.keys())

    def _get_turn_transition(self) -> str:
        """Get the tokens that close a previous assistant turn.

        Derived from the format schema — no template probing needed.
        The result is cached for the lifetime of the SessionManager.
        """
        if self._turn_transition_cache is not None:
            return self._turn_transition_cache

        config = self._backend.config
        renderer = _get_format_renderer(config.model.family)
        self._turn_transition_cache = renderer.render_turn_transition()
        log.info(
            "Session turn transition from schema: %r (%d chars)",
            self._turn_transition_cache[:60],
            len(self._turn_transition_cache),
        )
        return self._turn_transition_cache

    def _generation_guard(self):
        """The backend's GPU-work guard, or a no-op for backends/fakes
        that don't implement it (e.g. test doubles).

        Session state operations (load_state/save_state) and turns are
        GPU work — they must never run concurrently with a JIT scaling
        operation's spawn/warm-up/teardown.
        """
        guard = getattr(self._backend, "generation_guard", None)
        if guard is None:
            return contextlib.nullcontext()
        return guard()

    @contextlib.asynccontextmanager
    async def _turn_in_flight(self, session: "SessionState"):
        """Mark a session as actively generating for the duration of a turn.

        A LIVE GENERATION IS NOT IDLENESS. Without this the TTL monitor
        measures idleness from `last_turn_at`, which only advances on turn
        COMPLETION — so the first turn of a session is "idle" for its entire
        duration and any turn longer than the TTL destroys its own session
        mid-decode (see SessionState.in_flight for the incident).

        Stamping `last_turn_at` on exit is what makes the window honest: the
        idle clock starts when the turn ENDS, including when it ends by
        raising, so a failed turn cannot leave a session immortal either.
        """
        session.in_flight += 1
        try:
            yield
        finally:
            session.in_flight -= 1
            session.last_turn_at = time.monotonic()

    async def _resident_session_flow_fork(
        self, instance: Any, session: "SessionState", prompt: str
    ) -> Optional[list]:
        """Turn-0 flow fork: pin [global static + flow head] and fork it onto the
        live session seq so only the first user message's tail prefills.

        The first user turn is rendered as ``static_prefix + prompt`` (ONE turn —
        the identical token stream the stateless flow path produces), the stable
        head is pinned via the backend's resident flow cache (BUILD once per
        instance, HIT after), and only the suffix AFTER the pinned head is returned
        to append. Sets ``session.static_base`` (the resident head to preserve on
        windowing). Returns the turn-0 token suffix, or None to fall back to the
        normal turn-0 build (no usable head, or the backend fell back).
        """
        from inference.tokenizer import build_full_prompt, flow_head_tokens

        tokenizer = get_cached_tokenizer()
        static_toks = getattr(self._backend, "_resident_static_tokens", None) or []
        glob = int(getattr(self._backend, "_resident_static_len", 0) or 0)
        prefix = session.flow_static_prefix or ""

        # PROBE ONLY — reports, never repairs (operator, 2026-08-05).
        #
        # The stateless path strips a duplicated head (inference.py:399) but
        # this one has no such guard, and four agent-side callers pass
        # static_prefix=PERSONA *and* queue that same persona at the head of
        # their seed injection (diagnosis_session_actions, router_actions,
        # escalation_actions, interactive_actions). On a flow-fork server the
        # persona then lands in the token stream TWICE; on a plain server it
        # lands once, because start_session ignores static_prefix. Stripping
        # here would silently change model input for four flows mid-campaign,
        # so this only makes the condition visible enough to measure.
        if prefix and prompt.startswith(prefix):
            log.warning(
                "🧩 flow static_prefix DUPLICATED at the head of turn 0 "
                "(%d chars, flow_key=%s) — NOT stripping, unlike the "
                "stateless path. The caller is passing static_prefix AND "
                "seeding the same text; the model reads the persona twice.",
                len(prefix),
                session.flow_key,
            )

        dynamic_full = build_full_prompt(prefix + prompt, tokenizer)
        n = len(flow_head_tokens(prefix, tokenizer, confirm_with=dynamic_full))
        if n <= 0:
            session.static_base = glob
            return None
        head = list(static_toks) + list(dynamic_full[:n])
        got = await run_in_threadpool(
            self._backend._resident_flow,
            instance,
            session.flow_key,
            len(head),
            head,
        )
        if got is None:  # backend fell back to the global static base
            session.static_base = glob
            return None
        session.static_base = int(got)
        log.info(
            "🪡 session flow-fork %r: pinned %d-tok head, turn-0 suffix %d tok",
            session.flow_key,
            len(head),
            len(dynamic_full) - n,
        )
        return list(dynamic_full[n:])

    async def start_session(
        self,
        ttl_seconds: int = 300,
        flow_key: Optional[str] = None,
        static_prefix: Optional[str] = None,
        from_snapshot: Optional[str] = None,
        persona: Optional[str] = None,
    ) -> SessionInfo:
        """Acquire instance, save initial state, return session info.

        ``flow_key`` + ``static_prefix`` opt a resident session into the turn-0
        flow-fork (model.resident_session_flow_fork): the invariant preamble is
        pinned + forked onto the live seq at turn 0 so it isn't re-prefilled per
        session. Ignored unless resident_session_flow_fork is active.

        ``from_snapshot`` forks the new session from a pinned snapshot (see
        session_snapshot): hot = seq_cp (~zero prefill), cold/replay = re-
        prefill from the registry's token list. turn_count is seeded from the
        snapshot so the first turn renders the turn transition — the captured
        KV holds a CLOSED prior turn. Unknown key raises KeyError before any
        state is touched.

        ``persona`` leases the pool slot carrying that persona's SOUL
        (multi-persona pooling; session-scoped — the whole session speaks as
        that persona). None → the default persona's slot.
        """
        snap_entry = None
        if from_snapshot:
            registry = getattr(self._backend, "_snap_registry", {})
            snap_entry = registry[from_snapshot]  # KeyError = unknown snapshot

        try:
            instance = await self._backend.acquire_instance(persona=persona)
        except TypeError:
            # Backend double without persona routing (tests) — legacy path.
            instance = await self._backend.acquire_instance()
        if hasattr(instance, "pinned"):
            # Batched seat: mark it session-pinned so the engine's
            # KV-pressure ladder never evicts a live session's seq.
            instance.pinned = True
        session_id = _generate_session_id()

        # Resident-live sessions keep seq 0 live across turns (acquire_instance
        # already forked the pristine static onto it); full-replay sessions
        # restore the pristine static and re-prefill history per turn. Neither
        # takes an initial snapshot — the legacy save_state splice (and its
        # multi-GB per-session save_state here) was deleted 2026-07-30; the
        # validator refuses session_full_replay: false, so no loadable config
        # can reach that path.
        resident = bool(getattr(self._backend, "_resident_active", False))

        session = SessionState(
            instance=instance,
            ttl=ttl_seconds,
            created_at=time.monotonic(),
            last_turn_at=time.monotonic(),
            flow_key=flow_key,
            flow_static_prefix=static_prefix,
        )

        if snap_entry is not None:
            # THE SEAT MUST SURVIVE A FAILED FORK. Everything from here to
            # registration can raise — most reliably `rebuild_snapshot_cold`,
            # which under batched ALWAYS raises on a cold entry (a seat has no
            # .eval), and every entry goes cold after any context refresh. The
            # instance was acquired above and, on a batched seat, already
            # marked `pinned` — so an escaping exception left it leased with
            # no session to release it, and the seat reaper skips pinned seats
            # by design. That is one seat of 128 gone permanently, per call.
            try:
                session.snapshot_forked = True
                session.turn_count = int(snap_entry.get("turn_count") or 0)
                if resident:
                    # Hot fork (~zero prefill); cold miss (refresh/instance
                    # mismatch/replay-only entry) rebuilds by re-prefill. Both
                    # leave seq 0 holding static + snapshot content — the
                    # live-KV turn path appends from there.
                    async with self._generation_guard():
                        got = await run_in_threadpool(
                            self._backend.fork_snapshot_seq, instance, from_snapshot
                        )
                        if got is None:
                            await run_in_threadpool(
                                self._backend.rebuild_snapshot_cold,
                                instance,
                                from_snapshot,
                            )
                else:
                    # Replay fallback (recurrent models / resident off): seed
                    # the history; turn 1 re-prefills it on the static base.
                    # The KV holds only the static here — it does NOT match the
                    # seeded history, and before the invariant check the append
                    # path took "non-empty history + non-empty KV" for a match
                    # and silently never prefilled the snapshot.
                    session.token_history = list(snap_entry["dyn_tokens"])
                    session.kv_dirty = True
            except BaseException:
                # Unpin FIRST: release_instance on a still-pinned seat is a
                # no-op in the batched backend, which would re-leak it.
                if hasattr(instance, "pinned"):
                    instance.pinned = False
                with contextlib.suppress(Exception):
                    await self._backend.release_instance(instance)
                log.error(
                    "🌱 Session %s fork from snapshot %r FAILED — seat released "
                    "(a leaked seat is permanent; the reaper skips pinned seats)",
                    session_id,
                    from_snapshot,
                )
                raise
            log.info(
                "🌱 Session %s forked from snapshot %r (turn_count=%d, mode=%s)",
                session_id,
                from_snapshot,
                session.turn_count,
                "resident" if resident else "replay",
            )

        self._sessions[session_id] = session

        # Start TTL expiry timer + ensure the crash-proof orphan reaper is running.
        self._expiry_tasks[session_id] = asyncio.create_task(
            self._ttl_monitor(session_id, ttl_seconds)
        )
        self._ensure_orphan_reaper()

        log.info(
            "📌 Session %s started (ttl=%ds, pinned instance)",
            session_id,
            ttl_seconds,
        )

        return SessionInfo(
            session_id=session_id,
            instance_index=0,  # We don't expose internal index
            ttl_seconds=ttl_seconds,
        )

    async def session_turn(
        self,
        session_id: str,
        prompt: str,
        max_tokens: int = 256,
        temperature: float = 0.7,
        grammar: str | None = None,
        raw: bool = False,
        reasoning: str | None = None,
        sampling_overrides: dict | None = None,
    ) -> AsyncGenerator[str, None]:
        """Execute a turn within a memoryful session (streaming).

        ``reasoning`` (low/medium/high) triggers the reasoning HEAD-SWAP at turn 0
        for a non-flow session (backend.reasoning_head_swap): the level's pinned
        system head is forked onto the live seq, so the whole session runs at that
        reasoning effort. No-op unless enabled + a non-default level + turn 0.

        1. Establish the turn's KV base: resident keeps seq 0 live;
           full replay restores the pristine static and re-prefills the
           token history.
        2. Build continuation tokens: [close previous assistant turn] +
           [new user turn] + [generation prompt].
        3. Generate with reset=False (KV cache preserved).
        4. Persist the turn: resident leaves KV live; full replay extends
           token_history.
        5. Yield tokens as they're generated.

        IMPORTANT: The established KV base already contains the
        previous turn's generation.  We must NOT re-inject it as a
        message — that would duplicate it in the context.  Instead,
        for continuation turns (turn_count > 0), we only emit the
        transition tokens that close the previous assistant turn and
        open the new user turn.

        All delimiter/thinking extraction is handled by the FSM labeller
        in session_turn_complete(), which collects the full raw output
        and runs FSM-based phase labeling on the complete text.
        This streaming generator always yields raw chunks — no
        inline delimiter detection.
        """
        session = self._sessions.get(session_id)
        if session is None:
            log.error(
                "❌ Session %s not found (active sessions: %s)",
                session_id,
                list(self._sessions.keys()),
            )
            raise ValueError(f"Session {session_id} not found")

        instance = session.instance
        if session.kv_lost:
            raise RuntimeError(
                f"Session {session_id} lost its KV (a resident purge was refused "
                "by the memory) — end it and start a new session"
            )

        # The entire turn — KV-base restore, generation, reasoning strip —
        # is one continuous span of GPU work. Hold ONE
        # generation guard around all of it (the backend's generate
        # wrapper re-enters the guard; nested entries are counter-only)
        # so a JIT scaling operation can neither interleave with the
        # turn nor start mid-turn.
        #
        # _turn_in_flight rides the same span so the TTL monitor cannot reap
        # this session while the turn it is waiting on is still decoding.
        async with self._generation_guard(), self._turn_in_flight(session):
            # Bound to the backend this manager serves, not the global —
            # correct by construction across model swaps (Phase 2a).
            config = self._backend.config
            # Two session strategies remain (the legacy save_state/load_state
            # per-turn splice was deleted 2026-07-30 — the validator refuses
            # session_full_replay: false, so no loadable config reached it;
            # its rap sheet lives in dev/caching/CORPUS.md).
            # Resident-live takes precedence: seq 0 already holds static + every
            # prior turn (left live from last turn) — NO restore, NO re-prefill.
            # We only capture the live position so a degenerate turn can be
            # purged back to it. Otherwise full replay: restore the PRISTINE
            # static snapshot — the one whole-state op recurrent/hybrid
            # architectures support — and re-prefill the accumulated token
            # history below.
            resident = bool(getattr(self._backend, "_resident_active", False))
            pre_turn_pos = 0
            flow_turn_suffix = None  # set by the turn-0 flow fork, if any
            append_ok = False
            if resident:
                # Turn 0 of a flow session: fork the pinned [global static + flow
                # head] onto the live seq (BUILD once per instance, HIT after) so
                # only the first user message's tail prefills. Must run BEFORE
                # capturing pre_turn_pos (the fork advances the live position).
                if (
                    session.turn_count == 0
                    and getattr(self._backend, "_session_flow_fork", False)
                    and session.flow_key
                    and session.flow_static_prefix
                ):
                    flow_turn_suffix = await self._resident_session_flow_fork(
                        instance, session, prompt
                    )
                elif reasoning and getattr(
                    self._backend, "_reasoning_head_swap", False
                ):
                    if session.turn_count == 0:
                        # Reasoning HEAD-SWAP: override acquire's default-static fork
                        # with the requested level's pinned head. Whole-seq install is
                        # sound only before any turn sits above the head.
                        await run_in_threadpool(
                            self._backend._install_reasoning_head, instance, reasoning
                        )
                    else:
                        # Mid-session: SPLICE only the head span, body stays live —
                        # the adaptive router's per-turn actuator (no-op when the
                        # requested level is already installed).
                        await run_in_threadpool(
                            self._backend._splice_reasoning_head, instance, reasoning
                        )
                pre_turn_pos = int(getattr(instance, "n_tokens", 0) or 0)
            else:
                # Append fast path: continue from the live KV when it still
                # holds exactly [persona static] + token_history — VERIFIED, not
                # remembered (see backend.session_kv_matches). (append_ok is
                # pre-bound above so the later `not resident and not append_ok`
                # never depends on short-circuit evaluation to stay defined.)
                # Turn 0 keeps the restore: cheap, and a known-good base.
                append_ok = bool(session.token_history) and self._replay_kv_matches(
                    instance, session
                )
                if append_ok and session.kv_dirty:
                    # A failure BEFORE anything was evaluated (a context-window
                    # refusal) sets the flag without touching the KV.
                    log.info(
                        "🧩 Session %s: dirty flag set but the KV verifies "
                        "clean — appending",
                        session_id,
                    )
                if not append_ok and session.token_history:
                    # The KV ran past the last committed turn (a degenerate
                    # span, an abandoned stream, a refused-then-kept turn).
                    # Roll it back to that turn's checkpoint instead of
                    # re-prefilling the whole history.
                    append_ok = await self._rollback_to_history(
                        session_id, instance, session
                    )
                if not append_ok:
                    await self._restore_replay_static(instance)
                    if session.token_history:
                        log.info(
                            "🧩 Session %s: KV %s — restored static and "
                            "replaying %d history tokens",
                            session_id,
                            "was dirty" if session.kv_dirty else "diverged",
                            len(session.token_history),
                        )
                    # kv_dirty is NOT cleared here: it clears when this turn
                    # COMMITS. Cleared up front, a replay that failed after the
                    # restore left a static-only KV marked clean, and the next
                    # turn appended onto a session with no history in it.

            # Build turn tokens — different paths for first turn vs continuation
            renderer = _get_format_renderer(config.model.family)

            # Global per-model floor first (any request kind), then the
            # session-depth floor on top — deep turns are repetition
            # attractors; see _effective_session_temperature.
            gen_cfg = getattr(config, "generation", None)
            globally_floored = _global_temperature_floor(temperature, gen_cfg)
            if globally_floored != temperature:
                log.info(
                    "🌡️ Session %s turn %d: global floor %.2f -> %.2f",
                    session_id,
                    session.turn_count + 1,
                    temperature,
                    globally_floored,
                )
                temperature = globally_floored
            floored = _effective_session_temperature(
                temperature, session.turn_count, gen_cfg
            )
            if floored != temperature:
                # NB: this module's logger is `log`, not `logger` — the
                # original NameError here detonated only when the floor
                # first APPLIED (gpt-oss's sub-floor turn temps), killed
                # the turn mid-guard, and leaked the pinned session:
                # active=1/limit=1 deadlocked every session flow after.
                log.info(
                    "🌡️ Session %s turn %d: temperature floored %.2f -> %.2f",
                    session_id,
                    session.turn_count + 1,
                    temperature,
                    floored,
                )
                temperature = floored

            # Build the turn as (text, is_framing) segments so structural framing
            # tokenizes as canonical special tokens while the user prompt stays
            # plain text. Continuation turns (turn_count > 0) first emit the
            # transition that closes the previous assistant turn in the KV cache;
            # the first turn has no prior assistant output to close.
            if flow_turn_suffix is not None:
                # Turn-0 flow fork: the live seq already holds [global static + flow
                # head]; the suffix encodes the rest of the (static_prefix + prompt)
                # user turn + the generation prompt. Append only that.
                turn_tokens = flow_turn_suffix
                turn_only = turn_tokens
                # build_full_prompt rendered this suffix's gen prompt at the
                # default level.
                session.last_prefilled_think = renderer.prefills_think_opener(None)
            else:
                session.last_prefilled_think = renderer.prefills_think_opener(reasoning)
                segments: list = []
                if session.turn_count > 0:
                    segments += renderer.render_turn_transition_segments()
                segments += renderer.render_user_segments(prompt)
                # Per-turn level reaches the think GATE here (gate_levels
                # families: router low omits the prefill for this turn).
                segments += renderer.render_generation_prompt_segments(
                    reasoning=reasoning
                )

                tokenizer = get_cached_tokenizer()
                turn_tokens = tokenize_segments(tokenizer, segments)
                turn_only = turn_tokens
                if not resident and not append_ok:
                    # Full replay: re-prefill everything this session has ever
                    # evaluated, then this turn — identical token stream to
                    # what the KV would have held under state splicing, rebuilt
                    # exactly. (Resident keeps the KV live, so it appends
                    # turn_only only; the append fast path above does the same
                    # for full-replay sessions whose KV is still trustworthy.)
                    turn_tokens = list(session.token_history) + turn_only

            # Resident windowing: if this turn + its generation won't fit in the
            # context, slide the window (drop the oldest turns, keep the static
            # head + recent turns) instead of letting the backend raise at the
            # context-window guard. Update pre_turn_pos so a degenerate purge
            # targets the post-window position.
            if resident:
                n_ctx = int(getattr(instance, "_n_ctx", 0) or 0)
                # Preserve the session's actual resident head — the flow head when a
                # flow-fork session pinned one, else the global static — so windowing
                # never shaves off the agent preamble.
                n_keep = int(
                    session.static_base
                    or getattr(self._backend, "_resident_static_len", 0)
                    or 0
                )
                # Reserve only a BOUNDED generation headroom (not the full
                # max_tokens — a thinking-turn's max_tokens is often ~n_ctx, which
                # would window every turn). The backend caps real generation to the
                # remaining context anyway; this just keeps a slice free. Never
                # more than a quarter of the context: see _WINDOW_GEN_RESERVE.
                gen_reserve = min(int(max_tokens or 0), _WINDOW_GEN_RESERVE, n_ctx // 4)
                if n_ctx and pre_turn_pos + len(turn_tokens) + gen_reserve >= n_ctx:
                    if session.snapshot_key or session.snapshot_forked:
                        # Windowing position-shifts (memory_seq_add) KV cells
                        # this seq SHARES with a pinned snapshot — corruption,
                        # not relief. Snapshot sessions are sized to fit;
                        # overflow means the ask is wrong. Fail loudly.
                        raise SessionSnapshotOverflow(
                            f"session {session_id} is snapshot-linked "
                            f"(key={session.snapshot_key or 'forked'}) and hit the "
                            f"context window ({pre_turn_pos} + {len(turn_tokens)} "
                            f"+ {gen_reserve} reserve >= {n_ctx}) — windowing is "
                            f"forbidden; use a smaller pass or a fresh fork"
                        )
                    pre_turn_pos = await run_in_threadpool(
                        self._backend._window_resident_seq, instance, n_keep
                    )

            # Build generation kwargs
            gen_kwargs = {}
            if grammar:
                gen_kwargs["grammar"] = grammar

            # Session-mode stops. NOTE: these do NOT include the fake-
            # assistant-turn opener — stopping on <|start|>assistant cut the
            # legitimate analysis→final reopen mid-turn (the e75 46%-empty
            # regression; see renderer.stop_tokens' design note). Post-answer
            # rambling (the a7ff pathology: the model chains fake turns after
            # its final answer) is instead neutralized at extraction by the
            # FSM labeller's single_turn seal (first non-empty content phase
            # wins), with the repetition guard as the backstop when a ramble
            # degenerates (astropy-2 runaway capture 20260703T152322).
            gen_kwargs["stop_texts"] = renderer.stop_tokens(mode="session")

            # turn_tokens are PURELY incremental — the restored KV already holds
            # the static prefix + prior turns. Without this flag the backend's
            # completion-path slice skipped the first n_static tokens of the TURN
            # whenever the turn was longer than the static prefix, silently
            # amputating the prompt head (the model saw only the tail).
            gen_kwargs["static_in_prompt"] = False

            # Think-hold: when THIS turn's genprompt prefills the think opener
            # on a family that treats the opener as advisory (laguna), ban the
            # close tag for the first N tokens so the model commits to the
            # thinking branch instead of closing under persona pressure. See
            # inference/think_hold.py; no-op for every other family/level.
            from inference.think_hold import resolve_think_hold_kwargs

            _hold = resolve_think_hold_kwargs(
                renderer, get_cached_tokenizer(), reasoning
            )
            if _hold:
                gen_kwargs["think_hold"] = _hold

            # We already hold this turn's generation guard — the wrapper's
            # own guard entry must only balance the counter, not re-wait
            # the scaling gate (deadlock against a draining scaler).
            gen_kwargs["_nested_guard"] = True

            # Publish WHOSE generation this is on the health endpoint. The
            # session id is the natural identity: turns are sequential and only
            # this session's driver issues them, so "session X is generating"
            # unambiguously means "the caller polling for session X". Without
            # it, a client polling health during its own turn cannot tell its
            # numbers from another seat's — which is how six requests were
            # cancelled by an orphan's token count on 2026-07-28.
            gen_kwargs["request_id"] = session_id

            # Degeneration-retry recipe (session_turn_complete): per-request
            # Llama.generate sampling overrides, merged over the config
            # defaults in the backend.
            if sampling_overrides:
                gen_kwargs["sampling_overrides"] = dict(sampling_overrides)

            # Replay sessions on a rollback-capable model checkpoint the turn
            # boundary (the KV right before this turn's prompt), so this turn
            # can later be rolled back — stripped, dropped, or repaired after
            # an abandon/degeneration — without replaying the history.
            session.last_turn_dropped = False
            session.last_turn_stripped = False
            if (
                not resident
                and flow_turn_suffix is None
                and getattr(self._backend, "_turn_rollback", "none") != "none"
            ):
                pre_turn_pos = self._backend.session_base_len(instance) + len(
                    session.token_history
                )
                gen_kwargs["turn_checkpoint_at"] = pre_turn_pos
            elif not resident and hasattr(self._backend, "session_base_len"):
                pre_turn_pos = self._backend.session_base_len(instance) + len(
                    session.token_history
                )
            session.turn_seq += 1
            mark = TurnMark(
                turn_id=session.turn_seq,
                pre_turn_pos=int(pre_turn_pos),
                history_len=len(session.token_history),
                turn_count=session.turn_count,
            )
            session.last_turn = mark

            # Collect generated text for next turn's assistant prefix
            generated_parts: list[str] = []
            # Set on commit. Anything else leaving the try — an error, OR the
            # consumer abandoning this generator (GeneratorExit at the yield,
            # which no except clause below sees) — leaves a replay session's
            # KV holding a span its history never took.
            completed = False

            # Generate (KV cache has full prior context from load_state). If the
            # turn degenerates (e.g. the Gemma-4 repetition collapse), the backend
            # raises DegenerateGenerationError — we PURGE and re-raise so the failure
            # surfaces cleanly (GraphQL error → no_answer → mission_control) instead
            # of hanging until max_tokens.
            try:
                async for chunk in self._backend.generate_stream_async(
                    instance=instance,
                    prompt_tokens=turn_tokens,
                    max_tokens=max_tokens,
                    temperature=temperature,
                    **gen_kwargs,
                ):
                    generated_parts.append(chunk)
                    yield chunk

                if resident:
                    # Resident-live: KV stays live on seq 0 — NO save_state, NO
                    # token_history. Optional Factor-4 in-place CoT strip
                    # (model.resident_strip_reasoning): drop this turn's reasoning
                    # from the live KV so prior-turn CoT never accumulates. No-op
                    # for non-thinking families; skips truncated turns (handled in
                    # _maybe_strip_reasoning). Crash-free here (no save_state),
                    # unlike the splice path. Off by default (parity w/ full_replay).
                    if _think_strip_enabled() and strip_prior_reasoning_effective(
                        config.model
                    ):
                        from core.inference import _strip_delimiter

                        content = _strip_delimiter(
                            "".join(generated_parts),
                            prefilled_think=session.last_prefilled_think,
                        )
                        await self._maybe_strip_reasoning(instance, content)
                else:
                    # Full replay — commit EXACTLY what the live KV now holds:
                    # the raw turn, its stripped form, or (an answerless turn,
                    # rolled back) nothing. See _commit_replay_turn.
                    await self._commit_replay_turn(
                        session_id,
                        session,
                        instance,
                        turn_only,
                        "".join(generated_parts),
                        pre_turn_pos,
                        config,
                    )
                session.last_assistant_text = "".join(generated_parts)
                session.last_turn_at = time.monotonic()
                if not session.last_turn_dropped:
                    session.turn_count += 1
                mark.committed = True
                mark.dropped = session.last_turn_dropped
                completed = True

                log.info(
                    "Session %s turn %d complete (%d chars generated)",
                    session_id,
                    session.turn_count,
                    len(session.last_assistant_text),
                )
            except DegenerateGenerationError as e:
                if resident:
                    # Purge the degenerate span from the live seq: drop its KV
                    # from pre_turn_pos to the end and rewind the position. All
                    # prior turns survive (they sit below pre_turn_pos). No blob.
                    # Batched seats expose purge_to (per-seq, routed through the
                    # decode thread); pool instances keep the seq-0 surgery.
                    if hasattr(instance, "purge_to"):
                        await run_in_threadpool(instance.purge_to, pre_turn_pos)
                    else:

                        def _purge_resident() -> bool:
                            # A hybrid's recurrent half REFUSES a tail rm
                            # (qwen3-next runs resident): it returns False and
                            # changes nothing. Rewinding n_tokens over an
                            # untouched KV desynchronised positions from cells
                            # and the next decode failed or silently reset.
                            if (
                                instance._ctx.memory_seq_rm(0, pre_turn_pos, -1)
                                is False
                            ):
                                return False
                            instance.n_tokens = pre_turn_pos
                            return True

                        if not await run_in_threadpool(_purge_resident):
                            session.kv_lost = True
                            log.error(
                                "🛑 Session %s degenerate generation (%s) — "
                                "resident purge REFUSED by the memory (hybrid "
                                "tail rm); the degenerate span is still in the "
                                "KV, so the session is closed to further turns",
                                session_id,
                                e.reason,
                            )
                            raise
                    log.warning(
                        "🛑 Session %s degenerate generation (%s) — resident, "
                        "purged turn span back to pos %d",
                        session_id,
                        e.reason,
                        pre_turn_pos,
                    )
                    raise
                # Full replay: the history was never extended, so the
                # degenerate span does not exist as far as a re-prefill is
                # concerned — but it IS resident in the live KV, so the append
                # fast path can no longer continue from it. Mark the session
                # dirty: the next turn restores the static snapshot and replays
                # the (clean) history once, then resumes appending.
                session.kv_dirty = True
                log.warning(
                    "🛑 Session %s degenerate generation (%s) — full-replay "
                    "mode, degenerate turn dropped from history; KV marked "
                    "dirty (next turn restores + replays)",
                    session_id,
                    e.reason,
                )
                raise
            except RuntimeError:
                # Fatal decode (llama_decode -3/-2): the backend marked the
                # instance — its Metal backend is LATCHED and cannot decode
                # again until the context is rebuilt, which also destroys this
                # session's live KV. A pinned session never reaches
                # release_instance, so heal HERE: end the session (release →
                # targeted context refresh → slot rejoins the pool healthy).
                # The caller gets the error + a dead session; a NEW session on
                # this slot works immediately. One turn lost, not the slot.
                if getattr(instance, "_needs_context_refresh", False):
                    log.error(
                        "💥 Session %s fatal decode on a latched context — "
                        "scheduling session teardown + slot heal",
                        session_id,
                    )

                    # Deferred, NOT inline: at this point the failed turn's
                    # generator chain has not unwound, so its generation_guard
                    # is still counted in-flight — an inline end_session would
                    # stall the heal's drain until the 180s timeout (observed
                    # live). A task after a short delay runs once the stack
                    # has unwound and the guard is released.
                    async def _teardown(sid: str) -> None:
                        await asyncio.sleep(0.5)
                        try:
                            await self.end_session(sid)
                        except Exception:  # noqa: BLE001 — best-effort teardown
                            log.exception("deferred session teardown failed")

                    asyncio.get_running_loop().create_task(_teardown(session_id))
                raise
            finally:
                if not completed and not resident:
                    # Synchronous on purpose: this also runs under
                    # GeneratorExit, where awaiting is not an option. The next
                    # turn's invariant check does the actual repair.
                    session.kv_dirty = True

    def _replay_kv_matches(self, instance: Any, session: SessionState) -> bool:
        """Whether a replay session's live KV is [persona static] + history.

        Backend doubles without the invariant fall back to the old heuristic
        (non-empty KV and no dirty flag)."""
        check = getattr(self._backend, "session_kv_matches", None)
        if check is None:
            return (
                int(getattr(instance, "n_tokens", 0) or 0) > 0 and not session.kv_dirty
            )
        return bool(check(instance, session.token_history))

    async def _restore_replay_static(self, instance: Any) -> None:
        """Restore the instance's own persona static (see
        backend.restore_session_static); doubles without it use the old
        default-persona blob."""
        restore = getattr(self._backend, "restore_session_static", None)
        if restore is not None:
            await run_in_threadpool(restore, instance)
            return
        static = getattr(self._backend, "static_state", None)
        if static is not None:
            await run_in_threadpool(instance.load_state, static)
        else:
            await run_in_threadpool(instance.reset)

    async def _rollback_to_history(
        self, session_id: str, instance: Any, session: SessionState
    ) -> bool:
        """Roll a diverged replay KV back to its last committed turn boundary.

        The boundary's checkpoint was taken when the failed turn started, so
        this restores the recurrent state and drops the stray tail — no replay.
        False (and the KV untouched, or reset) when there is no usable
        checkpoint; the caller then restores the static base and replays."""
        rollback = getattr(self._backend, "rollback_turn", None)
        base_len = getattr(self._backend, "session_base_len", None)
        if rollback is None or base_len is None:
            return False
        boundary = int(base_len(instance)) + len(session.token_history)
        if not await run_in_threadpool(rollback, instance, boundary):
            return False
        if not self._replay_kv_matches(instance, session):
            return False
        log.info(
            "⏪ Session %s: rolled the KV back to the last committed turn "
            "(pos %d) — no replay",
            session_id,
            boundary,
        )
        return True

    async def _commit_replay_turn(
        self,
        session_id: str,
        session: SessionState,
        instance: Any,
        turn_only: list,
        raw: str,
        pre_turn_pos: int,
        config: Any,
    ) -> None:
        """Commit a finished replay turn to token_history — as whatever the
        live KV now holds.

        Without a turn checkpoint (no rollback on this model/turn) the raw turn
        is committed, as always. With one:

        * an ANSWERLESS turn — a prefilled think that never closed (cut at the
          budget or the context ceiling), or a closed think with nothing after
          it — is rolled back out of the context entirely (drop_answerless_turns,
          auto-on here). Committed, it sat under every later turn as a
          context-sized non-answer: the 2026-09-22 engine.py turn was 78,120
          tokens of unfinished thinking.
        * with strip_prior_reasoning, a turn whose reasoning CLOSED is rolled
          back and re-evaluated as its prompt up to the reasoning plus its clean
          answer — the template's no-reasoning form for a prior turn. Only this
          turn's own tokens are re-evaluated; the history is never replayed.
        """
        gen_in_kv = self._generated_in_kv(instance)
        if not getattr(instance, "_turn_ckpt_ok", False):
            session.token_history.extend(turn_only)
            session.token_history.extend(gen_in_kv)
            session.kv_dirty = False
            return

        family = config.model.family
        renderer = _get_format_renderer(family)
        delim = getattr(renderer, "delimiter_pattern", lambda: "")()
        if delim:
            from core.fsm_labeller import fsm_extract_phases

            content = fsm_extract_phases(
                raw, family=family, prefilled_think=session.last_prefilled_think
            ).get("C", "")
        else:
            content = raw
        content = content.strip()

        drop = getattr(config.model, "drop_answerless_turns", None)
        if not content and (drop is None or drop):
            ok = await run_in_threadpool(
                self._backend.rollback_turn, instance, pre_turn_pos
            )
            session.last_turn_dropped = True
            # The turn never enters history either way; a failed rollback just
            # leaves the invariant to force a replay next turn.
            session.kv_dirty = not ok
            log.warning(
                "🗑 Session %s: answerless turn (%d generated tokens%s) %s",
                session_id,
                len(gen_in_kv),
                ", unterminated think" if session.last_prefilled_think else "",
                "rolled out of the context" if ok else "NOT rolled back — replaying",
            )
            return

        if _think_strip_enabled() and strip_prior_reasoning_effective(config.model):
            stripped = await self._strip_replay_turn(
                session, instance, turn_only, content, pre_turn_pos, config
            )
            if stripped is not None:
                session.token_history.extend(stripped)
                session.last_turn_stripped = True
                return

        session.token_history.extend(turn_only)
        session.token_history.extend(gen_in_kv)
        session.kv_dirty = False

    async def _strip_replay_turn(
        self,
        session: SessionState,
        instance: Any,
        turn_only: list,
        content: str,
        pre_turn_pos: int,
        config: Any,
    ) -> list | None:
        """Strip this turn's reasoning by rollback + replay of the turn's own
        tokens. Returns the tokens now committed after ``pre_turn_pos`` (or
        None: nothing was stripped and the raw turn is still in the KV)."""
        gen_tokens = list(getattr(instance, "_last_completion_tokens", None) or [])
        gen_start = getattr(instance, "_last_gen_start_pos", None)
        if not gen_tokens or gen_start is None:
            return None
        if pre_turn_pos + len(turn_only) != int(gen_start):
            return None  # the turn prompt does not end where generation began
        think_on = bool(getattr(config.model, "thinking_available", True)) and (
            config.model.thinking != "off"
        )
        tokenizer = get_cached_tokenizer()
        span = reasoning_span(
            config.model.family, think_on, gen_tokens, int(gen_start), tokenizer
        )
        if span is None:
            return None  # no closed reasoning (or the model opened its own)
        t0, prefix = span
        n_kept = t0 - pre_turn_pos
        if not 0 <= n_kept <= len(turn_only):
            return None
        kept = list(turn_only[:n_kept])
        replay = tokenize_segments(tokenizer, [(prefix, True), (content, False)])
        new_tail = kept + list(replay)
        outcome = await run_in_threadpool(
            self._backend.strip_turn, instance, pre_turn_pos, new_tail
        )
        if outcome == "untouched":
            return None
        # "reset": the context is empty; the history still takes the stripped
        # form and the next turn replays it.
        session.kv_dirty = outcome != "stripped"
        log.info(
            "🧹 turn reasoning stripped by rollback: %d generated → %d replayed "
            "(%s)",
            len(gen_tokens),
            len(new_tail),
            outcome,
        )
        return new_tail

    @staticmethod
    def _generated_in_kv(instance: Any) -> list:
        """The generated ids the live KV actually holds.

        The binding's generate YIELDS a token and evaluates it only when the
        consumer resumes, so a loop that stops on the budget or a stop text
        leaves the last yielded token unevaluated. Committing it to history
        anyway made token_history one token longer than the KV, and every
        later append sat at the wrong position relative to a replay of the
        same history."""
        gen = list(getattr(instance, "_last_completion_tokens", None) or [])
        start = getattr(instance, "_last_gen_start_pos", None)
        if start is None or not isinstance(getattr(instance, "n_tokens", None), int):
            return gen
        in_kv = int(instance.n_tokens) - int(start)
        if in_kv in (len(gen), len(gen) - 1) and in_kv >= 0:
            return gen[:in_kv]
        log.warning(
            "🧩 generated span mismatch: %d generated, %d in KV — committing "
            "the generated ids; the next turn's invariant check will replay",
            len(gen),
            in_kv,
        )
        return gen

    async def _maybe_strip_reasoning(self, instance: Any, content: str) -> None:
        """Strip this turn's reasoning from the KV via truncate-and-replay
        (Factor 4): drop the reasoning + raw answer from ``t0`` to the end, then
        re-eval the canonical clean answer there. No-op when there's nothing to
        strip (non-thinking model/turn, or a truncated turn with no marker).
        """
        gen_tokens = getattr(instance, "_last_completion_tokens", None)
        p = getattr(instance, "_last_gen_start_pos", None)
        if not gen_tokens or p is None:
            return
        if not (content or "").strip():
            # The turn was truncated before emitting a final-channel answer (e.g.
            # max_tokens hit mid-analysis) — there is no clean answer to replay.
            # Stripping here would replace the raw output with an EMPTY final
            # channel, writing an answerless assistant turn into the KV (which
            # compounds across turns). Keep the raw generation instead.
            return
        config = self._backend.config
        # Ternary-aware (2026-08-03): "off" is a truthy STRING — collapse the
        # policy to the bool this function wants. Availability also gates:
        # a model that cannot think has no span to strip.
        _think_on = (
            bool(getattr(config.model, "thinking_available", True))
            and config.model.thinking != "off"
        )
        span = reasoning_span(
            config.model.family,
            _think_on,
            gen_tokens,
            p,
            get_cached_tokenizer(),
        )
        if span is None:
            return
        t0, prefix = span
        replay = tokenize_segments(
            get_cached_tokenizer(), [(prefix, True), (content or "", False)]
        )
        ok = await run_in_threadpool(
            self._backend.strip_reasoning_replay, instance, t0, replay
        )
        if ok:
            log.info(
                "🧹 reasoning strip: truncate@%d, replay %d answer tokens",
                t0,
                len(replay),
            )
            import os as _os

            if _os.getenv("OURO_RESIDENT_STRIP") == "1":  # diagnostic
                try:
                    gen_txt = instance.detokenize(list(gen_tokens)).decode(
                        "utf-8", "replace"
                    )
                    nt = instance.n_tokens
                    tail_ids = list(instance.input_ids[max(0, nt - 110) : nt])
                    tail = instance.detokenize(tail_ids).decode("utf-8", "replace")
                    log.warning(
                        "🔬 STRIP DIAG t0=%d nt=%d | content=%r | gen=%r | post-tail=%r",
                        t0,
                        nt,
                        (content or "")[:70],
                        gen_txt[:110],
                        tail[-230:],
                    )
                except Exception as ex:  # noqa: BLE001
                    log.warning("strip diag failed: %s", ex)

    async def session_turn_complete(
        self,
        session_id: str,
        prompt: str,
        max_tokens: int = 256,
        temperature: float = 0.7,
        grammar: str | None = None,
        reasoning: str | None = None,
    ) -> tuple[str, int, dict]:
        """Non-streaming session turn — returns full response.

        Collects raw output from the streaming generator, then runs
        the FSM-based _strip_delimiter on the full text. This gives
        the labeller complete context for accurate phase labeling.

        Handles two model behaviors:
          A) thinking <delimiter> response  → return response, capture thinking
          B) response <delimiter>           → return response (delimiter is end-of-turn)

        Returns:
            Tuple of (generated_text, generated_token_count). The token
            count is the number of chunks yielded by the streaming
            backend — each chunk corresponds to one generated token
            before FSM/delimiter stripping. This is the right signal
            for the caller to detect truncation (count >= max_tokens
            means the generation budget was reached), distinct from
            the post-strip character-based approximation that previous
            versions of this method returned.
        """
        raw_parts: list[str] = []
        gen_cfg = self._backend.config.generation
        attempt = 0
        while True:
            try:
                overrides = None
                turn_temp = temperature
                if attempt == 1:
                    # Recovery recipe: the vendor's own fix for the Qwen3
                    # endless-repetition failure (temp 1.0 + presence 1.5),
                    # with the penalty window widened so presence actually
                    # sees a paragraph-scale cycle. The degenerate span is gone
                    # before this re-drive decodes — purged in place (resident)
                    # or rolled back to the turn boundary at the retry's start
                    # (replay: a checkpoint restore, else a replay) — so the
                    # SAME turn re-runs on clean pre-turn state.
                    turn_temp = float(gen_cfg.degen_retry_temperature or 1.0)
                    overrides = {
                        "present_penalty": float(
                            gen_cfg.degen_retry_presence_penalty or 1.5
                        ),
                        "penalty_last_n": int(gen_cfg.penalty_last_n or 2048),
                    }
                async for chunk in self.session_turn(
                    session_id,
                    prompt,
                    max_tokens,
                    turn_temp,
                    grammar,
                    reasoning=reasoning,
                    sampling_overrides=overrides,
                ):
                    raw_parts.append(chunk)
                break
            except DegenerateGenerationError as e:
                if attempt >= 1 or not gen_cfg.degen_retry_enabled:
                    raise
                attempt += 1
                raw_parts.clear()
                log.warning(
                    "🔁 Session %s degenerate turn (%s) — retrying once at "
                    "recovery recipe (temp=%.2f, presence=%.2f, window=%d)",
                    session_id,
                    e.reason,
                    float(gen_cfg.degen_retry_temperature or 1.0),
                    float(gen_cfg.degen_retry_presence_penalty or 1.5),
                    int(gen_cfg.penalty_last_n or 2048),
                )

        raw_text = "".join(raw_parts)
        generated_tokens = len(raw_parts)

        # Cache-aware token telemetry from the session instance's per-request
        # stash (read-only). cached_prefix = full restored KV occupancy (static
        # + all prior turns — what the model skipped prefilling this turn);
        # fresh_prefill = the new turn's prefilled tokens. generated_tokens here
        # is the REAL completion count (vs the chunk-count proxy returned for
        # truncation). Empty when the instance didn't stash (degrades cleanly).
        _sess = self._sessions.get(session_id)
        _inst = getattr(_sess, "instance", None)
        _cached = int(getattr(_inst, "_last_kv_base", 0) or 0)
        _fresh = int(getattr(_inst, "_last_dynamic_len", 0) or 0)
        from core.generation_tracker import get_tracker

        _diag = get_tracker().get_last_diagnostics()
        cache = {
            "prompt_tokens": _cached + _fresh,
            "cached_prefix_tokens": _cached,
            "fresh_prefill_tokens": _fresh,
            "generated_tokens": len(
                getattr(_inst, "_last_completion_tokens", []) or []
            ),
            "cache_hit": bool(getattr(_inst, "_last_cache_hit", False)),
            "flow_key": str(getattr(_inst, "_last_flow_key", "") or ""),
            # Engine-side stop reason. A KV-pressure force-window ends the turn
            # BELOW max_tokens, so the caller's `tokens >= max_tokens` test
            # cannot see it and would read a severed turn as complete.
            "end_reason": str(getattr(_inst, "_last_end_reason", "") or ""),
            # The temperature the turn ACTUALLY sampled at (global floor,
            # session-depth floor and degenerate-retry recipe all applied).
            "temperature": getattr(_inst, "_last_temperature", None),
            # The budget the ENGINE enforced (min(max_tokens, context left)) and,
            # on a budget stop, which of the two bound — "context" means the
            # session itself ran out of window, not the caller's allowance.
            "effective_max": getattr(_inst, "_last_effective_max", None),
            "length_cause": str(getattr(_inst, "_last_length_cause", "") or ""),
            # Replay sessions with turn rollback: this turn's reasoning was
            # stripped from the context, or — it produced no answer — the whole
            # turn was rolled back out of it (and never entered history).
            "turn_stripped": bool(getattr(_sess, "last_turn_stripped", False)),
            "turn_dropped": bool(getattr(_sess, "last_turn_dropped", False)),
            # The id a caller passes to rewindSessionTurn to take this turn back.
            "turn_id": getattr(getattr(_sess, "last_turn", None), "turn_id", None),
            # Prefer the per-stream wall spans (batched seats stash them —
            # concurrency-accurate); the global tracker's single-generation
            # timing is only correct when one stream runs at a time (pool).
            # Mirrors run_completion; without this, batched SESSION turns
            # reported another stream's phase timing under concurrency.
            "prefill_ms": round(
                (
                    getattr(_inst, "_last_prefill_s", 0)
                    or _diag.get("eval_duration", 0)
                    or 0
                )
                * 1000,
                1,
            ),
            "decode_ms": round(
                (
                    getattr(_inst, "_last_decode_s", 0)
                    or _diag.get("generation_duration", 0)
                    or 0
                )
                * 1000,
                1,
            ),
        }

        # Capture raw output for training before any post-processing
        from core.interaction_logger import log_raw_generation

        log_raw_generation(
            raw_text=raw_text,
            tokens_generated=generated_tokens,
            stop_reason="session_turn",
            prompt_text=prompt,
        )

        config = self._backend.config
        delim = _get_format_renderer(config.model.family).delimiter_pattern()
        if not delim:
            text = raw_text.strip()
            return text, generated_tokens, cache

        # FSM-based delimiter stripping on the full raw output.
        # The FSM labels each atom as D/T/C/E based on current phase
        # and extracts only the content phase. Thinking is forwarded to
        # the GenerationTracker side-channel.
        from core.inference import _strip_delimiter

        text = _strip_delimiter(
            raw_text,
            prefilled_think=getattr(_sess, "last_prefilled_think", None),
        )

        log.info(
            "Session %s turn complete: raw=%d chars / %d tokens → content=%d chars",
            session_id,
            len(raw_text),
            generated_tokens,
            len(text) if text else 0,
        )

        # Truncation flag — distinguishes "empty response because the model
        # chose to say nothing" from "empty response because the budget ran
        # out mid-analysis and the FSM stripped the unfinished reasoning".
        # The chunk count against max_tokens is only HALF the test: the
        # engine's budget is min(max_tokens, context remaining), so a turn cut
        # at the context ceiling stops far below max_tokens and only the
        # engine's end_reason can say so (2026-09-22: 78,120 of 262,144, logged
        # untruncated, and its unfinished thinking was mined for a file).
        truncated = generated_tokens >= max_tokens or cache["end_reason"] in (
            "length",
            "kv_pressure_truncated",
        )
        cache["truncated"] = truncated

        # Log the session interaction so it appears in interactions.jsonl.

        # The FSM forwarded thinking to the tracker a few lines above; read it
        # back the same way the stateless path does (core/inference.py). Without
        # this, EVERY session turn logs `thinking` absent while the stateless
        # path logs it populated — a split by CODE PATH, not by model behaviour.
        # It reads exactly like a model that stopped emitting a thinking
        # channel, and on 2026-09-22 it was analysed as one: a qwen4exp walk
        # turn whose 151,595 chars of CoT were sitting in `raw_text` the whole
        # time got written up as "the model reasons in the open content
        # channel". The channel was fine; the log was half-blind.
        #
        # raw_length now decomposes: closed thinking gives
        # raw == thinking + content, while an UNTERMINATED thinking phase gives
        # thinking absent against a large raw — the two are finally
        # distinguishable from the log alone, which is the case that matters
        # (a turn that never closes its phase hands its whole deliberation to
        # the file extractor).
        from core.generation_tracker import get_tracker

        _think = (get_tracker().get_thinking() or {}).get("content", "") or ""
        _extra = {
            "raw_text": raw_text,
            "raw_length": len(raw_text),
            "extracted_length": len(text),
            "generated_tokens": generated_tokens,
            "max_tokens": max_tokens,
            "truncated": truncated,
            "end_reason": cache["end_reason"],
            "effective_max": cache["effective_max"],
            "length_cause": cache["length_cause"],
            "turn_stripped": cache["turn_stripped"],
            "turn_dropped": cache["turn_dropped"],
            "delimiter_configured": bool(delim),
        }
        if _think:
            # Same 200k cap as the stateless path — bounds file growth on
            # marathon thinks without losing the shape of one.
            _extra["thinking"] = _think[:200_000]
        log_interaction(
            prompt=prompt,
            response=text,
            mode=f"session:{session_id}:turn{_sess.turn_count if _sess else '?'}",
            extra=_extra,
        )

        return text, generated_tokens, cache

    async def rewind_turn(self, session_id: str, turn_id: int) -> dict:
        """Take the session's LAST turn back, by id — one level of undo.

        Replay sessions roll the KV back to the turn's boundary (a hybrid
        restores the boundary's recurrent checkpoint; no checkpoint → the
        history is truncated and the next turn replays it). Resident sessions
        tail-remove the turn's span, which a memory may refuse. A dropped or
        already-rewound turn is "already_absent" — nothing to take back.
        Refused while a turn is generating, and for any id but the last."""
        session = self._sessions.get(session_id)
        if session is None:
            raise ValueError(f"Session {session_id} not found")

        def result(ok: bool, reason: str) -> dict:
            return {
                "ok": ok,
                "reason": reason,
                "turn_count": session.turn_count,
                "tokens": len(session.token_history),
            }

        if session.in_flight:
            return result(False, "in_flight")
        mark = session.last_turn
        if mark is None or mark.turn_id != int(turn_id):
            return result(False, "not_last_turn")
        if mark.dropped or mark.rewound or not mark.committed:
            return result(True, "already_absent")

        instance = session.instance
        resident = bool(getattr(self._backend, "_resident_active", False))
        async with self._generation_guard():
            if resident:
                if hasattr(instance, "purge_to"):
                    await run_in_threadpool(instance.purge_to, mark.pre_turn_pos)
                else:

                    def _tail_rm() -> bool:
                        if (
                            instance._ctx.memory_seq_rm(0, mark.pre_turn_pos, -1)
                            is False
                        ):
                            return False
                        instance.n_tokens = mark.pre_turn_pos
                        return True

                    if not await run_in_threadpool(_tail_rm):
                        return result(False, "refused")
                how = "rolled_back"
            else:
                rollback = getattr(self._backend, "rollback_turn", None)
                ok = bool(
                    rollback is not None
                    and await run_in_threadpool(rollback, instance, mark.pre_turn_pos)
                )
                del session.token_history[mark.history_len :]
                if not ok:
                    session.kv_dirty = True  # the next turn restores + replays
                how = "rolled_back" if ok else "history_truncated"
            session.turn_count = mark.turn_count
            mark.rewound = True
        log.info(
            "⏪ Session %s: turn %d rewound (%s) — turn_count=%d",
            session_id,
            mark.turn_id,
            how,
            session.turn_count,
        )
        return result(True, how)

    async def session_snapshot(self, session_id: str, key: str) -> dict:
        """Pin the session's current context under ``key`` (semi-permanent).

        Survives session end and TTL expiry; freed only by purge_snapshot.
        Resident backends pin the live KV seq (hot fork source) AND record
        the token stream (cold rebuild source); non-resident/recurrent
        backends record the token history only — from_snapshot then pays a
        re-prefill, same API. The session becomes snapshot-linked: windowing
        is forbidden from here on (shared cells).
        """
        session = self._sessions.get(session_id)
        if session is None:
            raise KeyError(f"unknown session {session_id}")
        resident = bool(getattr(self._backend, "_resident_active", False))
        async with self._generation_guard():
            if resident:
                info = await run_in_threadpool(
                    self._backend.snapshot_working_seq, session.instance, key
                )
            else:
                info = self._backend.register_replay_snapshot(
                    key, list(session.token_history)
                )
        # The captured KV/history ends at a CLOSED turn — record the depth so
        # forked sessions render the turn transition correctly.
        self._backend._snap_registry[key]["turn_count"] = session.turn_count
        session.snapshot_key = key
        log.info(
            "📸 Session %s snapshotted as %r (%d tokens, turn %d, %s)",
            session_id,
            key,
            info.get("tokens", 0),
            session.turn_count,
            "resident" if info.get("resident") else "replay",
        )
        return {
            "key": key,
            "tokens": int(info.get("tokens", 0)),
            "resident": bool(info.get("resident")),
            "turn_count": session.turn_count,
        }

    async def purge_snapshot(self, key: str) -> bool:
        """Free a pinned snapshot everywhere (the explicit-release call)."""
        async with self._generation_guard():
            return await run_in_threadpool(self._backend.purge_snapshot, key)

    def list_snapshots(self) -> list:
        return self._backend.list_snapshots()

    async def expire_all_sessions(self, reason: str) -> int:
        """Force-expire every live session through the normal expiry path.

        Used by the context auto-refresh when sessions outlive the drain
        window: listeners get an "expired" event (so agent-side session
        machinery sees a clean expiry, not a vanished seat) and the seat is
        released via end_session. Returns the number expired.
        """
        ids = list(self._sessions.keys())
        for sid in ids:
            session = self._sessions.get(sid)
            if session is None:
                continue
            log.warning("⏰ Session %s force-expired (%s)", sid, reason)
            if session.listener is not None:
                await session.listener.put(
                    SessionEvent(
                        session_id=sid,
                        event_type="expired",
                        message=f"Session expired: {reason}",
                    )
                )
            await self.end_session(sid)
        return len(ids)

    async def end_session(self, session_id: str) -> bool:
        """Release the pinned instance and clean up."""
        session = self._sessions.pop(session_id, None)
        if session is None:
            return False

        # Cancel TTL timer
        task = self._expiry_tasks.pop(session_id, None)
        if task:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

        # Wake any session_events subscriber with a terminal event — the
        # normal explicit-end path previously pushed nothing, parking the
        # subscription task on queue.get() for the websocket's lifetime
        # (memory audit: per-session task + queue accumulation).
        if session.listener is not None:
            with contextlib.suppress(Exception):
                session.listener.put_nowait(
                    SessionEvent(
                        session_id=session_id,
                        event_type="closed",
                        message="session ended",
                    )
                )

        # Release instance back to pool
        await self._backend.release_instance(session.instance)

        log.info(
            "📌 Session %s ended (turns=%d, duration=%.1fs)",
            session_id,
            session.turn_count,
            time.monotonic() - session.created_at,
        )
        return True

    async def register_listener(self, session_id: str) -> Optional[asyncio.Queue]:
        """Register a listener queue for session events (TTL expiry)."""
        session = self._sessions.get(session_id)
        if session is None:
            return None
        queue: asyncio.Queue = asyncio.Queue()
        session.listener = queue
        return queue

    async def _ttl_monitor(self, session_id: str, ttl: int) -> None:
        """Monitor session TTL. Notify if listener exists, else silent cleanup."""
        try:
            while True:
                await asyncio.sleep(ttl)
                session = self._sessions.get(session_id)
                if session is None:
                    return

                # A turn in flight is the opposite of idle. Skip and re-arm:
                # the next sleep gives the turn another full TTL, and the
                # in_flight exit stamps last_turn_at so the window that
                # actually decides expiry starts when the turn ENDS.
                if session.in_flight:
                    continue

                elapsed = time.monotonic() - session.last_turn_at
                if elapsed >= ttl:
                    log.warning(
                        "⏰ Session %s expired (TTL=%ds, idle=%.1fs, turns=%d)",
                        session_id,
                        ttl,
                        elapsed,
                        session.turn_count,
                    )
                    if session.listener is not None:
                        event = SessionEvent(
                            session_id=session_id,
                            event_type="expired",
                            message=f"Session expired after {ttl}s inactivity",
                        )
                        await session.listener.put(event)
                    await self.end_session(session_id)
                    return
        except asyncio.CancelledError:
            return

    def _ensure_orphan_reaper(self) -> None:
        """Start the orphan reaper if not already running. Lazily started from
        start_session (it needs a running event loop, unlike __init__)."""
        if self._orphan_reaper_task is None or self._orphan_reaper_task.done():
            self._orphan_reaper_task = asyncio.create_task(self._orphan_reaper())

    async def _orphan_reaper(self) -> None:
        """Crash-proof backstop for the per-session TTL monitors.

        Each session's _ttl_monitor reclaims its instance after ~TTL of inactivity,
        but it is a fire-and-forget task guarded only against CancelledError — an
        unexpected exception in its body would kill it silently and leak the pinned
        instance forever. This central sweep (every 60s) force-ends any session idle
        beyond 2x its TTL — the unambiguous signal that its monitor died — so a
        standard client kill (pkill) can never permanently wedge the pool.

        Reclaim-safe: 2x TTL (>= 240s) far exceeds any single generation (tens of
        seconds), and it additionally skips the sweep whenever the backend reports an
        active generation, so it never releases an instance that is mid-decode.
        """
        try:
            while True:
                await asyncio.sleep(60)
                # Never reclaim while GPU work is in flight. (Single-instance: a held
                # dead session blocks all generation, so this reads 0 and we proceed.)
                if getattr(self._backend, "_active_generations", 0) > 0:
                    continue
                now = time.monotonic()
                for sid, session in list(self._sessions.items()):
                    # Per-session belt to the backend-wide braces above: a
                    # batched seat can decode for this session while
                    # _active_generations reads 0 for another.
                    if session.in_flight:
                        continue
                    idle = now - session.last_turn_at
                    if idle > 2 * session.ttl:
                        log.warning(
                            "🧹 Orphan reaper: session %s idle %.0fs > 2x TTL (%ds) "
                            "— TTL monitor likely died; force-releasing instance",
                            sid,
                            idle,
                            session.ttl,
                        )
                        try:
                            await self.end_session(sid)
                        except Exception:
                            log.exception("Orphan reaper failed to end session %s", sid)
                # Crash insurance for the semi-permanent tier: snapshots
                # survive session end/TTL/refresh by design, so a client
                # that dies between snapshot and purge would hold its
                # registry entry (and capacity slot) forever. Sweep by
                # age (config: model.session_snapshot_ttl_s; 0 = never).
                sweep = getattr(self._backend, "sweep_stale_snapshots", None)
                if sweep is not None:
                    try:
                        ttl = float(
                            getattr(
                                self._backend.config.model,
                                "session_snapshot_ttl_s",
                                7200,
                            )
                            or 0
                        )
                        sweep(ttl)
                    except Exception:
                        log.exception("stale-snapshot sweep failed")
        except asyncio.CancelledError:
            return

    async def shutdown(self) -> None:
        """End all active sessions during server shutdown."""
        if self._orphan_reaper_task is not None:
            self._orphan_reaper_task.cancel()
            try:
                await self._orphan_reaper_task
            except asyncio.CancelledError:
                pass
            self._orphan_reaper_task = None
        for sid in list(self._sessions.keys()):
            await self.end_session(sid)
