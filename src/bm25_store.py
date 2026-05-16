import json
import pickle
import re
from rank_bm25 import BM25Okapi

from .config import get_chunks_path, get_bm25_path


# =====================================================
# TOKENIZAÇÃO MELHORADA
# =====================================================

def tokenize(text):

    if not text:
        return []

    text = text.lower()

    tokens = re.findall(r"\w+", text)

    return tokens


# =====================================================
# BUILD BM25
# =====================================================

def build_bm25(base_name):

    print("Construindo indice BM25...")

    CHUNKS_PATH = get_chunks_path(base_name)
    BM25_PATH = get_bm25_path(base_name)

    if not CHUNKS_PATH.exists():
        print("[WARN] Nenhum chunks.jsonl encontrado.")
        return

    corpus = []
    metadata = []

    with open(CHUNKS_PATH, "r", encoding="utf-8") as f:

        for line in f:

            chunk = json.loads(line)

            tokens = tokenize(chunk["text"])

            corpus.append(tokens)
            metadata.append(chunk)

    if not corpus:
        print("[WARN] Nenhum chunk para indexar.")
        return

    bm25 = BM25Okapi(corpus)

    with open(BM25_PATH, "wb") as f:
        pickle.dump((bm25, metadata), f)

    print(f"BM25 criado com {len(corpus)} chunks")


# =====================================================
# LOAD BM25
# =====================================================

def load_bm25(base_name):

    BM25_PATH = get_bm25_path(base_name)

    if not BM25_PATH.exists():
        raise FileNotFoundError("BM25 ainda não criado.")

    with open(BM25_PATH, "rb") as f:
        return pickle.load(f)


# =====================================================
# QUERY PARSER
# =====================================================

def parse_query_terms(query):
    """
    Permite consultas com:
    - palavras simples
    - frases entre aspas
    """

    query = query.lower()

    # frases entre aspas
    phrases = re.findall(r'"([^"]+)"', query)

    # remove frases da query
    query_clean = re.sub(r'"[^"]+"', "", query)

    tokens = tokenize(query_clean)

    for phrase in phrases:
        tokens.extend(tokenize(phrase))

    return tokens


# =====================================================
# SEARCH BM25
# =====================================================

def search_bm25(query, base_name, top_k=10):

    try:

        bm25, metadata = load_bm25(base_name)

    except FileNotFoundError:

        print("[WARN] BM25 nao encontrado para esta base.")
        return []

    tokens = parse_query_terms(query)

    if not tokens:
        return []

    scores = bm25.get_scores(tokens)

    ranked = sorted(
        zip(scores, metadata),
        key=lambda x: x[0],
        reverse=True
    )

    return ranked[:top_k]


# =====================================================
# SEARCH DOCUMENTS (PARA FILTRO LEXICAL)
# =====================================================

def search_documents(query, base_name, top_k=200):
    """
    Retorna apenas documentos encontrados no BM25
    Usado para delimitar base lexical.
    """

    results = search_bm25(query, base_name, top_k)

    docs = set()

    for score, chunk in results:

        if score <= 0:
            continue

        docs.add(chunk["doc"])

    return list(docs)
