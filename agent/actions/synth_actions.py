"""Round actions of the `synth` flow set (flows/synth/synth_control.cue).

THE PRODUCT is a bank of placeholder TEMPLATES -- prompt/completion pairs
whose every value is a ``{slot}`` -- for the synthetic-corpus pilot
(dev/rock_olmo/PROCEDURE.md §21). The text model writes wording; the
deterministic filler in dev/rock_olmo fills the slots from the facts layer.
agent/actions/synth_gate.py is the contract both sides check.

THREE ACTIONS, ONE ROUND
  synth_plan_round      spec + bank -> the cells (kind, family, direction,
                        form) with the largest deficits, up to
                        `units_per_round`
  synth_generate_round  one inference per cell, in parallel under two
                        semaphores (local seats / cloud seats), the cloud
                        domain chosen by a stable per-cell hash against
                        `cloud_share`; parse, gate, append to the bank
  synth_check_done      round counter, completion (every cell at target or
                        `max_rounds`), and the back-off signal when every
                        unit of a round failed

WHAT THE AGENT NEVER IMPORTS. dev/rock_olmo owns the facts and the frame
library; the agent reads only spec/spec.json that dev/rock_olmo/
synth_export.py writes (slot catalogue, sample fills, family plan,
forbidden signatures, known entities). The venv boundary is real: the
root venv has no torch and the rock venv has no pydantic.

CLOUD ROUTE. A unit goes to the cloud by passing ``domain=<cloud_domain>``
in config_overrides; LocalEffects resolves it through the mission's
``llmvp_domains`` (a domain is remote iff its key exists) and the LLMVP
server routes the request's `model` to the claude_cli provider. The
subscription is protected twice: a LATCH on limit text (cloud_limits) and a
per-day call cap, both persisted in synth/state.json so a restart does not
forget them.

FILES (relative to the mission's working directory): spec/spec.json,
bank/templates.jsonl (append-only; template_id unique), bank/rejects.jsonl,
synth/state.json.
"""

from __future__ import annotations

import asyncio
import collections
import datetime as _dt
import hashlib
import json
import logging
from typing import Any

from agent.actions.cloud_limits import is_provider_limit
from agent.actions.drain_lane import server_alive
from agent.actions.synth_gate import bank_row, validate_template
from agent.llm_json import parse_llm_json
from agent.models import StepInput, StepOutput

logger = logging.getLogger(__name__)

SPEC_PATH = "spec/spec.json"
BANK_PATH = "bank/templates.jsonl"
REJECTS_PATH = "bank/rejects.jsonl"
STATE_PATH = "synth/state.json"
PROMPT_ID = "synth/templates_round"
FLOW_KEY = "synth_round"

DEFAULTS: dict[str, Any] = {
    "units_per_round": 6,
    "local_concurrency": 4,
    "cloud_concurrency": 2,
    "cloud_share": 0.0,
    "cloud_daily_cap": 150,
    "cloud_domain": "synth_cloud",
    "max_rounds": 400,
    "max_tokens": 2500,
    "temperature": "t*0.7",
    "retry_temperature": "t*0.4",
    "near_dup": 0.6,
}


# ── config / state ───────────────────────────────────────────────────


def _synth_cfg(mission: Any) -> dict[str, Any]:
    cfg = dict(DEFAULTS)
    raw = getattr(getattr(mission, "config", None), "synth", None) or {}
    if isinstance(raw, dict):
        cfg.update(raw)
    return cfg


def _cloud_model(mission: Any, cfg: dict[str, Any]) -> str:
    routes = getattr(getattr(mission, "config", None), "llmvp_domains", None) or {}
    route = routes.get(cfg["cloud_domain"]) if isinstance(routes, dict) else None
    if isinstance(route, dict):
        return str(route.get("model") or "")
    return ""


def _local_model(effects: Any) -> str:
    try:
        from agent.actions.curation_actions import _provenance_model

        return _provenance_model(effects) or "local"
    except Exception:  # noqa: BLE001 -- a label, never a failure
        return "local"


def _now() -> str:
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _today() -> str:
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%d")


async def _read_json(effects: Any, path: str) -> Any:
    fc = await effects.read_file(path)
    if not getattr(fc, "exists", False) or not (fc.content or "").strip():
        return None
    try:
        return json.loads(fc.content)
    except Exception:  # noqa: BLE001 -- unreadable is "absent"
        logger.warning("synth: %s is not valid JSON", path)
        return None


async def _read_jsonl(effects: Any, path: str) -> list[dict]:
    fc = await effects.read_file(path)
    if not getattr(fc, "exists", False):
        return []
    rows: list[dict] = []
    for line in (fc.content or "").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except Exception:  # noqa: BLE001 -- a torn tail is skipped, not fatal
            continue
        if isinstance(row, dict):
            rows.append(row)
    return rows


async def _read_state(effects: Any) -> dict[str, Any]:
    st = await _read_json(effects, STATE_PATH)
    st = st if isinstance(st, dict) else {}
    st.setdefault("rounds", 0)
    st.setdefault("cloud_latched", False)
    st.setdefault("latch_reason", "")
    st.setdefault("cloud_calls", {})
    return st


async def _write_state(effects: Any, state: dict[str, Any]) -> None:
    state["updated_at"] = _now()
    await effects.write_file(STATE_PATH, json.dumps(state, indent=1) + "\n")


# ── cells and deficits ───────────────────────────────────────────────


def cell_key(cell: dict) -> str:
    return f"{cell['kind']}/{cell['family']}/{cell['direction']}/{cell['form']}"


def cells(spec: dict) -> list[dict]:
    """Every (kind, family, direction, form) cell the spec asks for."""
    out: list[dict] = []
    for kind, ks in sorted((spec.get("kinds") or {}).items()):
        for fam, fs in sorted((ks.get("families") or {}).items()):
            for direction in fs.get("directions") or ["forward"]:
                for form in fs.get("forms") or ["statement"]:
                    out.append(
                        {
                            "kind": kind,
                            "family": fam,
                            "direction": direction,
                            "form": form,
                            "target": int(fs.get("target") or 20),
                        }
                    )
    return out


def bank_counts(bank: list[dict]) -> collections.Counter:
    return collections.Counter(
        cell_key(r)
        for r in bank
        if all(k in r for k in ("kind", "family", "direction", "form"))
    )


def deficits(spec: dict, bank: list[dict]) -> list[tuple[dict, int]]:
    """(cell, missing) for every cell below target, largest deficit first;
    ties broken by key so a round is reproducible from spec + bank."""
    have = bank_counts(bank)
    out = []
    for c in cells(spec):
        missing = c["target"] - have.get(cell_key(c), 0)
        if missing > 0:
            out.append((c, missing))
    out.sort(key=lambda cm: (-cm[1], cell_key(cm[0])))
    return out


# ── prompt ───────────────────────────────────────────────────────────


def _slot_table(kind_spec: dict, family: dict) -> str:
    slots = kind_spec.get("slots") or {}
    wanted = family.get("slots") or list(slots)
    lines = []
    for name in wanted:
        s = slots.get(name)
        if not isinstance(s, dict):
            continue
        ex = s.get("example")
        line = f"  {{{name}}}  ({s.get('type', 'text')}) — {s.get('describe', '')}"
        if ex not in (None, ""):
            line += f"; e.g. {ex}"
        lines.append(line)
    return "\n".join(lines)


def _direction_rules(cell: dict, kind_spec: dict) -> str:
    subject = ", ".join(
        "{%s}" % s for s in kind_spec.get("subject_slots") or ["species"]
    )
    answers = kind_spec.get("answer_slots") or {}
    fwd = ", ".join("{%s}" % s for s in answers.get("forward") or [])
    if cell["direction"] == "forward":
        return (
            f"FORWARD: the prompt names the subject ({subject}) and stops right "
            f"before the value; the completion carries the value placeholders "
            f"({fwd or 'the kind’s value slots'})."
        )
    return (
        f"BACKWARD: the prompt carries the value placeholders ({fwd or 'the value slots'}) "
        f"and never names the subject; the completion names the subject "
        f"({subject})."
    )


def _form_hint(cell: dict) -> str:
    if cell["form"] == "question":
        return (
            "QUESTION form: the prompt is a question (exam item, field query, "
            "catalogue lookup, dialogue turn) and the completion is its answer."
        )
    return (
        "STATEMENT form: the prompt is a lead-in (field note, catalogue line, "
        "report sentence, table row, caption) that the completion finishes."
    )


def _openings(rows: list[dict], limit: int = 40) -> list[str]:
    seen: list[str] = []
    for r in rows:
        head = " ".join((r.get("prompt") or "").split()[:6])
        if head and head not in seen:
            seen.append(head)
        if len(seen) >= limit:
            break
    return seen


def render_prompt(spec: dict, cell: dict, bank_rows: list[dict]) -> tuple[str, str]:
    """(static_prefix, dynamic) for one cell via prompts/synth/templates_round."""
    from agent.runtime import _get_prompt_renderer

    kind_spec = spec["kinds"][cell["kind"]]
    family = (kind_spec.get("families") or {}).get(cell["family"]) or {}
    avoid = _openings(bank_rows) + list(
        (spec.get("forbidden_openings") or {}).get(cell["kind"]) or []
    )
    namespaces = {
        "input": {},
        "context": {
            "kind": cell["kind"],
            "kind_describe": kind_spec.get("describe") or cell["kind"],
            "family": cell["family"],
            "family_brief": family.get("brief") or "",
            "direction": cell["direction"],
            "direction_rules": _direction_rules(cell, kind_spec),
            "form_hint": _form_hint(cell),
            "slot_table": _slot_table(kind_spec, family),
            "avoid_block": "\n".join(f"  - {a}" for a in avoid) if avoid else "",
        },
        "meta": {"flow_name": "synth_control", "step_id": "generate_round"},
    }
    return _get_prompt_renderer().render_with_cache_split(PROMPT_ID, namespaces)


def parse_templates(text: str, cell: dict) -> list[dict]:
    """The model's fenced JSON -> candidate dicts stamped with the cell's
    direction and form (the cell is authoritative for coverage accounting)."""
    data = parse_llm_json(text or "")
    items: Any
    if isinstance(data, dict):
        items = data.get("templates") or data.get("items") or []
    else:
        items = data or []
    out: list[dict] = []
    for it in items if isinstance(items, list) else []:
        if not isinstance(it, dict):
            continue
        if not isinstance(it.get("prompt"), str) or not isinstance(
            it.get("completion"), str
        ):
            continue
        t = dict(it)
        t["direction"] = cell["direction"]
        t["form"] = cell["form"]
        out.append(t)
    return out


# ── the unit ─────────────────────────────────────────────────────────


def _use_cloud(
    cfg: dict, state: dict, cell: dict, round_no: int, cloud_model: str
) -> bool:
    share = float(cfg.get("cloud_share") or 0.0)
    if share <= 0 or not cloud_model or state.get("cloud_latched"):
        return False
    if state["cloud_calls"].get(_today(), 0) >= int(cfg.get("cloud_daily_cap") or 0):
        return False
    h = int(hashlib.sha256(f"{cell_key(cell)}|{round_no}".encode()).hexdigest()[:8], 16)
    return (h % 100) < int(round(share * 100))


async def _one_unit(
    effects: Any,
    spec: dict,
    cell: dict,
    cfg: dict,
    cell_rows: list[dict],
    *,
    sem: asyncio.Semaphore,
    use_cloud: bool,
    model_label: str,
) -> dict[str, Any]:
    """One inference (plus one cooler retry when nothing parses), gated.
    Never raises: a failed unit reports `error` and banks nothing."""
    static_prefix, dynamic = render_prompt(spec, cell, cell_rows)
    prompt = static_prefix + dynamic
    kind_spec = spec["kinds"][cell["kind"]]
    known = set(spec.get("known_entities") or [])
    forbidden = set((spec.get("forbidden_signatures") or {}).get(cell["kind"]) or [])
    banked_sigs = {r.get("signature", "") for r in cell_rows}
    banked_texts = [
        f"{r.get('prompt', '')} {r.get('completion', '')}" for r in cell_rows
    ]
    result: dict[str, Any] = {
        "cell": cell_key(cell),
        "cloud": use_cloud,
        "accepted": [],
        "rejected": [],
        "error": "",
        "tokens": 0,
        "raw_templates": 0,
    }
    templates: list[dict] = []
    temps = [cfg["temperature"], cfg["retry_temperature"]]
    for attempt, temp in enumerate(temps):
        overrides: dict[str, Any] = {
            "temperature": temp,
            "max_tokens": cfg["max_tokens"],
        }
        if use_cloud:
            overrides["domain"] = cfg["cloud_domain"]
        try:
            async with sem:
                res = await effects.run_inference(
                    prompt,
                    overrides,
                    static_prefix=static_prefix or None,
                    flow_key=FLOW_KEY,
                )
        except Exception as exc:  # noqa: BLE001 -- a unit never takes the round down
            result["error"] = f"{type(exc).__name__}: {exc}"[:300]
            return result
        if getattr(res, "error", None):
            result["error"] = str(res.error)[:300]
            return result
        result["tokens"] += int(getattr(res, "tokens_generated", 0) or 0)
        text = getattr(res, "text", "") or ""
        templates = parse_templates(text, cell)
        if templates:
            result["temperature"] = temp
            break
        # The claude_cli route surfaces a limit as an error, but a provider
        # can also answer IN TEXT ("You've hit your usage limit") -- the
        # binder packer's precedent. Checked BEFORE the cooler retry: a
        # retry against a limited route spends quota on a second refusal
        # and would hide the limit behind "no parseable templates".
        if is_provider_limit(text[:400]):
            result["error"] = f"provider limit: {text[:200]}"
            return result
        if attempt == 0:
            logger.info(
                "synth %s: no parseable templates, cooler retry", cell_key(cell)
            )
    if not templates:
        result["error"] = "no parseable templates"
        return result
    result["raw_templates"] = len(templates)
    provenance = {
        "model": model_label,
        "domain": cfg["cloud_domain"] if use_cloud else "",
        "generated_at": _now(),
        "prompt_id": PROMPT_ID,
        "prompt_sha": hashlib.sha256(prompt.encode("utf-8")).hexdigest()[:12],
        "temperature": result.get("temperature"),
        "tokens_generated": result["tokens"],
    }
    for t in templates:
        problems = validate_template(
            t,
            kind_spec,
            banked_sigs=banked_sigs,
            banked_texts=banked_texts,
            known_entities=known,
            forbidden_sigs=forbidden,
            near_dup=float(cfg.get("near_dup") or 0.6),
        )
        row = bank_row(t, cell["kind"], cell["family"], provenance)
        if problems:
            row["problems"] = problems
            result["rejected"].append(row)
            continue
        banked_sigs.add(row["signature"])
        banked_texts.append(f"{row['prompt']} {row['completion']}")
        result["accepted"].append(row)
    return result


# ── actions ──────────────────────────────────────────────────────────


async def action_synth_plan_round(step_input: StepInput) -> StepOutput:
    """spec + bank -> the round's units (cells with the largest deficits)."""
    effects = step_input.effects
    mission = step_input.context.get("mission")
    if effects is None or mission is None:
        return StepOutput(
            result={"units_ready": False, "no_spec": True},
            observations="synth: no effects/mission — nothing to plan",
        )
    cfg = _synth_cfg(mission)
    spec = await _read_json(effects, SPEC_PATH)
    if not isinstance(spec, dict) or not spec.get("kinds"):
        return StepOutput(
            result={"units_ready": False, "no_spec": True},
            observations=(
                f"synth: no spec at {SPEC_PATH} — run dev/rock_olmo/synth_export.py "
                "into the workspace; idling"
            ),
        )
    if not await server_alive(effects):
        return StepOutput(
            result={"units_ready": False, "server_down": True},
            observations="synth: inference server unreachable — idling",
        )
    bank = await _read_jsonl(effects, BANK_PATH)
    todo = deficits(spec, bank)
    if not todo:
        return StepOutput(
            result={
                "units_ready": False,
                "bank_complete": True,
                "templates": len(bank),
            },
            observations=f"synth: bank complete — {len(bank)} templates, every cell at target",
        )
    n = max(1, int(cfg.get("units_per_round") or 1))
    units = [c for c, _ in todo[:n]]
    return StepOutput(
        result={
            "units_ready": True,
            "units": len(units),
            "cells_short": len(todo),
            "templates": len(bank),
        },
        observations=(
            f"synth: {len(bank)} banked, {len(todo)} cells short; "
            f"this round: {', '.join(cell_key(c) for c in units)}"
        ),
        context_updates={"synth_units": units},
    )


async def action_synth_generate_round(step_input: StepInput) -> StepOutput:
    """Generate, gate and bank one round over the planned units."""
    effects = step_input.effects
    mission = step_input.context.get("mission")
    units = list(step_input.context.get("synth_units") or [])
    summary: dict[str, Any] = {
        "templates": 0,
        "rejected": 0,
        "units": len(units),
        "errors": 0,
        "cloud_units": 0,
        "cloud_latched": False,
        "by_cell": {},
        "reject_reasons": {},
    }
    if effects is None or mission is None or not units:
        summary["reason"] = "nothing to generate"
        return StepOutput(
            result=summary,
            observations="synth: no units this round",
            context_updates={"synth_summary": summary},
        )
    cfg = _synth_cfg(mission)
    spec = await _read_json(effects, SPEC_PATH)
    if not isinstance(spec, dict):
        summary["reason"] = "no spec"
        return StepOutput(
            result=summary,
            observations="synth: spec vanished between plan and generate",
            context_updates={"synth_summary": summary},
        )
    state = await _read_state(effects)
    bank = await _read_jsonl(effects, BANK_PATH)
    cloud_model = _cloud_model(mission, cfg)
    local_label = _local_model(effects)
    sem_local = asyncio.Semaphore(max(1, int(cfg.get("local_concurrency") or 1)))
    sem_cloud = asyncio.Semaphore(max(1, int(cfg.get("cloud_concurrency") or 1)))

    by_cell: dict[str, list[dict]] = collections.defaultdict(list)
    for r in bank:
        if all(k in r for k in ("kind", "family", "direction", "form")):
            by_cell[cell_key(r)].append(r)

    round_no = int(state.get("rounds") or 0)
    tasks = []
    for cell in units:
        use_cloud = _use_cloud(cfg, state, cell, round_no, cloud_model)
        if use_cloud:
            state["cloud_calls"][_today()] = state["cloud_calls"].get(_today(), 0) + 1
            summary["cloud_units"] += 1
        tasks.append(
            _one_unit(
                effects,
                spec,
                cell,
                cfg,
                list(by_cell.get(cell_key(cell), [])),
                sem=sem_cloud if use_cloud else sem_local,
                use_cloud=use_cloud,
                model_label=cloud_model if use_cloud else local_label,
            )
        )
    results = await asyncio.gather(*tasks)

    accepted_rows: list[dict] = []
    rejected_rows: list[dict] = []
    seen_sigs: dict[str, set[str]] = {
        k: {r.get("signature", "") for r in v} for k, v in by_cell.items()
    }
    for res in results:
        key = res["cell"]
        cell_summary = {
            "accepted": 0,
            "rejected": len(res["rejected"]),
            "cloud": res["cloud"],
            "error": res["error"],
        }
        if res["error"]:
            summary["errors"] += 1
            if res["cloud"] and is_provider_limit(res["error"]):
                state["cloud_latched"] = True
                state["latch_reason"] = res["error"][:200]
                state["latched_at"] = _now()
        # A second unit in the same round cannot see the first one's rows, so
        # cross-unit duplicates are caught here before anything is appended.
        held = seen_sigs.setdefault(key, set())
        for row in res["accepted"]:
            if row["signature"] in held:
                row["problems"] = ["duplicate signature (same round)"]
                rejected_rows.append(row)
                cell_summary["rejected"] += 1
                continue
            held.add(row["signature"])
            accepted_rows.append(row)
            cell_summary["accepted"] += 1
        rejected_rows.extend(res["rejected"])
        summary["by_cell"][key] = cell_summary

    if accepted_rows:
        await effects.append_file(
            BANK_PATH,
            "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in accepted_rows),
        )
    if rejected_rows:
        await effects.append_file(
            REJECTS_PATH,
            "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rejected_rows),
        )
    reasons: collections.Counter = collections.Counter()
    for r in rejected_rows:
        for p in r.get("problems") or []:
            reasons[p.split(" (")[0].split(" KeyError")[0][:40]] += 1
    summary["templates"] = len(accepted_rows)
    summary["rejected"] = len(rejected_rows)
    summary["reject_reasons"] = dict(reasons.most_common(8))
    summary["cloud_latched"] = bool(state.get("cloud_latched"))
    await _write_state(effects, state)
    obs = (
        f"synth round {round_no + 1}: {len(accepted_rows)} templates banked, "
        f"{len(rejected_rows)} rejected, {summary['errors']}/{len(units)} units failed"
        f"; cloud {summary['cloud_units']}"
        + (" LATCHED" if summary["cloud_latched"] else "")
    )
    return StepOutput(
        result=summary, observations=obs, context_updates={"synth_summary": summary}
    )


async def action_synth_check_done(step_input: StepInput) -> StepOutput:
    """Advance the round counter; finish when every cell meets its target or
    the round ceiling is reached; ask for a back-off when a whole round
    failed (the server is down or every route is refusing)."""
    effects = step_input.effects
    mission = step_input.context.get("mission")
    summary = step_input.context.get("synth_summary") or {}
    if effects is None or mission is None:
        return StepOutput(result={"done": True}, observations="synth: nothing to check")
    cfg = _synth_cfg(mission)
    state = await _read_state(effects)
    state["rounds"] = int(state.get("rounds") or 0) + 1
    await _write_state(effects, state)
    spec = await _read_json(effects, SPEC_PATH)
    bank = await _read_jsonl(effects, BANK_PATH)
    todo = deficits(spec, bank) if isinstance(spec, dict) else []
    remaining = sum(m for _, m in todo)
    done = not todo or state["rounds"] >= int(cfg.get("max_rounds") or 1)
    units = int(summary.get("units") or 0)
    paused = (not done) and units > 0 and int(summary.get("errors") or 0) >= units
    result = {
        "done": done,
        "paused": paused,
        "rounds": state["rounds"],
        "cells_short": len(todo),
        "templates_missing": remaining,
        "templates": len(bank),
    }
    obs = (
        f"synth: round {state['rounds']}/{cfg.get('max_rounds')}, {len(bank)} banked, "
        f"{len(todo)} cells short ({remaining} templates)"
        + (
            " — DONE"
            if done
            else (" — every unit failed, backing off" if paused else "")
        )
    )
    return StepOutput(result=result, observations=obs)
