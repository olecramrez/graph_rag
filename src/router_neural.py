import json
import numpy as np
from pathlib import Path
from src.lia_client import get_embedding_batch

PROJECT_NAME = Path(__file__).resolve().parents[1].name
USER_ROOT = Path.home() / PROJECT_NAME
DATA_DIR = USER_ROOT / "data"
DOC_EMB_PATH = DATA_DIR / "doc_embeddings.json"


def cosine_sim(a, b):
    a = np.array(a)
    b = np.array(b)
    return float(np.dot(a, b) / (np.linalg.norm(a) * np.linalg.norm(b)))


def load_doc_embeddings():
    if not DOC_EMB_PATH.exists():
        return {}
    return json.loads(DOC_EMB_PATH.read_text(encoding="utf-8"))


def route_query(query):

    doc_embeddings = load_doc_embeddings()

    if not doc_embeddings:
        return {"strategy": "global", "filter_doc": None}

    q_emb = get_embedding_batch([query])[0]

    best_doc = None
    best_score = 0

    print("\n[INFO] Similaridades por documento:\n")

    for doc, emb in doc_embeddings.items():
        sim = cosine_sim(q_emb, emb)
        print(doc, "->", round(sim, 3))

        if sim > best_score:
            best_score = sim
            best_doc = doc

    print("\n[INFO] Melhor documento:", best_doc)
    print("[INFO] Melhor score:", round(best_score, 3))

    # 🔥 LIMIAR (vamos calibrar depois)
    if best_score > 0.82:
        return {
            "strategy": "document_filter",
            "filter_doc": best_doc
        }

    return {"strategy": "global", "filter_doc": None}
