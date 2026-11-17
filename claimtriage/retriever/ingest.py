"""Idempotent ingestion pipeline (D2): read -> parse -> chunk -> embed -> index.

    python -m claimtriage.retriever.ingest              # incremental, no-op if nothing changed
    python -m claimtriage.retriever.ingest --doc prospectus   # re-ingest one document
    python -m claimtriage.retriever.ingest --force      # rebuild from scratch (embeddings still cached)

Idempotency: each PDF's sha256 and each chunk's content hash are compared with
the existing index. Unchanged chunks are not re-embedded; removed chunks are
deleted; changed chunks are replaced in place. Running twice is a no-op.

Freshness (index/meta.json, also exported as Prometheus metrics by the
retriever): build time, source document versions + sha256, chunk counts,
pipeline throughput (pages/s, chunks/s), embedding model.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sys
import time
from pathlib import Path

import yaml

from ..common.config import get_settings
from .chunker import chunk_document
from .index import PolicyIndex


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(1 << 16), b""):
            h.update(block)
    return h.hexdigest()


def run(corpus_dir: str, index_dir: str, model: str, force: bool = False, only_doc: str | None = None) -> dict:
    if not model:
        raise SystemExit("EMBEDDING_MODEL is not set (see .env.example)")
    corpus = Path(corpus_dir)
    manifest = yaml.safe_load((corpus / "manifest.yaml").read_text(encoding="utf-8"))
    idx_path = Path(index_dir)
    if force and idx_path.exists():
        for sub in ("faiss", "chunks.jsonl", "meta.json"):
            p = idx_path / sub
            shutil.rmtree(p) if p.is_dir() else p.unlink(missing_ok=True)
    index = PolicyIndex(index_dir, model)
    previous: dict = {}
    if index.exists():
        index.load()
        previous = index.meta

    t0 = time.perf_counter()
    docs_meta, all_chunks, pages = [], [], 0
    prev_docs = {d["id"]: d for d in previous.get("documents", [])}
    for doc in manifest["documents"]:
        path = corpus / doc["file"]
        digest = sha256(path)
        if doc.get("sha256") and doc["sha256"] != digest:
            print(f"WARNING: {doc['file']} sha256 differs from manifest (document updated upstream?)",
                  file=sys.stderr)
        unchanged = (prev_docs.get(doc["id"], {}).get("sha256") == digest
                     and previous.get("embedding_model") == model and not force
                     and (only_doc is None or only_doc != doc["id"]))
        if unchanged:
            kept = [c for c in index.chunks.values() if c["doc_id"] == doc["id"]]
            all_chunks.extend(kept)
            docs_meta.append(prev_docs[doc["id"]])
            continue
        chunks = [c.to_dict() for c in chunk_document(str(path), doc)]
        pages += max((c["page_end"] for c in chunks), default=0)
        all_chunks.extend(chunks)
        docs_meta.append({"id": doc["id"], "title": doc["title"], "file": doc["file"], "version": doc["version"],
                          "uin": doc.get("uin", ""), "sha256": digest, "chunks": len(chunks),
                          "source_url": doc.get("source_url", ""), "ingested_at": time.time()})

    wanted = {c["clause_id"] for c in all_chunks}
    removed = index.delete([cid for cid in list(index.chunks) if cid not in wanted]) if index.chunks else 0
    stats = index.upsert(all_chunks)
    elapsed = time.perf_counter() - t0
    if stats["embedded"] == 0 and removed == 0 and previous:
        print(json.dumps({"status": "up_to_date", "chunks": len(index.order),
                          "built_at": previous.get("built_at")}))
        return previous
    meta = {
        "built_at": time.time(),
        "embedding_model": model,
        "documents": docs_meta,
        "chunks": len(index.order),
        "chunks_embedded_this_run": stats["embedded"],
        "chunks_removed_this_run": removed,
        "duration_s": round(elapsed, 3),
        "throughput_chunks_per_s": round(stats["embedded"] / elapsed, 2) if elapsed else 0.0,
        "throughput_pages_per_s": round(pages / elapsed, 2) if elapsed else 0.0,
        "index_version": hashlib.sha256("".join(sorted(c["sha"] for c in all_chunks)).encode()).hexdigest()[:12],
    }
    index.save(meta)
    print(json.dumps({"status": "built", **{k: v for k, v in meta.items() if k != "documents"}}))
    return meta


def main() -> None:
    s = get_settings()
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--corpus", default=s.corpus_dir)
    ap.add_argument("--index", default=s.index_dir)
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--doc", default=None, help="re-ingest only this document id")
    ap.add_argument("--dump", action="store_true", help="print chunk ids/headers after building")
    a = ap.parse_args()
    run(a.corpus, a.index, s.embedding_model, force=a.force, only_doc=a.doc)
    if a.dump:
        idx = PolicyIndex(a.index, s.embedding_model)
        idx.load()
        for cid in idx.order:
            c = idx.chunks[cid]
            print(f"{cid:12s} p{c['page_start']:>2}-{c['page_end']:<2} {len(c['text'].split()):>4}w {c['header'][:110]}")


if __name__ == "__main__":
    main()
