"""The mission's per-turn history: parquet rows + git tree snapshots.

Everything a mission does is recorded here, under ``<workspace>/.agent/history/``:

  * ``turns/``   — one row per inference call: the FULL prompt, response and
                   thinking, every id the server returns, every metric.
  * ``events/``  — every other trace event (cycles, steps, commands, MCP,
                   sessions, health).
  * ``commits/`` — one row per workspace tree change, keyed by the commit in
                   ``repo.git`` (a bare dulwich object store; the workspace
                   itself carries no ``.git``).
  * ``runs/``    — one row per process start, closed with the finite-time
                   summary.

``store.HistoryStore`` writes, ``reader`` reads, ``snapshot`` versions the
tree, ``rollback`` restores it, ``replay`` re-issues a recorded prompt.
"""

from agent.history.schema import SCHEMA_VERSION

__all__ = ["SCHEMA_VERSION"]
