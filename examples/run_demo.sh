#!/usr/bin/env bash
# Minimal end-to-end example for WikiKV:
#   (1) ingest a small corpus into a hierarchical wiki,
#   (2) run maintenance (merge/split + Error Book),
#   (3) query the wiki, and (4) lint it for structural health.
# Run from the `release/` directory.

set -euo pipefail

# 1. Configure the LLM backend (any OpenAI-compatible server works).
export OPENAI_API_KEY="${OPENAI_API_KEY:-sk-replace-me}"
export OPENAI_BASE_URL="${OPENAI_BASE_URL:-https://api.openai.com/v1}"
export LLM_PREMIUM_MODEL="${LLM_PREMIUM_MODEL:-gpt-4o}"       # strong model — synthesis / generation
export LLM_FAST_MODEL="${LLM_FAST_MODEL:-gpt-4o-mini}"       # fast model   — selection / analysis

cd "$(dirname "$0")/../src"

USER="${USER_KEY:-demo}"

# Place one Markdown file per article under  raw/<USER>/  (or raw/articles/).
# Provide a positioning descriptor by copying configs/purpose_template.md to
# purpose_<USER>.md and filling in the four sections.

# 2. Ingest the corpus (cold-start schema induction happens on the first run).
python3 main.py --user "$USER" ingest-all --limit 500

# 3. One-shot maintenance: schema evolution (merge/split) + Error Book repair.
python3 main.py --user "$USER" maintain
python3 main.py --user "$USER" finalize --max-rounds 3

# 4. Query the wiki.
python3 main.py --user "$USER" query "What does this corpus say about X?"

# 5. Health check (add --llm for LLM-based contradiction detection).
python3 main.py --user "$USER" lint

echo
echo "Done. The compiled wiki lives under  src/${USER}_wiki/wiki/"
