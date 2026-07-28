"""Health must say WHOSE generation it is describing.

WHY. ``GenerationTracker`` is a singleton with ONE ``GenerationStatus`` slot,
and the health endpoint published ``tokens_generated`` / ``generation_active``
/ ``generation_phase`` without ever attributing them. A polling client had no
way to tell its own generation's numbers from whatever the slot happened to
hold — so on 2026-07-28 six agent requests queued behind one orphaned
generation each read the orphan's counter (52,120, reported identically four
times) and cancelled themselves having produced nothing.

The tracker has ALWAYS returned ``request_id`` and ``thinking_complete`` from
``get_status()``. They were simply dropped at the API boundary. These tests pin
the two ends of that wire: the tracker keeps reporting them, and the resolver
maps them onto ``HealthStatus`` — plus the field that lets a client set the id
in the first place.

``thinking_complete`` matters for the same reason in the other direction: a
client that must reason about a large token count needs to know whether it is
looking at 46k tokens of chain-of-thought or 46k tokens of answer.
"""

from __future__ import annotations

import inspect

from core.generation_tracker import GenerationTracker


class TestTheTrackerReportsIdentity:
    def test_an_active_generation_names_its_request(self):
        t = GenerationTracker()
        t.start(request_id="req-42", prompt_tokens=10)
        t.record_token()
        status = t.get_status()
        assert status["request_id"] == "req-42"
        assert status["generation_active"] is True

    def test_thinking_completion_is_reported_while_active(self):
        """False until the delimiter, True after — the signal that separates
        chain-of-thought volume from answer volume."""
        t = GenerationTracker()
        t.start(request_id="req-42", prompt_tokens=10)
        t.record_token()
        assert t.get_status()["thinking_complete"] is False
        t.mark_thinking_complete()
        assert t.get_status()["thinking_complete"] is True

    def test_an_unlabelled_generation_reports_an_empty_id(self):
        """Not a fabricated one. A client must be able to tell "no identity"
        from "an identity that isn't yours" — matching on a guess is the
        failure this whole change exists to prevent."""
        t = GenerationTracker()
        t.start(prompt_tokens=10)
        assert t.get_status()["request_id"] == ""

    def test_a_new_generation_replaces_the_previous_id(self):
        """The single-slot reset, made explicit: this is exactly why a client
        cannot assume an unattributed snapshot is its own."""
        t = GenerationTracker()
        t.start(request_id="first", prompt_tokens=10)
        t.record_token()
        t.start(request_id="second", prompt_tokens=10)
        assert t.get_status()["request_id"] == "second"


class TestTheApiSurfaceCarriesItThrough:
    """The fields existed in ``get_status()`` for a long time and never reached
    a client. These guard the boundary that dropped them."""

    def test_health_status_declares_both_fields(self):
        from api.graphql_api import HealthStatus

        annotations = HealthStatus.__annotations__
        assert "request_id" in annotations
        assert "thinking_complete" in annotations

    def test_the_resolver_maps_both_from_the_tracker(self):
        """A field declared but never populated is silently always-default —
        how ``flash_attn`` and ``batch_size`` were no-ops for weeks."""
        from api import graphql_api

        src = inspect.getsource(graphql_api.Query.health)
        assert 'request_id=tracker_status.get("request_id"' in src
        assert 'thinking_complete=tracker_status.get("thinking_complete")' in src

    def test_a_completion_request_can_set_the_id(self):
        """Without an input field the id can only ever be empty, and the whole
        identity gate degrades to the fallback path for every request."""
        from api.graphql_api import CompletionRequest

        assert "request_id" in CompletionRequest.__annotations__

    def test_the_completion_resolver_forwards_it(self):
        from api import graphql_api

        src = inspect.getsource(graphql_api.Query.completion)
        assert "request_id=request.request_id" in src

    def test_run_completion_accepts_and_forwards_it(self):
        """The thread has to reach the backend kwarg the tracker reads
        (``kwargs.get("request_id")``); an accepted-but-dropped parameter looks
        identical from the API and reports nothing."""
        from core import inference

        assert "request_id" in inspect.signature(inference.run_completion).parameters
        src = inspect.getsource(inference.run_completion)
        assert 'gen_kwargs["request_id"]' in src

    def test_session_turns_are_labelled_with_the_session_id(self):
        """Sessions need no new field — turns are sequential and single-driver,
        so the session id already identifies the generation uniquely. A client
        polling for session X matches on X."""
        from core import session_manager

        src = inspect.getsource(session_manager.SessionManager.session_turn)
        assert 'gen_kwargs["request_id"] = session_id' in src
