#!/bin/bash
# Set up the fig_review sidecar venv (deliberately LIGHT — mlx-vlm +
# pillow only; pdf_extract's paddle stack stays in its own venv).
set -euo pipefail
cd "$(dirname "$0")/../tools/fig_review"

if [ ! -d .venv ]; then
    uv venv .venv
fi
uv pip install --python .venv/bin/python -e .
echo "fig_review venv ready: tools/fig_review/.venv"
echo "Pass --model an MLX model dir OR an HF repo id (auto-downloads to"
echo "the HF cache), e.g.:  mlx-community/Qwen3-VL-8B-Instruct-8bit"
echo "(NOTE: the ~/.lmstudio Qwen3-VL MLX dirs are empty placeholders.)"
