import argparse
import csv
import calendar
import json
import re
import shutil
from collections import Counter
import unicodedata
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple


csv.field_size_limit(100_000_000)


DEFAULT_METADATA_FIELDS = [
    "doc_id",
    "doc_id_curto",
    "classe_documental",
    "tipo_documento",
    "tipo_norma",
    "numero_norma",
    "ano_norma",
    "data_inicio_vigencia",
    "data_fim_vigencia",
    "status_normativo",
    "revogado_por",
    "revogado_por_data",
    "revogado_por_data_inicio_vigencia",
    "revogado_por_doc_id",
    "data_primeira_revogacao",
    "data_ultima_revogacao",
    "data_ultima_revogacao_parcial",
    "tem_revogacao_parcial_dispositivo",
    "confianca_parse_revogacao",
    "quantidade_eventos_revogacao_por_extraidos",
    "data_primeira_alteracao",
    "data_ultima_alteracao",
    "tipo_revogacao",
    "vacatio_dias",
    "classificacao_confianca",
    "area_juridica_principal",
    "relacao_com_mineracao",
    "familia_normativa_mineraria",
    "papel_no_corpus_minerario",
    "aplicacao_mineraria",
    "usar_como_fundamento_principal",
    "confianca_classificacao_mineraria",
    "motivo_classificacao_mineraria",
    "source_sha1",
]


def _maybe_fix_mojibake(text: str) -> str:
    if not isinstance(text, str):
        return text
    if not any(marker in text for marker in ("Ã", "Â", "â€", "�")):
        return text
    try:
        return text.encode("latin-1").decode("utf-8")
    except Exception:
        return text


def _normalize_name(name: str) -> str:
    if not name:
        return ""
    name = _maybe_fix_mojibake(str(name)).strip().replace("\\", "/").lower()
    name = name.split("/")[-1]
    if name.endswith(".pdf"):
        name = name[:-4]
    name = re.sub(r"[^a-z0-9]+", "", name)
    return name


def _parse_scalar(value: str):
    if value is None:
        return None

    value = value.strip()
    if value == "":
        return None

    lower = value.lower()
    if lower == "true":
        return True
    if lower == "false":
        return False

    if re.fullmatch(r"-?\d+", value):
        try:
            return int(value)
        except ValueError:
            pass

    if re.fullmatch(r"-?\d+\.\d+", value):
        try:
            return float(value)
        except ValueError:
            pass

    return value


def _parse_int(value: Any) -> Optional[int]:
    if value in (None, "", []):
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    text = str(value).strip()
    if re.fullmatch(r"-?\d+", text):
        try:
            return int(text)
        except ValueError:
            return None
    return None


def _is_truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    text = str(value).strip().lower()
    return text in {"1", "true", "t", "sim", "s", "yes", "y"}


def _metadata_prefers_partial(meta: Optional[Dict[str, Any]]) -> bool:
    """
    Indica quando o metadado sugere revogacao parcial do normativo
    (sem extinguir integralmente sua vigencia).
    """
    if not meta:
        return False

    tipo_revogacao = _normalize_spaces(str(meta.get("tipo_revogacao") or "")).lower()
    status_normativo = _normalize_spaces(str(meta.get("status_normativo") or "")).lower()
    has_partial_flag = _is_truthy(meta.get("tem_revogacao_parcial_dispositivo"))

    if tipo_revogacao == "parcial":
        return True

    if has_partial_flag and status_normativo not in {"revogado"}:
        return True

    return False


_MONTHS_PT = {
    "janeiro": 1,
    "fevereiro": 2,
    "marco": 3,
    "março": 3,
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


def _normalize_spaces(text: str) -> str:
    return re.sub(r"\s+", " ", str(text or "")).strip()


def _strip_accents(text: Any) -> str:
    normalized = unicodedata.normalize("NFKD", str(text or ""))
    return "".join(ch for ch in normalized if not unicodedata.combining(ch))


def _strip_print_stamp_prefix(text: Any) -> str:
    value = _normalize_spaces(_maybe_fix_mojibake(str(text or "")))
    if not value:
        return ""
    return re.sub(
        r"^\s*\d{1,2}/\d{1,2}/(19\d{2}|20\d{2})\s+\d{1,2}:\d{2}(?::\d{2})?\s*",
        "",
        value,
        flags=re.IGNORECASE,
    ).strip()


def _extract_print_stamp_date(value: Any) -> Optional[str]:
    text = _normalize_spaces(_maybe_fix_mojibake(str(value or "")))
    if not text:
        return None
    m = re.match(
        r"^\s*(\d{1,2}/\d{1,2}/(?:(?:19|20)\d{2}|\d{2}))\s+\d{1,2}:\d{2}(?::\d{2})?",
        text,
        flags=re.IGNORECASE,
    )
    if not m:
        return None
    return _coerce_iso_date(m.group(1))


def _expand_two_digit_year(year_text: str) -> int:
    year = int(year_text)
    if year >= 100:
        return year
    return 1900 + year if year >= 50 else 2000 + year


def _parse_portuguese_quantity(text: Any) -> Optional[int]:
    if text in (None, "", []):
        return None

    normalized = _strip_accents(_normalize_spaces(_maybe_fix_mojibake(str(text)))).lower()
    if not normalized:
        return None

    if re.fullmatch(r"\d+", normalized):
        try:
            value = int(normalized)
            return value if value > 0 else None
        except ValueError:
            return None

    normalized = normalized.replace("-", " ")
    normalized = re.sub(r"\s+", " ", normalized).strip()

    simple_values = {
        "um": 1,
        "uma": 1,
        "primeiro": 1,
        "primeira": 1,
        "dois": 2,
        "duas": 2,
        "segundo": 2,
        "segunda": 2,
        "tres": 3,
        "terceiro": 3,
        "terceira": 3,
        "quatro": 4,
        "quarto": 4,
        "quarta": 4,
        "cinco": 5,
        "quinto": 5,
        "quinta": 5,
        "seis": 6,
        "sexto": 6,
        "sexta": 6,
        "sete": 7,
        "setimo": 7,
        "setima": 7,
        "oito": 8,
        "oitavo": 8,
        "oitava": 8,
        "nove": 9,
        "nono": 9,
        "nona": 9,
        "dez": 10,
        "decimo": 10,
        "decima": 10,
        "onze": 11,
        "onze": 11,
        "doze": 12,
        "treze": 13,
        "catorze": 14,
        "quatorze": 14,
        "quinze": 15,
        "dezesseis": 16,
        "dezessete": 17,
        "dezoito": 18,
        "dezenove": 19,
        "vinte": 20,
        "trinta": 30,
        "quarenta": 40,
        "cinquenta": 50,
        "sessenta": 60,
        "setenta": 70,
        "oitenta": 80,
        "noventa": 90,
        "cem": 100,
        "cento": 100,
        "duzentos": 200,
        "trezentos": 300,
        "quatrocentos": 400,
        "quinhentos": 500,
        "seiscentos": 600,
        "setecentos": 700,
        "oitocentos": 800,
        "novecentos": 900,
        "mil": 1000,
    }

    total = 0
    current = 0
    consumed = False
    for part in normalized.split():
        if part == "e":
            continue
        value = simple_values.get(part)
        if value is None:
            return None
        consumed = True
        if value == 100 and current == 0 and part in {"cem", "cento"}:
            current = 100
            continue
        if value >= 100 and part in {"duzentos", "trezentos", "quatrocentos", "quinhentos", "seiscentos", "setecentos", "oitocentos", "novecentos"}:
            current += value
            continue
        if value == 1000:
            if current == 0:
                current = 1000
            else:
                current *= 1000
            continue
        current += value

    if not consumed:
        return None

    total += current
    return total if total > 0 else None

def _normalize_tipo_norma(value: Any) -> str:
    text = _strip_accents(_maybe_fix_mojibake(str(value or ""))).lower()
    text = _normalize_spaces(text)
    return text


def _normalize_numero_norma(value: Any) -> str:
    text = _normalize_spaces(str(value or "")).lower()
    text = text.replace("nº", "").replace("n°", "").replace("no", "")
    text = re.sub(r"[^0-9/.-]+", "", text)
    text = text.strip("./-")
    return text


def _normalize_lookup_text(value: Any) -> str:
    return _normalize_name(_strip_accents(_maybe_fix_mojibake(str(value or ""))).lower())


def _same_normalized_source(meta: Dict[str, Any], doc_name: Any) -> bool:
    doc_key = _normalize_lookup_text(doc_name)
    if not doc_key:
        return False

    for field in ("source_file_name", "source_relative_path"):
        source_value = meta.get(field)
        if source_value and _normalize_lookup_text(source_value) == doc_key:
            return True
    return False


def _revoker_candidate_score(meta: Dict[str, Any], ref_lookup: str, tipo: str, numero: str, ano: Optional[int]) -> Tuple[int, str]:
    source_name = _normalize_lookup_text(meta.get("source_file_name") or meta.get("source_relative_path"))
    source_title = _normalize_lookup_text(meta.get("titulo_norma") or "")
    score = 0

    if ref_lookup and source_name == ref_lookup:
        score += 100
    elif ref_lookup and (ref_lookup in source_name or source_name in ref_lookup):
        score += 70

    if tipo and tipo in source_name:
        score += 20
    if tipo and tipo in source_title:
        score += 20

    if numero and numero in source_name:
        score += 15
    if numero and numero in source_title:
        score += 15

    if ano is not None:
        ano_text = str(ano)
        if ano_text in source_name:
            score += 10
        if ano_text in source_title:
            score += 10

    if meta.get("doc_id"):
        score += 1

    return score, source_name


def _parse_year(value: Any) -> Optional[int]:
    if value in (None, "", []):
        return None
    if isinstance(value, int):
        return value
    text = str(value)
    m = re.search(r"\b(19\d{2}|20\d{2})\b", text)
    if not m:
        return None
    return int(m.group(1))


def _extract_year_from_text(value: Any) -> Optional[int]:
    if value in (None, "", []):
        return None
    years = re.findall(r"\b(19\d{2}|20\d{2})\b", str(value))
    if not years:
        return None
    parsed = sorted(int(y) for y in years)
    return parsed[-1]


def _parse_pt_date_to_iso(text: str) -> Optional[str]:
    if not text:
        return None
    cleaned = _strip_accents(_normalize_spaces(_maybe_fix_mojibake(text))).lower()
    m = re.search(r"\b(\d{1,2})(?:\u00ba|o|\u00aa)?\s+de\s+([a-z]+)(?:\s+(?:de|e))?\s+((?:19|20)\d{2}|\d{2})\b", cleaned)
    if not m:
        return None
    day = int(m.group(1))
    month_name = m.group(2).lower()
    year = _expand_two_digit_year(m.group(3))
    month = _MONTHS_PT.get(month_name)
    if not month:
        return None
    try:
        return datetime(year, month, day).date().isoformat()
    except ValueError:
        return None


def _extract_dates_from_text(value: Any) -> List[str]:
    text = _strip_print_stamp_prefix(value)
    if not text:
        return []

    out: List[str] = []

    # 12 de maio de 2016
    for m in re.finditer(
        r"\b(\d{1,2})\s+de\s+([a-zA-Z????????????????]+)(?:\s+(?:de|e))?\s+((?:19|20)\d{2}|\d{2})\b",
        text,
        flags=re.IGNORECASE,
    ):
        day = int(m.group(1))
        month_name = _maybe_fix_mojibake(m.group(2)).lower()
        month_name = (
            month_name.replace("??", "c")
            .replace("??", "a")
            .replace("??", "o")
            .replace("??", "e")
        )
        month = _MONTHS_PT.get(month_name)
        if not month:
            continue
        year = _expand_two_digit_year(m.group(3))
        try:
            out.append(datetime(year, month, day).date().isoformat())
        except ValueError:
            pass

    # 12 maio de 2016 (sem o "de" apos o dia, comum em nomes de arquivo)
    for m in re.finditer(
        r"\b(\d{1,2})\s+([A-Za-z????????????????????????????????????????????????]+)(?:\s+(?:de|e))?\s+((?:19|20)\d{2}|\d{2})\b",
        text,
        flags=re.IGNORECASE,
    ):
        day = int(m.group(1))
        month_name = _maybe_fix_mojibake(m.group(2)).lower()
        month_name = (
            month_name.replace("??", "c")
            .replace("??", "a")
            .replace("??", "o")
            .replace("??", "e")
        )
        month = _MONTHS_PT.get(month_name)
        if not month:
            continue
        year = _expand_two_digit_year(m.group(3))
        try:
            out.append(datetime(year, month, day).date().isoformat())
        except ValueError:
            pass

    # 12/05/2016
    for m in re.finditer(r"\b(\d{1,2})/(\d{1,2})/((?:19|20)\d{2}|\d{2})\b", text):
        day = int(m.group(1))
        month = int(m.group(2))
        year = _expand_two_digit_year(m.group(3))
        try:
            out.append(datetime(year, month, day).date().isoformat())
        except ValueError:
            pass

    # remove duplicados mantendo ordem
    dedup: List[str] = []
    seen = set()
    for item in out:
        if item not in seen:
            seen.add(item)
            dedup.append(item)
    return dedup


def _extract_publication_dates_from_text(value: Any) -> List[str]:
    """
    Extrai datas associadas a publicacao real do ato, priorizando linhas que
    mencionam DOU, Boletim Interno, publicacao interna ou termos equivalentes.
    Nao usa a data do titulo/cabecalho como evidencia de publicacao.
    """
    text = _normalize_spaces(_maybe_fix_mojibake(str(value or "")))
    if not text:
        return []

    def _strip_accents(value: str) -> str:
        normalized = unicodedata.normalize("NFKD", value)
        return "".join(ch for ch in normalized if not unicodedata.combining(ch))

    publication_markers = (
        "publicado",
        "publicada",
        "publicacao",
        "boletim interno",
        "boletim interno eletronico",
        "bie",
        "do.u",
        "d.o.u",
        "dou",
        "diario oficial",
        "diario oficial da uniao",
        "publicado internamente",
    )

    candidates: List[str] = []
    for line in str(value or "").splitlines():
        line_norm = _strip_accents(_strip_print_stamp_prefix(line).lower())
        if not line_norm:
            continue
        if not any(marker in line_norm for marker in publication_markers):
            continue
        candidates.extend(_extract_dates_from_text(line))

    dedup: List[str] = []
    seen = set()
    for item in candidates:
        if item not in seen:
            seen.add(item)
            dedup.append(item)
    return dedup


def _choose_date_by_year(candidates: List[str], year_hint: Optional[int]) -> Optional[str]:
    if not candidates:
        return None
    if not year_hint:
        return sorted(candidates)[0]
    ranked = sorted(
        candidates,
        key=lambda dt: (abs(int(dt[:4]) - int(year_hint)), dt),
    )
    return ranked[0]


def _infer_publication_date(chunk: Dict[str, Any], year_hint: Optional[int]) -> Optional[str]:
    """
    Inferencia de data de publicacao seguindo a hierarquia:
    1) data explicitamente associada a publicacao interna, DOU ou BIE
    2) data do titulo/nome do documento
    3) data no inicio do texto
    4) data ao final do documento
    """
    sources = (
        _extract_publication_dates_from_text(chunk.get("text")),
        _extract_dates_from_text(chunk.get("titulo_norma")),
        _extract_dates_from_text(chunk.get("doc_name") or chunk.get("doc")),
        str(chunk.get("text") or "")[:1400],
        str(chunk.get("text") or "")[-1200:],
    )
    for source in sources:
        if isinstance(source, list):
            candidates = source
        else:
            candidates = _extract_dates_from_text(source)
        if not candidates:
            continue
        chosen = _choose_date_by_year(candidates, year_hint)
        if chosen:
            return chosen
        return candidates[0]
    return None


def _infer_specific_start_date(chunk: Dict[str, Any]) -> Optional[str]:
    doc_key = _normalize_lookup_text(chunk.get("doc_name") or chunk.get("doc"))
    text = _strip_accents(_normalize_spaces(_maybe_fix_mojibake(str(chunk.get("text") or "")))).lower()

    if "constituicaofederal" in doc_key and "promulgacao" in text:
        if "primeiro dia do quinto mes" in text or "quinto mes seguinte" in text:
            return "1989-03-01"

    return None


def _infer_start_date(
    chunk: Dict[str, Any],
    year_hint: Optional[int],
    publication_base: Optional[str] = None,
) -> Optional[str]:
    """
    Regra de prioridade para inicio de vigencia:
    1) Dispositivo expresso de vigencia no texto
    2) Vacatio legis calculada sobre a data de publicacao
    3) Data de publicacao da norma
    4) Data ao final do documento
    """
    relative_start = _extract_relative_start_date(chunk.get("text"), publication_base)
    if relative_start:
        return relative_start

    specific_start = _infer_specific_start_date(chunk)
    if specific_start:
        return specific_start

    explicit_candidates = []
    for source in (chunk.get("text"), chunk.get("titulo_norma")):
        explicit_candidates.extend(_extract_entry_into_force_dates(source))

    if explicit_candidates:
        chosen = _choose_date_by_year(explicit_candidates, year_hint)
        if chosen:
            return chosen
        return explicit_candidates[0]

    vacatio_days = _parse_int(chunk.get("vacatio_dias")) or 0
    if publication_base and vacatio_days > 0:
        try:
            return (
                datetime.fromisoformat(publication_base).date()
                + timedelta(days=vacatio_days)
            ).isoformat()
        except ValueError:
            pass

    if publication_base:
        return publication_base

    title_candidates = _extract_dates_from_text(chunk.get("titulo_norma"))
    if title_candidates:
        chosen = _choose_date_by_year(title_candidates, year_hint)
        if chosen:
            return chosen

    text_head_candidates = _extract_dates_from_text(str(chunk.get("text") or "")[:1400])
    if text_head_candidates:
        chosen = _choose_date_by_year(text_head_candidates, year_hint)
        if chosen:
            return chosen

    filename_candidates = _extract_dates_from_text(chunk.get("doc_name") or chunk.get("doc"))
    if filename_candidates:
        chosen = _choose_date_by_year(filename_candidates, year_hint)
        if chosen:
            return chosen

    tail_candidates = _extract_dates_from_text(str(chunk.get("text") or "")[-1200:])
    if tail_candidates:
        chosen = _choose_date_by_year(tail_candidates, year_hint)
        if chosen:
            return chosen

    return None


def _extract_entry_into_force_dates(value: Any) -> List[str]:
    """
    Extrai datas explicitas de vigencia em frases como:
    - "entra em vigor em 22 de abril de 2027"
    - "entra em vigor em 22/04/2027"
    - "entra em vigor e produzira efeitos a partir de 1o de julho de 2021"
    """
    text = _strip_accents(_normalize_spaces(_maybe_fix_mojibake(str(value or "")))).lower()
    if not text:
        return []

    trigger_patterns = (
        r"entra\s+em\s+vigor",
        r"entrara\s+em\s+vigor",
        r"passa\s+a\s+vigorar",
        r"passara\s+a\s+vigorar",
        r"produz(?:em|indo|irao|ira)?\s+efeitos?",
        r"produzindo\s+efeitos?",
    )
    date_patterns = (
        re.compile(r"\b\d{1,2}\D{0,3}\s+de\s+[a-z]+\s+(?:de|e)\s+(?:19\d{2}|20\d{2})\b", re.IGNORECASE),
        re.compile(r"\b\d{1,2}/\d{1,2}/(?:19\d{2}|20\d{2})\b", re.IGNORECASE),
    )

    for trigger in trigger_patterns:
        for match in re.finditer(trigger, text, flags=re.IGNORECASE):
            window = text[match.start() : min(len(text), match.end() + 240)]
            if "na data de sua publicacao" in window or "sua publicacao" in window:
                # Se a norma diz apenas que entra em vigor na publicacao,
                # nao ha vacatio legis para inferir.
                continue

            tail = text[match.end() : min(len(text), match.end() + 240)]
            sentence = re.split(r"[.\n]", tail, maxsplit=1)[0]
            for date_pattern in date_patterns:
                for m in date_pattern.finditer(sentence):
                    dt = _parse_pt_date_to_iso(m.group(0)) or _coerce_iso_date(m.group(0))
                    if dt:
                        return [dt]

    return []

def _choose_nearest_date(candidates: List[str], reference_iso: str) -> Optional[str]:
    if not candidates or not reference_iso:
        return None
    ref = datetime.fromisoformat(reference_iso).date()
    ranked = sorted(
        candidates,
        key=lambda dt: (abs((datetime.fromisoformat(dt).date() - ref).days), dt),
    )
    return ranked[0]


def _quantity_token_to_int(token: Optional[str]) -> Optional[int]:
    return _parse_portuguese_quantity(token)


def _add_months(base: datetime.date, months: int) -> datetime.date:
    if months <= 0:
        return base

    month_index = base.month - 1 + months
    year = base.year + (month_index // 12)
    month = (month_index % 12) + 1
    last_day = calendar.monthrange(year, month)[1]
    day = min(base.day, last_day)
    return datetime(year, month, day).date()


def _add_years(base: datetime.date, years: int) -> datetime.date:
    if years <= 0:
        return base

    year = base.year + years
    last_day = calendar.monthrange(year, base.month)[1]
    day = min(base.day, last_day)
    return datetime(year, base.month, day).date()


def _first_business_day_of_month(base: datetime.date, months_ahead: int) -> datetime.date:
    if months_ahead <= 0:
        return base

    month_start = _add_months(base.replace(day=1), months_ahead)
    while month_start.weekday() >= 5:
        month_start += timedelta(days=1)
    return month_start


def _advance_relative_date(base: datetime.date, count: int, unit: str) -> Optional[str]:
    if count <= 0:
        return None

    unit_norm = _strip_accents(_normalize_spaces(str(unit or ""))).lower()
    if unit_norm.startswith("dia"):
        return (base + timedelta(days=count)).isoformat()
    if unit_norm.startswith("mes"):
        return _add_months(base, count).isoformat()
    if unit_norm.startswith("ano"):
        return _add_years(base, count).isoformat()
    return None


def _extract_relative_vacatio_days(value: Any) -> Optional[int]:
    """
    Extrai vacatio legis em frases relativas do tipo:
    - "entra em vigor 30 dias apos a data de sua publicacao"
    - "passa a vigorar apos 15 dias da publicacao"
    - "entra em vigor no prazo de 90 dias contados da publicacao"
    """
    text = _strip_accents(_normalize_spaces(_maybe_fix_mojibake(str(value or "")))).lower()
    if not text:
        return None

    verb_patterns = (
        r"\bentra\s+em\s+vigor\b",
        r"\bentrara\s+em\s+vigor\b",
        r"\bpassa\s+a\s+vigorar\b",
        r"\bpassara\s+a\s+vigorar\b",
        r"\bpassa\s+a\s+produzir\s+efeitos?\b",
        r"\bproduz\s+efeitos?\b",
        r"\bproduzira\s+efeitos?\b",
    )
    relation_markers = (
        "apos",
        "a partir de",
        "contados de",
        "contado de",
        "decorridos de",
        "decorrido de",
        "no prazo de",
        "depois de",
    )
    publication_markers = (
        r"a\s+data\s+de\s+sua\s+publicacao",
        r"da\s+data\s+de\s+sua\s+publicacao",
        r"da\s+publicacao",
        r"de\s+sua\s+publicacao",
        r"sua\s+publicacao",
        r"publicacao",
    )

    relative_patterns = []
    for verb_pattern in verb_patterns:
        relative_patterns.extend((verb_pattern,))

    publication_pattern = rf"(?:{'|'.join(publication_markers)})"

    def _scan_for_days(source_text: str) -> Optional[int]:
        for verb_pattern in relative_patterns:
            for match in re.finditer(verb_pattern, source_text, flags=re.IGNORECASE):
                tail = source_text[match.end() : min(len(source_text), match.end() + 240)]
                sentence = re.split(r"[.\n]", tail, maxsplit=1)[0]
                m = re.search(
                    rf"\bem\s+(?P<count>\d{{1,4}}|[a-z]+(?:\s+e\s+[a-z]+)*)\s*(?:\([^)]*\)\s*)?dias?\s+{publication_pattern}\b",
                    sentence,
                    flags=re.IGNORECASE,
                )
                if m:
                    days = _parse_portuguese_quantity(m.group("count"))
                    if days and 0 < days <= 3650:
                        return days

                for relation in relation_markers:
                    relation_pattern = re.escape(relation)
                    m = re.search(
                        rf"\b(?P<count>\d{{1,4}}|[a-z]+(?:\s+e\s+[a-z]+)*)\s*(?:\([^)]*\)\s*)?dias?\s+{relation_pattern}\s+{publication_pattern}\b",
                        sentence,
                        flags=re.IGNORECASE,
                    )
                    if m:
                        days = _parse_portuguese_quantity(m.group("count"))
                        if days and 0 < days <= 3650:
                            return days

                    m = re.search(
                        rf"\b{relation_pattern}\s+(?:o\s+prazo\s+de\s+)?(?P<count>\d{{1,4}}|[a-z]+(?:\s+e\s+[a-z]+)*)\s*(?:\([^)]*\)\s*)?dias?\s+{publication_pattern}\b",
                        sentence,
                        flags=re.IGNORECASE,
                    )
                    if m:
                        days = _parse_portuguese_quantity(m.group("count"))
                        if days and 0 < days <= 3650:
                            return days

        for pattern in (
            rf"\bem\s+(?P<count>\d{{1,4}}|[a-z]+(?:\s+e\s+[a-z]+)*)\s*(?:\([^)]*\)\s*)?dias?\s+{publication_pattern}\b",
            rf"\b(?P<count>\d{{1,4}}|[a-z]+(?:\s+e\s+[a-z]+)*)\s*(?:\([^)]*\)\s*)?dias?\s+{publication_pattern}\b",
            rf"\b(?:o\s+prazo\s+de\s+)?(?P<count>\d{{1,4}}|[a-z]+(?:\s+e\s+[a-z]+)*)\s*(?:\([^)]*\)\s*)?dias?\s+"
            rf"(?:{ '|'.join(relation_markers) })\s+{publication_pattern}\b",
        ):
            m = re.search(pattern, source_text, flags=re.IGNORECASE)
            if m:
                days = _parse_portuguese_quantity(m.groupdict().get("count"))
                if days and 0 < days <= 3650:
                    return days
        return None

    scanned = _scan_for_days(text)
    if scanned:
        return scanned

    return None


def _extract_relative_start_date(value: Any, publication_base: Optional[str] = None) -> Optional[str]:
    """
    Extrai data de inicio de vigencia em frases calendarias do tipo:
    - "no primeiro dia util do primeiro mes apos a data de sua publicacao"
    - "no primeiro dia util do mes subsequente"
    - "no primeiro dia util do mes seguinte"
    - "entra em vigor em 6 meses da publicacao"
    - "entra em vigor apos 1 ano da publicacao"
    """
    text = _strip_accents(_normalize_spaces(_maybe_fix_mojibake(str(value or "")))).lower()
    if not text or not publication_base:
        return None

    month_words = {
        "primeiro": 1,
        "segundo": 2,
        "terceiro": 3,
        "quarto": 4,
        "quinto": 5,
        "sexto": 6,
        "setimo": 7,
        "oitavo": 8,
        "nono": 9,
        "decimo": 10,
    }

    def _month_offset(token: Optional[str]) -> int:
        if not token:
            return 1
        parsed = _parse_portuguese_quantity(token)
        if parsed and parsed > 0:
            return parsed
        token_norm = _strip_accents(_normalize_spaces(token)).lower()
        return month_words.get(token_norm, 1)

    month_phrase = (
        r"(?:(?P<count>\d{1,4}|[a-z]+(?:\s+e\s+[a-z]+)*)\s+)?"
        r"mes(?:es)?"
    )
    relative_phrase = (
        r"(?:"
        r"apos(?:\s+a\s+data\s+de\s+sua\s+publicacao|\s+da\s+publicacao|\s+publicacao)?"
        r"|subsequente(?:\s+a(?:o|s)?\s+da\s+publicacao|\s+a\s+data\s+de\s+sua\s+publicacao|\s+da\s+publicacao|\s+publicacao)?"
        r"|seguinte(?:\s+a(?:o|s)?\s+da\s+publicacao|\s+a\s+data\s+de\s+sua\s+publicacao|\s+da\s+publicacao|\s+publicacao)?"
        r"|posterior(?:\s+a(?:o|s)?\s+da\s+publicacao|\s+a\s+data\s+de\s+sua\s+publicacao|\s+da\s+publicacao|\s+publicacao)?"
        r")"
    )
    patterns = (
        rf"primeiro\s+dia\s+util\s+do\s+{month_phrase}\s+{relative_phrase}",
        rf"primeiro\s+dia\s+util\s+{month_phrase}\s+{relative_phrase}",
    )
    try:
        pub = datetime.fromisoformat(publication_base).date()
    except ValueError:
        return None

    trigger_patterns = (
        r"entra\s+em\s+vigor",
        r"entrara\s+em\s+vigor",
        r"passa\s+a\s+vigorar",
        r"passara\s+a\s+vigorar",
        r"produz(?:em|indo|irao|ira)?\s+efeitos?",
        r"produzindo\s+efeitos?",
        r"passa\s+a\s+produzir\s+efeitos?",
    )
    duration_markers = (
        r"ate",
        r"apos",
        r"a\s+partir\s+de",
        r"a\s+contar\s+de",
        r"a\s+contar\s+da",
        r"contados?\s+de",
        r"contados?\s+da",
        r"decorridos?\s+de",
        r"decorridos?\s+da",
        r"no\s+prazo\s+de",
        r"em",
        r"dentro\s+de",
    )
    publication_pattern = (
        r"(?:a\s+data\s+de\s+sua\s+publicacao|da\s+data\s+de\s+sua\s+publicacao|"
        r"a\s+publicacao|da\s+publicacao|de\s+sua\s+publicacao|sua\s+publicacao|publicacao)"
    )
    business_day_pattern = (
        r"\b(?:no\s+)?primeiro\s+dia\s+util\s+do\s+"
        r"(?:(?P<count>\d{1,4}|[a-z]+(?:\s+e\s+[a-z]+)*)\s+)?"
        r"mes(?:es)?\b"
    )
    quantity_pattern = (
        r"\d{1,4}|[a-z]+(?:\s+e\s+[a-z]+)*"
    )
    duration_patterns = (
        rf"\b(?:(?:{'|'.join(duration_markers)})\s+)?(?:o\s+prazo\s+de\s+)?(?P<count>{quantity_pattern})"
        rf"(?:\s*\([^)]*\))?\s+(?P<unit>dias?|mes(?:es)?|anos?)\s+(?:de\s+)?{publication_pattern}\b",
        rf"\b(?P<count>{quantity_pattern})(?:\s*\([^)]*\))?\s+(?P<unit>dias?|mes(?:es)?|anos?)\s+"
        rf"(?:{'|'.join(duration_markers)})\s+(?:de\s+)?{publication_pattern}\b",
    )

    def _scan_relative_start(source_text: str) -> Optional[str]:
        match = None
        for trigger in trigger_patterns:
            for trigger_match in re.finditer(trigger, source_text, flags=re.IGNORECASE):
                tail = source_text[trigger_match.end() : min(len(source_text), trigger_match.end() + 280)]
                sentence = re.split(r"[.\n]", tail, maxsplit=1)[0]

                match = re.search(business_day_pattern, sentence, flags=re.IGNORECASE)

                if not match:
                    for pattern in patterns:
                        match = re.search(pattern, sentence, flags=re.IGNORECASE)
                        if match:
                            break

                if not match:
                    for pattern in duration_patterns:
                        match = re.search(pattern, sentence, flags=re.IGNORECASE)
                        if match:
                            break

                if not match:
                    continue

                groups = match.groupdict()
                count = _parse_portuguese_quantity(groups.get("count"))
                if not count or count <= 0:
                    count = 1

                unit = groups.get("unit") or ""
                if "dia util" in _strip_accents(_normalize_spaces(match.group(0))).lower():
                    return _first_business_day_of_month(pub, count).isoformat()
                if unit.startswith("mes"):
                    return _add_months(pub, count).isoformat()

                if unit.startswith("dia") or unit.startswith("ano"):
                    candidate = _advance_relative_date(pub, count, unit)
                    if candidate:
                        return candidate

                match = re.search(business_day_pattern, source_text, flags=re.IGNORECASE)
        if match:
            groups = match.groupdict()
            count = _parse_portuguese_quantity(groups.get("count")) or 1
            return _first_business_day_of_month(pub, count).isoformat()

        for pattern in patterns:
            match = re.search(pattern, source_text, flags=re.IGNORECASE)
            if match:
                groups = match.groupdict()
                count = _parse_portuguese_quantity(groups.get("count")) or 1
                return _first_business_day_of_month(pub, count).isoformat()

        for pattern in duration_patterns:
            match = re.search(pattern, source_text, flags=re.IGNORECASE)
            if not match:
                continue
            groups = match.groupdict()
            count = _parse_portuguese_quantity(groups.get("count")) or 1
            unit = groups.get("unit") or ""
            if unit.startswith("mes"):
                return _add_months(pub, count).isoformat()
            if unit.startswith("dia") or unit.startswith("ano"):
                candidate = _advance_relative_date(pub, count, unit)
                if candidate:
                    return candidate
        return None

    scanned = _scan_relative_start(text)
    if scanned:
        return scanned

    return None


def _looks_like_circuit_decision(doc_name: Any, titulo_norma: Any) -> bool:
    text = _normalize_spaces(
        _maybe_fix_mojibake(f"{doc_name or ''} {titulo_norma or ''}")
    ).lower()
    return (
        "decisão em circuito deliberativo" in text
        or "decisao em circuito deliberativo" in text
    )


def _extract_revocation_line_info(text: str) -> Optional[Dict[str, Any]]:
    if not text:
        return None

    # Prioriza as primeiras linhas (cabecalho + linha revogatoria).
    lines = [line.strip() for line in str(text).splitlines() if line.strip()]
    header_lines = lines[:12]

    line_pat = re.compile(
        r"^(?P<partial>parcialmente\s+)?revogad[ao]s?\s+pela\s+(?P<ref>.+)$",
        re.IGNORECASE,
    )
    inline_pat = re.compile(
        r"\b(?P<partial>parcialmente\s+)?revogad[ao]s?\s+pela\s+(?P<ref>[^.\n]{6,220})",
        re.IGNORECASE,
    )
    type_pat = re.compile(
        r"\b(portaria|decreto|lei|resolucao|instrucao normativa|medida provisoria)\b",
        re.IGNORECASE,
    )
    num_year_pat = re.compile(r"\b(\d{1,6})\s*/\s*(19\d{2}|20\d{2})\b")
    num_pat = re.compile(r"\b(?:n[º°o.]?\s*)?(\d{1,6})\b", re.IGNORECASE)

    candidate = None
    match_mode = None
    for line in header_lines:
        m = line_pat.search(_normalize_spaces(line))
        if m:
            candidate = {
                "partial": bool(m.group("partial")),
                "ref_raw": _normalize_spaces(m.group("ref")),
            }
            match_mode = "line"
            break

    if candidate is None:
        prefix = _normalize_spaces(str(text)[:1200])
        m = inline_pat.search(prefix)
        if m:
            candidate = {
                "partial": bool(m.group("partial")),
                "ref_raw": _normalize_spaces(m.group("ref")),
            }
            match_mode = "inline"

    if candidate is None:
        return None

    ref_raw = candidate["ref_raw"]
    ref_text = _normalize_spaces(_maybe_fix_mojibake(ref_raw))
    ref_norm = _strip_accents(ref_text).lower()

    tipo_norma = None
    type_match = type_pat.search(ref_norm)
    if type_match:
        tipo_norma = _normalize_tipo_norma(type_match.group(1))

    numero_norma = None
    ano_norma = None

    m_num_year = num_year_pat.search(ref_norm)
    if m_num_year:
        numero_norma = m_num_year.group(1)
        ano_norma = int(m_num_year.group(2))
    else:
        m_num = num_pat.search(ref_norm)
        if m_num:
            numero_norma = m_num.group(1)
        ano_norma = _parse_year(ref_norm)

    date_iso = _parse_pt_date_to_iso(ref_norm)
    if date_iso and not ano_norma:
        ano_norma = int(date_iso[:4])

    return {
        "status_normativo": "parcialmente_revogado" if candidate["partial"] else "revogado",
        "ref_raw": ref_text,
        "tipo_norma_revogadora": tipo_norma,
        "numero_norma_revogadora": numero_norma,
        "ano_norma_revogadora": ano_norma,
        "revogado_por_data": date_iso,
        "match_mode": match_mode,
    }


def _coerce_iso_date(value: Any) -> Optional[str]:
    if value in (None, "", []):
        return None
    text = _normalize_spaces(str(value))
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
        return text
    # Tenta parser PT-BR textual (ex.: 12 de maio de 2016).
    parsed = _parse_pt_date_to_iso(text)
    if parsed:
        return parsed
    # Tenta dd/mm/yyyy ou dd/mm/yy.
    m = re.fullmatch(r"(\d{1,2})/(\d{1,2})/((?:19|20)\d{2}|\d{2})", text)
    if m:
        d, mth, y = int(m.group(1)), int(m.group(2)), _expand_two_digit_year(m.group(3))
        try:
            return datetime(y, mth, d).date().isoformat()
        except ValueError:
            return None
    return None


def _build_doc_revocation_overrides(
    chunks: List[Dict],
    revoker_lookup: Dict[Tuple[str, str, int], List[Dict]],
    doc_start_map: Optional[Dict[str, str]] = None,
    doc_publication_map: Optional[Dict[str, str]] = None,
) -> Dict[str, Dict[str, Any]]:
    overrides: Dict[str, Dict[str, Any]] = {}

    for chunk in chunks:
        doc_name = chunk.get("doc_name") or chunk.get("doc")
        if not doc_name or doc_name in overrides:
            continue

        info = _extract_revocation_line_info(chunk.get("text") or "")
        if not info:
            continue

        override: Dict[str, Any] = {
            "status_normativo": info["status_normativo"],
            "revogado_por": info["ref_raw"],
            "confianca_parse_revogacao": 1.0,
            "parse_revogacao_match_mode": info.get("match_mode"),
        }

        if info["status_normativo"] == "parcialmente_revogado":
            override["tipo_revogacao"] = "parcial"
            override["tem_revogacao_parcial_dispositivo"] = True
        else:
            override["tipo_revogacao"] = "total"

        rev_data = info.get("revogado_por_data")
        key = (
            _normalize_tipo_norma(info.get("tipo_norma_revogadora") or ""),
            _normalize_numero_norma(info.get("numero_norma_revogadora")),
            info.get("ano_norma_revogadora"),
        )
        ref_meta_candidates = revoker_lookup.get(key) if all(key) else None
        ref_meta = None
        if ref_meta_candidates:
            ref_lookup = _normalize_lookup_text(info.get("ref_raw"))
            candidate_pool = [
                meta
                for meta in ref_meta_candidates
                if not _same_normalized_source(meta, doc_name)
            ]
            if not candidate_pool:
                candidate_pool = ref_meta_candidates
            exact_matches = [
                meta
                for meta in candidate_pool
                if _normalize_lookup_text(meta.get("source_file_name") or meta.get("source_relative_path"))
                == ref_lookup
            ]
            if exact_matches:
                ref_meta = exact_matches[0]
            else:
                partial_matches = [
                    meta
                    for meta in candidate_pool
                    if ref_lookup
                    and (
                        ref_lookup in _normalize_lookup_text(meta.get("source_file_name") or meta.get("source_relative_path"))
                        or _normalize_lookup_text(meta.get("source_file_name") or meta.get("source_relative_path")) in ref_lookup
                    )
                ]
                if partial_matches:
                    ref_meta = partial_matches[0]
                else:
                    tipo = _normalize_tipo_norma(info.get("tipo_norma_revogadora") or "")
                    numero = _normalize_numero_norma(info.get("numero_norma_revogadora"))
                    ano = info.get("ano_norma_revogadora")
                    scored_candidates = sorted(
                        candidate_pool,
                        key=lambda meta: _revoker_candidate_score(meta, ref_lookup, tipo, numero, ano),
                        reverse=True,
                    )
                    ref_meta = scored_candidates[0] if scored_candidates else ref_meta_candidates[0]

        if ref_meta:
            override["revogado_por_doc_id"] = ref_meta.get("doc_id")
            revoker_source_name = ref_meta.get("source_file_name") or ref_meta.get("source_relative_path")
            revoker_publication = (
                _lookup_doc_map_value(doc_publication_map, revoker_source_name)
                if doc_publication_map and revoker_source_name
                else None
            )
            revoker_start = (
                _lookup_doc_map_value(doc_start_map, revoker_source_name)
                if doc_start_map and revoker_source_name
                else None
            )
            if not revoker_start:
                revoker_start = _coerce_iso_date(ref_meta.get("data_inicio_vigencia"))
            if not revoker_start:
                revoker_start = _coerce_iso_date(ref_meta.get("data_publicacao"))
                if revoker_start:
                    revoker_vacatio = _parse_int(ref_meta.get("vacatio_dias")) or _parse_int(ref_meta.get("vacatio_legis_dias")) or 0
                    if revoker_vacatio > 0:
                        revoker_start = (
                            datetime.fromisoformat(revoker_start).date()
                            + timedelta(days=revoker_vacatio)
                        ).isoformat()
            if revoker_start:
                override["revogado_por_data_inicio_vigencia"] = revoker_start
            if not rev_data:
                rev_data = revoker_publication or (
                    _coerce_iso_date(ref_meta.get("revogado_por_data"))
                    or _coerce_iso_date(ref_meta.get("data_publicacao"))
                    or _coerce_iso_date(ref_meta.get("data_inicio_vigencia"))
                )

        rev_data = _coerce_iso_date(rev_data)
        rev_year = info.get("ano_norma_revogadora")
        if not rev_year and ref_meta:
            rev_year = (
                _parse_year(ref_meta.get("revogado_por_data"))
                or _parse_year(ref_meta.get("data_ultima_revogacao"))
                or _parse_year(ref_meta.get("data_publicacao"))
            )
        if rev_data:
            override["revogado_por_data"] = rev_data
            override["data_fim_vigencia"] = rev_data
        elif override.get("status_normativo") == "revogado" and rev_year:
            # Quando a data completa nao e inferivel, preserva ao menos o ano.
            override["data_fim_vigencia"] = str(int(rev_year))
            override["revogado_por_ano"] = int(rev_year)

        overrides[str(doc_name)] = override

    return overrides


def _load_metadata(
    metadata_csv: Path,
) -> Tuple[Dict[str, Dict], Dict[str, Dict], Dict[Tuple[str, str, int], List[Dict]]]:
    exact: Dict[str, Dict] = {}
    normalized: Dict[str, Dict] = {}
    revoker_lookup: Dict[Tuple[str, str, int], List[Dict]] = {}

    with metadata_csv.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        for row_no, row in enumerate(reader, start=1):
            if row_no == 1 or row_no % 1000 == 0:
                print(f"[METADADOS] carregando CSV: {row_no} linhas lidas")
            source_name = (
                row.get("source_file_name")
                or row.get("source_relative_path")
                or row.get("doc_id")
                or row.get("doc")
                or row.get("doc_name")
            )
            if not source_name and row.get("pdf_path"):
                source_name = Path(str(row.get("pdf_path"))).name
            if not source_name:
                continue

            parsed = {k: _parse_scalar(v) for k, v in row.items()}
            if parsed.get("titulo_norma"):
                parsed["titulo_norma"] = _strip_print_stamp_prefix(parsed.get("titulo_norma"))
            exact[source_name] = parsed

            normalized_key = _normalize_name(source_name)
            if normalized_key and normalized_key not in normalized:
                normalized[normalized_key] = parsed

            tipo = _normalize_tipo_norma(parsed.get("tipo_norma"))
            numero = _normalize_numero_norma(parsed.get("numero_norma"))
            ano = _parse_year(parsed.get("ano_norma"))

            if tipo and numero and ano:
                key = (tipo, numero, ano)
                revoker_lookup.setdefault(key, []).append(parsed)

    print(
        "[METADADOS] CSV carregado: "
        f"{len(exact)} documentos | {len(normalized)} chaves normalizadas | "
        f"{len(revoker_lookup)} chaves revogadoras"
    )
    return exact, normalized, revoker_lookup


def _iter_chunks(chunks_path: Path) -> Iterable[Dict]:
    with chunks_path.open("r", encoding="utf-8") as handle:
        for line_no, raw in enumerate(handle, start=1):
            raw = raw.strip()
            if not raw:
                continue
            try:
                yield json.loads(raw)
            except json.JSONDecodeError as exc:
                raise ValueError(
                    f"JSON invalido no chunks.jsonl (linha {line_no}): {exc}"
                ) from exc


def _backup_file(path: Path) -> Path:
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup = path.with_suffix(path.suffix + f".bak_{stamp}")
    shutil.copy2(path, backup)
    return backup


def _build_doc_publication_map(chunks: List[Dict]) -> Dict[str, str]:
    candidates_by_doc: Dict[str, Dict[str, List[str]]] = {}
    year_hint_by_doc: Dict[str, Optional[int]] = {}

    for chunk in chunks:
        doc_name = chunk.get("doc_name") or chunk.get("doc")
        if not doc_name:
            continue
        doc_key = str(doc_name)
        buckets = candidates_by_doc.setdefault(
            doc_key,
            {"markers": [], "title": [], "head": [], "tail": []},
        )
        buckets["markers"].extend(_extract_publication_dates_from_text(chunk.get("text")))
        buckets["title"].extend(_extract_dates_from_text(chunk.get("titulo_norma")))
        buckets["title"].extend(_extract_dates_from_text(doc_name))
        buckets["head"].extend(_extract_dates_from_text(str(chunk.get("text") or "")[:1400]))
        buckets["tail"].extend(_extract_dates_from_text(str(chunk.get("text") or "")[-1200:]))

        if doc_key not in year_hint_by_doc:
            year_hint_by_doc[doc_key] = (
                _parse_year(chunk.get("ano_norma"))
                or _parse_year(chunk.get("titulo_norma"))
                or _parse_year(doc_name)
            )

    chosen_by_doc: Dict[str, str] = {}
    for doc_name, buckets in candidates_by_doc.items():
        for source in ("markers", "title", "head", "tail"):
            candidates = buckets.get(source) or []
            if not candidates:
                continue
            chosen = _choose_date_by_year(candidates, year_hint_by_doc.get(doc_name))
            if chosen:
                chosen_by_doc[doc_name] = chosen
                break

    return chosen_by_doc


def _build_doc_start_map(chunks: List[Dict]) -> Dict[str, str]:
    candidates_by_doc: Dict[str, List[str]] = {}

    for chunk in chunks:
        doc_name = chunk.get("doc_name") or chunk.get("doc")
        if not doc_name:
            continue
        start = _coerce_iso_date(chunk.get("data_inicio_vigencia"))
        if not start:
            continue
        candidates_by_doc.setdefault(str(doc_name), []).append(start)

    chosen_by_doc: Dict[str, str] = {}
    for doc_name, candidates in candidates_by_doc.items():
        if not candidates:
            continue
        chosen_by_doc[doc_name] = max(candidates)

    return chosen_by_doc


def _build_doc_reference_map(chunks: List[Dict]) -> Dict[str, str]:
    candidates_by_doc: Dict[str, List[str]] = {}

    for chunk in chunks:
        doc_name = chunk.get("doc_name") or chunk.get("doc")
        if not doc_name:
            continue
        doc_key = str(doc_name)
        candidates = candidates_by_doc.setdefault(doc_key, [])
        candidates.extend(_extract_dates_from_text(doc_key))
        candidates.extend(_extract_dates_from_text(chunk.get("titulo_norma")))

    chosen_by_doc: Dict[str, str] = {}
    for doc_name, candidates in candidates_by_doc.items():
        if not candidates:
            continue
        year_hint = None
        for chunk in chunks:
            chunk_name = chunk.get("doc_name") or chunk.get("doc")
            if str(chunk_name) == doc_name:
                year_hint = _parse_year(chunk.get("ano_norma"))
                if year_hint:
                    break
        chosen = _choose_date_by_year(candidates, year_hint)
        if chosen:
            chosen_by_doc[doc_name] = chosen

    return chosen_by_doc


def _lookup_doc_map_value(doc_map: Dict[str, str], doc_name: Any) -> Optional[str]:
    if not doc_name:
        return None
    exact_key = str(doc_name)
    exact_value = doc_map.get(exact_key)
    if exact_value:
        return exact_value
    return doc_map.get(_normalize_name(exact_key))


def enrich_chunks(
    chunks_path: Path,
    metadata_csv: Path,
    output_path: Path,
    changed_output_path: Optional[Path],
    metadata_fields,
    create_backup: bool,
):
    if not chunks_path.exists():
        raise FileNotFoundError(f"chunks.jsonl nao encontrado: {chunks_path}")
    if not metadata_csv.exists():
        raise FileNotFoundError(f"index.csv nao encontrado: {metadata_csv}")

    exact_meta, normalized_meta, revoker_lookup = _load_metadata(metadata_csv)
    if not exact_meta:
        raise RuntimeError("Nenhum metadado valido encontrado no index.csv.")

    if create_backup and output_path.resolve() == chunks_path.resolve():
        backup = _backup_file(chunks_path)
        print(f"[BACKUP] {backup}")

    chunks = list(_iter_chunks(chunks_path))
    print(f"[METADADOS] chunks carregados: {len(chunks)}")
    original_chunks = [dict(chunk) for chunk in chunks]
    print("[METADADOS] preparando mapas auxiliares")
    doc_start_map = _build_doc_start_map(chunks)
    doc_publication_map = _build_doc_publication_map(chunks)
    doc_reference_map = _build_doc_reference_map(chunks)
    doc_revocation_overrides = _build_doc_revocation_overrides(chunks, revoker_lookup, doc_start_map, doc_publication_map)
    print("[METADADOS] mapas auxiliares prontos")

    total_chunks = 0
    matched_chunks = 0
    unmatched_chunks = 0
    matched_docs = set()
    unmatched_docs = set()
    changed_chunks = 0
    changed_docs = set()
    enriched_rows = []
    changed_rows = []

    total_to_process = len(chunks)
    for chunk_index, chunk in enumerate(chunks, start=1):
        total_chunks += 1
        if chunk_index == 1 or chunk_index % 5000 == 0 or chunk_index == total_to_process:
            pct = (chunk_index / total_to_process * 100.0) if total_to_process else 100.0
            print(
                "[METADADOS] enriquecendo chunks: "
                f"{chunk_index}/{total_to_process} ({pct:.1f}%)"
            )
        doc_name = chunk.get("doc_name") or chunk.get("doc")
        meta = exact_meta.get(doc_name)
        before = dict(chunk)

        if meta is None:
            meta = normalized_meta.get(_normalize_name(doc_name))

        if meta is None and str(doc_name) not in doc_revocation_overrides:
            unmatched_chunks += 1
            if doc_name:
                unmatched_docs.add(str(doc_name))
            enriched_rows.append(chunk)
            continue

        if meta is not None:
            matched_chunks += 1
            if doc_name:
                matched_docs.add(str(doc_name))

            for field in metadata_fields:
                new_value = meta.get(field)
                if chunk.get(field) != new_value:
                    chunk[field] = new_value

        override = doc_revocation_overrides.get(str(doc_name))
        if override:
            effective_override = dict(override)
            meta_status = (
                _normalize_spaces(str(meta.get("status_normativo") or "")).lower()
                if meta
                else ""
            )

            # Quando o metadado indica revogacao parcial, nao marca o
            # normativo como integralmente revogado via heuristica textual.
            if _metadata_prefers_partial(meta):
                parse_mode = effective_override.get("parse_revogacao_match_mode")
                parse_partial = bool(effective_override.get("parse_revogacao_partial"))
                has_strong_total_line = (
                    effective_override.get("status_normativo") == "revogado"
                    and parse_mode == "line"
                    and not parse_partial
                )
                if has_strong_total_line:
                    effective_override["tipo_revogacao"] = "total"
                    effective_override["tem_revogacao_parcial_dispositivo"] = False
                    effective_override["data_ultima_revogacao_parcial"] = None
                else:
                    effective_override["status_normativo"] = "parcialmente_revogado"
                    effective_override["tipo_revogacao"] = "parcial"
                    effective_override["tem_revogacao_parcial_dispositivo"] = True
                    effective_override.pop("data_fim_vigencia", None)

            # Evita falso positivo: match inline costuma refletir
            # dispositivo revogado dentro da norma (nao revogacao total).
            if (
                effective_override.get("status_normativo") == "revogado"
                and effective_override.get("parse_revogacao_match_mode") == "inline"
                and meta_status in {"vigente", "nao_normativo", "indeterminado", "vacatio"}
            ):
                effective_override.pop("status_normativo", None)
                effective_override.pop("tipo_revogacao", None)
                effective_override.pop("data_fim_vigencia", None)
                effective_override.pop("revogado_por", None)
                effective_override.pop("revogado_por_doc_id", None)
                effective_override.pop("revogado_por_data", None)
                effective_override["confianca_parse_revogacao"] = 0.0

            for key, value in effective_override.items():
                if chunk.get(key) != value:
                    chunk[key] = value

        # Evita falso positivo de revogacao em decisoes de circuito
        # quando nao ha evidencia textual de "revogado pela ...".
        if (
            meta is not None
            and not override
            and _looks_like_circuit_decision(doc_name, chunk.get("titulo_norma"))
            and _normalize_spaces(str(chunk.get("status_normativo") or "")).lower() == "revogado"
        ):
            chunk["status_normativo"] = "vigente"
            for fld in (
                "data_fim_vigencia",
                "revogado_por",
                "revogado_por_data",
                "revogado_por_doc_id",
                "data_primeira_revogacao",
                "data_ultima_revogacao",
                "data_ultima_revogacao_parcial",
                "tipo_revogacao",
                "revogado_por_ano",
            ):
                if chunk.get(fld) is not None:
                    chunk[fld] = None

        # Normalizacao final para revogacao total: se faltar data completa de
        # fim de vigencia, usa ao menos o ano de revogacao (quando inferivel).
        status_now = _normalize_spaces(str(chunk.get("status_normativo") or "")).lower()
        doc_year_hint = _parse_year(chunk.get("ano_norma"))

        # Regra solicitada:
        # 1) parse do normativo (titulo_norma/texto de cabecalho)
        # 2) se nao encontrar, parse do nome do arquivo
        # 3) se ainda nao houver e tiver ano_hint, usa ano-01-01
        publication_base = _lookup_doc_map_value(doc_publication_map, doc_name) or _infer_publication_date(chunk, doc_year_hint)

        vacatio_days = _parse_int(chunk.get("vacatio_dias")) or _parse_int(chunk.get("vacatio_legis_dias")) or 0
        if vacatio_days <= 0:
            inferred_vacatio_days = (
                _extract_relative_vacatio_days(chunk.get("text"))
                or _extract_relative_vacatio_days(chunk.get("titulo_norma"))
            )
            if inferred_vacatio_days:
                vacatio_days = inferred_vacatio_days
                chunk["vacatio_dias"] = inferred_vacatio_days

        inferred_start = _infer_start_date(chunk, doc_year_hint, publication_base)

        # Trata vacatio legis sem perder regra de entrada em vigor.
        if status_now == "vacatio":
            pub_iso = _coerce_iso_date(chunk.get("data_publicacao"))
            pub_year = _parse_year(chunk.get("data_publicacao"))

            if (
                not publication_base
                and pub_iso
                and (
                    not doc_year_hint
                    or not pub_year
                    or abs(int(pub_year) - int(doc_year_hint)) <= 1
                )
            ):
                publication_base = pub_iso

            if publication_base and chunk.get("data_publicacao") != publication_base:
                chunk["data_publicacao"] = publication_base

            explicit_vig_dates = _extract_entry_into_force_dates(chunk.get("titulo_norma"))
            vacatio_start: Optional[str] = None

            if explicit_vig_dates:
                expected_from_vacatio = None
                if publication_base and vacatio_days > 0:
                    expected_from_vacatio = (
                        datetime.fromisoformat(publication_base).date()
                        + timedelta(days=vacatio_days)
                    ).isoformat()

                if expected_from_vacatio:
                    vacatio_start = _choose_nearest_date(explicit_vig_dates, expected_from_vacatio)
                if not vacatio_start:
                    vacatio_start = _choose_date_by_year(explicit_vig_dates, doc_year_hint)

            if not vacatio_start and publication_base and vacatio_days > 0:
                vacatio_start = (
                    datetime.fromisoformat(publication_base).date()
                    + timedelta(days=vacatio_days)
                ).isoformat()

            if not vacatio_start:
                vacatio_start = inferred_start

            if vacatio_start and chunk.get("data_inicio_vigencia") != vacatio_start:
                chunk["data_inicio_vigencia"] = vacatio_start
        else:
            if inferred_start:
                if chunk.get("data_inicio_vigencia") != inferred_start:
                    chunk["data_inicio_vigencia"] = inferred_start
            elif doc_year_hint:
                fallback_start = f"{int(doc_year_hint):04d}-01-01"
                if chunk.get("data_inicio_vigencia") != fallback_start:
                    chunk["data_inicio_vigencia"] = fallback_start

        # Re-normaliza para ISO se entrou em formato textual.
        inicio_norm = _coerce_iso_date(chunk.get("data_inicio_vigencia"))
        if inicio_norm and chunk.get("data_inicio_vigencia") != inicio_norm:
            chunk["data_inicio_vigencia"] = inicio_norm

        if status_now == "revogado":
            # Sanidade temporal: uma norma nao pode ser revogada por outra
            # de ano anterior ao seu proprio ano de edicao.
            rev_year_ref = _extract_year_from_text(chunk.get("revogado_por"))
            if doc_year_hint and rev_year_ref and int(rev_year_ref) < int(doc_year_hint):
                chunk["status_normativo"] = "vigente"
                for fld in (
                    "data_fim_vigencia",
                    "revogado_por",
                    "revogado_por_data",
                    "revogado_por_doc_id",
                    "data_primeira_revogacao",
                    "data_ultima_revogacao",
                    "data_ultima_revogacao_parcial",
                    "tipo_revogacao",
                    "revogado_por_ano",
                ):
                    if chunk.get(fld) is not None:
                        chunk[fld] = None
                status_now = "vigente"

        if status_now == "revogado":
            fim_iso = _coerce_iso_date(chunk.get("data_fim_vigencia"))
            rev_data_iso = _coerce_iso_date(chunk.get("revogado_por_data"))
            rev_start_iso = _coerce_iso_date(chunk.get("revogado_por_data_inicio_vigencia"))
            rev_year_from_ref = _extract_year_from_text(chunk.get("revogado_por"))
            forced_rev_year = None

            # Prioriza o inicio de vigencia da norma revogadora, quando houver.
            if rev_start_iso and chunk.get("data_fim_vigencia") != rev_start_iso:
                chunk["data_fim_vigencia"] = rev_start_iso
                fim_iso = rev_start_iso
            elif rev_data_iso and chunk.get("data_fim_vigencia") != rev_data_iso:
                chunk["data_fim_vigencia"] = rev_data_iso
                fim_iso = rev_data_iso

            # Se houver divergencia forte entre fim e ano da revogadora
            # (tipico de data de impressao/coleta no topo do PDF),
            # corrige para o ano da norma revogadora.
            if fim_iso and rev_year_from_ref:
                try:
                    fim_year = int(str(fim_iso)[:4])
                except ValueError:
                    fim_year = None
                if fim_year is not None and abs(fim_year - int(rev_year_from_ref)) >= 2:
                    chunk["data_fim_vigencia"] = str(int(rev_year_from_ref))
                    chunk["revogado_por_ano"] = int(rev_year_from_ref)
                    chunk["revogado_por_data"] = str(int(rev_year_from_ref))
                    forced_rev_year = int(rev_year_from_ref)
                    fim_iso = None

            if fim_iso:
                if chunk.get("data_fim_vigencia") != fim_iso:
                    chunk["data_fim_vigencia"] = fim_iso
            else:
                rev_year = (
                    forced_rev_year
                    or rev_year_from_ref
                    or _parse_year(chunk.get("revogado_por_data"))
                    or _parse_year(chunk.get("data_ultima_revogacao"))
                    or _parse_year(chunk.get("data_primeira_revogacao"))
                )
                if rev_year:
                    if chunk.get("data_fim_vigencia") != str(int(rev_year)):
                        chunk["data_fim_vigencia"] = str(int(rev_year))
                    if chunk.get("revogado_por_ano") != int(rev_year):
                        chunk["revogado_por_ano"] = int(rev_year)

        if not _lookup_doc_map_value(doc_publication_map, doc_name):
            print_stamp_date = _extract_print_stamp_date(chunk.get("text") or chunk.get("titulo_norma"))
            if print_stamp_date and _coerce_iso_date(chunk.get("data_publicacao")) == print_stamp_date:
                chunk["data_publicacao"] = None

        if publication_base:
            current_pub = _coerce_iso_date(chunk.get("data_publicacao"))
            current_pub_year = _parse_year(chunk.get("data_publicacao"))
            inferred_pub_year = _parse_year(publication_base)
            if (
                not current_pub
                or not current_pub_year
                or not inferred_pub_year
                or abs(int(current_pub_year) - int(inferred_pub_year)) >= 2
            ):
                if chunk.get("data_publicacao") != publication_base:
                    chunk["data_publicacao"] = publication_base

        inferred_start = _infer_start_date(chunk, doc_year_hint, publication_base)
        if inferred_start and chunk.get("data_inicio_vigencia") != inferred_start:
            chunk["data_inicio_vigencia"] = inferred_start

        if chunk != before:
            changed_chunks += 1
            if doc_name:
                changed_docs.add(str(doc_name))
            changed_rows.append(chunk)

        enriched_rows.append(chunk)

    # Consenso por documento para evitar que o mesmo normativo fique com
    # datas de inicio de vigencia diferentes em chunks distintos.
    print("[METADADOS] consolidando consenso por documento")
    rows_by_doc: Dict[str, List[Dict[str, Any]]] = {}
    for row in enriched_rows:
        doc_name = row.get("doc_name") or row.get("doc")
        if not doc_name:
            continue
        rows_by_doc.setdefault(str(doc_name), []).append(row)

    total_docs_consensus = len(rows_by_doc)
    for doc_index, (doc_name, rows) in enumerate(rows_by_doc.items(), start=1):
        if doc_index == 1 or doc_index % 1000 == 0 or doc_index == total_docs_consensus:
            pct = (doc_index / total_docs_consensus * 100.0) if total_docs_consensus else 100.0
            print(
                "[METADADOS] consenso por documento: "
                f"{doc_index}/{total_docs_consensus} ({pct:.1f}%)"
            )
        pub_candidates = [
            _coerce_iso_date(row.get("data_publicacao")) or str(row.get("data_publicacao") or "")
            for row in rows
        ]
        pub_candidates = [value for value in pub_candidates if value]
        if pub_candidates:
            reference_pub = doc_publication_map.get(doc_name)
            canonical_pub = None
            if reference_pub:
                canonical_pub = reference_pub
            if not canonical_pub:
                canonical_pub = _choose_date_by_year(pub_candidates, _parse_year(rows[0].get("ano_norma")))
            if not canonical_pub:
                canonical_pub = sorted(pub_candidates)[0]
            for row in rows:
                if row.get("data_publicacao") != canonical_pub:
                    row["data_publicacao"] = canonical_pub

        start_candidates = [
            _coerce_iso_date(row.get("data_inicio_vigencia")) or str(row.get("data_inicio_vigencia") or "")
            for row in rows
        ]
        start_candidates = [value for value in start_candidates if value]
        if not start_candidates:
            continue

        reference_date = doc_reference_map.get(doc_name)
        canonical_start = None
        if reference_date:
            canonical_start = _choose_nearest_date(start_candidates, reference_date)
        if not canonical_start:
            canonical_start = _choose_date_by_year(start_candidates, _parse_year(rows[0].get("ano_norma")))
        if not canonical_start:
            canonical_start = sorted(start_candidates)[-1]

        for row in rows:
            if row.get("data_inicio_vigencia") != canonical_start:
                row["data_inicio_vigencia"] = canonical_start

    for row in enriched_rows:
        row.pop("data_publicacao", None)
        row.pop("titulo_norma", None)

    changed_rows = []
    changed_docs = set()
    changed_chunks = 0
    for before_row, after_row in zip(original_chunks, enriched_rows):
        if before_row != after_row:
            changed_rows.append(after_row)
            changed_chunks += 1
            doc_name = after_row.get("doc_name") or after_row.get("doc")
            if doc_name:
                changed_docs.add(str(doc_name))

    print("[METADADOS] gravando chunks enriquecidos")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", encoding="utf-8", newline="\n") as handle:
        for row in enriched_rows:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")

    if changed_output_path is None:
        changed_output_path = output_path.with_suffix(".changed.jsonl")
    changed_output_path.parent.mkdir(parents=True, exist_ok=True)
    with changed_output_path.open("w", encoding="utf-8", newline="\n") as handle:
        for row in changed_rows:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")

    print(f"[OK] chunks processados: {total_chunks}")
    print(f"[OK] chunks com metadados: {matched_chunks}")
    print(f"[WARN] chunks sem metadados: {unmatched_chunks}")
    print(f"[OK] documentos com metadados: {len(matched_docs)}")
    print(f"[WARN] documentos sem metadados: {len(unmatched_docs)}")
    print(f"[OK] chunks alterados: {changed_chunks}")
    print(f"[OK] documentos alterados: {len(changed_docs)}")
    print(f"[OK] arquivo de alteracoes: {changed_output_path}")
    if unmatched_docs:
        sample = sorted(unmatched_docs)[:10]
        print(f"[AMOSTRA] docs sem metadados: {sample}")


def parse_args():
    parser = argparse.ArgumentParser(
        description="Enriquece chunks.jsonl com metadados processados do index.csv."
    )
    parser.add_argument("--chunks", required=True, help="Caminho do chunks.jsonl")
    parser.add_argument("--metadata-csv", required=True, help="Caminho do index.csv")
    parser.add_argument(
        "--output",
        help="Caminho de saida do chunks.jsonl enriquecido (padrao: sobrescreve o arquivo de entrada).",
    )
    parser.add_argument(
        "--changed-output",
        help=(
            "Caminho de saida contendo somente os chunks alterados "
            "(padrao: <output>.changed.jsonl)."
        ),
    )
    parser.add_argument(
        "--metadata-fields",
        nargs="+",
        default=DEFAULT_METADATA_FIELDS,
        help="Lista de campos de metadados a copiar para cada chunk.",
    )
    parser.add_argument(
        "--no-backup",
        action="store_true",
        help="Nao cria backup quando sobrescrever o chunks.jsonl de entrada.",
    )
    return parser.parse_args()


def main():
    args = parse_args()
    chunks_path = Path(args.chunks)
    output_path = Path(args.output) if args.output else chunks_path
    changed_output_path = Path(args.changed_output) if args.changed_output else None
    metadata_csv = Path(args.metadata_csv)

    enrich_chunks(
        chunks_path=chunks_path,
        metadata_csv=metadata_csv,
        output_path=output_path,
        changed_output_path=changed_output_path,
        metadata_fields=args.metadata_fields,
        create_backup=not args.no_backup,
    )


if __name__ == "__main__":
    main()
