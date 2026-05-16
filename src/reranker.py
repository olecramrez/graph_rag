import re
import os
import time
import json

import numpy as np
import requests
from dotenv import load_dotenv

from src.config import PROJECT_ENV, USER_ENV
from src.lia_client import get_embedding_batch


# =====================================================
# CACHE DE EMBEDDINGS
# =====================================================

embedding_cache = {}

DEFAULT_LIA_RERANK_URL = "https://lia-api.cgu.gov.br/api/tools/rerank?allow_only_entraid=false"
DEFAULT_LIA_RERANK_MODEL = "Cohere-rerank-v4.0-pro"


# =====================================================
# SIMILARIDADE COSENO
# =====================================================

def cosine_sim(a, b):
    a = np.array(a, dtype=np.float32)
    b = np.array(b, dtype=np.float32)

    norm_a = np.linalg.norm(a)
    norm_b = np.linalg.norm(b)

    if norm_a == 0 or norm_b == 0:
        return 0.0

    return float(np.dot(a, b) / (norm_a * norm_b))


# =====================================================
# TOKENIZAÇÃO
# =====================================================

def tokenize(text):
    return [w for w in re.findall(r"\w+", text.lower()) if len(w) > 3]


# =====================================================
# SCORE LEXICAL
# =====================================================

def lexical_score(query, text):

    q_tokens = set(tokenize(query))
    t_tokens = set(tokenize(text))

    if not q_tokens:
        return 0.0

    overlap = len(q_tokens.intersection(t_tokens))

    return overlap / len(q_tokens)


# =====================================================
# EMBEDDING COM CACHE
# =====================================================

def get_cached_embeddings(chunks):

    missing = []
    missing_ids = []

    for chunk in chunks:

        cid = chunk["chunk_id"]

        if cid not in embedding_cache:
            missing.append(chunk["text"])
            missing_ids.append(cid)

    if missing:

        new_embeddings = get_embedding_batch(missing)

        for cid, emb in zip(missing_ids, new_embeddings):
            embedding_cache[cid] = emb

    return [embedding_cache[c["chunk_id"]] for c in chunks]


# =====================================================
# RE-RANK
# =====================================================

def _load_rerank_config():
    if USER_ENV.exists():
        load_dotenv(USER_ENV, override=True)
    elif PROJECT_ENV.exists():
        load_dotenv(PROJECT_ENV, override=True)
    else:
        load_dotenv(override=True)

    provider = str(os.getenv("RAG_RERANK_PROVIDER", "local") or "local").strip().lower()
    api_key = str(os.getenv("LIA_RERANK_API_KEY") or os.getenv("LIA_API_KEY") or "").strip()

    return {
        "provider": provider,
        "api_key": api_key,
        "url": str(os.getenv("LIA_RERANK_URL", DEFAULT_LIA_RERANK_URL) or "").strip(),
        "model": str(os.getenv("LIA_RERANK_MODEL", DEFAULT_LIA_RERANK_MODEL) or "").strip(),
        "max_tokens_per_doc": int(os.getenv("LIA_RERANK_MAX_TOKENS_PER_DOC", "4096") or "4096"),
        "timeout": int(os.getenv("LIA_RERANK_TIMEOUT", "120") or "120"),
    }


def get_runtime_rerank_model():
    cfg = _load_rerank_config()
    provider = cfg["provider"]
    if provider in {"lia", "lia_cohere", "cohere", "cohere_lia"}:
        return cfg["model"] or DEFAULT_LIA_RERANK_MODEL
    return "local"


def _chunk_to_rerank_document(chunk):
    metadata_parts = []
    for key in (
        "doc",
        "doc_name",
        "page",
        "titulo",
        "titulo_norma",
        "tipo_norma",
        "numero_norma",
        "ano_norma",
        "status_normativo",
        "data_inicio_vigencia",
        "data_fim_vigencia",
        "revogado_por",
        "relacao_com_mineracao",
        "area_juridica_principal",
        "familia_normativa_mineraria",
        "usar_como_fundamento_principal",
    ):
        value = chunk.get(key)
        if value not in (None, "", []):
            metadata_parts.append(f"{key}: {value}")

    metadata = "\n".join(metadata_parts)
    text = str(chunk.get("text") or "")
    if metadata:
        return f"{metadata}\n\n{text}"
    return text


def _parse_json_text(value):
    if not isinstance(value, str):
        return None

    text = value.strip()
    if not text:
        return None

    fenced = re.search(r"```(?:json)?\s*(.*?)```", text, re.DOTALL | re.IGNORECASE)
    if fenced:
        text = fenced.group(1).strip()

    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return None


def _extract_rerank_candidate_lists(payload, depth=0):
    if depth > 6:
        return []

    parsed_text = _parse_json_text(payload)
    if parsed_text is not None:
        return _extract_rerank_candidate_lists(parsed_text, depth=depth + 1)

    if isinstance(payload, list):
        if any(isinstance(item, dict) for item in payload):
            return [payload]

        candidates = []
        for item in payload:
            candidates.extend(_extract_rerank_candidate_lists(item, depth=depth + 1))
        return candidates

    if not isinstance(payload, dict):
        return []

    candidates = []
    preferred_keys = (
        "results",
        "rerank_results",
        "rankings",
        "ranking",
        "data",
        "documents",
        "items",
    )

    for key in preferred_keys:
        value = payload.get(key)
        if isinstance(value, list):
            candidates.append(value)
        elif isinstance(value, (dict, str)):
            candidates.extend(_extract_rerank_candidate_lists(value, depth=depth + 1))

    for key, value in payload.items():
        if key in preferred_keys:
            continue
        if isinstance(value, (dict, list, str)):
            candidates.extend(_extract_rerank_candidate_lists(value, depth=depth + 1))

    return candidates


def _parse_lia_rerank_response(payload):
    candidate_lists = _extract_rerank_candidate_lists(payload)

    for candidates in candidate_lists:
        parsed = []

        for position, item in enumerate(candidates):
            if not isinstance(item, dict):
                continue

            index = item.get(
                "index",
                item.get(
                    "document_index",
                    item.get(
                        "documentIndex",
                        item.get("document_idx", item.get("ranked_index", position)),
                    ),
                ),
            )
            score = item.get(
                "relevance_score",
                item.get(
                    "relevanceScore",
                    item.get(
                        "score",
                        item.get("relevance", item.get("rank_score", item.get("rankScore"))),
                    ),
                ),
            )

            if score is None:
                continue

            try:
                parsed.append((int(index), float(score)))
            except (TypeError, ValueError):
                continue

        if parsed:
            return parsed

    return []


def _payload_shape(value, depth=0):
    if depth > 2:
        return type(value).__name__

    if isinstance(value, dict):
        shaped = {}
        for key, item in list(value.items())[:8]:
            shaped[str(key)] = _payload_shape(item, depth=depth + 1)
        return shaped

    if isinstance(value, list):
        if not value:
            return []
        return [f"{len(value)} items", _payload_shape(value[0], depth=depth + 1)]

    return type(value).__name__


def _rerank_lia_cohere(query, results, top_k=8, max_retries=3):
    cfg = _load_rerank_config()
    if not cfg["api_key"]:
        raise ValueError("LIA_API_KEY ou LIA_RERANK_API_KEY nao definida para rerank.")
    if not cfg["url"]:
        raise ValueError("LIA_RERANK_URL nao definida para rerank.")

    documents = [_chunk_to_rerank_document(chunk) for _, chunk in results]
    body = {
        "model_id": cfg["model"],
        "query": query,
        "documents": documents,
        "top_n": min(int(top_k), len(documents)),
        "max_tokens_per_doc": cfg["max_tokens_per_doc"],
    }
    headers = {
        "accept": "application/json",
        "Authorization": f"Bearer {cfg['api_key']}",
        "Content-Type": "application/json",
    }

    last_error = None
    for attempt in range(max_retries):
        try:
            response = requests.post(
                cfg["url"],
                headers=headers,
                json=body,
                timeout=cfg["timeout"],
            )
            if response.status_code == 200:
                payload = response.json()
                parsed = _parse_lia_rerank_response(payload)
                reranked = []
                seen = set()
                for index, relevance in parsed:
                    if index < 0 or index >= len(results) or index in seen:
                        continue
                    orig_score, chunk = results[index]
                    final_score = float(relevance) + 0.05 * float(orig_score)
                    reranked.append((final_score, chunk))
                    seen.add(index)

                if reranked:
                    return reranked[:top_k]

                raise ValueError(
                    "Resposta do rerank sem resultados reconheciveis. "
                    f"Formato recebido: {_payload_shape(payload)}"
                )

            if response.status_code >= 500:
                wait = 2 ** attempt
                time.sleep(wait)
                continue

            raise RuntimeError(f"Erro rerank LIA: {response.status_code} - {response.text}")
        except requests.exceptions.RequestException as exc:
            last_error = exc
            wait = 2 ** attempt
            time.sleep(wait)

    if last_error:
        raise RuntimeError(f"Falha de rede no rerank LIA: {last_error}") from last_error
    raise RuntimeError("Falha apos multiplas tentativas de rerank LIA.")


def _rerank_local(query, results, alpha=0.7, beta=0.3, gamma=0.1, top_k=8):
    """
    alpha = peso semântico
    beta  = peso lexical
    gamma = peso do score original do retrieval
    """

    if not results:
        return []

    chunks = [r[1] for r in results]

    # =====================================================
    # EMBEDDING QUERY
    # =====================================================

    q_emb = get_embedding_batch([query])[0]

    # =====================================================
    # EMBEDDINGS CHUNKS (COM CACHE)
    # =====================================================

    t_embs = get_cached_embeddings(chunks)

    reranked = []

    for (orig_score, chunk), emb in zip(results, t_embs):

        sem_score = cosine_sim(q_emb, emb)
        lex_score = lexical_score(query, chunk["text"])

        final_score = (
            alpha * sem_score +
            beta * lex_score +
            gamma * orig_score
        )

        reranked.append((final_score, chunk))

    reranked.sort(key=lambda x: x[0], reverse=True)

    return reranked[:top_k]


def rerank(query, results, alpha=0.7, beta=0.3, gamma=0.1, top_k=8):
    """
    Usa rerank local por padrao. Para Cohere via API institucional:

    RAG_RERANK_PROVIDER=lia_cohere
    LIA_RERANK_MODEL=Cohere-rerank-v4.0-pro
    LIA_RERANK_URL=https://lia-api.cgu.gov.br/api/tools/rerank?allow_only_entraid=false

    A chave usa LIA_RERANK_API_KEY, se existir; caso contrario, LIA_API_KEY.
    """

    if not results:
        return []

    cfg = _load_rerank_config()
    if cfg["provider"] in {"lia", "lia_cohere", "cohere", "cohere_lia"}:
        try:
            return _rerank_lia_cohere(query, results, top_k=top_k)
        except Exception as exc:
            print(f"[WARN] Rerank LIA/Cohere falhou; usando rerank local. Detalhe: {exc}")

    return _rerank_local(
        query,
        results,
        alpha=alpha,
        beta=beta,
        gamma=gamma,
        top_k=top_k,
    )
