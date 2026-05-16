import re
import json
import os

from dotenv import load_dotenv

from src.config import PROJECT_ENV, USER_ENV
from src.lia_client import chat_completion


STOPWORDS = {
    "o","a","os","as","de","do","da","dos","das",
    "no","na","nos","nas","para","por","com",
    "qual","quais","que","é","são","existe","existem"
}


def _load_query_generation_env():
    if USER_ENV.exists():
        load_dotenv(USER_ENV, override=True)
    elif PROJECT_ENV.exists():
        load_dotenv(PROJECT_ENV, override=True)
    else:
        load_dotenv(override=True)


def generate_query_variants(query):

    variants = {query}

    tokens = re.findall(r"\w+", query.lower())

    # remover stopwords
    filtered = [t for t in tokens if t not in STOPWORDS]

    # variante técnica curta
    if len(filtered) >= 2:
        variants.add(" ".join(filtered[:3]))

    # variante conceitual
    variants.add(query + " conceito")

    # variante técnica
    variants.add(query + " requisitos")

    variants.update(_generate_llm_query_variants(query))

    return list(variants)


def _query_generation_enabled():
    _load_query_generation_env()
    provider = str(os.getenv("RAG_QUERY_GENERATION_PROVIDER", "local") or "local")
    return provider.strip().lower() in {"lia", "lia_gpt", "lia_gpt53", "gpt53", "llm"}


def get_runtime_query_generation_model():
    if not _query_generation_enabled():
        return "local"
    model = str(os.getenv("LIA_QUERY_GENERATION_MODEL", "gpt-5.3-chat") or "").strip()
    return model or "gpt-5.3-chat"


def _extract_json_array(text):
    raw = str(text or "").strip()
    if not raw:
        return []
    if raw.startswith("```"):
        raw = re.sub(r"^```(?:json)?\s*", "", raw)
        raw = re.sub(r"\s*```$", "", raw)

    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        start = raw.find("[")
        end = raw.rfind("]")
        if start == -1 or end <= start:
            return []
        try:
            payload = json.loads(raw[start:end + 1])
        except json.JSONDecodeError:
            return []

    if isinstance(payload, dict):
        payload = payload.get("queries") or payload.get("consultas") or []

    if not isinstance(payload, list):
        return []

    values = []
    seen = set()
    for item in payload:
        value = re.sub(r"\s+", " ", str(item or "")).strip()
        key = value.lower()
        if not value or key in seen:
            continue
        seen.add(key)
        values.append(value)
        if len(values) >= 6:
            break
    return values


def _generate_llm_query_variants(query):
    if not _query_generation_enabled():
        return []

    model = str(os.getenv("LIA_QUERY_GENERATION_MODEL", "gpt-5.3-chat") or "").strip()
    api_version = str(os.getenv("LIA_QUERY_GENERATION_API_VERSION", "2025-04-01-preview") or "").strip()
    allow_only_entraid_raw = str(os.getenv("LIA_QUERY_GENERATION_ALLOW_ONLY_ENTRAID", "false") or "false").strip().lower()
    allow_only_entraid = allow_only_entraid_raw in {"1", "true", "sim", "yes"}

    prompt = (
        "Gere ate 5 consultas alternativas curtas para busca RAG juridica em portugues. "
        "Preserve numeros de normas, datas, artigos e termos tecnicos. "
        "Inclua sinonimos uteis, mas nao invente fatos. "
        "Responda somente com JSON array de strings.\n\n"
        f"Pergunta: {query}"
    )

    try:
        response = chat_completion(
            [
                {
                    "role": "system",
                    "content": (
                        "Voce gera variantes de consulta para recuperacao de documentos juridicos. "
                        "Responda apenas JSON valido."
                    ),
                },
                {"role": "user", "content": prompt},
            ],
            temperature=0.0,
            llm_model=model,
            api_version=api_version,
            allow_only_entraid=allow_only_entraid,
        )
    except Exception as exc:
        print(f"[WARN] Query generation LIA falhou; usando variantes locais. Detalhe: {exc}")
        return []

    return _extract_json_array(response)
