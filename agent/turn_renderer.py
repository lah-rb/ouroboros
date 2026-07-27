"""Turn renderer — builds the prompt string for an inference turn.

Given a TurnDefinition and runtime namespaces (input/context/meta),
this module assembles:

  1. The mode banner (=== SHAPE ===) at the top, derived from
     response_shape or overridden by turn.mode_banner.
  2. Each section in declared order — resolving refs against namespaces,
     loading templates from the prompts directory, and substituting
     {input.x} / {context.y} / {meta.z} references inside content.
  3. The envelope at the end, whose form depends on response_shape.

New per-section template format (distinct from the legacy multi-section
PromptRenderer format in agent/loader.py):

    # prompts/personas/env_detector.yaml
    id: personas/env_detector
    content: |
      ---ACT AS---
      You are ...
      ---END---

The existing PromptRenderer operates on the legacy `sections:`-at-top
format and continues to serve any StepDefinition that still uses
`prompt_template`. Both formats live side-by-side in prompts/.

Phase 3 scope — what this module currently implements:
  - json_document response shape (envelope rendering)
  - All five banner derivations (they're pure mapping)
  - role / problem / evidence / context_files / target_entity /
    dependencies / prior_attempts / observations / instruction sections
  - envelope section for json_document
  - ref / template / literal / dynamic-template-ref content sources
  - Conditional section omission when ref resolves empty

Deferred to later phases:
  - menu_single / menu_compound envelope and options rendering (Phase with first menu site)
  - code envelope
  - prose envelope (optional; may be empty)
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import yaml

from agent import languages
from agent.models import OptionSource, Ref, Section, TurnDefinition
from agent.schema_registry import SchemaRegistry, get_default_registry


class TurnRenderError(Exception):
    """Raised on renderer-side problems: missing template, unresolved
    required ref, unsupported shape, malformed template file."""


class EmptyMenuError(TurnRenderError):
    """A menu turn whose options resolved to nothing AT RUNTIME — the
    ``options_from`` projection yielded an empty set — as opposed to a static
    template/config error. A distinct subclass so the agent loop can pause the run
    cleanly (the condition is data-dependent and recoverable) instead of crashing,
    while genuine renderer/config errors (the other TurnRenderError raises) still
    fail loudly."""


# Default banner per response shape. Overridable via turn.mode_banner.
# Kept at module scope so tests can introspect without instantiating.
_BANNERS: dict[str, str] = {
    "menu_single": "=== MENU CHOICE ===",
    "menu_compound": "=== MENU + ARGUMENT ===",
    "json_document": "=== JSON DOCUMENT ===",
    "code": "=== CODE EDITOR ===",
    "prose": "=== WRITING ===",
}

# The code-envelope example block's language↔extension↔fence hints now live in
# the canonical agent/languages.py registry (languages.ext_for_name /
# fence_for_ext), keyed there as a name→ext map + an ext→fence map.

# Namespace reference pattern — matches {input.x}, {context.y}, {meta.z}.
# Deliberately compatible with the existing PromptRenderer pattern so
# templates authored for one renderer can reference variables the same
# way in the other. Does NOT match single-word placeholders like {file}
# or JSON examples — a known namespace prefix plus a dot is required.
_REF_PATTERN = re.compile(r"\{((?:input|context|meta)\.[a-zA-Z_][a-zA-Z0-9_.]*)\}")


class TurnRenderer:
    """Assembles prompts from TurnDefinition declarations."""

    def __init__(
        self,
        prompts_dir: Path | str,
        schema_registry: SchemaRegistry | None = None,
    ) -> None:
        self.prompts_dir = Path(prompts_dir)
        self.schema_registry = (
            schema_registry if schema_registry is not None else get_default_registry()
        )
        self._template_cache: dict[str, dict] = {}

    # ── Public API ────────────────────────────────────────────────────

    def render(self, turn: TurnDefinition, namespaces: dict[str, Any]) -> str:
        """Render a turn's full prompt.

        `namespaces` is a dict with keys `input`, `context`, `meta` —
        the same structure used by the legacy PromptRenderer. Missing
        namespaces are treated as empty.
        """
        parts: list[str] = [self._banner(turn)]

        for section in turn.sections:
            rendered = self._render_section(section, turn, namespaces)
            if rendered is not None:
                parts.append(rendered)

        return "\n\n".join(parts)

    # ── Banner ────────────────────────────────────────────────────────

    def _banner(self, turn: TurnDefinition) -> str:
        if turn.mode_banner:
            return turn.mode_banner
        try:
            return _BANNERS[turn.response_shape]
        except KeyError:
            raise TurnRenderError(
                f"Unknown response_shape {turn.response_shape!r}; "
                f"expected one of {sorted(_BANNERS)}"
            ) from None

    # ── Section dispatch ──────────────────────────────────────────────

    def _render_section(
        self,
        section: Section,
        turn: TurnDefinition,
        namespaces: dict[str, Any],
    ) -> str | None:
        if section.type == "envelope":
            return self._render_envelope(turn, namespaces)
        if section.type == "options":
            return self._render_options(turn, namespaces)
        return self._render_content_section(section, namespaces)

    # ── Content-bearing sections ──────────────────────────────────────

    def _render_content_section(
        self, section: Section, namespaces: dict[str, Any]
    ) -> str | None:
        """Render a section whose content comes from ref / template /
        literal.

        Returns None when the content resolves empty AND the section is
        not marked `required: true` — this is how evidence and problem
        sections with optional source data disappear cleanly when the
        source isn't populated for this invocation.
        """
        content: str | None = None

        if section.ref is not None:
            content = self._render_ref_section(section, namespaces)
        elif section.template is not None:
            content = self._render_template_section(section, namespaces)
        elif section.literal is not None:
            content = self._substitute(section.literal, namespaces)
        else:
            raise TurnRenderError(
                f"Content-bearing section {section.type!r} has no source "
                f"(must have exactly one of ref / template / literal)"
            )

        if content is None:
            return None

        if section.title:
            title = self._substitute(section.title, namespaces)
            return f"## {title}\n{content}"
        return content

    def _render_ref_section(
        self, section: Section, namespaces: dict[str, Any]
    ) -> str | None:
        """Ref-sourced sections resolve against namespaces. Empty
        resolution → omitted unless required.

        Symbol-shaped dicts (``{"body": "...", "kind": "...", "name": "..."}``)
        are rendered as the code body with a compact kind-and-name
        header, not as a Python dict repr. Pre-fix, patch's
        rewrite_symbol turn was rendering ``current_symbol`` as
        ``{'name': 'parse_command', 'kind': 'function', 'body':
        'def parse_command...\\n    ...'}`` — the model saw escape
        sequences and had to mentally parse the dict to find the
        actual code, and sometimes produced class wrappings when
        asked for function bodies (76d trace: 6 class-vs-function
        retries across ~23 symbol rewrites).
        """
        assert section.ref is not None
        resolved = self._resolve_path(section.ref.ref, namespaces)
        if resolved is None or (isinstance(resolved, str) and resolved == ""):
            if section.required:
                return ""  # Emit with empty content
            return None  # Omit the section entirely
        if isinstance(resolved, dict) and isinstance(resolved.get("body"), str):
            return self._format_symbol_dict(resolved)
        return str(resolved)

    def _format_symbol_dict(self, symbol: dict[str, Any]) -> str:
        """Render a symbol-table entry as a readable code block with a
        compact header line. Expected keys: ``body`` (the source
        text), ``kind`` (class/function/method), ``name``, and
        optionally ``signature``. Missing keys degrade gracefully —
        we always emit the body even if the header can't be built.
        """
        name = symbol.get("name", "")
        kind = symbol.get("kind", "")
        parent = symbol.get("parent")
        body = symbol["body"]

        # Header: "method GameEngine.run" / "function parse_command" /
        # "class NPC". The parent field lives on methods and tells the
        # renderer to qualify the name — the model gets an unambiguous
        # signal about whether it's producing a standalone function
        # vs a method-inside-a-class. Nothing else in the prompt
        # makes that distinction visually.
        if parent and name and not name.startswith(parent + "."):
            qualified = f"{parent}.{name}"
        else:
            qualified = name

        header_parts = [p for p in (kind, qualified) if p]
        header = " ".join(header_parts)

        if header:
            return f"{header}\n\n```python\n{body}\n```"
        return f"```python\n{body}\n```"

    def _render_template_section(
        self, section: Section, namespaces: dict[str, Any]
    ) -> str | None:
        """Template-sourced sections load the named file and substitute.

        Template name can be a literal string or a Ref (dynamic template
        selection, per Site #10's kind-aware instruction pattern)."""
        assert section.template is not None
        template_spec = section.template

        if isinstance(template_spec, Ref):
            template_id = self._resolve_path(template_spec.ref, namespaces)
            if not template_id:
                if section.required:
                    raise TurnRenderError(
                        f"Dynamic template ref {template_spec.ref!r} "
                        f"resolved to empty on a required section"
                    )
                return None
        else:
            template_id = template_spec

        return self._render_template(str(template_id), namespaces)

    def _render_template(self, template_id: str, namespaces: dict[str, Any]) -> str:
        template = self._load_template(template_id)
        content = template.get("content", "")
        return self._substitute(content, namespaces)

    def _load_template(self, template_id: str) -> dict:
        """Load a turn-format template file. Cached after first load."""
        if template_id in self._template_cache:
            return self._template_cache[template_id]

        path = self.prompts_dir / f"{template_id}.yaml"
        if not path.exists():
            raise TurnRenderError(f"Turn template not found: {path}")

        with path.open("r", encoding="utf-8") as fh:
            template = yaml.safe_load(fh)

        if not isinstance(template, dict):
            raise TurnRenderError(
                f"Turn template {template_id!r} must be a YAML mapping; "
                f"got {type(template).__name__}"
            )
        if "content" not in template:
            # Legacy templates use `sections:` — detect and fail with
            # a clear message rather than letting `content=""` silently pass.
            if "sections" in template:
                raise TurnRenderError(
                    f"Turn template {template_id!r} uses the legacy "
                    f"`sections:` format; new turn-format templates must "
                    f"use a single `content:` field. Create a new file "
                    f"or convert the existing one."
                )
            raise TurnRenderError(
                f"Turn template {template_id!r} missing required `content` field"
            )

        self._template_cache[template_id] = template
        return template

    # ── Envelope ──────────────────────────────────────────────────────

    def _render_envelope(
        self, turn: TurnDefinition, namespaces: dict[str, Any]
    ) -> str | None:
        """Generate the response-shape-specific envelope block.

        Returns None when the shape has no envelope content (prose) —
        the renderer's main loop filters None and omits the section
        cleanly, avoiding trailing whitespace.
        """
        shape = turn.response_shape

        if shape == "json_document":
            return self._render_json_document_envelope(turn)

        if shape == "prose":
            # Prose shape has no structured envelope. The === WRITING ===
            # banner + SOUL primer carry the mode priming. No format
            # instruction or example is emitted — the response is
            # free-form.
            return None

        if shape == "code":
            return self._render_code_envelope(turn, namespaces)

        if shape in ("menu_single", "menu_compound"):
            return self._render_menu_envelope(turn, namespaces)

        raise NotImplementedError(
            f"Envelope rendering for response_shape {shape!r} is not yet "
            f"implemented. This shape arrives with a later migration batch."
        )

    def _render_json_document_envelope(self, turn: TurnDefinition) -> str:
        """json_document envelope: terse Respond-with, fenced multi-line
        example from schema's x-example, trailing `> ` terminator.

        Multi-line example kept fenced because the structural shape is
        the point — the model needs to see the keys and nesting. The
        `> ` after a blank line signals "your turn now" the way a
        shell/REPL prompt does.
        """
        schema_id = turn.response.schema_id  # type: ignore[union-attr]
        schema = self.schema_registry.get(schema_id)
        example = schema.get("x-example")
        if example is None:
            raise TurnRenderError(
                f"Schema {schema_id!r} has no `x-example` field — "
                f"json_document envelope rendering requires a concrete "
                f"example the model can pattern-match against."
            )
        example_json = json.dumps(example, indent=2)
        return (
            "Respond with JSON matching this shape:\n\n"
            f"```json\n{example_json}\n```\n\n"
            # A CLOSING DIRECTIVE, not just the example. Ending on a 40-line
            # JSON blob leaves the last instruction "here is a shape" and never
            # "emit only this", so a model inclined to preamble opens with
            # "I'll examine the project files to determine…" — which a JSON
            # extractor reads as nothing at all.
            #
            # Measured on the exact env-detection prompt that failed in
            # production (laguna-S-2.1, 6 reps per arm, both temperatures):
            # as-shipped 1/6 JSON, with this line 6/6. Identical at t=0.2 and
            # t=1.0, so it is arrangement, not sampling.
            #
            # The steps that never had this problem already close this way —
            # design_architecture ends "Now return ONLY the single fenced JSON
            # blueprint … Do NOT write files, code, prose, or commands."
            "Return ONLY the fenced JSON object above — no preamble, "
            "explanation, or commentary. A JSON extractor consumes this "
            "response directly.\n\n"
            "> "
        )

    def _render_code_envelope(
        self, turn: TurnDefinition, namespaces: dict[str, Any]
    ) -> str:
        """code envelope: terse Respond-with, fenced example, trailing
        `> ` terminator.

        Three distinct cases:

        - ``scope == "symbol"``: kind-aware example loaded from
          `prompts/envelopes/symbol_{language}_{kind}.yaml`. No
          `# === FILE:` marker — the splicer places the output at
          AST-computed byte offsets, so surrounding scaffolding breaks
          the replacement. Adding a new language is purely additive:
          drop three `symbol_<lang>_*.yaml` files matching the naming
          convention. The 76d class-wrapping bug lived here: a naked
          method example (with `self` as first param, no class wrapper)
          is the strongest anti-pattern.

        - Single-language full-file (``scope == "file"`` or absent,
          ``language != ""``): one fenced block tagged with the
          declared language. The model produces one file.

        - Multi-language full-file (``language == ""``): the universal
          shell example stands in as the format demonstrator. The
          shebang line signals "this is a complete file, not a shell
          command you should execute" — language-agnostic, the model
          infers the actual output language from the task + instruction
          rather than the envelope.

        All three modes are consumed by markdown_fence.parse_file_blocks
        (full-file cases) or rewrite_symbol_turn's fence extractor
        (symbol case).
        """
        response = turn.response  # type: ignore[union-attr]
        language = response.language
        scope = getattr(response, "scope", None)
        # The actual target format. response.language is a coarse default the
        # create flow sets ("python"); the real format comes from the target
        # extension. Empty for symbol scope or when no target is known.
        target_ext = "" if scope == "symbol" else self._target_extension(namespaces)

        # JSON whole-file writes get a dedicated, marker-free envelope: the
        # `# === FILE: <path> ===` first line is a comment, and JSON has none,
        # so embedding it produces invalid JSON.
        if target_ext == "json":
            return self._render_json_file_envelope()

        if scope == "symbol":
            # current_symbol.kind is available in context. Template
            # lookup is direct — no fallback, no defaults: a missing
            # template surfaces as a lint error, not a silent pass.
            if not language:
                raise TurnRenderError(
                    "Symbol-scope code envelope requires "
                    "turn.response.language — got empty."
                )
            current_symbol = self._resolve_path("context.current_symbol", namespaces)
            kind = None
            if isinstance(current_symbol, dict):
                kind = current_symbol.get("kind")
            elif current_symbol is not None and hasattr(current_symbol, "kind"):
                kind = current_symbol.kind
            if not kind:
                raise TurnRenderError(
                    "Symbol-scope code envelope requires "
                    "context.current_symbol.kind to pick an example "
                    "template. Ensure the step's context includes "
                    "current_symbol with a `kind` field."
                )
            template_id = f"envelopes/symbol_{language}_{kind}"
            try:
                example_body = self._load_template(template_id).get("content", "")
            except Exception as e:
                raise TurnRenderError(
                    f"No symbol example template for language={language!r} "
                    f"kind={kind!r} (expected at {template_id!r}). "
                    f"Add a YAML file at prompts/{template_id}.yaml "
                    f"with a minimal example body. Underlying error: {e}"
                )
            fenced_example = f"```{language}\n{example_body.rstrip()}\n```"
            return (
                f"Respond with a fenced code block containing the complete "
                f"rewritten {kind}. No imports, no surrounding scaffolding, "
                f"no `# === FILE: ===` marker — only the {kind} body:"
                f"\n\n{fenced_example}\n\n"
                "> "
            )

        if language:
            # Prefer the target's actual format for the fence tag + example
            # extension; fall back to response.language when no target is known.
            if target_ext:
                fence_lang = languages.fence_for_ext(target_ext)
                ext = target_ext
            else:
                fence_lang = language
                ext = languages.ext_for_name(language)
            fenced_example = (
                f"```{fence_lang}\n"
                f"# === FILE: path/to/file.{ext} ===\n"
                f"<complete file content>\n"
                f"```"
            )
            return (
                "Respond with a fenced code block containing the complete "
                "file. The first line inside the fence must be "
                "`# === FILE: <path> ===`:"
                f"\n\n{fenced_example}\n\n"
                "> "
            )

        # Multi-language / multi-file: universal shell example with
        # shebang. The shebang disambiguates "complete file" from
        # "shell command to execute" — project-universal, minimal,
        # shows the full `# === FILE: ===` marker + fence pattern.
        # The actual output language is inferred from task + instruction;
        # the envelope's only job is format demonstration.
        fenced_example = (
            "```sh\n"
            "# === FILE: path/to/file.sh ===\n"
            "#!/usr/bin/env sh\n"
            "<complete file content>\n"
            "```"
        )
        return (
            "Respond with one or more fenced code blocks, one per file. "
            "Each fence's first line must be `# === FILE: <path> ===`:"
            f"\n\n{fenced_example}\n\n"
            "> "
        )

    def _render_json_file_envelope(self) -> str:
        """Marker-free envelope for whole-file JSON writes.

        Terse and pointed: JSON can't carry the `# === FILE:` comment marker,
        and the single-file write step already knows the path via
        fallback_path = target — so just ask for the file.
        """
        return (
            "Respond with the complete file as one fenced ```json block — "
            "no `# === FILE:` marker (JSON has no comments):\n\n"
            "```json\n<complete file content>\n```\n\n"
            "> "
        )

    def _target_extension(self, namespaces: dict[str, Any]) -> str:
        """Lowercase extension of the turn's target file, or '' if unknown.

        Resolves `context.target_file_path` then `input.target_file_path` (the
        create/rewrite flows publish the latter). Lets the code envelope route
        JSON whole-file writes to their dedicated marker-free envelope.
        """
        for ref in ("context.target_file_path", "input.target_file_path"):
            val = self._resolve_path(ref, namespaces)
            if isinstance(val, str) and "." in val:
                return val.rsplit(".", 1)[-1].lower()
        return ""

    def _render_options(self, turn: TurnDefinition, namespaces: dict[str, Any]) -> str:
        """Render the options section for menu_single and menu_compound.

        Resolves options via `turn.response.options_from` (projection or
        context), `turn.response.options` (embedded), and/or
        `turn.response.stock` (stock options merged in). Emits a
        `## Options` heading followed by a bulleted list of
        `- **key** — description` entries. For menu_compound, each
        option's `arg` descriptor is shown inline so the model knows
        the argument the chosen option expects.
        """
        options = self.resolve_options(turn, namespaces)
        if not options:
            raise EmptyMenuError(
                "Menu turn has no options — options_from yielded nothing, "
                "response.options is empty, and no stock options declared. "
                "An empty menu has no shape the model can choose from."
            )

        lines = ["## Options", ""]
        for opt in options:
            key = opt["key"]
            description = opt.get("description", "")
            arg = opt.get("arg")  # {name, description} for menu_compound
            if arg:
                # menu_compound: show the arg name so the model knows
                # what payload to include with the choice.
                lines.append(f"- **{key}** (argument: `{arg['name']}`) — {description}")
            else:
                lines.append(f"- **{key}** — {description}")
        return "\n".join(lines)

    def resolve_options(
        self, turn: TurnDefinition, namespaces: dict[str, Any]
    ) -> list[dict[str, Any]]:
        """Assemble the full list of option dicts the model sees.

        Shared between _render_options (display) and runtime choice
        extraction (parsing) — same source-resolution logic so the
        option-key list the model picks from exactly matches the one
        the renderer showed.

        Each returned dict has at minimum a "key" and "description".
        menu_compound options also carry "arg": {"name", "description"}.
        Order is preserved: primary options first, then stock options
        (if any).
        """
        response = turn.response  # MenuResponseContract
        resolved: list[dict[str, Any]] = []

        # Merge order: options_from (projection/context) first, then
        # embedded `options` (flow-specific declarations), then stock.
        # The three are ADDITIVE — not mutually exclusive. pick_action
        # is the canonical three-way case: options_from yields the
        # symbol list from the context-published symbol_menu_options,
        # embedded `options` declares examine_another_file, stock
        # carries __all_symbols__ and __conclude__. Before this fix
        # the resolver used `if options_from else embedded`, silently
        # dropping every embedded option whenever options_from was
        # set — examine_another_file had been invisible on every
        # pick_action render since the menu landed.
        source = getattr(response, "options_from", None)
        embedded = getattr(response, "options", None)

        if source is not None:
            raw = self._resolve_option_source(source, namespaces)
            resolved.extend(self._normalize_raw_options(raw))

        if embedded:
            seen_so_far = {o["key"] for o in resolved}
            for key, opt in embedded.items():
                if key in seen_so_far:
                    continue
                entry = {
                    "key": key,
                    "description": opt.description,
                }
                if opt.arg is not None:
                    entry["arg"] = {
                        "name": opt.arg.name,
                        "description": opt.arg.description,
                    }
                resolved.append(entry)
                seen_so_far.add(key)

        # Stock options always merge in at the end. Deduplicate by key
        # in case a stock option was also listed in embedded/projection.
        seen = {o["key"] for o in resolved}
        for stock_opt in getattr(response, "stock", []) or []:
            if stock_opt.key in seen:
                continue
            entry = {
                "key": stock_opt.key,
                "description": stock_opt.description,
            }
            if stock_opt.arg is not None:
                entry["arg"] = {
                    "name": stock_opt.arg.name,
                    "description": stock_opt.arg.description,
                }
            resolved.append(entry)
            seen.add(stock_opt.key)

        return resolved

    def _resolve_option_source(
        self, source: OptionSource, namespaces: dict[str, Any]
    ) -> Any:
        """Fetch the raw option data the `options_from` spec points at."""
        src_kind = source.source
        if src_kind == "projection":
            if not source.projection:
                raise TurnRenderError(
                    "OptionSource source='projection' requires `projection` "
                    "field naming the projection slot."
                )
            # Projections land in inputs keyed by projection name
            # (see agent/loop.py::_materialize_projections).
            value = self._resolve_path(f"input.{source.projection}", namespaces)
            if value is None:
                raise TurnRenderError(
                    f"Projection {source.projection!r} not found in "
                    f"namespaces (expected at input.{source.projection}). "
                    f"Is the projection declared in the flow's "
                    f"`projections:` list?"
                )
            return value
        if src_kind == "context":
            if not source.context_key:
                raise TurnRenderError(
                    "OptionSource source='context' requires `context_key` "
                    "field naming the context entry."
                )
            value = self._resolve_path(f"context.{source.context_key}", namespaces)
            if value is None:
                raise TurnRenderError(
                    f"Context key {source.context_key!r} not present; "
                    f"options cannot be resolved."
                )
            return value
        if src_kind == "embedded":
            # Embedded should not come through options_from — that's
            # what turn.response.options is for. Accept but empty-out.
            return []
        raise TurnRenderError(
            f"Unknown OptionSource source {src_kind!r}; "
            f"expected 'projection', 'context', or 'embedded'."
        )

    def _normalize_raw_options(self, raw: Any) -> list[dict[str, Any]]:
        """Normalize the various shapes a projection/context entry can
        take into a uniform `[{key, description, arg?}, ...]` list.

        Accepted input shapes:
          - List of dicts with "id"/"key" + "description" (primary Pattern C shape)
          - List of dicts with "key" + "description" (embedded/stock shape)
          - Dict of key → {description, ...} (map shape)
        """
        out: list[dict[str, Any]] = []
        if isinstance(raw, list):
            for item in raw:
                if not isinstance(item, dict):
                    continue
                # Pattern C projections use "id"; embedded/stock use "key".
                key = item.get("key") or item.get("id")
                if not key:
                    continue
                entry: dict[str, Any] = {
                    "key": str(key),
                    "description": item.get("description", ""),
                }
                if "arg" in item and item["arg"] is not None:
                    entry["arg"] = item["arg"]
                out.append(entry)
            return out
        if isinstance(raw, dict):
            for key, val in raw.items():
                if isinstance(val, dict):
                    entry = {
                        "key": str(key),
                        "description": val.get("description", ""),
                    }
                    if "arg" in val and val["arg"] is not None:
                        entry["arg"] = val["arg"]
                    out.append(entry)
                elif isinstance(val, str):
                    out.append({"key": str(key), "description": val})
            return out
        raise TurnRenderError(
            f"Option source returned unexpected shape {type(raw).__name__}: "
            f"expected a list of dicts or a dict of key → description."
        )

    def _render_menu_envelope(
        self, turn: TurnDefinition, namespaces: dict[str, Any]
    ) -> str:
        """Emit the menu response envelope.

        menu_single: model emits `{"choice": "<option_key>"}`.
        menu_compound: model emits `{"choice": "<option_key>", "<arg_name>": "<value>"}`.

        Uniform shape for both: a terse one-line `Respond with JSON: ...`
        directive, blank line, then `> ` as the ready-for-input
        terminator. No fenced code block wrapping — the JSON is a
        one-liner and the model doesn't need format ceremony to remember
        to emit JSON. This matches the pre-Step-C menu convention which
        was empirically the most reliable shape in the codebase (22/22
        clean in the 892 trace).

        For menu_compound, the example's arg key name MUST match the
        actual arg.name of some option the model might pick — otherwise
        the model copies the placeholder and the runtime's arg
        extractor (which looks up the chosen option's arg.name) misses.
        The b6c trace showed this exact failure mode: the old envelope
        fell through to an "argument" placeholder; the model emitted
        {"choice": "__run_command__", "argument": "..."}, and
        execute_investigation_tool read pick_action_choice_arg as empty
        because the schema's __run_command__ option declares arg.name =
        "command", not "argument".

        Fix: consult the full resolved options list (flow-specific +
        stock), which `resolve_options` already merges consistently
        with what the model sees in the Options section.
        """
        shape = turn.response_shape

        # Resolve options once up front — both shapes need the key
        # list for the "Valid keys:" line that grounds the response.
        # Without this, the model sees `{"choice": "<option_key>"}` as
        # an abstract placeholder and invents content. The cce trace
        # showed the model responding `{"choice":"A"}` on the first
        # turn of a select_symbols session — reading the bolded option
        # list as a multiple-choice questionnaire and picking "A" for
        # question 1. The old pre-Step-C menu prompt closed this loop
        # by appending `Valid IDs: _load_yaml, _parse_items, ...`
        # after the "Respond with" line — the same shape this code
        # now restores, but driven off the resolved options so flow-
        # specific + stock keys are always consistent with what the
        # model saw in the Options section.
        try:
            resolved = self.resolve_options(turn, namespaces)
        except Exception:
            # Option resolution can fail if projection source isn't
            # wired yet during early prompt construction; fall back to
            # the embedded-only view rather than crashing the render.
            resolved = []
            response = turn.response
            if hasattr(response, "options") and response.options:
                for key, opt in response.options.items():
                    entry: dict[str, Any] = {"key": key}
                    if opt.arg is not None:
                        entry["arg"] = {"name": opt.arg.name}
                    resolved.append(entry)
            for stock_opt in getattr(turn.response, "stock", []) or []:
                entry = {"key": stock_opt.key}
                if stock_opt.arg is not None:
                    entry["arg"] = {"name": stock_opt.arg.name}
                resolved.append(entry)

        valid_keys = [opt["key"] for opt in resolved if opt.get("key")]
        valid_keys_line = (
            f"Valid keys: {', '.join(valid_keys)}\n\n" if valid_keys else ""
        )

        if shape == "menu_single":
            return (
                "Respond with one JSON object:\n"
                '{"choice": "<option_key>"}\n\n'
                f"{valid_keys_line}"
                "> "
            )

        # menu_compound: include the arg in the example. Use the first
        # option with an arg — resolved from the same merged list the
        # model sees, so stock-option args (like __run_command__'s
        # "command") are discovered. If no option has an arg, the
        # example has just the choice (same as menu_single, but the
        # shape is still declared compound).
        example_key = "option_key"
        example_arg_name = "argument"
        example_arg_val = "value"
        for opt in resolved:
            arg = opt.get("arg")
            if isinstance(arg, dict) and arg.get("name"):
                example_key = opt["key"]
                example_arg_name = arg["name"]
                break

        return (
            f"Respond with one JSON object:\n"
            f'{{"choice": "{example_key}", '
            f'"{example_arg_name}": "<{example_arg_val}>"}}\n\n'
            f"{valid_keys_line}"
            "> "
        )

    # ── Substitution / path resolution ────────────────────────────────

    def _substitute(self, template: str, namespaces: dict[str, Any]) -> str:
        """Substitute {namespace.path} references in a string.

        Missing values resolve to empty string. JSON braces and
        single-word placeholders pass through (the pattern requires a
        known namespace prefix followed by a dot)."""

        def _replacer(match: re.Match) -> str:
            value = self._resolve_path(match.group(1), namespaces)
            return "" if value is None else str(value)

        return _REF_PATTERN.sub(_replacer, template)

    def _resolve_path(self, path: str, namespaces: dict[str, Any]) -> Any:
        """Resolve a dotted path against namespaces.

        Identical semantics to PromptRenderer._resolve_path — dict-key
        lookup first, attribute lookup as fallback for object-ish
        namespace values."""
        parts = path.split(".")
        current = namespaces.get(parts[0])
        for part in parts[1:]:
            if current is None:
                return None
            if isinstance(current, dict):
                current = current.get(part)
            elif hasattr(current, part):
                current = getattr(current, part)
            else:
                return None
        return current
