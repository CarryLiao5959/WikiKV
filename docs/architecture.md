# WikiKV Architecture

This note summarizes the offline construction-and-evolution pipeline and the
online query path of WikiKV. It is intentionally implementation-agnostic: all
external services (LLM endpoint, KV store, optional distributed filesystem) are
configured via environment variables (see the top-level README).

## Overview

```
        raw articles (Markdown)
                  │
                  ▼
        preprocess.py            cleaning / normalization
                  │
                  ▼
        ingest.py                two-step LLM ingestion
          ├─ cold-start schema induction (first batch)
          ├─ page generation
          └─ schema evolution: DimensionMerge / PageSplit
                  │
                  ▼
        error_book.py            content-level self-correction (cross-batch)
                  │
                  ▼
        wiki_sync_kv.py          path-as-key sync to the KV store
                                 (parent-after-child write protocol)
```

The online tier serves read-only navigation queries over the path-keyed
namespace, optionally fronted by a three-tier cache (in-process / shared /
KV). The read and write paths share no synchronous coordination beyond the
parent-after-child protocol.

## Wiki directory model

Each knowledge base is an independent directory tree with a fixed four-level
path schema and five node types:

```
/                     Index   (root)
/<dimension>          Dimension
/<dimension>/<entity> Entity
/sources/digests/...  Digest      (deepest level)
/sources/articles/... Document    (deepest level)
```

A node's path is used verbatim as its KV key, so a directory listing is served
by a single point lookup on the directory record (which co-locates its child
segments). See [`wiki-schema.md`](./wiki-schema.md) for the page-level layout.

## Components

| File | Role |
|------|------|
| `preprocess.py`        | raw articles → cleaned Markdown |
| `preprocess_sampled.py`| corpus sampling for cold-start |
| `ingest.py`            | two-step ingestion + schema evolution operators |
| `error_book.py`        | cross-batch content self-correction |
| `lint.py`              | structural health check + LLM contradiction detection |
| `wiki_page.py`         | page / path model |
| `wiki_sync_kv.py`      | path-as-key sync to the KV store |
| `wiki_sync_wfs.py`     | optional filesystem mirror |
| `incr_pipeline.py`     | incremental ingestion pipeline |
| `pipeline.py`          | full construction pipeline |
| `main.py`              | CLI entry point |

## Incremental updates

`incr_pipeline.py` maintains a small JSON state file recording which source
documents have already been ingested per knowledge base. On each run it reads
new articles from the local data directory, ingests only the unseen ones, and
updates the state. The state file can optionally be persisted to a distributed
filesystem (configure `WIKI_HDFS_BASE`); by default it lives on the local disk.

## Online query path

Online queries traverse the wiki through `wiki_search` / `wiki_read`-style
operations exposed to an agent. The agent composes these calls, follows
wikilinks, and checks evidence sufficiency before producing an answer; the
budgeted navigation operator (paper §V) bounds the number of LLM-assisted hops.
