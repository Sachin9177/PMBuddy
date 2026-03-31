#!/bin/bash
# run_prd.sh — Generate a PRD using PMBuddy

INPUT_FILE=${1:-"inputs/sample_prd_input.txt"}
PROMPT_FILE="prompts/generate_prd.md"
OUTPUT_DIR="outputs"
TIMESTAMP=$(date +"%Y%m%d_%H%M%S")
OUTPUT_FILE="$OUTPUT_DIR/prd_$TIMESTAMP.md"

if [ ! -f "$INPUT_FILE" ]; then
  echo "Error: Input file not found: $INPUT_FILE"
  exit 1
fi

echo "Running PRD generation..."
echo "Input:  $INPUT_FILE"
echo "Output: $OUTPUT_FILE"

# Combine prompt + input and send to Claude CLI
INPUT_CONTENT=$(cat "$INPUT_FILE")
PROMPT_CONTENT=$(cat "$PROMPT_FILE")
FULL_PROMPT="${PROMPT_CONTENT/\{\{INPUT\}\}/$INPUT_CONTENT}"

echo "$FULL_PROMPT" | claude --print > "$OUTPUT_FILE"

echo "Done. PRD saved to $OUTPUT_FILE"
