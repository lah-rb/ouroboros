# configs/

```
configs/
  <model>.yaml       one servable config per model — the thing you'd actually run
  boss/              remote-provider entries (claude_cli, openai_compat)
  experiments/       variants that exist to answer a question
  archive/           retired; NEVER resolved and never listed
```

A bare name resolves against root → `boss/` → `experiments/`, in that order.
Root shadows a variant that copied its name; a name present in **both**
`boss/` and `experiments/` is a hard error rather than a silent pick.

`archive/` is deliberately outside resolution. Retiring a config means it stops
answering to its name — otherwise "archived" is just another namespace.

## What lists vs what resolves

`swapModel` and the registry list **root + boss/** only. Experiments still
resolve by name (pointer file, scripts, an explicit swap) but stay out of the
menu; `OURO_LIST_EXPERIMENTS=1` opts them in for a session.

Boss entries are addressed per-request via the completion `model` field and
compose on top of whichever model is resident — they are never swap targets.

## extends:

An experiment carries only its **diff**:

```yaml
extends: laguna-s-2.1-apex

model:
  name: Laguna-S-2.1-APEX-think
  thinking: true
generation:
  logit_bias: {25: -100.0}
```

Boot logs what resolved, and you should read it rather than trust the file:

```
🧬 Config laguna-s-2.1-apex-think extends laguna-s-2.1-apex
   — overrides: model.name, model.thinking, generation.logit_bias
```

**Rules, and the reason for each:**

- **One level.** `extends` may not point at something that itself extends. A
  chain means you cannot answer "what is this config" without walking a graph,
  which is the property the flat directory had and the one worth keeping.

- **Key presence is the override signal, not value.** A key you write wins even
  when its value is `null`; a key you omit is inherited. So `null` means
  *reset to this field's default*.

  This matters because the schema encodes meaning in `None` and the meaning is
  **not uniform** — `temperature_floor: null` is disabled,
  `repetition_guard_enabled: null` is ENABLED, the rope/yarn fields mean "don't
  pass this to llama.cpp at all", `kv_preflight_gb: null` is 100. A value-based
  merge could never take a field back to its default, and which behaviour you
  got instead would differ per field.

- **Sections merge; values replace.** `model:`, `generation:` and the other
  Pydantic sub-models merge key by key. Everything else replaces wholesale —
  including lists and dict-valued *fields* like `generation.logit_bias` and
  `personas`.

  Not a nicety: `laguna-s-2.1-apex` bans token 19 (`</think>`) via `logit_bias`
  because thinking is off there. A thinking variant that inherited that mapping
  key-by-key would silently ban the token it depends on.

- **`results:` and `notes:` are documentation**, stripped before validation.
  `Config` is `extra="forbid"` and stays that way; these are listed explicitly
  in `DOC_ONLY_KEYS`. Record what an experiment answered *with* the experiment —
  a config that has served its purpose should say so.

## Per-machine settings

The configs are SHARED between machines (this repo runs on a CUDA box and an
Apple Silicon box), but weights paths, device layouts and context ceilings
are properties of a HOST. Three mechanisms keep one tracked file honest on
both, and none of them edits a shared yaml:

- **`active_config.txt`** (gitignored) — which config the server boots. Per
  machine by construction; the tracked `active_config.txt.example` is the
  fallback. Tracking it once cost three rebases in an afternoon.

- **`LLMVP_MODELS_ROOT`** — the models directory on THIS host. A `model.path`
  or `model.mmproj_path` that does not exist as written is re-rooted onto
  `$LLMVP_MODELS_ROOT/<basename>`; a path that resolves is never touched, so a
  correct config is never second-guessed. Applies on boot **and** on
  `loadModel`/`swapModel` (both go through the same loader since
  2026-09-06 — before that a hot-load read the raw yaml and died at the other
  machine's absolute path). The redirect is by BASENAME: keep the file names
  the yaml states, or symlink to them. Set it in the server's environment:

  ```
  cd llmvp && LLMVP_MODELS_ROOT=<models dir on this host> \
      [LD_LIBRARY_PATH=/home/lah-rb/cuda-libs   # CUDA hosts: the pinned cuBLAS] \
      setsid nohup .venv/bin/python api/main.py > ~/tmp/llmvp_launch_$(date +%Y%m%d-%H%M%S).log 2>&1 &
  ```

- **`LLMVP_N_CTX`** — this host's context ceiling for the PRIMARY it serves
  (clamped to the model's `probe_verified_n_ctx`). Primary only, deliberately:
  a secondary like paddle keeps its own minimal `n_ctx`.

**Device layout is a variant, not an env var.** A model whose base yaml
describes one box's GPUs gets `<model>-<host>.yaml` with `extends:` and ONLY
the device keys (`main_gpu`, `split_mode`, `tensor_split`,
`vision_projector_device`, `vision_pool_size`) — see `paddle-ocr-vl-mac.yaml`.
Two rules: restate `model.name` to the variant's own stem (the vision path
reports `visionModel = model.name`, and a strict client refuses a mismatch),
and have the client name the variant — a mission's `llmvp_domains["ocr"]`
carries `{"endpoint": ..., "model": "paddle-ocr-vl-mac"}` because a registry
name is host-local.

## Adding a config

A new **model** goes in root, self-contained. Compute its KV geometry from the
GGUF headers *before* choosing `n_ctx` — and note the preflight works in decimal
**GB**, not GiB; sizing a config in GiB is how a load gets refused for being ~7%
over a budget that looked right.

A new **experiment** goes in `experiments/` with `extends:` and the smallest
diff that expresses the question.
