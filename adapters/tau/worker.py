"""MissionWorker: an Ouroboros ops mission as the τ episode's operator.

Control inversion made concrete: the boss hands down a directive; the
worker runs ONE short ops mission in the shared episode workspace to carry
it out. The mission reads the live conversation (./conversation.md) and
policy (./policy.md), calls domain tools through the ./tau CLI (→ the
ToolBridge → the graded env), and writes its customer-facing message to
./reply.txt. The chat env delivers that reply to the customer.

Reuses the ops flow set wholesale (the GAIA/tb pattern): scan_project
re-reads *.md every cycle so the transcript and policy fold into the
charter, and the objective (directive + standing contract) propagates to
every prompt. No new actions, flows, or effects.
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import subprocess
import sys
import tempfile
from typing import List, Optional

from agent.chat.env import WorkerReport
from adapters.tau.bridge import TAU_CLI_TEMPLATE

log = logging.getLogger(__name__)

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

_STANDING_CONTRACT = """
You are a customer-service operator working ONE step of a live chat, under
your supervisor's direction. Your workspace holds:
- ./conversation.md — the conversation so far (the customer's latest
  message is flagged). READ IT FIRST.
- ./policy.md — the domain policy you MUST follow.
- ./TOOLS.md — the account tools you have and how to call them.
- ./tau — the CLI that runs a tool, e.g.
      ./tau get_order_details --json '{"order_id": "#W1"}'
  It prints the tool's result. Use it for every lookup or change; never
  guess account data.

YOUR DELIVERABLE: write the single message to send to the customer NOW to
./reply.txt — just the message text, no quotes, no "Agent:" prefix. It
must follow policy and address the supervisor's directive below. Do the
necessary tool calls first, then write reply.txt. Write reply.txt exactly
once, last.
""".strip()


class MissionWorker:
    """Runs one ops mission per boss directive in a persistent workspace."""

    def __init__(
        self,
        policy: str,
        tools_info: List[dict],
        bridge_url: str,
        *,
        llmvp_endpoint: str = "http://localhost:8008/graphql",
        max_cycles: int = 3,
        wall_clock_s: float = 300.0,
    ):
        self.llmvp_endpoint = llmvp_endpoint
        self.max_cycles = max_cycles
        self.wall_clock_s = wall_clock_s
        self.workspace = tempfile.mkdtemp(prefix="ouro-tau-")
        self._seed_workspace(policy, tools_info, bridge_url)
        self.mission_runs = 0

    # -- workspace seeding (once per episode) ----------------------------
    def _seed_workspace(self, policy: str, tools_info: List[dict], bridge_url: str):
        # Own venv: mission pip installs must never mutate the repo venv
        # (the GAIA httpx-poisoning lesson).
        subprocess.run(
            [sys.executable, "-m", "venv", os.path.join(self.workspace, ".venv")],
            check=True, capture_output=True, timeout=120,
        )
        self._write("policy.md", policy)
        self._write("TOOLS.md", _render_tools(tools_info))
        cli = os.path.join(self.workspace, "tau")
        self._write("tau", TAU_CLI_TEMPLATE.format(bridge_url=bridge_url))
        os.chmod(cli, 0o755)

    def _write(self, name: str, content: str):
        with open(os.path.join(self.workspace, name), "w", encoding="utf-8") as f:
            f.write(content)

    def _reply_path(self) -> str:
        return os.path.join(self.workspace, "reply.txt")

    # -- one directive = one mission -------------------------------------
    async def execute(self, directive: str, transcript: List[dict]) -> WorkerReport:
        self._write("conversation.md", _render_conversation(transcript))
        # Rotate reply.txt: a stale reply must never be re-sent.
        try:
            os.remove(self._reply_path())
        except OSError:
            pass

        objective = f"SUPERVISOR'S DIRECTIVE:\n{directive}\n\n{_STANDING_CONTRACT}"
        ok = await self._run_mission(objective)
        reply = self._read_reply()

        if reply:
            return WorkerReport(reply=reply, ok=True, notes=self._tool_note())
        # No reply.txt: one retry with pointed feedback, then a holding line.
        if ok:
            retry_obj = (
                objective
                + "\n\nYOU DID NOT WRITE ./reply.txt LAST RUN. Your ONLY "
                "remaining job: write the customer message to ./reply.txt now."
            )
            await self._run_mission(retry_obj)
            reply = self._read_reply()
            if reply:
                return WorkerReport(reply=reply, ok=True, notes=self._tool_note())
        log.warning("mission produced no reply.txt — holding reply")
        return WorkerReport(
            reply="Thanks for your patience — let me look into that and get "
            "right back to you.",
            ok=False,
            notes="operator produced no reply (technical fault)",
        )

    async def _run_mission(self, objective: str) -> bool:
        """Run one ops mission in ITS OWN thread + event loop.

        run_agent's MCP/PTY machinery uses anyio cancel scopes bound to the
        task that created them; awaiting it inside the episode's loop leaks a
        cancellation across the boss/user-sim httpx clients (observed). Like
        GAIA, each mission gets a top-level asyncio.run — here on a worker
        thread so the episode loop stays free. Never raises."""
        return await asyncio.to_thread(self._run_mission_blocking, objective)

    def _run_mission_blocking(self, objective: str) -> bool:
        from agent.effects.local import LocalEffects
        from agent.flow_sets import get_flow_set
        from agent.loop import run_agent
        from agent.persistence.manager import PersistenceManager
        from agent.persistence.models import MissionConfig, MissionState

        self.mission_runs += 1
        pm = PersistenceManager(self.workspace)
        pm.init_agent_dir()
        mission = MissionState(
            objective=objective,
            status="active",
            config=MissionConfig(
                working_directory=self.workspace,
                flow_set="ops",
                task_profile="task",
                llmvp_endpoint=self.llmvp_endpoint,
                web_research=False,
            ),
        )
        pm.save_mission(mission)
        effects = LocalEffects(
            working_directory=self.workspace, llmvp_endpoint=self.llmvp_endpoint
        )
        entry_flow = get_flow_set("ops").entry_flow
        ok = True

        async def _run_with_drain():
            try:
                await run_agent(
                    mission_id=mission.id,
                    effects=effects,
                    flows_dir=os.path.join(_REPO_ROOT, "flows"),
                    prompts_dir=os.path.join(_REPO_ROOT, "prompts"),
                    entry_flow=entry_flow,
                    max_cycles=self.max_cycles,
                    max_wall_clock_s=self.wall_clock_s,
                )
            finally:
                for teardown in ("end_open_inference_sessions", "mcp_disconnect_all"):
                    fn = getattr(effects, teardown, None)
                    if fn is not None:
                        try:
                            await fn()
                        except (Exception, asyncio.CancelledError):
                            pass

        try:
            asyncio.run(_run_with_drain())
        except RuntimeError as e:
            if "parked as paused" in str(e):
                log.info("tau mission budget stop (parked)")
            else:
                log.warning("tau mission RuntimeError: %s", e)
                ok = False
        except Exception:
            log.exception("tau mission crashed")
            ok = False
        return ok

    def _read_reply(self) -> str:
        try:
            with open(self._reply_path(), encoding="utf-8", errors="replace") as f:
                text = f.read().strip()
        except OSError:
            return ""
        # Defensive strip of an "Agent:" prefix the model may add.
        for prefix in ("Agent:", "AGENT:", "Reply:"):
            if text.startswith(prefix):
                text = text[len(prefix):].strip()
        return text.strip().strip('"').strip()

    def _tool_note(self) -> str:
        return "operator completed the directive"

    def cleanup(self, preserve_to: Optional[str] = None) -> None:
        if preserve_to:
            try:
                src = os.path.join(self.workspace, ".agent")
                if os.path.isdir(src):
                    shutil.copytree(src, os.path.join(preserve_to, "mission-agent"),
                                    dirs_exist_ok=True)
            except Exception:  # noqa: BLE001
                log.debug("mission .agent preserve failed")
        shutil.rmtree(self.workspace, ignore_errors=True)


def _render_tools(tools_info: List[dict]) -> str:
    lines = ["# Account Tools", "",
             "Call each via `./tau <name> --json '{...}'`. Arguments:"]
    for t in tools_info:
        fn = t.get("function", {})
        name = fn.get("name", "?")
        desc = fn.get("description", "").strip().split("\n")[0]
        params = fn.get("parameters", {}).get("properties", {})
        required = set(fn.get("parameters", {}).get("required", []))
        lines.append(f"\n## {name}\n{desc}")
        for pname, spec in params.items():
            req = " (required)" if pname in required else ""
            ptype = spec.get("type", "")
            pdesc = spec.get("description", "").strip().split("\n")[0]
            lines.append(f"- `{pname}` [{ptype}]{req}: {pdesc}")
    return "\n".join(lines)


def _render_conversation(transcript: List[dict]) -> str:
    """Render the chat-env transcript into the operator's briefing. Only
    customer/agent utterances (boss decisions are internal)."""
    lines = ["# Conversation so far", ""]
    spoken = [e for e in transcript if e.get("role") in ("user", "agent")
              and e.get("text")]
    for i, e in enumerate(spoken):
        who = "Customer" if e["role"] == "user" else "You (agent)"
        latest = (e["role"] == "user" and i == len(spoken) - 1)
        flag = "  ← LATEST, respond to this" if latest else ""
        lines.append(f"**{who}:**{flag}\n{e['text']}\n")
    return "\n".join(lines)
