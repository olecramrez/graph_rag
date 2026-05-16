from src.vector_store import load_faiss, load_idmap
from src.bm25_store import search_bm25
from src.lia_client import get_embedding_batch
from src.safe_jsonl import load_valid_jsonl
from src.config import get_chunks_path

import numpy as np
import re

# =====================================================
# CACHE GLOBAL DE CHUNKS
# =====================================================

chunk_cache = {}


# =====================================================
# LOAD CHUNKS COM CACHE
# =====================================================

def get_chunk_lookup(base_name):

    if base_name in chunk_cache:
        return chunk_cache[base_name]

    chunks_path = get_chunks_path(base_name)
    all_chunks = load_valid_jsonl(chunks_path)

    lookup = {c["chunk_id"]: c for c in all_chunks}

    chunk_cache[base_name] = lookup

    return lookup


def normalize_scores(values):

    if not values:
        return {}

    v = np.array(values, dtype=float)

    min_v = np.min(v)
    max_v = np.max(v)

    if max_v - min_v == 0:
        return {i: 1.0 for i in range(len(values))}

    norm = (v - min_v) / (max_v - min_v)

    return {i: float(norm[i]) for i in range(len(values))}


def _tokenize_query(text):
    return set(re.findall(r"\w+", str(text or "").lower()))


def _constraint_docs(filter_doc=None, allowed_docs=None):
    docs = set()

    if filter_doc:
        docs.add(filter_doc)

    if allowed_docs is not None:
        docs.update(str(doc) for doc in allowed_docs)

    if filter_doc or allowed_docs is not None:
        return docs

    return None


def _chunk_matches_constraints(chunk, constrained_docs):
    if constrained_docs is None:
        return True

    return chunk.get("doc") in constrained_docs


def _fallback_constrained_chunks(query, chunk_lookup, constrained_docs, top_k):
    if not constrained_docs:
        return []

    query_tokens = _tokenize_query(query)
    candidates = []

    for order, chunk in enumerate(chunk_lookup.values()):
        if not _chunk_matches_constraints(chunk, constrained_docs):
            continue

        chunk_tokens = _tokenize_query(chunk.get("text", ""))
        overlap = len(query_tokens & chunk_tokens) if query_tokens else 0
        score = float(overlap)
        candidates.append((score, order, chunk))

    candidates.sort(key=lambda item: (-item[0], item[1]))
    return [(score, chunk) for score, _, chunk in candidates[:top_k]]


def hybrid_search(
    query,
    base_name,
    top_k=20,
    filter_doc=None,
    allowed_docs=None,
    alpha=0.7,
    beta=0.3,
):

    index = load_faiss(base_name)
    idmap = load_idmap(base_name)
    constrained_docs = _constraint_docs(filter_doc=filter_doc, allowed_docs=allowed_docs)
    candidate_top_k = top_k
    if constrained_docs is not None:
        candidate_top_k = min(
            max(top_k * 50, 1000),
            max(getattr(index, "ntotal", top_k), top_k),
        )

    # =====================================================
    # EMBEDDING DA QUERY
    # =====================================================

    query_embedding = get_embedding_batch([query])[0]
    query_vector = np.array([query_embedding], dtype="float32")

    D, I = index.search(query_vector, candidate_top_k)

    reverse_idmap = {v: k for k, v in idmap.items()}

    semantic_scores = []
    semantic_ids = []

    for score, idx in zip(D[0], I[0]):

        if idx == -1:
            continue

        chunk_id = reverse_idmap.get(idx)

        if not chunk_id:
            continue

        semantic_scores.append(score)
        semantic_ids.append(chunk_id)

    # =====================================================
    # BUSCA BM25
    # =====================================================

    bm25_results = search_bm25(query, base_name, top_k=candidate_top_k)

    lexical_scores = []
    lexical_ids = []

    for score, chunk in bm25_results:

        lexical_scores.append(score)
        lexical_ids.append(chunk["chunk_id"])

    # =====================================================
    # NORMALIZACAO
    # =====================================================

    semantic_norm = normalize_scores(semantic_scores)
    lexical_norm = normalize_scores(lexical_scores)

    # =====================================================
    # COMBINAR SCORES
    # =====================================================

    combined_scores = {}

    alpha = float(alpha)
    beta = float(beta)
    if alpha < 0:
        alpha = 0.0
    if beta < 0:
        beta = 0.0
    if alpha == 0 and beta == 0:
        alpha = 0.7
        beta = 0.3

    for i, chunk_id in enumerate(semantic_ids):

        combined_scores[chunk_id] = {
            "semantic": semantic_norm.get(i, 0),
            "lexical": 0,
        }

    for i, chunk_id in enumerate(lexical_ids):

        if chunk_id not in combined_scores:

            combined_scores[chunk_id] = {
                "semantic": 0,
                "lexical": lexical_norm.get(i, 0),
            }

        else:

            combined_scores[chunk_id]["lexical"] = lexical_norm.get(i, 0)

    # =====================================================
    # CARREGAR CHUNKS
    # =====================================================

    chunk_lookup = get_chunk_lookup(base_name)
    results = []

    for chunk_id, scores in combined_scores.items():

        chunk = chunk_lookup.get(chunk_id)

        if not chunk:
            continue

        if not _chunk_matches_constraints(chunk, constrained_docs):
            continue

        final_score = (
            alpha * scores["semantic"] +
            beta * scores["lexical"]
        )

        results.append((final_score, chunk))

    results.sort(key=lambda x: x[0], reverse=True)

    if not results and constrained_docs:
        return _fallback_constrained_chunks(
            query,
            chunk_lookup,
            constrained_docs,
            top_k,
        )

    return results[:top_k]
