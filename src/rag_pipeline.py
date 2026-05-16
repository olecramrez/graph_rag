import re
import time
import unicodedata
from datetime import date
from collections import defaultdict

from src.hybrid_retriever import hybrid_search, get_chunk_lookup
from src.reranker import rerank, get_runtime_rerank_model
from src.router import route_query
from src.lia_client import (
    DEFAULT_LLM_MODEL,
    LIAClientError,
    chat_completion,
    get_runtime_llm_model,
)

from src.query_intent import detect_query_intent  # compatibilidade
from src.lexical_stats import phrase_frequency  # compatibilidade

from src.query_decomposition import decompose_query
from src.query_variants import generate_query_variants, get_runtime_query_generation_model
from src.normative_temporal import (
    detect_temporal_normative_intent,
    format_reference_period,
    has_normative_metadata,
    is_active_in_reference_period,
    is_revoked_related,
    chunk_normative_title,
    effective_period,
    status_in_reference_period,
    is_effectively_normative,
)


RAG_CONFIG = {
    "top_k_retrieval": 60,
    "top_k_final": 20,
    "alpha_semantic": 0.7,
    "beta_lexical": 0.3,
    "max_context_chars": 20000,
    "adaptive_tuning_enabled": True,
    "max_query_variants": 4,
    "hybrid_profiles_per_query": 1,
    "max_hybrid_searches": 8,
    "hybrid_time_budget_s": 45.0,
    "min_hybrid_calls_before_early_stop": 2,
    "early_stop_min_unique_chunks": 28,
    "early_stop_no_gain_patience": 2,
}

MAX_CHUNKS_PER_DOC = 4
LEGAL_REF_PATTERN = re.compile(
    r"\b(?:art\.?|artigo)\s*(\d+[A-Za-zº°]*)\b(?:[^\n]{0,80}?\b(?:inciso|inc\.)\s*([IVXLCDM]+)\b)?",
    re.IGNORECASE,
)


def expand_query(query):
    expansions = {
        "parcialidade": "parcialidade favorecimento conflito de interesse tratamento desigual",
        "divergencia": "divergencia contradicao discordancia diferenca entendimento distinto",
        "risco": "risco impacto perigo ameaca dano potencial",
        "irregularidade": "irregularidade falha inconsistencia problema desconformidade",
    }

    q_lower = query.lower()
    for key, expansion in expansions.items():
        if key in q_lower:
            query = query + " " + expansion
    return query


def _strip_accents(text):
    normalized = unicodedata.normalize("NFKD", str(text or ""))
    return "".join(ch for ch in normalized if not unicodedata.combining(ch))


def _normalize_text_simple(text):
    text = _strip_accents((text or "").lower())
    return re.sub(r"\s+", " ", text).strip()


LEGAL_QUERY_PROFILE_SANCOES_RAL = {
    "name": "sancoes_ral_omissao",
    "trigger_groups": [
        (
            "sancao",
            "sancoes",
            "penalidade",
            "penalidades",
            "multa",
            "suspensao",
            "caducidade",
            "cancelamento",
        ),
        (
            "relatorio anual de lavra",
            "ral",
        ),
        (
            "omitir",
            "omissao",
            "informacao falsa",
            "declaracao falsa",
            "falsa declaracao",
            "inveridica",
        ),
    ],
    "boost_terms": (
        "sancao",
        "penalidade",
        "multa",
        "suspensao",
        "caducidade",
        "cancelamento",
        "relatorio anual de lavra",
        "ral",
        "omissao",
        "omitir",
        "informacao falsa",
        "declaracao falsa",
        "inveridica",
        "declarante",
        "titulo minerario",
        "titulo de lavra",
    ),
    "boost_phrases": (
        "relatorio anual de lavra",
        "nao apresentacao do relatorio anual de lavra",
        "prestacao de informacao falsa",
        "omitir informacao",
        "titulo minerario",
    ),
    "doc_title_hints": (
        "relatorio",
        "lavra",
        "sanc",
        "penal",
        "multa",
    ),
}


MINING_DOMAIN_TERMS = (
    "mineracao",
    "minerario",
    "mineraria",
    "minerais",
    "mineral",
    "minerio",
    "lavra",
    "garimpo",
    "jazida",
    "pesquisa mineral",
    "titulo minerario",
    "processo minerario",
    "codigo de mineracao",
    "anm",
    "dnpm",
    "cfem",
    "tah",
    "barragem de mineracao",
    "ral",
)

BROAD_NORMATIVE_TERMS = (
    "norma",
    "normas",
    "normativo",
    "normativos",
    "regra",
    "regras",
    "legislacao",
    "lei",
    "portaria",
    "resolucao",
    "decreto",
    "vigente",
    "vigencia",
    "aplicavel",
    "aplicaveis",
)


def _detect_legal_query_profile(query):
    qn = _normalize_text_simple(query)
    groups = LEGAL_QUERY_PROFILE_SANCOES_RAL["trigger_groups"]
    matches_per_group = [
        any(term in qn for term in group)
        for group in groups
    ]
    matched_groups = sum(1 for x in matches_per_group if x)

    # Ativa para o caso classico (3 grupos) e tambem para derivados fortes (2 grupos).
    if matched_groups >= 2:
        return LEGAL_QUERY_PROFILE_SANCOES_RAL
    return None


def _detect_mining_domain_intent(query):
    qn = _normalize_text_simple(query)
    has_mining_terms = any(term in qn for term in MINING_DOMAIN_TERMS)
    has_broad_normative_terms = any(term in qn for term in BROAD_NORMATIVE_TERMS)
    token_count = len(re.findall(r"\w+", qn))
    is_open_normative_query = bool(has_broad_normative_terms and token_count <= 14)

    return {
        "enabled": bool(has_mining_terms or is_open_normative_query),
        "has_mining_terms": has_mining_terms,
        "is_open_normative_query": is_open_normative_query,
    }


def _metadata_bool(value):
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in {"1", "true", "sim", "yes"}


def _chunk_domain_category(chunk):
    relation = str(chunk.get("relacao_com_mineracao") or "").strip().lower()
    use_main = _metadata_bool(chunk.get("usar_como_fundamento_principal"))
    family = str(chunk.get("familia_normativa_mineraria") or "").strip().lower()
    area = str(chunk.get("area_juridica_principal") or "").strip().lower()

    if use_main or relation == "direta":
        return "principal"
    if relation in {"indireta", "referenciada"}:
        return "apoio"
    if family and family not in {"nao_classificada", "contexto"}:
        return "apoio"
    if area == "minerario":
        return "apoio"
    return "contexto"


def _domain_boost_factor(chunk, mining_intent):
    if not mining_intent or not mining_intent.get("enabled"):
        return 1.0

    category = _chunk_domain_category(chunk)
    relation = str(chunk.get("relacao_com_mineracao") or "").strip().lower()
    area = str(chunk.get("area_juridica_principal") or "").strip().lower()
    family = str(chunk.get("familia_normativa_mineraria") or "").strip().lower()
    q_has_mining_terms = bool(mining_intent.get("has_mining_terms"))

    if category == "principal":
        factor = 1.28 if q_has_mining_terms else 1.05
    elif category == "apoio":
        factor = 1.08 if q_has_mining_terms else 1.00
    else:
        factor = 0.90 if q_has_mining_terms else 0.99

    if area == "minerario":
        factor += 0.08
    if family in {
        "regimes_de_aproveitamento",
        "garimpo_plg",
        "fiscalizacao_sancoes_minerarias",
        "barragens_rejeitos_seguranca",
        "mineracao_geral",
    }:
        factor += 0.08
    if family in {
        "cfem_tah_arrecadacao",
        "ambiental_mineracao",
        "seguranca_saude_ocupacional_mineracao",
    }:
        factor += 0.04
    if relation == "sem_relacao_identificada":
        factor -= 0.06

    return max(0.75, min(1.55, factor))


def _apply_mining_domain_policy(results, mining_intent):
    if not results or not mining_intent or not mining_intent.get("enabled"):
        return results

    adjusted = []
    for score, chunk in results:
        adjusted.append((float(score) * _domain_boost_factor(chunk, mining_intent), chunk))

    adjusted.sort(key=lambda item: float(item[0]), reverse=True)
    return adjusted


def _ensure_mining_fallback_coverage(reranked, candidate_pool, mining_intent, top_k):
    if not mining_intent or not mining_intent.get("enabled") or not candidate_pool:
        return reranked
    if not mining_intent.get("has_mining_terms"):
        return reranked

    selected = list(reranked or [])
    selected_ids = {chunk.get("chunk_id") for _, chunk in selected}
    has_principal = any(_chunk_domain_category(chunk) == "principal" for _, chunk in selected)
    if has_principal:
        return selected[:top_k]

    principal_candidates = [
        item
        for item in _apply_mining_domain_policy(candidate_pool, mining_intent)
        if _chunk_domain_category(item[1]) == "principal"
        and item[1].get("chunk_id") not in selected_ids
    ]
    if not principal_candidates:
        return selected[:top_k]

    reserve = max(1, min(3, top_k // 5))
    selected = selected[: max(0, top_k - reserve)] + principal_candidates[:reserve]
    selected = _dedupe_results(selected)
    return selected[:top_k]


def _build_profile_query_expansions(profile):
    if not profile:
        return []

    if profile["name"] == "sancoes_ral_omissao":
        return [
            "sancoes penalidades multa suspensao caducidade relatorio anual de lavra",
            "nao apresentacao do relatorio anual de lavra omissao informacao falsa",
            "titulo minerario declarante informacao falsa penalidade",
        ]

    return []


def _clamp_int(value, min_value, max_value):
    try:
        parsed = int(value)
    except Exception:
        parsed = min_value
    return max(min_value, min(max_value, parsed))


def _normalize_alpha_beta(alpha, beta):
    try:
        alpha = float(alpha)
    except Exception:
        alpha = 0.7
    try:
        beta = float(beta)
    except Exception:
        beta = 0.3

    alpha = max(0.0, min(1.0, alpha))
    beta = max(0.0, min(1.0, beta))

    if alpha == 0 and beta == 0:
        return 0.7, 0.3

    total = alpha + beta
    return alpha / total, beta / total


def _dedupe_keep_order(values):
    seen = set()
    ordered = []
    for value in values:
        key = (value or "").strip().lower()
        if not key or key in seen:
            continue
        seen.add(key)
        ordered.append(value.strip())
    return ordered


def _build_hybrid_profiles(alpha, beta, count):
    base_alpha, base_beta = _normalize_alpha_beta(alpha, beta)
    profiles = [(base_alpha, base_beta)]

    if count >= 2:
        a, b = _normalize_alpha_beta(base_alpha - 0.2, base_beta + 0.2)
        profiles.append((a, b))

    if count >= 3:
        a, b = _normalize_alpha_beta(base_alpha + 0.2, base_beta - 0.2)
        profiles.append((a, b))

    unique = []
    seen = set()
    for a, b in profiles:
        key = (round(a, 4), round(b, 4))
        if key in seen:
            continue
        seen.add(key)
        unique.append((a, b))
    return unique


def _auto_tune_config(query, config, temporal_intent):
    tuned = dict(config)
    legal_profile = _detect_legal_query_profile(query)
    intent_labels = set(detect_query_intent(query or ""))
    alpha, beta = _normalize_alpha_beta(
        tuned.get("alpha_semantic"),
        tuned.get("beta_lexical"),
    )
    tuned["alpha_semantic"] = alpha
    tuned["beta_lexical"] = beta

    tuned["top_k_retrieval"] = _clamp_int(tuned.get("top_k_retrieval", 60), 10, 200)
    tuned["top_k_final"] = _clamp_int(tuned.get("top_k_final", 20), 5, 80)
    tuned["max_context_chars"] = _clamp_int(
        tuned.get("max_context_chars", 20000), 4000, 120000
    )
    tuned["max_query_variants"] = _clamp_int(
        tuned.get("max_query_variants", 5), 1, 20
    )
    tuned["hybrid_profiles_per_query"] = _clamp_int(
        tuned.get("hybrid_profiles_per_query", 1), 1, 3
    )
    tuned["max_hybrid_searches"] = _clamp_int(
        tuned.get("max_hybrid_searches", 8), 1, 60
    )
    tuned["hybrid_time_budget_s"] = float(tuned.get("hybrid_time_budget_s", 45.0))
    tuned["hybrid_time_budget_s"] = max(10.0, min(180.0, tuned["hybrid_time_budget_s"]))
    tuned["min_hybrid_calls_before_early_stop"] = _clamp_int(
        tuned.get("min_hybrid_calls_before_early_stop", 2), 1, 10
    )
    tuned["early_stop_min_unique_chunks"] = _clamp_int(
        tuned.get("early_stop_min_unique_chunks", 28), 5, 200
    )
    tuned["early_stop_no_gain_patience"] = _clamp_int(
        tuned.get("early_stop_no_gain_patience", 2), 1, 8
    )

    adaptive_enabled = bool(tuned.get("adaptive_tuning_enabled", True))
    if not adaptive_enabled:
        if tuned["top_k_final"] > tuned["top_k_retrieval"]:
            tuned["top_k_final"] = tuned["top_k_retrieval"]
        return tuned

    query_lc = (query or "").lower()
    tokens = re.findall(r"\w+", query_lc)
    token_count = len(tokens)

    complexity_markers = (
        "compar",
        "diferenc",
        "entre",
        "histor",
        "linha do tempo",
        "vigent",
        "revog",
        "aplica",
        "altera",
    )
    has_complexity_marker = any(marker in query_lc for marker in complexity_markers)
    is_complex_query = token_count >= 16 or has_complexity_marker

    if is_complex_query:
        tuned["top_k_retrieval"] = max(tuned["top_k_retrieval"], 80)
        tuned["top_k_final"] = max(tuned["top_k_final"], 24)
        tuned["max_context_chars"] = max(tuned["max_context_chars"], 26000)
        tuned["max_query_variants"] = max(tuned["max_query_variants"], 5)
        tuned["max_hybrid_searches"] = max(tuned["max_hybrid_searches"], 8)
        tuned["hybrid_time_budget_s"] = max(tuned["hybrid_time_budget_s"], 50.0)
        tuned["early_stop_min_unique_chunks"] = max(
            tuned["early_stop_min_unique_chunks"], 24
        )

    if legal_profile and legal_profile.get("name") == "sancoes_ral_omissao":
        tuned["top_k_retrieval"] = max(tuned["top_k_retrieval"], 90)
        tuned["top_k_final"] = max(tuned["top_k_final"], 26)
        tuned["max_context_chars"] = max(tuned["max_context_chars"], 32000)
        tuned["max_query_variants"] = max(tuned["max_query_variants"], 6)
        tuned["max_hybrid_searches"] = max(tuned["max_hybrid_searches"], 10)
        tuned["hybrid_time_budget_s"] = max(tuned["hybrid_time_budget_s"], 55.0)
        tuned["early_stop_min_unique_chunks"] = max(
            tuned["early_stop_min_unique_chunks"], 26
        )
        tuned["min_hybrid_calls_before_early_stop"] = max(
            tuned["min_hybrid_calls_before_early_stop"], 6
        )
        tuned["early_stop_no_gain_patience"] = max(
            tuned["early_stop_no_gain_patience"], 4
        )
        # Favorece leve componente lexical para linguagem sancionatoria.
        alpha, beta = _normalize_alpha_beta(
            max(0.2, tuned["alpha_semantic"] - 0.08),
            min(0.8, tuned["beta_lexical"] + 0.08),
        )
        tuned["alpha_semantic"] = alpha
        tuned["beta_lexical"] = beta

    if temporal_intent.get("enabled"):
        tuned["top_k_retrieval"] = max(tuned["top_k_retrieval"], 90)
        tuned["top_k_final"] = max(tuned["top_k_final"], 26)
        tuned["max_context_chars"] = max(tuned["max_context_chars"], 32000)
        tuned["max_query_variants"] = max(tuned["max_query_variants"], 6)
        tuned["max_hybrid_searches"] = max(tuned["max_hybrid_searches"], 10)
        tuned["hybrid_time_budget_s"] = max(tuned["hybrid_time_budget_s"], 55.0)
        tuned["early_stop_min_unique_chunks"] = max(
            tuned["early_stop_min_unique_chunks"], 26
        )
        tuned["min_hybrid_calls_before_early_stop"] = max(
            tuned["min_hybrid_calls_before_early_stop"], 6
        )
        tuned["early_stop_no_gain_patience"] = max(
            tuned["early_stop_no_gain_patience"], 4
        )

        # Em normativa temporal, damos um pouco mais de peso lexical.
        alpha, beta = _normalize_alpha_beta(
            max(0.2, tuned["alpha_semantic"] - 0.1),
            min(0.8, tuned["beta_lexical"] + 0.1),
        )
        tuned["alpha_semantic"] = alpha
        tuned["beta_lexical"] = beta

    if "term_frequency" in intent_labels or "term_documents" in intent_labels:
        tuned["top_k_retrieval"] = max(tuned["top_k_retrieval"], 85)
        tuned["top_k_final"] = max(tuned["top_k_final"], 24)
        tuned["max_context_chars"] = max(tuned["max_context_chars"], 26000)
        tuned["max_query_variants"] = max(tuned["max_query_variants"], 5)
        tuned["max_hybrid_searches"] = max(tuned["max_hybrid_searches"], 8)
        tuned["hybrid_time_budget_s"] = max(tuned["hybrid_time_budget_s"], 45.0)
        tuned["early_stop_min_unique_chunks"] = max(
            tuned["early_stop_min_unique_chunks"], 24
        )

        alpha, beta = _normalize_alpha_beta(
            max(0.2, tuned["alpha_semantic"] - 0.15),
            min(0.8, tuned["beta_lexical"] + 0.15),
        )
        tuned["alpha_semantic"] = alpha
        tuned["beta_lexical"] = beta

    if token_count <= 5 and not temporal_intent.get("enabled"):
        tuned["top_k_retrieval"] = min(tuned["top_k_retrieval"], 65)
        tuned["top_k_final"] = min(tuned["top_k_final"], 18)
        tuned["max_context_chars"] = min(tuned["max_context_chars"], 18000)
        tuned["max_query_variants"] = min(tuned["max_query_variants"], 3)
        tuned["hybrid_profiles_per_query"] = min(tuned["hybrid_profiles_per_query"], 1)
        tuned["max_hybrid_searches"] = min(tuned["max_hybrid_searches"], 5)
        tuned["hybrid_time_budget_s"] = min(tuned["hybrid_time_budget_s"], 30.0)

    # Guardrail de custo para evitar explosao combinatoria.
    search_space = tuned["max_query_variants"] * tuned["hybrid_profiles_per_query"]
    if search_space > 10:
        tuned["hybrid_profiles_per_query"] = 1
        search_space = tuned["max_query_variants"]

    tuned["max_hybrid_searches"] = min(
        tuned["max_hybrid_searches"],
        max(4, search_space),
        12,
    )

    if tuned["top_k_final"] > tuned["top_k_retrieval"]:
        tuned["top_k_final"] = tuned["top_k_retrieval"]

    return tuned


def _run_rerank_with_fallback(query, results, config, top_k, progress_callback=None):
    if not results:
        return []

    t_rerank = time.time()
    rerank_model = get_runtime_rerank_model()
    try:
        ranked = rerank(
            query,
            results,
            alpha=config["alpha_semantic"],
            beta=config["beta_lexical"],
            top_k=top_k,
        )
        _emit_timing(
            f"rerank [{rerank_model}]",
            t_rerank,
            progress_callback=progress_callback,
        )
        return ranked
    except Exception:
        # Degradacao segura quando embedding/reranker falhar:
        # mantem ordenacao por score original.
        ranked = sorted(results, key=lambda x: float(x[0]), reverse=True)[:top_k]
        _emit_timing(
            f"rerank [{rerank_model}->fallback_local]",
            t_rerank,
            progress_callback=progress_callback,
        )
        return ranked


def _answer_llm_fallback_models(primary_model):
    model = str(primary_model or "").strip() or DEFAULT_LLM_MODEL
    return [model]


def _messages_with_reduced_context(messages, original_context, max_context_chars):
    if not original_context or len(original_context) <= max_context_chars:
        return messages

    reduced_context = (
        original_context[:max_context_chars].rstrip()
        + "\n\n[Contexto reduzido automaticamente apos timeout da LIA.]"
    )
    updated_messages = []
    replaced = False

    for message in messages:
        updated = dict(message)
        content = str(updated.get("content", ""))

        if not replaced and original_context in content:
            updated["content"] = content.replace(original_context, reduced_context, 1)
            replaced = True

        updated_messages.append(updated)

    return updated_messages


def _chat_completion_with_model_fallback(
    messages,
    temperature,
    llm_model=None,
    progress_callback=None,
    retry_context=None,
):
    last_error = None
    fallback_models = _answer_llm_fallback_models(llm_model)
    context_retry_limits = [None]
    if retry_context:
        context_retry_limits.extend([12000, 8000, 5000])

    for attempt, model in enumerate(fallback_models):
        for context_limit in context_retry_limits:
            attempt_messages = messages
            if context_limit is not None:
                attempt_messages = _messages_with_reduced_context(
                    messages,
                    retry_context,
                    context_limit,
                )

            try:
                return model, chat_completion(
                    attempt_messages,
                    temperature=temperature,
                    llm_model=model,
                    max_retries=1,
                )
            except LIAClientError as exc:
                last_error = exc

                can_retry_context = (
                    exc.status_code in {502, 503, 504}
                    and retry_context
                    and context_limit != context_retry_limits[-1]
                )

                if not can_retry_context:
                    raise

                if progress_callback:
                    next_limit = context_retry_limits[
                        context_retry_limits.index(context_limit) + 1
                    ]
                    progress_callback(
                        f"[WARN] Chamada final falhou na LIA com HTTP {exc.status_code} "
                        f"usando {model}. Tentando novamente com contexto reduzido "
                        f"({next_limit} caracteres)."
                    )

    if last_error:
        raise last_error

    raise LIAClientError("Nenhum modelo LLM final disponivel para tentativa.")


def _reference_period_from_intent(temporal_intent):
    if not temporal_intent:
        return None, None

    reference_start = temporal_intent.get("reference_start")
    reference_end = temporal_intent.get("reference_end") or reference_start
    return reference_start, reference_end


def _reference_label_from_intent(temporal_intent):
    if not temporal_intent:
        return "nao informado"

    reference_start, reference_end = _reference_period_from_intent(temporal_intent)
    return (
        temporal_intent.get("reference_label")
        or format_reference_period(reference_start, reference_end)
    )


def _reference_search_hint_from_intent(temporal_intent):
    if not temporal_intent:
        return None

    reference_start, _ = _reference_period_from_intent(temporal_intent)
    if not reference_start:
        return None
    return reference_start.strftime("%d/%m/%Y")


def _evaluate_temporal_gate(results, temporal_intent):
    reference_start, reference_end = _reference_period_from_intent(temporal_intent)
    if not reference_start or not reference_end:
        return {
            "year": None,
            "reference_label": None,
            "normative_count": 0,
            "active_normative_count": 0,
            "revoked_related_count": 0,
            "has_active_normative": False,
        }

    normative = [
        chunk
        for _, chunk in results
        if has_normative_metadata(chunk) and is_effectively_normative(chunk)
    ]
    active = [
        chunk
        for chunk in normative
        if is_active_in_reference_period(chunk, reference_start, reference_end)
    ]
    revoked = [
        chunk
        for chunk in normative
        if is_revoked_related(
            chunk,
            year=temporal_intent.get("year"),
            reference_start=reference_start,
            reference_end=reference_end,
        )
    ]

    return {
        "year": temporal_intent.get("year"),
        "reference_label": _reference_label_from_intent(temporal_intent),
        "normative_count": len(normative),
        "active_normative_count": len(active),
        "revoked_related_count": len(revoked),
        "has_active_normative": bool(active),
    }


DEFAULT_PROMPT_TEMPLATE = """
Voce e um assistente tecnico-juridico especializado em pesquisa de legislacao.

Data atual para referencia temporal: {data_referencia_iso} ({data_referencia_br})

Responda com base exclusivamente nas evidencias fornecidas no contexto, sem extrapolacoes.

REGRAS OBRIGATORIAS:
1. Utilize apenas informacoes presentes no contexto.
2. Sempre cite o documento e a pagina ao apresentar informacoes materiais.
3. Se houver metadados normativos (vigencia/revogacao), explicite a situacao da norma.
4. Se a consulta for temporal (data, mes/ano, ano ou \"hoje/atualmente\"), destaque a aplicabilidade no periodo consultado.
5. Quando a pergunta for mineraria, priorize normas com metadado usar_como_fundamento_principal=true ou relacao_com_mineracao=direta.
6. Use normas indiretas, referenciadas ou contextuais apenas como apoio/fallback, deixando isso claro.
7. Caso a informacao nao esteja no contexto, diga explicitamente que nao foi encontrada.
8. Nao invente interpretacoes ou dados ausentes.
9. Ao citar artigo/inciso/paragrafo/alinea, use a numeracao literal do contexto (nao renumerar e nao aproximar).
10. Se nao conseguir confirmar literalmente um numero de artigo/inciso no contexto, escreva "referencia normativa nao confirmada no contexto".

Estruture a resposta em:
1. Resposta direta
2. Fundamentacao normativa
3. Aplicabilidade temporal (quando couber)
4. Evidencias encontradas (com referencia aos documentos)
5. Checagem de referencia normativa (artigo/inciso citados e fonte)
6. Conclusao

Mapa de referencias normativas extraidas do contexto:
{referencias_normativas_contexto}

Pergunta:
{query}

Contexto:
{context}
"""


TEMPORAL_PROMPT_TEMPLATE = """
Voce e um assistente tecnico-juridico especializado em sucessao normativa no tempo.

Recorte temporal da consulta: {recorte_temporal}
Ano principal de referencia: {ano_referencia}
Data de corte efetivamente adotada: {data_corte_temporal}
Aviso de corte temporal: {nota_corte_temporal}
Data atual para referencia temporal: {data_referencia_iso} ({data_referencia_br})

REGRAS OBRIGATORIAS:
1. Use exclusivamente o contexto fornecido.
2. Identifique a situacao de cada norma no recorte temporal consultado (vigente, revogada, parcialmente revogada ou nao identificada).
3. Para as conclusoes, use SOMENTE regras em vigor no periodo consultado.
4. Se uma norma estiver revogada, NAO a use como conclusao vigente. Trate como historico e delimite sua aplicabilidade ao intervalo de vigencia.
5. Quando houver indicio de revogacao por outro normativo, priorize o normativo em vigor para conclusoes e mencione o revogado apenas como historico.
6. Considere vacatio legis quando houver (ex.: publicacao + vacatio_dias) para definir inicio de eficacia.
7. Se houver ambiguidade ou ausencia de metadado, declare a incerteza.
8. Quando a pergunta for mineraria, priorize normas minerarias diretas vigentes no periodo; use normas transversais/referenciadas apenas como apoio/fallback.
9. Cite documento e pagina para cada afirmacao material.
10. Ao citar artigo/inciso/paragrafo/alinea, use numeracao literal do contexto (nao renumerar e nao aproximar).
11. Se numero de artigo/inciso nao estiver literal no contexto, marque como "nao confirmado no contexto".
12. Se houver um aviso de corte temporal diferente de "nao se aplica", mencione isso explicitamente na resposta direta.

Formato da resposta:
1. Tabela "Conclusao normativa (SOMENTE vigentes no periodo consultado)" com colunas:
   - Norma
   - Status no recorte temporal
   - Periodo de eficacia considerado
   - Regra aplicavel para conclusao
   - Fonte (documento e pagina)
2. Tabela "Historico de normas revogadas ou alteradas" com colunas:
   - Norma
   - O que previa (resumo objetivo)
   - Periodo em que produziu efeitos
   - Situacao atual (revogada/parcialmente revogada)
   - Revogado por / substituida por
   - Fonte (documento e pagina)
3. Linha do tempo sintetica (bullet points curtos)
4. Conclusao final (baseada SOMENTE nas normas vigentes no periodo consultado)
5. Pontos incertos para revisao manual

Se algum campo nao estiver no contexto, preencha como "nao identificado".
Se nao houver norma vigente identificada para o periodo consultado, explicite que nao ha base suficiente para conclusao normativa valida.

Mapa de referencias normativas extraidas do contexto:
{referencias_normativas_contexto}

Pergunta:
{query}

Contexto:
{context}
"""


def mmr_select(results, top_k=20, lambda_param=0.7):
    if not results:
        return []

    selected = []
    candidates = results.copy()
    selected.append(candidates.pop(0))

    while candidates and len(selected) < top_k:
        best_score = -1
        best_candidate = None

        for candidate in candidates:
            relevance = candidate[0]
            diversity = max(abs(candidate[0] - s[0]) for s in selected)
            mmr_score = (lambda_param * relevance) - ((1 - lambda_param) * diversity)

            if mmr_score > best_score:
                best_score = mmr_score
                best_candidate = candidate

        selected.append(best_candidate)
        candidates.remove(best_candidate)

    return selected


def _normative_metadata_line(chunk):
    if not has_normative_metadata(chunk):
        return ""

    fields = []
    for key in (
        "tipo_norma",
        "numero_norma",
        "ano_norma",
        "status_normativo",
        "data_inicio_vigencia",
        "data_fim_vigencia",
        "vacatio_dias",
        "revogado_por",
        "revogado_por_data",
        "tipo_revogacao",
        "tipo_chunk",
    ):
        value = chunk.get(key)
        if value not in (None, "", []):
            fields.append(f"{key}={value}")

    revoga = chunk.get("revoga")
    if isinstance(revoga, list) and revoga:
        fields.append(f"revoga={'; '.join(str(x) for x in revoga[:3])}")

    if not fields:
        return ""

    return "[Metadados normativos] " + " | ".join(fields)


def _domain_metadata_line(chunk):
    fields = []
    for key in (
        "base_rag",
        "area_juridica_principal",
        "relacao_com_mineracao",
        "familia_normativa_mineraria",
        "papel_no_corpus_minerario",
        "aplicacao_mineraria",
        "usar_como_fundamento_principal",
        "confianca_classificacao_mineraria",
    ):
        value = chunk.get(key)
        if value not in (None, "", []):
            fields.append(f"{key}={value}")

    if not fields:
        return ""
    return "[Metadados de dominio] " + " | ".join(fields)


class _PromptVars(dict):
    def __missing__(self, key):
        return "{" + str(key) + "}"


def _format_prompt_template(template, prompt_vars):
    return str(template or "").format_map(_PromptVars(prompt_vars))


def _build_metadata_context(results, temporal_intent=None):
    lines = []
    seen = set()

    for _, chunk in results or []:
        doc = chunk.get("doc") or chunk.get("doc_name") or "documento_desconhecido"
        page = chunk.get("page") or "nao identificada"
        meta_line = _normative_metadata_line(chunk)
        domain_line = _domain_metadata_line(chunk)
        temporal_line = _temporal_metadata_line(chunk, temporal_intent)
        metadata_lines = [line for line in (meta_line, domain_line, temporal_line) if line]
        if not metadata_lines:
            continue

        key = (doc, page, tuple(metadata_lines))
        if key in seen:
            continue
        seen.add(key)
        lines.append(f"- {doc}, pagina {page}: " + " ".join(metadata_lines))

    if not lines:
        return "Metadados especificos nao identificados no contexto recuperado."

    return "\n".join(lines)


def _format_date(value):
    if not value:
        return "nao identificado"
    return value.isoformat()


def _format_period(start, end):
    start_text = _format_date(start)
    end_text = _format_date(end)
    if start_text == "nao identificado" and end_text == "nao identificado":
        return "nao identificado"
    return f"{start_text} a {end_text}"


def _temporal_metadata_line(chunk, temporal_intent):
    if not temporal_intent or not has_normative_metadata(chunk):
        return ""

    reference_start, reference_end = _reference_period_from_intent(temporal_intent)
    if not reference_start or not reference_end:
        return ""

    start, end = effective_period(chunk)
    status_period = status_in_reference_period(chunk, reference_start, reference_end)
    status_atual = chunk.get("status_normativo") or "desconhecido"
    revogado_por = chunk.get("revogado_por") or "nao identificado"
    vacatio = chunk.get("vacatio_dias")
    vacatio_text = "nao identificado" if vacatio in (None, "", []) else str(vacatio)

    return (
        "[Analise temporal derivada] "
        f"status_no_recorte={status_period} | status_atual={status_atual} | "
        f"periodo_eficacia={_format_period(start, end)} | "
        f"vacatio_dias={vacatio_text} | revogado_por={revogado_por}"
    )


def _dedupe_results(results):
    seen = set()
    deduped = []
    for score, chunk in results:
        chunk_id = chunk.get("chunk_id")
        if chunk_id in seen:
            continue
        seen.add(chunk_id)
        deduped.append((score, chunk))
    return deduped


def _query_token_set(text):
    normalized = _normalize_text_simple(text)
    return {token for token in normalized.split() if len(token) > 3}


def _lexical_overlap_score(query, chunk):
    tokens = _query_token_set(query)
    if not tokens:
        return 0.0

    candidate_text = " ".join(
        str(chunk.get(key) or "")
        for key in ("doc_name", "titulo_norma", "text")
    )
    candidate_norm = _normalize_text_simple(candidate_text)
    if not candidate_norm:
        return 0.0

    overlap = sum(1 for token in tokens if token in candidate_norm)
    score = overlap / len(tokens)

    if "relatorio anual de lavra" in candidate_norm and "relatorio" in tokens:
        score += 0.25
    if "informacao falsa" in candidate_norm and "informacao" in tokens:
        score += 0.2
    if "declaracao falsa" in candidate_norm and "declaracao" in tokens:
        score += 0.2
    if "sanc" in candidate_norm and any(token.startswith("sanc") for token in tokens):
        score += 0.15

    return score


def _recover_successor_normative_results(
    revoked_results,
    base_name,
    query,
    reference_start,
    reference_end,
    max_results,
):
    if not revoked_results or not base_name or max_results <= 0:
        return []

    try:
        chunk_lookup = get_chunk_lookup(base_name)
    except Exception:
        return []

    successor_doc_ids = []
    for _, chunk in revoked_results:
        successor_doc_id = str(chunk.get("revogado_por_doc_id") or "").strip()
        if not successor_doc_id:
            continue
        if successor_doc_id not in successor_doc_ids:
            successor_doc_ids.append(successor_doc_id)

    if not successor_doc_ids:
        return []

    candidates = []
    seen = set()
    for chunk in chunk_lookup.values():
        doc_id = str(chunk.get("doc_id") or "").strip()
        chunk_id = chunk.get("chunk_id")
        if not chunk_id or doc_id not in successor_doc_ids:
            continue
        if chunk_id in seen:
            continue
        if not has_normative_metadata(chunk) or not is_effectively_normative(chunk):
            continue
        if not is_active_in_reference_period(chunk, reference_start, reference_end):
            continue

        overlap_score = _lexical_overlap_score(query, chunk)
        if overlap_score <= 0:
            continue

        seen.add(chunk_id)
        candidates.append((overlap_score, chunk))

    candidates.sort(
        key=lambda item: (
            -float(item[0]),
            int(item[1].get("page") or 0),
            int(item[1].get("chunk_index") or 0),
        )
    )
    return candidates[:max_results]


def _summarize_temporal_groups(active_results, revoked_results, temporal_intent):
    reference_start, reference_end = _reference_period_from_intent(temporal_intent)
    reference_label = _reference_label_from_intent(temporal_intent)
    lines = [f"### Resumo temporal normativo (recorte de referencia: {reference_label})"]
    cutoff_notice = str(temporal_intent.get("cutoff_notice") or "").strip()
    if cutoff_notice:
        lines.append(f"Aviso de corte temporal adotado: {cutoff_notice}")

    if active_results:
        lines.append("Normativos com indicio de vigencia no periodo:")
        seen_titles = set()
        for _, chunk in active_results:
            title = chunk_normative_title(chunk)
            if title in seen_titles:
                continue
            seen_titles.add(title)
            status_atual = chunk.get("status_normativo") or "desconhecido"
            status_no_periodo = status_in_reference_period(
                chunk,
                reference_start,
                reference_end,
            )
            ini, fim = effective_period(chunk)
            periodo = _format_period(ini, fim)
            revogado_por = chunk.get("revogado_por") or "nao identificado"
            ref = f"{chunk.get('doc')} p.{chunk.get('page')}"
            lines.append(
                f"- {title} | status_no_recorte={status_no_periodo} | status_atual={status_atual} | "
                f"periodo_eficacia={periodo} | revogado_por={revogado_por} | fonte={ref}"
            )
            if len(seen_titles) >= 8:
                break
    else:
        lines.append("Normativos com indicio de vigencia no periodo: nao identificados com seguranca.")

    if revoked_results:
        lines.append("Normativos revogados relacionados:")
        seen_titles = set()
        active_titles = {
            chunk_normative_title(chunk) for _, chunk in active_results
        }
        for _, chunk in revoked_results:
            title = chunk_normative_title(chunk)
            if title in seen_titles or title in active_titles:
                continue
            seen_titles.add(title)
            ini, fim = effective_period(chunk)
            periodo = _format_period(ini, fim)
            revogado_por = chunk.get("revogado_por")
            ref = f"{chunk.get('doc')} p.{chunk.get('page')}"
            lines.append(
                f"- {title} | periodo_eficacia={periodo} | revogado_por={revogado_por} | fonte={ref}"
            )
            if len(seen_titles) >= 8:
                break
    else:
        lines.append("Normativos revogados relacionados: nao identificados com seguranca.")

    return "\n".join(lines) + "\n\n"


def _select_temporal_results(results, query, config, temporal_intent, base_name=None, progress_callback=None):
    reference_start, reference_end = _reference_period_from_intent(temporal_intent)
    if not reference_start or not reference_end:
        return None, None

    normative_results = [
        (score, chunk)
        for score, chunk in results
        if has_normative_metadata(chunk) and is_effectively_normative(chunk)
    ]
    if not normative_results:
        return None, None

    active = []
    revoked = []
    other = []

    for score, chunk in normative_results:
        active_flag = is_active_in_reference_period(chunk, reference_start, reference_end)
        revoked_flag = is_revoked_related(
            chunk,
            year=temporal_intent.get("year"),
            reference_start=reference_start,
            reference_end=reference_end,
        )

        if active_flag:
            active.append((score, chunk))
        if revoked_flag:
            revoked.append((score, chunk))
        if not active_flag and not revoked_flag:
            other.append((score, chunk))

    if not active and not revoked:
        return None, None

    top_k_final = int(config["top_k_final"])
    active_budget = max(1, round(top_k_final * 0.65))
    revoked_budget = max(1, top_k_final - active_budget)

    active_ranked = []
    if active:
        active_ranked = _run_rerank_with_fallback(
            query=query,
            results=active,
            config=config,
            top_k=min(active_budget, len(active)),
            progress_callback=progress_callback,
        )

    revoked_ranked = []
    if revoked:
        revoked_ranked = _run_rerank_with_fallback(
            query=query,
            results=revoked,
            config=config,
            top_k=min(revoked_budget, len(revoked)),
            progress_callback=progress_callback,
        )

    recovered_successors = []
    if not active_ranked and revoked_ranked:
        recovered_successors = _recover_successor_normative_results(
            revoked_results=revoked_ranked,
            base_name=base_name,
            query=query,
            reference_start=reference_start,
            reference_end=reference_end,
            max_results=active_budget,
        )
        if recovered_successors:
            active_ranked = recovered_successors

    selected = _dedupe_results(active_ranked + revoked_ranked)
    normative_pool = _dedupe_results(normative_results + recovered_successors)

    if len(selected) < top_k_final:
        fallback_ranked = _run_rerank_with_fallback(
            query=query,
            results=normative_pool,
            config=config,
            top_k=min(len(normative_pool), top_k_final * 2),
            progress_callback=progress_callback,
        )
        for item in fallback_ranked:
            if len(selected) >= top_k_final:
                break
            chunk_id = item[1].get("chunk_id")
            if all(existing[1].get("chunk_id") != chunk_id for existing in selected):
                selected.append(item)

    summary = _summarize_temporal_groups(active_ranked, revoked_ranked, temporal_intent)
    return selected[:top_k_final], summary


def build_context(results, max_chars, preamble=None, temporal_intent=None):
    context = []
    total = 0

    if preamble:
        preamble_text = preamble.strip() + "\n\n"
        if len(preamble_text) <= max_chars:
            context.append(preamble_text)
            total += len(preamble_text)

    reordered = []
    left = 0
    right = len(results) - 1
    while left <= right:
        reordered.append(results[left])
        if left != right:
            reordered.append(results[right])
        left += 1
        right -= 1

    doc_counter = {}
    docs = defaultdict(list)

    for score, chunk in reordered:
        doc = chunk.get("doc") or chunk.get("doc_name") or "documento_desconhecido"
        if doc_counter.get(doc, 0) >= MAX_CHUNKS_PER_DOC:
            continue
        doc_counter[doc] = doc_counter.get(doc, 0) + 1
        docs[doc].append((score, chunk))

    for doc, chunks in docs.items():
        header = f"\n### Documento fonte: {doc}\n"
        if total + len(header) > max_chars:
            break
        context.append(header)
        total += len(header)

        for _, chunk in chunks:
            page = chunk.get("page")
            meta_line = _normative_metadata_line(chunk)
            domain_line = _domain_metadata_line(chunk)
            temporal_line = _temporal_metadata_line(chunk, temporal_intent)
            text = f"[Pagina {page}]\n"
            if meta_line:
                text += f"{meta_line}\n"
            if domain_line:
                text += f"{domain_line}\n"
            if temporal_line:
                text += f"{temporal_line}\n"
            text += f"{chunk.get('text', '')}\n"

            if total + len(text) > max_chars:
                break
            context.append(text)
            total += len(text)

    return "\n".join(context)


def highlight_text(text, query):
    words = set(w for w in re.findall(r"\w+", query.lower()) if len(w) > 3)
    for word in words:
        text = re.sub(
            fr"\b({re.escape(word)})\b",
            r"**\1**",
            text,
            flags=re.IGNORECASE,
        )
    return text


def build_evidence(results, query):
    evidences = []
    for score, chunk in results[:12]:
        sentence = (chunk.get("text") or "")[:600]
        sentence = highlight_text(sentence, query)
        meta_line = _normative_metadata_line(chunk)
        domain_line = _domain_metadata_line(chunk)
        base_label = chunk.get("base_rag")
        title = f"{chunk.get('doc')} | Pagina {chunk.get('page')} | Score {round(score, 3)}"
        if base_label:
            title = f"Base {base_label} | " + title

        block = f"### {title}\n\n"
        if meta_line:
            block += f"- {meta_line}\n\n"
        if domain_line:
            block += f"- {domain_line}\n\n"
        block += f"> {sentence}\n"
        evidences.append(block)

    return "\n".join(evidences)


def _normalize_article_token(article_token):
    token = str(article_token or "").strip()
    token = token.replace("º", "").replace("°", "")
    token = re.sub(r"\s+", "", token)
    return token.lower()


def _article_pattern_from_token(article_token):
    token = _normalize_article_token(article_token)
    m = re.match(r"^(\d+)([a-z]*)$", token)
    if not m:
        return re.compile(
            rf"\b(?:art\.?|artigo)\s*{re.escape(token)}\b",
            re.IGNORECASE,
        )

    number, suffix = m.groups()
    if suffix:
        pattern = rf"\b(?:art\.?|artigo)\s*{re.escape(number)}\s*[º°]?\s*{re.escape(suffix)}\b"
    else:
        pattern = rf"\b(?:art\.?|artigo)\s*{re.escape(number)}\s*[º°]?\b"
    return re.compile(pattern, re.IGNORECASE)


def _extract_answer_references(answer_text):
    refs = []
    seen = set()
    for match in LEGAL_REF_PATTERN.finditer(answer_text or ""):
        article = _normalize_article_token(match.group(1))
        inciso = (match.group(2) or "").upper().strip()
        if not article:
            continue
        key = (article, inciso)
        if key in seen:
            continue
        seen.add(key)
        refs.append((article, inciso, match.group(0).strip()))
    return refs


def _context_has_reference(context_text, article, inciso):
    text = context_text or ""
    art_pattern = _article_pattern_from_token(article)
    art_matches = list(art_pattern.finditer(text))
    if not art_matches:
        return False

    if not inciso:
        return True

    inciso_pattern = re.compile(
        rf"\b(?:inciso|inc\.)\s*{re.escape(inciso)}\b",
        re.IGNORECASE,
    )
    for art_match in art_matches:
        start = max(0, art_match.start() - 80)
        end = min(len(text), art_match.end() + 220)
        window = text[start:end]
        if inciso_pattern.search(window):
            return True
    return False


def _build_reference_map(results, max_items=24):
    items = []
    seen = set()

    for _, chunk in results:
        doc = chunk.get("doc")
        page = chunk.get("page")
        text = chunk.get("text") or ""

        for match in LEGAL_REF_PATTERN.finditer(text):
            article_raw = (match.group(1) or "").strip()
            inciso = (match.group(2) or "").strip().upper()
            article_norm = _normalize_article_token(article_raw)
            if not article_norm:
                continue

            key = (doc, page, article_norm, inciso)
            if key in seen:
                continue
            seen.add(key)

            ref_text = f"art. {article_raw}"
            if inciso:
                ref_text += f", inciso {inciso}"

            snippet_start = max(0, match.start() - 70)
            snippet_end = min(len(text), match.end() + 130)
            snippet = re.sub(r"\s+", " ", text[snippet_start:snippet_end]).strip()
            items.append(
                f"- {doc} p.{page}: {ref_text} | trecho: \"{snippet}\""
            )
            if len(items) >= max_items:
                return "\n".join(items)

    if not items:
        return "nao identificado"
    return "\n".join(items)


def _reference_consistency_note(answer_text, context_text):
    refs = _extract_answer_references(answer_text)
    if not refs:
        return ""

    missing = []
    for article, inciso, raw in refs:
        if not _context_has_reference(context_text, article, inciso):
            missing.append(raw)

    if not missing:
        return ""

    missing_text = "; ".join(sorted(set(missing)))
    return (
        "As seguintes referencias normativas nao foram confirmadas literalmente no contexto recuperado: "
        f"{missing_text}. Revise manualmente a numeracao (artigo/inciso)."
    )


def _emit_timing(label, start_ts, progress_callback=None):
    elapsed = round(time.time() - start_ts, 2)
    message = f"[TIME] {label}: {elapsed} s"
    print(message)
    if progress_callback:
        try:
            progress_callback(message)
        except Exception:
            pass


def ask(
    query,
    base_name,
    temperature=0.2,
    llm_model=None,
    forced_doc=None,
    config_override=None,
    custom_prompt=None,
    allowed_docs=None,
    progress_callback=None,
):
    config = RAG_CONFIG.copy()
    if config_override:
        config.update(config_override)

    temporal_intent = detect_temporal_normative_intent(
        query,
        progress_callback=progress_callback,
    )
    routing = route_query(
        query,
        base_name=base_name,
        allowed_docs=allowed_docs,
        temporal_intent=temporal_intent,
    )
    if forced_doc:
        routing = {"strategy": "manual_override", "filter_doc": forced_doc}

    filter_doc = routing.get("filter_doc")
    query_expanded = expand_query(query)
    query_profile = _detect_legal_query_profile(query)
    mining_intent = _detect_mining_domain_intent(query)
    routing["mining_domain_intent"] = mining_intent
    config = _auto_tune_config(query, config, temporal_intent)

    t0 = time.time()
    query_candidates = [query_expanded]
    query_candidates.extend(generate_query_variants(query_expanded))
    query_candidates.extend(decompose_query(query_expanded))
    query_candidates.extend(_build_profile_query_expansions(query_profile))
    if temporal_intent.get("enabled"):
        reference_label = temporal_intent.get("reference_label")
        reference_year = temporal_intent.get("year")
        reference_search_hint = _reference_search_hint_from_intent(temporal_intent)
        query_candidates.extend(
            [
                f"{query_expanded} vigencia",
                f"{query_expanded} revogacao",
                f"{query_expanded} norma vigente",
            ]
        )
        if reference_search_hint:
            query_candidates.append(f"{query_expanded} vigente em {reference_search_hint}")
        if reference_year:
            query_candidates.extend(
                [
                    f"{query_expanded} vigencia {reference_year}",
                    f"{query_expanded} revogacao {reference_year}",
                    f"{query_expanded} norma vigente {reference_year}",
                ]
            )
        query_candidates.extend(temporal_intent.get("llm_search_queries") or [])
    if mining_intent.get("enabled"):
        query_candidates.extend(
            [
                f"{query_expanded} mineracao norma mineraria",
                f"{query_expanded} legislacao mineraria anm dnpm",
            ]
        )
        if mining_intent.get("has_mining_terms"):
            query_candidates.extend(
                [
                    f"{query_expanded} codigo de mineracao",
                    f"{query_expanded} titulo minerario lavra pesquisa mineral",
                ]
            )
    queries = _dedupe_keep_order(query_candidates)
    queries = queries[: config["max_query_variants"]]
    if not queries:
        queries = [query_expanded]

    profiles = _build_hybrid_profiles(
        config["alpha_semantic"],
        config["beta_lexical"],
        config["hybrid_profiles_per_query"],
    )
    _emit_timing(
        f"query generation [{get_runtime_query_generation_model()}]",
        t0,
        progress_callback=progress_callback,
    )

    results_list = []
    max_hybrid_searches = int(config["max_hybrid_searches"])
    hybrid_time_budget_s = float(config.get("hybrid_time_budget_s", 45.0))
    min_calls_before_early_stop = int(
        config.get("min_hybrid_calls_before_early_stop", 2)
    )
    early_stop_min_unique_chunks = int(
        config.get(
            "early_stop_min_unique_chunks",
            max(config["top_k_final"] * 2, 24),
        )
    )
    early_stop_no_gain_patience = int(config.get("early_stop_no_gain_patience", 2))
    loop_start = time.time()
    search_calls = 0
    hybrid_failure_reported = False
    unique_chunk_ids = set()
    no_gain_streak = 0
    stop_search = False
    for q_idx, q in enumerate(queries, start=1):
        if search_calls >= max_hybrid_searches or stop_search:
            break

        for p_idx, (h_alpha, h_beta) in enumerate(profiles, start=1):
            if search_calls >= max_hybrid_searches or stop_search:
                break

            t_search = time.time()
            try:
                found = hybrid_search(
                    q,
                    top_k=config["top_k_retrieval"],
                    filter_doc=filter_doc,
                    base_name=base_name,
                    allowed_docs=allowed_docs,
                    alpha=h_alpha,
                    beta=h_beta,
                )
            except Exception:
                found = []
                if progress_callback and not hybrid_failure_reported:
                    progress_callback(
                        "[WARN] Falha na busca hibrida; tentativa ignorada (fallback lexical desativado)."
                    )
                    hybrid_failure_reported = True

            _emit_timing(
                f"hybrid_search q{q_idx}/{len(queries)} p{p_idx}/{len(profiles)}",
                t_search,
                progress_callback=progress_callback,
            )

            if found:
                results_list.append(found)
                before_count = len(unique_chunk_ids)
                for _, chunk in found:
                    chunk_id = chunk.get("chunk_id")
                    if chunk_id:
                        unique_chunk_ids.add(chunk_id)
                new_chunks = len(unique_chunk_ids) - before_count
                if new_chunks == 0:
                    no_gain_streak += 1
                else:
                    no_gain_streak = 0
            else:
                no_gain_streak += 1

            search_calls += 1

            elapsed_total = time.time() - loop_start
            if elapsed_total >= hybrid_time_budget_s:
                stop_search = True
                if progress_callback:
                    progress_callback(
                        f"[INFO] Orcamento de tempo da busca hibrida atingido ({elapsed_total:.1f}s)."
                    )
                break

            if (
                search_calls >= min_calls_before_early_stop
                and unique_chunk_ids
                and no_gain_streak >= early_stop_no_gain_patience
            ):
                stop_search = True
                if progress_callback:
                    progress_callback(
                        "[INFO] Busca encerrada por baixo ganho incremental."
                    )
                break

            if (
                search_calls >= min_calls_before_early_stop
                and len(unique_chunk_ids) >= early_stop_min_unique_chunks
                and no_gain_streak >= 1
            ):
                stop_search = True
                if progress_callback:
                    progress_callback(
                        "[INFO] Cobertura de chunks suficiente; encerrando buscas adicionais."
                    )
                break

    t_rrf = time.time()
    scores = defaultdict(float)
    chunk_map = {}
    k = 60

    for results in results_list:
        for rank, (_, chunk) in enumerate(results):
            chunk_id = chunk.get("chunk_id")
            chunk_map[chunk_id] = chunk
            scores[chunk_id] += 1 / (k + rank + 1)

    results = sorted(
        [(score, chunk_map[cid]) for cid, score in scores.items()],
        key=lambda x: x[0],
        reverse=True,
    )
    results = _apply_mining_domain_policy(results, mining_intent)

    results = mmr_select(
        results,
        top_k=config["top_k_retrieval"],
        lambda_param=0.7,
    )
    _emit_timing("RRF fusion", t_rrf, progress_callback=progress_callback)

    if not results:
        return "Nenhuma evidencia encontrada.", [], routing

    temporal_preamble = None
    temporal_mode = False

    t_temporal = time.time()
    if temporal_intent.get("enabled"):
        temporal_selected, temporal_preamble = _select_temporal_results(
            results=results,
            query=query,
            config=config,
            temporal_intent=temporal_intent,
            base_name=base_name,
            progress_callback=progress_callback,
        )
        if temporal_selected:
            reranked = temporal_selected
            temporal_mode = True
        else:
            reranked = _run_rerank_with_fallback(
                query=query,
                results=results,
                config=config,
                top_k=config["top_k_final"],
                progress_callback=progress_callback,
            )
            reranked = _ensure_mining_fallback_coverage(
                reranked,
                results,
                mining_intent,
                config["top_k_final"],
            )
    else:
        reranked = _run_rerank_with_fallback(
            query=query,
            results=results,
            config=config,
            top_k=config["top_k_final"],
            progress_callback=progress_callback,
        )
        reranked = _ensure_mining_fallback_coverage(
            reranked,
            results,
            mining_intent,
            config["top_k_final"],
        )
    _emit_timing("temporal selection", t_temporal, progress_callback=progress_callback)

    temporal_gate = None
    temporal_metadata_available = False
    temporal_gate_block = False
    if temporal_intent.get("enabled"):
        temporal_gate = _evaluate_temporal_gate(
            results=reranked,
            temporal_intent=temporal_intent,
        )
        temporal_metadata_available = bool(
            temporal_gate and int(temporal_gate.get("normative_count", 0)) > 0
        )
        temporal_gate_block = bool(
            temporal_metadata_available and not temporal_gate.get("has_active_normative")
        )
        if temporal_intent.get("enabled") and not temporal_metadata_available:
            if progress_callback:
                progress_callback(
                    "[INFO] Metadados normativos insuficientes; usando modo de resposta legado."
                )

    context = build_context(
        reranked,
        config["max_context_chars"],
        preamble=temporal_preamble,
        temporal_intent=(
            temporal_intent if temporal_mode and temporal_metadata_available else None
        ),
    )

    default_prompt = (DEFAULT_PROMPT_TEMPLATE or "").strip()
    custom_prompt_clean = (custom_prompt or "").strip()
    has_meaningful_custom_prompt = bool(
        custom_prompt_clean and custom_prompt_clean != default_prompt
    )
    today = date.today()
    prompt_vars = {
        "query": query,
        "context": context,
        "ano_referencia": temporal_intent.get("year"),
        "recorte_temporal": _reference_label_from_intent(temporal_intent),
        "data_corte_temporal": _reference_search_hint_from_intent(temporal_intent) or "nao informado",
        "nota_corte_temporal": temporal_intent.get("cutoff_notice") or "nao se aplica",
        "data_referencia_iso": today.isoformat(),
        "data_referencia_br": today.strftime("%d/%m/%Y"),
        "referencias_normativas_contexto": _build_reference_map(reranked),
        "metadados_contexto": _build_metadata_context(
            reranked,
            temporal_intent if temporal_mode and temporal_metadata_available else None,
        ),
    }

    if temporal_gate_block:
        answer = (
            "Nao foi possivel emitir conclusao normativa valida para o periodo consultado, "
            "pois nao houve evidencia suficiente de norma vigente no recorte temporal informado. "
            "Foram encontradas referencias historicas/revogadas, mas sem base segura para "
            "conclusao normativa aplicavel no periodo."
        )
    elif temporal_mode and temporal_metadata_available:
        prompt = _format_prompt_template(TEMPORAL_PROMPT_TEMPLATE, prompt_vars)
        if has_meaningful_custom_prompt:
            prompt += (
                "\n\n---\n\n"
                "Instrucoes adicionais do usuario (aplicar sem violar as regras "
                "temporais obrigatorias acima):\n"
                f"{_format_prompt_template(custom_prompt_clean, prompt_vars)}"
            )
    elif has_meaningful_custom_prompt:
        prompt = _format_prompt_template(custom_prompt_clean, prompt_vars)
    else:
        prompt = _format_prompt_template(DEFAULT_PROMPT_TEMPLATE, prompt_vars)

    if not temporal_gate_block:
        t_llm = time.time()
        answer_model = _answer_llm_fallback_models(llm_model)[0]
        answer_model_used, answer = _chat_completion_with_model_fallback(
            [
                {
                    "role": "system",
                    "content": "Responda em portugues tecnico estruturado, citando fontes do contexto.",
                },
                {"role": "user", "content": prompt},
            ],
            temperature=temperature,
            llm_model=llm_model,
            progress_callback=progress_callback,
            retry_context=context,
        )
        answer_label = answer_model_used
        if answer_model_used != answer_model:
            answer_label = f"{answer_model}->{answer_model_used}"
        _emit_timing(
            f"LLM [{answer_label}]",
            t_llm,
            progress_callback=progress_callback,
        )

    cutoff_notice = str(temporal_intent.get("cutoff_notice") or "").strip()
    if cutoff_notice:
        answer = f"Nota de corte temporal adotada: {cutoff_notice}\n\n{answer}"

    consistency_note = _reference_consistency_note(answer, context)
    if consistency_note:
        answer = (
            f"{answer}\n\n"
            f"Observacao de consistencia automatica: {consistency_note}"
        )

    evidence_block = build_evidence(reranked, query)
    final_output = (
        f"# Resposta\n\n{answer}\n\n"
        f"---\n\n"
        f"# Evidencia\n\n"
        f"{evidence_block}"
    )

    return final_output, reranked, routing


def ask_multi_base(
    query,
    base_names,
    temperature=0.2,
    llm_model=None,
    config_override=None,
    custom_prompt=None,
    allowed_docs_by_base=None,
    progress_callback=None,
):
    base_names = [str(base).strip() for base in (base_names or []) if str(base).strip()]
    if not base_names:
        return "Nenhuma base selecionada.", [], {"strategy": "multi_base", "bases": []}

    config = RAG_CONFIG.copy()
    if config_override:
        config.update(config_override)

    temporal_intent = detect_temporal_normative_intent(
        query,
        progress_callback=progress_callback,
    )
    query_expanded = expand_query(query)
    query_profile = _detect_legal_query_profile(query)
    mining_intent = _detect_mining_domain_intent(query)
    config = _auto_tune_config(query, config, temporal_intent)

    t0 = time.time()
    query_candidates = [query_expanded]
    query_candidates.extend(generate_query_variants(query_expanded))
    query_candidates.extend(decompose_query(query_expanded))
    query_candidates.extend(_build_profile_query_expansions(query_profile))
    if temporal_intent.get("enabled"):
        reference_year = temporal_intent.get("year")
        reference_search_hint = _reference_search_hint_from_intent(temporal_intent)
        query_candidates.extend(
            [
                f"{query_expanded} vigencia",
                f"{query_expanded} revogacao",
                f"{query_expanded} norma vigente",
            ]
        )
        if reference_search_hint:
            query_candidates.append(f"{query_expanded} vigente em {reference_search_hint}")
        if reference_year:
            query_candidates.extend(
                [
                    f"{query_expanded} vigencia {reference_year}",
                    f"{query_expanded} revogacao {reference_year}",
                    f"{query_expanded} norma vigente {reference_year}",
                ]
            )
        query_candidates.extend(temporal_intent.get("llm_search_queries") or [])
    if mining_intent.get("enabled"):
        query_candidates.extend(
            [
                f"{query_expanded} mineracao norma mineraria",
                f"{query_expanded} legislacao mineraria anm dnpm",
            ]
        )
        if mining_intent.get("has_mining_terms"):
            query_candidates.extend(
                [
                    f"{query_expanded} codigo de mineracao",
                    f"{query_expanded} titulo minerario lavra pesquisa mineral",
                ]
            )

    queries = _dedupe_keep_order(query_candidates)[: config["max_query_variants"]]
    if not queries:
        queries = [query_expanded]

    profiles = _build_hybrid_profiles(
        config["alpha_semantic"],
        config["beta_lexical"],
        config["hybrid_profiles_per_query"],
    )
    _emit_timing(
        f"query generation [{get_runtime_query_generation_model()}]",
        t0,
        progress_callback=progress_callback,
    )

    results_list = []
    max_hybrid_searches = int(config["max_hybrid_searches"])
    hybrid_time_budget_s = float(config.get("hybrid_time_budget_s", 45.0))
    per_base_call_budget = max(1, max_hybrid_searches)

    for base_idx, base_name in enumerate(base_names, start=1):
        base_start = time.time()
        if progress_callback:
            progress_callback(f"[BASE {base_idx}/{len(base_names)}: {base_name}] inicio")

        if isinstance(allowed_docs_by_base, dict):
            base_allowed_docs = allowed_docs_by_base.get(base_name)
        else:
            base_allowed_docs = allowed_docs_by_base

        routing = route_query(
            query,
            base_name=base_name,
            allowed_docs=base_allowed_docs,
            temporal_intent=temporal_intent,
        )
        filter_doc = routing.get("filter_doc")
        search_calls = 0
        loop_start = time.time()

        for q_idx, q in enumerate(queries, start=1):
            if search_calls >= per_base_call_budget:
                break
            for p_idx, (h_alpha, h_beta) in enumerate(profiles, start=1):
                if search_calls >= per_base_call_budget:
                    break
                t_search = time.time()
                try:
                    found = hybrid_search(
                        q,
                        top_k=config["top_k_retrieval"],
                        filter_doc=filter_doc,
                        base_name=base_name,
                        allowed_docs=base_allowed_docs,
                        alpha=h_alpha,
                        beta=h_beta,
                    )
                except Exception as exc:
                    found = []
                    if progress_callback:
                        progress_callback(
                            f"[BASE {base_idx}/{len(base_names)}: {base_name}] "
                            f"[WARN] Falha na busca hibrida: {exc}"
                        )

                if found:
                    tagged = []
                    for score, chunk in found:
                        chunk_copy = dict(chunk)
                        chunk_copy["base_rag"] = base_name
                        tagged.append((score, chunk_copy))
                    results_list.append(tagged)

                _emit_timing(
                    f"hybrid_search base={base_name} q{q_idx}/{len(queries)} p{p_idx}/{len(profiles)}",
                    t_search,
                    progress_callback=progress_callback,
                )
                search_calls += 1

                if time.time() - loop_start >= hybrid_time_budget_s:
                    break

        _emit_timing(
            f"base search total [{base_name}]",
            base_start,
            progress_callback=progress_callback,
        )

    t_rrf = time.time()
    scores = defaultdict(float)
    chunk_map = {}
    k = 60

    for results in results_list:
        for rank, (_, chunk) in enumerate(results):
            chunk_id = f"{chunk.get('base_rag')}::{chunk.get('chunk_id')}"
            chunk_map[chunk_id] = chunk
            scores[chunk_id] += 1 / (k + rank + 1)

    results = sorted(
        [(score, chunk_map[cid]) for cid, score in scores.items()],
        key=lambda x: x[0],
        reverse=True,
    )
    results = _apply_mining_domain_policy(results, mining_intent)
    results = mmr_select(
        results,
        top_k=config["top_k_retrieval"],
        lambda_param=0.7,
    )
    _emit_timing("RRF fusion multibase", t_rrf, progress_callback=progress_callback)

    if not results:
        return "Nenhuma evidencia encontrada.", [], {
            "strategy": "multi_base_unified",
            "bases": base_names,
            "mining_domain_intent": mining_intent,
        }

    temporal_preamble = None
    temporal_mode = False
    t_temporal = time.time()
    if temporal_intent.get("enabled"):
        temporal_selected, temporal_preamble = _select_temporal_results(
            results=results,
            query=query,
            config=config,
            temporal_intent=temporal_intent,
            base_name=None,
            progress_callback=progress_callback,
        )
        if temporal_selected:
            reranked = temporal_selected
            temporal_mode = True
        else:
            reranked = _run_rerank_with_fallback(
                query=query,
                results=results,
                config=config,
                top_k=config["top_k_final"],
                progress_callback=progress_callback,
            )
            reranked = _ensure_mining_fallback_coverage(
                reranked,
                results,
                mining_intent,
                config["top_k_final"],
            )
    else:
        reranked = _run_rerank_with_fallback(
            query=query,
            results=results,
            config=config,
            top_k=config["top_k_final"],
            progress_callback=progress_callback,
        )
        reranked = _ensure_mining_fallback_coverage(
            reranked,
            results,
            mining_intent,
            config["top_k_final"],
        )
    _emit_timing("temporal selection", t_temporal, progress_callback=progress_callback)

    temporal_gate = None
    temporal_metadata_available = False
    temporal_gate_block = False
    if temporal_intent.get("enabled"):
        temporal_gate = _evaluate_temporal_gate(
            results=reranked,
            temporal_intent=temporal_intent,
        )
        temporal_metadata_available = bool(
            temporal_gate and int(temporal_gate.get("normative_count", 0)) > 0
        )
        temporal_gate_block = bool(
            temporal_metadata_available and not temporal_gate.get("has_active_normative")
        )

    context = build_context(
        reranked,
        config["max_context_chars"],
        preamble=temporal_preamble,
        temporal_intent=(
            temporal_intent if temporal_mode and temporal_metadata_available else None
        ),
    )

    default_prompt = (DEFAULT_PROMPT_TEMPLATE or "").strip()
    custom_prompt_clean = (custom_prompt or "").strip()
    has_meaningful_custom_prompt = bool(
        custom_prompt_clean and custom_prompt_clean != default_prompt
    )
    today = date.today()
    prompt_vars = {
        "query": query,
        "context": context,
        "ano_referencia": temporal_intent.get("year"),
        "recorte_temporal": _reference_label_from_intent(temporal_intent),
        "data_corte_temporal": _reference_search_hint_from_intent(temporal_intent) or "nao informado",
        "nota_corte_temporal": temporal_intent.get("cutoff_notice") or "nao se aplica",
        "data_referencia_iso": today.isoformat(),
        "data_referencia_br": today.strftime("%d/%m/%Y"),
        "referencias_normativas_contexto": _build_reference_map(reranked),
        "metadados_contexto": _build_metadata_context(
            reranked,
            temporal_intent if temporal_mode and temporal_metadata_available else None,
        ),
    }

    if temporal_gate_block:
        answer = (
            "Nao foi possivel emitir conclusao normativa valida para o periodo consultado, "
            "pois nao houve evidencia suficiente de norma vigente no recorte temporal informado. "
            "Foram encontradas referencias historicas/revogadas, mas sem base segura para "
            "conclusao normativa aplicavel no periodo."
        )
    elif temporal_mode and temporal_metadata_available:
        prompt = _format_prompt_template(TEMPORAL_PROMPT_TEMPLATE, prompt_vars)
        if has_meaningful_custom_prompt:
            prompt += (
                "\n\n---\n\n"
                "Instrucoes adicionais do usuario (aplicar sem violar as regras "
                "temporais obrigatorias acima):\n"
                f"{_format_prompt_template(custom_prompt_clean, prompt_vars)}"
            )
    elif has_meaningful_custom_prompt:
        prompt = _format_prompt_template(custom_prompt_clean, prompt_vars)
    else:
        prompt = _format_prompt_template(DEFAULT_PROMPT_TEMPLATE, prompt_vars)

    if not temporal_gate_block:
        t_llm = time.time()
        answer_model = _answer_llm_fallback_models(llm_model)[0]
        answer_model_used, answer = _chat_completion_with_model_fallback(
            [
                {
                    "role": "system",
                    "content": (
                        "Responda em portugues tecnico estruturado, citando fontes do contexto. "
                        "Quando houver metadado base_rag, cite tambem a base de origem."
                    ),
                },
                {"role": "user", "content": prompt},
            ],
            temperature=temperature,
            llm_model=llm_model,
            progress_callback=progress_callback,
            retry_context=context,
        )
        answer_label = answer_model_used
        if answer_model_used != answer_model:
            answer_label = f"{answer_model}->{answer_model_used}"
        _emit_timing(
            f"LLM [{answer_label}]",
            t_llm,
            progress_callback=progress_callback,
        )

    cutoff_notice = str(temporal_intent.get("cutoff_notice") or "").strip()
    if cutoff_notice:
        answer = f"Nota de corte temporal adotada: {cutoff_notice}\n\n{answer}"

    consistency_note = _reference_consistency_note(answer, context)
    if consistency_note:
        answer = (
            f"{answer}\n\n"
            f"Observacao de consistencia automatica: {consistency_note}"
        )

    evidence_block = build_evidence(reranked, query)
    final_output = (
        f"# Resposta\n\n{answer}\n\n"
        f"---\n\n"
        f"# Evidencia\n\n"
        f"{evidence_block}"
    )

    return final_output, reranked, {
        "strategy": "multi_base_unified",
        "bases": base_names,
        "mining_domain_intent": mining_intent,
    }
