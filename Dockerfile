# ============================================================================
# WikiKV image
# Responsibilities:
#   1. wiki_sync_kv.py   — sync the compiled wiki to the path-indexed KV store
#   2. pipeline.py       — full construction pipeline (preprocess -> ingest -> sync)
#   3. incr_pipeline.py  — incremental update pipeline (new articles only)
#   4. init_hdfs_state.py — (optional) initialize HDFS incremental state
# ============================================================================
FROM python:3.11-slim

WORKDIR /app

# ─── System dependencies ───
RUN apt-get update && \
    apt-get install -y --no-install-recommends zip unzip ca-certificates && \
    rm -rf /var/lib/apt/lists/*

# ─── Python dependencies ───
COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

# ─── Application code ───
COPY src/ /app/

# ─── Configuration (page-type catalog, schema, purpose template) ───
COPY configs/ /app/configs/

# Provide your corpus by mounting Markdown articles under /app/raw/<user>/ and
# a positioning descriptor purpose_<user>.md (see configs/purpose_template.md).
# Configure the LLM and KV backends via environment variables, e.g.:
#   OPENAI_API_KEY, OPENAI_BASE_URL, LLM_PREMIUM_MODEL, LLM_FAST_MODEL,
#   WIKI_KV_API_URL, WIKI_HDFS_BASE.

CMD ["/bin/bash"]
