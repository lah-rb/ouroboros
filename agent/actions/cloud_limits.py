"""Provider-limit detection shared by every cloud-routed lane.

A subscription-metered provider (the ``claude_cli`` boss/synth route) answers
a limit not with an HTTP status the effects layer can see but with TEXT --
"You've hit your usage limit", "session limit reached", a 429 body. The
binder packer (dev/pack_binder_sonnet.py) learned to latch on that text and
stop claiming work; this is that regex, promoted so the synthesis flow set
(and any later cloud lane) latches the same way instead of growing a copy.

A latch is the right shape because a limit is not transient at the scale of
a round: retrying spends the operator's quota on failures.
"""

from __future__ import annotations

import re

PROVIDER_LIMIT_RE = re.compile(
    r"session limit|usage limit|rate limit|quota (?:exceeded|reached)|"
    r"\b429\b|too many requests|overloaded",
    re.IGNORECASE,
)


def is_provider_limit(text: str | None) -> bool:
    """True when a provider's reply or error text says the account/route is
    at a limit and further calls should stop for now."""
    return bool(text) and bool(PROVIDER_LIMIT_RE.search(str(text)))
