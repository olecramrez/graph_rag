import json
import re

from src.config import get_chunks_path, get_default_base


EXPLICIT_NORM_REFERENCE_RE = re.compile(
    r"\b(?P<tipo>"
    r"lei|decreto|portaria|resolucao|instrucao normativa|instru[cç][aã]o normativa|ato"
    r")\s*"
    r"(?:n(?:o|u?m|\.|º|°)?\s*)?"
    r"(?P<numero>[\d.]{1,12})"
    r"(?:\s*/\s*(?P<ano>\d{2,4}))?\b",
    re.IGNORECASE,
)


# =====================================================
# CARREGA NOMES DOS DOCUMENTOS
# =====================================================

def load_document_names(base_name=None, allowed_docs=None):
    docs = set()
    chunks_path = get_chunks_path(base_name or get_default_base())

    if not chunks_path.exists():
        return docs

    with open(chunks_path, "r", encoding="utf-8") as f:
        for line in f:
            try:
                chunk = json.loads(line)
            except json.JSONDecodeError:
                continue
            doc = chunk["doc"]

            if allowed_docs is not None and doc not in allowed_docs:
                continue

            docs.add(doc)

    return list(docs)


# =====================================================
# NORMALIZACAO
# =====================================================

def normalize(text):
    return re.sub(r"[^a-z0-9]", "", str(text or "").lower())


# =====================================================
# EXTRAI TIPO, NUMERO E ANO DA PERGUNTA
# =====================================================

def extract_metadata(query, temporal_intent=None):
    query = str(query or "").strip()
    if not query:
        return None, None, None

    match = EXPLICIT_NORM_REFERENCE_RE.search(query.lower())
    if not match:
        return None, None, None

    tipo = (match.group("tipo") or "").strip() or None
    numero = (match.group("numero") or "").strip() or None
    ano = (match.group("ano") or "").strip() or None

    if ano and len(ano) == 2:
        ano = "20" + ano

    if temporal_intent and temporal_intent.get("enabled") and not tipo:
        return None, None, None

    return tipo, numero, ano


# =====================================================
# ROUTER PRINCIPAL
# =====================================================

def route_query(query, base_name=None, allowed_docs=None, temporal_intent=None):
    docs = load_document_names(base_name=base_name, allowed_docs=allowed_docs)
    tipo, numero, ano = extract_metadata(query, temporal_intent=temporal_intent)
    candidatos = []

    if numero:
        numero_norm = normalize(numero)
        ano_norm = normalize(ano) if ano else None

        for doc in docs:
            doc_norm = normalize(doc)

            if numero_norm not in doc_norm:
                continue

            if ano_norm and ano_norm not in doc_norm:
                continue

            candidatos.append(doc)

    if tipo and candidatos:
        tipo_norm = normalize(tipo)
        filtrados = []

        for doc in candidatos:
            doc_norm = normalize(doc)
            if tipo_norm in doc_norm:
                filtrados.append(doc)

        if filtrados:
            candidatos = filtrados

    if len(candidatos) == 1:
        return {
            "strategy": "document_filter",
            "filter_doc": candidatos[0],
        }

    return {
        "strategy": "global",
        "filter_doc": None,
    }
