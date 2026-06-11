# WikiKV: A Path-Indexed Key-Value Storage Model for LLM-Curated Hierarchical Knowledge Bases

**Haoliang Ming, Feifei Li, Xiaoqing Wu, Wenhui Que**

This repository contains the official code release for **WikiKV**, the storage,
deployment, and evolution layer behind LLM-curated hierarchical knowledge bases.
WikiKV encodes each node's path verbatim as its storage key (`/dim/entity`,
`/sources/...`), so that a directory listing is served by a single point lookup
on one directory node. On top of this storage core it provides a data-driven
schema layer (cold-start induction plus merge/split evolution operators), a
parent-after-child consistency protocol with a no-partial-read guarantee, a
path-keyed three-tier cache, and a budgeted, search-accelerated navigation
query operator.

WikiKV is the **systems/database-side** companion to the algorithm-side paper
*Retrieval as Reasoning: Self-Evolving Agent-Native Retrieval via LLM-Wiki*
([arXiv:2605.25480](https://arxiv.org/abs/2605.25480)). The agent-native
retrieval behavior and the content-level **Error Book** are inherited from that
work; the contributions implemented here are the storage encoding, the
consistency protocol, the cache, and the schema cold-start / evolution pipeline.

> **Paper (arXiv):** see [`arxiv.txt`](./arxiv.txt).

---

## Repository layout

```
release/
├── README.md
├── LICENSE
├── requirements.txt
├── arxiv.txt
├── Dockerfile / build_img.sh     # containerized deployment
├── configs/
│   ├── page_types.yaml           # default page-type catalog
│   ├── purpose_template.md       # positioning descriptor P = <focus, audience, ingestion-bias>
│   └── wiki-schema.md            # wiki schema specification
├── docs/                         # design notes
│   ├── architecture.md
│   ├── wiki-schema.md
│   ├── incr_pipeline_design.md
│   └── CHANGELOG_v1_to_v2.md
├── src/
│   ├── main.py                   # CLI entry point (ingest / query / lint / maintain / finalize)
│   ├── config.py                 # paths, LLM config, wiki-directory management
│   ├── llm_client.py             # OpenAI-compatible chat-completion client
│   ├── preprocess.py             # raw articles → cleaned Markdown
│   ├── preprocess_sampled.py     # corpus sampling for cold-start
│   ├── ingest.py                 # two-step ingestion + schema evolution (merge / split)
│   ├── lint.py                   # structural health check + LLM contradiction detection
│   ├── error_book.py             # cross-batch content self-correction
│   ├── wiki_page.py              # page / path model
│   ├── wiki_sync_kv.py           # path-as-key sync to the KV store (parent-after-child)
│   ├── wiki_sync_wfs.py          # file-system mirror
│   ├── init_hdfs_state.py        # storage-state bootstrap
│   ├── incr_pipeline.py          # incremental ingestion pipeline
│   ├── pipeline.py               # full construction pipeline
│   ├── fetch_articles.py         # corpus fetching helper
│   ├── pack_completed.py / reclassify_index.py / write_all_answers.py   # batch utilities
└── examples/
    └── run_demo.sh               # minimal end-to-end example
```

## Requirements

```bash
pip install -r requirements.txt
```

Python ≥ 3.10. The code calls any **OpenAI-compatible** chat-completion API
(OpenAI, Azure OpenAI, vLLM, Ollama, etc.) over HTTP.

## Configure the LLM backend

```bash
export OPENAI_API_KEY="sk-..."
export OPENAI_BASE_URL="https://api.openai.com/v1"   # or your local server

# Models used by the pipeline (override as needed):
export LLM_PREMIUM_MODEL="gpt-4o"       # strong model — synthesis / generation steps
export LLM_FAST_MODEL="gpt-4o-mini"     # fast model   — selection / analysis steps
```

To use the path-indexed KV backend, point WikiKV at your KV service:

```bash
export WIKI_KV_API_URL="http://your-kv-host:port"
# Optional: export WIKI_KV_PROXY_URL="http://your-proxy" if the service is not directly reachable.
```

## Quickstart

```bash
cd src

# 1. Place one Markdown article per file under  raw/<user>/  (or raw/articles/).
#    Copy configs/purpose_template.md to purpose_<user>.md and fill it in.

# 2. Ingest the corpus (cold-start schema induction runs on the first batch).
python3 main.py --user demo ingest-all --limit 500

# 3. Maintenance: schema evolution (merge / split) + Error Book repair.
python3 main.py --user demo maintain
python3 main.py --user demo finalize --max-rounds 3

# 4. Query the compiled wiki.
python3 main.py --user demo query "What does this corpus say about X?"

# 5. Health check.
python3 main.py --user demo lint           # add --llm for contradiction detection
```

The compiled wiki is written to `src/<user>_wiki/wiki/`. See
[`examples/run_demo.sh`](./examples/run_demo.sh) for the full flow.

## Syncing to the KV store

WikiKV materializes the wiki into a path-keyed KV namespace using the
parent-after-child write protocol:

```bash
cd src
python3 wiki_sync_kv.py --user demo --api-url "$WIKI_KV_API_URL"
```

## Containerized deployment

```bash
bash build_img.sh        # builds the image from Dockerfile
```

## Schema model

WikiKV uses a fixed four-level path schema with five node types
(`Index → Dimension → Entity → {Digest, Document}`); see
[`configs/wiki-schema.md`](./configs/wiki-schema.md) and
[`docs/architecture.md`](./docs/architecture.md) for the full specification.

## Citation

If you find this code useful, please cite the WikiKV paper (see `arxiv.txt`)
and the companion LLM-Wiki paper:

```bibtex
@misc{ming2026retrievalreasoningselfevolvingagentnative,
      title={Retrieval as Reasoning: Self-Evolving Agent-Native Retrieval via LLM-Wiki},
      author={Haoliang Ming and Feifei Li and Xiaoqing Wu and Wenhui Que},
      year={2026},
      eprint={2605.25480},
      archivePrefix={arXiv},
      primaryClass={cs.CL},
      url={https://arxiv.org/abs/2605.25480},
}
```

## License

Released under the MIT License. See [`LICENSE`](./LICENSE).
