#!/bin/bash
# ============================================================================
# WikiKV image — build script
# Builds a self-contained image from the Dockerfile in this directory.
# Usage: ./build_img.sh [tag]
# ============================================================================
set -e

BASEPATH=$(dirname "$(readlink -f "$0")")
cd "$BASEPATH"

TAG="${1:-wikikv:latest}"

echo "============================================"
echo "  Building image: ${TAG}"
echo "============================================"

docker build -t "${TAG}" -f ./Dockerfile .

echo ""
echo "============================================"
echo "  Build complete: ${TAG}"
echo "============================================"
echo "  Run it with:"
echo "    docker run -it \\"
echo "      -e OPENAI_API_KEY=sk-... \\"
echo "      -e OPENAI_BASE_URL=https://api.openai.com/v1 \\"
echo "      -e WIKI_KV_API_URL=http://your-kv-host:port \\"
echo "      -v \$(pwd)/raw:/app/raw \\"
echo "      ${TAG}"
echo ""
echo "  Inside the container, e.g.:"
echo "    python main.py --user demo ingest-all --limit 500"
echo "    python main.py --user demo maintain"
echo "    python wiki_sync_kv.py --user demo --api-url \$WIKI_KV_API_URL"
echo "============================================"
