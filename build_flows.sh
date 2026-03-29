#!/bin/bash
# Generate compiled.json from CUE flow definitions.
# Requires: cue (https://cuelang.org/docs/install/)
#
# Usage: ./build_flows.sh
# Output: flows/compiled.json

set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

if ! command -v cue &> /dev/null; then
    echo "Error: 'cue' not found. Install with: brew install cue"
    exit 1
fi

echo "Validating CUE schemas..."
cue vet flows/cue/

echo "Exporting flows to JSON..."
cue export flows/cue/ --out json > flows/compiled.json

echo "Done. flows/compiled.json generated."
echo "Flow count: $(python3 -c "import json; d=json.load(open('flows/compiled.json')); print(sum(1 for v in d.values() if isinstance(v,dict) and 'flow' in v))")"
