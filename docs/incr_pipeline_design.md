# Incremental Ingestion Pipeline Design

`incr_pipeline.py` performs incremental wiki updates: instead of rebuilding a
knowledge base from scratch, it ingests only source documents that have not been
seen before, based on a persisted state file.

## Full vs. incremental

| Aspect | Full pipeline (`pipeline.py`) | Incremental (`incr_pipeline.py`) |
|--------|-------------------------------|----------------------------------|
| State dependency | none (starts from zero) | depends on a persisted incremental state |
| Scope | ingests the whole corpus | ingests only new articles |
| Maintenance | merge/split every K articles | typically skipped (few new articles per run) |

## Flow

```
[1] load incremental state  (local file; optionally a distributed filesystem)
[2] read new source articles from the local data directory
[3] preprocess.py: CSV / Markdown → cleaned articles
[4] ingest only the unseen articles
[5] save updated incremental state
```

State is saved after each knowledge base completes, so a crash does not lose
progress already made.

## State file

The incremental state is a JSON file keyed by knowledge base, e.g.:

```json
{
  "demo": {
    "processed_source_ids": ["2024-01-01-title-a", "2024-01-02-title-b"],
    "total_ingested": 2,
    "updated_at": "2026-01-01T00:00:00"
  }
}
```

By default the state file lives on the local disk. To persist it on a
distributed filesystem instead, set `WIKI_HDFS_BASE` (and ensure a `hadoop`
client is on `PATH`); the pipeline then reads from the remote path first and
falls back to the local copy.

## Data input

Source articles are read from the local data directory. Prepare one CSV (or
Markdown) file per knowledge base under the data directory (configurable via
`WIKI_CSV_DIR`); `preprocess.py` reads from there and never accesses remote
storage directly.

## Command-line options

| Option | Default | Meaning |
|--------|---------|---------|
| `--users`, `-u` | all | comma-separated knowledge-base keys, e.g. `--users demo` |
| `--hdfs-state`  | local | path to the incremental state file |
| `--no-hdfs`     | false | use only the local state file |
| `--dry-run`     | false | do not write any state / KV output |

## Failure handling

State is written locally first and then (optionally) uploaded to the
distributed filesystem, so a failure during upload leaves a valid local state
that the next run can resume from.
