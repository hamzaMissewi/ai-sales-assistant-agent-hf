#!/bin/sh
set -e

if [ -z "$LLM_API_KEY" ] || [ "$LLM_API_KEY" = "your_key_here" ]; then
  echo "WARNING: LLM_API_KEY not set."
  echo "         In the Space: Settings > Variables and secrets, set:"
  echo "         LLM_BASE_URL, LLM_API_KEY, LLM_MODEL (and optionally FALLBACK_LLM_API_KEY)."
fi

for c in clients/*/; do
  [ -f "$c/config.yaml" ] || continue
  echo "Indexing $(basename "$c")"
  python ingest.py "$(basename "$c")"
done

exec uvicorn server:app --host 0.0.0.0 --port "${PORT:-7860}"