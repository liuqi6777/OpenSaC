# Local dense search

OpenSAC includes a standalone dense retrieval service migrated from
`DeepResearch-dev`. It loads a ready-made FAISS index; it does not build or retrain an index.
The service is compatible with OpenSAC's `local` backend and owns search-result snippet
shaping.

## 1. Install

Python 3.12 is required. The project uses standard-library `venv` and pip; uv is not required:

```bash
./local_search/run setup
```

The setup command creates `local_search/.venv` and installs FAISS, PyTorch, Transformers, and tiktoken into
`local_search/.venv`. These packages are not part of the root OpenSAC project or its lockfile
because the embedding model and runtime are large. Index download uses the Python standard
library and therefore adds no direct Hugging Face SDK dependency.

To use another Python 3.12 executable or a pre-existing virtual environment:

```bash
LOCAL_SEARCH_PYTHON=/path/to/python3.12 ./local_search/run setup
LOCAL_SEARCH_VENV=/path/to/venv ./local_search/run
```

## 2. Download the existing index

The default source is
[`liuqi6777/Browsecomp-Plus-Indexes`](https://huggingface.co/datasets/liuqi6777/Browsecomp-Plus-Indexes):

```bash
./local_search/run prepare
```

This downloads only these four files into `local_search/indexes/browsecomp-plus/`:

```text
index.faiss
index.faiss.metadata.json
index_ids.json
corpus.jsonl
```

The directory is ignored by Git. To pin a reproducible dataset revision:

```bash
./local_search/run prepare --revision COMMIT_SHA
```

Use `--repo-id OWNER/DATASET` or `--output-dir PATH` for another repository or location.

## 3. Start the service

```bash
./local_search/run
```

The service runs in the foreground; press `Ctrl-C` to stop it. Arguments after `run` are passed
directly to the server.

The first start downloads and loads `Qwen/Qwen3-Embedding-8B`, so it can take several minutes.
`--device auto` uses CUDA when available and otherwise uses CPU. To select a GPU explicitly:

```bash
./local_search/run --device cuda:0
```

CPU mode works but an 8B embedding model requires substantial RAM and is much slower. The
exact embedding model, query prefix, max length, and pooling must match the values used to
produce the index. The provided defaults match the accompanying metadata.

The independent service accepts `LOCAL_SEARCH_*` environment variables; they are deliberately
kept out of OpenSAC's `.env.example`. Important paths are:

```bash
export LOCAL_SEARCH_INDEX_PATH=local_search/indexes/browsecomp-plus/index.faiss
export LOCAL_SEARCH_CORPUS_PATH=local_search/indexes/browsecomp-plus/corpus.jsonl
export LOCAL_SEARCH_INDEX_IDS_PATH=local_search/indexes/browsecomp-plus/index_ids.json
```

`index_ids.json` defines the mapping from each FAISS vector position to a document id. The
loader does not assume that `corpus.jsonl` has the same row order. Corpus records may use
either `{"id", "contents"}` or `{"docid", "text"}` fields.

## 4. Verify the API

Health check:

```bash
curl -fsS http://127.0.0.1:8081/healthz
```

Single and batch search:

```bash
curl -fsS http://127.0.0.1:8081/search \
  -H 'Content-Type: application/json' \
  -d '{"query":"Who invented the World Wide Web?","top_k":5}'

curl -fsS http://127.0.0.1:8081/search_many \
  -H 'Content-Type: application/json' \
  -d '{"queries":["World Wide Web inventor","first web browser"],"top_k":5}'
```

Fetch a full document using a returned `docid`:

```bash
curl -fsS http://127.0.0.1:8081/get_document \
  -H 'Content-Type: application/json' \
  -d '{"docid":"RETURNED_DOCID"}'
```

The result mode is server-side: `full` returns the document head, `compact` removes flat YAML
frontmatter and returns a short body head, and `query_aware` selects the lexical window with
the best query coverage. Set `LOCAL_SEARCH_RESULT_MODE` before starting the service.

## 5. Connect OpenSAC

In a second terminal:

```bash
export OPENSAC_SEARCH_BACKEND=local
export OPENSAC_LOCAL_SEARCH_BASE_URL=http://127.0.0.1:8081
uv run opensac serve
```

OpenSAC sends search and document requests over HTTP. Keep the search service bound to
`127.0.0.1` unless remote access is intentional and protected by network controls; the local
search API itself has no authentication.

## Troubleshooting

- `file does not exist`: run the preparation script or set all three path variables.
- vector/id count mismatch: `index.faiss` and `index_ids.json` are from different revisions.
- missing indexed document: `corpus.jsonl` does not contain an id referenced by
  `index_ids.json`.
- corpus SHA-256 mismatch: the corpus and metadata are from different revisions.
- CUDA out of memory: reduce `LOCAL_SEARCH_BATCH_SIZE` and
  `LOCAL_SEARCH_BATCH_TOKEN_BUDGET`, or select another device.
