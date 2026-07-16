"""terminal-bench harness adapter for Ouroboros (ops mission per task)."""

# OuroborosAgent needs the `terminal_bench` package (BaseAgent/TmuxSession).
# ContainerEffects (adapters.tb.container_effects) does NOT — it imports only
# agent.* — and adapters.swe reuses it. Import the agent lazily so
# `from adapters.tb.container_effects import ContainerEffects` never requires
# terminal_bench to be installed.
__all__ = ["OuroborosAgent"]


def __getattr__(name):
    if name == "OuroborosAgent":
        from adapters.tb.agent import OuroborosAgent

        return OuroborosAgent
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
