"""Phase 0 — static structural proof of session KV malformation.

No model required. For each family, reconstruct the EXACT text that
accumulates in the KV cache across a multi-turn memoryful session, using
the same renderer methods the session manager uses:

  turn 0 : render_user(u) + render_generation_prompt()      -> model emits a0
  turn N : render_turn_transition() + render_user(u) + render_generation_prompt()

Key backend fact (llama_cpp_backend.py:935-948): generation breaks on the
EOG/stop token BEFORE it is eval'd into KV, so the model's own closing token
(gen_stop) is NEVER in the KV cache. The previous assistant turn is closed
ONLY by whatever render_turn_transition() prepends next turn.

We compare that against the CANONICAL multi-turn rendering (render_assistant_history,
which uses proper closes) to show exactly what is missing.
"""

from formats.registry import get_renderer

FAMILIES = ["harmony", "chatml", "tekken"]


# Deterministic stand-ins for what the model generates each turn. We model a
# thinking model: reasoning then a short JSON answer. Critically we do NOT
# append the gen_stop token, because the backend never evals it into KV.
def simulated_generation(renderer, reasoning: str, answer: str) -> str:
    s = renderer.s
    if s.thinking.style == "channel":
        # Harmony: analysis channel then final channel (no <|return|> in KV)
        return (
            f"{s.thinking.channel_token}{s.thinking.channel_name}{s.tokens.msg_content}"
            f"{reasoning}{s.tokens.msg_close}"
            f"{s.tokens.msg_open}{s.roles['assistant']}"
            f"{s.thinking.channel_token}{s.thinking.content_channel}{s.tokens.msg_content}"
            f"{answer}"
        )
    if s.thinking.style == "inline_tags" and s.thinking.open_tag:
        # ChatML/Tekken thinking: <think>...</think> then answer (no gen_stop in KV)
        return f"{s.thinking.open_tag}\n{reasoning}\n{s.thinking.close_tag}\n{answer}"
    return answer


def build_session_kv(family: str, turns: list[tuple[str, str, str]]) -> str:
    """Reconstruct what the SESSION path accumulates in KV across turns."""
    r = get_renderer(family)
    parts: list[str] = []
    for i, (user, reasoning, answer) in enumerate(turns):
        if i == 0:
            parts.append(r.render_user(user) + r.render_generation_prompt())
        else:
            parts.append(
                r.render_turn_transition()
                + r.render_user(user)
                + r.render_generation_prompt()
            )
        parts.append(simulated_generation(r, reasoning, answer))
    return "".join(parts)


def build_canonical_kv(family: str, turns: list[tuple[str, str, str]]) -> str:
    """Reconstruct the CORRECT multi-turn rendering (proper closes, thinking dropped
    from prior turns per family best-practice) using render_assistant_history."""
    r = get_renderer(family)
    parts: list[str] = []
    for i, (user, reasoning, answer) in enumerate(turns):
        parts.append(r.render_user(user))
        if i < len(turns) - 1:
            # prior assistant turn, properly closed, thinking dropped
            parts.append(r.render_assistant_history(answer, thinking=None))
        else:
            parts.append(r.render_generation_prompt())  # current turn: prompt only
    return "".join(parts)


def show(label: str, text: str) -> None:
    print(f"\n----- {label} -----")
    # Make the raw special tokens visible and mark each newline
    print(repr(text))


TURNS = [
    ("u1: pick an action", "the obvious choice is trace", '{"choice":"trace"}'),
    ("u2: pick an action", "now conclude", '{"choice":"conclude"}'),
    ("u3: pick an action", "trace again", '{"choice":"trace"}'),
    ("u4: pick an action", "final answer", '{"choice":"conclude"}'),
]

for fam in FAMILIES:
    r = get_renderer(fam)
    s = r.s
    print("\n" + "=" * 78)
    print(
        f"FAMILY: {fam}   gen_stop={s.tokens.gen_stop!r}  "
        f"history_close={s.tokens.history_close!r}  "
        f"turn_transition={s.turn_transition.after_generation!r}  "
        f"needs_close={s.turn_transition.needs_close}"
    )
    print("=" * 78)

    sess = build_session_kv(fam, TURNS)
    canon = build_canonical_kv(fam, TURNS)

    show("SESSION KV (what the model actually sees, accumulated)", sess)
    show("CANONICAL KV (correct multi-turn, what plain chat sends)", canon)

    # Check every assistant->next-turn boundary for a proper close token
    close = s.tokens.history_close
    print(f"\n  Boundary audit (looking for {close!r} closing each assistant turn):")
    # The session string should contain N-1 transitions; count proper closes.
    # A correct multi-turn has (N-1) assistant turns each closed by history_close
    # before the next user opener.
    user_opener = (
        s.role_tokens.get("user").msg_open
        if s.role_tokens.get("user") and s.role_tokens["user"].msg_open
        else s.tokens.msg_open + s.roles.get("user", "")
    )
    sess_boundaries = sess.count(user_opener)  # number of user turns
    sess_closes = sess.count(close)
    canon_closes = canon.count(close)
    print(f"    user turns in session KV     : {sess_boundaries}")
    print(f"    {close!r} count in SESSION KV : {sess_closes}")
    print(f"    {close!r} count in CANONICAL  : {canon_closes}")
    verdict = (
        "OK (turns closed)"
        if sess_closes >= canon_closes
        else f"BROKEN — session KV is missing {canon_closes - sess_closes} close token(s)"
    )
    print(f"    VERDICT: {verdict}")
