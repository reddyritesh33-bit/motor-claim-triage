"""Policy index: LangChain FAISS vector store (dense, all-MiniLM-L6-v2 via
sentence-transformers, run locally) + BM25 (lexical), fused with reciprocal rank
fusion. Document embeddings are cached on disk by content hash
(CacheBackedEmbeddings), so re-embedding a changed clause never recomputes the
others, and the FAISS store supports delete+add by clause id, so a re-embed
never rebuilds the whole index (L1).
"""
from __future__ import annotations

import json
import os
import re
import threading
import time
from functools import lru_cache
from pathlib import Path

from rank_bm25 import BM25Okapi

STOP = set("the a an of to in and or for on by with is be as at any such this that from which shall will under "
           "policy company insured vehicle".split())
TOKEN_RE = re.compile(r"[a-z0-9]+")
MODIFIER_SECTIONS = ("Section V:", "Section VI:", "Section VII:")  # exclusions, deductible, conditions
MODIFIER_HINT = re.compile(r"shall not be liable|exclusion|provided always|subject to|not be payable|conditions?:", re.I)


def tokenize(text: str) -> list[str]:
    return [t for t in TOKEN_RE.findall(text.lower()) if t not in STOP and len(t) > 1]


class PolicyIndex:
    def __init__(self, index_dir: str, model_name: str):
        self.dir = Path(index_dir)
        self.model_name = model_name
        self.lock = threading.RLock()
        self.chunks: dict[str, dict] = {}
        self.order: list[str] = []
        self.meta: dict = {}
        self.vs = None
        self.bm25 = None
        self._emb = None
        self._cached_emb = None

    # ------------------------------------------------------------ embeddings
    def embeddings(self):
        if self._emb is None:
            from langchain_huggingface import HuggingFaceEmbeddings

            self._emb = HuggingFaceEmbeddings(model_name=self.model_name,
                                              encode_kwargs={"normalize_embeddings": True})
        return self._emb

    def cached_embeddings(self):
        """Document embeddings cached on disk keyed by text hash (namespaced by model)."""
        if self._cached_emb is None:
            from langchain.embeddings import CacheBackedEmbeddings
            from langchain.storage import LocalFileStore

            store = LocalFileStore(str(self.dir / "embedding_cache"))
            self._cached_emb = CacheBackedEmbeddings.from_bytes_store(
                self.embeddings(), store, namespace=re.sub(r"[^A-Za-z0-9]", "_", self.model_name))
        return self._cached_emb

    @lru_cache(maxsize=4096)
    def embed_query(self, query: str) -> tuple[float, ...]:
        return tuple(self.embeddings().embed_query(query))

    # ------------------------------------------------------------ persistence
    def exists(self) -> bool:
        return (self.dir / "faiss" / "index.faiss").exists() and (self.dir / "chunks.jsonl").exists()

    def load(self) -> None:
        from langchain_community.vectorstores import FAISS

        with self.lock:
            self.vs = FAISS.load_local(str(self.dir / "faiss"), self.embeddings(),
                                       allow_dangerous_deserialization=True)
            self.chunks, self.order = {}, []
            with open(self.dir / "chunks.jsonl", encoding="utf-8") as fh:
                for line in fh:
                    c = json.loads(line)
                    self.chunks[c["clause_id"]] = c
                    self.order.append(c["clause_id"])
            self.meta = json.loads((self.dir / "meta.json").read_text(encoding="utf-8"))
            self._build_bm25()

    def _build_bm25(self) -> None:
        self.bm25 = BM25Okapi([tokenize(self.chunks[cid]["embed_text"]) for cid in self.order])

    def save(self, meta: dict) -> None:
        with self.lock:
            self.dir.mkdir(parents=True, exist_ok=True)
            self.vs.save_local(str(self.dir / "faiss"))
            tmp = self.dir / "chunks.jsonl.tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                for cid in self.order:
                    fh.write(json.dumps(self.chunks[cid], ensure_ascii=False) + "\n")
            os.replace(tmp, self.dir / "chunks.jsonl")
            self.meta = meta
            (self.dir / "meta.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")

    # ------------------------------------------------------------ upsert / delete
    def upsert(self, new_chunks: list[dict]) -> dict:
        """Insert or replace chunks by clause id; embeds only chunks whose content
        hash changed (the cache makes unchanged texts free even if re-sent)."""
        from langchain_community.vectorstores import FAISS
        from langchain_community.vectorstores.utils import DistanceStrategy

        with self.lock:
            changed = [c for c in new_chunks
                       if c["clause_id"] not in self.chunks or self.chunks[c["clause_id"]]["sha"] != c["sha"]]
            if not changed:
                return {"embedded": 0, "replaced": 0}
            t0 = time.perf_counter()
            vecs = self.cached_embeddings().embed_documents([c["embed_text"] for c in changed])
            embed_s = time.perf_counter() - t0
            pairs = list(zip([c["embed_text"] for c in changed], vecs))
            metas = [{"clause_id": c["clause_id"]} for c in changed]
            ids = [c["clause_id"] for c in changed]
            replaced = [cid for cid in ids if cid in self.chunks]
            if self.vs is None:
                self.vs = FAISS.from_embeddings(pairs, self.embeddings(), metadatas=metas, ids=ids,
                                                distance_strategy=DistanceStrategy.MAX_INNER_PRODUCT)
            else:
                if replaced:
                    self.vs.delete(ids=replaced)
                self.vs.add_embeddings(pairs, metadatas=metas, ids=ids)
            for c in changed:
                if c["clause_id"] not in self.chunks:
                    self.order.append(c["clause_id"])
                self.chunks[c["clause_id"]] = c
            self._build_bm25()
            return {"embedded": len(changed), "replaced": len(replaced), "embed_seconds": round(embed_s, 3)}

    def delete(self, clause_ids: list[str]) -> int:
        with self.lock:
            present = [c for c in clause_ids if c in self.chunks]
            if present:
                self.vs.delete(ids=present)
                for cid in present:
                    self.chunks.pop(cid)
                self.order = [c for c in self.order if c in self.chunks]
                self._build_bm25()
            return len(present)

    # ------------------------------------------------------------ search
    def _passes(self, c: dict, filters: dict | None) -> bool:
        if not filters:
            return True
        if filters.get("doc_id") and c["doc_id"] != filters["doc_id"]:
            return False
        if filters.get("section") and filters["section"].lower() not in c["section"].lower():
            return False
        return True

    def dense(self, query: str, n: int) -> list[tuple[str, float]]:
        vec = list(self.embed_query(query))
        docs = self.vs.similarity_search_with_score_by_vector(vec, k=min(n, len(self.order)))
        return [(d.metadata["clause_id"], float(s)) for d, s in docs]

    def lexical(self, query: str, n: int) -> list[tuple[str, float]]:
        scores = self.bm25.get_scores(tokenize(query))
        ranked = sorted(zip(self.order, scores), key=lambda x: x[1], reverse=True)[:n]
        return [(cid, float(s)) for cid, s in ranked if s > 0]

    def search(self, query: str, k: int = 5, mode: str = "hybrid", filters: dict | None = None) -> list[dict]:
        with self.lock:
            pool = max(20, k * 4)
            dense = self.dense(query, pool)
            dense_score = dict(dense)
            if mode == "dense":
                ranked = [(cid, s) for cid, s in dense]
            else:
                lex = self.lexical(query, pool) if mode in ("hybrid", "bm25") else []
                if mode == "bm25":
                    ranked = lex
                else:
                    fused: dict[str, float] = {}
                    for lst in (dense, lex):
                        for rank, (cid, _) in enumerate(lst):
                            fused[cid] = fused.get(cid, 0.0) + 1.0 / (60 + rank + 1)
                    ranked = sorted(fused.items(), key=lambda x: x[1], reverse=True)
            out = []
            for cid, score in ranked:
                c = self.chunks.get(cid)
                if not c or not self._passes(c, filters):
                    continue
                if cid not in dense_score:  # cosine for weak-retrieval detection (F7)
                    dense_score[cid] = self.cosine(query, c["embed_text"])
                out.append(self.public(c) | {"score": round(score, 5), "dense_score": round(dense_score[cid], 4)})
                if len(out) >= k:
                    break
            return out

    def cosine(self, a: str, b: str) -> float:
        va, vb = self.embed_query(a), self.embed_query(b[:2000])
        return float(sum(x * y for x, y in zip(va, vb)))

    def public(self, c: dict) -> dict:
        return {k: c[k] for k in ("clause_id", "doc_id", "doc_title", "doc_version", "section", "subsection",
                                  "page_start", "page_end", "text", "refs")}

    def get(self, clause_id: str) -> dict | None:
        c = self.chunks.get(clause_id)
        return self.public(c) if c else None

    def neighbours(self, clause_id: str) -> list[str]:
        i = self.order.index(clause_id)
        return [cid for cid in self.order[max(0, i - 1): i + 2] if cid != clause_id]

    def modifiers(self, clause_id: str) -> dict:
        """Provisions that may modify the given clause: the general exclusions,
        deductible and conditions of the policy wording, plus exclusion/proviso
        chunks inside the clause's own section. Also reports defined terms the
        clause and its modifiers reference, so the agent can go and find them."""
        c = self.chunks.get(clause_id)
        if not c:
            return {"error": f"unknown clause {clause_id}"}
        mods: list[dict] = []
        for cid in self.order:
            o = self.chunks[cid]
            if cid == clause_id:
                continue
            general = o["doc_id"] == "policy_wording" and o["section"].startswith(MODIFIER_SECTIONS)
            local = o["doc_id"] == c["doc_id"] and o["section"] == c["section"] and \
                (not c["subsection"] or o["subsection"] == c["subsection"]) and MODIFIER_HINT.search(o["text"])
            if general or local:
                mods.append({"clause_id": cid, "section": o["section"], "subsection": o["subsection"],
                             "kind": "general" if general else "same_section",
                             "doc_title": o["doc_title"], "page_start": o["page_start"], "refs": o["refs"],
                             "text": o["text"]})
        refs = sorted(set(c["refs"]).union(*[set(self.chunks[m["clause_id"]]["refs"]) for m in mods]))
        return {"clause_id": clause_id, "modifiers": mods[:14], "referenced_terms": refs}
