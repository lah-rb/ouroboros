# spectra-demo

A live demo that runs OLMo 2 1B base and the v3l fine-tune side by side on canned probes, both quantized, on candle. The prompts are byte-identical to the §22l probes: FIM-wrapped stripped records with digit-spaced bands.

## On the laptop

You need two model files and this folder (without `target*/`):

- `~/models/spectra-demo/olmo2-1b-v3l-q8_0.gguf` (1.6 GB)
- `~/models/spectra-demo/olmo2-1b-base-q8_0.gguf` (1.6 GB)

Each `.gguf` carries its own config and tokenizer. Build and serve:

```sh
cargo build --release                    # CPU. Apple Silicon: --features metal. NVIDIA: --features cuda
./target/release/spectra-demo serve \
    --model v3l=/path/to/olmo2-1b-v3l-q8_0.gguf \
    --model base=/path/to/olmo2-1b-base-q8_0.gguf
# open http://127.0.0.1:8080/
```

- `rust-toolchain.toml` pins Rust 1.90; rustup fetches it on the first build.
- The server picks the device itself (CUDA, then Metal, then CPU). `--device cpu` overrides.
- RAM is about 2.5 GB per model on CPU.

Deep links open a view directly:

- `#polymorphs`
- `#name>bands`
- `#formula>name`
- adding `?autorun` before the hash runs every card on load

Without the server (for example `python3 -m http.server` inside `static/`), the page shows the recorded Q8_0 outputs.

## Speed (per probe, v3l)

| where | prefill + answer |
|---|---|
| i9-13900KF, CPU, Q8_0 | ~2 s (the base model runs to its 24-token cap, ~3 s) |
| RTX 3090, CUDA, Q8_0 | ~0.07 s |

On CPU, candle's quantized matmul reads the whole weight matrix once per prompt token. So prefill (~40–70 tokens) costs more than the answer does.

## Why Q8_0, not Q6_K

Measured on the §22l T group: 60 trained species, PSM, the demo's three pairs, HITs out of 60.

| build | bands → name | name → formula | name → bands |
|---|---|---|---|
| HF bf16 (the published probes) | 54 | 46 | 58 |
| candle f32 (unquantized, same runtime) | 54 | 46 | 58 |
| candle Q8_0, CPU | 53 | 45 | 58 |
| candle Q8_0, CUDA | 54 | 46 | 57 |
| candle Q6_K, CPU | 53 | 40 | 51 |

- The runtime is exact: f32 matches bf16 on all 180 verdicts.
- Q6_K loses 6–7 points on the forward numeric recall.
- On the canned set, Q8_0 and Q6_K give the same verdicts as bf16.
- The Q6_K files (1.2 GB each) are in `~/models/spectra-demo/` if RAM is tight.

## Rebuilding the pieces

```sh
cd dev/rock_olmo
./.venv/bin/python candle_demo/build_probes.py            # static/probes.json + validation.json, bf16 references (GPU)
cd candle_demo
./target/release/spectra-demo quantize --src ~/models/olmo2-1b-spectra-full/v3_stage2l/final \
    --out ~/models/spectra-demo/olmo2-1b-v3l-q8_0.gguf --dtype q8_0
./target/release/spectra-demo check-tokens --model ~/models/spectra-demo/olmo2-1b-v3l-q8_0.gguf --probes static/probes.json
./target/release/spectra-demo run --model ~/models/spectra-demo/olmo2-1b-v3l-q8_0.gguf \
    --probes validation.json --out /tmp/v.json
cd .. && ./.venv/bin/python candle_demo/check_quant.py --probes candle_demo/validation.json --run /tmp/v.json --model v3l
# --probes static/probes.json ... --record stores a run as the page's recorded fallback
node candle_demo/test_probe_lib.js                         # the page's prompt builders and scorers vs Python
```
