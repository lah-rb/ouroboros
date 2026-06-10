# LLMVP: GGUF Metadata-Driven Configuration

**Status:** Next improvement task  
**Priority:** High — eliminates per-model config errors and enables new-model onboarding without manual template verification  
**Depends on:** Current session's format template fixes (BOS, stop tokens, thinking flag)

---

## Problem

LLMVP currently determines model behavior from two sources:

1. **Format schema** (`formats/{family}.yaml`) — hardcoded per-family template structure
2. **Model config** (`configs/{model}.yaml`) — manual per-model overrides (`thinking: false`, etc.)

Both require the operator to know the model's training conventions and set them correctly. When a new model is onboarded, the operator must:

- Know whether it needs BOS (check HuggingFace or GGUF metadata)
- Know whether it supports thinking tags (read the model card)
- Know the correct EOS token (may differ from the family default)
- Set `thinking: false` manually for non-thinking models in a thinking family

This is error-prone. The GGUF file itself contains authoritative metadata about all of these — the same metadata that llama.cpp's `--jinja` mode reads to apply the correct template automatically.

## Solution

Read GGUF metadata at model load time and use it to configure behavior that currently requires manual setup. The format schema remains the structural authority (how to render system/user/assistant blocks), but runtime details come from the model file itself.

## Available Metadata

The `Llama` object in llama-cpp-python exposes metadata via `llm.metadata` (a dict) and dedicated methods. Key fields:

### Tokenizer Metadata

```python
# Access pattern (from a Llama instance):
llm.metadata.get('tokenizer.ggml.add_bos_token')   # 'true' / 'false' (string)
llm.metadata.get('tokenizer.ggml.add_eos_token')    # 'true' / 'false'
llm.metadata.get('tokenizer.ggml.bos_token_id')     # e.g. '1' (string)
llm.metadata.get('tokenizer.ggml.eos_token_id')     # e.g. '2' or '151643'

# Dedicated methods (return int token IDs):
llm.token_bos()   # BOS token ID
llm.token_eos()   # EOS token ID

# Text representation of special tokens (internal API):
llm._model.token_get_text(token_id)  # e.g. '<s>', '</s>', '<|im_end|>'
```

### Model Identity

```python
llm.metadata.get('general.name')           # e.g. 'Devstral-Small-2-24B-Instruct-2512'
llm.metadata.get('general.architecture')   # e.g. 'llama', 'qwen2', 'mistral'
llm.metadata.get('general.finetune')       # e.g. 'Devstral-Small-2-24B-Instruct-2512'
```

### Chat Template

```python
llm.metadata.get('tokenizer.chat_template')  # Full Jinja template string
```

## Implementation Plan

### Phase 1: Metadata Reader Module

Create `inference/metadata.py` — a module that reads GGUF metadata from a loaded `Llama` instance and produces a structured `ModelMetadata` dataclass:

```python
@dataclass
class ModelMetadata:
    """Metadata extracted from a loaded GGUF model file."""
    
    # Identity
    name: str                    # general.name
    architecture: str            # general.architecture
    
    # BOS/EOS behavior
    add_bos: bool                # tokenizer.ggml.add_bos_token
    add_eos: bool                # tokenizer.ggml.add_eos_token
    bos_token_id: int            # llm.token_bos()
    eos_token_id: int            # llm.token_eos()
    bos_token_text: str          # text representation of BOS token
    eos_token_text: str          # text representation of EOS token
    
    # Chat template (for validation)
    chat_template: str | None    # tokenizer.chat_template (Jinja)
    
    # Derived flags
    has_thinking: bool | None    # inferred from chat_template content
```

The reader function:

```python
def read_metadata(llm_instance) -> ModelMetadata:
    """Extract metadata from a loaded Llama instance."""
    meta = llm_instance.metadata
    
    add_bos = meta.get('tokenizer.ggml.add_bos_token', 'false').lower() == 'true'
    
    chat_template = meta.get('tokenizer.chat_template', '')
    has_thinking = None
    if '<think>' in chat_template or '[THINK]' in chat_template:
        has_thinking = True
    elif chat_template and '<think>' not in chat_template:
        has_thinking = False
    
    return ModelMetadata(
        name=meta.get('general.name', 'unknown'),
        architecture=meta.get('general.architecture', 'unknown'),
        add_bos=add_bos,
        add_eos=meta.get('tokenizer.ggml.add_eos_token', 'false').lower() == 'true',
        bos_token_id=llm_instance.token_bos(),
        eos_token_id=llm_instance.token_eos(),
        bos_token_text=llm_instance._model.token_get_text(llm_instance.token_bos()),
        eos_token_text=llm_instance._model.token_get_text(llm_instance.token_eos()),
        chat_template=chat_template or None,
        has_thinking=has_thinking,
    )
```

### Phase 2: Wire Metadata Into Static Token Builder

Currently `preprocessing/cli.py` and `core/lifecycle.py` check the schema's `bos` field to decide `add_bos`. Replace with metadata:

```python
# In _build_bare_static_tokens() and cli.py main():
metadata = read_metadata(tokenizer_instance)
token_ids = tokenize_text(tokenizer, bare_text, add_bos=metadata.add_bos)
```

The challenge: the tokenizer used for `--prep` is a `vocab_only=True` Llama instance. Need to verify that `metadata` and `token_bos()`/`token_eos()` work on vocab-only instances.

### Phase 3: Validate Schema Against Metadata

At backend startup, after loading the model and reading metadata, cross-check against the format schema:

```python
def validate_schema_against_metadata(schema: FormatSchema, meta: ModelMetadata):
    """Warn about mismatches between schema assumptions and model reality."""
    
    warnings = []
    
    # BOS mismatch
    schema_needs_bos = bool(schema.tokens.bos)
    if schema_needs_bos != meta.add_bos:
        warnings.append(
            f"Schema expects add_bos={schema_needs_bos} but model metadata "
            f"says add_bos_token={meta.add_bos}"
        )
    
    # EOS token mismatch
    if schema.tokens.gen_stop and meta.eos_token_text:
        if schema.tokens.gen_stop != meta.eos_token_text:
            warnings.append(
                f"Schema gen_stop={schema.tokens.gen_stop!r} but model "
                f"eos_token={meta.eos_token_text!r}"
            )
    
    # Thinking capability
    config = get_config()
    if meta.has_thinking is not None:
        if config.model.thinking != meta.has_thinking:
            warnings.append(
                f"Config thinking={config.model.thinking} but model template "
                f"{'contains' if meta.has_thinking else 'lacks'} thinking tags"
            )
    
    for w in warnings:
        log.warning(f"⚠️ Schema/metadata mismatch: {w}")
    
    return warnings
```

This runs at startup and logs warnings but doesn't block — the operator's explicit config takes precedence, but they get alerted about potential issues.

### Phase 4: Auto-Configure from Metadata

For fields where metadata is authoritative, use it directly instead of requiring config:

```python
# In the backend initialization, after loading the model:
meta = read_metadata(llm_instance)

# Override config.model.thinking if not explicitly set
if not config_explicitly_sets('thinking'):
    config.model.thinking = meta.has_thinking or False

# Store metadata for use by tokenizer and renderer
set_model_metadata(meta)
```

The `tokenize_text` function can then check stored metadata:

```python
def tokenize_text(tokenizer, text, add_bos=None):
    if add_bos is None:
        # Auto-detect from metadata
        meta = get_model_metadata()
        add_bos = meta.add_bos if meta else False
    ...
```

### Phase 5: EOS Token Cross-Check

The stop sequence detector currently uses the schema's `gen_stop` field. Metadata provides the actual EOS token ID. Add a cross-check:

```python
# In generate_stream_sync, alongside the text-based stop detection:
model_eos_id = meta.eos_token_id
if token == model_eos_id:
    # Model emitted its metadata-declared EOS — always stop
    break
```

This is more robust than `llama_token_is_eog()` which checks the model's built-in EOG flag — some quantizations or conversions may not set this correctly.

## Architecture Considerations

### Where metadata lives

The metadata is read from the `Llama` instance which lives in the backend pool. The backend already stores `_static_state` per-pool — add `_model_metadata: ModelMetadata` alongside it.

The metadata needs to be accessible from:
- `preprocessing/cli.py` (for `--prep` BOS decision)
- `core/lifecycle.py` (for `--skip-knowledge` BOS decision)
- `core/inference.py` (for EOS cross-check)
- `formats/renderer.py` (for thinking flag)

A module-level accessor (`get_model_metadata()`) in the metadata module handles this. It's populated once at backend init and read-only thereafter.

### Tokenizer-only instances

For `--prep` and `--skip-knowledge`, we create a `vocab_only=True` Llama instance for tokenization. This instance has access to metadata and `token_bos()`/`token_eos()` but cannot do inference. Need to verify that all metadata fields are available on vocab-only instances — they should be since metadata is in the GGUF header, not the tensor data.

### Backward compatibility

The schema `bos` field and config `thinking` flag remain as manual overrides. Metadata auto-detection provides defaults that the operator can override. The validation step warns about conflicts but doesn't block.

## Migration Path

1. **✅ Phase 1-2 (DONE)**: Metadata reader (`inference/metadata.py`) + BOS from GGUF metadata. Schema `bos` field **removed** (clean break, no legacy). Thinking detection from chat template (informational). Startup logging compares metadata vs config.
2. **Phase 3**: EOS cross-check against schema `gen_stop`. Warn on mismatch at startup.
3. **Phase 4**: Auto-configure thinking flag from metadata. Remove `thinking: true/false` from model configs (clean break).
4. **Phase 5**: EOG-aware stop detection. Use metadata EOS token ID as additional stop signal alongside text-based `gen_stop`.

## Fields Summary

| Metadata Key | Previous Source | Current Source | Status |
|---|---|---|---|
| `add_bos_token` | Schema `bos` field | GGUF metadata | ✅ Done — schema field removed |
| `eos_token_id` | Schema `gen_stop` | Schema (with metadata cross-check logged) | Phase 3 |
| `chat_template` | Not used | GGUF metadata (thinking detection) | ✅ Read, informational |
| `add_eos_token` | Not used | GGUF metadata | Captured, future use |
| `general.architecture` | Config `family` | GGUF metadata | Captured, future use |
| `general.name` | Config `name` | GGUF metadata | ✅ Logged at startup |

## Testing

- Load each model config and verify metadata matches expectations
- Test `--prep` and `--skip-knowledge` with metadata-driven BOS
- Test `--collect-training` with Devstral (BOS=true) and Qwen (BOS=false)
- Verify no double-BOS warnings in llama.cpp output
- Test a model with mismatched schema (e.g. Tekken schema but `add_bos_token=false`) to verify validation warnings fire
