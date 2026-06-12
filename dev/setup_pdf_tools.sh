#!/bin/bash
# Provision the isolated PDF-extraction toolchain (scraper v2).
#
# Creates tools/pdf_extract/.venv and converts PaddleOCR-VL-1.6 to MLX
# 8-bit if the model directory is absent. Idempotent; re-run safely.
# The conversion recipe was validated live (June 2026): ~3 minutes,
# ~9.6 bits/weight, quality within noise of the community 1.5 builds.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
TOOL="$ROOT/tools/pdf_extract"
VENV="$TOOL/.venv"
MODEL="$TOOL/models/PaddleOCR-VL-1.6-MLX-8bit"

echo "── venv ──"
uv venv "$VENV" --python 3.12 --allow-existing
uv pip install --python "$VENV/bin/python" \
    "paddlepaddle>=3.2.1" "paddleocr[doc-parser]>=3.0" \
    "paddlex[ocr,genai-client]" "mlx-vlm>=0.3.11" \
    "pymupdf>=1.24" "pillow>=10.0"

if [ ! -d "$MODEL" ]; then
  echo "── converting PaddleOCR-VL-1.6 → MLX 8-bit ──"
  "$VENV/bin/python" -m mlx_vlm convert \
    --hf-path PaddlePaddle/PaddleOCR-VL-1.6 \
    --mlx-path "$MODEL" -q --q-bits 8
else
  echo "── model present: $MODEL ──"
fi
echo "── smoke: imports ──"
"$VENV/bin/python" -c "import fitz, paddleocr, mlx_vlm; print('toolchain ok')"
