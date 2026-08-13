from __future__ import annotations

import hashlib
import json
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

DENSE_INDEX_POOLING = "last_token_left_padding_v1"


@dataclass(frozen=True)
class LocalSearchHit:
    docid: str
    score: float
    snippet: str


class LocalSearcher(Protocol):
    backend_name: str

    def search(self, query: str, k: int) -> list[LocalSearchHit]: ...

    def search_many(self, queries: list[str], k: int) -> list[list[LocalSearchHit]]: ...

    def get_document(self, docid: str) -> dict[str, Any] | None: ...


def _last_token_pool(hidden_state, attention_mask):
    del attention_mask
    return hidden_state[:, -1]


def _load_index_ids(path: Path) -> list[str]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"Failed to read index ids from '{path}'.") from exc
    if isinstance(payload, dict):
        for key in ("ids", "docids", "index_ids"):
            if isinstance(payload.get(key), list):
                payload = payload[key]
                break
    if not isinstance(payload, list) or not all(
        isinstance(item, (str, int)) for item in payload
    ):
        raise ValueError(
            f"'{path}' must contain a JSON list of document ids, optionally under "
            "an 'ids', 'docids', or 'index_ids' key."
        )
    docids = [str(item).strip() for item in payload]
    if not docids or any(not docid for docid in docids):
        raise ValueError(f"'{path}' contains no usable document ids.")
    if len(set(docids)) != len(docids):
        raise ValueError(f"'{path}' contains duplicate document ids.")
    return docids


def _load_corpus(path: Path) -> tuple[dict[str, str], str]:
    documents: dict[str, str] = {}
    digest = hashlib.sha256()
    line_number = 0
    try:
        with path.open("rb") as handle:
            for _line_number, raw_line in enumerate(handle, start=1):
                line_number = _line_number
                digest.update(raw_line)
                if not raw_line.strip():
                    continue
                record = json.loads(raw_line)
                docid = str(record.get("id", record.get("docid", ""))).strip()
                text = str(record.get("contents", record.get("text", ""))).strip()
                if not docid or not text:
                    continue
                documents[docid] = text
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(
            f"Failed to read corpus JSONL '{path}' near line {line_number}."
        ) from exc
    if not documents:
        raise ValueError(f"No documents loaded from corpus '{path}'.")
    return documents, digest.hexdigest()


def _load_metadata(index_path: Path) -> dict[str, Any]:
    path = Path(f"{index_path}.metadata.json")
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"Failed to read index metadata from '{path}'.") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"Index metadata '{path}' must be a JSON object.")
    return payload


def _validate_metadata(
    metadata: dict[str, Any],
    *,
    model_name: str,
    max_length: int,
    vector_count: int,
    corpus_sha256: str,
) -> None:
    expected = {
        "model_name": model_name,
        "max_length": max_length,
        "pooling": DENSE_INDEX_POOLING,
        "vector_count": vector_count,
        "ordered_corpus_sha256": corpus_sha256,
    }
    for field, expected_value in expected.items():
        if field in metadata and metadata[field] != expected_value:
            raise ValueError(
                f"Index metadata field {field!r} is {metadata[field]!r}, "
                f"expected {expected_value!r}."
            )


class DenseLocalSearcher:
    backend_name = "dense"

    def __init__(
        self,
        *,
        model_name: str,
        index_path: str,
        corpus_path: str,
        index_ids_path: str,
        query_prefix: str,
        max_length: int,
        device: str = "auto",
    ) -> None:
        index_file = Path(index_path)
        corpus_file = Path(corpus_path)
        ids_file = Path(index_ids_path)
        for label, path in (
            ("FAISS index", index_file),
            ("corpus", corpus_file),
            ("index ids", ids_file),
        ):
            if not path.is_file():
                raise ValueError(f"{label} file does not exist: '{path}'.")

        try:
            import faiss
        except ModuleNotFoundError as exc:
            raise RuntimeError(
                "faiss-cpu is required; install OpenSAC with the local-search extra."
            ) from exc
        try:
            import torch
            from transformers import AutoModel, AutoTokenizer
        except ModuleNotFoundError as exc:
            raise RuntimeError(
                "torch and transformers are required; install OpenSAC with the "
                "local-search extra."
            ) from exc

        self.model_name = model_name
        self.query_prefix = query_prefix
        self.max_length = max_length
        self._index = faiss.read_index(str(index_file))
        self._docids = _load_index_ids(ids_file)
        self._documents, corpus_sha256 = _load_corpus(corpus_file)
        self._index_metadata = _load_metadata(index_file)

        if self._index.ntotal != len(self._docids):
            raise ValueError(
                f"FAISS index has {self._index.ntotal} vectors but index_ids.json "
                f"contains {len(self._docids)} ids."
            )
        missing = [docid for docid in self._docids if docid not in self._documents]
        if missing:
            raise ValueError(
                f"corpus.jsonl is missing {len(missing)} indexed documents; first "
                f"missing id: {missing[0]!r}."
            )
        _validate_metadata(
            self._index_metadata,
            model_name=model_name,
            max_length=max_length,
            vector_count=self._index.ntotal,
            corpus_sha256=corpus_sha256,
        )

        self._torch = torch
        self.device = (
            "cuda" if device == "auto" and torch.cuda.is_available() else device
        )
        if self.device == "auto":
            self.device = "cpu"
        self._tokenizer = AutoTokenizer.from_pretrained(model_name, padding_side="left")
        self._model = AutoModel.from_pretrained(
            model_name,
            trust_remote_code=True,
            dtype=torch.bfloat16 if self.device.startswith("cuda") else torch.float32,
        ).to(self.device)
        self._model.eval()
        self._infer_lock = threading.Lock()
        self._tokenizer_lock = threading.Lock()

    @property
    def index_metadata(self) -> dict[str, Any]:
        return dict(self._index_metadata)

    def _encode_queries(self, queries: list[str]):
        with self._tokenizer_lock:
            inputs = self._tokenizer(
                [self.query_prefix + query for query in queries],
                padding=True,
                truncation=True,
                max_length=self.max_length,
                return_tensors="pt",
            )
        inputs = {key: value.to(self.device) for key, value in inputs.items()}
        with self._torch.no_grad():
            outputs = self._model(**inputs)
            embeddings = _last_token_pool(
                outputs.last_hidden_state, inputs["attention_mask"]
            )
            embeddings = self._torch.nn.functional.normalize(
                embeddings.detach().float(), p=2, dim=1
            )
        return embeddings.cpu().numpy().astype("float32")

    def query_token_lengths(self, queries: list[str]) -> list[int]:
        if not queries:
            return []
        with self._tokenizer_lock:
            inputs = self._tokenizer(
                [self.query_prefix + query for query in queries],
                padding=False,
                truncation=True,
                max_length=self.max_length,
            )
        return [len(token_ids) for token_ids in inputs["input_ids"]]

    def search(self, query: str, k: int) -> list[LocalSearchHit]:
        return self.search_many([query], k)[0]

    def search_many(self, queries: list[str], k: int) -> list[list[LocalSearchHit]]:
        if not queries:
            return []
        with self._infer_lock:
            scores, indices = self._index.search(self._encode_queries(queries), k)
        batches: list[list[LocalSearchHit]] = []
        for row_scores, row_indices in zip(scores, indices, strict=True):
            hits = []
            for score, index in zip(row_scores, row_indices, strict=True):
                if index < 0 or index >= len(self._docids):
                    continue
                docid = self._docids[index]
                hits.append(LocalSearchHit(docid, float(score), self._documents[docid]))
            batches.append(hits)
        return batches

    def get_document(self, docid: str) -> dict[str, Any] | None:
        text = self._documents.get(docid)
        return None if text is None else {"docid": docid, "text": text}
