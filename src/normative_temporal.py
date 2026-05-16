import json
import os
import re
import time
import unicodedata
from calendar import monthrange
from datetime import date
from typing import Any, Dict, Optional

from src.lia_client import chat_completion, get_runtime_llm_model


NORMATIVE_KEYWORDS = (
    "norma",
    "normativo",
    "normativa",
    "regra",
    "regras",
    "artigo",
    "art.",
    "dispositivo",
    "vigente",
    "vigencia",
    "revogada",
    "revogado",
    "revogadas",
    "revogacao",
    "portaria",
    "resolucao",
    "instrucao normativa",
    "lei",
    "decreto",
    "ato",
)

TODAY_MARKERS = (
    "hoje",
    "atualmente",
    "atual",
    "neste momento",
    "nesta data",
)

APPLICABILITY_MARKERS = (
    "regra",
    "regras",
    "dispositivo",
    "dispositivos",
    "artigo",
    "art.",
    "se aplica",
    "aplicavel",
    "aplicaveis",
    "aplica",
    "aplicacao",
    "sancao",
    "sancoes",
    "penalidade",
    "penalidades",
    "normativo aplicado",
    "qual normativo",
    "em vigor",
    "vigente",
    "vigentes",
)

TEMPORAL_MARKERS = (
    "em ",
    "no ano",
    "na data",
    "na epoca",
    "a epoca",
    "entao",
    "vigente em",
    "vigencia em",
    "em vigor em",
    "durante",
    "quando",
    "periodo",
)

MONTH_NAME_TO_NUMBER = {
    "janeiro": 1,
    "fevereiro": 2,
    "marco": 3,
    "abril": 4,
    "maio": 5,
    "junho": 6,
    "julho": 7,
    "agosto": 8,
    "setembro": 9,
    "outubro": 10,
    "novembro": 11,
    "dezembro": 12,
}

ISO_DATE_RE = re.compile(r"\b((?:19|20)\d{2})-(\d{2})-(\d{2})\b")
BR_DATE_RE = re.compile(r"\b([0-3]?\d)[/.-]([01]?\d)[/.-](\d{2}|(?:19|20)\d{2})\b")
TEXTUAL_DATE_RE = re.compile(
    r"\b([0-3]?\d)\s+de\s+"
    r"(janeiro|fevereiro|marco|abril|maio|junho|julho|agosto|setembro|outubro|novembro|dezembro)"
    r"\s+de\s+(\d{2}|(?:19|20)\d{2})\b"
)
MONTH_YEAR_TEXT_RE = re.compile(
    r"\b(?:em|no|na|durante|desde)?\s*"
    r"(janeiro|fevereiro|marco|abril|maio|junho|julho|agosto|setembro|outubro|novembro|dezembro)"
    r"\s+de\s+(\d{2}|(?:19|20)\d{2})\b"
)
MONTH_YEAR_NUMERIC_RE = re.compile(r"\b(0?[1-9]|1[0-2])\s*/\s*(\d{2}|(?:19|20)\d{2})\b")
YEAR_RE = re.compile(r"\b(19\d{2}|20\d{2})\b")

TEMPORAL_YEAR_PATTERNS = (
    re.compile(
        r"\b(?:em|no|na|durante|desde|ate|até|vigente em|vigencia em|em vigor em|no ano de|na epoca de|a epoca de)\s+"
        r"(19\d{2}|20\d{2})\b"
    ),
    re.compile(r"^(19\d{2}|20\d{2})(?=\b|[\s,.:;])"),
    re.compile(
        r"\b(19\d{2}|20\d{2})\b(?=,?\s+(?:qual|quais|como|era|eram|estava|estavam|vigorava|vigia|vigente))"
    ),
)


def _strip_accents(text: str) -> str:
    normalized = unicodedata.normalize("NFKD", text or "")
    return "".join(ch for ch in normalized if not unicodedata.combining(ch))


def normalize_text(text: str) -> str:
    text = _strip_accents((text or "").lower())
    text = re.sub(r"\s+", " ", text).strip()
    return text


def _safe_date(year: int, month: int, day: int) -> Optional[date]:
    try:
        return date(int(year), int(month), int(day))
    except (TypeError, ValueError):
        return None


def _last_day_of_month(year: int, month: int) -> int:
    return monthrange(int(year), int(month))[1]


def _normalize_year_token(value: Any) -> Optional[int]:
    text = str(value or "").strip()
    if not re.fullmatch(r"\d{2}|\d{4}", text):
        return None

    year = int(text)
    if len(text) == 2:
        if year <= 49:
            year += 2000
        else:
            year += 1900

    if 1900 <= year <= 2100:
        return year
    return None


def _build_cutoff_notice(
    precision: str,
    start: Optional[date],
    original_label: Optional[str],
) -> Optional[str]:
    if not start:
        return None

    if precision == "year":
        base_label = str(original_label or start.year)
        return (
            f"A consulta informou apenas o ano ({base_label}); "
            f"foi adotado o corte temporal em {start.strftime('%d/%m/%Y')}."
        )

    if precision == "month":
        base_label = str(original_label or f"{start.month:02d}/{start.year}")
        return (
            f"A consulta informou apenas mes/ano ({base_label}); "
            f"foi adotado o corte temporal em {start.strftime('%d/%m/%Y')}."
        )

    return None


def _build_reference_label(
    precision: str,
    start: Optional[date],
    end: Optional[date],
    label: Optional[str],
) -> str:
    base_label = str(label or format_reference_period(start, end)).strip()
    cutoff_notice = _build_cutoff_notice(precision, start, label)
    if cutoff_notice:
        return f"{base_label} [corte adotado: {start.strftime('%d/%m/%Y')}]"
    return base_label


def _build_reference_window(
    start: Optional[date],
    end: Optional[date],
    precision: str,
    label: Optional[str],
    source: str,
) -> Dict[str, Any]:
    normalized_start = start
    normalized_end = end or start

    if start and precision == "year":
        normalized_start = _safe_date(start.year, 1, 1)
        normalized_end = normalized_start
    elif start and precision == "month":
        normalized_start = _safe_date(start.year, start.month, 1)
        normalized_end = normalized_start
    elif start and precision in {"day", "current"}:
        normalized_end = start

    return {
        "reference_start": normalized_start,
        "reference_end": normalized_end,
        "reference_precision": precision,
        "reference_label": _build_reference_label(
            precision,
            normalized_start,
            normalized_end,
            label,
        ),
        "cutoff_notice": _build_cutoff_notice(precision, normalized_start, label),
        "reference_source": source,
    }


def _extract_exact_date(raw_query: str, normalized_query: str) -> Optional[Dict[str, Any]]:
    match = ISO_DATE_RE.search(raw_query)
    if match:
        parsed = _safe_date(int(match.group(1)), int(match.group(2)), int(match.group(3)))
        if parsed:
            return _build_reference_window(
                start=parsed,
                end=parsed,
                precision="day",
                label=parsed.strftime("%d/%m/%Y"),
                source="heuristic_iso_date",
            )

    match = BR_DATE_RE.search(raw_query)
    if match:
        parsed_year = _normalize_year_token(match.group(3))
        parsed = _safe_date(int(parsed_year or 0), int(match.group(2)), int(match.group(1)))
        if parsed:
            return _build_reference_window(
                start=parsed,
                end=parsed,
                precision="day",
                label=parsed.strftime("%d/%m/%Y"),
                source="heuristic_br_date",
            )

    match = TEXTUAL_DATE_RE.search(normalized_query)
    if match:
        month = MONTH_NAME_TO_NUMBER.get(match.group(2))
        parsed_year = _normalize_year_token(match.group(3))
        parsed = _safe_date(int(parsed_year or 0), int(month or 0), int(match.group(1)))
        if parsed:
            return _build_reference_window(
                start=parsed,
                end=parsed,
                precision="day",
                label=parsed.strftime("%d/%m/%Y"),
                source="heuristic_textual_date",
            )

    return None


def _extract_month_window(normalized_query: str) -> Optional[Dict[str, Any]]:
    match = MONTH_YEAR_TEXT_RE.search(normalized_query)
    if match:
        month = MONTH_NAME_TO_NUMBER.get(match.group(1))
        year = _normalize_year_token(match.group(2))
        if month:
            start = _safe_date(year, month, 1)
            if start:
                return _build_reference_window(
                    start=start,
                    end=start,
                    precision="month",
                    label=f"{month:02d}/{year:04d}",
                    source="heuristic_textual_month",
                )

    match = MONTH_YEAR_NUMERIC_RE.search(normalized_query)
    if match:
        month = int(match.group(1))
        year = _normalize_year_token(match.group(2))
        start = _safe_date(year, month, 1)
        if start:
            return _build_reference_window(
                start=start,
                end=start,
                precision="month",
                label=f"{month:02d}/{year:04d}",
                source="heuristic_numeric_month",
            )

    return None


def _is_year_bound_to_norm_reference(normalized_query: str, match: re.Match) -> bool:
    year_text = match.group(1)
    start = max(0, match.start() - 20)
    end = min(len(normalized_query), match.end() + 8)
    neighborhood = normalized_query[start:end]

    if re.search(r"\d+\s*/\s*" + re.escape(year_text), neighborhood):
        prefix = normalized_query[max(0, start - 40):end]
        if not any(marker in prefix for marker in (" em ", " no ano ", " na epoca ", " vigente em ")):
            return True

    return False


def parse_year_from_query(query: str) -> Optional[int]:
    q = normalize_text(query)

    for pattern in TEMPORAL_YEAR_PATTERNS:
        match = pattern.search(q)
        if match:
            year = int(match.group(1))
            if 1900 <= year <= 2100:
                return year

    matches = list(YEAR_RE.finditer(q))
    if not matches:
        return None

    candidates = []
    for match in matches:
        if _is_year_bound_to_norm_reference(q, match):
            continue
        year = int(match.group(1))
        if 1900 <= year <= 2100:
            candidates.append(year)

    if len(candidates) == 1:
        return candidates[0]

    return None


def _extract_year_window(query: str, normalized_query: str) -> Optional[Dict[str, Any]]:
    year = parse_year_from_query(query)
    if not year:
        return None

    start = _safe_date(year, 1, 1)
    if not start:
        return None

    source = "heuristic_year"
    if normalized_query.startswith(str(year)):
        source = "heuristic_leading_year"

    return _build_reference_window(
        start=start,
        end=start,
        precision="year",
        label=str(year),
        source=source,
    )


def _extract_temporal_reference_heuristic(
    query: str,
    normalized_query: str,
    has_today_marker: bool,
    temporal_legal_context: bool,
) -> Optional[Dict[str, Any]]:
    exact = _extract_exact_date(query, normalized_query)
    if exact:
        return exact

    month_window = _extract_month_window(normalized_query)
    if month_window:
        return month_window

    year_window = _extract_year_window(query, normalized_query)
    if year_window:
        return year_window

    if has_today_marker and temporal_legal_context:
        today = date.today()
        return _build_reference_window(
            start=today,
            end=today,
            precision="current",
            label=f"hoje ({today.strftime('%d/%m/%Y')})",
            source="heuristic_today",
        )

    return None


def _extract_json_object(text: str) -> Optional[Dict[str, Any]]:
    raw = str(text or "").strip()
    if not raw:
        return None

    if raw.startswith("```"):
        raw = re.sub(r"^```(?:json)?\s*", "", raw)
        raw = re.sub(r"\s*```$", "", raw)

    first = raw.find("{")
    last = raw.rfind("}")
    if first == -1 or last == -1 or last <= first:
        return None

    candidate = raw[first:last + 1]
    try:
        payload = json.loads(candidate)
    except json.JSONDecodeError:
        return None

    return payload if isinstance(payload, dict) else None


def _llm_temporal_parser_enabled() -> bool:
    configured = os.getenv("RAG_ENABLE_TEMPORAL_QUERY_LLM")
    if configured is None:
        return False
    return str(configured).strip() == "1"


def _llm_temporal_model() -> str:
    return str(os.getenv("RAG_TEMPORAL_QUERY_MODEL", "o3-mini")).strip() or "o3-mini"


def _parse_llm_reference_date(value: Any) -> Optional[date]:
    if not value:
        return None
    text = str(value).strip()
    try:
        return date.fromisoformat(text[:10])
    except ValueError:
        return None


def _normalize_llm_search_queries(payload: Dict[str, Any]) -> list[str]:
    values = payload.get("search_queries")
    if not isinstance(values, list):
        return []

    seen = set()
    cleaned = []
    for item in values:
        text = re.sub(r"\s+", " ", str(item or "")).strip()
        key = text.lower()
        if not text or key in seen:
            continue
        seen.add(key)
        cleaned.append(text)
        if len(cleaned) >= 3:
            break

    return cleaned


def _emit_temporal_progress_timing(label: str, start_ts: float, progress_callback=None):
    elapsed = round(time.time() - start_ts, 2)
    message = f"[TIME] {label}: {elapsed} s"
    print(message)
    if progress_callback:
        try:
            progress_callback(message)
        except Exception:
            pass


def _try_llm_temporal_parse(query: str, progress_callback=None) -> Optional[Dict[str, Any]]:
    prompt = (
        "Extraia o recorte temporal de uma pergunta juridica e devolva JSON puro. "
        "Nao use markdown. Considere que anos em identificadores de norma como "
        "\"Portaria 123/2019\" nao sao automaticamente a data da consulta.\n\n"
        "Campos obrigatorios:\n"
        "{\n"
        '  "is_temporal": true|false,\n'
        '  "reference_precision": "day"|"month"|"year"|"current"|"unknown",\n'
        '  "reference_start": "YYYY-MM-DD"|null,\n'
        '  "reference_end": "YYYY-MM-DD"|null,\n'
        '  "reference_label": "texto curto"|null,\n'
        '  "search_queries": ["ate 3 consultas curtas para retrieval temporal"]\n'
        "}\n\n"
        "Pergunta:\n"
        f"{query}"
    )

    llm_model = _llm_temporal_model()
    t_llm = time.time()
    try:
        response = chat_completion(
            [
                {
                    "role": "system",
                    "content": (
                        "Voce extrai recorte temporal de perguntas juridicas e responde "
                        "somente com JSON valido."
                    ),
                },
                {"role": "user", "content": prompt},
            ],
            temperature=0.0,
            llm_model=llm_model,
        )
    except Exception:
        return None
    finally:
        _emit_temporal_progress_timing(
            f"temporal query parse [{get_runtime_llm_model(llm_model)}]",
            t_llm,
            progress_callback=progress_callback,
        )

    payload = _extract_json_object(response)
    if not payload:
        return None

    if not bool(payload.get("is_temporal")):
        payload["search_queries"] = _normalize_llm_search_queries(payload)
        return payload

    start = _parse_llm_reference_date(payload.get("reference_start"))
    end = _parse_llm_reference_date(payload.get("reference_end")) or start
    if not start or not end:
        return None

    precision = str(payload.get("reference_precision") or "unknown").strip().lower()
    if precision not in {"day", "month", "year", "current", "unknown"}:
        precision = "unknown"

    payload["reference_start"] = start
    payload["reference_end"] = end
    payload["reference_precision"] = precision
    payload["search_queries"] = _normalize_llm_search_queries(payload)
    return payload


def _precision_rank(value: Optional[str]) -> int:
    ranks = {
        "unknown": 0,
        "year": 1,
        "month": 2,
        "day": 3,
        "current": 3,
    }
    return ranks.get(str(value or "unknown").strip().lower(), 0)


def _llm_can_refine_heuristic(
    heuristic: Optional[Dict[str, Any]],
    llm_payload: Optional[Dict[str, Any]],
) -> bool:
    if not llm_payload or not bool(llm_payload.get("is_temporal")):
        return False
    if not heuristic:
        return True

    llm_start = llm_payload.get("reference_start")
    llm_end = llm_payload.get("reference_end")
    heuristic_start = heuristic.get("reference_start")
    heuristic_end = heuristic.get("reference_end")

    if not llm_start or not llm_end or not heuristic_start or not heuristic_end:
        return False

    if llm_start == heuristic_start and llm_end == heuristic_end:
        return True

    if (
        _precision_rank(llm_payload.get("reference_precision"))
        > _precision_rank(heuristic.get("reference_precision"))
        and heuristic_start <= llm_start <= heuristic_end
        and heuristic_start <= llm_end <= heuristic_end
    ):
        return True

    return False


def _has_explicit_temporal_reference(heuristic: Optional[Dict[str, Any]]) -> bool:
    if not heuristic:
        return False

    if not heuristic.get("reference_start"):
        return False

    return str(heuristic.get("reference_precision") or "").strip().lower() in {
        "day",
        "month",
        "year",
        "current",
    }


def detect_temporal_normative_intent(query: str, allow_llm: bool = True, progress_callback=None) -> Dict[str, Any]:
    q = normalize_text(query)
    has_normative_word = any(k in q for k in NORMATIVE_KEYWORDS)
    has_today_marker = any(marker in q for marker in TODAY_MARKERS)
    has_applicability_marker = any(marker in q for marker in APPLICABILITY_MARKERS)
    wants_revoked = any(k in q for k in ("revogad", "revoga", "revogacao", "revogada"))
    temporal_marker_present = any(marker in q for marker in TEMPORAL_MARKERS)
    temporal_legal_context = bool(has_normative_word or has_applicability_marker)

    heuristic = _extract_temporal_reference_heuristic(
        query=query,
        normalized_query=q,
        has_today_marker=has_today_marker,
        temporal_legal_context=temporal_legal_context,
    )
    explicit_temporal_reference = _has_explicit_temporal_reference(heuristic)
    llm_temporal_context = bool(temporal_legal_context or explicit_temporal_reference)

    llm_probe_candidate = bool(
        heuristic is not None
        or temporal_marker_present
        or has_today_marker
        or any(pattern.search(q) for pattern in TEMPORAL_YEAR_PATTERNS)
    )

    llm_payload = None
    llm_search_queries = []
    if (
        allow_llm
        and _llm_temporal_parser_enabled()
        and llm_temporal_context
        and llm_probe_candidate
    ):
        llm_payload = _try_llm_temporal_parse(
            query,
            progress_callback=progress_callback,
        )
        if llm_payload:
            llm_search_queries = list(llm_payload.get("search_queries") or [])

    chosen = heuristic
    analysis_source = "heuristic" if heuristic else "none"
    if _llm_can_refine_heuristic(heuristic, llm_payload):
        chosen = _build_reference_window(
            start=llm_payload.get("reference_start"),
            end=llm_payload.get("reference_end"),
            precision=llm_payload.get("reference_precision") or "unknown",
            label=llm_payload.get("reference_label"),
            source="llm_refined",
        )
        analysis_source = "heuristic+llm"
    elif not heuristic and llm_payload and bool(llm_payload.get("is_temporal")):
        chosen = _build_reference_window(
            start=llm_payload.get("reference_start"),
            end=llm_payload.get("reference_end"),
            precision=llm_payload.get("reference_precision") or "unknown",
            label=llm_payload.get("reference_label"),
            source="llm",
        )
        analysis_source = "llm"

    reference_start = chosen.get("reference_start") if chosen else None
    reference_end = chosen.get("reference_end") if chosen else None
    reference_precision = chosen.get("reference_precision") if chosen else "unknown"
    reference_label = chosen.get("reference_label") if chosen else None
    cutoff_notice = chosen.get("cutoff_notice") if chosen else None
    inferred_current_year = bool(chosen and chosen.get("reference_source") == "heuristic_today")
    year = reference_start.year if isinstance(reference_start, date) else None
    enabled = bool(llm_temporal_context and reference_start and reference_end)

    return {
        "enabled": enabled,
        "year": year,
        "reference_start": reference_start,
        "reference_end": reference_end,
        "reference_precision": reference_precision,
        "reference_label": reference_label,
        "cutoff_notice": cutoff_notice,
        "has_normative_word": has_normative_word,
        "has_today_marker": has_today_marker,
        "has_applicability_marker": has_applicability_marker,
        "temporal_marker_present": temporal_marker_present,
        "explicit_temporal_reference": explicit_temporal_reference,
        "inferred_current_year": inferred_current_year,
        "wants_revoked": wants_revoked or enabled,
        "analysis_source": analysis_source,
        "llm_search_queries": llm_search_queries,
    }


def _parse_iso_date(value: Any) -> Optional[date]:
    if not value or not isinstance(value, str):
        return None
    value = value.strip()
    if not value:
        return None
    try:
        return date.fromisoformat(value[:10])
    except ValueError:
        return None


def _parse_year_only(value: Any, end_of_year: bool) -> Optional[date]:
    if value is None:
        return None
    text = str(value).strip()
    if not re.fullmatch(r"(19\d{2}|20\d{2})", text):
        return None
    year = int(text)
    if end_of_year:
        return date(year, 12, 31)
    return date(year, 1, 1)


def _parse_int(value: Any) -> Optional[int]:
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    text = str(value).strip()
    if not text:
        return None
    if re.fullmatch(r"-?\d+", text):
        try:
            return int(text)
        except ValueError:
            return None
    return None


def _canonical_status(value: Any) -> str:
    raw = normalize_text(str(value or "desconhecido"))
    normalized = re.sub(r"[\s_\-]+", " ", raw).strip()

    if not normalized:
        return "desconhecido"

    if "parcial" in normalized and "revog" in normalized:
        return "parcialmente_revogado"
    if any(token in normalized for token in ("nao vigente", "nao vigencia", "revog")):
        return "revogado"
    if normalized in ("vigente", "em vigor"):
        return "vigente"
    if "alterad" in normalized:
        return "alterado"

    return normalized


def _infer_header_revocation_status(chunk: Dict[str, Any]) -> Optional[str]:
    text = str(chunk.get("text") or "")
    if not text.strip():
        return None

    raw_lines = [
        re.sub(r"\s+", " ", line).strip()
        for line in text.splitlines()
        if str(line).strip()
    ]
    header_lines = [normalize_text(line) for line in raw_lines[:8]]

    partial_line_pat = re.compile(
        r"\bparcial(?:mente)?\s+revogad[ao]s?\b.{0,120}\bpela\b",
        re.IGNORECASE,
    )
    revoked_line_pat = re.compile(
        r"\brevogad[ao]s?\b.{0,120}\bpela\b",
        re.IGNORECASE,
    )

    for line in header_lines:
        if "nao revogad" in line:
            continue
        if partial_line_pat.search(line):
            return "parcialmente_revogado"
        if revoked_line_pat.search(line):
            return "revogado"

    prefix = normalize_text(re.sub(r"\s+", " ", text[:900]))
    if "nao revogad" not in prefix:
        if partial_line_pat.search(prefix):
            return "parcialmente_revogado"
        if revoked_line_pat.search(prefix):
            return "revogado"

    return None


def _status(chunk: Dict[str, Any]) -> str:
    inferred_header_status = _infer_header_revocation_status(chunk)
    if inferred_header_status:
        return inferred_header_status

    status = _canonical_status(chunk.get("status_normativo"))
    if status in ("desconhecido", "indeterminado", "vigente", "alterado"):
        tipo_revogacao = normalize_text(str(chunk.get("tipo_revogacao") or ""))
        tem_revog_parcial = bool(chunk.get("tem_revogacao_parcial_dispositivo"))
        eventos_revog = _parse_int(chunk.get("quantidade_eventos_revogacao_por_extraidos")) or 0
        if tipo_revogacao == "parcial" or tem_revog_parcial or eventos_revog > 0:
            return "parcialmente_revogado"
    return status


def is_effectively_normative(chunk: Dict[str, Any]) -> bool:
    status = _status(chunk)
    return status not in ("nao normativo", "nao_normativo")


def _effective_start(chunk: Dict[str, Any]) -> Optional[date]:
    start = _parse_iso_date(chunk.get("data_inicio_vigencia"))
    if start:
        return start
    start_year = _parse_year_only(chunk.get("data_inicio_vigencia"), end_of_year=False)
    if start_year:
        return start_year
    return None


def _effective_end(chunk: Dict[str, Any]) -> Optional[date]:
    end = _parse_iso_date(chunk.get("data_fim_vigencia"))
    if end:
        return end
    end_year = _parse_year_only(chunk.get("data_fim_vigencia"), end_of_year=True)
    if end_year:
        return end_year

    revocation_date = _parse_iso_date(chunk.get("revogado_por_data"))
    if revocation_date:
        return revocation_date
    revocation_year = _parse_year_only(chunk.get("revogado_por_data"), end_of_year=True)
    if revocation_year:
        return revocation_year

    last_rev = _parse_iso_date(chunk.get("data_ultima_revogacao"))
    if last_rev:
        return last_rev

    last_rev_year = _parse_year_only(chunk.get("data_ultima_revogacao"), end_of_year=True)
    if last_rev_year:
        return last_rev_year

    rev_year = _parse_int(chunk.get("revogado_por_ano"))
    if rev_year and 1900 <= rev_year <= 2100:
        return date(rev_year, 12, 31)

    return None


def effective_period(chunk: Dict[str, Any]) -> tuple[Optional[date], Optional[date]]:
    return _effective_start(chunk), _effective_end(chunk)


def has_normative_metadata(chunk: Dict[str, Any]) -> bool:
    return any(
        chunk.get(field) not in (None, "", [])
        for field in (
            "tipo_norma",
            "numero_norma",
            "ano_norma",
            "status_normativo",
            "data_inicio_vigencia",
            "data_fim_vigencia",
            "vacatio_dias",
            "vacatio_legis_dias",
            "revogado_por",
            "revogado_por_data",
            "revoga",
            "tipo_chunk",
            "tem_dispositivo_revogacao",
            "tem_dispositivo_vigencia",
        )
    )


def _normalize_reference_bounds(
    reference_start: Optional[date],
    reference_end: Optional[date],
) -> tuple[Optional[date], Optional[date]]:
    if reference_start and not reference_end:
        reference_end = reference_start
    if reference_end and not reference_start:
        reference_start = reference_end
    return reference_start, reference_end


def is_active_in_reference_period(
    chunk: Dict[str, Any],
    reference_start: Optional[date],
    reference_end: Optional[date] = None,
) -> bool:
    reference_start, reference_end = _normalize_reference_bounds(reference_start, reference_end)
    if not reference_start or not reference_end:
        return False

    start, end = effective_period(chunk)
    status = _status(chunk)

    if status in ("nao normativo", "nao_normativo"):
        return False

    if start and end:
        return start <= reference_end and end >= reference_start

    if start and not end:
        if status == "revogado":
            return False
        return start <= reference_end

    if not start and end:
        return end >= reference_start

    if status in ("vigente", "alterado", "parcialmente_revogado"):
        return True
    if status == "revogado":
        return False
    return False


def is_active_in_year(chunk: Dict[str, Any], year: int) -> bool:
    if not year:
        return False
    return is_active_in_reference_period(
        chunk,
        reference_start=date(year, 1, 1),
        reference_end=date(year, 12, 31),
    )


def is_revoked_related(
    chunk: Dict[str, Any],
    year: Optional[int] = None,
    reference_start: Optional[date] = None,
    reference_end: Optional[date] = None,
) -> bool:
    status = _status(chunk)
    tipo_chunk = normalize_text(str(chunk.get("tipo_chunk") or ""))
    has_rev_flag = bool(chunk.get("tem_dispositivo_revogacao"))
    has_revogado_por = bool(chunk.get("revogado_por"))
    revoga_list = chunk.get("revoga") or []
    _, end = effective_period(chunk)

    reference_start, reference_end = _normalize_reference_bounds(reference_start, reference_end)
    if year and not reference_start and not reference_end:
        reference_start = date(year, 1, 1)
        reference_end = date(year, 12, 31)

    if reference_end and end and end > reference_end:
        pass

    if status in ("revogado", "parcialmente_revogado"):
        return True
    if tipo_chunk == "dispositivo_revogacao":
        return True
    if has_rev_flag or has_revogado_por:
        return True
    if isinstance(revoga_list, list) and len(revoga_list) > 0:
        return True
    return False


def status_in_reference_period(
    chunk: Dict[str, Any],
    reference_start: Optional[date],
    reference_end: Optional[date] = None,
) -> str:
    reference_start, reference_end = _normalize_reference_bounds(reference_start, reference_end)
    if not reference_start or not reference_end:
        return "periodo_nao_informado"

    start, end = effective_period(chunk)
    current_status = _status(chunk)
    active = is_active_in_reference_period(chunk, reference_start, reference_end)
    single_day = reference_start == reference_end

    if active:
        if current_status == "revogado":
            return (
                "vigente_na_data_e_revogada_posteriormente"
                if single_day
                else "vigente_no_periodo_e_revogada_posteriormente"
            )
        if current_status == "parcialmente_revogado":
            return (
                "vigente_na_data_com_revogacao_parcial"
                if single_day
                else "vigente_no_periodo_com_revogacao_parcial"
            )
        return "vigente_na_data" if single_day else "vigente_no_periodo"

    if start and start > reference_end:
        return "ainda_nao_vigente_no_periodo"

    if current_status == "revogado":
        if end and end < reference_start:
            return "revogada_antes_do_periodo"
        return "revogada_no_periodo_ou_sem_data_precisa"

    return "nao_identificado"


def status_in_reference_year(chunk: Dict[str, Any], year: Optional[int]) -> str:
    if not year:
        return "ano_nao_informado"
    return status_in_reference_period(
        chunk,
        reference_start=date(year, 1, 1),
        reference_end=date(year, 12, 31),
    )


def format_reference_period(
    reference_start: Optional[date],
    reference_end: Optional[date] = None,
) -> str:
    reference_start, reference_end = _normalize_reference_bounds(reference_start, reference_end)
    if not reference_start or not reference_end:
        return "nao informado"

    if reference_start == reference_end:
        return reference_start.strftime("%d/%m/%Y")

    if (
        reference_start.day == 1
        and reference_start.month == 1
        and reference_end.month == 12
        and reference_end.day == 31
        and reference_start.year == reference_end.year
    ):
        return str(reference_start.year)

    if (
        reference_start.day == 1
        and reference_start.year == reference_end.year
        and reference_start.month == reference_end.month
        and reference_end.day == _last_day_of_month(reference_end.year, reference_end.month)
    ):
        return f"{reference_start.month:02d}/{reference_start.year}"

    return (
        f"{reference_start.strftime('%d/%m/%Y')} "
        f"a {reference_end.strftime('%d/%m/%Y')}"
    )


def chunk_normative_title(chunk: Dict[str, Any]) -> str:
    tipo = chunk.get("tipo_norma")
    numero = chunk.get("numero_norma")
    ano = chunk.get("ano_norma")
    if tipo and numero and ano:
        return f"{tipo} n. {numero}/{ano}"
    if tipo and numero:
        return f"{tipo} n. {numero}"
    return str(chunk.get("doc") or chunk.get("doc_name") or "documento sem identificacao")
